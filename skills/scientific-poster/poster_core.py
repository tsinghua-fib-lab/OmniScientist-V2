"""Standard-library contracts for inert HTML scientific posters."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from pathlib import Path
from types import MappingProxyType
from typing import Any

import poster_assets
from css_safety import offline_css_issues

ACTION_DRAFT = "draft"
ACTION_ESTIMATE = "estimate"
ACTION_REVISE = "revise"
ACTION_INSPECT = "inspect"
ACTION_APPROVE = "approve"
ACTION_PREVIEW = "preview"
ACTION_EXPORT_PPTX = "export-pptx"
ACTION_PREPARE_VISUAL_REVIEW = "prepare-visual-review"
ACTION_SUBMIT_VISUAL_REVIEW = "submit-visual-review"
MAX_HTML_BYTES = 32 * 1024 * 1024
POSTER_ROOT_SELECTOR = (
    "body > main[data-poster-id], body > article[data-poster-id], "
    "body > div[data-poster-id]"
)

PUBLIC_ACTIONS = frozenset(
    {
        ACTION_DRAFT,
        ACTION_ESTIMATE,
        ACTION_REVISE,
        ACTION_INSPECT,
        ACTION_APPROVE,
        ACTION_PREVIEW,
        ACTION_EXPORT_PPTX,
        ACTION_PREPARE_VISUAL_REVIEW,
        ACTION_SUBMIT_VISUAL_REVIEW,
        "validate",
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
        "estimate_complete": _outcome("ok", False, False),
        "invalid_content_budget": _outcome("error", False, True),
        "invalid_page": _outcome("error", False, True),
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
        "pptx_export_complete": _outcome("ok", False, False),
        "pptx_export_failed": _outcome("error", True, True),
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
        "visual_review_unavailable": _outcome("partial", False, True),
        "visual_revision_required": _outcome("partial", False, True),
        "visual_review_passed": _outcome("ok", False, False),
        "visual_review_failed": _outcome("error", True, False),
        "visual_review_invalid": _outcome("error", False, True),
        "missing_capability": _outcome("partial", True, True),
        "capabilities_ready": _outcome("ok", False, False),
        "capability_probe_failed": _outcome("error", True, True),
        "approval_required": _outcome("error", False, True),
        "poster_approval_recorded": _outcome("ok", False, False),
        "approval_receipt_untrusted": _outcome("error", True, True),
        "approval_source_mismatch": _outcome("error", True, True),
    }
)

SEMANTIC_ROLES = frozenset(
    {"claim", "context", "method", "evidence", "limitation", "provenance"}
)
MODULE_PRIORITIES = frozenset({"focal", "primary", "supporting", "footer"})
ORGANIZATION_MODES = frozenset(
    {"scan-first", "figure-led", "method-led", "result-led", "narrative"}
)
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
    r"(?:\blorem ipsum\b|\breplace this\b|\binsert (?:text|figure|image)(?: here)?\b|"
    r"\[(?:todo|tbd|placeholder)\]|\{\{[^{}]+\}\})",
    re.IGNORECASE,
)
_RIGHTS_CLAIM_PATTERNS = (
    re.compile(r"\ball\s+rights\s+reserved\b", re.IGNORECASE),
    re.compile(
        r"\b(?:reproduced|reprinted|republished|adapted|used|included|provided)"
        r"\s+(?:with|by|under)\s+(?:the\s+)?(?:[\w-]+\s+){0,3}permission\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bpermission\s+(?:(?:was|is|has\s+been)\s+)?"
        r"(?:granted|obtained)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:©|\bcopyright(?:ed)?\b)", re.IGNORECASE),
)
_SOURCE_LOCATOR_RE = re.compile(
    r"(?:\b(?:abstract|bibliography|references|title\s+page|grounded\s+brief)\b|"
    r"\b(?:figures?|figs?\.?|tables?|pages?|pp?\.?)\s*\d+\b|"
    r"\b(?:equations?|eqs?\.?)\s*\(?\d+(?:\.\d+)*\)?(?![\d.])|"
    r"(?:§{1,2}|\b(?:sections?|secs?\.?))\s*\d+(?:\.\d+)*\b|"
    r"\b(?:doi|arxiv)\s*:)",
    re.IGNORECASE,
)
_SOURCE_PAGE_MARKER_RE = re.compile(r"(?m)^\[Page\s+(\d+)\]\s*$", re.IGNORECASE)
_LABEL_PAGE_RE = re.compile(r"\b(?:pages?|pp?\.?)\s*(\d+)\b", re.IGNORECASE)
_LABEL_FIGURE_RE = re.compile(r"\b(?:figures?|figs?\.?)\s*(\d+)\b", re.IGNORECASE)
_LABEL_TABLE_RE = re.compile(r"\btables?\s*(\d+)\b", re.IGNORECASE)
_LABEL_EQUATION_RE = re.compile(
    r"\b(?:equations?|eqs?\.?)\s*\(?(\d+(?:\.\d+)*)\)?",
    re.IGNORECASE,
)
_LABEL_SECTION_RE = re.compile(
    r"(?:§{1,2}|\b(?:sections?|secs?\.?))\s*(\d+(?:\.\d+)*)",
    re.IGNORECASE,
)
_PAGE_SIZE_RE = re.compile(
    r"@page\s*(?:[^\{]*)\{[^{}]*?\bsize\s*:\s*"
    r"([0-9]+(?:\.[0-9]+)?)mm\s+([0-9]+(?:\.[0-9]+)?)mm\s*;?",
    re.IGNORECASE | re.DOTALL,
)
_MATH_LAYOUT_OVERRIDE_RE = re.compile(
    r"(?:\bmath\b|\[\s*data-content-role\s*=\s*['\"]?equation['\"]?\s*\]"
    r"|\[\s*data-latex(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\]\s]+))?\s*\])"
    r"[^{}]*\{[^{}]*\bdisplay\s*:\s*(?:block|flex|grid|inline-block)\s*(?:;|})",
    re.IGNORECASE | re.DOTALL,
)
_UNESCAPED_MATH_LESS_THAN_RE = re.compile(
    r"<(?:mi|mn|mo|mtext|ms)\b[^>]*>\s*<\s*</",
    re.IGNORECASE,
)
POSTER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
POSTER_MODULE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


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
    code = (
        raw_code
        if isinstance(raw_code, str) and raw_code in OUTCOME_CONTRACTS
        else fallback_code
    )
    raw_summary = source.get("summary")
    summary = raw_summary.strip() if isinstance(raw_summary, str) else ""
    details = {
        key: item
        for key, item in source.items()
        if key not in {"status", "outcome", "summary", "blocking", "recoverable"}
    }
    return outcome_result(code, summary=summary or fallback_summary, **details)


class ParsedPosterHtml(HTMLParser):
    """Collect semantic, safety, and grounding facts from one HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doctypes: list[str] = []
        self.html_count = 0
        self.body_count = 0
        self.poster_roots = 0
        self.title_bands = 0
        self.title_bands_inside_root = 0
        self.poster_ids: list[str] = []
        self.modules: Counter[str] = Counter()
        self.module_order: list[str] = []
        self.module_text: dict[str, list[str]] = {}
        self.module_visible_media: set[str] = set()
        self.module_roles: dict[str, tuple[str, ...]] = {}
        self.module_priorities: dict[str, str] = {}
        self.semantic_roles: set[str] = set()
        self.styles: list[str] = []
        self.visible_text: list[str] = []
        self.source_labels: list[str] = []
        self.visible_source_figure_sha256s: set[str] = set()
        self.issues: list[dict[str, str]] = []
        self.parse_error: str | None = None
        self._stack: list[tuple[str, str | None, bool]] = []
        self._open_poster_root_depth: int | None = None
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
            self.issues.append(
                _issue("duplicate_attribute", f"<{tag}> repeats an attribute.")
            )
        attr = {name.lower(): str(value or "") for name, value in attrs}
        if tag == "html":
            self.html_count += 1
        elif tag == "body":
            self.body_count += 1
        if tag in poster_assets.ACTIVE_CONTENT_TAGS:
            self.issues.append(
                _issue("active_html", f"Active element <{tag}> is forbidden.")
            )
        charset_meta = names == ["charset"] and (
            attr.get("charset", "").strip().lower() == "utf-8"
        )
        viewport_meta = (
            len(names) == 2
            and set(names) == {"name", "content"}
            and (attr.get("name", "").strip().lower() == "viewport")
        )
        if tag == "meta" and not (charset_meta or viewport_meta):
            self.issues.append(
                _issue(
                    "active_html",
                    "Only UTF-8 charset and inert viewport metadata are allowed.",
                )
            )
        if any(name.startswith("on") for name in attr):
            self.issues.append(
                _issue("event_handler", f"<{tag}> contains an event handler.")
            )
        if any(name in _INTERACTIVE_ATTRIBUTES for name in attr):
            self.issues.append(
                _issue(
                    "interactive_html", f"<{tag}> contains an interactive attribute."
                )
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
        module_id = attr.get("data-poster-module", "").strip()
        is_poster_root = (
            tag in {"main", "article", "div"}
            and poster_id
            and not module_id
            and self._stack
            and self._stack[-1][0] == "body"
        )
        if is_poster_root:
            self.poster_roots += 1
            self._open_poster_root_depth = None if self_closing else len(self._stack)
        if "data-poster-title-band" in attr:
            self.title_bands += 1
            if (
                self._open_poster_root_depth is not None
                and len(self._stack) > self._open_poster_root_depth
                and not is_poster_root
            ):
                self.title_bands_inside_root += 1
        raw_roles = attr.get("data-semantic-roles", "").strip()
        priority = attr.get("data-module-priority", "").strip()
        if module_id:
            self.modules[module_id] += 1
            self.module_order.append(module_id)
            self.module_text.setdefault(module_id, [])
            if POSTER_MODULE_RE.fullmatch(module_id) is None:
                self.issues.append(
                    _issue(
                        "module_id",
                        "data-poster-module must use a stable kebab-case identifier.",
                    )
                )
            if not poster_id:
                self.issues.append(
                    _issue(
                        "module_id",
                        f"Module {module_id!r} needs a stable data-poster-id.",
                    )
                )
            roles = tuple(raw_roles.split())
            audit_roles = tuple(
                dict.fromkeys(role for role in roles if role in SEMANTIC_ROLES)
            )
            if audit_roles:
                self.module_roles[module_id] = audit_roles
                self.semantic_roles.update(audit_roles)
            if priority in MODULE_PRIORITIES:
                self.module_priorities[module_id] = priority
            source_label = attr.get("data-source-label", "").strip()
            if not source_label:
                self.issues.append(
                    _issue(
                        "source_label",
                        f"Module {module_id!r} needs data-source-label.",
                    )
                )
            elif _SOURCE_LOCATOR_RE.search(source_label) is None:
                self.issues.append(
                    _issue(
                        "source_locator",
                        f"Module {module_id!r} needs a page, section, equation, "
                        "figure, table, bibliography, or grounded-brief locator.",
                    )
                )
            else:
                self.source_labels.append(source_label)
        inherited_module = next(
            (item[1] for item in reversed(self._stack) if item[1]), None
        )
        active_module = module_id or inherited_module
        if (
            active_module
            and tag in {"img", "math"}
            and not hidden
            and not any(item[2] for item in self._stack)
        ):
            self.module_visible_media.add(active_module)
        for name, value in attr.items():
            if (
                name in _RESOURCE_ATTRIBUTES
                and not poster_assets.safe_embedded_reference(value)
            ):
                self.issues.append(
                    _issue(
                        "external_resource",
                        f"<{tag}> {name} is not an inert embedded reference.",
                    )
                )
            if name in _SVG_PRESENTATION_ATTRIBUTES and not poster_assets.safe_svg_css(
                value
            ):
                self.issues.append(
                    _issue(
                        "external_css",
                        f"<{tag}> {name} contains an unsafe CSS reference.",
                    )
                )
        if tag == "img" and not attr.get("alt", "").strip():
            self.issues.append(
                _issue("image_alt", "Every poster image needs meaningful alt text.")
            )
        if tag == "img" and attr.get("data-source-figure-sha256"):
            claimed_figure_sha256 = attr["data-source-figure-sha256"].strip()
            actual_figure_sha256 = poster_assets.data_image_sha256(attr.get("src", ""))
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
            self._stack.append((tag, module_id or inherited_module, hidden))

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if not self._stack:
            self.issues.append(
                _issue("malformed_html", f"Unexpected closing tag </{tag}>.")
            )
            return
        closing_depth = len(self._stack) - 1
        open_tag, _, _ = self._stack.pop()
        if open_tag != lowered:
            self.issues.append(
                _issue("malformed_html", f"Expected </{open_tag}> before </{lowered}>.")
            )
        if closing_depth == self._open_poster_root_depth:
            self._open_poster_root_depth = None
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
        module_id = next((item[1] for item in reversed(self._stack) if item[1]), None)
        if module_id in self.module_text:
            self.module_text[module_id].append(text)

    def _check_css(self, css: str) -> None:
        issues = offline_css_issues(
            css,
            safe_reference=poster_assets.safe_embedded_reference,
        )
        if "unsafe_syntax" in issues:
            self.issues.append(
                _issue("unsafe_css", "CSS escapes and ambiguous syntax are forbidden.")
            )
        if "active_construct" in issues:
            self.issues.append(
                _issue(
                    "active_css",
                    "CSS imports, animation, and transitions are forbidden.",
                )
            )
        if "unsafe_reference" in issues:
            self.issues.append(
                _issue(
                    "external_css",
                    "CSS references must use supported inert embedded images.",
                )
            )
        if _MATH_LAYOUT_OVERRIDE_RE.search(css):
            self.issues.append(
                _issue(
                    "math_layout_override",
                    "Do not set MathML to ordinary block, flex, or grid display; use "
                    'the native <math display="block"> layout so fractions and '
                    "scripts remain typeset.",
                )
            )


def parse_poster_html(html_text: str) -> ParsedPosterHtml:
    """Parse static-validation facts once for reuse by related hard gates."""

    parser = ParsedPosterHtml()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - represented as a contract issue
        parser.parse_error = str(exc)
    return parser


def validate_poster_html(
    html_text: str,
    *,
    source_text: str = "",
    facts: ParsedPosterHtml | None = None,
) -> dict[str, Any]:
    """Validate one complete, offline, semantically selectable poster document."""

    issues: list[dict[str, str]] = []
    raw = html_text.encode("utf-8")
    if len(raw) > MAX_HTML_BYTES:
        issues.append(_issue("document_size", "Poster HTML exceeds the byte limit."))
    if not html_text.lower().startswith("<!doctype html>"):
        issues.append(
            _issue("doctype", "Poster must begin exactly with <!doctype html>.")
        )
    if "asset://" in html_text:
        issues.append(
            _issue("asset_token", "Poster contains an unresolved asset token.")
        )
    if _UNESCAPED_MATH_LESS_THAN_RE.search(html_text):
        issues.append(
            _issue(
                "malformed_mathml_operator",
                "Escape a MathML less-than operator as &lt;; a literal < inside a "
                "math token corrupts the equation structure.",
            )
        )
    parsed = facts or parse_poster_html(html_text)
    if parsed.parse_error:
        issues.append(
            _issue("malformed_html", f"HTML parser failed: {parsed.parse_error}")
        )
    if parsed._stack:
        unclosed = ", ".join(tag for tag, _, _ in parsed._stack[-5:])
        issues.append(
            _issue("malformed_html", f"Poster has unclosed element(s): {unclosed}.")
        )
    issues.extend(parsed.issues)
    if parsed.doctypes != ["doctype html"]:
        issues.append(_issue("doctype", "Poster needs exactly one HTML5 doctype."))
    if parsed.html_count != 1 or parsed.body_count != 1:
        issues.append(
            _issue(
                "document_root", "Poster needs exactly one html and one body element."
            )
        )
    if parsed.poster_roots != 1:
        issues.append(
            _issue(
                "poster_root",
                "Poster needs exactly one body-level main, article, or div with "
                f"data-poster-id; found {parsed.poster_roots}.",
            )
        )
    if parsed.title_bands != 1 or parsed.title_bands_inside_root != 1:
        issues.append(
            _issue(
                "title_band_structure",
                "Poster needs exactly one data-poster-title-band nested inside the "
                "body-level poster root.",
            )
        )
    duplicates = sorted(
        name for name, count in Counter(parsed.poster_ids).items() if count > 1
    )
    if duplicates:
        issues.append(
            _issue(
                "duplicate_poster_id",
                "Duplicate data-poster-id: " + ", ".join(duplicates),
            )
        )
    duplicate_modules = sorted(
        name for name, count in parsed.modules.items() if count > 1
    )
    if duplicate_modules:
        issues.append(
            _issue(
                "duplicate_module",
                "Duplicate data-poster-module: " + ", ".join(duplicate_modules),
            )
        )
    for module_id in sorted(parsed.modules):
        if (
            not " ".join(parsed.module_text.get(module_id, ())).strip()
            and module_id not in parsed.module_visible_media
        ):
            issues.append(
                _issue(
                    "empty_module", f"Poster module {module_id!r} has no visible text."
                )
            )
        if "provenance" not in parsed.module_roles.get(module_id, ()) and any(
            re.match(r"^\s*source\s*[:：]", item, re.IGNORECASE)
            for item in parsed.module_text.get(module_id, ())
        ):
            issues.append(
                _issue(
                    "visible_source_locator",
                    f"Module {module_id!r} prints a paper locator as poster copy. Keep "
                    "source labels in metadata and explain what the evidence shows; "
                    "render locators only in the provenance/references module.",
                )
            )
    css = "\n".join(parsed.styles)
    page_match = _PAGE_SIZE_RE.search(css)
    page: dict[str, float] | None = None
    if page_match is None:
        issues.append(
            _issue(
                "physical_page", "Inline CSS must declare @page size in millimetres."
            )
        )
    else:
        width = float(page_match.group(1))
        height = float(page_match.group(2))
        if not (200 <= width <= 2000 and 200 <= height <= 2000):
            issues.append(
                _issue(
                    "physical_page", "Poster page dimensions are outside safe bounds."
                )
            )
        else:
            page = {"width_mm": width, "height_mm": height}
    visible = " ".join(parsed.visible_text)
    if _PLACEHOLDER_RE.search(visible):
        issues.append(_issue("placeholder_copy", "Poster contains placeholder copy."))
    unsupported_rights = _unsupported_rights_claims(visible, source_text)
    if unsupported_rights:
        issues.append(
            _issue(
                "ungrounded_rights_claim",
                "Rights or permission language is not supported by the source: "
                + ", ".join(unsupported_rights),
            )
        )
    if source_text:
        issues.extend(_source_locator_issues(parsed.source_labels, source_text))
    return {
        "status": "error" if issues else "ok",
        "issues": issues,
        "page": page,
        "poster_ids": sorted(parsed.poster_ids),
        "modules": {
            name: {
                "count": parsed.modules[name],
                "semantic_roles": list(parsed.module_roles.get(name, ())),
                "priority": parsed.module_priorities.get(name),
            }
            for name in sorted(parsed.modules)
        },
        "module_order": parsed.module_order,
        "semantic_roles": sorted(parsed.semantic_roles),
    }


def validate_grounded_fragments(
    fragments: Sequence[Mapping[str, Any]],
    *,
    source_text: str = "",
) -> dict[str, Any]:
    """Validate fragment locators and reject unsupported legal assertions."""

    issues: list[dict[str, Any]] = []
    source_labels: list[str] = []
    fragment_text: list[str] = []
    for index, fragment in enumerate(fragments):
        source_label = str(fragment.get("source_label") or "").strip()
        if not source_label or _SOURCE_LOCATOR_RE.search(source_label) is None:
            shown_label = source_label or "<missing>"
            issues.append(
                _issue(
                    "source_locator",
                    f"Grounded fragment {index + 1} has source_label "
                    f"{shown_label!r}; use a page, section, equation, figure, "
                    "table, bibliography, or grounded-brief locator.",
                )
            )
        else:
            source_labels.append(source_label)
        fragment_text.append(str(fragment.get("text") or ""))
        detail_points = fragment.get("detail_points")
        if isinstance(detail_points, Sequence) and not isinstance(
            detail_points, (str, bytes)
        ):
            fragment_text.extend(str(item) for item in detail_points)
    unsupported_rights = _unsupported_rights_claims(
        " ".join(fragment_text),
        source_text,
    )
    if unsupported_rights:
        issues.append(
            _issue(
                "ungrounded_rights_claim",
                "Rights or permission language is not supported by the source: "
                + ", ".join(unsupported_rights),
            )
        )
    if source_text:
        issues.extend(_source_locator_issues(source_labels, source_text))
    return {"status": "error" if issues else "ok", "issues": issues}


def _unsupported_rights_claims(
    candidate_text: str,
    source_text: str,
) -> list[str]:
    """Return legal assertions that are not present verbatim in the source."""

    candidate = " ".join(candidate_text.split())
    source = " ".join(source_text.split()).casefold()
    unsupported: list[str] = []
    for pattern in _RIGHTS_CLAIM_PATTERNS:
        for match in pattern.finditer(candidate):
            phrase = " ".join(match.group(0).split())
            if phrase.casefold() not in source and phrase not in unsupported:
                unsupported.append(phrase)
    return unsupported


def remove_unsupported_rights_claims(
    candidate_text: str,
    source_text: str,
) -> str:
    """Remove generated legal boilerplate while preserving source-supported wording."""

    source = " ".join(source_text.split()).casefold()
    has_unaffected_sentence = any(
        segment.strip() and not _unsupported_rights_claims(segment, source_text)
        for segment in re.split(r"(?<=[.!?])\s+", candidate_text)
    )
    removed = False
    cleaned = candidate_text
    for pattern in _RIGHTS_CLAIM_PATTERNS:

        def replace(match: re.Match[str]) -> str:
            nonlocal removed
            phrase = " ".join(match.group(0).split())
            if phrase.casefold() in source:
                return match.group(0)
            removed = True
            return ""

        cleaned = pattern.sub(replace, cleaned)
    if not removed:
        return candidate_text
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[,;:]\s*([.!?])", r"\1", cleaned)
    cleaned = re.sub(r"([.!?])(?:\s*[.!?])+", r"\1", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" ,;:-")
    if len(re.findall(r"\b[\w'-]+\b", cleaned)) <= 4 and not has_unaffected_sentence:
        return ""
    return cleaned


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
            if (
                re.search(
                    rf"\b(?:figure|fig\.?)\s*{re.escape(value)}\b",
                    source_text,
                    re.IGNORECASE,
                )
                is None
            ):
                invalid.add(f"Figure {value}")
        for value in _LABEL_TABLE_RE.findall(label):
            if (
                re.search(
                    rf"\btable\s*{re.escape(value)}\b", source_text, re.IGNORECASE
                )
                is None
            ):
                invalid.add(f"Table {value}")
        for value in _LABEL_EQUATION_RE.findall(label):
            escaped = re.escape(value)
            explicit_equation = re.search(
                rf"\b(?:equation|eq\.?)\s*\(?{escaped}\)?(?![\d.])",
                source_text,
                re.IGNORECASE,
            )
            numbered_display = re.search(
                rf"(?<![\d.])\(\s*{escaped}\s*\)(?![\d.])",
                source_text,
            )
            if explicit_equation is None and numbered_display is None:
                invalid.add(f"Equation {value}")
        for value in _LABEL_SECTION_RE.findall(label):
            escaped = re.escape(value)
            heading = re.compile(
                rf"(?m)(?:^|[ \t]{{2,}}){escaped}(?:\.\s+|[ \t]+)",
                re.IGNORECASE,
            )
            explicit = re.compile(
                rf"(?:\b(?:section|sec\.?)\s*|§\s*){escaped}(?![\d.])",
                re.IGNORECASE,
            )
            if (
                pages
                and heading.search(source_text) is None
                and explicit.search(source_text) is None
            ):
                invalid.add(f"§{value}")
        if "abstract" in label.lower() and pages and "abstract" not in source_lower:
            invalid.add("Abstract")
        if "references" in label.lower() and pages and "references" not in source_lower:
            invalid.add("References")
        if (
            "bibliography" in label.lower()
            and pages
            and "bibliography" not in source_lower
            and "references" not in source_lower
        ):
            invalid.add("Bibliography")
    if not invalid:
        return []
    return [
        _issue(
            "source_locator_mismatch",
            "Source locator(s) are absent from the supplied paper: "
            + ", ".join(sorted(invalid)),
        )
    ]


def prune_invalid_source_locator_parts(source_label: str, source_text: str) -> str:
    """Drop invalid semicolon-delimited locators when another locator remains valid."""

    original = str(source_label).strip()
    parts = [part.strip() for part in original.split(";") if part.strip()]
    if len(parts) < 2:
        return original
    valid = [
        part
        for part in parts
        if _SOURCE_LOCATOR_RE.search(part) is not None
        and not _source_locator_issues([part], source_text)
    ]
    return "; ".join(valid) if valid else original


def source_figure_usage_issues(
    html_text: str,
    expected_sha256s: set[str] | tuple[str, ...] | list[str],
    *,
    facts: ParsedPosterHtml | None = None,
) -> list[dict[str, str]]:
    """Require one statically visible, byte-matched figure from a prepared PDF."""

    expected = {str(value) for value in expected_sha256s}
    if not expected:
        return []
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in expected):
        return [
            _issue(
                "source_figure_identity", "Expected source-figure hashes are invalid."
            )
        ]
    parsed = facts or parse_poster_html(html_text)
    if parsed.parse_error:
        return [_issue("malformed_html", f"HTML parser failed: {parsed.parse_error}")]
    if expected.isdisjoint(parsed.visible_source_figure_sha256s):
        return [
            _issue(
                "missing_source_figure",
                "Use at least one visible figure extracted from the supplied PDF.",
            )
        ]
    return []


class _PosterIdentityParser(HTMLParser):
    """Index stable ids with inherited module identity."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str, str, str]] = []
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
        inherited_module = self.stack[-1][1] if self.stack else ""
        inherited_roles = self.stack[-1][2] if self.stack else ""
        inherited_priority = self.stack[-1][3] if self.stack else ""
        module_id = attributes.get("data-poster-module", "").strip() or inherited_module
        semantic_roles = (
            attributes.get("data-semantic-roles", "").strip() or inherited_roles
        )
        priority = (
            attributes.get("data-module-priority", "").strip() or inherited_priority
        )
        poster_id = attributes.get("data-poster-id", "").strip()
        if poster_id:
            if poster_id in self.elements:
                raise ValueError(f"poster_id is not unique in source HTML: {poster_id}")
            identity = {
                "poster_module": module_id,
                "semantic_roles": semantic_roles,
                "module_priority": priority,
            }
            self.elements[poster_id] = identity
        if not self_closing:
            self.stack.append((tag, module_id, semantic_roles, priority))

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


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code, "message": message, "severity": "error"}
    issue.update(details)
    return issue
