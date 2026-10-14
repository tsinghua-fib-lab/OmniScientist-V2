"""Prepare grounded text and reusable visual carriers from one local PDF."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import image_assets

from posterlib.content.equations import (
    EquationRendererUnavailable,
    EquationSyntaxError,
    latex_to_mathml,
)
from posterlib.runtime.dependencies import PYMUPDF_REQUIREMENT, pymupdf_supported

_CAPTION_PATTERN = re.compile(
    r"^\s*(?:figure|fig\.?)\s*(\d+)(?![A-Za-z0-9])\s*[.:：]?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)
_CAPTION_LINE_PATTERN = re.compile(
    r"^\s*(?:figure|fig\.?)\s*(\d+)\s*[.:：\-–—]\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)
_TABLE_CARRIER_PATTERN = re.compile(
    r"^\s*table\s*(\d+)(?![A-Za-z0-9])\s*[.:：]?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)
_ALGORITHM_CARRIER_PATTERN = re.compile(
    r"^\s*algorithm\s*(\d+)(?![A-Za-z0-9])\s*[.:：]?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)
_PSEUDOCODE_STEP_PATTERN = re.compile(r"^\s*(\d+)\s*:")
_ABSTRACT_HEADING_PATTERN = re.compile(
    r"^abstract(?:\s*[\u2014\u2013:\-]\s*.*)?$",
    re.IGNORECASE,
)
_NON_AUTHOR_PATTERN = re.compile(
    r"(?:@|https?://|\b(?:abstract|academy|arxiv|center|centre|college|company|"
    r"conference|corporation|department|division|faculty|inc|institute|laborator(?:y|ies)|"
    r"ltd|permission|research|school|university)\b)",
    re.IGNORECASE,
)
_AUTHOR_MARKER_PATTERN = re.compile(r"[\s*∗†‡§¶⁰¹²³⁴⁵⁶⁷⁸⁹]+$")
_AFFILIATION_TYPE_PATTERN = re.compile(
    r"\b(?:academy|centre|center|college|company|department|faculty|inc\.?|"
    r"institute|laborator(?:y|ies)|research|school|university)\b",
    re.IGNORECASE,
)
_AFFILIATION_MARKER_PATTERN = re.compile(r"^[\s]*[0-9⁰¹²³⁴⁵⁶⁷⁸⁹]+(?=[A-Z])")
_ATTRIBUTION_STOP_PATTERN = re.compile(
    r"(?:correspondence\b|copyright\b|equal contribution\b|"
    r"proceedings of\b|published as\b|work was done\b)",
    re.IGNORECASE,
)
_TITLE_SIZE_MATCH_RATIO = 0.93
_MIN_TITLE_SCALE_RATIO = 1.15
_MAX_PDF_BYTES = 256 * 1024 * 1024
_MAX_PAGES = 500
_DEFAULT_MAX_FIGURE_CANDIDATES = 16
_MAX_STRUCTURED_EQUATIONS = 32
_MAX_STRUCTURED_EQUATION_CHARS = 600
_MAX_ARXIV_HTML_BYTES = 16 * 1024 * 1024
_FIGURE_RENDER_DPI = 450
_PDF_POINTS_PER_INCH = 72
_MIN_EMBEDDED_RASTER_COVERAGE = 0.8
_RASTER_MIME_EXTENSIONS = {
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_SVG_DATA_URI_RE = re.compile(
    r"(?P<prefix>data:image/[A-Za-z0-9.+-]+;base64,)"
    r"(?P<payload>[A-Za-z0-9+/=\s]+)",
    re.IGNORECASE,
)
_ARXIV_ID_PATTERN = re.compile(
    r"\barxiv\s*:\s*(\d{4}\.\d{4,5}(?:v\d+)?)\b",
    re.IGNORECASE,
)
_LATEX_LAYOUT_COMMAND = re.compile(
    r"\\vskip\s*[-+]?\d*\.?\d+\s*pt\s*",
    re.IGNORECASE,
)


class PaperSourceError(RuntimeError):
    """A PDF cannot be converted into a trustworthy poster source."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedPaper:
    """Text, metadata, and bounded visual carriers from a paper."""

    text: str
    title: str
    authors: str
    page_count: int
    figures: tuple[dict[str, Any], ...]
    equations: tuple[dict[str, str], ...] = ()
    affiliations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FigureAsset:
    """One prepared figure representation selected by the extraction cascade."""

    path: Path
    extraction_mode: str


