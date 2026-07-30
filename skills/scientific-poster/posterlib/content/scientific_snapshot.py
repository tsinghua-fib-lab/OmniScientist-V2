"""Freeze grounded scientific content while allowing visual poster revisions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

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
_NON_VISIBLE_TEXT_TAGS = frozenset({"noscript", "script", "style", "template"})
_TEXT_BOUNDARY_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

SCIENTIFIC_CONTENT_SNAPSHOT_SCHEMA = "scientific-content-snapshot-v1"


@dataclass(frozen=True, slots=True)
class ScientificHtmlFacts:
    """Scientific and title-band facts collected from one HTML parse."""

    snapshot: Mapping[str, Any]
    title_band_text: tuple[str, ...]
    author_text: tuple[str, ...]
    author_marker_count: int
    venue_marker: bool
    logo_sources: frozenset[str]
    source_figure_sha256s: frozenset[str]
    equation_latex: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _TextSpan:
    start: int
    end: int
    raw: str


class _ModuleTextSpanParser(HTMLParser):
    """Locate visible text nodes without freezing a module's visual structure."""

    def __init__(self, html_text: str) -> None:
        super().__init__(convert_charrefs=False)
        self.spans: dict[str, list[_TextSpan]] = {}
        self.invalid = False
        self._line_starts = [0]
        self._stack: list[dict[str, Any]] = []
        for match in re.finditer(r"\n", html_text):
            self._line_starts.append(match.end())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {name.lower(): str(value or "") for name, value in attrs}
        module_id = attributes.get("data-poster-module", "").strip()
        parent = self._stack[-1] if self._stack else {}
        parent_module_id = str(parent.get("module_id") or "")
        if module_id and (
            module_id in self.spans or parent_module_id or tag in _VOID_TAGS
        ):
            self.invalid = True
        if module_id and module_id not in self.spans:
            self.spans[module_id] = []
        frame = {
            "tag": tag,
            "module_id": module_id or parent_module_id,
            "owns_module": bool(module_id),
            "visible": bool(parent.get("visible", True))
            and tag not in _NON_VISIBLE_TEXT_TAGS,
        }
        if tag not in _VOID_TAGS:
            self._stack.append(frame)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {name.lower(): str(value or "") for name, value in attrs}
        if attributes.get("data-poster-module", "").strip():
            self.invalid = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]["tag"] != tag:
                continue
            closed = self._stack[index:]
            if any(frame.get("owns_module") for frame in closed[1:]):
                self.invalid = True
            del self._stack[index:]
            return

    def handle_data(self, data: str) -> None:
        self._record_text(data)

    def handle_entityref(self, name: str) -> None:
        self._record_text(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._record_text(f"&#{name};")

    def finish(self) -> None:
        self.close()
        if any(frame.get("owns_module") for frame in self._stack):
            self.invalid = True

    def _record_text(self, raw: str) -> None:
        if not self._stack or not raw.strip():
            return
        frame = self._stack[-1]
        module_id = str(frame.get("module_id") or "")
        if not module_id or not frame.get("visible"):
            return
        start = self._absolute_position()
        self.spans[module_id].append(
            _TextSpan(start=start, end=start + len(raw), raw=raw)
        )

    def _absolute_position(self) -> int:
        line, column = self.getpos()
        if line < 1 or line > len(self._line_starts):
            self.invalid = True
            return 0
        return self._line_starts[line - 1] + column


class _ScientificContentParser(HTMLParser):
    """Collect content-frozen scientific and identity fields from poster markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.modules: list[dict[str, Any]] = []
        self.titles: list[list[str]] = []
        self.authors: list[list[str]] = []
        self.venues: list[list[str]] = []
        self.venue_logos: list[tuple[str, str]] = []
        self.title_band_text: list[str] = []
        self.author_text: list[str] = []
        self.author_marker_count = 0
        self.venue_marker = False
        self.logo_sources: set[str] = set()
        self.contract_source_figure_sha256s: set[str] = set()
        self.contract_equation_latex: dict[str, list[str]] = {}
        self._stack: list[dict[str, Any]] = []

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
        parent = self._stack[-1] if self._stack else {}
        hidden = bool(parent.get("hidden")) or _element_is_hidden(tag, attributes)
        in_title_band = bool(parent.get("in_title_band")) or (
            "data-poster-title-band" in attributes
        )
        contract_module_id = attributes.get("data-poster-module", "").strip() or str(
            parent.get("contract_module_id") or ""
        )
        contract_content_role = attributes.get("data-content-role", "").strip() or str(
            parent.get("contract_content_role") or ""
        )
        inline_style = attributes.get("style", "").replace(" ", "").lower()
        contract_hidden = (
            bool(parent.get("contract_hidden"))
            or "display:none" in inline_style
            or "visibility:hidden" in inline_style
        )

        if in_title_band:
            self.venue_marker |= attributes.get("data-poster-venue") == "verified"
            if tag == "img" and "data-poster-venue-logo" in attributes:
                self.logo_sources.add(attributes.get("src", "").strip())
            if attributes.get("data-poster-authors") == "verified":
                self.author_marker_count += 1

        source_figure_sha256 = attributes.get("data-source-figure-sha256", "").strip()
        if (
            tag == "img"
            and not contract_hidden
            and re.fullmatch(r"[0-9a-f]{64}", source_figure_sha256)
        ):
            self.contract_source_figure_sha256s.add(source_figure_sha256)
        if (
            tag == "math"
            and contract_module_id
            and contract_content_role == "equation"
            and not contract_hidden
        ):
            self.contract_equation_latex.setdefault(contract_module_id, []).append(
                attributes.get("data-latex", "")
            )

        module_index = parent.get("module_index")
        if "data-poster-module" in attributes:
            module_index = len(self.modules)
            self.modules.append(
                {
                    "module_id": attributes.get("data-poster-module", "").strip(),
                    "poster_id": attributes.get("data-poster-id", "").strip(),
                    "semantic_roles": tuple(
                        sorted(set(attributes.get("data-semantic-roles", "").split()))
                    ),
                    "priority": attributes.get("data-module-priority", "").strip(),
                    "_text": [],
                    "_text_by_role": {},
                    "source_labels": [],
                    "focal_roles": [],
                    "source_figure_sha256s": [],
                    "equation_latex": [],
                    "equation_structures": [],
                }
            )

        content_role = attributes.get("data-content-role", "").strip() or str(
            parent.get("content_role") or ""
        )
        math_capture = parent.get("math_capture")
        if (
            tag == "math"
            and contract_module_id
            and contract_content_role == "equation"
            and not contract_hidden
        ):
            math_capture = {"module_index": module_index, "tokens": []}
        if isinstance(math_capture, dict):
            tokens = math_capture.get("tokens")
            if isinstance(tokens, list):
                tokens.append(("start", tag, tuple(sorted(attributes.items()))))
        title_index = parent.get("title_index")
        if in_title_band and tag == "h1" and title_index is None:
            title_index = len(self.titles)
            self.titles.append([])
        author_index = parent.get("author_index")
        if (
            in_title_band
            and attributes.get("data-poster-authors") == "verified"
            and author_index is None
        ):
            author_index = len(self.authors)
            self.authors.append([])
        venue_index = parent.get("venue_index")
        if (
            in_title_band
            and attributes.get("data-poster-venue") == "verified"
            and venue_index is None
        ):
            venue_index = len(self.venues)
            self.venues.append([])

        if not hidden and module_index is not None:
            module = self.modules[int(module_index)]
            source_label = _normalize_visible_text(
                attributes.get("data-source-label", "")
            )
            if source_label:
                module["source_labels"].append(source_label)
            focal_role = attributes.get("data-focal-role", "").strip()
            if focal_role:
                module["focal_roles"].append(focal_role)
            if tag == "img":
                digest = attributes.get("data-source-figure-sha256", "").strip()
                if digest:
                    module["source_figure_sha256s"].append(digest.casefold())
            if "data-latex" in attributes:
                module["equation_latex"].append(
                    _normalize_visible_text(attributes["data-latex"])
                )
        if (
            not hidden
            and in_title_band
            and tag == "img"
            and "data-poster-venue-logo" in attributes
        ):
            self.venue_logos.append(
                (
                    attributes.get("src", "").strip(),
                    _normalize_visible_text(attributes.get("alt", "")),
                )
            )

        frame = {
            "tag": tag,
            "hidden": hidden,
            "in_title_band": in_title_band,
            "module_index": module_index,
            "content_role": content_role,
            "title_index": title_index,
            "author_index": author_index,
            "venue_index": venue_index,
            "contract_module_id": contract_module_id,
            "contract_content_role": contract_content_role,
            "contract_hidden": contract_hidden,
            "math_capture": math_capture,
        }
        if self_closing and isinstance(math_capture, dict):
            self._finish_math_capture(math_capture, tag)
        if tag in _TEXT_BOUNDARY_TAGS:
            self._record_text(frame, " ")
        if not self_closing:
            self._stack.append(frame)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].get("tag") == tag:
                frame = self._stack[index]
                math_capture = frame.get("math_capture")
                if isinstance(math_capture, dict):
                    self._finish_math_capture(math_capture, tag)
                if tag in _TEXT_BOUNDARY_TAGS:
                    self._record_text(frame, " ")
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if not self._stack:
            return
        frame = self._stack[-1]
        if frame.get("in_title_band") and data.strip():
            self.title_band_text.append(data)
            if frame.get("author_index") is not None:
                self.author_text.append(data)
        math_capture = frame.get("math_capture")
        if isinstance(math_capture, dict):
            token = _normalize_visible_text(data)
            if token:
                tokens = math_capture.get("tokens")
                if isinstance(tokens, list):
                    tokens.append(("text", token))
        self._record_text(frame, data)

    def _finish_math_capture(self, capture: dict[str, Any], tag: str) -> None:
        """Record canonical MathML element, attribute, and text tokens at root close."""

        tokens = capture.get("tokens")
        if not isinstance(tokens, list):
            return
        tokens.append(("end", tag))
        if tag != "math":
            return
        module_index = capture.get("module_index")
        if module_index is None:
            return
        self.modules[int(module_index)]["equation_structures"].append(tuple(tokens))

    def _record_text(self, frame: Mapping[str, Any], text: str) -> None:
        if frame.get("hidden") or not text:
            return
        module_index = frame.get("module_index")
        if module_index is not None:
            module = self.modules[int(module_index)]
            module["_text"].append(text)
            content_role = str(frame.get("content_role") or "")
            if content_role:
                text_by_role = module["_text_by_role"]
                text_by_role.setdefault(content_role, []).append(text)
        for key, groups in (
            ("title_index", self.titles),
            ("author_index", self.authors),
            ("venue_index", self.venues),
        ):
            group_index = frame.get(key)
            if group_index is not None:
                groups[int(group_index)].append(text)


def parse_scientific_html(html_text: str) -> ScientificHtmlFacts:
    """Collect reusable scientific, identity, figure, and equation facts once."""

    parser = _ScientificContentParser()
    parser.feed(html_text)
    parser.close()
    snapshot = _snapshot_from_parser(parser)
    return ScientificHtmlFacts(
        snapshot=snapshot,
        title_band_text=tuple(parser.title_band_text),
        author_text=tuple(parser.author_text),
        author_marker_count=parser.author_marker_count,
        venue_marker=parser.venue_marker,
        logo_sources=frozenset(parser.logo_sources),
        source_figure_sha256s=frozenset(parser.contract_source_figure_sha256s),
        equation_latex={
            module_id: tuple(values)
            for module_id, values in parser.contract_equation_latex.items()
        },
    )


def scientific_content_snapshot(html_text: str) -> dict[str, Any]:
    """Return an order-independent snapshot of content frozen during visual revision."""

    return dict(parse_scientific_html(html_text).snapshot)


def restore_frozen_module_text(reference_html: str, candidate_html: str) -> str:
    """Restore unchanged-authority text while retaining candidate visual markup.

    Full-layout revision may regroup content inside a module, but it cannot rewrite its
    scientific copy. Text nodes are restored only when reference and candidate expose an
    unambiguous one-to-one sequence. Otherwise the candidate is left for the normal
    scientific snapshot validator to reject and repair rather than guessing.
    """

    reference = _ModuleTextSpanParser(reference_html)
    candidate = _ModuleTextSpanParser(candidate_html)
    reference.feed(reference_html)
    candidate.feed(candidate_html)
    reference.finish()
    candidate.finish()
    if (
        reference.invalid
        or candidate.invalid
        or not reference.spans
        or set(reference.spans) != set(candidate.spans)
    ):
        return candidate_html
    restored = candidate_html
    replacements: list[tuple[int, int, str]] = []
    for module_id, candidate_spans in candidate.spans.items():
        reference_spans = reference.spans[module_id]
        if len(reference_spans) != len(candidate_spans):
            continue
        replacements.extend(
            (candidate_span.start, candidate_span.end, reference_span.raw)
            for reference_span, candidate_span in zip(
                reference_spans,
                candidate_spans,
                strict=True,
            )
        )
    for start, end, raw_text in sorted(replacements, reverse=True):
        restored = restored[:start] + raw_text + restored[end:]
    return restored


def _snapshot_from_parser(parser: _ScientificContentParser) -> dict[str, Any]:
    """Build the stable scientific snapshot from already-parsed HTML facts."""

    modules: dict[str, list[dict[str, Any]]] = {}
    for raw in parser.modules:
        module = {
            "poster_id": raw["poster_id"],
            "visible_text": _normalize_visible_text("".join(raw["_text"])),
            "text_by_role": {
                role: _normalize_visible_text("".join(chunks))
                for role, chunks in sorted(raw["_text_by_role"].items())
            },
            "source_labels": tuple(sorted(raw["source_labels"])),
            "semantic_roles": raw["semantic_roles"],
            "priority": raw["priority"],
            "focal_roles": tuple(sorted(raw["focal_roles"])),
            "source_figure_sha256s": tuple(sorted(raw["source_figure_sha256s"])),
            "equation_latex": tuple(raw["equation_latex"]),
            "equation_structure": tuple(raw["equation_structures"]),
        }
        modules.setdefault(str(raw["module_id"]), []).append(module)
    return {
        "schema": SCIENTIFIC_CONTENT_SNAPSHOT_SCHEMA,
        "identity": {
            "title": tuple(_normalized_groups(parser.titles)),
            "authors": tuple(_normalized_groups(parser.authors)),
            "venue": tuple(_normalized_groups(parser.venues)),
            "venue_logos": tuple(sorted(parser.venue_logos)),
        },
        "modules": {
            module_id: tuple(sorted(entries, key=_module_snapshot_sort_key))
            for module_id, entries in sorted(modules.items())
        },
    }


def scientific_content_snapshot_issues(
    reference: str | Mapping[str, Any],
    candidate: str | Mapping[str, Any],
) -> list[dict[str, str]]:
    """Compare poster HTML or snapshots and report frozen scientific-content changes."""

    expected = _coerce_scientific_snapshot(reference)
    observed = _coerce_scientific_snapshot(candidate)
    issues: list[dict[str, str]] = []
    expected_identity = expected.get("identity")
    observed_identity = observed.get("identity")
    if expected_identity != observed_identity:
        identity_fields = _changed_mapping_fields(
            expected_identity,
            observed_identity,
        )
        issues.append(
            _issue(
                "scientific_identity_changed",
                "Visual revision changed verified poster identity field(s): "
                + ", ".join(identity_fields)
                + ".",
            )
        )

    expected_modules = expected.get("modules")
    observed_modules = observed.get("modules")
    expected_mapping = expected_modules if isinstance(expected_modules, Mapping) else {}
    observed_mapping = observed_modules if isinstance(observed_modules, Mapping) else {}
    expected_ids = {str(value) for value in expected_mapping}
    observed_ids = {str(value) for value in observed_mapping}
    if expected_ids != observed_ids:
        details: list[str] = []
        missing = sorted(expected_ids - observed_ids)
        unexpected = sorted(observed_ids - expected_ids)
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        issues.append(
            _issue(
                "scientific_module_set_changed",
                "Visual revision changed the scientific module set ("
                + "; ".join(details)
                + ").",
            )
        )
    for module_id in sorted(expected_ids & observed_ids):
        expected_entries = expected_mapping[module_id]
        observed_entries = observed_mapping[module_id]
        if expected_entries == observed_entries:
            continue
        fields = _changed_module_fields(expected_entries, observed_entries)
        issues.append(
            _issue(
                "scientific_module_changed",
                f"Visual revision changed frozen field(s) in module {module_id!r}: "
                + ", ".join(fields)
                + ".",
            )
        )
    return issues


def grounded_replan_snapshot_issues(
    reference: str | Mapping[str, Any],
    candidate: str | Mapping[str, Any],
    *,
    target_module_ids: frozenset[str] | set[str] | list[str],
) -> list[dict[str, str]]:
    """Allow grounded copy edits while freezing every other poster fact.

    Content replanning may alter only ``visible_text`` and ``text_by_role`` inside an
    existing module instance. Source grounding and the HTML contract remain separate
    gates; this comparator prevents the visual loop from using that freedom to replace
    identity, evidence, equation, role, or module structure.
    """

    expected = _coerce_scientific_snapshot(reference)
    observed = _coerce_scientific_snapshot(candidate)
    targets = {str(module_id).strip() for module_id in target_module_ids}
    issues: list[dict[str, str]] = []
    if expected.get("identity") != observed.get("identity"):
        issues.append(
            _issue(
                "scientific_identity_changed",
                "Content replan changed verified poster identity field(s): "
                + ", ".join(
                    _changed_mapping_fields(
                        expected.get("identity"), observed.get("identity")
                    )
                )
                + ".",
            )
        )
    expected_modules = expected.get("modules")
    observed_modules = observed.get("modules")
    expected_mapping = expected_modules if isinstance(expected_modules, Mapping) else {}
    observed_mapping = observed_modules if isinstance(observed_modules, Mapping) else {}
    expected_ids = {str(value) for value in expected_mapping}
    observed_ids = {str(value) for value in observed_mapping}
    if expected_ids != observed_ids:
        details: list[str] = []
        missing = sorted(expected_ids - observed_ids)
        unexpected = sorted(observed_ids - expected_ids)
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        issues.append(
            _issue(
                "scientific_module_set_changed",
                "Content replan changed the scientific module set ("
                + "; ".join(details)
                + ").",
            )
        )
    for module_id in sorted(expected_ids & observed_ids):
        expected_instances = _module_instances_by_poster_id(expected_mapping[module_id])
        observed_instances = _module_instances_by_poster_id(observed_mapping[module_id])
        if expected_instances is None or observed_instances is None:
            issues.append(
                _issue(
                    "scientific_module_instances_changed",
                    f"Content replan changed module instances in {module_id!r}.",
                )
            )
            continue
        if set(expected_instances) != set(observed_instances):
            issues.append(
                _issue(
                    "scientific_module_instances_changed",
                    f"Content replan changed module instances in {module_id!r}.",
                )
            )
            continue
        for poster_id in sorted(expected_instances):
            expected_instance = expected_instances[poster_id]
            observed_instance = observed_instances[poster_id]
            changed = (
                _changed_module_fields_except_text(expected_instance, observed_instance)
                if module_id in targets
                else _changed_mapping_fields(expected_instance, observed_instance)
            )
            if module_id in targets and (
                expected_instance.get("text_by_role", {}).get("equation")
                != observed_instance.get("text_by_role", {}).get("equation")
            ):
                changed = sorted(set(changed) | {"text_by_role.equation"})
            if changed:
                issues.append(
                    _issue(
                        "scientific_module_changed",
                        f"Content replan changed frozen field(s) in module "
                        f"{module_id!r} instance {poster_id!r}: "
                        + ", ".join(changed)
                        + ".",
                    )
                )
    return issues


def _coerce_scientific_snapshot(
    value: str | Mapping[str, Any],
) -> Mapping[str, Any]:
    return scientific_content_snapshot(value) if isinstance(value, str) else value


def _changed_mapping_fields(expected: Any, observed: Any) -> list[str]:
    if not isinstance(expected, Mapping) or not isinstance(observed, Mapping):
        return ["snapshot"]
    return sorted(
        str(key)
        for key in set(expected) | set(observed)
        if expected.get(key) != observed.get(key)
    )


def _changed_module_fields(expected: Any, observed: Any) -> list[str]:
    if (
        isinstance(expected, (list, tuple))
        and isinstance(observed, (list, tuple))
        and len(expected) == len(observed) == 1
    ):
        fields = _changed_mapping_fields(expected[0], observed[0])
        return fields or ["content"]
    return ["module_instances"]


def _module_instances_by_poster_id(value: Any) -> dict[str, Mapping[str, Any]] | None:
    """Index a module's stable instances without treating copy as identity."""

    if not isinstance(value, (list, tuple)):
        return None
    indexed: dict[str, Mapping[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            return None
        poster_id = str(item.get("poster_id") or "").strip()
        if not poster_id or poster_id in indexed:
            return None
        indexed[poster_id] = item
    return indexed


def _changed_module_fields_except_text(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> list[str]:
    allowed = {"visible_text", "text_by_role"}
    return sorted(
        str(key)
        for key in set(expected) | set(observed)
        if key not in allowed and expected.get(key) != observed.get(key)
    )


def _module_snapshot_sort_key(module: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(module.get("poster_id") or ""),
        repr(module.get("source_labels")),
        str(module.get("visible_text") or ""),
        repr(module.get("text_by_role")),
        repr(module.get("semantic_roles")),
        str(module.get("priority") or ""),
        repr(module.get("focal_roles")),
        repr(module.get("source_figure_sha256s")),
        repr(module.get("equation_latex")),
        repr(module.get("equation_structure")),
    )


def _normalized_groups(groups: list[list[str]]) -> list[str]:
    return [_normalize_visible_text("".join(group)) for group in groups]


def _normalize_visible_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _element_is_hidden(tag: str, attributes: Mapping[str, str]) -> bool:
    style = re.sub(r"\s+", "", attributes.get("style", "")).casefold()
    return (
        tag in _NON_VISIBLE_TEXT_TAGS
        or "hidden" in attributes
        or attributes.get("aria-hidden", "").casefold() == "true"
        or "display:none" in style
        or "visibility:hidden" in style
    )


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": "error", "message": message}
