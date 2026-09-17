"""Static HTML validation and embedded-image binding for scientific posters."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

import poster_assets
import poster_core

from . import scientific_snapshot

_EMBEDDED_IMAGE_PATTERN = re.compile(
    r"data:image/(?:png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)
_IMAGE_ASSET_PATTERN = re.compile(
    r"\bsrc\s*=\s*([\"'])(asset://[0-9]+)\1",
    re.IGNORECASE,
)
_IMAGE_TAG_PATTERN = re.compile(
    r"<img\b(?:[^>\"']+|\"[^\"]*\"|'[^']*')*>",
    re.IGNORECASE,
)
_SOURCE_FIGURE_ATTR_PATTERN = re.compile(
    r"\bdata-source-figure-sha256\s*=\s*([\"'])([0-9a-f]{64})\1",
    re.IGNORECASE,
)
_SOURCE_FIGURE_ATTR_REMOVE_PATTERN = re.compile(
    r"\s+\bdata-source-figure-sha256\s*=\s*"
    r"(?:\"[0-9a-f]{64}\"|'[0-9a-f]{64}'|[0-9a-f]{64})",
    re.IGNORECASE,
)
_STYLE_ELEMENT_PATTERN = re.compile(
    r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL
)
_MATH_TYPE_START = "/* scientific-poster-math-type:start */"
_MATH_TYPE_END = "/* scientific-poster-math-type:end */"
_MATH_TYPE_PATTERN = re.compile(
    rf"\s*{re.escape(_MATH_TYPE_START)}.*?{re.escape(_MATH_TYPE_END)}\s*",
    re.DOTALL,
)


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
    if title and _canonical_identity_text(title) not in visible:
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
        occurrences = _canonical_author_text(" ".join(parsed.title_band_text)).count(
            expected
        )
        if parsed.author_marker_count != 1 or marked != expected:
            issues.append(
                _issue(
                    "missing_paper_authors",
                    "Render the complete verified author list once in the title band inside "
                    'data-poster-authors="verified".',
                )
            )
        elif occurrences != 1:
            issues.append(
                _issue(
                    "duplicate_paper_authors",
                    "Render the verified author list exactly once in the title band.",
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


def validate_candidate(
    html_text: str,
    *,
    assets: list[dict[str, Any]],
    required_source_figure_sha256s: set[str] | None = None,
    expected_page: dict[str, Any] | None = None,
    allow_adaptive_height: bool = False,
    content_contract: Mapping[str, Any] | None = None,
    paper_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate grounded HTML against assets, page size, and content integrity."""

    asset_source_figures = {
        str(item["content_sha256"])
        for item in assets
        if item.get("source_kind") == "pdf_figure"
    }
    requested_source_figures = {
        value for value in required_source_figure_sha256s or set() if value
    }
    prepared_source_figures = asset_source_figures or requested_source_figures
    try:
        annotated = _annotate_source_figure_tokens(html_text, assets)
        resolved = _embed_annotated_assets(annotated, assets)
    except ValueError as exc:
        return {"status": "error", "issues": [_issue("asset_token", str(exc))]}
    poster_facts = poster_core.parse_poster_html(resolved)
    report = poster_core.validate_poster_html(resolved, facts=poster_facts)
    needs_scientific_facts = bool(
        paper_identity or content_contract or "data-source-figure-sha256" in annotated
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
    if content_contract or "data-source-figure-sha256" in resolved:
        report = _validate_content_contract(
            report,
            resolved=annotated,
            content_contract=content_contract or {},
            prepared_source_figure_sha256s=prepared_source_figures,
            facts=scientific_facts,
        )
    if expected_page is not None and report.get("status") == "ok":
        issue = page_plan_issue(
            report.get("page"),
            expected_page,
            allow_adaptive_height=allow_adaptive_height,
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
    *,
    allow_adaptive_height: bool = False,
) -> dict[str, str] | None:
    """Return a physical-page issue, allowing only bounded adaptive height changes."""

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
    adaptive = allow_adaptive_height and expected.get("strategy") == "auto"
    if not adaptive:
        if not math.isclose(height, expected_height, abs_tol=0.01):
            return _issue(
                "page_changed",
                "HTML physical page dimensions differ from the active page plan.",
            )
        return None
    if math.isclose(height, expected_height, abs_tol=0.01):
        return None
    try:
        minimum = float(expected["min_height_mm"])
        maximum = float(expected["max_height_mm"])
    except (KeyError, TypeError, ValueError):
        return _issue("page_changed", "Adaptive page height bounds are invalid.")
    if height < minimum - 0.01 or height > maximum + 0.01:
        return _warning(
            "page_height_out_of_bounds",
            f"Adaptive page height is outside the planned {minimum:g}-{maximum:g} mm "
            "range; inspect whitespace, density, and overflow before publication.",
        )
    return None


def source_figure_sha256s(assets: list[dict[str, Any]]) -> set[str]:
    """Return verified PDF-figure image identities from an asset manifest."""

    return {
        str(item.get("content_sha256") or "")
        for item in assets
        if item.get("source_kind") == "pdf_figure"
        and re.fullmatch(r"[0-9a-f]{64}", str(item.get("content_sha256") or ""))
    }


def embed_assets(html_text: str, assets: list[dict[str, Any]]) -> str:
    """Bind source hashes and replace every known asset token with inert bytes."""

    annotated = _annotate_source_figure_tokens(html_text, assets)
    return _embed_annotated_assets(annotated, assets)


def _embed_annotated_assets(
    annotated_html: str,
    assets: list[dict[str, Any]],
) -> str:
    """Replace asset tokens after source-figure annotations have been bound."""

    mapping = {str(item["token"]): str(item["data_uri"]) for item in assets}
    used = set(re.findall(r"asset://\d+", annotated_html))
    unknown = sorted(used - set(mapping))
    if unknown:
        raise ValueError("Unknown embedded figure token(s): " + ", ".join(unknown))
    resolved = annotated_html
    for token in sorted(used, key=len, reverse=True):
        resolved = resolved.replace(token, mapping[token])
    return resolved


def tokenize_embedded_images(
    html_text: str,
    *,
    preferred_tokens: Mapping[str, str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Replace embedded bytes while preserving durable asset tokens when supplied."""

    assets: list[dict[str, str]] = []
    tokens_by_uri: dict[str, str] = {}
    preferred = dict(preferred_tokens or {})
    used_tokens: set[str] = set()
    source_figure_hashes = {
        match.group(2) for match in _SOURCE_FIGURE_ATTR_PATTERN.finditer(html_text)
    }

    def replace(match: re.Match[str]) -> str:
        data_uri = match.group(0)
        token = tokens_by_uri.get(data_uri)
        if token is None:
            digest = poster_assets.data_image_sha256(data_uri)
            token = preferred.get(digest or "", "")
            if not re.fullmatch(r"asset://[1-9]\d*", token) or token in used_tokens:
                next_index = 1
                while f"asset://{next_index}" in used_tokens:
                    next_index += 1
                token = f"asset://{next_index}"
            tokens_by_uri[data_uri] = token
            used_tokens.add(token)
            assets.append(
                {
                    "token": token,
                    "data_uri": data_uri,
                    "content_sha256": digest or "",
                    "source_kind": (
                        "pdf_figure" if digest in source_figure_hashes else "user_asset"
                    ),
                }
            )
        return token

    return _EMBEDDED_IMAGE_PATTERN.sub(replace, html_text), assets


def replace_single_stylesheet(html_text: str, stylesheet: str) -> str:
    """Replace the sole stylesheet without allowing body or evidence edits."""

    matches = list(_STYLE_ELEMENT_PATTERN.finditer(html_text))
    if len(matches) != 1:
        raise ValueError(
            "Style-only revision requires exactly one existing style element."
        )
    if not re.fullmatch(
        r"<style\b[^>]*>.*?</style>",
        stylesheet.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raise ValueError("Replacement must be exactly one complete style element.")
    replacement = stylesheet.strip()
    safety = _MATH_TYPE_PATTERN.search(matches[0].group())
    if safety is not None and _MATH_TYPE_START not in replacement:
        insertion = replacement.rfind("</style>")
        replacement = (
            replacement[:insertion]
            + "\n"
            + safety.group().strip()
            + "\n"
            + replacement[insertion:]
        )
    match = matches[0]
    return html_text[: match.start()] + replacement + html_text[match.end() :]


def append_stylesheet_override(html_text: str, stylesheet: str) -> str:
    """Append model-authored override rules without exposing or replacing base CSS."""

    matches = list(_STYLE_ELEMENT_PATTERN.finditer(html_text))
    if len(matches) != 1:
        raise ValueError(
            "Style-only revision requires exactly one existing style element."
        )
    override = stylesheet.strip()
    if not re.fullmatch(
        r"<style\b[^>]*>.*?</style>",
        override,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raise ValueError("Override must be exactly one complete style element.")
    opening_end = override.find(">")
    closing_start = override.lower().rfind("</style>")
    rules = override[opening_end + 1 : closing_start].strip()
    if not rules:
        raise ValueError("Stylesheet override must contain at least one rule.")
    match = matches[0]
    close_offset = match.group().lower().rfind("</style>")
    insertion = match.start() + close_offset
    patch = "\n/* scientific-poster-visual-override */\n" + rules + "\n"
    return html_text[:insertion] + patch + html_text[insertion:]


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
            planned_equations = module.get("equation_latex")
            if isinstance(planned_equations, (list, tuple)):
                expected_latex = [
                    _normalize_equation_latex(value) for value in planned_equations
                ]
                observed_latex = [
                    _normalize_equation_latex(value)
                    for value in parsed.equation_latex.get(module_id, ())
                ]
                if (
                    expected_latex != observed_latex
                    or any(not value for value in expected_latex)
                    or any(not value for value in observed_latex)
                ):
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


def _annotate_source_figure_tokens(
    html_text: str,
    assets: list[dict[str, Any]],
) -> str:
    source_figures = {
        str(item["token"]): str(item["content_sha256"])
        for item in assets
        if item.get("source_kind") == "pdf_figure"
    }
    if not source_figures:
        return html_text

    def annotate(match: re.Match[str]) -> str:
        tag = match.group(0)
        token_match = _IMAGE_ASSET_PATTERN.search(tag)
        if token_match is None:
            return tag
        digest = source_figures.get(token_match.group(2))
        if digest is None:
            return tag
        tag = _SOURCE_FIGURE_ATTR_REMOVE_PATTERN.sub("", tag)
        insertion = f' data-source-figure-sha256="{digest}"'
        return (
            tag[:-2].rstrip() + insertion + "/>"
            if tag.endswith("/>")
            else tag[:-1].rstrip() + insertion + ">"
        )

    return _IMAGE_TAG_PATTERN.sub(annotate, html_text)


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
