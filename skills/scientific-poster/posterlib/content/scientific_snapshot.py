"""Freeze grounded scientific content while allowing visual poster revisions."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from css_validation import inline_style_is_hidden

from posterlib.runtime.identifiers import is_sha256

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
_TABLE_STRUCTURE_ATTRIBUTES = {
    "table": (),
    "thead": (),
    "tbody": (),
    "tfoot": (),
    "tr": (),
    "th": ("id", "headers", "scope", "abbr", "colspan", "rowspan"),
    "td": ("id", "headers", "scope", "abbr", "colspan", "rowspan"),
    "colgroup": ("span",),
    "col": ("span",),
}
_TABLE_ROLE_TAGS = {
    "table": "table", "rowgroup": "tbody", "row": "tr",
    "columnheader": "th", "rowheader": "th", "cell": "td",
}
_TABLE_ARIA_ATTRIBUTES = (
    "role", "aria-rowcount", "aria-colcount", "aria-rowindex", "aria-colindex",
    "aria-rowspan", "aria-colspan", "aria-labelledby",
)
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
    all_equation_structures: tuple[tuple[Any, ...], ...]
    all_equation_latex: tuple[str, ...]


class _ScientificContentParser(HTMLParser):
    """Collect content-frozen scientific and identity fields from poster markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.modules: list[dict[str, Any]] = []
        self.titles: list[list[Any]] = []
        self.authors: list[list[Any]] = []
        self.affiliations: list[list[Any]] = []
        self.venues: list[list[Any]] = []
        self.venue_logos: list[tuple[str, str]] = []
        self.identity_equation_structures: list[tuple[Any, ...]] = []
        self.all_equation_structures: list[tuple[Any, ...]] = []
        self.all_equation_latex: list[str] = []
        self.unbound_equation_structures: list[tuple[Any, ...]] = []
        self.unbound_source_figures: list[tuple[str, str]] = []
        self.unbound_native_components: list[str] = []
        self.unbound_tables: list[list[Any]] = []
        self.unbound_text: list[Any] = []
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

        if in_title_band:
            self.venue_marker |= attributes.get("data-poster-venue") == "verified"
            if tag == "img" and "data-poster-venue-logo" in attributes:
                self.logo_sources.add(attributes.get("src", "").strip())
            if attributes.get("data-poster-authors") == "verified":
                self.author_marker_count += 1

        source_figure_sha256 = attributes.get("data-source-figure-sha256", "").strip()
        native_component_sha256 = ""
        if tag == "img":
            native_component_sha256 = attributes.get("data-native-component-sha256", "").strip()
            if attributes.get("data-native-pptx"):
                from posterlib.delivery.pptx_components import decode_component

                native_component_sha256 = hashlib.sha256(
                    decode_component(attributes["data-native-pptx"])
                ).hexdigest()
        if (
            tag == "img"
            and not hidden
            and is_sha256(source_figure_sha256)
        ):
            self.contract_source_figure_sha256s.add(source_figure_sha256)
        if (
            tag == "math"
            and contract_module_id
            and contract_content_role == "equation"
            and not hidden
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
                    "visual_kind": attributes.get("data-visual-kind", "").strip(),
                    "_text": [],
                    "_text_by_role": {},
                    "_unscoped_text": [],
                    "source_labels": [],
                    "source_figure_sha256s": [],
                    "source_figure_geometry": [],
                    "native_component_sha256s": [],
                    "equation_latex": [],
                    "equation_structures": [],
                    "table_structures": [],
                    "figure_groups": [],
                }
            )

        figure_capture = parent.get("figure_capture")
        if (tag == "figure" or attributes.get("role") == "figure") and module_index is not None and not hidden:
            figure_capture = []
            self.modules[int(module_index)]["figure_groups"].append(figure_capture)
        if tag == "img" and source_figure_sha256 and not hidden:
            if module_index is None:
                self.unbound_source_figures.append((
                    source_figure_sha256.casefold(),
                    attributes.get("data-source-figure-aspect-ratio", "").strip(),
                ))
            if isinstance(figure_capture, list):
                figure_capture.append(("source_figure", source_figure_sha256.casefold()))
        if tag == "img" and native_component_sha256 and not hidden and module_index is None:
            self.unbound_native_components.append(native_component_sha256)

        # Table semantics remain independent of module copy ownership. Explicit
        # presentation tables are layout wrappers; enclosing data cells still
        # capture their content.
        table_tag = _TABLE_ROLE_TAGS.get(attributes.get("role", ""), tag)
        table_attributes = _TABLE_STRUCTURE_ATTRIBUTES.get(table_tag)
        table_capture = parent.get("table_capture")
        table_is_data = bool(parent.get("table_is_data"))
        if tag == "caption":
            table_capture = None  # Captions are prose; rows and cells own table data.
        if table_tag == "table" and not hidden:
            table_is_data = attributes.get("role") not in {"presentation", "none"}
            if table_is_data:
                parent_table = table_capture
                table_capture = []
                if isinstance(parent_table, list):
                    parent_table.append(("nested_table", table_capture))
                if module_index is None:
                    self.unbound_tables.append(table_capture)
                else:
                    self.modules[int(module_index)]["table_structures"].append(table_capture)
        if isinstance(table_capture, list) and not hidden:
            if "data-comparison-fraction" in attributes:
                table_capture.append(("comparison_fraction", attributes["data-comparison-fraction"]))
            if table_is_data and table_attributes is not None:
                table_capture.append((
                    "start", table_tag,
                    tuple((name, attributes[name])
                          for name in (*table_attributes, *_TABLE_ARIA_ATTRIBUTES)
                          if name in attributes),
                ))
                if self_closing:
                    table_capture.append(("end", table_tag))
            elif tag == "img":
                table_capture.append((
                    "figure", native_component_sha256 or source_figure_sha256 or attributes.get("src", ""),
                    attributes.get("alt", ""),
                ))

        content_role = attributes.get("data-content-role", "").strip() or str(
            parent.get("content_role") or ""
        )
        if not content_role and module_index is not None and tag in {"h2", "h3"}:
            content_role = "heading"
        math_capture = parent.get("math_capture")
        if not hidden and "data-latex" in attributes:
            self.all_equation_latex.append(attributes["data-latex"].strip())
        if tag == "math" and not hidden:
            math_capture = {
                "module_index": module_index, "identity": in_title_band, "tokens": [],
                "table_capture": table_capture,
            }
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
        affiliation_index = parent.get("affiliation_index")
        if (
            in_title_band
            and attributes.get("data-poster-affiliations") == "verified"
            and affiliation_index is None
        ):
            affiliation_index = len(self.affiliations)
            self.affiliations.append([])
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
            if tag == "img":
                if native_component_sha256:
                    module["native_component_sha256s"].append(native_component_sha256)
                digest = attributes.get("data-source-figure-sha256", "").strip()
                if digest:
                    module["source_figure_sha256s"].append(digest.casefold())
                    module["source_figure_geometry"].append(
                        (
                            digest.casefold(),
                            attributes.get(
                                "data-source-figure-aspect-ratio", ""
                            ).strip(),
                        )
                    )
            if "data-latex" in attributes:
                module["equation_latex"].append(
                    attributes["data-latex"].strip()
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
            "in_body": bool(parent.get("in_body")) or tag == "body",
            "in_title_band": in_title_band,
            "module_index": module_index,
            "content_role": content_role,
            "title_index": title_index,
            "author_index": author_index,
            "author_pointer": bool(parent.get("author_pointer")) or (author_index is not None and tag == "sup"),
            "affiliation_index": affiliation_index,
            "venue_index": venue_index,
            "contract_module_id": contract_module_id,
            "contract_content_role": contract_content_role,
            "math_capture": math_capture,
            "table_capture": table_capture,
            "table_is_data": table_is_data,
            "table_tag": table_tag if table_is_data and table_attributes is not None else "",
            "figure_capture": figure_capture,
            "in_figure_caption": bool(parent.get("in_figure_caption")) or tag == "figcaption"
            or attributes.get("data-content-role") == "caption",
            "text_boundary": tag in _TEXT_BOUNDARY_TAGS or bool(attributes.get("data-content-role")),
        }
        if tag == "math" and isinstance(math_capture, dict) and attributes.get("display") != "block":
            math_capture["text_frame"] = {**frame, "math_capture": None, "table_capture": None}
        if self_closing and isinstance(math_capture, dict):
            self._finish_math_capture(math_capture, tag)
        if frame["text_boundary"]:
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
                table_capture = frame.get("table_capture")
                if (
                    isinstance(table_capture, list) and not frame.get("hidden")
                    and frame.get("table_tag")
                ):
                    table_capture.append(("end", frame["table_tag"]))
                if frame.get("text_boundary"):
                    self._record_text(frame, " ")
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if not self._stack:
            return
        frame = self._stack[-1]
        if frame.get("tag") == "style":
            return
        if frame.get("in_title_band") and not frame.get("hidden") and data.strip():
            self.title_band_text.append(data)
            if frame.get("author_index") is not None and not frame.get("author_pointer"):
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
        self.all_equation_structures.append(tuple(tokens))
        if text_frame := capture.get("text_frame"):
            self._record_text(text_frame, ("math", tuple(tokens)))
        table_capture = capture.get("table_capture")
        if isinstance(table_capture, list):
            table_capture.append(("math", tuple(tokens)))
        if capture.get("identity"):
            self.identity_equation_structures.append(tuple(tokens))
        module_index = capture.get("module_index")
        if module_index is None:
            if not capture.get("identity"):
                self.unbound_equation_structures.append(tuple(tokens))
            return
        self.modules[int(module_index)]["equation_structures"].append(tuple(tokens))

    def _record_text(self, frame: Mapping[str, Any], text: str | tuple[Any, ...]) -> None:
        if (
            frame.get("hidden")
            or not text
            or isinstance(frame.get("math_capture"), dict)
        ):
            return
        table_capture = frame.get("table_capture")
        if isinstance(table_capture, list):
            table_capture.append(text)
        figure_capture = frame.get("figure_capture")
        if isinstance(figure_capture, list) and frame.get("in_figure_caption"):
            figure_capture.append(text)
        module_index = frame.get("module_index")
        if module_index is not None:
            module = self.modules[int(module_index)]
            module["_text"].append(text)
            content_role = str(frame.get("content_role") or "")
            if content_role:
                text_by_role = module["_text_by_role"]
                text_by_role.setdefault(content_role, []).append(text)
            else:
                module["_unscoped_text"].append(text)
        identity_groups = (
            ("title_index", self.titles),
            ("author_index", self.authors),
            ("affiliation_index", self.affiliations),
            ("venue_index", self.venues),
        )
        for key, groups in identity_groups:
            group_index = frame.get(key)
            if group_index is not None:
                groups[int(group_index)].append(text)
        if (
            frame.get("in_body")
            and module_index is None
            and all(frame.get(key) is None for key, _groups in identity_groups)
        ):
            self.unbound_text.append(text)


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
        all_equation_structures=tuple(parser.all_equation_structures),
        all_equation_latex=tuple(parser.all_equation_latex),
    )


