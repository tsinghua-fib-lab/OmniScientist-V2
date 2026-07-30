"""Prepare a grounded paper source and reusable figures from one local PDF."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import poster_assets

from posterlib.runtime.capability import pymupdf_supported

_CAPTION_PATTERN = re.compile(
    r"^\s*(?:figure|fig\.?)\s*(\d+)\s*[.:：]?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)
_ABSTRACT_HEADING_PATTERN = re.compile(r"^abstract\s*$", re.IGNORECASE)
_NON_AUTHOR_PATTERN = re.compile(
    r"(?:@|https?://|\b(?:abstract|arxiv|conference|department|institute|laborator(?:y|ies)|"
    r"permission|research|school|university)\b)",
    re.IGNORECASE,
)
_AUTHOR_MARKER_PATTERN = re.compile(r"[\s*∗†‡§¶⁰¹²³⁴⁵⁶⁷⁸⁹]+$")
_TITLE_SIZE_MATCH_RATIO = 0.93
_MIN_TITLE_SCALE_RATIO = 1.15
_MAX_PDF_BYTES = 256 * 1024 * 1024
_MAX_PAGES = 500
_DEFAULT_MAX_FIGURE_CANDIDATES = 16
_FIGURE_RENDER_DPI = 450
_PDF_POINTS_PER_INCH = 72
_MIN_EMBEDDED_RASTER_COVERAGE = 0.8
_FIGURE_CAPTION_SIGNAL_PATTERN = re.compile(
    r"\b(?:ablation|analysis|architecture|benchmark|comparison|evaluation|experiment|"
    r"failure|framework|method|overview|performance|pipeline|result|robustness|scaling|"
    r"tradeoff)\b",
    re.IGNORECASE,
)
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


class PaperSourceError(RuntimeError):
    """A PDF cannot be converted into a trustworthy poster source."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedPaper:
    """Text, metadata, and caption-grounded figure assets from a paper."""

    text: str
    title: str
    authors: str
    page_count: int
    figures: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _FigureAsset:
    """One prepared figure representation selected by the extraction cascade."""

    path: Path
    width: int
    height: int
    extraction_mode: str