def enrich_structured_equations(
    paper: PreparedPaper,
) -> tuple[PreparedPaper, tuple[str, ...]]:
    """Attach optional equation transcription shortcuts from matching arXiv HTML.

    The shortcut preserves published LaTeX and verifies that the HTML title matches
    the prepared PDF title. Network or parser failures leave normal source-grounded
    mathematical authoring available to the main model.
    """

    identifier = _primary_arxiv_identifier(paper.text)
    if not identifier:
        return paper, ()
    warnings: list[str] = []
    try:
        html_bytes = _download_arxiv_html(identifier)
        equations = _structured_equations_from_arxiv_html(
            html_bytes,
            expected_title=paper.title,
            warnings=warnings,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return paper, (
            (
                "Structured equation shortcuts were unavailable from the matching "
                f"paper source ({type(exc).__name__}: {exc})."
            ),
        )
    if not equations:
        return paper, tuple(warnings) or (
            (
                "The matching structured paper source exposed no equation shortcuts."
            ),
        )
    return replace(paper, equations=equations), tuple(warnings)


def _primary_arxiv_identifier(source_text: str) -> str:
    """Read only the paper's first-page arXiv stamp, never cited identifiers."""

    first_page = source_text.split("[Page 2]", 1)[0]
    match = _ARXIV_ID_PATTERN.search(first_page)
    return match.group(1) if match is not None else ""


def _download_arxiv_html(identifier: str) -> bytes:
    """Fetch one allowlisted arXiv HTML document with a bounded response."""

    if re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", identifier) is None:
        raise ValueError("arXiv identifier is invalid")
    request = urllib.request.Request(
        f"https://arxiv.org/html/{identifier}",
        headers={"User-Agent": "OmniScientist scientific-poster/1.0"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        content_type = str(response.headers.get("Content-Type") or "").casefold()
        if "html" not in content_type:
            raise ValueError("arXiv did not return HTML")
        content = response.read(_MAX_ARXIV_HTML_BYTES + 1)
    if len(content) > _MAX_ARXIV_HTML_BYTES:
        raise ValueError("arXiv HTML exceeds the structured-source limit")
    return content


def _structured_equations_from_arxiv_html(
    html_bytes: bytes,
    *,
    expected_title: str,
    warnings: list[str] | None = None,
) -> tuple[dict[str, str], ...]:
    """Extract source-authored display LaTeX with theorem/equation provenance."""

    try:
        from lxml import html as lxml_html
    except ImportError as exc:
        raise RuntimeError("lxml is required for structured equation extraction") from exc
    try:
        document = lxml_html.fromstring(html_bytes)
    except (TypeError, ValueError) as exc:
        raise ValueError("arXiv HTML could not be parsed") from exc
    title_nodes = document.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), "
        "' ltx_title_document ')]"
    )
    observed_title = _node_text(title_nodes[0]) if title_nodes else ""
    if (
        not observed_title
        or _canonical_identity_text(observed_title)
        != _canonical_identity_text(expected_title)
    ):
        raise ValueError("arXiv HTML title does not match the prepared PDF")

    equations: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    # LaTeXML may mark numbered equations inside its equation tables as inline.
    # Their structural equation owner, not that rendering hint, supplies provenance.
    selected_math = document.xpath(
        "//math[@alttext][@display='block' or ancestor::*["
        "contains(concat(' ', normalize-space(@class), ' '), ' ltx_equation ')]]"
    )
    units: dict[Any, list[Any]] = {}
    for math in selected_math:
        owner = _structured_equation_owner(math)
        units.setdefault(owner, []).append(math)

    for owner, math_nodes in units.items():
        latex = _structured_equation_latex(owner, math_nodes)
        if not latex or len(latex) > _MAX_STRUCTURED_EQUATION_CHARS:
            continue
        source_label, context = _structured_equation_provenance(owner)
        if not source_label:
            continue
        try:
            latex_to_mathml(latex)
        except (EquationRendererUnavailable, EquationSyntaxError) as exc:
            if warnings is not None:
                warnings.append(f"Structured {source_label} could not be rendered: {exc}")
            continue
        key = (latex, source_label)
        if key in seen:
            continue
        seen.add(key)
        equations.append(
            {
                "latex": latex,
                "source_label": source_label,
                "context": context,
            }
        )
        if len(equations) >= _MAX_STRUCTURED_EQUATIONS:
            break
    return tuple(equations)


def _structured_equation_owner(node: Any) -> Any:
    """Return the smallest complete source-owned equation container."""

    equation = None
    equation_group = None
    for ancestor in node.iterancestors():
        if equation is None and _has_class(ancestor, "ltx_equation"):
            equation = ancestor
        if _has_class(ancestor, "ltx_equationgroup"):
            equation_group = ancestor
            break
    if equation_group is not None:
        labels = equation_group.xpath(
            ".//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' ltx_tag_equation ')]"
        )
        if len(labels) <= 1:
            return equation_group
    return equation if equation is not None else node


def _structured_equation_latex(owner: Any, math_nodes: list[Any]) -> str:
    """Compose one equation unit without inventing mathematical operators."""

    parts = [
        _normalize_structured_latex(str(math.get("alttext") or ""))
        for math in math_nodes
    ]
    if not parts or any(not part for part in parts):
        return ""
    if len(parts) == 1:
        return parts[0]

    table_rows: list[list[str]] = []
    if str(owner.tag).casefold() in {"table", "tbody", "tr"}:
        part_by_math = dict(zip(math_nodes, parts, strict=True))
        row_nodes: list[Any] = []
        for math in math_nodes:
            row = next(
                (
                    ancestor
                    for ancestor in math.iterancestors()
                    if str(ancestor.tag).casefold() == "tr"
                ),
                None,
            )
            if row is None:
                table_rows = []
                break
            if row not in row_nodes:
                row_nodes.append(row)
        captured: set[Any] = set()
        for row in row_nodes:
            row_parts: list[str] = []
            for cell in row.xpath("./th|./td"):
                if (
                    _has_class(cell, "ltx_eqn_center_padleft")
                    or _has_class(cell, "ltx_eqn_center_padright")
                    or _has_class(cell, "ltx_eqn_eqno")
                    or _has_class(cell, "ltx_tag_equation")
                    or cell.xpath(
                        ".//*[contains(concat(' ', normalize-space(@class), ' '), "
                        "' ltx_tag_equation ')]"
                    )
                ):
                    continue
                cell_math = [
                    math
                    for math in cell.xpath(".//math[@alttext]")
                    if math in part_by_math
                ]
                captured.update(cell_math)
                row_parts.append(" ".join(part_by_math[math] for math in cell_math))
            table_rows.append(row_parts)
        if captured != set(math_nodes):
            table_rows = []
    if not table_rows:
        return " ".join(parts)

    columns = max(len(row) for row in table_rows)
    rows = [
        " & ".join(row + [""] * (columns - len(row)))
        for row in table_rows
    ]
    return (
        f"\\begin{{array}}{{{'l' * columns}}}"
        + r"\\".join(rows)
        + r"\end{array}"
    )


def _structured_equation_provenance(node: Any) -> tuple[str, str]:
    """Bind one display expression to its nearest theorem or numbered equation."""

    theorem = next(
        (
            ancestor
            for ancestor in node.iterancestors()
            if _has_class(ancestor, "ltx_theorem")
        ),
        None,
    )
    if theorem is not None:
        labels = theorem.xpath(
            ".//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' ltx_tag_theorem ')]"
        )
        source_label = _node_text(labels[0]) if labels else ""
        return source_label, _structured_context(theorem)

    equation = (
        node
        if _has_class(node, "ltx_equation") or _has_class(node, "ltx_equationgroup")
        else next(
            (
                ancestor
                for ancestor in node.iterancestors()
                if _has_class(ancestor, "ltx_equation")
            ),
            None,
        )
    )
    if equation is None:
        return "", ""
    labels = equation.xpath(
        ".//*[contains(concat(' ', normalize-space(@class), ' '), "
        "' ltx_tag_equation ')]"
    )
    equation_number = _node_text(labels[0]) if labels else ""
    if not equation_number:
        return "", ""
    context_node = equation.getprevious()
    while context_node is not None and not _node_text(context_node):
        context_node = context_node.getprevious()
    context = _structured_context(context_node) if context_node is not None else ""
    return f"Equation {equation_number}", context


def _has_class(node: Any, value: str) -> bool:
    return value in str(node.get("class") or "").split()


def _structured_context(node: Any) -> str:
    if node is None:
        return ""
    parts: list[str] = []

    def collect(element: Any) -> None:
        if str(element.tag).casefold() == "math":
            parts.append(str(element.get("alttext") or ""))
            return
        if _has_class(element, "ltx_tag_equation"):
            return
        if element.text:
            parts.append(str(element.text))
        for child in element:
            collect(child)
            if child.tail:
                parts.append(str(child.tail))

    collect(node)
    return _clean_html_text(" ".join(parts))[:700]


def _node_text(node: Any) -> str:
    return _clean_html_text(" ".join(str(node.text_content()).split()))


def _clean_html_text(value: str) -> str:
    text = " ".join(value.split())
    return re.sub(r"\s+([,.;:])", r"\1", text).strip()


def _canonical_identity_text(value: str) -> str:
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", value)
        if character.isalnum()
    )


def _normalize_structured_latex(value: str) -> str:
    latex = _LATEX_LAYOUT_COMMAND.sub("", value.strip())
    return " ".join(latex.split())


def prepare_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
) -> PreparedPaper:
    """Extract text and a bounded, document-wide set of native figures."""

    try:
        import pymupdf
    except ImportError as exc:
        raise PaperSourceError(
            "missing_capability",
            "PDF ingestion requires the optional pymupdf package.",
        ) from exc
    if not pymupdf_supported(pymupdf):
        raise PaperSourceError(
            "missing_capability",
            f"PDF ingestion requires {PYMUPDF_REQUIREMENT}.",
        )

    source = Path(pdf_path).expanduser()
    _validate_pdf_file(source)
    destination = Path(output_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)

    try:
        document = pymupdf.open(source)
    except Exception as exc:  # PyMuPDF raises several format-specific errors
        raise PaperSourceError(
            "source_read_failed", f"Could not open PDF: {exc}"
        ) from exc

    try:
        if document.page_count > _MAX_PAGES:
            raise PaperSourceError(
                "source_too_large",
                f"PDF has {document.page_count} pages; the limit is {_MAX_PAGES}.",
            )
        metadata = document.metadata or {}
        fallback_title, fallback_authors, affiliations = _first_page_identity(
            document[0]
        )
        title = fallback_title or str(metadata.get("title") or "").strip()
        authors = fallback_authors or str(metadata.get("author") or "").strip()
        running_headers = _running_header_rects(document, pymupdf)
        pages: list[str] = []
        figure_candidates: list[dict[str, Any]] = []
        caption_rects_by_page: dict[int, tuple[Any, ...]] = {}
        for page_index, page in enumerate(document):
            page_text = _page_text(page)
            if page_text:
                pages.append(f"[Page {page_index + 1}]\n{page_text}")
            captions = _caption_blocks(page, pymupdf)
            caption_rects_by_page[page_index] = tuple(
                caption["rect"] for caption in captions
            )
            figure_candidates.extend(
                {
                    **caption,
                    "page_index": page_index,
                }
                for caption in captions
            )
        text = "\n\n".join(pages).strip()
        if not text:
            raise PaperSourceError(
                "source_read_failed",
                "The PDF contains no extractable text; OCR is not provided by this Skill.",
            )
        selected_candidates = _select_figure_candidates(
            figure_candidates,
            max_candidates=_DEFAULT_MAX_FIGURE_CANDIDATES,
        )
        warnings = ()
        if len(selected_candidates) < len(figure_candidates):
            warnings = (
                "PDF figure materialization budget retained "
                f"{len(selected_candidates)} of {len(figure_candidates)} captioned candidates; "
                "document-wide coverage selection was used.",
            )
        for candidate in selected_candidates:
            candidate["context"] = _figure_context(
                text,
                int(candidate["figure_number"]),
            )
        figures: list[dict[str, Any]] = []
        for candidate in selected_candidates:
            figures.extend(
                _extract_captioned_figures(
                    document[int(candidate["page_index"])],
                    destination,
                    captions=[candidate],
                    page_index=int(candidate["page_index"]),
                    pymupdf=pymupdf,
                    excluded_text_rects=running_headers.get(
                        int(candidate["page_index"]), ()
                    ),
                    page_caption_rects=caption_rects_by_page[
                        int(candidate["page_index"])
                    ],
                )
            )
        return PreparedPaper(
            text=text,
            title=title,
            authors=authors,
            page_count=document.page_count,
            figures=tuple(figures),
            affiliations=affiliations,
            warnings=warnings,
        )
    except PaperSourceError:
        raise
    except Exception as exc:
        raise PaperSourceError(
            "source_read_failed",
            f"PDF extraction failed: {exc}",
        ) from exc
    finally:
        document.close()