def scientific_content_snapshot(html_text: str) -> dict[str, Any]:
    """Return an order-independent snapshot of content frozen during visual revision."""

    return dict(parse_scientific_html(html_text).snapshot)


def _snapshot_from_parser(parser: _ScientificContentParser) -> dict[str, Any]:
    """Build the stable scientific snapshot from already-parsed HTML facts."""

    modules: dict[str, list[dict[str, Any]]] = {}
    for raw in parser.modules:
        module = {
            "poster_id": raw["poster_id"],
            "visible_text": _visible_text(raw["_text"]),
            "text_by_role": {
                role: _visible_text(chunks)
                for role, chunks in sorted(raw["_text_by_role"].items())
            },
            "unscoped_text": _visible_text(raw["_unscoped_text"]),
            **_inline_math_context({**raw["_text_by_role"], "": raw["_unscoped_text"]}),
            "source_labels": tuple(sorted(raw["source_labels"])),
            "semantic_roles": raw["semantic_roles"],
            "priority": raw["priority"],
            "visual_kind": raw["visual_kind"],
            "source_figure_sha256s": tuple(sorted(raw["source_figure_sha256s"])),
            "source_figure_geometry": tuple(sorted(raw["source_figure_geometry"])),
            **({"native_component_sha256s": tuple(sorted(raw["native_component_sha256s"]))}
               if raw["native_component_sha256s"] else {}),
            "equation_latex": tuple(raw["equation_latex"]),
            "equation_structure": tuple(raw["equation_structures"]),
            **({"table_structure": tuple(
                _mixed_content_tokens(tokens) for tokens in raw["table_structures"]
            )} if raw["table_structures"] else {}),
        }
        figure_groups = [
            _mixed_content_tokens(tokens) for tokens in raw["figure_groups"]
            if any(isinstance(token, tuple) and token[0] == "source_figure" for token in tokens)
            and (_visible_text(tokens) or any(
                isinstance(token, tuple) and token[0] == "math" for token in tokens
            ))
        ]
        if figure_groups:
            module["source_figure_groups"] = tuple(sorted(figure_groups, key=repr))
        modules.setdefault(str(raw["module_id"]), []).append(module)
    unbound_tables = [
        _mixed_content_tokens(tokens) for tokens in parser.unbound_tables
    ]
    return {
        "schema": SCIENTIFIC_CONTENT_SNAPSHOT_SCHEMA,
        "unbound_text": _visible_text(parser.unbound_text),
        **_inline_math_context({"": parser.unbound_text}),
        **({"unbound_equation_structure": tuple(parser.unbound_equation_structures)}
           if parser.unbound_equation_structures else {}),
        **({"unbound_source_figures": tuple(sorted(parser.unbound_source_figures))}
           if parser.unbound_source_figures else {}),
        **({"unbound_native_components": tuple(sorted(parser.unbound_native_components))}
           if parser.unbound_native_components else {}),
        **({"unbound_table_structure": tuple(sorted(unbound_tables, key=repr))}
           if unbound_tables else {}),
        "identity": {
            "title": tuple(_normalized_groups(parser.titles)),
            "authors": tuple(_normalized_groups(parser.authors)),
            "affiliations": tuple(_normalized_groups(parser.affiliations)),
            "venue": tuple(_normalized_groups(parser.venues)),
            "venue_logos": tuple(sorted(parser.venue_logos)),
            **_inline_math_context({
                f"{name}:{index}": chunks
                for name, groups in (("title", parser.titles), ("authors", parser.authors),
                                     ("affiliations", parser.affiliations), ("venue", parser.venues))
                for index, chunks in enumerate(groups)
            }),
            **({"equation_structure": tuple(parser.identity_equation_structures)}
               if parser.identity_equation_structures else {}),
        },
        "modules": {
            module_id: tuple(sorted(entries, key=_module_snapshot_sort_key))
            for module_id, entries in sorted(modules.items())
        },
    }


