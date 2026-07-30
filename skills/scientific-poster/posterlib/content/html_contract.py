"""Static HTML validation and embedded-image binding for scientific posters."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from html import escape
from html.parser import HTMLParser
from typing import Any

import poster_assets
import poster_core

from . import planning, scientific_snapshot

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
_DATA_LATEX_ATTR_PATTERN = re.compile(
    r"\sdata-latex\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
    re.IGNORECASE,
)
_ASSET_TOKEN_RE = re.compile(r"asset://[1-9]\d*")
_STYLE_ELEMENT_PATTERN = re.compile(
    r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL
)
_PAGE_SIZE_PRESENT_PATTERN = re.compile(
    r"@page\b[^{}]*\{[^{}]*\bsize\s*:", re.IGNORECASE | re.DOTALL
)
_CSS_RULE_PATTERN = re.compile(r"([^{}]+)\{([^{}]*)\}")
_MATH_SELECTOR_PATTERN = re.compile(
    r"(?:\bmath\b|\[\s*data-(?:content-role|latex)\b)", re.IGNORECASE
)
_MATH_DISPLAY_DECLARATION_PATTERN = re.compile(
    r"(?P<prefix>^|;)\s*display\s*:\s*"
    r"(?:block|flex|grid|inline-block)\s*(?=;|$)",
    re.IGNORECASE,
)
_MATH_ELEMENT_PATTERN = re.compile(r"<math\b", re.IGNORECASE)
_MATH_TYPE_START = "/* scientific-poster-math-type:start */"
_MATH_TYPE_END = "/* scientific-poster-math-type:end */"
_MATH_TYPE_PATTERN = re.compile(
    rf"\s*{re.escape(_MATH_TYPE_START)}.*?{re.escape(_MATH_TYPE_END)}\s*",
    re.DOTALL,
)
VERIFIED_TITLE_TOKEN = "__OMNI_VERIFIED_POSTER_TITLE__"
VERIFIED_AUTHORS_TOKEN = "__OMNI_VERIFIED_POSTER_AUTHORS__"
_VOID_TAGS = frozenset(
    {
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
)


class _EquationLatexBinder(HTMLParser):
    """Record exact start-tag edits for machine-owned equation provenance."""

    def __init__(self, html_text: str, expected: Mapping[str, list[str]]) -> None:
        super().__init__(convert_charrefs=False)
        self._html_text = html_text
        self._expected = expected
        self._line_offsets = [0]
        self._line_offsets.extend(match.end() for match in re.finditer("\n", html_text))
        self._stack: list[tuple[str, str, str, bool]] = []
        self._counts: dict[str, int] = {}
        self._replacements: list[tuple[int, int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs, self_closing=tag.lower() in _VOID_TAGS)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._start(tag.lower(), attrs, self_closing=True)

    def _start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        attributes = {name.lower(): str(value or "") for name, value in attrs}
        inherited_module = self._stack[-1][1] if self._stack else ""
        inherited_role = self._stack[-1][2] if self._stack else ""
        inherited_hidden = self._stack[-1][3] if self._stack else False
        module_id = attributes.get("data-poster-module", "").strip() or inherited_module
        content_role = attributes.get("data-content-role", "").strip() or inherited_role
        inline_style = attributes.get("style", "").replace(" ", "").lower()
        hidden = (
            inherited_hidden
            or "display:none" in inline_style
            or ("visibility:hidden" in inline_style)
        )
        if tag != "math" and (
            attributes.get("data-content-role", "").strip() == "equation"
            or "data-latex" in attributes
        ):
            raw = self.get_starttag_text() or ""
            if raw:
                line, offset = self.getpos()
                start = self._line_offsets[line - 1] + offset
                replacement = _set_start_tag_attributes(
                    raw,
                    {},
                    remove=("data-content-role", "data-latex"),
                )
                self._replacements.append((start, start + len(raw), replacement))
        if tag == "math" and not hidden and module_id in self._expected:
            index = self._counts.get(module_id, 0)
            self._counts[module_id] = index + 1
            expected = self._expected[module_id]
            if index < len(expected):
                raw = self.get_starttag_text() or ""
                if raw:
                    line, offset = self.getpos()
                    start = self._line_offsets[line - 1] + offset
                    replacement = _bind_data_latex(raw, expected[index])
                    replacement = _set_start_tag_attributes(
                        replacement,
                        {"data-content-role": "equation"},
                    )
                    self._replacements.append((start, start + len(raw), replacement))
        if not self_closing:
            self._stack.append((tag, module_id, content_role, hidden))

    def handle_endtag(self, tag: str) -> None:
        del tag
        if self._stack:
            self._stack.pop()

    def apply(self) -> str:
        result = self._html_text
        for start, end, replacement in reversed(self._replacements):
            result = result[:start] + replacement + result[end:]
        return result


def bind_equation_latex(
    html_text: str,
    content_budget: Mapping[str, Any] | None,
) -> str:
    """Bind exact planned LaTeX to visible MathML without changing its rendering."""

    if not isinstance(content_budget, Mapping):
        return html_text
    raw_modules = content_budget.get("content_modules")
    if not isinstance(raw_modules, list):
        return html_text
    expected: dict[str, list[str]] = {}
    for module in raw_modules:
        if not isinstance(module, Mapping):
            continue
        module_id = str(module.get("id") or "").strip()
        raw_equations = module.get("equations")
        if not module_id or not isinstance(raw_equations, list) or not raw_equations:
            continue
        latex = [
            str(equation.get("latex") or "")
            for equation in raw_equations
            if isinstance(equation, Mapping)
        ]
        if latex and all(latex):
            expected[module_id] = latex
    if not expected:
        return html_text
    binder = _EquationLatexBinder(html_text, expected)
    binder.feed(html_text)
    binder.close()
    return binder.apply()


class _AuthoredContractBinder(HTMLParser):
    """Recover exact machine metadata without rewriting authored visible content."""

    def __init__(
        self,
        html_text: str,
        *,
        modules: Mapping[str, Mapping[str, str]],
        assets: Mapping[str, Mapping[str, str]],
        paper_identity: Mapping[str, Any] | None,
    ) -> None:
        super().__init__(convert_charrefs=False)
        self._html_text = html_text
        self._modules = modules
        self._assets = assets
        self._assets_by_token = {
            str(asset["token"]): asset
            for asset in assets.values()
            if str(asset.get("token") or "")
        }
        self._paper_identity = paper_identity or {}
        self._line_offsets = [0]
        self._line_offsets.extend(match.end() for match in re.finditer("\n", html_text))
        self._stack: list[dict[str, Any]] = []
        self._frames: list[dict[str, Any]] = []
        self._module_frames: dict[str, list[dict[str, Any]]] = {}
        self._has_title_band = False
        self._title_band_frames: list[dict[str, Any]] = []
        self._has_poster_root = False
        self._start_edits: dict[int, tuple[int, str]] = {}
        self._insertions: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs, self_closing=tag.lower() in _VOID_TAGS)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._start(tag.lower(), attrs, self_closing=True)

    def _start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        raw = self.get_starttag_text() or ""
        if not raw:
            return
        line, offset = self.getpos()
        start = self._line_offsets[line - 1] + offset
        attributes = {name.lower(): str(value or "") for name, value in attrs}
        is_title_band = "data-poster-title-band" in attributes
        self._has_title_band |= is_title_band
        body_level = bool(self._stack and self._stack[-1]["tag"] == "body")
        self._has_poster_root |= bool(
            body_level
            and tag in {"main", "article", "div"}
            and attributes.get("data-poster-id", "").strip()
        )

        frame: dict[str, Any] = {
            "tag": tag,
            "start": start,
            "end": start + len(raw),
            "raw": raw,
            "depth": len(self._stack),
            "text": [],
            "module_markers": 0,
            "venue_logo_markers": 0,
            "body_level": body_level,
            "asset": None,
            "asset_bound": False,
        }
        if is_title_band:
            self._title_band_frames.append(frame)
        module_id = attributes.get("data-poster-module", "").strip()
        if module_id not in self._modules:
            module_id = attributes.get("data-poster-id", "").strip()
        if module_id not in self._modules:
            module_id = attributes.get("id", "").strip()
        if module_id in self._modules:
            self._module_frames.setdefault(module_id, []).append(frame)
            for ancestor in self._stack:
                ancestor["module_markers"] += 1

        venue = self._paper_identity.get("venue_identity")
        expected_logo_token = (
            str(venue.get("logo_asset_token") or "").strip()
            if isinstance(venue, Mapping)
            else ""
        )
        is_venue_logo = tag == "img" and (
            "data-poster-venue-logo" in attributes
            or bool(
                expected_logo_token and attributes.get("src") == expected_logo_token
            )
        )
        if is_venue_logo:
            if expected_logo_token and attributes.get("src") == expected_logo_token:
                self._queue_start_edit(frame, {"data-poster-venue-logo": None})
            for ancestor in self._stack:
                ancestor["venue_logo_markers"] += 1

        if tag == "a" and "href" in attributes:
            self._queue_start_edit(frame, {}, remove=("href",))

        digest = attributes.get("data-source-figure-sha256", "").strip().lower()
        token = (
            attributes.get("src", "").strip()
            if tag == "img"
            else attributes.get("data-token", "").strip()
        )
        direct_asset = self._assets.get(digest) or self._assets_by_token.get(token)
        if tag == "img" and direct_asset is not None:
            self._queue_start_edit(frame, _asset_image_attributes(direct_asset))
        elif direct_asset is not None and not self_closing:
            frame["asset"] = direct_asset
            self._queue_start_edit(
                frame,
                {"style": _positioned_wrapper_style(attributes.get("style", ""))},
                remove=("data-token", "data-source-figure-sha256"),
            )

        if tag == "img" and direct_asset is None:
            wrapper = next(
                (
                    ancestor
                    for ancestor in reversed(self._stack)
                    if ancestor.get("asset") is not None
                    and not ancestor.get("asset_bound")
                ),
                None,
            )
            if wrapper is not None:
                wrapper["asset_bound"] = True
                self._queue_start_edit(
                    frame,
                    _asset_image_attributes(wrapper["asset"], overlay=True),
                )

        self._frames.append(frame)
        if not self_closing:
            self._stack.append(frame)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        match_index = next(
            (
                index
                for index in range(len(self._stack) - 1, -1, -1)
                if self._stack[index]["tag"] == tag
            ),
            None,
        )
        if match_index is None:
            return
        frame = self._stack[match_index]
        if frame.get("asset") is not None and not frame.get("asset_bound"):
            line, offset = self.getpos()
            closing_start = self._line_offsets[line - 1] + offset
            self._insertions.append(
                (closing_start, _asset_image_markup(frame["asset"]))
            )
            frame["asset_bound"] = True
        del self._stack[match_index:]

    def handle_data(self, data: str) -> None:
        if not data:
            return
        for frame in self._stack:
            frame["text"].append(data)

    def _queue_start_edit(
        self,
        frame: Mapping[str, Any],
        updates: Mapping[str, str | None],
        *,
        remove: tuple[str, ...] = (),
    ) -> None:
        start = int(frame["start"])
        end = int(frame["end"])
        current = self._start_edits.get(start, (end, str(frame["raw"])))[1]
        replacement = _set_start_tag_attributes(current, updates, remove=remove)
        self._start_edits[start] = (end, replacement)

    def apply(self) -> str:
        for module_id, frames in self._module_frames.items():
            if len(frames) != 1:
                continue
            binding = self._modules[module_id]
            remove = ["data-frame-level"]
            if not binding.get("data-focal-role"):
                remove.append("data-focal-role")
            self._queue_start_edit(
                frames[0],
                binding,
                remove=tuple(remove),
            )

        self._bind_identity_markers()
        if not self._has_poster_root:
            expected_title = _canonical_identity_text(
                str(self._paper_identity.get("title") or "")
            )
            candidates = [
                frame
                for frame in self._frames
                if frame["body_level"]
                and frame["tag"] in {"main", "article", "div"}
                and frame["module_markers"] == len(self._modules)
                and (
                    not expected_title
                    or expected_title
                    in _canonical_identity_text(" ".join(frame["text"]))
                )
            ]
            if len(candidates) == 1:
                self._queue_start_edit(candidates[0], {"data-poster-id": "poster"})

        title_band: dict[str, Any] | None = None
        if not self._has_title_band:
            candidates = self._title_band_candidates()
            if candidates:
                title_band = max(candidates, key=self._title_band_score)
                self._queue_start_edit(title_band, {"data-poster-title-band": None})
        elif len(self._title_band_frames) == 1:
            title_band = self._title_band_frames[0]
        if title_band is not None:
            self._bind_missing_venue_logo(title_band)

        edits = [
            (start, end, replacement)
            for start, (end, replacement) in self._start_edits.items()
        ]
        edits.extend((start, start, markup) for start, markup in self._insertions)
        result = self._html_text
        for start, end, replacement in sorted(edits, reverse=True):
            result = result[:start] + replacement + result[end:]
        return result

    def _bind_identity_markers(self) -> None:
        for frame in self._frames:
            raw = str(frame["raw"])
            if re.search(r"\bdata-poster-title(?![\w.:-])", raw, re.IGNORECASE):
                self._queue_start_edit(frame, {}, remove=("data-poster-title",))
            if re.search(r"\bdata-poster-authors(?![\w.:-])", raw, re.IGNORECASE):
                self._queue_start_edit(frame, {}, remove=("data-poster-authors",))
            if re.search(r"\bdata-poster-venue(?![\w.:-])", raw, re.IGNORECASE):
                self._queue_start_edit(frame, {}, remove=("data-poster-venue",))

        expected_title = _canonical_identity_text(
            str(self._paper_identity.get("title") or "")
        )
        if expected_title:
            candidates = [
                frame
                for frame in self._frames
                if frame["tag"] == "h1"
                and _canonical_identity_text(" ".join(frame["text"])) == expected_title
            ]
            if len(candidates) == 1:
                self._queue_start_edit(candidates[0], {"data-poster-title": "verified"})

        expected_authors = _canonical_author_text(
            str(self._paper_identity.get("authors") or "")
        )
        if expected_authors:
            candidates = [
                frame
                for frame in self._frames
                if frame["tag"] in {"p", "div", "span"}
                and _canonical_author_text(" ".join(frame["text"])) == expected_authors
            ]
            if len(candidates) == 1:
                self._queue_start_edit(
                    candidates[0], {"data-poster-authors": "verified"}
                )

        venue = self._paper_identity.get("venue_identity")
        if isinstance(venue, Mapping):
            expected_venue = _canonical_identity_text(
                " ".join(str(venue.get(key) or "") for key in ("label", "distinction"))
            )
            if expected_venue:
                candidates = [
                    (frame, _canonical_identity_text(" ".join(frame["text"])))
                    for frame in self._frames
                    if frame["tag"] in {"p", "div", "span"}
                    and expected_venue
                    in _canonical_identity_text(" ".join(frame["text"]))
                ]
                if candidates:
                    minimum_extra = min(
                        len(text) - len(expected_venue) for _frame, text in candidates
                    )
                    closest = [
                        frame
                        for frame, text in candidates
                        if len(text) - len(expected_venue) == minimum_extra
                    ]
                else:
                    closest = []
                if len(closest) == 1:
                    self._queue_start_edit(
                        closest[0], {"data-poster-venue": "verified"}
                    )

    def _title_band_candidates(self) -> list[dict[str, Any]]:
        """Return containers that visibly contain the exact planned identity."""

        expected_title = _canonical_identity_text(
            str(self._paper_identity.get("title") or "")
        )
        expected_authors = _canonical_author_text(
            str(self._paper_identity.get("authors") or "")
        )
        if not expected_title or not expected_authors:
            return []
        return [
            frame
            for frame in self._frames
            if frame["tag"] in {"header", "div", "section"}
            and expected_title in _canonical_identity_text(" ".join(frame["text"]))
            and expected_authors in _canonical_author_text(" ".join(frame["text"]))
        ]

    def _title_band_score(self, frame: Mapping[str, Any]) -> tuple[int, int, int]:
        """Prefer the smallest identity container that owns venue branding."""

        venue = self._paper_identity.get("venue_identity")
        expected_venue = (
            _canonical_identity_text(
                " ".join(str(venue.get(key) or "") for key in ("label", "distinction"))
            )
            if isinstance(venue, Mapping)
            else ""
        )
        visible = _canonical_identity_text(" ".join(frame["text"]))
        return (
            int(bool(frame["venue_logo_markers"])),
            int(bool(expected_venue and expected_venue in visible)),
            int(frame["depth"]),
        )

    def _bind_missing_venue_logo(self, title_band: Mapping[str, Any]) -> None:
        """Insert the exact local venue mark when authoring omitted only that asset."""

        venue = self._paper_identity.get("venue_identity")
        if not isinstance(venue, Mapping) or title_band["venue_logo_markers"]:
            return
        token = str(venue.get("logo_asset_token") or "").strip()
        if not token:
            return
        label = " ".join(
            str(venue.get(key) or "").strip()
            for key in ("label", "distinction")
            if str(venue.get(key) or "").strip()
        )
        markup = (
            f'<img src="{escape(token, quote=True)}" '
            "data-poster-venue-logo "
            f'alt="{escape(label or "Conference logo", quote=True)}" '
            'style="width:auto;max-width:68mm;max-height:30mm;object-fit:contain">'
        )
        self._insertions.append((int(title_band["end"]), markup))


def bind_authored_contract(
    html_text: str,
    *,
    content_budget: Mapping[str, Any] | None,
    page_plan: Mapping[str, Any] | None,
    paper_identity: Mapping[str, Any] | None,
    assets: list[dict[str, Any]],
) -> str:
    """Bind planned metadata and exact prepared figures, then bind equation provenance."""

    html_text = _bind_verified_identity_tokens(html_text, paper_identity)
    modules = _authored_module_bindings(content_budget)
    prepared_assets = _prepared_figure_bindings(assets)
    binder = _AuthoredContractBinder(
        html_text,
        modules=modules,
        assets=prepared_assets,
        paper_identity=paper_identity,
    )
    binder.feed(html_text)
    binder.close()
    bound = bind_equation_latex(binder.apply(), content_budget)
    bound = ensure_native_math_layout(bound)
    bound = ensure_physical_page_rule(bound, page_plan)
    return ensure_math_type_floor(bound, page_plan)


def _bind_verified_identity_tokens(
    html_text: str,
    paper_identity: Mapping[str, Any] | None,
) -> str:
    """Inject verified identity only where authoring preserved one unique slot."""

    if not paper_identity:
        return html_text
    replacements = {
        VERIFIED_TITLE_TOKEN: str(paper_identity.get("title") or "").strip(),
        VERIFIED_AUTHORS_TOKEN: str(paper_identity.get("authors") or "").strip(),
    }
    bound = html_text
    for token, value in replacements.items():
        if value and bound.count(token) == 1:
            bound = bound.replace(token, escape(value))
    return bound


def ensure_native_math_layout(html_text: str) -> str:
    """Remove CSS display overrides that disable Chromium's MathML layout."""

    def rewrite_style(match: re.Match[str]) -> str:
        style = match.group()
        opening = style.find(">") + 1
        closing = style.lower().rfind("</style>")
        if opening <= 0 or closing < opening:
            return style
        css = style[opening:closing]

        def rewrite_rule(rule: re.Match[str]) -> str:
            selector, declarations = rule.groups()
            if not _MATH_SELECTOR_PATTERN.search(selector):
                return rule.group()
            cleaned = _MATH_DISPLAY_DECLARATION_PATTERN.sub(
                lambda item: item.group("prefix"),
                declarations,
            )
            return f"{selector}{{{cleaned}}}"

        return (
            style[:opening] + _CSS_RULE_PATTERN.sub(rewrite_rule, css) + style[closing:]
        )

    return _STYLE_ELEMENT_PATTERN.sub(rewrite_style, html_text)


