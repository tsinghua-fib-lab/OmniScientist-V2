"""Static HTML validation and embedded-image binding for scientific posters."""

from __future__ import annotations

import hashlib
import html as html_text_utils
import json
import math
from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from typing import Any

import image_assets
import poster_contract
import pyparsing as pp
import tinycss2
from tinycss2.serializer import serialize_string_value

from posterlib.math_fonts import FONT_FACE, font_css
from posterlib.runtime.identifiers import is_asset_token, is_sha256

from . import scientific_snapshot
from .equations import render_inline_math

_ATTRIBUTE_VALUE = (
    pp.QuotedString('"', multiline=True, convert_whitespace_escapes=False)
    | pp.QuotedString("'", multiline=True, convert_whitespace_escapes=False)
    | pp.CharsNotIn(" \t\r\n\f>").set_whitespace_chars(" \t\r\n\f")
)
_START_TAG = (
    pp.Suppress("<") + pp.CharsNotIn(" \t\r\n\f/>").set_whitespace_chars(" \t\r\n\f")("tag")
    + pp.ZeroOrMore(pp.Group(pp.Located(
        pp.CharsNotIn(" \t\r\n\f=/>").set_whitespace_chars(" \t\r\n\f")("name")
        + pp.Optional(pp.Suppress("=") + pp.Located(_ATTRIBUTE_VALUE)("value"))
    )))("attributes")
    + pp.Optional(pp.Suppress("/")) + pp.Suppress(">")
).parse_with_tabs()