def scientific_content_snapshot_issues(
    reference: str | Mapping[str, Any],
    candidate: str | Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Compare poster HTML or snapshots and report frozen scientific-content changes."""

    expected = _coerce_scientific_snapshot(reference)
    observed = _coerce_scientific_snapshot(candidate)
    issues = _unbound_content_issues(expected, observed)
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
        issue: dict[str, Any] = _issue(
            "scientific_module_changed",
            f"Visual revision changed frozen field(s) in module {module_id!r}: "
            + ", ".join(fields)
            + ".",
        )
        if isinstance(expected_entries, (list, tuple)) and isinstance(
            observed_entries, (list, tuple)
        ) and len(expected_entries) == len(observed_entries) == 1 and all(
            isinstance(entry, Mapping) for entry in (expected_entries[0], observed_entries[0])
        ):
            issue["module_id"] = module_id
            issue["expected"] = {field: expected_entries[0].get(field) for field in fields}
            issue["observed"] = {field: observed_entries[0].get(field) for field in fields}
        issues.append(issue)
    return issues


def grounded_replan_snapshot_issues(
    reference: str | Mapping[str, Any],
    candidate: str | Mapping[str, Any],
    *,
    target_module_ids: frozenset[str] | set[str] | list[str],
) -> list[dict[str, Any]]:
    """Allow grounded copy edits while freezing every other poster fact.

    Module targets own their prose; poster targets unowned prose. Identity, table
    data, native equations, image inventories and non-target content remain fixed.
    """

    expected = _coerce_scientific_snapshot(reference)
    observed = _coerce_scientific_snapshot(candidate)
    targets = {str(module_id).strip() for module_id in target_module_ids}
    issues = _unbound_content_issues(expected, observed, allow_copy="poster" in targets)
    targets.discard("poster")
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
                _changed_module_fields_except_text(
                    expected_instance,
                    observed_instance,
                )
                if module_id in targets
                else _changed_mapping_fields(expected_instance, observed_instance)
            )
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


def _unbound_content_issues(
    expected: Mapping[str, Any], observed: Mapping[str, Any],
    *,
    allow_copy: bool = False,
) -> list[dict[str, Any]]:
    """Preserve unowned scientific content independently of incidental wrappers."""

    fields = ("unbound_equation_structure",) + (
        () if allow_copy else ("unbound_text", "inline_math_context")
    )
    before = tuple(expected.get(field) for field in fields)
    after = tuple(observed.get(field) for field in fields)
    issues = [] if before == after else [{
        **_issue(
            "scientific_unbound_text_changed",
            "Revision changed visible text or mathematics outside scientific modules or verified identity.",
        ),
        "expected": before,
        "observed": after,
    }]
    if expected.get("unbound_source_figures") != observed.get("unbound_source_figures"):
        issues.append(_issue(
            "scientific_unbound_figure_changed",
            "Revision changed source-figure occurrences outside scientific modules.",
        ))
    if expected.get("unbound_native_components") != observed.get("unbound_native_components"):
        issues.append(_issue(
            "scientific_unbound_component_changed",
            "Revision changed native drawing component occurrences outside scientific modules.",
        ))
    if expected.get("unbound_table_structure") != observed.get("unbound_table_structure"):
        issues.append(_issue(
            "scientific_unbound_table_changed",
            "Revision changed data-table cells or relationships outside scientific modules.",
        ))
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
    allowed_fields = {
        "visible_text", "text_by_role", "unscoped_text", "inline_math_context",
        "source_figure_groups",  # Caption wording; image and math identities have their own fields.
    }
    return sorted(
        str(key)
        for key in set(expected) | set(observed)
        if key not in allowed_fields and expected.get(key) != observed.get(key)
    )


def _module_snapshot_sort_key(module: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(module.get("poster_id") or ""),
        repr(module.get("source_labels")),
        str(module.get("visible_text") or ""),
        repr(module.get("text_by_role")),
        repr(module.get("inline_math_context")),
        repr(module.get("semantic_roles")),
        str(module.get("priority") or ""),
        str(module.get("visual_kind") or ""),
        repr(module.get("source_figure_sha256s")),
        repr(module.get("source_figure_geometry")),
        repr(module.get("native_component_sha256s")),
        repr(module.get("equation_latex")),
        repr(module.get("equation_structure")),
        repr(module.get("table_structure")),
    )


def _mixed_content_tokens(tokens: list[Any]) -> tuple[Any, ...]:
    """Normalize text between exact semantic object and boundary tokens."""

    result: list[Any] = []
    text: list[str] = []
    for token in [*tokens, None]:
        if isinstance(token, str):
            text.append(token)
            continue
        value = _normalize_visible_text("".join(text))
        if value:
            result.append(("text", value))
        text.clear()
        if token is not None:
            if token[0] == "nested_table":
                token = ("nested_table", _mixed_content_tokens(token[1]))
            result.append(token)
    return tuple(result)


def _inline_math_context(groups: Mapping[str, list[Any]]) -> dict[str, Any]:
    """Bind inline expressions to surrounding text through the shared token stream."""

    contexts = {
        role: _mixed_content_tokens(chunks) for role, chunks in groups.items()
        if any(isinstance(chunk, tuple) for chunk in chunks)
    }
    return {"inline_math_context": contexts} if contexts else {}


def _visible_text(chunks: list[Any]) -> str:
    return _normalize_visible_text("".join(chunk for chunk in chunks if isinstance(chunk, str)))


def _normalized_groups(groups: list[list[Any]]) -> list[str]:
    return [_visible_text(group) for group in groups]


def _normalize_visible_text(value: str) -> str:
    return " ".join(value.split())


def _element_is_hidden(tag: str, attributes: Mapping[str, str]) -> bool:
    return (
        tag in _NON_VISIBLE_TEXT_TAGS
        or "hidden" in attributes
        or inline_style_is_hidden(attributes.get("style", ""))
    )


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": "error", "message": message}