def ensure_physical_page_rule(
    html_text: str,
    page_plan: Mapping[str, Any] | None,
) -> str:
    """Add the machine-owned print page rule when an author omitted it."""

    if not isinstance(page_plan, Mapping):
        return html_text
    css = "\n".join(_STYLE_ELEMENT_PATTERN.findall(html_text))
    if _PAGE_SIZE_PRESENT_PATTERN.search(css):
        return html_text
    try:
        width = float(page_plan["width_mm"])
        height = float(page_plan["height_mm"])
    except (KeyError, TypeError, ValueError):
        return html_text
    if not (math.isfinite(width) and math.isfinite(height)):
        return html_text
    matches = list(_STYLE_ELEMENT_PATTERN.finditer(html_text))
    if len(matches) != 1:
        return html_text
    style = matches[0].group()
    insertion = style.find(">") + 1
    if insertion <= 0:
        return html_text
    rule = f"\n@page {{ size: {width:g}mm {height:g}mm; margin: 0; }}\n"
    replacement = style[:insertion] + rule + style[insertion:]
    return html_text[: matches[0].start()] + replacement + html_text[matches[0].end() :]


def ensure_math_type_floor(
    html_text: str,
    page_plan: Mapping[str, Any] | None,
) -> str:
    """Add one late, page-scaled MathML floor while preserving stronger author CSS."""

    if not _MATH_ELEMENT_PATTERN.search(html_text) or _MATH_TYPE_START in html_text:
        return html_text
    width = page_plan.get("width_mm") if isinstance(page_plan, Mapping) else None
    if isinstance(width, bool) or not isinstance(width, (int, float)):
        return html_text
    try:
        minimum_mm = planning.typography_metrics(float(width))["body_min_mm"]
    except (TypeError, ValueError, planning.PlanningError):
        return html_text
    matches = list(_STYLE_ELEMENT_PATTERN.finditer(html_text))
    if len(matches) != 1:
        return html_text
    style = matches[0].group()
    insertion = style.rfind("</style>")
    if insertion < 0:
        return html_text
    safety_rule = (
        f"\n{_MATH_TYPE_START}\n"
        f"math[data-latex] {{ font-size: max({minimum_mm:g}mm, 1em); }}\n"
        f"{_MATH_TYPE_END}\n"
    )
    replacement = style[:insertion] + safety_rule + style[insertion:]
    return html_text[: matches[0].start()] + replacement + html_text[matches[0].end() :]


