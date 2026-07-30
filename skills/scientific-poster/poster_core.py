"""Standard-library contracts for inert HTML scientific posters."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from types import MappingProxyType
from typing import Any
from xml.etree import ElementTree

from css_safety import offline_css_issues

ACTION_DRAFT = "draft"
ACTION_REVISE = "revise"
ACTION_INSPECT = "inspect"
ACTION_APPROVE = "approve"
ACTION_PREVIEW = "preview"
MAX_EMBEDDED_ASSET_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 32 * 1024 * 1024

PUBLIC_ACTIONS = frozenset(
    {
        ACTION_DRAFT,
        ACTION_REVISE,
        ACTION_INSPECT,
        ACTION_APPROVE,
        ACTION_PREVIEW,
        "validate",
        "query-resource",
        "propose-resource",
        "promote-resource",
        "rollback-resource",
    }
)


def build_preview_argv(
    html_path: str | Path,
    *,
    skill_dir: str | Path,
    python_executable: str,
) -> list[str]:
    """Return an injection-safe argv for the loopback live-preview proxy."""

    poster = Path(html_path).expanduser().resolve()
    if poster.name != "poster.html":
        raise ValueError("live preview requires a file named poster.html")
    return [
        str(python_executable),
        str(Path(skill_dir).resolve() / "scripts" / "preview_server.py"),
        "--root",
        str(poster.parent),
        "--port",
        "0",
    ]


def _outcome(status: str, blocking: bool, recoverable: bool) -> MappingProxyType:
    return MappingProxyType(
        {"status": status, "blocking": blocking, "recoverable": recoverable}
    )


OUTCOME_CONTRACTS = MappingProxyType(
    {
        "invalid_action": _outcome("error", False, True),
        "invalid_json": _outcome("error", False, True),
        "invalid_payload": _outcome("error", False, True),
        "runner_failed": _outcome("error", True, True),
        "missing_input": _outcome("error", False, True),
        "missing_html": _outcome("error", False, True),
        "source_too_large": _outcome("error", False, True),
        "llm_unavailable": _outcome("error", False, True),
        "llm_error": _outcome("error", False, True),
        "host_agent_required": _outcome("partial", False, True),
        "candidate_validation_failed": _outcome("error", False, True),
        "source_not_found": _outcome("error", False, True),
        "source_read_failed": _outcome("error", True, False),
        "source_html_invalid": _outcome("error", False, True),
        "stale_selection": _outcome("error", False, True),
        "invalid_selection": _outcome("error", False, True),
        "live_preview_update_failed": _outcome("error", False, True),
        "preview_ready": _outcome("ok", False, False),
        "poster_filename_required": _outcome("error", False, True),
        "poster_valid": _outcome("ok", False, False),
        "poster_invalid": _outcome("error", False, True),
        "inspection_complete": _outcome("ok", False, False),
        "inspection_unavailable": _outcome("partial", True, True),
        "invalid_inspection_options": _outcome("error", True, True),
        "html_not_found": _outcome("error", True, True),
        "inspection_output_failed": _outcome("error", True, True),
        "chromium_inspection_failed": _outcome("error", True, True),
        "dom_evaluation_failed": _outcome("error", True, True),
        "screenshot_failed": _outcome("error", True, True),
        "inspection_blocked": _outcome("error", True, True),
        "inspection_failed": _outcome("error", True, True),
        "missing_capability": _outcome("partial", True, True),
        "capabilities_ready": _outcome("ok", False, False),
        "capability_probe_failed": _outcome("error", True, True),
        "approval_required": _outcome("error", False, True),
        "poster_approval_recorded": _outcome("ok", False, False),
        "approval_receipt_untrusted": _outcome("error", True, True),
        "approval_source_mismatch": _outcome("error", True, True),
        "resource_query_complete": _outcome("ok", False, False),
        "resource_candidate_created": _outcome("ok", False, False),
        "resource_promoted": _outcome("ok", False, False),
        "resource_rollback_complete": _outcome("ok", False, False),
        "resource_conflict": _outcome("error", True, True),
        "resource_candidate_required": _outcome("partial", False, True),
        "promotion_approval_required": _outcome("error", False, True),
        "resource_identity_changed": _outcome("error", True, True),
        "rollback_target_missing": _outcome("error", False, True),
    }
)

_REQUIRED_REGIONS = ("hero", "method", "evidence", "limitations", "provenance")
_ACTIVE_TAGS = {
    "animate",
    "animatemotion",
    "animatetransform",
    "audio",
    "base",
    "button",
    "details",
    "discard",
    "dialog",
    "embed",
    "foreignobject",
    "form",
    "iframe",
    "input",
    "link",
    "marquee",
    "object",
    "script",
    "select",
    "set",
    "summary",
    "textarea",
    "video",
}
_INTERACTIVE_ATTRIBUTES = {
    "autofocus",
    "contenteditable",
    "popover",
    "popovertarget",
    "popovertargetaction",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_RESOURCE_ATTRIBUTES = {
    "action",
    "archive",
    "background",
    "cite",
    "data",
    "formaction",
    "href",
    "imagesrcset",
    "poster",
    "src",
    "srcset",
    "xlink:href",
}
_SVG_PRESENTATION_ATTRIBUTES = {
    "clip-path",
    "cursor",
    "fill",
    "filter",
    "marker",
    "marker-end",
    "marker-mid",
    "marker-start",
    "mask",
    "stroke",
}
_PLACEHOLDER_RE = re.compile(
    r"\b(?:lorem ipsum|placeholder|todo|tbd|replace this|insert (?:text|figure|image))\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(
    r"(?<![\w.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:[eE][+-]?\d+)?(?:%(?!\w)|(?![\w.]))"
)
_SOURCE_NUMBER_RE = re.compile(
    r"(?<![\d.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:[eE][+-]?\d+)?(?:%(?!\w)|(?![\d.]))"
)
_SOURCE_LOCATOR_RE = re.compile(
    r"(?:\b(?:abstract|references|title\s+page|grounded\s+brief)\b|"
    r"\b(?:figure|fig\.?|table|page|p\.?)\s*\d+\b|"
    r"§\s*\d+|\b(?:doi|arxiv)\s*:)",
    re.IGNORECASE,
)
_SOURCE_PAGE_MARKER_RE = re.compile(r"(?m)^\[Page\s+(\d+)\]\s*$", re.IGNORECASE)
_LABEL_PAGE_RE = re.compile(r"\b(?:page|p\.?)\s*(\d+)\b", re.IGNORECASE)
_LABEL_FIGURE_RE = re.compile(r"\b(?:figure|fig\.?)\s*(\d+)\b", re.IGNORECASE)
_LABEL_TABLE_RE = re.compile(r"\btable\s*(\d+)\b", re.IGNORECASE)
_LABEL_SECTION_RE = re.compile(r"§\s*(\d+(?:\.\d+)*)", re.IGNORECASE)
_PAGE_SIZE_RE = re.compile(
    r"@page\s*(?:[^\{]*)\{[^{}]*?\bsize\s*:\s*"
    r"([0-9]+(?:\.[0-9]+)?)mm\s+([0-9]+(?:\.[0-9]+)?)mm\s*;?",
    re.IGNORECASE | re.DOTALL,
)
_REMOTE_RE = re.compile(r"^(?:https?:)?//|^file:|^javascript:|^vbscript:", re.IGNORECASE)
COMPONENT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
POSTER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
COMPONENT_VERSION_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
REGION_RE = re.compile(r"^[a-z][a-z0-9-]{0,127}$")
_DATA_IMAGE_RE = re.compile(
    r"^data:image/(?:png|jpeg|gif|webp|svg\+xml);base64,([A-Za-z0-9+/=]+)$",
    re.IGNORECASE,
)


def normalize_action(value: Any, *, default: str = ACTION_DRAFT) -> str:
    """Normalize one public verb-object action without compatibility aliases."""

    action = str(value or default).strip().lower()
    if action not in PUBLIC_ACTIONS:
        raise ValueError(f"invalid_action: {action}")
    return action


def outcome_result(code: str, *, summary: str, **details: Any) -> dict[str, Any]:
    """Return stable status semantics shared by every Skill entry point."""

    contract = OUTCOME_CONTRACTS.get(code)
    if contract is None:
        raise ValueError(f"unknown outcome code: {code}")
    result = dict(details)
    result.update(
        {
            "status": contract["status"],
            "outcome": {"code": code},
            "summary": summary,
            "blocking": contract["blocking"],
            "recoverable": contract["recoverable"],
        }
    )
    return result


def source_figure_manifest_sha256(values: Any) -> str:
    """Hash a canonical set of prepared PDF-figure identities."""

    try:
        hashes = sorted({str(value) for value in values})
    except TypeError as exc:
        raise ValueError("source-figure manifest must be iterable") from exc
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes):
        raise ValueError("source-figure manifest contains an invalid SHA-256")
    raw = json.dumps(hashes, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(b"scientific-poster-source-figures-v1\0" + raw).hexdigest()


def poster_approval_phrase(
    html_sha256: str,
    grounding_source_sha256: str,
    source_figure_manifest_sha256: str,
) -> str:
    """Return the exact approval phrase bound to HTML, source, and source figures."""

    hashes = (html_sha256, grounding_source_sha256, source_figure_manifest_sha256)
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes):
        raise ValueError("approval phrase hashes must be lowercase SHA-256 digests")
    return (
        f"APPROVE SCIENTIFIC-POSTER poster {html_sha256} "
        f"source {grounding_source_sha256} figures {source_figure_manifest_sha256}"
    )


def normalize_outcome_result(
    value: Any,
    *,
    fallback_code: str,
    fallback_summary: str,
) -> dict[str, Any]:
    """Rebuild an untrusted child result with registered outcome semantics."""

    source = value if isinstance(value, dict) else {}
    raw_outcome = source.get("outcome")
    raw_code = raw_outcome.get("code") if isinstance(raw_outcome, dict) else None
    code = raw_code if isinstance(raw_code, str) and raw_code in OUTCOME_CONTRACTS else fallback_code
    raw_summary = source.get("summary")
    summary = raw_summary.strip() if isinstance(raw_summary, str) else ""
    details = {
        key: item
        for key, item in source.items()
        if key not in {"status", "outcome", "summary", "blocking", "recoverable"}
    }
    return outcome_result(code, summary=summary or fallback_summary, **details)


def prepare_asset_manifest(
    values: Any,
    *,
    resolve: Callable[[str], Path | None],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve supplied image assets into bounded, inert data URI records."""

    if values is None:
        items: list[Any] = []
    elif isinstance(values, (str, Path, dict)):
        items = [values]
    else:
        try:
            items = list(values)
        except TypeError:
            items = [values]
    manifest: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, value in enumerate(items, start=1):
        source, description = _asset_source_and_description(value)
        token = f"asset://{index}"
        if not source:
            warnings.append(f"{token}: asset source is missing.")
            continue
        try:
            resolved = resolve(source)
        except (OSError, RuntimeError, ValueError) as exc:
            warnings.append(f"{token}: asset could not be resolved: {exc}")
            continue
        path = Path(resolved) if resolved is not None else None
        if path is None or not path.is_file() or path.is_symlink():
            warnings.append(f"{token}: regular asset file not found: {source}")
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            warnings.append(f"{token}: asset could not be read: {exc}")
            continue
        if len(content) > MAX_EMBEDDED_ASSET_BYTES:
            warnings.append(f"{token}: asset exceeds the embedded-image byte limit.")
            continue
        mime = _detect_image_mime(content)
        if mime is None:
            warnings.append(f"{token}: unsupported image type: {path.name}")
            continue
        if mime == "image/svg+xml" and not _svg_is_safe(content):
            warnings.append(f"{token}: SVG contains active or external content.")
            continue
        content_sha256 = hashlib.sha256(content).hexdigest()
        source_kind = (
            str(value.get("source_kind") or "user_asset")
            if isinstance(value, dict)
            else "user_asset"
        )
        if source_kind not in {"pdf_figure", "user_asset"}:
            warnings.append(f"{token}: unsupported asset provenance: {source_kind}")
            continue
        claimed_sha256 = (
            str(value.get("content_sha256") or "")
            if isinstance(value, dict)
            else ""
        )
        if claimed_sha256 and claimed_sha256 != content_sha256:
            warnings.append(f"{token}: source image hash changed before embedding.")
            continue
        manifest.append(
            {
                "token": token,
                "source": source,
                "filename": path.name,
                "mime": mime,
                "description": description,
                "bytes": len(content),
                "content_sha256": content_sha256,
                "source_kind": source_kind,
                "data_uri": f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}",
                **(
                    {
                        "figure_number": value.get("figure_number"),
                        "page": value.get("page"),
                        "crop_bbox": value.get("crop_bbox"),
                    }
                    if isinstance(value, dict) and source_kind == "pdf_figure"
                    else {}
                ),
            }
        )
    return manifest, warnings