def _page_text(page: Any) -> str:
    """Preserve text-block line structure instead of flattening display math."""

    blocks = page.get_text("blocks", sort=True)
    return "\n".join(
        text
        for block in blocks
        if len(block) >= 5
        and (len(block) < 7 or int(block[6]) == 0)
        and (text := str(block[4]).strip())
    )


def _validate_pdf_file(path: Path) -> None:
    if not path.is_file():
        raise PaperSourceError("source_not_found", f"PDF was not found: {path}")
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            signature = handle.read(5)
    except OSError as exc:
        raise PaperSourceError(
            "source_read_failed", f"Could not read PDF: {exc}"
        ) from exc
    if size > _MAX_PDF_BYTES:
        raise PaperSourceError(
            "source_too_large",
            f"PDF is larger than {_MAX_PDF_BYTES // (1024 * 1024)} MiB.",
        )
    if signature != b"%PDF-":
        raise PaperSourceError(
            "source_read_failed", "The supplied file is not a PDF document."
        )


def _first_page_identity(page: Any) -> tuple[str, str, tuple[str, ...]]:
    """Recover high-confidence title, authors, and affiliations in one pass."""

    lines = _horizontal_text_lines(page)
    abstract = next(
        (line for line in lines if _ABSTRACT_HEADING_PATTERN.fullmatch(line["text"])),
        None,
    )
    page_height = float(page.rect.height)
    identity_bottom = (
        float(abstract["bbox"][1]) if abstract is not None else page_height * 0.35
    )
    title_candidates = [
        line
        for line in lines
        if float(line["bbox"][1]) < identity_bottom
        and float(line["bbox"][1]) < page_height * 0.55
        and _has_letters(line["text"])
        and "@" not in line["text"]
    ]
    if not title_candidates:
        return "", "", ()

    largest_size = max(float(line["size"]) for line in title_candidates)
    title_lines = [
        line
        for line in title_candidates
        if float(line["size"]) >= largest_size * _TITLE_SIZE_MATCH_RATIO
    ]
    supporting_sizes = [
        float(line["size"])
        for line in title_candidates
        if line not in title_lines and float(line["size"]) > 0
    ]
    if (
        not supporting_sizes
        or largest_size < max(supporting_sizes) * _MIN_TITLE_SCALE_RATIO
    ):
        return "", "", ()
    title_lines.sort(key=lambda line: (float(line["bbox"][1]), float(line["bbox"][0])))
    title = _join_wrapped_identity_lines([str(line["text"]) for line in title_lines])
    title_bottom = max(float(line["bbox"][3]) for line in title_lines)
    if abstract is None:
        return title, "", ()

    identity_lines = [
        line
        for line in lines
        if title_bottom < float(line["bbox"][1]) < identity_bottom
    ]
    bold_author_lines = [
        line
        for line in identity_lines
        if _looks_like_author_copy(str(line["bold_text"]))
    ]
    author_lines = bold_author_lines or [
        line
        for line in identity_lines
        if _looks_like_unstyled_author_copy(str(line["text"]))
    ]
    author_lines.sort(
        key=lambda line: (round(float(line["bbox"][1]), 1), float(line["bbox"][0]))
    )
    authors = ", ".join(
        _clean_author_copy(str(line["text"]))
        for line in author_lines
    )
    if not author_lines:
        return title, authors, ()
    last_author_top = max(float(line["bbox"][1]) for line in author_lines)
    direct = [
        str(line["text"])
        for line in lines
        if last_author_top < float(line["bbox"][1]) < identity_bottom
        and _has_affiliation_evidence(str(line["text"]))
    ]
    affiliations = _clean_affiliation_parts(direct, join_wrapped=False)
    if not affiliations:
        affiliations = _footer_affiliations(lines, page=page)
    return title, authors, affiliations


def _has_affiliation_evidence(value: str, *, require_marker: bool = False) -> bool:
    """Recognize institution names before an inline contact or attribution tail."""

    normalized = _ATTRIBUTION_STOP_PATTERN.split(
        _normalize_identity_text(value), maxsplit=1,
    )[0].strip(" ,;.*")
    if not normalized or "@" in normalized:
        return False
    marked = bool(_AFFILIATION_MARKER_PATTERN.match(normalized))
    if require_marker and not marked:
        return False
    without_marker = _AFFILIATION_MARKER_PATTERN.sub("", normalized).strip()
    if _AFFILIATION_TYPE_PATTERN.search(without_marker):
        return True
    return bool(
        marked
        and all(
            len(part) <= 40
            and re.fullmatch(r"[A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*){0,3}", part)
            for part in _clean_affiliation_parts([normalized], join_wrapped=False)
        )
    )


def _footer_affiliations(lines: list[dict[str, Any]], *, page: Any) -> tuple[str, ...]:
    """Read one marked affiliation footnote without crossing into body or venue text."""

    page_height = float(page.rect.height)
    page_width = float(page.rect.width)
    ordered = sorted(
        lines,
        key=lambda line: (round(float(line["bbox"][1])), float(line["bbox"][0])),
    )
    start_index = next(
        (
            index
            for index, line in enumerate(ordered)
            if float(line["bbox"][1]) >= page_height * 0.72
            and float(line["bbox"][0]) < page_width * 0.5
            and _has_affiliation_evidence(
                str(line["text"]),
                require_marker=True,
            )
        ),
        None,
    )
    if start_index is None:
        return ()
    run: list[str] = []
    previous_bottom: float | None = None
    for line in ordered[start_index:]:
        x0, y0, _x1, y1 = (float(value) for value in line["bbox"])
        if x0 >= page_width * 0.5:
            continue
        text = _normalize_identity_text(str(line["text"]))
        stop = _ATTRIBUTION_STOP_PATTERN.search(text)
        if stop is not None or "@" in text:
            prefix = text[: stop.start()] if stop is not None else ""
            if prefix.strip(" ,;."):
                run.append(prefix)
            break
        if previous_bottom is not None and y0 - previous_bottom > 18.0:
            break
        run.append(text)
        previous_bottom = max(previous_bottom or y1, y1)
    return _clean_affiliation_parts(run, join_wrapped=True)


def _clean_affiliation_parts(
    values: list[str],
    *,
    join_wrapped: bool,
) -> tuple[str, ...]:
    """Remove affiliation markers and dehyphenate only bounded identity lines."""

    raw_values = [_join_wrapped_identity_lines(values)] if join_wrapped else values
    parts: list[str] = []
    marker = r"[0-9⁰¹²³⁴⁵⁶⁷⁸⁹]+"
    for raw in raw_values:
        normalized = _normalize_identity_text(raw)
        normalized = re.sub(
            rf"(?<![A-Za-z0-9]){marker}(?=[A-Z])",
            "; ",
            normalized,
        )
        for part in normalized.split(";"):
            value = part.strip(" ,;.*")
            if not value or "@" in value or _ATTRIBUTION_STOP_PATTERN.match(value):
                continue
            parts.append(value)
    deduplicated: list[str] = []
    seen: set[str] = set()
    for value in parts:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(value)
    return tuple(deduplicated)


def _join_wrapped_identity_lines(lines: list[str]) -> str:
    """Join cover identity lines while removing PDF line-break hyphenation."""

    joined = ""
    for raw_line in lines:
        line = _normalize_identity_text(raw_line)
        if not line:
            continue
        if joined and re.search(r"[A-Za-z]-$", joined) and re.match(r"[a-z]", line):
            joined = joined[:-1] + line
        else:
            joined = f"{joined} {line}".strip()
    return _normalize_identity_text(joined)


def _normalize_identity_text(value: str) -> str:
    """Compose spacing diacritics that PDF fonts emit before their base letter."""

    characters = list(str(value))
    normalized: list[str] = []
    index = 0
    while index < len(characters):
        character = characters[index]
        decomposition = unicodedata.normalize("NFKD", character)
        combining_marks = [
            item for item in decomposition if unicodedata.combining(item)
        ]
        spacing_mark = bool(combining_marks) and all(
            item.isspace() or unicodedata.combining(item)
            for item in decomposition
        )
        next_index = index + 1
        while next_index < len(characters) and characters[next_index].isspace():
            next_index += 1
        if (
            spacing_mark
            and next_index < len(characters)
            and characters[next_index].isalpha()
        ):
            while normalized and normalized[-1].isspace():
                normalized.pop()
            normalized.append(characters[next_index])
            normalized.extend(combining_marks)
            index = next_index + 1
            continue
        normalized.append(character)
        index += 1
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFC", "".join(normalized)),
    ).strip()