def _authored_module_bindings(
    content_budget: Mapping[str, Any] | None,
) -> dict[str, dict[str, str]]:
    if not isinstance(content_budget, Mapping):
        return {}
    raw_modules = content_budget.get("content_modules")
    if not isinstance(raw_modules, list):
        return {}
    focal_role = str(content_budget.get("focal_role") or "").strip()
    bindings: dict[str, dict[str, str]] = {}
    for module in raw_modules:
        if not isinstance(module, Mapping):
            continue
        module_id = str(module.get("id") or "").strip()
        if not module_id:
            continue
        values = {
            "data-poster-module": module_id,
            "data-poster-id": module_id,
            "data-section-id": str(module.get("section_id") or "").strip(),
            "data-semantic-roles": " ".join(
                str(value).strip()
                for value in module.get("semantic_roles") or []
                if str(value).strip()
            ),
            "data-module-priority": str(module.get("priority") or "").strip(),
            "data-source-label": str(module.get("source_label") or "").strip(),
        }
        if values["data-module-priority"] == "focal" and focal_role:
            values["data-focal-role"] = focal_role
        bindings[module_id] = values
    return bindings


def _prepared_figure_bindings(
    assets: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    bindings: dict[str, dict[str, str]] = {}
    for asset in assets:
        digest = str(asset.get("content_sha256") or "").strip().lower()
        token = str(asset.get("token") or "").strip()
        description = str(asset.get("description") or "").strip()
        if (
            asset.get("source_kind") != "pdf_figure"
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or _ASSET_TOKEN_RE.fullmatch(token) is None
            or not description
        ):
            continue
        bindings[digest] = {
            "token": token,
            "digest": digest,
            "alt": _prepared_figure_alt(description),
        }
    return bindings


def _prepared_figure_alt(description: str) -> str:
    match = re.search(
        r"\bcaption:\s*(.*?)(?:\.\s*paper discussion:|$)",
        description,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return (match.group(1) if match is not None else description).strip()


def _asset_image_attributes(
    asset: Mapping[str, str],
    *,
    overlay: bool = False,
) -> dict[str, str]:
    attributes = {
        "src": asset["token"],
        "data-source-figure-sha256": asset["digest"],
        "alt": asset["alt"],
    }
    if overlay:
        attributes["style"] = (
            "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;"
            "z-index:2;background:#fff"
        )
    return attributes


def _asset_image_markup(asset: Mapping[str, str]) -> str:
    attributes = _asset_image_attributes(asset, overlay=True)
    return (
        "<img "
        + " ".join(
            f'{name}="{escape(value, quote=True)}"'
            for name, value in attributes.items()
        )
        + ">"
    )


def _positioned_wrapper_style(style: str) -> str:
    if re.search(r"(?:^|;)\s*position\s*:", style, flags=re.IGNORECASE):
        return style
    prefix = style.strip().rstrip(";")
    return (prefix + ";" if prefix else "") + "position:relative"


def _set_start_tag_attributes(
    start_tag: str,
    updates: Mapping[str, str | None],
    *,
    remove: tuple[str, ...] = (),
) -> str:
    cleaned = start_tag
    for name in (*remove, *updates.keys()):
        cleaned = re.sub(
            rf"\s+{re.escape(name)}(?![\w.:-])"
            r"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
    close = cleaned.rfind(">")
    if close < 0:
        return start_tag
    prefix = cleaned[:close].rstrip()
    suffix = cleaned[close:]
    self_closing = prefix.endswith("/")
    if self_closing:
        prefix = prefix[:-1].rstrip()
    for name, value in updates.items():
        prefix += f" {name}"
        if value is not None:
            prefix += f'="{escape(value, quote=True)}"'
    if self_closing:
        prefix += " /"
    return prefix + suffix


def _bind_data_latex(start_tag: str, latex: str) -> str:
    """Replace duplicate or missing data-latex attributes with one escaped value."""

    cleaned = _DATA_LATEX_ATTR_PATTERN.sub("", start_tag)
    close = cleaned.rfind(">")
    if close < 0:
        return start_tag
    prefix = cleaned[:close].rstrip()
    suffix = cleaned[close:]
    attribute = f' data-latex="{escape(latex, quote=True)}"'
    if prefix.endswith("/"):
        prefix = prefix[:-1].rstrip() + attribute + " /"
    else:
        prefix += attribute
    return prefix + suffix


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
    source_text: str,
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
    report = poster_core.validate_poster_html(
        resolved,
        source_text=source_text,
        facts=poster_facts,
    )
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
    source_figure_issues = poster_core.source_figure_usage_issues(
        resolved,
        prepared_source_figures,
        facts=poster_facts,
    )
    if source_figure_issues:
        report = _append_issues(report, source_figure_issues)
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