class _PosterParser(HTMLParser):
    """Collect semantic, safety, and grounding facts from one HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doctypes: list[str] = []
        self.html_count = 0
        self.body_count = 0
        self.poster_roots = 0
        self.poster_ids: list[str] = []
        self.regions: Counter[str] = Counter()
        self.region_text: dict[str, list[str]] = {name: [] for name in _REQUIRED_REGIONS}
        self.styles: list[str] = []
        self.visible_text: list[str] = []
        self.integrity_attributes: list[tuple[str, ...]] = []
        self.source_labels: list[str] = []
        self.visible_source_figure_sha256s: set[str] = set()
        self.issues: list[dict[str, str]] = []
        self._stack: list[tuple[str, str | None, bool]] = []
        self._style_depth = 0

    def handle_decl(self, decl: str) -> None:
        self.doctypes.append(decl.strip().lower())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs, self_closing=tag.lower() in _VOID_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs, self_closing=True)

    def _start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        names = [name.lower() for name, _ in attrs]
        if len(names) != len(set(names)):
            self.issues.append(_issue("duplicate_attribute", f"<{tag}> repeats an attribute."))
        attr = {name.lower(): str(value or "") for name, value in attrs}
        protected = (
            tag,
            attr.get("data-poster-id", "").strip(),
            attr.get("data-poster-region", "").strip(),
            attr.get("data-source-label", "").strip(),
            attr.get("data-component-id", "").strip(),
            attr.get("data-component-version", "").strip(),
            attr.get("alt", "").strip(),
        )
        if any(protected[1:]):
            self.integrity_attributes.append(protected)
        if tag == "html":
            self.html_count += 1
        elif tag == "body":
            self.body_count += 1
        if tag in _ACTIVE_TAGS:
            self.issues.append(_issue("active_html", f"Active element <{tag}> is forbidden."))
        if tag == "meta" and not (
            names == ["charset"] and attr.get("charset", "").strip().lower() == "utf-8"
        ):
            self.issues.append(
                _issue("active_html", "Only <meta charset=\"utf-8\"> is allowed.")
            )
        if any(name.startswith("on") for name in attr):
            self.issues.append(_issue("event_handler", f"<{tag}> contains an event handler."))
        if any(name in _INTERACTIVE_ATTRIBUTES for name in attr):
            self.issues.append(
                _issue("interactive_html", f"<{tag}> contains an interactive attribute.")
            )
        hidden = "display:none" in attr.get("style", "").replace(" ", "").lower() or (
            "visibility:hidden" in attr.get("style", "").replace(" ", "").lower()
        )
        poster_id = attr.get("data-poster-id", "").strip()
        if poster_id:
            self.poster_ids.append(poster_id)
            if POSTER_ID_RE.fullmatch(poster_id) is None:
                self.issues.append(
                    _issue(
                        "poster_id",
                        "data-poster-id must use 1-200 stable ASCII identifier characters.",
                    )
                )
        region = attr.get("data-poster-region", "").strip()
        if (
            tag in {"main", "article", "div"}
            and poster_id
            and not region
            and self._stack
            and self._stack[-1][0] == "body"
        ):
            self.poster_roots += 1
        if region:
            self.regions[region] += 1
            if not poster_id:
                self.issues.append(
                    _issue("region_id", f"Region {region!r} needs a stable data-poster-id.")
                )
            source_label = attr.get("data-source-label", "").strip()
            if not source_label:
                self.issues.append(
                    _issue("source_label", f"Region {region!r} needs data-source-label.")
                )
            elif _SOURCE_LOCATOR_RE.search(source_label) is None:
                self.issues.append(
                    _issue(
                        "source_locator",
                        f"Region {region!r} needs a page, section, figure, table, or bibliography locator.",
                    )
                )
            else:
                self.source_labels.append(source_label)
        component_id = attr.get("data-component-id", "").strip()
        component_version = attr.get("data-component-version", "").strip()
        if bool(component_id) != bool(component_version):
            self.issues.append(
                _issue(
                    "component_identity",
                    "Optional data-component-id and data-component-version must appear together.",
                )
            )
        elif component_id and (
            COMPONENT_ID_RE.fullmatch(component_id) is None
            or COMPONENT_VERSION_RE.fullmatch(component_version) is None
        ):
            self.issues.append(
                _issue(
                    "component_identity",
                    "Component ids must use kebab-case and semantic versions.",
                )
            )
        for name, value in attr.items():
            if name in _RESOURCE_ATTRIBUTES and not _safe_embedded_reference(value):
                self.issues.append(
                    _issue("external_resource", f"<{tag}> {name} is not an inert embedded reference.")
                )
            if name in _SVG_PRESENTATION_ATTRIBUTES and not _safe_svg_css(value):
                self.issues.append(
                    _issue("external_css", f"<{tag}> {name} contains an unsafe CSS reference.")
                )
        if tag == "img" and not attr.get("alt", "").strip():
            self.issues.append(_issue("image_alt", "Every poster image needs meaningful alt text."))
        if tag == "img" and attr.get("data-source-figure-sha256"):
            claimed_figure_sha256 = attr["data-source-figure-sha256"].strip()
            actual_figure_sha256 = data_image_sha256(attr.get("src", ""))
            if (
                re.fullmatch(r"[0-9a-f]{64}", claimed_figure_sha256) is None
                or actual_figure_sha256 != claimed_figure_sha256
            ):
                self.issues.append(
                    _issue(
                        "source_figure_identity",
                        "Source-figure identity must match the embedded image bytes.",
                    )
                )
            elif not hidden and not any(item[2] for item in self._stack):
                self.visible_source_figure_sha256s.add(claimed_figure_sha256)
        if "style" in attr:
            self._check_css(attr["style"])
        if tag == "style":
            self._style_depth += 1
        if not self_closing:
            inherited = next((item[1] for item in reversed(self._stack) if item[1]), None)
            self._stack.append((tag, region or inherited, hidden))

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if not self._stack:
            self.issues.append(_issue("malformed_html", f"Unexpected closing tag </{tag}>."))
            return
        open_tag, _, _ = self._stack.pop()
        if open_tag != lowered:
            self.issues.append(
                _issue("malformed_html", f"Expected </{open_tag}> before </{lowered}>.")
            )
        if lowered == "style" and self._style_depth:
            self._style_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            self.styles.append(data)
            self._check_css(data)
            return
        text = " ".join(data.split())
        if not text or any(hidden for _, _, hidden in self._stack):
            return
        self.visible_text.append(text)
        region = next((item[1] for item in reversed(self._stack) if item[1]), None)
        if region in self.region_text:
            self.region_text[region].append(text)

    def _check_css(self, css: str) -> None:
        issues = offline_css_issues(css, safe_reference=_safe_embedded_reference)
        if "unsafe_syntax" in issues:
            self.issues.append(
                _issue("unsafe_css", "CSS escapes and ambiguous syntax are forbidden.")
            )
        if "active_construct" in issues:
            self.issues.append(_issue("active_css", "CSS imports, animation, and transitions are forbidden."))
        if "unsafe_reference" in issues:
            self.issues.append(
                _issue(
                    "external_css",
                    "CSS references must use supported inert embedded images.",
                )
            )


def validate_poster_html(html_text: str, *, source_text: str = "") -> dict[str, Any]:
    """Validate one complete, offline, semantically selectable poster document."""

    issues: list[dict[str, str]] = []
    raw = html_text.encode("utf-8")
    if len(raw) > MAX_HTML_BYTES:
        issues.append(_issue("document_size", "Poster HTML exceeds the byte limit."))
    if not html_text.lower().startswith("<!doctype html>"):
        issues.append(_issue("doctype", "Poster must begin exactly with <!doctype html>."))
    if "asset://" in html_text:
        issues.append(_issue("asset_token", "Poster contains an unresolved asset token."))
    parser = _PosterParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - parser errors become contract issues
        issues.append(_issue("malformed_html", f"HTML parser failed: {exc}"))
    if parser._stack:
        unclosed = ", ".join(tag for tag, _, _ in parser._stack[-5:])
        issues.append(_issue("malformed_html", f"Poster has unclosed element(s): {unclosed}."))
    issues.extend(parser.issues)
    if parser.doctypes != ["doctype html"]:
        issues.append(_issue("doctype", "Poster needs exactly one HTML5 doctype."))
    if parser.html_count != 1 or parser.body_count != 1:
        issues.append(_issue("document_root", "Poster needs exactly one html and one body element."))
    if parser.poster_roots != 1:
        issues.append(
            _issue(
                "poster_root",
                "Poster needs exactly one body-level main, article, or div with "
                f"data-poster-id; found {parser.poster_roots}.",
            )
        )
    duplicates = sorted(name for name, count in Counter(parser.poster_ids).items() if count > 1)
    if duplicates:
        issues.append(_issue("duplicate_poster_id", "Duplicate data-poster-id: " + ", ".join(duplicates)))
    for region in _REQUIRED_REGIONS:
        if parser.regions[region] != 1:
            issues.append(
                _issue(
                    "semantic_region",
                    f"Poster needs exactly one visible {region!r} region; "
                    f"found {parser.regions[region]}.",
                )
            )
        elif not " ".join(parser.region_text[region]).strip():
            issues.append(_issue("empty_region", f"Poster region {region!r} has no visible text."))
    unknown_regions = sorted(set(parser.regions) - set(_REQUIRED_REGIONS))
    if unknown_regions:
        issues.append(
            _issue("semantic_region", "Unknown data-poster-region: " + ", ".join(unknown_regions))
        )
    css = "\n".join(parser.styles)
    page_match = _PAGE_SIZE_RE.search(css)
    page: dict[str, float] | None = None
    if page_match is None:
        issues.append(_issue("physical_page", "Inline CSS must declare @page size in millimetres."))
    else:
        width = float(page_match.group(1))
        height = float(page_match.group(2))
        if not (200 <= width <= 2000 and 200 <= height <= 2000):
            issues.append(_issue("physical_page", "Poster page dimensions are outside safe bounds."))
        else:
            page = {"width_mm": width, "height_mm": height}
    visible = " ".join(parser.visible_text)
    if _PLACEHOLDER_RE.search(visible):
        issues.append(_issue("placeholder_copy", "Poster contains placeholder copy."))
    if source_text:
        issues.extend(_source_locator_issues(parser.source_labels, source_text))
        source_numbers = {_number_key(value) for value in _SOURCE_NUMBER_RE.findall(source_text)}
        poster_numbers = {_number_key(value) for value in _NUMBER_RE.findall(visible)}
        ungrounded = sorted(poster_numbers - source_numbers)
        if ungrounded:
            issues.append(
                _issue("ungrounded_number", "Visible numbers absent from source: " + ", ".join(ungrounded))
            )
        visible_lower = visible.lower()
        source_lower = source_text.lower()
        unsupported_rights = [
            phrase
            for phrase in ("reproduced with permission", "all rights reserved")
            if phrase in visible_lower and phrase not in source_lower
        ]
        if "©" in visible and "©" not in source_text:
            unsupported_rights.append("copyright symbol")
        if unsupported_rights:
            issues.append(
                _issue(
                    "ungrounded_rights_claim",
                    "Rights or permission language is absent from the source: "
                    + ", ".join(unsupported_rights),
                )
            )
    return {
        "status": "error" if issues else "ok",
        "issues": issues,
        "page": page,
        "poster_ids": sorted(parser.poster_ids),
        "regions": {name: parser.regions[name] for name in _REQUIRED_REGIONS},
    }


def _source_locator_issues(
    source_labels: list[str],
    source_text: str,
) -> list[dict[str, str]]:
    """Reject concrete locators that cannot exist in the supplied source."""

    pages = {int(value) for value in _SOURCE_PAGE_MARKER_RE.findall(source_text)}
    source_lower = source_text.lower()
    invalid: set[str] = set()
    for label in source_labels:
        for value in _LABEL_PAGE_RE.findall(label):
            if pages and int(value) not in pages:
                invalid.add(f"p.{value}")
        for value in _LABEL_FIGURE_RE.findall(label):
            if re.search(rf"\b(?:figure|fig\.?)\s*{re.escape(value)}\b", source_text, re.IGNORECASE) is None:
                invalid.add(f"Figure {value}")
        for value in _LABEL_TABLE_RE.findall(label):
            if re.search(rf"\btable\s*{re.escape(value)}\b", source_text, re.IGNORECASE) is None:
                invalid.add(f"Table {value}")
        for value in _LABEL_SECTION_RE.findall(label):
            heading = re.compile(
                rf"(?m)^\s*{re.escape(value)}(?:\s|\.)",
                re.IGNORECASE,
            )
            if pages and heading.search(source_text) is None:
                invalid.add(f"§{value}")
        if "abstract" in label.lower() and pages and "abstract" not in source_lower:
            invalid.add("Abstract")
        if "references" in label.lower() and pages and "references" not in source_lower:
            invalid.add("References")
    if not invalid:
        return []
    return [
        _issue(
            "source_locator_mismatch",
            "Source locator(s) are absent from the supplied paper: " + ", ".join(sorted(invalid)),
        )
    ]


def poster_content_fingerprint(html_text: str) -> tuple[tuple[str, ...], ...]:
    """Freeze scientific copy and semantic identities across CSS-only repair calls."""

    parser = _PosterParser()
    parser.feed(html_text)
    parser.close()
    asset_tokens = tuple(re.findall(r"asset://[0-9]+", html_text))
    return (
        tuple(parser.visible_text),
        tuple("\x1f".join(values) for values in parser.integrity_attributes),
        asset_tokens,
    )


def data_image_sha256(value: str) -> str | None:
    """Hash one bounded embedded image URI, or return ``None`` when invalid."""

    match = _DATA_IMAGE_RE.fullmatch(value.strip())
    if match is None:
        return None
    try:
        content = base64.b64decode(match.group(1), validate=True)
    except (ValueError, TypeError):
        return None
    if len(content) > MAX_EMBEDDED_ASSET_BYTES or _detect_image_mime(content) is None:
        return None
    return hashlib.sha256(content).hexdigest()


def source_figure_usage_issues(
    html_text: str,
    expected_sha256s: set[str] | tuple[str, ...] | list[str],
) -> list[dict[str, str]]:
    """Require one statically visible, byte-matched figure from a prepared PDF."""

    expected = {str(value) for value in expected_sha256s}
    if not expected:
        return []
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in expected):
        return [_issue("source_figure_identity", "Expected source-figure hashes are invalid.")]
    parser = _PosterParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - caller merges parser failure into issues
        return [_issue("malformed_html", f"HTML parser failed: {exc}")]
    if expected.isdisjoint(parser.visible_source_figure_sha256s):
        return [
            _issue(
                "missing_source_figure",
                "Use at least one visible figure extracted from the supplied PDF.",
            )
        ]
    return []


class _PosterIdentityParser(HTMLParser):
    """Index stable ids with inherited semantic and component identity."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str, tuple[str, str] | None]] = []
        self.elements: dict[str, dict[str, str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs, self_closing=tag.lower() in _VOID_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs, self_closing=True)

    def _start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        attributes = {name.lower(): str(value or "") for name, value in attrs}
        inherited_region = self.stack[-1][1] if self.stack else ""
        inherited_component = self.stack[-1][2] if self.stack else None
        region = attributes.get("data-poster-region", "").strip() or inherited_region
        component_id = attributes.get("data-component-id", "").strip()
        component_version = attributes.get("data-component-version", "").strip()
        component = (
            (component_id, component_version)
            if component_id and component_version
            else inherited_component
        )
        poster_id = attributes.get("data-poster-id", "").strip()
        if poster_id:
            if poster_id in self.elements:
                raise ValueError(f"poster_id is not unique in source HTML: {poster_id}")
            identity = {"semantic_region": region}
            if component is not None:
                identity["component_id"], identity["component_version"] = component
            self.elements[poster_id] = identity
        if not self_closing:
            self.stack.append((tag, region, component))

    def handle_endtag(self, tag: str) -> None:
        del tag
        if self.stack:
            self.stack.pop()


def poster_identity_map(html_text: str) -> dict[str, dict[str, str]]:
    """Return exact live-selection identities from canonical poster HTML."""

    parser = _PosterIdentityParser()
    parser.feed(html_text)
    parser.close()
    return parser.elements


def _asset_source_and_description(value: Any) -> tuple[str, str]:
    if isinstance(value, dict):
        source = next(
            (value.get(key) for key in ("uri", "path", "source", "file") if value.get(key)),
            "",
        )
        return str(source).strip(), str(value.get("description") or value.get("alt") or "").strip()
    return str(value or "").strip(), ""


def _detect_image_mime(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    try:
        root = ElementTree.fromstring(content.decode("utf-8-sig"))
    except (ElementTree.ParseError, UnicodeDecodeError):
        return None
    return "image/svg+xml" if root.tag.rsplit("}", 1)[-1].lower() == "svg" else None


def _svg_is_safe(content: bytes) -> bool:
    try:
        text = content.decode("utf-8-sig")
        root = ElementTree.fromstring(text)
    except (ElementTree.ParseError, UnicodeDecodeError):
        return False
    if "<!doctype" in text.lower() or "<?xml-stylesheet" in text.lower():
        return False
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in _ACTIVE_TAGS:
            return False
        for raw_name, raw_value in element.attrib.items():
            name = raw_name.rsplit("}", 1)[-1].lower()
            value = str(raw_value or "").strip()
            if name.startswith("on") or not _safe_svg_css(value):
                return False
            if name in {"href", "src"} and not _safe_embedded_reference(value):
                return False
        if tag == "style" and not _safe_svg_css(element.text or ""):
            return False
    return True


def _safe_svg_css(css: str) -> bool:
    return not offline_css_issues(css, safe_reference=_safe_embedded_reference)


def _safe_embedded_reference(value: str) -> bool:
    candidate = value.strip()
    if not candidate or candidate.startswith("#"):
        return True
    if _REMOTE_RE.search(candidate):
        return False
    match = _DATA_IMAGE_RE.fullmatch(candidate)
    if match is None:
        return False
    try:
        decoded = base64.b64decode(match.group(1), validate=True)
    except ValueError:
        return False
    if len(decoded) > MAX_EMBEDDED_ASSET_BYTES:
        return False
    if candidate.lower().startswith("data:image/svg+xml"):
        return _svg_is_safe(decoded)
    return _detect_image_mime(decoded) is not None


def _number_key(value: str) -> str:
    return value.replace(",", "").lower()


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message, "severity": "error"}