def _horizontal_text_lines(page: Any) -> list[dict[str, Any]]:
    """Return horizontal first-page lines with dominant and bold text metadata."""

    try:
        blocks = page.get_text("dict", sort=True).get("blocks", [])
    except Exception:
        return []
    lines: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            direction = line.get("dir", (1.0, 0.0))
            if len(direction) != 2 or float(direction[0]) < 0.9:
                continue
            spans = [
                span for span in line.get("spans", []) if str(span.get("text", ""))
            ]
            if not spans:
                continue
            text = _normalize_identity_text(
                "".join(str(span["text"]) for span in spans)
            )
            if not text:
                continue
            size = max(float(span.get("size") or 0.0) for span in spans)
            bold_text = _normalize_identity_text(
                "".join(
                    str(span["text"])
                    for span in spans
                    if int(span.get("flags") or 0) & 16
                    and float(span.get("size") or 0.0) >= size * 0.8
                )
            )
            lines.append(
                {
                    "text": text,
                    "bold_text": bold_text,
                    "size": size,
                    "bbox": tuple(
                        line.get("bbox") or block.get("bbox") or (0, 0, 0, 0)
                    ),
                }
            )
    return lines


def _has_letters(value: str) -> bool:
    return any(character.isalpha() for character in value)


def _looks_like_author_copy(value: str) -> bool:
    cleaned = _clean_author_copy(value).strip(" ,;")
    if not cleaned or len(cleaned) > 300 or _NON_AUTHOR_PATTERN.search(cleaned):
        return False
    words = [token.strip(".,;:()[]{}") for token in cleaned.split()]
    return sum(any(character.isalpha() for character in word) for word in words) >= 2


def _looks_like_unstyled_author_copy(value: str) -> bool:
    """Recognize an unbolded name line without admitting affiliation prose."""

    cleaned = _clean_author_copy(value).strip(" ,;")
    if not _looks_like_author_copy(cleaned) or any(char.isdigit() for char in cleaned):
        return False
    parts = [
        part.strip()
        for part in re.split(r"\s*(?:,|;|&|\band\b)\s*", cleaned)
        if part.strip()
    ]
    if not parts:
        return False
    particles = {"al", "da", "de", "del", "der", "di", "la", "van", "von"}
    for part in parts:
        words = [word.strip(".()[]{}") for word in part.split() if word.strip()]
        if not 1 <= len(words) <= 4:
            return False
        for word in words:
            letters = "".join(char for char in word if char.isalpha())
            if not letters or letters.casefold() in particles:
                continue
            if not letters[0].isupper() or (len(letters) > 1 and letters.isupper()):
                return False
    return True