def prepare_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    max_figure_candidates: int = _DEFAULT_MAX_FIGURE_CANDIDATES,
) -> PreparedPaper:
    """Extract text and a bounded, document-wide set of figure candidates."""

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
            "PDF ingestion requires pymupdf>=1.24.",
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
        title = str(metadata.get("title") or "").strip()
        authors = str(metadata.get("author") or "").strip()
        if not title or not authors:
            fallback_title, fallback_authors = _first_page_identity(document[0])
            title = title or fallback_title
            authors = authors or fallback_authors
        pages: list[str] = []
        figure_candidates: list[dict[str, Any]] = []
        for page_index, page in enumerate(document):
            page_text = page.get_text("text", sort=True).strip()
            if page_text:
                pages.append(f"[Page {page_index + 1}]\n{page_text}")
            figure_candidates.extend(
                {
                    **caption,
                    "page_index": page_index,
                }
                for caption in _caption_blocks(page, pymupdf)
            )
        text = "\n\n".join(pages).strip()
        if not text:
            raise PaperSourceError(
                "source_read_failed",
                "The PDF contains no extractable text; OCR is not provided by this Skill.",
            )
        selected_candidates = _select_figure_candidates(
            figure_candidates,
            max_candidates=max_figure_candidates,
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
                )
            )
        return PreparedPaper(
            text=text,
            title=title,
            authors=authors,
            page_count=document.page_count,
            figures=tuple(figures),
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


def _first_page_identity(page: Any) -> tuple[str, str]:
    """Recover high-confidence identity when a PDF omits document metadata."""

    lines = _horizontal_text_lines(page)
    abstract = next(
        (line for line in lines if _ABSTRACT_HEADING_PATTERN.fullmatch(line["text"])),
        None,
    )
    if abstract is None:
        return "", ""

    page_height = float(page.rect.height)
    abstract_top = float(abstract["bbox"][1])
    title_candidates = [
        line
        for line in lines
        if float(line["bbox"][1]) < abstract_top
        and float(line["bbox"][1]) < page_height * 0.55
        and _has_letters(line["text"])
        and "@" not in line["text"]
    ]
    if not title_candidates:
        return "", ""

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
        return "", ""
    title_lines.sort(key=lambda line: (float(line["bbox"][1]), float(line["bbox"][0])))
    title = re.sub(
        r"\s+",
        " ",
        " ".join(str(line["text"]) for line in title_lines),
    ).strip()
    title_bottom = max(float(line["bbox"][3]) for line in title_lines)

    author_lines = [
        line
        for line in lines
        if title_bottom < float(line["bbox"][1]) < abstract_top
        and _looks_like_author_copy(str(line["bold_text"]))
    ]
    author_lines.sort(
        key=lambda line: (round(float(line["bbox"][1]), 1), float(line["bbox"][0]))
    )
    authors = ", ".join(
        _AUTHOR_MARKER_PATTERN.sub("", str(line["bold_text"])).strip()
        for line in author_lines
    )
    return title, authors


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
            text = re.sub(
                r"\s+", " ", "".join(str(span["text"]) for span in spans)
            ).strip()
            if not text:
                continue
            size = max(float(span.get("size") or 0.0) for span in spans)
            bold_text = re.sub(
                r"\s+",
                " ",
                "".join(
                    str(span["text"])
                    for span in spans
                    if int(span.get("flags") or 0) & 16
                    and float(span.get("size") or 0.0) >= size * 0.8
                ),
            ).strip()
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
    cleaned = _AUTHOR_MARKER_PATTERN.sub("", value).strip(" ,;")
    if not cleaned or len(cleaned) > 300 or _NON_AUTHOR_PATTERN.search(cleaned):
        return False
    words = [token.strip(".,;:()[]{}") for token in cleaned.split()]
    return sum(any(character.isalpha() for character in word) for word in words) >= 2


def _extract_captioned_figures(
    page: Any,
    output_dir: Path,
    *,
    captions: list[dict[str, Any]],
    page_index: int,
    pymupdf: Any,
) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    for caption in captions:
        region = _figure_region(page, caption["rect"], pymupdf)
        if region is None:
            continue
        number = int(caption["figure_number"])
        stem = f"figure-{number}-page-{page_index + 1}"
        asset = _write_embedded_raster(page, region, output_dir / stem, pymupdf)
        if asset is None:
            asset = _write_vector_clip(
                page, region, output_dir / f"{stem}.svg", pymupdf
            )
        if asset is None:
            asset = _write_raster_fallback(
                page,
                region,
                output_dir / f"{stem}.png",
                pymupdf,
            )
        if asset is None:
            continue
        extracted.append(
            {
                "path": str(asset.path),
                "page": page_index + 1,
                "figure_number": number,
                "caption": caption["caption"],
                "context": str(caption.get("context") or ""),
                "width": asset.width,
                "height": asset.height,
                "source": "caption-anchored",
                "extraction_mode": asset.extraction_mode,
                "crop_bbox": [round(value, 2) for value in tuple(region)],
                "sha256": hashlib.sha256(asset.path.read_bytes()).hexdigest(),
            }
        )
    return extracted


def _select_figure_candidates(
    candidates: list[dict[str, Any]],
    *,
    max_candidates: int,
) -> list[dict[str, Any]]:
    """Select bounded crops using soft caption relevance and page coverage."""

    if max_candidates <= 0 or not candidates:
        return []
    ordered = sorted(
        candidates,
        key=lambda item: (
            int(item.get("page_index") or 0),
            int(item.get("figure_number") or 0),
        ),
    )
    if len(ordered) <= max_candidates:
        return ordered

    coverage_count = min(max_candidates, max(1, (max_candidates + 2) // 3))
    if coverage_count == 1:
        selected_indices = {0}
    else:
        selected_indices = {
            round(slot * (len(ordered) - 1) / (coverage_count - 1))
            for slot in range(coverage_count)
        }

    while len(selected_indices) < max_candidates:
        remaining = [
            index for index in range(len(ordered)) if index not in selected_indices
        ]
        next_index = max(
            remaining,
            key=lambda index: (
                _figure_candidate_signal_score(ordered[index]),
                min(abs(index - selected) for selected in selected_indices),
                -index,
            ),
        )
        selected_indices.add(next_index)
    return [ordered[index] for index in sorted(selected_indices)]


def _figure_candidate_signal_score(candidate: dict[str, Any]) -> int:
    """Return a soft relevance score without making any figure role mandatory."""

    return len(
        {
            match.group(0).casefold()
            for match in _FIGURE_CAPTION_SIGNAL_PATTERN.finditer(
                str(candidate.get("caption") or "")
            )
        }
    )


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
        mime = poster_assets.image_asset_mime(content)
        extension = _RASTER_MIME_EXTENSIONS.get(mime or "")
        if extension is None:
            return None
        width = int(image.get("width") or 0)
        height = int(image.get("height") or 0)
        if width <= 0 or height <= 0:
            return None
        output_path = output_stem.with_suffix(f".{extension}")
        output_path.write_bytes(content)
        return _FigureAsset(output_path, width, height, "embedded-raster")
    except Exception:
        return None


def _region_has_vector_or_text_overlay(page: Any, region: Any, pymupdf: Any) -> bool:
    """Return whether lossless raster extraction would omit visible overlays."""

    try:
        for drawing in page.get_drawings():
            rect = pymupdf.Rect(drawing["rect"])
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
        if not poster_assets.svg_asset_is_safe(svg_bytes):
            return None
        output_path.write_bytes(svg_bytes)
        return _FigureAsset(
            output_path,
            max(1, round(float(region.width))),
            max(1, round(float(region.height))),
            "vector-clip",
        )
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
        return _FigureAsset(
            output_path,
            int(pixmap.width),
            int(pixmap.height),
            "raster-fallback",
        )
    except Exception:
        return None


def _caption_blocks(page: Any, pymupdf: Any) -> list[dict[str, Any]]:
    captions: list[dict[str, Any]] = []
    try:
        blocks = page.get_text("blocks", sort=True)
    except Exception:
        return captions
    for block in blocks:
        if len(block) < 7 or block[6] != 0:
            continue
        text = re.sub(r"\s+", " ", str(block[4])).strip()
        match = _CAPTION_PATTERN.match(text)
        if match is None:
            continue
        caption = match.group(2).strip()
        captions.append(
            {
                "figure_number": int(match.group(1)),
                "caption": caption[:600],
                "rect": pymupdf.Rect(block[:4]),
            }
        )
    return captions


def _figure_region(page: Any, caption: Any, pymupdf: Any) -> Any | None:
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

    graphic_candidates: list[Any] = []
    label_candidates: list[Any] = []
    try:
        for image in page.get_image_info(xrefs=True):
            rect = pymupdf.Rect(image["bbox"])
            if rect.width >= 12 and rect.height >= 12 and rect.intersects(search):
                graphic_candidates.append(rect & search)
    except Exception:
        pass
    try:
        for drawing in page.get_drawings():
            rect = pymupdf.Rect(drawing["rect"])
            if rect.width < 3 or rect.height < 3:
                continue
            if rect.get_area() > page_rect.get_area() * 0.6:
                continue
            if rect.intersects(search):
                graphic_candidates.append(rect & search)
    except Exception:
        pass
    try:
        for block in page.get_text("blocks", sort=True):
            if len(block) < 7 or block[6] != 0:
                continue
            text = re.sub(r"\s+", " ", str(block[4])).strip()
            rect = pymupdf.Rect(block[:4])
            if not text or len(text) > 80 or _CAPTION_PATTERN.match(text):
                continue
            if rect.intersects(search):
                label_candidates.append(rect & search)
    except Exception:
        pass

    clusters = _cluster_rectangles(
        [*graphic_candidates, *label_candidates],
        pymupdf,
        gap=22,
    )
    valid = [
        rect
        for rect in clusters
        if rect.width >= 60
        and rect.height >= 60
        and rect.y1 <= caption.y0 + 2
        and rect.get_area() <= page_rect.get_area() * 0.6
        and any(rect.intersects(graphic) for graphic in graphic_candidates)
    ]
    if not valid:
        return None
    best = min(valid, key=lambda rect: caption.y0 - rect.y1)
    return _padded_figure_region(best, caption, page_rect, pymupdf)


def _padded_figure_region(
    figure: Any,
    caption: Any,
    page_rect: Any,
    pymupdf: Any,
) -> Any:
    """Pad artwork without leaking the anchoring caption into the bitmap."""

    bottom = min(figure.y1 + 3, caption.y0 - 2)
    padded = pymupdf.Rect(
        figure.x0 - 6,
        figure.y0 - 6,
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
    reference = re.compile(
        rf"(?:figure|fig\.?)\s*{figure_number}(?!\d)",
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