class _HtmlSourceEditor(HTMLParser):
    """Locate resource edits while retaining every untouched source character."""

    def __init__(
        self, source: str,
        edit_attributes: Callable[[str, dict[str, str]], Mapping[str, str | None]],
        transform: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.edit_attributes = edit_attributes
        self.transform = transform
        self.line_offsets = [0, *(index + 1 for index, value in enumerate(source) if value == "\n")]
        self.edits: list[tuple[int, int, str]] = []
        self.source_figure_hashes: set[str] = set()
        self.has_math = False
        self.math_font_style_spans: list[tuple[int, int]] = []
        self.head_end: int | None = None
        self.body_end: int | None = None
        self._in_style = False
        self._math_font_style_start: int | None = None
        self.feed(source)
        self.close()

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "img" and is_sha256(attributes.get("data-source-figure-sha256")):
            self.source_figure_hashes.add(attributes["data-source-figure-sha256"])
        if tag == "math":
            self.has_math = True
        if tag == "style" and attributes.get("data-poster-math-font") == FONT_FACE:
            self._math_font_style_start = self._offset()
        self._in_style = tag == "style"
        changes = self.edit_attributes(tag, attributes)
        if not changes:
            return
        raw = self.get_starttag_text()
        try:
            parsed = _START_TAG.parse_string(raw, parse_all=True)
        except pp.ParseBaseException as exc:
            raise ValueError("Resource attributes have invalid HTML syntax.") from exc
        start = self._offset()
        found: set[str] = set()
        for attribute in parsed.attributes:
            name = attribute.value.name.lower()
            if name not in changes:
                continue
            found.add(name)
            value = changes[name]
            if value is None:
                begin = attribute.locn_start
                while begin and raw[begin - 1].isspace():
                    begin -= 1
                self.edits.append((start + begin, start + attribute.locn_end, ""))
            elif "value" in attribute.value:
                span = attribute.value["value"]
                self.edits.append((start + span.locn_start, start + span.locn_end,
                                   '"' + html_text_utils.escape(value, quote=True) + '"'))
            else:
                self.edits.append((start + attribute.locn_end, start + attribute.locn_end,
                                   '="' + html_text_utils.escape(value, quote=True) + '"'))
        insertion = len(raw) - (2 if raw.endswith("/>") else 1)
        additions = "".join(
            f' {name}="{html_text_utils.escape(value, quote=True)}"'
            for name, value in changes.items() if name not in found and value is not None
        )
        if additions:
            self.edits.append((start + insertion, start + insertion, additions))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self._in_style = False
        self._math_font_style_start = None

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False
            if self._math_font_style_start is not None:
                end = self.source.index(">", self._offset()) + 1
                self.math_font_style_spans.append((self._math_font_style_start, end))
                self._math_font_style_start = None
        if tag == "head":
            self.head_end = self._offset()
        if tag == "body":
            self.body_end = self._offset()

    def handle_data(self, data: str) -> None:
        if self._in_style and self.transform is not None:
            mapped = _map_css_references(data, self.transform)
            if mapped != data:
                start = self._offset()
                self.edits.append((start, start + len(data), mapped))

    def render(self) -> str:
        result = self.source
        for start, end, replacement in sorted(self.edits, reverse=True):
            result = result[:start] + replacement + result[end:]
        return result


def inject_before_body_end(html_text: str, injection: str) -> str:
    """Insert preview tooling at the parsed body close, retaining authored bytes."""

    parser = _HtmlSourceEditor(html_text, lambda tag, attrs: {})
    offset = parser.body_end if parser.body_end is not None else len(html_text)
    return html_text[:offset] + injection + html_text[offset:]


def map_html_asset_references(
    html_text: str, transform: Callable[[str], str],
    *, remove_source_hashes: bool = False,
) -> str:
    """Transform resource attributes and CSS URLs while preserving scientific copy."""

    def edit(tag: str, attributes: dict[str, str]) -> dict[str, str | None]:
        changes: dict[str, str | None] = {}
        if remove_source_hashes and tag == "img":
            for name in ("data-source-figure-sha256", "data-native-component-sha256"):
                if is_sha256(attributes.get(name)):
                    changes[name] = None
        for name, value in attributes.items():
            if name in {"src", "href", "xlink:href", "poster"}:
                mapped = transform(value)
            elif name in {
                "style", "clip-path", "cursor", "fill", "filter", "marker",
                "marker-end", "marker-mid", "marker-start", "mask", "stroke",
            }:
                mapped = _map_css_references(value, transform)
            else:
                continue
            if mapped != value:
                changes[name] = mapped
        return changes

    return _HtmlSourceEditor(html_text, edit, transform).render()


def _map_css_references(css: str, transform: Callable[[str], str]) -> str:
    tokens = tinycss2.parse_component_value_list(css)
    changed = False

    def visit(items: list[Any]) -> None:
        nonlocal changed
        for token in items:
            if token.type == "url":
                mapped = transform(token.value)
                if mapped != token.value:
                    changed = True
                    token.value = mapped
                    token.representation = 'url("' + serialize_string_value(mapped) + '")'
            elif token.type == "function" and token.lower_name == "url":
                args = [item for item in token.arguments if item.type not in {"whitespace", "comment"}]
                if len(args) == 1 and args[0].type == "string":
                    value = args[0]
                    mapped = transform(value.value)
                    if mapped != value.value:
                        changed = True
                        value.value = mapped
                        value.representation = '"' + serialize_string_value(mapped) + '"'
            else:
                nested = getattr(token, "content", getattr(token, "arguments", None))
                if nested is not None:
                    visit(nested)

    visit(tokens)
    return tinycss2.serialize(tokens) if changed else css


def _normalize_equation_latex(value: Any) -> str:
    """Trim transport whitespace without changing mathematical content."""

    return str(value).strip()


def paper_identity_issues(
    html_text: str,
    paper_identity: dict[str, Any] | None,
    *,
    facts: scientific_snapshot.ScientificHtmlFacts | None = None,
) -> list[dict[str, str]]:
    """Require exact paper identity once, without mutating authored markup."""

    if not paper_identity:
        return []
    parsed = facts or scientific_snapshot.parse_scientific_html(html_text)
    visible = _canonical_identity_text(" ".join(parsed.title_band_text))
    issues: list[dict[str, str]] = []
    title = str(paper_identity.get("title") or "").strip()
    rendered_title = scientific_snapshot.parse_scientific_html(
        '<header data-poster-title-band>' + render_inline_math(title) + '</header>'
    )
    expected_title = _canonical_identity_text(" ".join(rendered_title.title_band_text))
    if title and expected_title not in visible:
        issues.append(
            _issue(
                "missing_paper_title",
                "Render the verified paper title in the title band.",
            )
        )
    authors = str(paper_identity.get("authors") or "").strip()
    if authors:
        expected = _canonical_author_text(authors)
        marked = _canonical_author_text(" ".join(parsed.author_text))
        # Persisted identities may contain the full author line, including pointers.
        displayed = _canonical_author_text(" ".join(parsed.snapshot["identity"]["authors"]))
        if parsed.author_marker_count != 1 or expected not in {marked, displayed}:
            issues.append(
                _issue(
                    "missing_paper_authors",
                    "Render the complete verified author list once in the title band inside "
                    'data-poster-authors="verified".',
                )
            )
    return issues


def venue_identity_warnings(
    html_text: str,
    paper_identity: dict[str, Any] | None,
    *,
    facts: scientific_snapshot.ScientificHtmlFacts | None = None,
) -> list[dict[str, str]]:
    """Report optional venue branding without blocking a grounded poster."""

    venue = paper_identity.get("venue_identity") if paper_identity else None
    if not isinstance(venue, dict):
        return []
    parsed = facts or scientific_snapshot.parse_scientific_html(html_text)
    visible = _canonical_identity_text(" ".join(parsed.title_band_text))
    required_copy = [
        str(venue.get(key) or "").strip()
        for key in ("label", "distinction")
        if str(venue.get(key) or "").strip()
    ]
    warnings: list[dict[str, str]] = []
    logo_token = str(venue.get("logo_asset_token") or "").strip()
    logo_bound = bool(logo_token and logo_token in parsed.logo_sources)
    copy_bound = bool(required_copy) and all(
        _canonical_identity_text(value) in visible for value in required_copy
    )
    if logo_token and not logo_bound:
        warnings.append(
            _warning(
                "venue_branding_omitted",
                "A verified local venue logo was available but is not visible in the "
                "title band.",
            )
        )
    elif not logo_bound and required_copy and not copy_bound:
        warnings.append(
            _warning(
                "venue_branding_omitted",
                "Verified venue branding is not visible in the title band.",
            )
        )
    return warnings


def _canonical_identity_text(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _canonical_author_text(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalpha())


def authored_content_contract(html_text: str, assets: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind later revisions to the actual authored selection, not an early outline."""

    facts = scientific_snapshot.parse_scientific_html(_annotate_asset_tokens(html_text, assets))
    return {
        "modules": [
            {
                "module_id": module_id,
                "equation_latex": list(facts.equation_latex.get(module_id, ())),
                "source_figure_sha256s": sorted({
                    digest for entry in entries for digest in entry["source_figure_sha256s"]
                }),
                **({"native_component_sha256s": sorted(
                    digest for entry in entries for digest in entry.get("native_component_sha256s", ())
                )} if any(entry.get("native_component_sha256s") for entry in entries) else {}),
            }
            for module_id, entries in facts.snapshot["modules"].items()
        ],
        "source_figure_sha256s": sorted(facts.source_figure_sha256s),
    }


def validate_candidate(
    html_text: str,
    *,
    assets: list[dict[str, Any]],
    required_source_figure_sha256s: set[str] | None = None,
    expected_page: dict[str, Any] | None = None,
    content_contract: Mapping[str, Any] | None = None,
    paper_identity: dict[str, Any] | None = None,
    reference_html: str | None = None,
    content_replan_targets: frozenset[str] | set[str] | list[str] | None = None,
    source_text: str | None = None,
    source_assets: list[dict[str, Any]] | None = None,
    source_equations: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Validate grounded HTML against assets, page size, and content integrity."""

    asset_source_figures = {
        str(item["content_sha256"])
        for item in assets
        if image_assets.is_source_figure_asset(item)
    }
    requested_source_figures = {
        value for value in required_source_figure_sha256s or set() if value
    }
    prepared_source_figures = asset_source_figures or requested_source_figures
    try:
        annotated = _annotate_asset_tokens(html_text, assets)
        resolved = _embed_annotated_assets(annotated, assets)
    except ValueError as exc:
        return {"status": "error", "issues": [_issue("asset_token", str(exc))]}
    poster_facts = poster_contract.parse_poster_html(resolved)
    report = poster_contract.validate_poster_html(resolved, facts=poster_facts)
    check_content_contract = bool(
        content_contract
        or required_source_figure_sha256s is not None
        or poster_facts.visible_source_figure_sha256s
    )
    needs_scientific_facts = bool(
        paper_identity
        or check_content_contract
        or reference_html is not None
        or source_text is not None
    )
    scientific_facts = (
        scientific_snapshot.parse_scientific_html(annotated)
        if needs_scientific_facts
        else None
    )
    identity_issues = paper_identity_issues(
        annotated,
        paper_identity,
        facts=scientific_facts,
    )
    if identity_issues:
        report = _append_issues(report, identity_issues)
    for warning in venue_identity_warnings(
        annotated,
        paper_identity,
        facts=scientific_facts,
    ):
        report = _append_warning(report, warning)
    if check_content_contract:
        report = _validate_content_contract(
            report,
            resolved=annotated,
            content_contract=content_contract or {},
            prepared_source_figure_sha256s=prepared_source_figures,
            facts=scientific_facts,
        )
    if reference_html is not None:
        assert scientific_facts is not None
        reference_annotated = _annotate_asset_tokens(reference_html, assets)
        reference_facts = scientific_snapshot.parse_scientific_html(
            reference_annotated
        )
        snapshot_issues = (
            scientific_snapshot.grounded_replan_snapshot_issues(
                reference_facts.snapshot,
                scientific_facts.snapshot,
                target_module_ids=content_replan_targets,
            )
            if content_replan_targets is not None
            else scientific_snapshot.scientific_content_snapshot_issues(
                reference_facts.snapshot,
                scientific_facts.snapshot,
            )
        )
        if snapshot_issues:
            report = _append_issues(report, snapshot_issues)
    if expected_page is not None and report.get("status") == "ok":
        issue = page_plan_issue(
            report.get("page"),
            expected_page,
        )
        if issue is not None:
            if issue.get("severity") == "warning":
                report = _append_warning(report, issue)
            else:
                report = _append_issue(report, issue)
    return report


def page_plan_issue(
    observed: Any,
    expected: Mapping[str, Any],
) -> dict[str, str] | None:
    """Return a physical-page issue when the authored canvas changes."""

    if not isinstance(observed, Mapping):
        return _issue("page_changed", "HTML has no valid physical page dimensions.")
    try:
        width = float(observed["width_mm"])
        height = float(observed["height_mm"])
        expected_width = float(expected["width_mm"])
        expected_height = float(expected["height_mm"])
    except (KeyError, TypeError, ValueError):
        return _issue("page_changed", "HTML has no valid physical page dimensions.")
    if not math.isclose(width, expected_width, abs_tol=0.01):
        return _issue(
            "page_changed",
            "HTML physical page width differs from the active page plan.",
        )
    if not math.isclose(height, expected_height, abs_tol=0.01):
        return _issue(
            "page_changed",
            "HTML physical page dimensions differ from the active page plan.",
        )
    return None


def source_figure_sha256s(assets: list[dict[str, Any]]) -> set[str]:
    """Return verified paper-figure identities from an asset manifest."""

    return {
        str(item.get("content_sha256") or "")
        for item in assets
        if image_assets.is_source_figure_asset(item)
        and is_sha256(item.get("content_sha256"))
    }


def embed_assets(html_text: str, assets: list[dict[str, Any]]) -> str:
    """Bind source assets and the shared math font for offline rendering."""

    annotated = _annotate_asset_tokens(strip_math_font_styles(html_text), assets)
    resolved = _embed_annotated_assets(annotated, assets)
    return embed_math_font(resolved)


def embed_math_font(html_text: str) -> str:
    """Prepare one shared mathematical face before browser layout and native capture."""

    parser = _HtmlSourceEditor(html_text, lambda tag, attrs: {})
    parser.edits.extend((start, end, "") for start, end in parser.math_font_style_spans)
    if parser.has_math:
        offset = parser.head_end if parser.head_end is not None else parser.body_end
        if offset is None:
            offset = len(html_text)
        parser.edits.append((
            offset, offset, f'<style data-poster-math-font="{FONT_FACE}">{font_css()}</style>',
        ))
    return parser.render()


def strip_math_font_styles(html_text: str) -> str:
    """Keep skill-owned font bytes outside editable templates and asset mapping."""

    parser = _HtmlSourceEditor(html_text, lambda tag, attrs: {})
    parser.edits.extend((start, end, "") for start, end in parser.math_font_style_spans)
    return parser.render()


def _embed_annotated_assets(
    annotated_html: str,
    assets: list[dict[str, Any]],
) -> str:
    """Replace asset tokens after source-figure annotations have been bound."""

    mapping = {str(item["token"]): str(item["data_uri"]) for item in assets}
    native = {str(item["token"]): str(item["native_pptx"]) for item in assets if item.get("native_pptx")}
    annotated_html = _HtmlSourceEditor(
        annotated_html,
        lambda tag, attrs: {"data-native-pptx": native[attrs["src"]]}
        if tag == "img" and attrs.get("src") in native else {},
    ).render() if native else annotated_html
    unknown: set[str] = set()

    def replace(value: str) -> str:
        if value.startswith("asset://"):
            if not is_asset_token(value) or value not in mapping:
                unknown.add(value)
            else:
                return mapping[value]
        return value

    resolved = map_html_asset_references(annotated_html, replace)
    if unknown:
        raise ValueError("Unknown embedded figure token(s): " + ", ".join(sorted(unknown)))
    return resolved


def tokenize_embedded_images(
    html_text: str,
    *,
    preferred_tokens: Mapping[str, str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Replace embedded bytes while preserving durable asset tokens when supplied."""

    html_text = strip_math_font_styles(html_text)
    assets: list[dict[str, str]] = []
    tokens_by_uri: dict[tuple[str, str], str] = {}
    preferred = dict(preferred_tokens or {})
    used_tokens: set[str] = set()
    source_figure_hashes = _HtmlSourceEditor(html_text, lambda tag, attrs: {}).source_figure_hashes

    def replace(data_uri: str, native_pptx: str = "") -> str:
        if not data_uri.lower().startswith("data:image/"):
            return data_uri
        digest = image_assets.data_image_sha256(data_uri)
        if digest is None:
            if native_pptx:
                raise ValueError("Native PPTX component has an invalid browser preview")
            return data_uri
        if native_pptx:
            from posterlib.delivery.pptx_components import decode_component
            digest = hashlib.sha256(decode_component(native_pptx)).hexdigest()
        key = (data_uri, native_pptx)
        token = tokens_by_uri.get(key)
        if token is None:
            token = preferred.get(digest or "", "")
            if not is_asset_token(token) or token in used_tokens:
                next_index = 1
                while f"asset://{next_index}" in used_tokens:
                    next_index += 1
                token = f"asset://{next_index}"
            tokens_by_uri[key] = token
            used_tokens.add(token)
            assets.append(
                {
                    "token": token,
                    "data_uri": data_uri,
                    "content_sha256": digest or "",
                    "source_kind": (
                        "native_component" if native_pptx else
                        "pdf_figure" if digest in source_figure_hashes else "user_asset"
                    ),
                    **({"native_pptx": native_pptx} if native_pptx else {}),
                }
            )
        return token

    native_tokenized = _HtmlSourceEditor(
        html_text,
        lambda tag, attrs: {
            "src": replace(attrs.get("src", ""), attrs["data-native-pptx"]),
            "data-native-pptx": None,
        } if tag == "img" and attrs.get("data-native-pptx") else {},
    ).render()
    return map_html_asset_references(native_tokenized, replace), assets


def _validate_content_contract(
    report: dict[str, Any],
    *,
    resolved: str,
    content_contract: Mapping[str, Any],
    prepared_source_figure_sha256s: set[str] | None = None,
    facts: scientific_snapshot.ScientificHtmlFacts | None = None,
) -> dict[str, Any]:
    """Validate exact equations and source-figure membership, not composition."""

    parsed = facts or scientific_snapshot.parse_scientific_html(resolved)
    used_source_figures = parsed.source_figure_sha256s
    if prepared_source_figure_sha256s is not None:
        unknown_figures = sorted(used_source_figures - prepared_source_figure_sha256s)
    else:
        unknown_figures = []
    if unknown_figures:
        report = _append_issue(
            report,
            _issue(
                "unknown_source_figure",
                "The poster claims source-figure hashes outside the prepared asset "
                "manifest: " + ", ".join(unknown_figures),
            ),
        )
    planned_source_figures = content_contract.get("source_figure_sha256s")
    if isinstance(planned_source_figures, list):
        planned = {str(value) for value in planned_source_figures if str(value)}
        missing = sorted(planned - used_source_figures)
        unselected = sorted(used_source_figures - planned)
        if missing:
            report = _append_issue(
                report,
                _issue(
                    "missing_selected_source_figure",
                    "The poster omits source figures selected by the grounded evidence "
                    "plan: " + ", ".join(missing),
                ),
            )
        if unselected:
            report = _append_issue(
                report,
                _issue(
                    "unselected_source_figure",
                    "The poster uses prepared source figures outside the grounded "
                    "evidence selection: " + ", ".join(unselected),
                ),
            )
    raw_modules = content_contract.get("modules")
    equation_contract_issues: list[tuple[str, list[str], list[str]]] = []
    if isinstance(raw_modules, list):
        planned_module_ids = {
            str(module.get("module_id") or "")
            for module in raw_modules
            if isinstance(module, Mapping) and str(module.get("module_id") or "")
        }
        raw_observed_modules = parsed.snapshot.get("modules")
        observed_module_ids = (
            {str(module_id) for module_id in raw_observed_modules}
            if isinstance(raw_observed_modules, Mapping)
            else set()
        )
        missing_modules = sorted(planned_module_ids - observed_module_ids)
        unexpected_modules = sorted(observed_module_ids - planned_module_ids)
        if missing_modules:
            report = _append_issue(
                report,
                _issue(
                    "missing_planned_module",
                    "The poster omits grounded content module(s): "
                    + ", ".join(missing_modules),
                ),
            )
        if unexpected_modules:
            report = _append_issue(
                report,
                _issue(
                    "unexpected_module",
                    "Remove data-poster-module wrapper(s) not present in the grounded "
                    "content plan: " + ", ".join(unexpected_modules),
                ),
            )
        for module in raw_modules:
            if not isinstance(module, Mapping):
                continue
            module_id = str(module.get("module_id") or "")
            planned_components = module.get("native_component_sha256s")
            if isinstance(planned_components, list):
                observed_components = sorted(
                    digest for entry in (raw_observed_modules or {}).get(module_id, ())
                    for digest in entry.get("native_component_sha256s", ())
                )
                if sorted(planned_components) != observed_components:
                    report = _append_issue(report, _issue(
                        "native_component_mismatch",
                        f"Module {module_id!r} changed its selected native drawing components.",
                    ))
            planned_equations = module.get("equation_latex")
            if isinstance(planned_equations, (list, tuple)):
                expected_latex = [
                    _normalize_equation_latex(value) for value in planned_equations
                ]
                observed_latex = [
                    _normalize_equation_latex(value)
                    for value in parsed.equation_latex.get(module_id, ())
                ]
                if expected_latex != observed_latex:
                    equation_contract_issues.append(
                        (module_id, expected_latex, observed_latex)
                    )
    if equation_contract_issues:
        details = "; ".join(
            f"module_id={json.dumps(module_id, ensure_ascii=False)}, "
            f"expected_count={len(expected_latex)}, "
            f"observed_count={len(observed_latex)}, "
            "expected_data_latex="
            f"{json.dumps(expected_latex, ensure_ascii=False)}, "
            "observed_data_latex="
            f"{json.dumps(observed_latex, ensure_ascii=False)}"
            for module_id, expected_latex, observed_latex in equation_contract_issues
        )
        report = _append_issue(
            report,
            _issue(
                "equation_markup_mismatch",
                "For each listed module, render exactly expected_count visible semantic "
                "MathML equations in the listed order and copy the corresponding "
                "expected_data_latex strings into data-latex. Decode JSON string escaping "
                "exactly once: each JSON \\\\ represents one LaTeX backslash in the HTML "
                "attribute; encode < as &lt; inside the quoted attribute. Mismatches: "
                + details,
            ),
        )
    return report


def _annotate_asset_tokens(
    html_text: str,
    assets: list[dict[str, Any]],
) -> str:
    assets_by_token = {str(item["token"]): item for item in assets}

    def annotate(tag: str, attrs: dict[str, str]) -> dict[str, str | None]:
        if tag != "img":
            return {}
        asset = assets_by_token.get(attrs.get("src", ""))
        changes: dict[str, str | None] = {}
        if asset is not None and asset.get("source_kind") == "native_component":
            changes["data-native-component-sha256"] = str(asset["content_sha256"])
            for name in ("data-source-figure-sha256", "data-source-figure-aspect-ratio"):
                if name in attrs:
                    changes[name] = None
        elif not attrs.get("data-native-pptx") and "data-native-component-sha256" in attrs:
            changes["data-native-component-sha256"] = None
        if image_assets.is_source_figure_asset(asset):
            changes["data-source-figure-sha256"] = str(asset["content_sha256"])
        return {name: value for name, value in changes.items() if value != attrs.get(name)}

    return _HtmlSourceEditor(html_text, annotate).render()


def _append_issue(
    report: dict[str, Any],
    issue: dict[str, str],
) -> dict[str, Any]:
    return _append_issues(report, [issue])


def _append_warning(
    report: dict[str, Any],
    warning: dict[str, Any],
) -> dict[str, Any]:
    return {
        **report,
        "warnings": [*(report.get("warnings") or []), warning],
    }


def _append_issues(
    report: dict[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **report,
        "status": "error",
        "issues": [
            *[item for item in report.get("issues", []) if isinstance(item, dict)],
            *issues,
        ],
    }


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message, "severity": "warning"}


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": "error", "message": message}