def _clean_author_copy(value: str) -> str:
    """Remove affiliation markers while preserving the visible author names."""

    markers = r"0-9⁰¹²³⁴⁵⁶⁷⁸⁹"
    cleaned = re.sub(
        rf"(?<=[A-Za-z])[{markers}]+(?:\s*,\s*[{markers}]+)*",
        ",",
        value,
    )
    cleaned = re.sub(
        rf"(?<!\w)[{markers}]+(?:\s+[{markers}]+)*(?!\w)",
        ",",
        cleaned,
    )
    cleaned = re.sub(r"[*∗†‡§¶]+", " ", cleaned)
    cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
    cleaned = re.sub(r"(?:,\s*){2,}", ", ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = _AUTHOR_MARKER_PATTERN.sub("", cleaned).strip(" ,;")
    return ", ".join(
        part.strip()
        for part in cleaned.split(",")
        if any(character.isalpha() for character in part)
    )


def _running_header_rects(document: Any, pymupdf: Any) -> dict[int, tuple[Any, ...]]:
    """Locate repeated top-margin text that cannot belong to one figure."""

    occurrences: dict[str, list[tuple[int, Any, tuple[float, float, float]]]] = {}
    for page_index, page in enumerate(document):
        top_limit = page.rect.y0 + page.rect.height * 0.14
        minimum_width = page.rect.width * 0.12
        for line in _horizontal_text_lines(page):
            text = " ".join(str(line.get("text") or "").split()).casefold()
            rect = pymupdf.Rect(line["bbox"])
            if len(text) < 12 or rect.width < minimum_width or rect.y1 > top_limit:
                continue
            geometry = (
                rect.x0 / page.rect.width,
                rect.y0 / page.rect.height,
                rect.height / page.rect.height,
            )
            occurrences.setdefault(text, []).append((page_index, rect, geometry))
    # Running heads may alternate between paper title and authors on facing pages.
    minimum_pages = max(3, (document.page_count - 1) // 2)
    repeated = {
        text
        for text, entries in occurrences.items()
        if len({page_index for page_index, _rect, _geometry in entries})
        >= minimum_pages
        and _stable_top_line_geometry(entries)
    }
    by_page: dict[int, list[Any]] = {}
    for text in repeated:
        for page_index, rect, _geometry in occurrences[text]:
            page_rect = document[page_index].rect
            by_page.setdefault(page_index, []).append(
                pymupdf.Rect(page_rect.x0, rect.y0, page_rect.x1, rect.y1)
            )
    return {page_index: tuple(rects) for page_index, rects in by_page.items()}


def _stable_top_line_geometry(
    entries: list[tuple[int, Any, tuple[float, float, float]]],
) -> bool:
    """Require repeated header text to occupy the same normalized page position."""

    coordinates = tuple(zip(*(geometry for _page, _rect, geometry in entries), strict=True))
    return all(
        max(values) - min(values) <= tolerance
        for values, tolerance in zip(coordinates, (0.015, 0.005, 0.003), strict=True)
    )


def _extract_captioned_figures(
    page: Any,
    output_dir: Path,
    *,
    captions: list[dict[str, Any]],
    page_index: int,
    pymupdf: Any,
    page_caption_rects: tuple[Any, ...],
    excluded_text_rects: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    for caption in captions:
        region = _figure_region(
            page,
            caption["rect"],
            pymupdf,
            excluded_text_rects=excluded_text_rects,
            page_caption_rects=page_caption_rects,
        )
        if region is None:
            continue
        number = int(caption["figure_number"])
        # A page can repeat a figure number for distinct views. Keep each
        # physical caption's file separate so its recorded hash remains true.
        caption_key = repr(tuple(caption["rect"])).encode("utf-8")
        caption_digest = hashlib.sha256(caption_key).hexdigest()[:12]
        stem = f"figure-{number}-page-{page_index + 1}-{caption_digest}"
        asset = _write_visual_region(page, region, output_dir / stem, pymupdf)
        if asset is None:
            continue
        extracted.append(
            {
                "path": str(asset.path),
                "page": page_index + 1,
                "figure_number": number,
                "caption": caption["caption"],
                "caption_bbox": list(caption["rect"]),
                "context": str(caption.get("context") or ""),
                "extraction_mode": asset.extraction_mode,
                "crop_bbox": [round(value, 2) for value in tuple(region)],
                "sha256": hashlib.sha256(asset.path.read_bytes()).hexdigest(),
                "source_kind": "pdf_figure",
                "carrier_number": number,
                "carrier_label": f"Figure {number}",
            }
        )
    return extracted


def _extract_layout_visual_carriers(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    layout: dict[str, Any],
    source_text: str,
) -> tuple[dict[str, Any], ...]:
    """Extract bounded non-Figure visual blocks identified by physical PDF layout.

    A Table label is admitted only when it anchors a nearby ``picture`` region;
    a normal layout ``table`` remains structured source text. An Algorithm label
    owns only the contiguous block before the next section boundary. These
    physical ownership rules avoid inferring scientific roles from paper wording.
    """

    try:
        import pymupdf
    except ImportError:
        return ()
    destination = Path(output_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    pages = layout.get("pages")
    if not isinstance(pages, list):
        return ()
    candidates: dict[str, list[dict[str, Any]]] = {
        "pdf_table": [],
        "pdf_algorithm": [],
    }
    try:
        document = pymupdf.open(Path(pdf_path).expanduser())
    except (OSError, RuntimeError, ValueError):
        return ()
    try:
        for raw_page in pages:
            if not isinstance(raw_page, dict):
                continue
            try:
                page_number = int(raw_page.get("page_number"))
                page = document[page_number - 1]
            except (IndexError, TypeError, ValueError):
                continue
            boxes = raw_page.get("boxes")
            if not isinstance(boxes, list):
                continue
            for index, box in enumerate(boxes):
                if not isinstance(box, dict):
                    continue
                boxclass = str(box.get("boxclass") or "").casefold()
                text = _layout_box_text(box)
                table_match = (
                    _TABLE_CARRIER_PATTERN.match(text)
                    if boxclass == "caption"
                    else None
                )
                if table_match is not None:
                    region = _table_picture_region(boxes, index, pymupdf)
                    if region is not None:
                        candidates["pdf_table"].append(
                            {
                                "page": page,
                                "page_number": page_number,
                                "number": int(table_match.group(1)),
                                "caption": table_match.group(2).strip()[:600],
                                "anchor": pymupdf.Rect(box["x0"], box["y0"], box["x1"], box["y1"]),
                                "region": region,
                            }
                        )
                algorithm_lines = _layout_box_lines(box)
                algorithm_match = None
                if (
                    boxclass in {"caption", "section-header", "text"}
                    and len(algorithm_lines) == 1
                ):
                    candidate_match = _ALGORITHM_CARRIER_PATTERN.match(
                        algorithm_lines[0]
                    )
                    if (
                        candidate_match is not None
                        and candidate_match.group(2).strip()
                    ):
                        algorithm_match = candidate_match
                if algorithm_match is not None:
                    region = _algorithm_block_region(boxes, index, pymupdf)
                    if region is not None:
                        candidates["pdf_algorithm"].append(
                            {
                                "page": page,
                                "page_number": page_number,
                                "number": int(algorithm_match.group(1)),
                                "caption": algorithm_match.group(2).strip()[:600],
                                "anchor": pymupdf.Rect(box["x0"], box["y0"], box["x1"], box["y1"]),
                                "region": region,
                            }
                        )

        extracted: list[dict[str, Any]] = []
        for source_kind in ("pdf_table", "pdf_algorithm"):
            for candidate in candidates[source_kind]:
                prepared = _write_layout_visual_carrier(
                    candidate,
                    output_dir=destination,
                    source_kind=source_kind,
                    source_text=source_text,
                    pymupdf=pymupdf,
                )
                if prepared is not None:
                    extracted.append(prepared)
        return tuple(extracted)
    finally:
        document.close()


def _layout_box_text(box: dict[str, Any]) -> str:
    return " ".join(_layout_box_lines(box))


def _layout_box_lines(box: dict[str, Any]) -> list[str]:
    """Preserve physical line starts used by formal visual carriers."""

    lines: list[str] = []
    for line in box.get("textlines") or []:
        if not isinstance(line, dict):
            continue
        text = " ".join(
            str(span.get("text") or "").strip()
            for span in line.get("spans") or []
            if isinstance(span, dict) and str(span.get("text") or "").strip()
        )
        if text:
            lines.append(text)
    return lines


def _layout_box_rect(box: Any, pymupdf: Any) -> Any | None:
    if not isinstance(box, dict):
        return None
    try:
        rect = pymupdf.Rect(box["x0"], box["y0"], box["x1"], box["y1"])
    except (KeyError, TypeError, ValueError):
        return None
    return None if rect.is_empty else rect


def _table_picture_region(
    boxes: list[Any], anchor_index: int, pymupdf: Any
) -> Any | None:
    """Return the nearest picture group physically owned by one Table caption."""

    anchor = _layout_box_rect(boxes[anchor_index], pymupdf)
    if anchor is None:
        return None
    pictures: list[tuple[float, Any]] = []
    for box in boxes:
        if not isinstance(box, dict) or box.get("boxclass") != "picture":
            continue
        rect = _layout_box_rect(box, pymupdf)
        if rect is None:
            continue
        overlap = max(0.0, min(anchor.x1, rect.x1) - max(anchor.x0, rect.x0))
        if overlap < min(anchor.width, rect.width) * 0.5:
            continue
        if rect.y1 > anchor.y0:
            continue
        if any(
            other_index != anchor_index
            and isinstance(other, dict)
            and other.get("boxclass") == "caption"
            and (other_rect := _layout_box_rect(other, pymupdf)) is not None
            and rect.y1 <= other_rect.y0
            and other_rect.y1 <= anchor.y0
            for other_index, other in enumerate(boxes)
        ):
            continue
        gap = anchor.y0 - rect.y1
        if 0 <= gap <= 48:
            pictures.append((gap, rect))
    if not pictures:
        return None
    nearest_gap, nearest = min(pictures, key=lambda item: item[0])
    region = pymupdf.Rect(nearest)
    for gap, peer in pictures:
        if gap > nearest_gap + 22:
            continue
        region |= peer
    if region.width < 80 or region.height < 30:
        return None
    return region


def _algorithm_block_region(
    boxes: list[Any], anchor_index: int, pymupdf: Any
) -> Any | None:
    """Return the shortest contiguous block with line-level pseudocode structure."""

    anchor = _layout_box_rect(boxes[anchor_index], pymupdf)
    if anchor is None:
        return None
    body: list[Any] = []
    pending_prefix: list[Any] = []
    has_step = False
    for box in boxes[anchor_index + 1 :]:
        if not isinstance(box, dict):
            continue
        boxclass = str(box.get("boxclass") or "").casefold()
        rect = _layout_box_rect(box, pymupdf)
        if rect is None or rect.y0 < anchor.y0:
            continue
        if boxclass in {"caption", "page-footer", "page-header", "section-header"}:
            break
        if boxclass == "formula":
            # Do not turn the equation-image restriction into a truncated
            # algorithm asset. Keep its complete source text and structured math
            # available for native authoring instead of publishing a prefix.
            return None
        if boxclass not in {"list-item", "text"}:
            break
        lines = _layout_box_lines(box)
        structured = boxclass == "list-item" or any(
            _PSEUDOCODE_STEP_PATTERN.match(line) for line in lines
        )
        if not has_step:
            pending_prefix.append(rect)
            if structured:
                body.extend(pending_prefix)
                pending_prefix.clear()
                has_step = True
            continue
        if not structured:
            break
        body.append(rect)
    if not body or not has_step:
        return None
    region = pymupdf.Rect(anchor)
    for rect in body:
        region |= rect
    return region


def _write_layout_visual_carrier(
    candidate: dict[str, Any],
    *,
    output_dir: Path,
    source_kind: str,
    source_text: str,
    pymupdf: Any,
) -> dict[str, Any] | None:
    page = candidate["page"]
    anchor = candidate["anchor"]
    region = pymupdf.Rect(candidate["region"])
    region = pymupdf.Rect(
        region.x0 - 6,
        region.y0 - 6,
        region.x1 + 6,
        region.y1 + 3,
    ) & page.rect
    number = int(candidate["number"])
    if source_kind == "pdf_table":
        label_kind = "Table"
    elif source_kind == "pdf_algorithm":
        label_kind = "Algorithm"
    else:
        raise ValueError(f"unsupported layout visual carrier: {source_kind}")
    caption_key = repr(tuple(anchor)).encode("utf-8")
    anchor_digest = hashlib.sha256(caption_key).hexdigest()[:12]
    stem = (
        f"{label_kind.casefold()}-{number}-page-{candidate['page_number']}-"
        f"{anchor_digest}"
    )
    asset = _write_visual_region(page, region, output_dir / stem, pymupdf)
    if asset is None:
        return None
    label = f"{label_kind} {number}"
    return {
        "path": str(asset.path),
        "page": int(candidate["page_number"]),
        "carrier_number": number,
        "carrier_label": label,
        "caption": str(candidate.get("caption") or ""),
        "context": _carrier_context(source_text, label_kind, number),
        "source_kind": source_kind,
        "extraction_mode": asset.extraction_mode,
        "crop_bbox": [round(value, 2) for value in tuple(region)],
        "sha256": hashlib.sha256(asset.path.read_bytes()).hexdigest(),
    }


def _select_figure_candidates(
    candidates: list[dict[str, Any]],
    *,
    max_candidates: int,
) -> list[dict[str, Any]]:
    """Apply one caller-owned materialization budget across the whole paper."""

    if not candidates:
        return []
    ordered = sorted(
        candidates,
        key=lambda item: (
            int(item.get("page_index") or 0),
            int(item.get("figure_number") or 0),
        ),
    )
    if max_candidates <= 0:
        return []
    if len(ordered) <= max_candidates:
        return ordered

    if max_candidates == 1:
        selected_indices = {(len(ordered) - 1) // 2}
    else:
        selected_indices = {
            round(slot * (len(ordered) - 1) / (max_candidates - 1))
            for slot in range(max_candidates)
        }
    return [ordered[index] for index in sorted(selected_indices)]


def _write_visual_region(
    page: Any,
    region: Any,
    output_stem: Path,
    pymupdf: Any,
) -> _FigureAsset | None:
    """Materialize one PDF region with the shared lossless-first fallback order."""

    asset = _write_embedded_raster(page, region, output_stem, pymupdf)
    if asset is None:
        asset = _write_vector_clip(
            page,
            region,
            output_stem.with_suffix(".svg"),
            pymupdf,
        )
    if asset is None:
        asset = _write_raster_fallback(
            page,
            region,
            output_stem.with_suffix(".png"),
            pymupdf,
        )
    return asset


def _write_embedded_raster(
    page: Any,
    region: Any,
    output_stem: Path,
    pymupdf: Any,
) -> _FigureAsset | None:
    """Extract one covering raster XObject without rendering it again."""

    try:
        candidates = []
        for image in page.get_image_info(xrefs=True):
            xref = int(image.get("xref") or 0)
            rect = pymupdf.Rect(image["bbox"])
            if xref <= 0 or rect.is_empty or not rect.intersects(region):
                continue
            overlap = rect & region
            if overlap.get_area() < rect.get_area() * 0.98:
                continue
            candidates.append((xref, rect))
        if len(candidates) != 1:
            return None
        xref, image_rect = candidates[0]
        if image_rect.get_area() < region.get_area() * _MIN_EMBEDDED_RASTER_COVERAGE:
            return None
        if _region_has_vector_or_text_overlay(page, region, pymupdf):
            return None
        document = page.parent
        image = document.extract_image(xref)
        content = image.get("image")
        if not isinstance(content, bytes):
            return None
        mime = image_assets.image_asset_mime(content)
        extension = _RASTER_MIME_EXTENSIONS.get(mime or "")
        if extension is None:
            return None
        width = int(image.get("width") or 0)
        height = int(image.get("height") or 0)
        if width <= 0 or height <= 0:
            return None
        output_path = output_stem.with_suffix(f".{extension}")
        output_path.write_bytes(content)
        return _FigureAsset(output_path, "embedded-raster")
    except Exception:
        return None


def _region_has_vector_or_text_overlay(page: Any, region: Any, pymupdf: Any) -> bool:
    """Return whether lossless raster extraction would omit visible overlays."""

    try:
        for _drawing, rect in _visible_page_drawings(page, pymupdf):
            if rect.width >= 1 and rect.height >= 1 and rect.intersects(region):
                return True
    except Exception:
        return True
    try:
        for block in page.get_text("blocks", sort=True):
            if len(block) < 7 or block[6] != 0:
                continue
            text = re.sub(r"\s+", " ", str(block[4])).strip()
            rect = pymupdf.Rect(block[:4])
            if text and rect.intersects(region):
                return True
    except Exception:
        return True
    return False


def _visible_page_drawings(page: Any, pymupdf: Any) -> list[tuple[dict[str, Any], Any]]:
    """Return painted vector bounds after applying their active PDF clip stack."""

    active_clips: list[tuple[int, Any]] = []
    visible: list[tuple[dict[str, Any], Any]] = []
    for drawing in page.get_drawings(extended=True):
        level = int(drawing.get("level") or 0)
        active_clips = [
            (clip_level, clip)
            for clip_level, clip in active_clips
            if clip_level < level
        ]
        drawing_type = str(drawing.get("type") or "")
        if drawing_type in {"clip", "group"}:
            scissor = drawing.get("scissor")
            if scissor is not None:
                clip = pymupdf.Rect(scissor)
                if not clip.is_empty:
                    active_clips.append((level, clip))
            continue
        rect_value = drawing.get("rect")
        if rect_value is None:
            continue
        rect = pymupdf.Rect(rect_value)
        clipped_out = False
        for _clip_level, clip in active_clips:
            x0 = max(rect.x0, clip.x0)
            y0 = max(rect.y0, clip.y0)
            x1 = min(rect.x1, clip.x1)
            y1 = min(rect.y1, clip.y1)
            if x1 < x0 or y1 < y0:
                clipped_out = True
                break
            rect = pymupdf.Rect(x0, y0, x1, y1)
        if not clipped_out and (rect.width > 0 or rect.height > 0):
            visible.append((drawing, rect))
    return visible


def _write_vector_clip(
    page: Any,
    region: Any,
    output_path: Path,
    pymupdf: Any,
) -> _FigureAsset | None:
    """Preserve a mixed or vector caption region as a self-contained SVG."""

    clipped_document = None
    try:
        source_document = page.parent
        page_number = int(page.number)
        clipped_document = pymupdf.open()
        clipped_page = clipped_document.new_page(
            width=float(region.width),
            height=float(region.height),
        )
        clipped_page.show_pdf_page(
            clipped_page.rect,
            source_document,
            page_number,
            clip=region,
        )
        svg_text = clipped_page.get_svg_image(text_as_path=1)
        svg_bytes = _normalize_svg_data_uris(svg_text).encode("utf-8")
        if not image_assets.svg_asset_is_safe(svg_bytes):
            return None
        output_path.write_bytes(svg_bytes)
        return _FigureAsset(output_path, "vector-clip")
    except Exception:
        return None
    finally:
        if clipped_document is not None:
            clipped_document.close()


def _normalize_svg_data_uris(svg_text: str) -> str:
    """Remove whitespace that MuPDF inserts inside embedded base64 payloads."""

    def compact(match: re.Match[str]) -> str:
        payload = re.sub(r"\s+", "", match.group("payload"))
        return match.group("prefix") + payload

    return _SVG_DATA_URI_RE.sub(compact, svg_text)


def _write_raster_fallback(
    page: Any,
    region: Any,
    output_path: Path,
    pymupdf: Any,
) -> _FigureAsset | None:
    """Render a high-resolution PNG only when native extraction cannot succeed."""

    try:
        scale = _FIGURE_RENDER_DPI / _PDF_POINTS_PER_INCH
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            clip=region,
            alpha=False,
        )
        pixmap.save(output_path)
        return _FigureAsset(output_path, "raster-fallback")
    except Exception:
        return None


def _caption_blocks(page: Any, pymupdf: Any) -> list[dict[str, Any]]:
    captions: list[dict[str, Any]] = []
    try:
        blocks = page.get_text("dict", sort=True).get("blocks", [])
    except Exception:
        return captions
    lines: list[tuple[str, Any]] = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        lines.extend(
            (
                re.sub(
                    r"\s+",
                    " ",
                    "".join(
                        str(span.get("text") or "")
                        for span in line.get("spans", [])
                    ),
                ).strip(),
                pymupdf.Rect(line["bbox"]),
            )
            for line in block.get("lines", [])
            if line.get("bbox")
        )
    for line_index, (text, rect) in enumerate(lines):
        match = _CAPTION_LINE_PATTERN.match(text)
        if match is None:
            continue
        caption_rect = pymupdf.Rect(rect)
        caption_text = match.group(2).strip()
        peers = sorted(
            (
                (peer_text, peer_rect)
                for peer_index, (peer_text, peer_rect) in enumerate(lines)
                if peer_index != line_index and peer_text and peer_rect.x0 >= rect.x1 - 1
            ),
            key=lambda item: item[1].x0,
        )
        for peer_text, peer_rect in peers:
            vertical_overlap = max(
                0.0,
                min(rect.y1, peer_rect.y1) - max(rect.y0, peer_rect.y0),
            )
            if vertical_overlap < min(rect.height, peer_rect.height) * 0.8:
                continue
            if peer_rect.x0 - caption_rect.x1 > max(12.0, rect.height * 1.2):
                continue
            caption_rect |= peer_rect
            caption_text = " ".join(
                part for part in (caption_text, peer_text.strip()) if part
            )
        captions.append(
            {
                "figure_number": int(match.group(1)),
                "caption": caption_text[:600],
                "rect": caption_rect,
            }
        )
    return captions


def _figure_region(
    page: Any,
    caption: Any,
    pymupdf: Any,
    *,
    page_caption_rects: tuple[Any, ...],
    excluded_text_rects: tuple[Any, ...] = (),
) -> Any | None:
    page_rect = page.rect
    column = _column_bounds(page_rect, caption, pymupdf)
    search = pymupdf.Rect(
        column.x0,
        max(page_rect.y0, caption.y0 - 500),
        column.x1,
        caption.y0 - 3,
    )
    if search.is_empty or search.height < 60:
        return None
    full_search = pymupdf.Rect(page_rect.x0, search.y0, page_rect.x1, search.y1)

    graphic_candidates: list[Any] = []
    full_graphic_candidates: list[Any] = []
    label_candidates: list[Any] = []
    table_caption_candidates: list[tuple[Any, float]] = []
    text_line_candidates: list[Any] = []
    try:
        # Image placement bounds can extend outside a PDF clip and into prose.
        # Let MuPDF apply the native clip; no image bytes or xref lookup needed.
        for block in page.get_text(
            "blocks", flags=pymupdf.TEXT_PRESERVE_IMAGES | pymupdf.TEXT_MEDIABOX_CLIP
        ):
            if block[6] != 1:
                continue
            rect = pymupdf.Rect(block[:4])
            if rect.width < 12 or rect.height < 12 or not rect.intersects(full_search):
                continue
            full_graphic_candidates.append(rect & full_search)
            if rect.intersects(search):
                graphic_candidates.append(rect & search)
    except Exception:
        pass
    try:
        drawings: list[tuple[Any, bool]] = []
        horizontal_separators: list[tuple[Any, float]] = []
        for drawing, rect in _visible_page_drawings(page, pymupdf):
            if rect.width >= 60 and rect.height <= 1.5:
                horizontal_separators.append(
                    (rect, max(0.0, float(drawing.get("width") or 0.0)))
                )
            if rect.is_empty:
                continue
            if rect.get_area() > page_rect.get_area() * 0.6:
                continue
            items = drawing.get("items") or []
            fill = drawing.get("fill")
            is_blank_canvas = (
                drawing.get("color") is None
                and isinstance(fill, (tuple, list))
                and len(fill) >= 3
                and all(float(channel) >= 0.98 for channel in fill[:3])
                and len(items) == 1
                and items[0][0] == "re"
            )
            drawings.append((rect, is_blank_canvas))

        for rect, is_blank_canvas in drawings:
            if is_blank_canvas:
                continue
            if rect.intersects(full_search):
                full_graphic_candidates.append(rect & full_search)
            if rect.intersects(search):
                graphic_candidates.append(rect & search)
    except Exception:
        pass
    try:
        for block in page.get_text("dict", sort=True).get("blocks", []):
            if block.get("type") != 0:
                continue
            owned_lines = []
            has_table_caption = False
            contains_anchor_caption = False
            for line in block.get("lines", []):
                if not line.get("bbox"):
                    continue
                text = re.sub(
                    r"\s+",
                    " ",
                    "".join(
                        str(span.get("text") or "")
                        for span in line.get("spans", [])
                    ),
                ).strip()
                rect = pymupdf.Rect(line["bbox"])
                center = pymupdf.Point(
                    (rect.x0 + rect.x1) / 2,
                    (rect.y0 + rect.y1) / 2,
                )
                if any(boundary.contains(center) for boundary in excluded_text_rects):
                    continue
                if _TABLE_CARRIER_PATTERN.match(text):
                    has_table_caption = True
                caption_match = _CAPTION_LINE_PATTERN.match(text)
                if caption_match is not None and rect.intersects(caption):
                    contains_anchor_caption = True
                if text and caption_match is None:
                    owned_lines.append(rect)
            if not owned_lines:
                continue
            candidate_lines = (
                [line_rect for line_rect in owned_lines if line_rect.y1 <= caption.y0]
                if contains_anchor_caption
                else owned_lines
            )
            if not candidate_lines:
                text_line_candidates.extend(owned_lines)
                continue
            rect = pymupdf.Rect(candidate_lines[0])
            for line_rect in candidate_lines[1:]:
                rect |= line_rect
            if rect.intersects(full_search):
                if has_table_caption:
                    table_caption_candidates.append(
                        (
                            rect,
                            max((line.height for line in owned_lines), default=1.0),
                        )
                    )
                label_candidates.append(rect)
                text_line_candidates.extend(owned_lines)
    except Exception:
        pass

    cluster_gap = 22
    clusters = _cluster_rectangles(
        graphic_candidates,
        pymupdf,
        gap=cluster_gap,
    )
    eligible_clusters = [
        rect
        for rect in clusters
        if rect.y1 <= caption.y0 + 2
        and rect.get_area() <= page_rect.get_area() * 0.6
    ]
    valid = [
        rect
        for rect in eligible_clusters
        if rect.width >= 60
        and rect.height >= 12
    ]
    if not valid:
        return None
    best = min(valid, key=lambda rect: caption.y0 - rect.y1)
    graphic_region = pymupdf.Rect(best)
    figure_bounds = page_rect & column
    for peer in eligible_clusters:
        vertical_overlap = max(
            0.0,
            min(peer.y1, best.y1) - max(peer.y0, best.y0),
        )
        center_delta = abs(
            (peer.y0 + peer.y1) / 2 - (best.y0 + best.y1) / 2
        )
        if (
            min(abs(peer.y1 - best.y1), center_delta) <= cluster_gap
            and vertical_overlap >= min(peer.height, best.height) * 0.5
        ):
            graphic_region |= peer

    page_captions = page_caption_rects
    while True:
        preceding_rows: list[tuple[float, Any]] = []
        for peer in eligible_clusters:
            if peer.y1 > graphic_region.y0 + 2:
                continue
            gap = graphic_region.y0 - peer.y1
            horizontal_overlap = max(
                0.0,
                min(peer.x1, graphic_region.x1)
                - max(peer.x0, graphic_region.x0),
            )
            if horizontal_overlap < min(peer.width, graphic_region.width) * 0.5:
                continue
            if gap > max(cluster_gap, min(peer.height, best.height)):
                continue
            if any(
                peer.y1 <= other.y0
                and other.y1 <= graphic_region.y0
                and max(
                    0.0,
                    min(other.x1, graphic_region.x1) - max(other.x0, graphic_region.x0),
                )
                > 0
                for other in page_captions
            ):
                continue
            preceding_rows.append((gap, peer))
        if not preceding_rows:
            break
        nearest_gap = min(gap for gap, _peer in preceding_rows)
        for gap, peer in preceding_rows:
            if gap <= nearest_gap + cluster_gap:
                graphic_region |= peer

    if column.width < page_rect.width:
        boundary = column.x1 if column.x1 < page_rect.x1 else column.x0
        crossing_primitives = [
            rect
            for rect in full_graphic_candidates
            if rect.x0 < boundary < rect.x1 and rect.intersects(graphic_region)
        ]
        if crossing_primitives:
            full_clusters = _cluster_rectangles(
                full_graphic_candidates,
                pymupdf,
                gap=cluster_gap,
            )
            expansions = [
                candidate
                for candidate in full_clusters
                if any(
                    candidate.contains(
                        pymupdf.Point(
                            (primitive.x0 + primitive.x1) / 2,
                            (primitive.y0 + primitive.y1) / 2,
                        )
                    )
                    for primitive in crossing_primitives
                )
            ]
            if expansions:
                graphic_region |= min(expansions, key=lambda rect: rect.get_area())
                graphic_candidates = full_graphic_candidates
                figure_bounds = page_rect

    # Text ownership and final clipping share the resolved graphic bounds. Text
    # in an adjacent column must not become a side annotation or raise the crop's
    # padding floor after being clipped away from the exported figure itself.
    label_candidates = [
        rect for rect in label_candidates if figure_bounds.contains(rect)
    ]
    text_line_candidates = [
        rect for rect in text_line_candidates if figure_bounds.contains(rect)
    ]

    # A later prose reference cannot claim artwork across its nearer caption.
    nearest_caption = min(
        (
            item
            for item in page_captions
            if item.y0 >= graphic_region.y1
            and graphic_region.x0
            <= (item.x0 + item.x1) / 2
            <= graphic_region.x1
        ),
        key=lambda rect: rect.y0,
        default=caption,
    )
    if nearest_caption.y0 < caption.y0:
        return None

    figure_region = pymupdf.Rect(graphic_region)
    tabular_rows = _horizontal_text_rows(text_line_candidates, pymupdf)
    table_boundaries: list[float] = []
    for separator, stroke_width in horizontal_separators:
        graphic_overlap = max(
            0.0,
            min(separator.x1, graphic_region.x1)
            - max(separator.x0, graphic_region.x0),
        )
        if (
            separator.y0 >= graphic_region.y0
            or graphic_overlap < graphic_region.width * 0.8
        ):
            continue
        belongs_to_table = any(
            _table_component_reaches_rule(
                table_caption,
                caption_line_height,
                separator,
                horizontal_separators,
                tabular_rows,
            )
            for table_caption, caption_line_height in table_caption_candidates
        )
        if belongs_to_table:
            table_boundaries.append(separator.y0 + stroke_width / 2)
    page_header_boundaries = [
        separator.y0 + stroke_width / 2
        for separator, stroke_width in horizontal_separators
        if separator.y0 < graphic_region.y0
        and graphic_region.y0 - separator.y0 <= cluster_gap
        and separator.width >= page_rect.width * 0.7
        and separator.y0 <= page_rect.y0 + page_rect.height * 0.12
    ]
    ownership_floor = max(
        [*table_boundaries, *page_header_boundaries],
        default=search.y0,
    )
    label_envelope = pymupdf.Rect(
        graphic_region.x0 - cluster_gap,
        max(graphic_region.y0 - cluster_gap, ownership_floor),
        graphic_region.x1 + cluster_gap,
        caption.y0 - 2,
    )
    caption_gap = 2.0
    accepted_labels: list[Any] = []
    side_annotation_groups: dict[str, list[tuple[Any, Any, Any]]] = {
        "left": [],
        "right": [],
    }
    for label in label_candidates:
        center = pymupdf.Point((label.x0 + label.x1) / 2, (label.y0 + label.y1) / 2)
        if not graphic_region.y0 <= center.y <= graphic_region.y1:
            continue
        if (
            graphic_region.x0 - 2 * cluster_gap
            <= label.x1
            <= graphic_region.x0 + cluster_gap
        ):
            side = "left"
        elif (
            graphic_region.x1 - cluster_gap
            <= label.x0
            <= graphic_region.x1 + 2 * cluster_gap
        ):
            side = "right"
        else:
            continue
        row_graphics = [
            rect
            for rect in graphic_candidates
            if rect.intersects(graphic_region)
            and rect.y0 <= center.y <= rect.y1
        ]
        if not row_graphics:
            continue
        anchor = min(
            row_graphics,
            key=lambda rect: (
                abs(
                    (rect.x0 - label.x1)
                    if side == "left"
                    else (label.x0 - rect.x1)
                ),
                rect.height,
                rect.width,
            ),
        )
        side_annotation_groups[side].append((label, center, anchor))
    side_annotation_ids = {
        id(label)
        for side, group in side_annotation_groups.items()
        for label, center, anchor in group
        if any(
            abs(
                (label.x1 if side == "left" else label.x0)
                - (peer.x1 if side == "left" else peer.x0)
            )
            <= cluster_gap
            and max(
                0.0,
                min(anchor.y1, peer_anchor.y1)
                - max(anchor.y0, peer_anchor.y0),
            )
            < min(anchor.height, peer_anchor.height) * 0.5
            for peer, _peer_center, peer_anchor in group
            if peer is not label
        )
    }
    for label in label_candidates:
        # Ownership uses the complete text object, not a clipped neighbour's tail.
        center = pymupdf.Point((label.x0 + label.x1) / 2, (label.y0 + label.y1) / 2)
        if (
            (label_envelope.contains(center) or id(label) in side_annotation_ids)
            and label.y1 <= caption.y0
            and label.y0 >= ownership_floor
        ):
            figure_region |= label
            accepted_labels.append(label)
            if label.y1 > caption.y0 - 2:
                caption_gap = 0.0
    padding_floor = ownership_floor
    for line in text_line_candidates:
        if not (
            line.y0 < figure_region.y0
            and line.y1 > figure_region.y0 - 6
            and min(line.x1, figure_region.x1) > max(line.x0, figure_region.x0)
        ):
            continue
        center = pymupdf.Point((line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2)
        if any(label.contains(center) for label in accepted_labels):
            continue
        padding_floor = max(padding_floor, line.y1)
    return _padded_figure_region(
        figure_region,
        caption,
        figure_bounds,
        pymupdf,
        caption_gap=caption_gap,
        top_floor=padding_floor,
    )


def _horizontal_text_rows(lines: list[Any], pymupdf: Any) -> list[Any]:
    """Group sibling text cells by their shared baseline, across PDF blocks."""

    groups: list[list[Any]] = []
    for line in sorted(lines, key=lambda rect: (rect.y0, rect.x0)):
        for group in groups:
            anchor = group[0]
            vertical_overlap = max(
                0.0,
                min(anchor.y1, line.y1) - max(anchor.y0, line.y0),
            )
            if vertical_overlap >= min(anchor.height, line.height) * 0.5:
                group.append(line)
                break
        else:
            groups.append([line])

    rows: list[Any] = []
    for group in groups:
        if len(group) < 2 or not any(
            first.x1 <= second.x0 or second.x1 <= first.x0
            for index, first in enumerate(group)
            for second in group[index + 1 :]
        ):
            continue
        row = pymupdf.Rect(group[0])
        for line in group[1:]:
            row |= line
        rows.append(row)
    return rows


def _same_horizontal_rule_span(first: Any, second: Any) -> bool:
    """Return whether two rules form one physical table-rule family."""

    overlap = max(0.0, min(first.x1, second.x1) - max(first.x0, second.x0))
    return overlap >= max(first.width, second.width) * 0.9


def _table_component_reaches_rule(
    caption: Any,
    caption_line_height: float,
    target: Any,
    horizontal_rules: list[tuple[Any, float]],
    rows: list[Any],
) -> bool:
    """Require one caption, row, and rule family to form a continuous PDF band."""

    if caption.y1 > target.y0 or caption.x0 > target.x1 or caption.x1 < target.x0:
        return False
    nodes = [
        (rule, False, rule is target)
        for rule, _stroke_width in horizontal_rules
        if caption.y1 <= rule.y0 <= target.y0
        and _same_horizontal_rule_span(rule, target)
    ]
    nodes.extend(
        (row, True, False)
        for row in rows
        if caption.y1 <= row.y0 and row.y1 <= target.y0
        and row.x0 <= target.x1 and row.x1 >= target.x0
    )
    cursor = caption.y1
    local_text_height = max(caption_line_height, 1.0)
    seen_peer_rule = False
    seen_tabular_row = False
    previous_node_was_tabular_row = False
    for rect, is_text, is_target in sorted(
        nodes,
        key=lambda item: (item[0].y0, 0 if item[1] else 1),
    ):
        allowed_gap = max(local_text_height, rect.height if is_text else 0.0)
        if rect.y0 - cursor > allowed_gap:
            return False
        cursor = max(cursor, rect.y1)
        if is_target:
            return (
                seen_peer_rule
                and seen_tabular_row
                and previous_node_was_tabular_row
            )
        if is_text:
            local_text_height = max(rect.height, 1.0)
            seen_tabular_row = True
        else:
            seen_peer_rule = True
        previous_node_was_tabular_row = is_text
    return False


def _padded_figure_region(
    figure: Any,
    caption: Any,
    page_rect: Any,
    pymupdf: Any,
    *,
    caption_gap: float = 2.0,
    top_floor: float | None = None,
) -> Any:
    """Pad artwork without leaking the anchoring caption into the bitmap."""

    bottom = min(figure.y1 + 3, caption.y0 - caption_gap)
    top = figure.y0 - 6
    if top_floor is not None:
        top = max(top, top_floor)
    padded = pymupdf.Rect(
        figure.x0 - 6,
        top,
        figure.x1 + 6,
        bottom,
    )
    return padded & page_rect


def _column_bounds(page_rect: Any, caption: Any, pymupdf: Any) -> Any:
    midpoint = page_rect.x0 + page_rect.width / 2
    if caption.width >= page_rect.width * 0.68:
        return pymupdf.Rect(page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1)
    if caption.x1 <= midpoint + 12:
        return pymupdf.Rect(page_rect.x0, page_rect.y0, midpoint, page_rect.y1)
    if caption.x0 >= midpoint - 12:
        return pymupdf.Rect(midpoint, page_rect.y0, page_rect.x1, page_rect.y1)
    return pymupdf.Rect(page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1)


def _cluster_rectangles(
    rectangles: list[Any], pymupdf: Any, *, gap: float
) -> list[Any]:
    pending = [pymupdf.Rect(rect) for rect in rectangles if not rect.is_empty]
    clusters: list[Any] = []
    while pending:
        cluster = pending.pop()
        changed = True
        while changed:
            changed = False
            envelope = pymupdf.Rect(
                cluster.x0 - gap,
                cluster.y0 - gap,
                cluster.x1 + gap,
                cluster.y1 + gap,
            )
            keep: list[Any] = []
            for rect in pending:
                if envelope.intersects(rect):
                    cluster |= rect
                    changed = True
                else:
                    keep.append(rect)
            pending = keep
        clusters.append(cluster)
    return clusters


def _figure_context(text: str, figure_number: int) -> str:
    return _carrier_context(text, "Figure", figure_number)


def _carrier_context(text: str, label: str, number: int) -> str:
    """Return one bounded prose reference to a formally numbered visual carrier."""

    reference = re.compile(
        (
            rf"(?:figure|fig\.?)\s*{number}(?!\d)"
            if label == "Figure"
            else rf"{re.escape(label)}\s*{number}(?!\d)"
        ),
        re.IGNORECASE,
    )
    for paragraph in re.split(r"\n\s*\n", text):
        compact = re.sub(r"\s+", " ", paragraph).strip()
        if not compact or _CAPTION_PATTERN.match(compact):
            continue
        match = reference.search(compact)
        if match is None:
            continue
        start = max(0, match.start() - 220)
        end = min(len(compact), match.end() + 320)
        return compact[start:end]
    return ""


__all__ = [
    "PaperSourceError",
    "PreparedPaper",
    "prepare_pdf",
]
