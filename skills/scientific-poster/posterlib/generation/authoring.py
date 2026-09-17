"""Prompt contracts for grounded scientific-poster authoring and repair."""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from html import escape, unescape
from itertools import combinations, groupby, pairwise
from typing import Any

import poster_assets

from posterlib.content import planning
from posterlib.content.equations import latex_to_mathml
from posterlib.visual import visual_design

# Reference-extracted spacing should yield before it produces a measured page
# overflow. Compact placement gets more estimator slack because Chromium remains
# the delivery authority and the coarse depth model otherwise adds a needless lane.
_REFERENCE_LAYOUT_TOLERANCE = 1.03
_PACKING_ESTIMATE_TOLERANCE = 1.03
_AVERAGE_BODY_GLYPH_EM = 0.46
_SPACING_DEPTH_FACTORS = {"compact": 0.95, "balanced": 1.0, "open": 1.08}
_TEXT_ONLY_READER_SCALE = 1.15
_STRUCTURED_TEXT_KINDS = frozenset({"comparison", "method-flow", "metrics", "table"})


def _content_typography_metrics(
    width_mm: float,
    modules: list[dict[str, Any]],
    *,
    column_count: int | None = None,
) -> dict[str, float]:
    """Use the larger text-only scale only while lanes remain broad."""

    metrics = dict(planning.typography_metrics(width_mm))
    if any(module.get("figure_sha256s") for module in modules) or (
        column_count is not None and column_count >= 4
    ):
        return metrics
    for key in (
        "section_heading_min_mm",
        "body_min_mm",
        "body_target_mm",
        "provenance_min_mm",
    ):
        metrics[key] = round(metrics[key] * _TEXT_ONLY_READER_SCALE, 2)
    return metrics


def evidence_budget_prompt(
    *,
    source_text: str,
    assets: list[dict[str, Any]],
    authoring_request: str,
    page: Any = None,
) -> tuple[str, str]:
    """Request a truthful page-aware content budget before layout is authored."""

    figures = _prepared_figure_manifest(assets)
    system = """Plan the scientific content of one conference poster from the supplied source.
Return exactly one JSON object with no Markdown or commentary.

SCIENTIFIC EVIDENCE
Use only source-supported claims, literal numbers, locators, equations, and prepared figure hashes.
Preserve epistemic qualifiers, causal status, scope, variants, and assumptions. Never substitute a
base, large, ablated, or task-specific variant; retain all exceptions that affect a superlative and
the assumptions attached to an assumption-dependent quantity. Bind each number or comparison to its
experiment, model, dataset, protocol, axis, and table cell. When running prose conflicts with a displayed table,
preserve the table value and context. Transcribe only decision-essential equations,
including relations, limits, and bounds, as {"latex":"...","source_label":"Equation (6)"}.
Default to observational language for measured associations unless the supplied source explicitly
identifies a causal design or intervention. A plausible mechanism may explain an observation, but
must not be promoted into a causal result in a module title, interpretation, or takeaway.
When the source calls a quantity a proxy or not a direct measure of the target construct, keep that
boundary in the same module as the quantity instead of silently upgrading the claim.

ORGANIZATION
Find the paper's argumentative center instead of mirroring its section order. Use the fewest scan
sections and independently placeable modules needed to communicate the central contribution, the
method detail necessary to interpret it, decisive evidence, and any source-grounded qualification.
Combine closely related material; split only when evidence needs a different viewing scale or an
independent interpretation. Sections are source-grounded scan groups, not rails or a mandatory
problem/method/results story. Narrative ordering is optional and only for a genuinely sequential
argument; otherwise prefer scan-first. organization_mode, focal_role,
priority, and visual_kind are optional hints, not layout instructions. Do not emit semantic_roles.
Do not create a module for title, authors, affiliations, venue, logo, contact, or other masthead
identity. Omit a recap or conclusion module when it adds no evidence or qualification beyond modules
already present.

A poster module is not a miniature paper abstract. Choose its primary communication channel before
writing copy: source figure, compact comparison/table, metric callouts, equation/method flow, or prose.
Treat figures, equations, and structured values as evidence that replaces explanatory bulk, not as
decoration beside a paragraph. Its optional title, text, detail_points, takeaway, figures, and equations
are complementary channels, not a checklist to fill. A figure or equation normally needs only the
shortest interpretation that tells a reader what to notice. Parallel values normally belong in
visual_kind "metrics", "comparison", or "table", with compact evidence atoms for the author to format,
not a prose summary plus a default bullet list. Use both text and detail_points only when they carry
non-overlapping evidence and still form a compact module. Never restate one fact across text,
detail_points, and takeaway. Prefer one primary verbal channel per module and omit unused optional
fields instead of returning empty or repetitive fields. Every module still needs a unique kebab-case
id, a declared section_id, a concise source_label copied from the source's own provenance vocabulary,
and some visible evidence. The source_label is metadata, not visible poster copy: do not repeat it in
title, text, detail_points, or takeaway. Never set visual_kind to "figure" without binding a prepared
figure hash in figure_sha256s. Include a limitation only when the source supports a real boundary or
failure condition.

Write for a standing reader, not for a reviewer reconstructing the paper. In the first three seconds,
the title and module headings should expose the question, central finding, decisive evidence, and
method. In the next thirty seconds, figures, equations, and metric atoms should carry the argument.
Long prose is only for a qualification that cannot be made visual. Use short, declarative module
titles and compact scan atoms. Do not repeat model names, benchmarks, protocol settings, or values
that are already legible in the bound figure unless they are needed to interpret the claim or
preserve a qualifier. Let the paper's argument and evidence determine the amount of copy; do not
write toward a word, module, or figure quota.

The user request below contains the complete task for the outer poster workflow. During this content
step, ignore execution and presentation controls that are already bound elsewhere: page format,
orientation, visual reference or template, layout, palette, typography, rendering, review, export,
output paths, model/provider choice, and mock or tooling instructions. Those controls must not change
which scientific claims or source figures are selected. Honor only explicit audience, scientific
emphasis, content-density, inclusion, and exclusion preferences from the request.

Close with one compact scope or provenance module when the source provides a material limitation not
already visible elsewhere or a public paper, code, or project locator. Combine those items in one
module rather than adding separate footer cards. Omit this module when the source supplies neither;
never invent a link, limitation, or availability statement to fill space. When included, return it as
the final content_module because it closes the reading order. Do not assign a layout-specific priority
or placement hint; the independent reference grammar and deterministic renderer own its placement.

Use these module recipes as the default output shape:
- figure: title + source_label + figure hash + one declarative interpretation sentence, normally no
  more than about 35 words. Add no more than two brief detail points only when they identify protocol
  or a non-obvious comparison. A takeaway replaces, rather than repeats, that sentence.
- metrics: title + source_label + two to four standalone headline values or trends in detail_points.
- comparison: title + source_label + two or three compact contrast atoms in detail_points.
- table: title + source_label + three to six repeated rows that compare the same methods, conditions,
  or measures across datasets or settings. Put one complete row in each detail point so the renderer
  can expose the shared numeric structure instead of producing a prose list.
For metrics, comparison, and table modules, use text only for one protocol qualifier; normally omit
takeaway and equations.
- method-flow: use for an ordered process, lifecycle, architecture path, or equation-led method. Give
  one setup sentence, normally no more than about 35 words, followed by three to six compact ordered
  steps. Add one or two decision-essential equations only when the source has them; an equation is
  not required for a source-supported process flow. Do not reproduce a derivation.
- text/provenance: title + either one compact paragraph of roughly 45 words or fewer, or a short list
  of at most four scan atoms, not both.

The absence of prepared paper figures is not a reason to turn the poster into prose columns. Preserve
source-supported relationships through the structured channels above: ordered stages as method-flow,
repeated dimensions as table rows, contrasts as comparison atoms, and headline outcomes as metrics.
Do not invent a relationship merely to obtain a visual channel.

PHYSICAL CAPACITY AND FIGURES
Treat the requested page as a soft one-page physical-capacity budget, not a target to fill. Choose a
complementary, non-redundant set of prepared figures that explains the contribution, mechanism, and
decisive evidence. This is not a fixed count of figures, modules, sections, rails, or columns. Omit
redundant figures when labels would be unreadable; do not invent or repeat content when the page is
sparse. Respect the supplied figure-readability reference: plan the display scale before the count,
and consolidate or omit secondary plots when they cannot remain interpretable without zoom. Bind each
selected SHA-256 directly to the module that explains it. A source_label may cite additional evidence
used for the module's grounded claim without forcing every cited figure onto the poster. Never visibly describe an unbound prepared
figure.

For each claim, choose the most direct non-redundant evidence carrier that remains readable at the
planned physical size. Do not select a summary visual when selected direct evidence already delivers
the same point. A regular module should normally carry one prepared source figure; multiple figures
belong together only when their relationship is itself the evidence and each remains legible.

Treat a prepared figure as a visual channel only when its axes, geometry, panel relationship, or
spatial pattern is necessary to understand the argument at poster scale. When the same scientific
point can be communicated faithfully as a few values or one compact trend statement, prefer that
lighter channel if it preserves the source's conditions and qualifiers. The focal role does not
exempt an unreadable source figure. This decision depends on the paper's argument and the figure's
readable evidence area, never on a named figure category.

When the draft exceeds the readable page envelope, first merge modules that repeat one interpretation
or qualification, then replace a redundant visual channel with a lighter faithful channel. Preserve
independently interpretable evidence as a distinct module even when its section is shared. Do not solve
the excess by shrinking every module or by removing scientific qualifiers.

Before returning, review the whole budget against the physical page envelope. If it is too dense,
edit the evidence plan semantically: keep the main claim, key method, and decisive evidence; remove
repeated explanations and omit secondary figures or modules. Do not solve capacity by mechanically
truncating prose, weakening scientific qualifiers, shrinking figure intent, or inventing replacement
content.

Required top-level shape: {"sections":[...],"content_modules":[...]}.
Each section requires a unique kebab-case id and a short visible label. Each module requires id,
section_id, and source_label. Add only the useful optional fields among
title, text, detail_points, takeaway, priority, visual_kind, figure_sha256s, and equations. A sparse
module is valid; do not populate every optional field merely because it exists.
"""
    user = (
        "Create the grounded evidence budget now.\n\n"
        "Requested physical page (content capacity constraint):\n"
        f"{json.dumps(page, ensure_ascii=False, sort_keys=True) if page is not None else 'adaptive'}\n\n"
        "User preferences are context only, never scientific evidence:\n"
        f"{authoring_request or 'No additional preferences.'}\n\n"
        "Prepared PDF figures (the only allowed source-figure hashes):\n"
        f"{json.dumps(figures, ensure_ascii=False, sort_keys=True)}\n\n"
        "SUPPLIED PAPER OR GROUNDED BRIEF:\n<source>\n"
        f"{source_text}\n"
        "</source>"
    )
    return system, user


def constrain_visual_design_plan(
    content_budget: dict[str, Any],
    page_plan: dict[str, Any],
    design: visual_design.VisualDesignPlan,
) -> visual_design.VisualDesignPlan:
    """Adapt reference grammar only when its lanes cannot hold the physical page.

    Reference topology remains the first choice. When its estimated lane depth is
    larger than the fixed page, tighten only spatial chrome first and add a readable
    lane only when that compact topology still cannot fit. After adding a lane,
    restore the reference's original spacing and masthead whenever the expanded
    topology can hold them.
    """

    modules = [
        dict(item)
        for item in content_budget.get("content_modules") or []
        if isinstance(item, dict)
    ]
    if not modules:
        return design
    try:
        width_mm = float(page_plan["width_mm"])
        height_mm = float(page_plan["height_mm"])
    except (KeyError, TypeError, ValueError):
        return design
    if width_mm <= 0 or height_mm <= 0:
        return design

    layout = design.layout
    lead_module = _select_lead_module(modules, role=layout.lead_role)
    flow_modules = [
        module
        for module in modules
        if not (
            module is lead_module and layout.lead_placement == "full-width"
        )
    ]
    columns, weights = _columns_for_layout(
        flow_modules,
        layout=layout,
        lead_module=lead_module,
        width_mm=width_mm,
        height_mm=height_mm,
    )
    lead_demoted = bool(
        layout.lead_placement == "full-width"
        and lead_module is not None
        and not _full_width_lead_fits(
            lead_module,
            columns,
            width_mm=width_mm,
            height_mm=height_mm,
            spacing=layout.spacing,
            masthead_scale=layout.masthead_scale,
            figure_emphasis=layout.figure_emphasis,
            column_weights=weights,
        )
    )
    if lead_demoted:
        layout = replace(layout, lead_placement="flow")
        lead_module = None
        flow_modules = modules
        columns, weights = _columns_for_layout(
            flow_modules,
            layout=layout,
            lead_module=None,
            width_mm=width_mm,
            height_mm=height_mm,
        )
    available_depth = max(
        160.0,
        height_mm - _reserved_page_depth_mm(layout.masthead_scale),
    )
    estimated_depth = _maximum_estimated_column_depth_mm(
        columns,
        width_mm=width_mm,
        figure_emphasis=layout.figure_emphasis,
        column_weights=weights,
    )
    overfull = (
        _spacing_depth_factor(layout.spacing) * estimated_depth
        > available_depth * _REFERENCE_LAYOUT_TOLERANCE
    )
    if not overfull:
        if not lead_demoted:
            return design
        note = (
            "Reference full-width lead was returned to normal flow because it "
            "would starve the remaining body lanes."
        )
        warning = " ".join(item for item in (design.warning, note) if item)
        return replace(design, layout=layout, warning=warning)

    compact_layout = replace(
        layout,
        spacing="compact",
        masthead_scale="compact",
    )
    compact_columns, compact_weights = _columns_for_layout(
        flow_modules,
        layout=compact_layout,
        lead_module=lead_module,
        width_mm=width_mm,
        height_mm=height_mm,
    )
    compact_available_depth = max(
        160.0,
        height_mm - _reserved_page_depth_mm(compact_layout.masthead_scale),
    )
    compact_depth = _maximum_estimated_column_depth_mm(
        compact_columns,
        width_mm=width_mm,
        figure_emphasis=compact_layout.figure_emphasis,
        column_weights=compact_weights,
    )
    compact_overfull = (
        _spacing_depth_factor(compact_layout.spacing) * compact_depth
        > compact_available_depth * _PACKING_ESTIMATE_TOLERANCE
    )
    if compact_overfull:
        reading_floor_depth = _maximum_estimated_column_depth_mm(
            compact_columns,
            width_mm=width_mm,
            figure_emphasis=compact_layout.figure_emphasis,
            column_weights=compact_weights,
            use_body_reading_floor=True,
        )
        compact_overfull = (
            _spacing_depth_factor(compact_layout.spacing) * reading_floor_depth
            > compact_available_depth * _PACKING_ESTIMATE_TOLERANCE
        )
    maximum_columns = int(
        planning.layout_capacity(width_mm)["maximum_readable_column_count"]
    )
    if compact_overfull and len(compact_columns) < maximum_columns:
        compact_columns = _poster_module_columns(
            flow_modules,
            width_mm=width_mm,
            height_mm=height_mm,
            minimum_column_count=len(compact_columns) + 1,
            depth_factor=_spacing_depth_factor(compact_layout.spacing),
            masthead_scale=compact_layout.masthead_scale,
            figure_emphasis=compact_layout.figure_emphasis,
        )
        compact_weights = _expanded_column_weights(
            tuple(compact_weights),
            column_count=len(compact_columns),
            width_mm=width_mm,
        )
        expanded_layout = replace(layout, column_weights=compact_weights)
        expanded_columns, expanded_weights = _columns_for_layout(
            flow_modules,
            layout=expanded_layout,
            lead_module=lead_module,
            width_mm=width_mm,
            height_mm=height_mm,
        )
        expanded_available_depth = max(
            160.0,
            height_mm - _reserved_page_depth_mm(expanded_layout.masthead_scale),
        )
        expanded_depth = _maximum_estimated_column_depth_mm(
            expanded_columns,
            width_mm=width_mm,
            figure_emphasis=expanded_layout.figure_emphasis,
            column_weights=expanded_weights,
        )
        if (
            _spacing_depth_factor(expanded_layout.spacing) * expanded_depth
            <= expanded_available_depth * _REFERENCE_LAYOUT_TOLERANCE
        ):
            compact_layout = expanded_layout
            compact_weights = expanded_weights
    compact_layout = replace(
        compact_layout,
        column_weights=tuple(compact_weights),
    )
    note = (
        "Reference topology was adapted within the physical page because its original "
        "lanes could not contain the grounded modules at readable scale."
    )
    warning = " ".join(item for item in (design.warning, note) if item)
    return replace(design, layout=compact_layout, warning=warning)


def _expanded_column_weights(
    weights: tuple[float, ...],
    *,
    column_count: int,
    width_mm: float,
) -> tuple[float, ...]:
    """Add readable lanes without erasing reference-measured proportions."""

    values = [float(value) for value in weights if float(value) > 0]
    if not values:
        return (1.0,) * column_count
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    median = (
        ordered[midpoint]
        if len(ordered) % 2
        else (ordered[midpoint - 1] + ordered[midpoint]) / 2.0
    )
    while len(values) < column_count:
        values.append(median)
    candidate = tuple(values[:column_count])
    minimum_width = float(
        planning.layout_capacity(width_mm)["minimum_column_width_mm"]
    )
    if min(_column_widths_mm(width_mm, candidate), default=minimum_width) < minimum_width:
        return (1.0,) * column_count
    return candidate


def render_draft_html(
    *,
    assets: list[dict[str, Any]],
    content_budget: dict[str, Any],
    page_plan: dict[str, Any],
    paper_source: dict[str, Any] | None = None,
    visual_design_plan: visual_design.VisualDesignPlan | None = None,
) -> str:
    """Render one stable poster document from normalized, grounded inputs.

    The text model chooses scientific content upstream. The draft pipeline binds
    the physically constrained visual plan before this renderer executes it; the
    renderer does not reinterpret layout capacity or own scientific selection.
    """

    design = _require_visual_design(visual_design_plan)
    width = float(page_plan["width_mm"])
    height = float(page_plan["height_mm"])
    modules = [
        dict(item)
        for item in content_budget.get("content_modules") or []
        if isinstance(item, dict)
    ]
    sections = {
        str(item.get("id") or ""): str(item.get("label") or "")
        for item in content_budget.get("sections") or []
        if isinstance(item, dict)
    }
    asset_by_hash = {
        str(item.get("content_sha256") or ""): item
        for item in assets
        if str(item.get("content_sha256") or "")
    }
    layout = design.layout
    effective_lead_placement = layout.lead_placement
    lead_module = _select_lead_module(modules, role=layout.lead_role)
    if layout.lead_placement == "flow":
        lead_module = None
    flow_modules = [
        module
        for module in modules
        if not (module is lead_module and layout.lead_placement == "full-width")
    ]
    module_columns, column_weights = _columns_for_layout(
        flow_modules,
        layout=layout,
        lead_module=lead_module,
        width_mm=width,
        height_mm=height,
    )
    if (
        effective_lead_placement == "full-width"
        and lead_module is not None
        and not _full_width_lead_fits(
            lead_module,
            module_columns,
            width_mm=width,
            height_mm=height,
            spacing=layout.spacing,
            masthead_scale=layout.masthead_scale,
            figure_emphasis=layout.figure_emphasis,
            column_weights=column_weights,
        )
    ):
        effective_lead_placement = "flow"
        lead_module = None
        flow_modules = modules
        module_columns, column_weights = _columns_for_layout(
            flow_modules,
            layout=layout,
            lead_module=None,
            width_mm=width,
            height_mm=height,
        )
    column_count = len(module_columns)
    metrics = _content_typography_metrics(
        width,
        modules,
        column_count=column_count,
    )
    column_weights = _effective_column_weights(
        column_weights,
        module_count=column_count,
        width_mm=width,
    )
    column_widths = _column_widths_mm(width, column_weights)
    minimum_figure_width_mm = float(
        planning.layout_capacity(width)["minimum_column_width_mm"]
    )
    palette = design.palette
    seen_section_ids: set[str] = set()

    def render_column(
        column: list[dict[str, Any]],
        *,
        column_width_mm: float,
        lane_index: int,
        stage: bool = False,
    ) -> str:
        rendered: list[str] = []
        for section_id, raw_group in groupby(
            column,
            key=lambda module: str(module.get("section_id") or ""),
        ):
            group = list(raw_group)
            section_label = sections.get(section_id, "")
            show_section_label = bool(
                section_label and section_id not in seen_section_ids
            )
            section_heading = (
                f'<header class="section-group-header" data-section-id="'
                f'{escape(section_id, quote=True)}"><p class="section-label">'
                f"{escape(section_label)}</p></header>"
                if show_section_label
                else ""
            )
            group_modules = "".join(
                _render_module(
                    module,
                    section_label=section_label,
                    show_section_label=False,
                    asset_by_hash=asset_by_hash,
                    focal_role=str(content_budget.get("focal_role") or ""),
                    available_width_mm=_module_inner_width_mm(
                        module,
                        column_width_mm=column_width_mm,
                    ),
                    minimum_figure_width_mm=minimum_figure_width_mm,
                    equation_font_size_mm=metrics["body_target_mm"],
                )
                for module in group
            )
            rendered.append(
                f'<div class="poster-section-group" data-poster-section="'
                f'{escape(section_id, quote=True)}">{section_heading}{group_modules}</div>'
            )
            seen_section_ids.add(section_id)
        class_name = "poster-column poster-column-stage" if stage else "poster-column"
        return (
            f'<div class="{class_name}" data-poster-lane="{lane_index}">'
            + "".join(rendered)
            + "</div>"
        )

    lead_markup = ""
    if lead_module is not None and effective_lead_placement == "full-width":
        section_id = str(lead_module.get("section_id") or "")
        lead_markup = _render_module(
            lead_module,
            section_label=sections.get(section_id, ""),
            show_section_label=section_id not in seen_section_ids,
            asset_by_hash=asset_by_hash,
            focal_role=str(content_budget.get("focal_role") or ""),
            layout_class="poster-lead",
            available_width_mm=_module_inner_width_mm(
                lead_module,
                column_width_mm=max(80.0, width - 44.0),
            ),
            minimum_figure_width_mm=minimum_figure_width_mm,
            equation_font_size_mm=metrics["body_target_mm"],
        )
        seen_section_ids.add(section_id)
    rendered_columns = [
        render_column(
            column,
            column_width_mm=column_widths[index],
            lane_index=index + 1,
            stage=(
                effective_lead_placement == "center-lane"
                and lead_module is not None
                and index == len(module_columns) // 2
            ),
        )
        for index, column in enumerate(module_columns)
    ]
    column_markup = "".join(rendered_columns)
    module_markup = lead_markup + f'<div class="poster-columns">{column_markup}</div>'
    identity = paper_source or {}
    title = escape(str(identity.get("title") or "Scientific poster"))
    authors = str(identity.get("authors") or "").strip()
    author_markup = (
        f'<p class="poster-authors" data-poster-authors="verified">{escape(authors)}</p>'
        if authors
        else ""
    )
    venue_markup = _render_venue_identity(identity)
    css = _deterministic_stylesheet(
        width_mm=width,
        height_mm=height,
        column_count=column_count,
        palette=palette,
        metrics=metrics,
        typography=design.typography,
        spacing=layout.spacing,
        masthead_scale=layout.masthead_scale,
        figure_emphasis=layout.figure_emphasis,
        column_weights=column_weights,
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title><style>{css}</style></head><body>"
        f'<main class="poster-root spacing-{layout.spacing} section-style-{layout.section_style} '
        f"module-style-{layout.module_style} lead-placement-{effective_lead_placement} "
        f"masthead-{layout.masthead_scale} masthead-align-{layout.masthead_alignment} "
        f'figure-emphasis-{layout.figure_emphasis}" '
        f'data-poster-root data-poster-id="poster" '
        f'data-layout-columns="{len(column_weights)}">'
        '<header class="title-band" data-poster-title-band>'
        '<div class="title-copy">'
        f'<h1 data-poster-title="verified">{title}</h1>{author_markup}'
        f"</div>{venue_markup}</header>"
        f'<div class="poster-body" data-column-count="{column_count}">{module_markup}</div>'
        "</main></body></html>"
    )


def _poster_module_columns(
    modules: list[dict[str, Any]],
    *,
    width_mm: float,
    height_mm: float,
    minimum_column_count: int | None = None,
    depth_factor: float = 1.0,
    masthead_scale: str = "balanced",
    figure_emphasis: str = "balanced",
) -> list[list[dict[str, Any]]]:
    """Use the fewest readable columns whose estimated depth fits the page."""

    module_count = len(modules)
    if module_count <= 1:
        return [list(modules)]
    maximum = int(planning.layout_capacity(width_mm)["maximum_readable_column_count"])
    minimum = minimum_column_count or 2
    minimum = max(1, min(module_count, maximum, minimum))
    # The fixed renderer spends about 92 mm on root padding, the one-line title
    # band, and the body gap. Keep a modest wrap buffer without discarding enough
    # usable depth to force a readable near-fit poster into an extra column.
    available_depth = max(160.0, height_mm - _reserved_page_depth_mm(masthead_scale))
    fallback: list[list[dict[str, Any]]] | None = None
    for count in range(minimum, min(module_count, maximum) + 1):
        columns = _balanced_module_columns(
            modules,
            column_count=count,
            width_mm=width_mm,
            figure_emphasis=figure_emphasis,
        )
        fallback = columns
        if (
            depth_factor
            * _maximum_estimated_column_depth_mm(
                columns,
                width_mm=width_mm,
                figure_emphasis=figure_emphasis,
            )
            <= available_depth * _PACKING_ESTIMATE_TOLERANCE
        ):
            return columns
    return fallback or [list(modules)]


def _centered_lead_columns(
    modules: list[dict[str, Any]],
    *,
    lead_module: dict[str, Any],
    column_weights: tuple[float, ...],
    width_mm: float,
    figure_emphasis: str = "balanced",
) -> list[list[dict[str, Any]]]:
    """Reserve the middle-stage lead, then balance the remaining scan modules."""

    column_count = len(column_weights)
    if len(modules) <= 2 or column_count < 3 or lead_module not in modules:
        return [list(modules)]
    center = column_count // 2
    widths = _column_widths_mm(width_mm, column_weights)
    metrics = _content_typography_metrics(
        width_mm,
        modules,
        column_count=column_count,
    )
    minimum_figure_width_mm = float(
        planning.layout_capacity(width_mm)["minimum_column_width_mm"]
    )

    def module_depth(module: dict[str, Any], column_index: int) -> float:
        return _estimated_module_height_mm(
            module,
            inner_width_mm=_module_inner_width_mm(
                module,
                column_width_mm=widths[column_index],
            ),
            metrics=metrics,
            column_count=column_count,
            figure_emphasis=figure_emphasis,
            minimum_figure_width_mm=minimum_figure_width_mm,
        )

    columns: list[list[dict[str, Any]]] = [[] for _ in range(column_count)]
    columns[center].append(lead_module)
    loads = [0.0] * column_count
    loads[center] = module_depth(lead_module, center)
    assigned_column = {id(lead_module): center}
    remaining = sorted(
        (module for module in modules if module is not lead_module),
        key=lambda module: max(
            module_depth(module, column_index)
            for column_index in range(column_count)
        ),
        reverse=True,
    )
    for module in remaining:
        choices: list[tuple[float, float, float, int, float]] = []
        for column_index in range(column_count):
            addition = module_depth(module, column_index)
            if columns[column_index]:
                addition += 8.0
            candidate_loads = list(loads)
            candidate_loads[column_index] += addition
            choices.append(
                (
                    max(candidate_loads) - min(candidate_loads),
                    max(candidate_loads),
                    candidate_loads[column_index],
                    column_index,
                    addition,
                )
            )
        *_score, column_index, addition = min(choices)
        columns[column_index].append(module)
        loads[column_index] += addition
        assigned_column[id(module)] = column_index
    columns = [
        [
            module
            for module in modules
            if module is not lead_module
            and assigned_column[id(module)] == column_index
        ]
        for column_index in range(column_count)
    ]
    columns[center].insert(0, lead_module)
    return columns


def _select_lead_module(
    modules: list[dict[str, Any]],
    *,
    role: visual_design.LeadRole,
) -> dict[str, Any] | None:
    """Resolve a generic lead role against grounded module metadata."""

    if not modules or role == "none":
        return None
    if role == "method":
        candidates = [
            module
            for module in modules
            if str(module.get("visual_kind") or "") == "method-flow"
            or "method" in {str(item) for item in module.get("semantic_roles") or []}
        ]
        if candidates:
            return candidates[0]
    return next(
        (module for module in modules if str(module.get("priority") or "") == "focal"),
        modules[0],
    )


def _effective_column_weights(
    weights: tuple[float, ...] | list[float],
    *,
    module_count: int,
    width_mm: float,
) -> tuple[float, ...]:
    """Clamp reference-extracted lanes to actual modules and page readability."""

    if not weights:
        return ()
    maximum = int(planning.layout_capacity(width_mm)["maximum_readable_column_count"])
    count = max(1, min(len(weights), module_count or 1, maximum))
    return tuple(float(weight) for weight in weights[:count])


def _columns_for_layout(
    modules: list[dict[str, Any]],
    *,
    layout: visual_design.LayoutGrammar,
    lead_module: dict[str, Any] | None,
    width_mm: float,
    height_mm: float,
) -> tuple[list[list[dict[str, Any]]], tuple[float, ...]]:
    """Apply one executable reference grammar within the physical page envelope."""

    column_weights = _effective_column_weights(
        layout.column_weights,
        module_count=len(modules),
        width_mm=width_mm,
    )
    if not column_weights:
        columns = _poster_module_columns(
            modules,
            width_mm=width_mm,
            height_mm=height_mm,
            depth_factor=_spacing_depth_factor(layout.spacing),
            masthead_scale=layout.masthead_scale,
            figure_emphasis=layout.figure_emphasis,
        )
        return columns, (1.0,) * len(columns)
    if (
        layout.lead_placement == "center-lane"
        and lead_module is not None
        and len(column_weights) >= 3
    ):
        columns = _centered_lead_columns(
            modules,
            lead_module=lead_module,
            column_weights=column_weights,
            width_mm=width_mm,
            figure_emphasis=layout.figure_emphasis,
        )
    else:
        columns = _balanced_module_columns(
            modules,
            column_count=len(column_weights),
            width_mm=width_mm,
            figure_emphasis=layout.figure_emphasis,
            column_weights=column_weights,
        )
    return columns, column_weights


def _spacing_depth_factor(spacing: str) -> float:
    """Return the estimator correction paired with executable spacing CSS."""

    return _SPACING_DEPTH_FACTORS.get(spacing, 1.0)


def _reserved_page_depth_mm(masthead_scale: str) -> float:
    """Reserve the same broad masthead proportions that the stylesheet renders."""

    return {
        "compact": 94.0,
        "balanced": 105.0,
        "prominent": 120.0,
    }.get(masthead_scale, 105.0)


def _full_width_lead_fits(
    lead_module: dict[str, Any],
    flow_columns: list[list[dict[str, Any]]],
    *,
    width_mm: float,
    height_mm: float,
    spacing: str,
    masthead_scale: str = "balanced",
    figure_emphasis: str = "balanced",
    column_weights: tuple[float, ...] = (),
) -> bool:
    """Keep a broad lead only when it leaves readable depth for the body lanes."""

    lead_width = max(80.0, width_mm - 44.0)
    metrics = _content_typography_metrics(
        width_mm,
        [lead_module, *(module for column in flow_columns for module in column)],
        column_count=max(1, len(flow_columns)),
    )
    minimum_figure_width_mm = float(
        planning.layout_capacity(width_mm)["minimum_column_width_mm"]
    )
    structured_text_lead = _uses_structured_text_lead(
        lead_module,
        details=[
            str(item).strip()
            for item in lead_module.get("detail_points") or []
            if str(item).strip()
        ],
        has_figures=bool(lead_module.get("figure_sha256s") or []),
        has_equations=bool(lead_module.get("equations") or []),
    )
    if (
        (lead_module.get("figure_sha256s") or [])
        or (lead_module.get("equations") or [])
        or structured_text_lead
    ):
        copy_width, evidence_width = _full_width_lead_split_widths(
            lead_width,
            structured_text=structured_text_lead,
        )
        copy_module = {
            **lead_module,
            "figure_sha256s": [],
            "equations": [],
            "detail_points": (
                [] if structured_text_lead else lead_module.get("detail_points", [])
            ),
        }
        evidence_module = {
            **lead_module,
            "text": "",
            "takeaway": "",
            "detail_points": (
                lead_module.get("detail_points", []) if structured_text_lead else []
            ),
            "figure_sha256s": (
                [] if structured_text_lead else lead_module.get("figure_sha256s", [])
            ),
            "equations": (
                [] if structured_text_lead else lead_module.get("equations", [])
            ),
        }
        copy_depth = _estimated_module_height_mm(
            copy_module,
            inner_width_mm=copy_width,
            metrics=metrics,
            column_count=2,
            figure_emphasis=figure_emphasis,
            minimum_figure_width_mm=minimum_figure_width_mm,
        )
        evidence_depth = _estimated_module_height_mm(
            evidence_module,
            inner_width_mm=evidence_width,
            metrics=metrics,
            column_count=2,
            figure_emphasis=figure_emphasis,
            minimum_figure_width_mm=minimum_figure_width_mm,
        ) - _estimated_module_header_height_mm(
            evidence_module,
            inner_width_mm=evidence_width,
            metrics=metrics,
        )
        lead_depth = max(copy_depth, evidence_depth)
    else:
        lead_depth = _estimated_module_height_mm(
            lead_module,
            inner_width_mm=_module_inner_width_mm(
                lead_module,
                column_width_mm=lead_width,
            ),
            metrics=metrics,
            column_count=2,
            figure_emphasis=figure_emphasis,
            minimum_figure_width_mm=minimum_figure_width_mm,
        )
    flow_depth = _maximum_estimated_column_depth_mm(
        flow_columns,
        width_mm=width_mm,
        figure_emphasis=figure_emphasis,
        column_weights=column_weights,
    )
    available_depth = max(160.0, height_mm - _reserved_page_depth_mm(masthead_scale))
    required_depth = _spacing_depth_factor(spacing) * (lead_depth + 8.0 + flow_depth)
    return required_depth <= available_depth * _PACKING_ESTIMATE_TOLERANCE


def _full_width_lead_split_widths(
    lead_width_mm: float,
    *,
    structured_text: bool = False,
) -> tuple[float, float]:
    """Return the copy/evidence widths emitted by the full-width lead grid."""

    content_width = max(80.0, float(lead_width_mm) - 16.0 - 14.0)
    if structured_text:
        return content_width * 0.425, content_width * 0.575
    return content_width * 0.55, content_width * 0.45


def _maximum_estimated_column_depth_mm(
    columns: list[list[dict[str, Any]]],
    *,
    width_mm: float,
    figure_emphasis: str = "balanced",
    column_weights: tuple[float, ...] = (),
    use_body_reading_floor: bool = False,
) -> float:
    return max(
        _estimated_column_loads_mm(
            columns,
            width_mm=width_mm,
            figure_emphasis=figure_emphasis,
            column_weights=column_weights,
            use_body_reading_floor=use_body_reading_floor,
        ),
        default=0.0,
    )


def _estimated_column_loads_mm(
    columns: list[list[dict[str, Any]]],
    *,
    width_mm: float,
    figure_emphasis: str = "balanced",
    column_weights: tuple[float, ...] = (),
    use_body_reading_floor: bool = False,
) -> tuple[float, ...]:
    """Estimate the vertical demand of each physical reading lane."""

    count = max(1, len(columns))
    widths = _column_widths_mm(
        width_mm,
        column_weights or (1.0,) * count,
    )
    metrics = _content_typography_metrics(
        width_mm,
        [module for column in columns for module in column],
        column_count=count,
    )
    if use_body_reading_floor:
        metrics = {
            **metrics,
            "body_target_mm": metrics["body_min_mm"],
        }
    minimum_figure_width_mm = float(
        planning.layout_capacity(width_mm)["minimum_column_width_mm"]
    )
    return tuple(
            sum(
                _estimated_module_height_mm(
                    module,
                    inner_width_mm=_module_inner_width_mm(
                        module,
                        column_width_mm=widths[index],
                    ),
                    metrics=metrics,
                    column_count=count,
                    figure_emphasis=figure_emphasis,
                    minimum_figure_width_mm=minimum_figure_width_mm,
                )
                for module in column
            )
            + max(0, len(column) - 1) * 8.0
            for index, column in enumerate(columns)
    )


def _balanced_module_columns(
    modules: list[dict[str, Any]],
    *,
    column_count: int,
    width_mm: float,
    figure_emphasis: str = "balanced",
    column_weights: tuple[float, ...] = (),
) -> list[list[dict[str, Any]]]:
    """Partition ordered modules into balanced contiguous physical columns.

    CSS multi-column flow creates extra off-page columns when atomic cards exceed the
    available height. Explicit, contiguous columns make horizontal page bounds and the
    planned reading order invariant. The depth estimate never removes or rewrites
    scientific content.
    """

    count = max(1, min(column_count, len(modules) or 1))
    weights = (
        tuple(float(weight) for weight in column_weights)
        if len(column_weights) == count and all(weight > 0 for weight in column_weights)
        else (1.0,) * count
    )
    column_widths = _column_widths_mm(width_mm, weights)
    metrics = _content_typography_metrics(
        width_mm,
        modules,
        column_count=count,
    )
    minimum_figure_width_mm = float(
        planning.layout_capacity(width_mm)["minimum_column_width_mm"]
    )
    depths_by_column = [
        [
            _estimated_module_height_mm(
                module,
                inner_width_mm=_module_inner_width_mm(
                    module,
                    column_width_mm=column_width,
                ),
                metrics=metrics,
                column_count=count,
                figure_emphasis=figure_emphasis,
                minimum_figure_width_mm=minimum_figure_width_mm,
            )
            for module in modules
        ]
        for column_width in column_widths
    ]
    best_score: tuple[float, float, tuple[int, ...]] | None = None
    best_bounds: tuple[int, ...] | None = None
    for cuts in combinations(range(1, len(modules)), count - 1):
        bounds = (0, *cuts, len(modules))
        loads = [
            sum(depths_by_column[index][start:end])
            + max(0, end - start - 1) * 8.0
            for index, (start, end) in enumerate(pairwise(bounds))
        ]
        score = (
            max(loads),
            max(loads) - min(loads),
            cuts,
        )
        if best_score is None or score < best_score:
            best_score = score
            best_bounds = bounds
    if best_bounds is None:
        return [list(modules)]
    return [list(modules[start:end]) for start, end in pairwise(best_bounds)]


def _column_widths_mm(
    width_mm: float,
    column_weights: tuple[float, ...],
) -> tuple[float, ...]:
    """Resolve the same weighted body tracks emitted by the stylesheet."""

    count = max(1, len(column_weights))
    available = max(1.0, width_mm - 44.0 - 14.0 * (count - 1))
    total = sum(column_weights) or float(count)
    return tuple(available * weight / total for weight in column_weights)


def _module_inner_width_mm(
    module: dict[str, Any],
    *,
    column_width_mm: float,
) -> float:
    """Return the content width produced by the renderer's module padding."""

    horizontal_padding_mm = 12.0 if module.get("priority") == "focal" else 6.0
    return max(80.0, column_width_mm - horizontal_padding_mm)


def _figure_grid_column_count(
    figure_count: int,
    *,
    available_width_mm: float,
    minimum_figure_width_mm: float,
) -> int:
    """Use parallel tracks only when every source figure remains readable."""

    if figure_count <= 1:
        return 1
    gap_mm = 6.0
    maximum_by_width = math.floor(
        (available_width_mm + gap_mm) / (minimum_figure_width_mm + gap_mm)
    )
    return max(1, min(2, figure_count, maximum_by_width))


def _uses_detail_grid(module: dict[str, Any]) -> bool:
    """Use two columns only for metrics or genuinely compact evidence atoms."""

    visual_kind = str(module.get("visual_kind") or "")
    if visual_kind in {"method-flow", "table"}:
        return False
    details = [
        str(item).strip()
        for item in module.get("detail_points") or []
        if str(item).strip()
    ]
    if visual_kind == "metrics":
        return True
    maximum = 120 if visual_kind == "figure" else 90
    return len(details) >= 4 and max(
        (planning.display_width_units(item) for item in details),
        default=0,
    ) <= maximum


def _estimated_module_header_height_mm(
    module: dict[str, Any],
    *,
    inner_width_mm: float,
    metrics: dict[str, float],
) -> float:
    """Estimate the shared module header and surrounding vertical chrome."""

    heading_size = metrics["section_heading_min_mm"]
    heading_chars = max(
        8.0,
        inner_width_mm / (heading_size * _AVERAGE_BODY_GLYPH_EM),
    )
    title_lines = _estimated_wrapped_line_count(
        str(module.get("title") or "Evidence"),
        maximum_characters=heading_chars,
    )
    return 15.0 + 5.0 + 6.0 + title_lines * heading_size * 1.08


def _estimated_module_height_mm(
    module: dict[str, Any],
    *,
    inner_width_mm: float,
    metrics: dict[str, float],
    column_count: int,
    single_figure_height_mm: float | None = None,
    figure_emphasis: str = "balanced",
    minimum_figure_width_mm: float = 220.0,
) -> float:
    """Approximate the physical depth of the renderer's visible channels."""

    visual_kind = str(module.get("visual_kind") or "text")
    body_size = (
        metrics["body_min_mm"]
        if visual_kind == "table" and column_count >= 4
        else metrics["body_target_mm"]
    )
    body_chars = max(
        12.0,
        inner_width_mm / (body_size * _AVERAGE_BODY_GLYPH_EM),
    )
    height = _estimated_module_header_height_mm(
        module,
        inner_width_mm=inner_width_mm,
        metrics=metrics,
    )

    text, details, takeaway = _module_copy_channels(module)
    figures = [str(item) for item in module.get("figure_sha256s") or [] if str(item)]
    if text:
        height += (
            4.0
            + _estimated_wrapped_line_count(
                text,
                maximum_characters=body_chars,
            )
            * body_size
            * 1.2
        )
    if details:
        if _uses_detail_grid(module):
            cell_width = (inner_width_mm - 3.0) / 2.0 - 6.8
            detail_chars = max(
                8.0,
                cell_width / (body_size * _AVERAGE_BODY_GLYPH_EM),
            )
            item_heights = []
            for item in details:
                item_height = (
                    _estimated_wrapped_line_count(
                        item,
                        maximum_characters=detail_chars,
                    )
                    * body_size
                    * 1.2
                    + 4.0
                )
                if visual_kind == "metrics":
                    item_height = max(item_height, 29.0)
                item_heights.append(item_height)
            row_heights = [
                max(item_heights[index : index + 2])
                for index in range(0, len(item_heights), 2)
            ]
            height += 4.0 + sum(row_heights) + max(0, len(row_heights) - 1) * 3.0
        else:
            detail_chars = max(10.0, body_chars * 0.85)
            item_padding = {
                "comparison": 6.0,
                "method-flow": 6.0,
                "table": 5.0,
                "text": 5.0,
            }.get(visual_kind, 2.0)
            item_gap = {
                "comparison": 3.0,
                "method-flow": 3.5,
            }.get(visual_kind, 0.0)
            line_counts = [
                (
                    _estimated_table_row_line_count(
                        item,
                        maximum_characters=body_chars,
                        stack_label=column_count >= 4,
                    )
                    if visual_kind == "table"
                    else _estimated_detail_line_count(
                        item,
                        visual_kind=visual_kind,
                        maximum_characters=detail_chars,
                    )
                )
                for item in details
            ]
            height += 4.0 + sum(
                lines * body_size * 1.2 + item_padding for lines in line_counts
            )
            height += max(0, len(details) - 1) * item_gap
    if takeaway:
        height += (
            6.0
            + _estimated_wrapped_line_count(
                takeaway,
                maximum_characters=body_chars * 0.9,
            )
            * body_size
            * 1.16
        )

    if figures:
        single_limit, paired_limit, grid_limit = _figure_height_limits(
            column_count,
            figure_emphasis=figure_emphasis,
        )
        grid_columns = _figure_grid_column_count(
            len(figures),
            available_width_mm=inner_width_mm,
            minimum_figure_width_mm=minimum_figure_width_mm,
        )
        figure_width = (inner_width_mm - 6.0 * (grid_columns - 1)) / grid_columns
        try:
            ratio = max(0.4, min(4.0, float(module.get("figure_aspect_ratio") or 1.0)))
        except (TypeError, ValueError):
            ratio = 1.0
        rows = math.ceil(len(figures) / grid_columns)
        maximum = (
            single_figure_height_mm or single_limit
            if len(figures) == 1
            else paired_limit
            if len(figures) == 2
            else grid_limit
        )
        caption_depth = max(17.0, metrics["provenance_min_mm"] * 2.65)
        height += 5.0 + rows * (min(figure_width / ratio, maximum) + caption_depth)
        height += max(0, rows - 1) * 6.0
    equation_depth_mm = (
        15.0 if str(module.get("visual_kind") or "") == "method-flow" else 24.0
    )
    height += sum(
        _equation_row_count(
            str(equation.get("latex") or ""),
            available_width_mm=inner_width_mm,
            equation_font_size_mm=body_size,
        )
        for equation in module.get("equations") or []
        if isinstance(equation, dict)
    ) * equation_depth_mm
    return height


def _estimated_detail_line_count(
    value: str,
    *,
    visual_kind: str,
    maximum_characters: float,
) -> int:
    """Mirror the label/value line break used by structured evidence cards."""

    if visual_kind == "table":
        return _estimated_wrapped_line_count(
            value,
            maximum_characters=maximum_characters,
        )
    label, remainder = _split_labeled_atom(value)
    if not label:
        return _estimated_wrapped_line_count(
            value,
            maximum_characters=maximum_characters,
        )
    return _estimated_wrapped_line_count(
        label,
        maximum_characters=maximum_characters,
    ) + _estimated_wrapped_line_count(
        remainder,
        maximum_characters=maximum_characters,
    )


def _estimated_table_row_line_count(
    value: str,
    *,
    maximum_characters: float,
    stack_label: bool = False,
) -> int:
    """Estimate the tallest cell using the same row shape emitted by HTML."""

    label, cells = _evidence_row_parts(value)
    if not label:
        return _estimated_wrapped_line_count(
            value,
            maximum_characters=max(8.0, maximum_characters * 0.9),
        )
    single_value = len(cells) == 1
    if stack_label and not single_value:
        label_lines = _estimated_wrapped_line_count(
            label,
            maximum_characters=max(8.0, maximum_characters * 0.86),
        )
        cell_lines = max(
            _estimated_wrapped_line_count(
                cell,
                maximum_characters=max(8.0, maximum_characters * 0.41 * 0.86),
            )
            for cell in cells
        )
        return label_lines + cell_lines
    label_fraction = 0.28 if single_value else 0.17
    cell_fraction = 0.72 if single_value else 0.41
    line_counts = [
        _estimated_wrapped_line_count(
            label,
            maximum_characters=max(6.0, maximum_characters * label_fraction * 0.86),
        )
    ]
    line_counts.extend(
        _estimated_wrapped_line_count(
            cell,
            maximum_characters=max(8.0, maximum_characters * cell_fraction * 0.86),
        )
        for cell in cells
    )
    return max(line_counts)


def _estimated_wrapped_line_count(
    text: str,
    *,
    maximum_characters: float,
) -> int:
    """Estimate narrow-column wrapping without assuming perfectly filled lines."""

    words = re.findall(r"\S+", text)
    if not words:
        return 0
    limit = max(1.0, float(maximum_characters))
    lines = 0
    used = 0.0
    for word in words:
        word_width = planning.display_width_units(word)
        if word_width > limit:
            if used > 0.0:
                lines += 1
                used = 0.0
            full_lines, word_width = divmod(word_width, limit)
            lines += int(full_lines)
            if word_width == 0.0:
                continue
        candidate = word_width if used == 0.0 else used + 1.0 + word_width
        if candidate <= limit:
            used = candidate
            continue
        lines += 1
        used = word_width
    return lines + int(used > 0.0)


def _render_module(
    module: dict[str, Any],
    *,
    section_label: str,
    asset_by_hash: dict[str, dict[str, Any]],
    focal_role: str,
    show_section_label: bool = True,
    layout_class: str = "",
    available_width_mm: float | None = None,
    minimum_figure_width_mm: float = 220.0,
    equation_font_size_mm: float = 11.0,
) -> str:
    module_id = str(module.get("id") or "")
    priority = str(module.get("priority") or "supporting")
    visual_kind = (
        re.sub(
            r"[^a-z0-9-]+",
            "-",
            str(module.get("visual_kind") or "text").strip().lower(),
        ).strip("-")
        or "text"
    )
    roles = " ".join(str(item) for item in module.get("semantic_roles") or [])
    attributes = {
        "id": module_id,
        "data-poster-module": module_id,
        "data-poster-id": module_id,
        "data-section-id": str(module.get("section_id") or ""),
        "data-semantic-roles": roles,
        "data-module-priority": priority,
        "data-visual-kind": visual_kind,
        "data-source-label": str(module.get("source_label") or ""),
    }
    if priority == "focal" and focal_role:
        attributes["data-focal-role"] = focal_role
    attribute_text = " ".join(
        f'{name}="{escape(value, quote=True)}"'
        for name, value in attributes.items()
        if value
    )
    title = str(module.get("title") or section_label or "Evidence")
    section_markup = (
        f'<p class="section-label">{escape(section_label)}</p>'
        if section_label and show_section_label
        else ""
    )
    kicker = (
        f'<div class="module-kicker">{section_markup}</div>'
        if section_markup
        else ""
    )
    header = f"{kicker}<h2>{escape(title)}</h2>"
    text, details, takeaway = _module_copy_channels(module)
    figure_hashes = [str(item) for item in module.get("figure_sha256s") or []]
    figures = "".join(
        _render_figure(
            asset_by_hash[digest],
            digest=digest,
        )
        for digest in figure_hashes
        if digest in asset_by_hash
    )
    figure_columns = _figure_grid_column_count(
        len(figure_hashes),
        available_width_mm=(
            available_width_mm if available_width_mm is not None else math.inf
        ),
        minimum_figure_width_mm=minimum_figure_width_mm,
    )
    figure_markup = (
        f'<div class="figure-grid figure-count-{len(figure_hashes)} '
        f'figure-columns-{figure_columns}">{figures}</div>'
        if figures
        else ""
    )
    text_markup = (
        f'<p class="module-text" data-content-role="text">{escape(text)}</p>'
        if text
        else ""
    )
    details_markup = _render_detail_points(details, visual_kind=visual_kind)
    equations = "".join(
        _render_equation(
            item,
            available_width_mm=available_width_mm,
            equation_font_size_mm=equation_font_size_mm,
        )
        for item in module.get("equations") or []
        if isinstance(item, dict) and str(item.get("latex") or "").strip()
    )
    takeaway_markup = (
        f'<p class="takeaway module-deck" data-content-role="takeaway">'
        f"{escape(takeaway)}</p>"
        if takeaway
        else ""
    )
    module_classes = [
        "poster-module",
    ]
    if layout_class:
        module_classes.append(layout_class)
    module_classes.extend(
        [
            f"priority-{priority}",
            f"kind-{visual_kind}",
        ]
    )
    if _uses_detail_grid(module):
        module_classes.append("detail-grid")
    if details and visual_kind == "text":
        module_classes.append("has-detail-points")
    structured_text_lead = bool(
        layout_class == "poster-lead"
        and _uses_structured_text_lead(
            module,
            details=details,
            has_figures=bool(figure_markup),
            has_equations=bool(equations),
        )
    )
    if structured_text_lead:
        module_classes.append("lead-text-visual")
    if visual_kind == "method-flow":
        body_markup = (
            f"{takeaway_markup}{figure_markup}{text_markup}{equations}{details_markup}"
        )
    else:
        body_markup = (
            f"{takeaway_markup}{figure_markup}{text_markup}{details_markup}{equations}"
        )
    if layout_class == "poster-lead" and (
        figure_markup or equations or structured_text_lead
    ):
        copy_markup = f"{takeaway_markup}{text_markup}"
        evidence_markup = (
            details_markup if structured_text_lead else f"{figure_markup}{equations}"
        )
        if not structured_text_lead:
            copy_markup += details_markup
        return (
            f'<section class="{escape(" ".join(module_classes))}" {attribute_text}>'
            f'<div class="module-lead-copy"><header class="module-header">'
            f"{header}</header>{copy_markup}</div>"
            f'<div class="module-lead-evidence">{evidence_markup}</div></section>'
        )
    return (
        f'<section class="{escape(" ".join(module_classes))}" {attribute_text}>'
        f'<header class="module-header">{header}</header>{body_markup}</section>'
    )


def _uses_structured_text_lead(
    module: dict[str, Any],
    *,
    details: list[str],
    has_figures: bool,
    has_equations: bool,
) -> bool:
    """Split a text-carried visual lead into explicit claim and evidence regions."""

    return bool(
        details
        and not has_figures
        and not has_equations
        and str(module.get("visual_kind") or "") in _STRUCTURED_TEXT_KINDS
    )


def _render_detail_points(details: list[str], *, visual_kind: str) -> str:
    """Render truthful evidence atoms using their declared presentation channel."""

    if not details:
        return ""
    if visual_kind == "table":
        rows = "".join(_render_evidence_row(item) for item in details)
        return (
            '<div class="evidence-points evidence-matrix" '
            'data-content-role="detail-points" role="table">'
            f"{rows}</div>"
        )
    list_tag = "ol" if visual_kind in _STRUCTURED_TEXT_KINDS else "ul"
    class_name = (
        "evidence-points evidence-cards"
        if visual_kind in _STRUCTURED_TEXT_KINDS
        else "evidence-points evidence-callouts"
    )
    items = "".join(f"<li>{_render_labeled_atom(item)}</li>" for item in details)
    return (
        f'<{list_tag} class="{class_name}" data-content-role="detail-points">'
        f"{items}</{list_tag}>"
    )


def _render_evidence_row(value: str) -> str:
    """Expose one repeated evidence row without changing or inferring its meaning."""

    label, cells = _evidence_row_parts(value)
    if not label:
        return (
            '<div class="evidence-row evidence-row-unlabeled" role="row">'
            f'<span class="evidence-cell" role="cell">{escape(value)}</span></div>'
        )
    shape_class = (
        "evidence-row-single-value" if len(cells) == 1 else "evidence-row-multi-value"
    )
    return (
        f'<div class="evidence-row {shape_class}" role="row">'
        f'<strong class="evidence-label" role="rowheader">{escape(label)}</strong>'
        + "".join(
            f'<span class="evidence-cell" role="cell">{escape(item)}</span>'
            for item in cells
        )
        + "</div>"
    )


def _evidence_row_parts(value: str) -> tuple[str, list[str]]:
    """Parse the explicit row delimiters shared by estimation and rendering."""

    explicit_cells = [item.strip() for item in value.split("|") if item.strip()]
    if len(explicit_cells) >= 2:
        label, cells = explicit_cells[0], explicit_cells[1:]
    else:
        label, remainder = _split_labeled_atom(value)
        cells = [
            item.strip()
            for item in re.split(r"[;；]", remainder)
            if item.strip()
        ] or [remainder]
    return label, cells


def _render_labeled_atom(value: str) -> str:
    """Add visual hierarchy to an explicit source label while preserving all copy."""

    label, remainder = _split_labeled_atom(value)
    if not label:
        return f'<span class="evidence-copy">{escape(value)}</span>'
    return (
        '<span class="evidence-copy">'
        f'<strong class="evidence-label">{escape(label)}</strong>'
        f'<span class="evidence-value">{escape(remainder)}</span></span>'
    )


def _split_labeled_atom(value: str) -> tuple[str, str]:
    """Split only an explicit leading label; punctuation remains presentation data."""

    match = re.match(r"^\s*([^:：\n]{1,48})\s*[:：]\s*(.+?)\s*$", value)
    if match is None:
        return "", value
    return match.group(1).strip(), match.group(2).strip()


def _module_copy_channels(
    module: dict[str, Any],
) -> tuple[str, list[str], str]:
    """Remove only byte-equivalent visible repetition across optional copy channels."""

    text = str(module.get("text") or "").strip()
    takeaway = str(module.get("takeaway") or "").strip()
    if text and takeaway and _copy_key(text) == _copy_key(takeaway):
        text = ""

    occupied = {_copy_key(value) for value in (text, takeaway) if value}
    details: list[str] = []
    for raw in module.get("detail_points") or []:
        value = str(raw).strip()
        key = _copy_key(value)
        if not value or key in occupied:
            continue
        details.append(value)
        occupied.add(key)
    return text, details, takeaway


def _copy_key(value: str) -> str:
    """Normalize presentation-only whitespace and terminal punctuation for exact dedupe."""

    return re.sub(r"[\s.!?;:]+$", "", " ".join(value.split())).casefold()


def _render_figure(
    asset: dict[str, Any],
    *,
    digest: str,
) -> str:
    description = _concise_asset_description(asset)
    caption = _visible_figure_caption(description)
    raw_number = asset.get("figure_number")
    figure_label = (
        f"Fig. {int(raw_number)}"
        if isinstance(raw_number, (int, float)) and int(raw_number) > 0
        else ""
    )
    visible_caption = " — ".join(item for item in (figure_label, caption) if item)
    return (
        '<figure class="source-figure">'
        f'<img src="{escape(str(asset.get("token") or ""), quote=True)}" '
        f'data-source-figure-sha256="{escape(digest, quote=True)}" '
        f'alt="{escape(description, quote=True)}">'
        + (
            f'<figcaption data-content-role="caption">'
            f"{escape(visible_caption)}</figcaption>"
            if visible_caption
            else ""
        )
        + "</figure>"
    )


def _visible_figure_caption(description: str) -> str:
    caption = description.split("paper discussion:", 1)[0].strip()
    caption_match = re.search(r"\bcaption\s*:\s*", caption, flags=re.IGNORECASE)
    if caption_match:
        caption = caption[caption_match.end() :].strip()
    caption = re.sub(
        r"^(?:figure|fig\.)\s*\d+\s+from\s+source\s+pdf\s*,?\s*"
        r"(?:page\s*\d+\s*[.:\-–—]?\s*)?",
        "",
        caption,
        flags=re.IGNORECASE,
    )
    caption = re.sub(
        r"^(?:figure|fig\.)\s*\d+\s*[:.\-–—]?\s*",
        "",
        caption,
        flags=re.IGNORECASE,
    )
    caption = " ".join(caption.split())
    sentences = re.split(r"(?<=[.!?])\s+", caption)
    brief = sentences[0] if sentences else caption
    if len(brief) > 210:
        brief = brief[:207].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return brief


def _render_equation(
    equation: dict[str, Any],
    *,
    available_width_mm: float | None = None,
    equation_font_size_mm: float = 11.0,
) -> str:
    latex = str(equation.get("latex") or "").strip()
    source_label = str(equation.get("source_label") or "").strip()
    label_markup = (
        f'<span class="equation-label">{escape(source_label)}</span>'
        if source_label
        else ""
    )
    mathml = latex_to_mathml(
        latex,
        attributes={
            "data-content-role": "equation",
            "data-latex": latex,
        },
    )
    segments = _equation_row_segments(
        latex,
        mathml=mathml,
        available_width_mm=available_width_mm,
        equation_font_size_mm=equation_font_size_mm,
    )
    if len(segments) > 1:
        mathml = _mathml_with_rows(mathml, segments)
    return (
        '<div class="equation-shell">'
        f"{label_markup}"
        f"{mathml}"
        "</div>"
    )


def _equation_row_count(
    latex: str,
    *,
    available_width_mm: float,
    equation_font_size_mm: float,
) -> int:
    """Estimate the same explicit MathML rows used by the renderer."""

    if not latex.strip():
        return 0
    mathml = latex_to_mathml(latex)
    return len(
        _equation_row_segments(
            latex,
            mathml=mathml,
            available_width_mm=available_width_mm,
            equation_font_size_mm=equation_font_size_mm,
        )
    )


def _equation_row_segments(
    latex: str,
    *,
    mathml: str,
    available_width_mm: float | None,
    equation_font_size_mm: float,
) -> list[str]:
    """Use explicit top-level spacing as a soft row break only when math is too wide."""

    if available_width_mm is None or available_width_mm <= 0:
        return [latex]
    visible_math = unescape(re.sub(r"<[^>]+>", "", mathml))
    character_capacity = max(
        8.0,
        available_width_mm
        / max(1.0, equation_font_size_mm * _AVERAGE_BODY_GLYPH_EM),
    )
    if planning.display_width_units(visible_math) <= character_capacity:
        return [latex]
    segments = _split_latex_rows(latex)
    return segments if len(segments) > 1 else [latex]


def _split_latex_rows(latex: str) -> list[str]:
    """Split only explicit top-level visual separators; preserve the bound source string."""

    separators = (r"\qquad", r"\quad", r"\\")
    segments: list[str] = []
    start = 0
    depth = 0
    index = 0
    while index < len(latex):
        character = latex[index]
        if character == "{":
            depth += 1
            index += 1
            continue
        if character == "}":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0:
            separator = next(
                (item for item in separators if latex.startswith(item, index)),
                None,
            )
            if separator is not None:
                segment = latex[start:index].strip()
                if segment:
                    segments.append(segment)
                index += len(separator)
                start = index
                continue
        index += 1
    tail = latex[start:].strip()
    if tail:
        segments.append(tail)
    return segments


def _mathml_with_rows(mathml: str, segments: list[str]) -> str:
    """Keep one evidence-bound math root while arranging long segments as rows."""

    opening_end = mathml.find(">")
    closing_start = mathml.rfind("</math>")
    if opening_end < 0 or closing_start < 0:
        return mathml
    rows = []
    for segment in segments:
        row_mathml = latex_to_mathml(segment)
        row_opening_end = row_mathml.find(">")
        row_closing_start = row_mathml.rfind("</math>")
        if row_opening_end < 0 or row_closing_start < 0:
            return mathml
        rows.append(
            "<mtr><mtd>"
            + row_mathml[row_opening_end + 1 : row_closing_start]
            + "</mtd></mtr>"
        )
    table = '<mtable columnalign="left" rowspacing="0.8ex">' + "".join(rows) + "</mtable>"
    return mathml[: opening_end + 1] + table + mathml[closing_start:]


def _render_venue_identity(identity: dict[str, Any]) -> str:
    venue = identity.get("venue_identity")
    if not isinstance(venue, dict):
        return ""
    logo = str(venue.get("logo_asset_token") or "").strip()
    label = " · ".join(
        str(venue.get(key) or "").strip()
        for key in ("label", "distinction")
        if str(venue.get(key) or "").strip()
    )
    logo_markup = (
        f'<img src="{escape(logo, quote=True)}" data-poster-venue-logo '
        f'alt="{escape(label or "Conference logo", quote=True)}">'
        if logo
        else ""
    )
    label_markup = f"<span>{escape(label)}</span>" if label else ""
    return (
        '<div class="venue-block" data-poster-venue="verified">'
        f"{logo_markup}{label_markup}</div>"
        if logo_markup or label_markup
        else ""
    )


def _deterministic_stylesheet(
    *,
    width_mm: float,
    height_mm: float,
    column_count: int,
    palette: dict[str, str],
    metrics: dict[str, float],
    typography: str,
    spacing: str = "balanced",
    masthead_scale: str = "balanced",
    figure_emphasis: str = "balanced",
    column_weights: tuple[float, ...] | None = None,
) -> str:
    display_font, body_font = _font_families(typography)
    single_figure_height, paired_figure_height, grid_figure_height = (
        _figure_height_limits(
            column_count,
            figure_emphasis=figure_emphasis,
        )
    )
    masthead_factor = {
        "compact": 0.88,
        "balanced": 1.0,
        "prominent": 1.12,
    }.get(masthead_scale, 1.0)
    title_size = metrics["title_min_mm"] * max(1.0, masthead_factor)
    title_padding = 9.0 * masthead_factor
    author_margin = 5.0 * masthead_factor
    logo_height = 30.0 * masthead_factor
    layout_css = _layout_grammar_stylesheet(
        palette=palette,
        metrics=metrics,
    )
    compact_css = f"""
.spacing-compact.poster-root {{ gap: 3.5mm; padding: 16mm 22mm 9mm; }}
.spacing-compact .title-band {{ padding-bottom: 5.5mm; }}
.spacing-compact h1 {{ font-size: {metrics["title_min_mm"]:g}mm; }}
.spacing-compact .poster-authors {{ margin-top: 3.2mm; line-height: 1.12; }}
.spacing-compact .poster-section-group {{ gap: 1.6mm; }}
.spacing-compact .section-group-header {{ margin-bottom: .8mm; }}
.spacing-compact .poster-module.priority-focal {{ padding: 4mm 6mm 5mm; }}
.spacing-compact .module-header {{ margin-bottom: 2.4mm; }}
.spacing-compact .module-kicker {{ gap: 3mm; margin-bottom: 1.5mm; }}
.spacing-compact .module-text, .spacing-compact .evidence-points {{ margin-top: 3mm; }}
.spacing-compact .evidence-points li + li {{ margin-top: 1.6mm; }}
.spacing-compact .kind-comparison .evidence-points {{ gap: 2mm; }}
.spacing-compact .kind-comparison .evidence-points li {{ padding-top: 2.4mm; padding-bottom: 2.4mm; }}
.spacing-compact .figure-grid {{ gap: 4mm; margin-top: 3.5mm; }}
.spacing-compact .source-figure {{ gap: 2.4mm; }}
.spacing-compact .equation-shell {{ margin-top: 2.5mm; padding: 2.2mm 4mm; }}
""".strip()
    effective_weights = column_weights or (1.0,) * column_count
    column_tracks = " ".join(
        f"minmax(0, {float(weight):g}fr)" for weight in effective_weights
    )
    column_gap_mm = {"compact": 1.6, "balanced": 6.0, "open": 8.0}.get(spacing, 6.0)
    module_padding = {
        "compact": "1.6mm 3mm 2.2mm",
        "balanced": "3mm 3mm 4mm",
        "open": "4mm 4mm 5mm",
    }.get(spacing, "3mm 3mm 4mm")
    separator_padding_mm = {"compact": 2.5, "balanced": 7.0, "open": 9.0}.get(
        spacing, 7.0
    )
    return f"""
@page {{ size: {width_mm:g}mm {height_mm:g}mm; margin: 0; }}
* {{ box-sizing: border-box; }}
html, body {{ width: {width_mm:g}mm; height: {height_mm:g}mm; margin: 0; }}
body {{ background: {palette["background"]}; color: {palette["ink"]}; font-family: {body_font}; }}
.poster-root {{ width: {width_mm:g}mm; height: {height_mm:g}mm; padding: 20mm 22mm 14mm; display: grid; grid-template-rows: auto minmax(0, 1fr); gap: 6mm; background: {palette["background"]}; }}
.title-band {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 18mm; padding: 0 2mm {title_padding:g}mm; border-bottom: 3mm solid {palette["accent"]}; }}
.title-copy {{ min-width: 0; }}
.masthead-align-center .title-band {{ position: relative; display: block; padding-inline: 110mm; text-align: center; }}
.masthead-align-center .venue-block {{ position: absolute; top: 0; right: 2mm; justify-items: end; text-align: right; }}
h1, h2 {{ font-family: {display_font}; }}
h1 {{ margin: 0; font-size: {title_size:g}mm; line-height: 1.02; letter-spacing: -0.035em; }}
.poster-authors {{ margin: {author_margin:g}mm 0 0; font-size: {metrics["body_min_mm"]:g}mm; line-height: 1.16; color: {palette["muted"]}; }}
.venue-block {{ display: grid; justify-items: end; gap: 3mm; max-width: 155mm; color: {palette["muted"]}; font-size: {metrics["provenance_min_mm"]:g}mm; text-align: right; }}
.venue-block img {{ width: auto; height: auto; max-width: 80mm; max-height: {logo_height:g}mm; object-fit: contain; }}
.poster-body {{ min-height: 0; height: 100%; }}
.poster-columns {{ min-height: 0; display: grid; grid-template-columns: {column_tracks}; gap: 14mm; align-items: start; }}
.poster-column {{ --lane-body-size: {metrics["body_target_mm"]:g}mm; --lane-table-body-size: {metrics["body_min_mm"]:g}mm; --lane-caption-size: {max(metrics["provenance_min_mm"], 6.0):g}mm; --lane-table-padding: 2.5mm; min-width: 0; display: flex; flex-direction: column; justify-content: flex-start; gap: {column_gap_mm:g}mm; }}
.poster-section-group {{ min-width: 0; display: flex; flex-direction: column; gap: 2mm; }}
.section-group-header {{ margin: 0 0 1mm; }}
.poster-module {{ display: block; width: 100%; min-width: 0; margin: 0; padding: {module_padding}; overflow-wrap: anywhere; background: transparent; border: 0; border-radius: 0; }}
.poster-section-group > .poster-module + .poster-module {{ padding-top: {separator_padding_mm:g}mm; border-top: 0.8mm solid color-mix(in srgb, {palette["accent"]} 38%, transparent); }}
.poster-module.priority-focal {{ padding: 5mm 6mm 6mm; border-left: 2.4mm solid {palette["accent"]}; background: color-mix(in srgb, {palette["accent"]} 6%, {palette["surface"]}); }}
.poster-module.kind-comparison .module-header, .poster-module.kind-method-flow .module-header, .poster-module.kind-metrics .module-header, .poster-module.kind-table .module-header {{ padding-bottom: 3mm; border-bottom: 0.5mm solid color-mix(in srgb, {palette["accent"]} 35%, transparent); }}
.poster-module.detail-grid .evidence-points {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 3mm; padding: 0; list-style: none; }}
.poster-module.detail-grid .evidence-points li {{ margin: 0; padding: 2mm 3mm; border-left: 0.8mm solid color-mix(in srgb, {palette["accent"]} 50%, transparent); }}
.poster-module.kind-comparison .evidence-points {{ counter-reset: comparison-atom; display: grid; grid-template-columns: 1fr; gap: 3mm; padding: 0; list-style: none; }}
.poster-module.kind-comparison.detail-grid .evidence-points {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.poster-module.kind-comparison .evidence-points li {{ counter-increment: comparison-atom; display: grid; grid-template-columns: 9mm minmax(0, 1fr); align-items: start; gap: 3mm; margin: 0; padding: 3mm 4mm 3mm 3mm; border: .5mm solid color-mix(in srgb, {palette["accent"]} 24%, transparent); background: {palette["surface"]}; font-variant-numeric: tabular-nums; }}
.poster-module.kind-comparison .evidence-points li::before {{ content: counter(comparison-atom, decimal-leading-zero); width: 8mm; height: 8mm; display: grid; place-items: center; border-radius: 50%; background: {palette["accent"]}; color: #fff; font-size: {metrics["provenance_min_mm"]:g}mm; font-weight: 700; line-height: 1; }}
.evidence-copy {{ min-width: 0; display: grid; gap: 1mm; }}
.evidence-label {{ color: {palette["accent"]}; font-weight: 800; }}
.evidence-value {{ min-width: 0; font-weight: 500; }}
.poster-module.kind-table .evidence-matrix {{ display: grid; grid-template-columns: 1fr; gap: 1.2mm; padding: 1.2mm 0; border-block: .55mm solid {palette["accent"]}; font-variant-numeric: tabular-nums; }}
.poster-module.kind-table .evidence-row {{ display: grid; grid-template-columns: minmax(34mm, .42fr) repeat(2, minmax(0, 1fr)); align-items: stretch; column-gap: 3mm; margin: 0; border-bottom: .25mm solid color-mix(in srgb, {palette["accent"]} 22%, transparent); }}
.poster-module.kind-table .evidence-row-single-value {{ grid-template-columns: minmax(34mm, .28fr) minmax(0, .72fr); }}
.poster-root[data-layout-columns="4"] .poster-module.kind-table .evidence-row-multi-value {{ grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: 1mm; }}
.poster-root[data-layout-columns="4"] .poster-module.kind-table .evidence-row-multi-value > .evidence-label {{ grid-column: 1 / -1; padding-block: 1.5mm; }}
.poster-module.kind-table .evidence-row:last-child {{ border-bottom: 0; }}
.poster-module.kind-table .evidence-label, .poster-module.kind-table .evidence-cell {{ min-width: 0; padding: var(--lane-table-padding) 3mm; }}
.poster-module.kind-table .evidence-label {{ display: flex; align-items: center; padding-left: 4mm; border-left: 1.2mm solid {palette["accent"]}; color: {palette["ink"]}; background: color-mix(in srgb, {palette["accent"]} 7%, {palette["surface"]}); }}
.poster-module.kind-table .evidence-cell {{ border-left: 0; }}
.poster-module.kind-table .evidence-row-unlabeled .evidence-cell {{ grid-column: 1 / -1; border-left: 0; }}
.poster-module.kind-metrics .evidence-points {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 3mm; padding: 0; list-style: none; }}
.poster-module.kind-metrics .evidence-points li {{ min-height: 29mm; margin: 0; padding: 3.5mm 4mm; border: .55mm solid color-mix(in srgb, {palette["accent"]} 28%, transparent); border-left: 1.4mm solid {palette["accent"]}; background: color-mix(in srgb, {palette["accent"]} 5%, {palette["surface"]}); font-weight: 650; font-variant-numeric: tabular-nums; }}
.poster-module.kind-metrics .evidence-label {{ font-size: 1.08em; line-height: 1.05; }}
.poster-module.kind-text.has-detail-points .evidence-callouts {{ counter-reset: text-callout; padding: 0; list-style: none; }}
.poster-module.kind-text.has-detail-points .evidence-callouts li {{ counter-increment: text-callout; position: relative; margin: 0; padding: 2.5mm 2mm 2.5mm 10mm; border-bottom: .35mm solid color-mix(in srgb, {palette["accent"]} 30%, transparent); }}
.poster-module.kind-text.has-detail-points .evidence-callouts li::before {{ content: counter(text-callout); position: absolute; left: 1mm; top: 2.6mm; color: {palette["accent"]}; font-weight: 800; font-variant-numeric: tabular-nums; }}
.module-header {{ margin-bottom: 3mm; }}
.module-kicker {{ display: flex; align-items: baseline; justify-content: space-between; gap: 4mm; margin: 0 0 2mm; }}
.section-label {{ margin: 0; font-size: {metrics["provenance_min_mm"]:g}mm; line-height: 1.1; }}
.section-label {{ color: {palette["accent"]}; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }}
h2 {{ margin: 0; font-size: {metrics["section_heading_min_mm"]:g}mm; line-height: 1.08; }}
.module-text, .evidence-points, .takeaway {{ font-size: var(--lane-body-size); line-height: 1.2; }}
.poster-root[data-layout-columns="4"] .poster-module.kind-table .evidence-points {{ font-size: var(--lane-table-body-size); line-height: 1.16; }}
.module-text {{ margin: 4mm 0 0; }}
.evidence-points {{ margin: 4mm 0 0; padding-left: 1.15em; }}
.evidence-points li + li {{ margin-top: 2mm; }}
.module-deck {{ margin: 1mm 0 0; padding: 0 0 0 4mm; border-left: 1.2mm solid {palette["accent"]}; color: {palette["ink"]}; font-weight: 700; }}
.kind-figure .module-text {{ color: {palette["muted"]}; }}
.figure-grid {{ display: grid; grid-template-columns: 1fr; gap: 6mm; margin: 5mm 0 0; }}
.figure-columns-2 {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.source-figure {{ min-width: 0; margin: 0; display: grid; align-content: start; gap: 3mm; }}
.source-figure img {{ display: block; width: 100%; height: auto; max-height: {single_figure_height}mm; object-fit: contain; object-position: center; background: #fff; border-radius: 2mm; }}
.figure-count-2 .source-figure img {{ max-height: {paired_figure_height}mm; }}
.figure-count-3 .source-figure img, .figure-count-4 .source-figure img {{ max-height: {grid_figure_height}mm; }}
figcaption {{ font-size: var(--lane-caption-size); line-height: 1.16; color: {palette["muted"]}; }}
.equation-shell {{ margin-top: 4mm; padding: 3mm 4mm; background: color-mix(in srgb, {palette["muted"]} 7%, transparent); border-radius: 2mm; }}
.equation-label {{ display: block; margin-bottom: 1.5mm; color: {palette["muted"]}; font-size: {metrics["provenance_min_mm"]:g}mm; }}
math[data-content-role="equation"] {{ font-size: {metrics["body_target_mm"]:g}mm; }}
.kind-method-flow .equation-shell {{ display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; column-gap: 3mm; margin-top: 2mm; padding: 2mm 3mm; }}
.kind-method-flow .equation-label {{ margin: 0; white-space: nowrap; }}
.poster-module.kind-method-flow .module-text {{ padding: 3mm 4mm; border-left: 1mm solid {palette["accent"]}; background: color-mix(in srgb, {palette["accent"]} 6%, {palette["surface"]}); }}
.poster-module.kind-method-flow .evidence-points {{ counter-reset: method-step; position: relative; display: grid; grid-template-columns: 1fr; gap: 3.5mm; padding: 0; list-style: none; }}
.poster-module.kind-method-flow .evidence-points::before {{ content: ""; position: absolute; z-index: 0; left: 5.4mm; top: 5mm; bottom: 5mm; width: .8mm; background: color-mix(in srgb, {palette["accent"]} 50%, transparent); }}
.poster-module.kind-method-flow .evidence-points li {{ counter-increment: method-step; position: relative; z-index: 1; margin: 0; padding: 3mm 3mm 3mm 13mm; border: .45mm solid color-mix(in srgb, {palette["accent"]} 22%, transparent); background: color-mix(in srgb, {palette["accent"]} 5%, {palette["surface"]}); }}
.poster-module.kind-method-flow .evidence-points li::before {{ content: counter(method-step); position: absolute; left: 2.2mm; top: 2.7mm; width: 7.2mm; height: 7.2mm; display: grid; place-items: center; border-radius: 50%; background: {palette["accent"]}; color: #fff; font-size: {metrics["provenance_min_mm"]:g}mm; font-weight: 700; line-height: 1; }}
.poster-module.kind-method-flow .evidence-points li:not(:last-child)::after {{ content: "↓"; position: absolute; left: 3.8mm; bottom: -4.2mm; color: {palette["accent"]}; font-size: {metrics["provenance_min_mm"]:g}mm; font-weight: 800; line-height: 1; }}
{layout_css}
{compact_css}
""".strip()


def _layout_grammar_stylesheet(
    *,
    palette: dict[str, str],
    metrics: dict[str, float],
) -> str:
    """Style only generic layout primitives emitted by reference interpretation."""

    return f"""
.section-style-band .section-group-header,
.section-style-band .poster-lead .module-header:has(.section-label) {{ margin-bottom: 2mm; padding: 3mm 4mm; background: {palette["accent"]}; }}
.section-style-band .section-group-header .section-label,
.section-style-band .poster-lead .module-header:has(.section-label) h2,
.section-style-band .poster-lead .module-header:has(.section-label) .section-label {{ color: #fff; }}
.section-style-rule .section-group-header,
.section-style-rule .poster-lead .module-header:has(.section-label) {{ padding-bottom: 2mm; border-bottom: 1.5mm solid {palette["accent"]}; }}
.section-style-outlined .poster-section-group,
.section-style-outlined .poster-lead {{ padding: 3mm; border: .7mm solid {palette["accent"]}; border-radius: 2mm; }}
.module-style-outlined .poster-module {{ padding: 4mm; border: .7mm solid {palette["accent"]}; }}
.module-style-outlined .poster-module.priority-focal {{ background: transparent; }}
.spacing-compact.module-style-outlined .poster-module {{ padding: 1.6mm 3mm 2mm; }}
.spacing-compact.section-style-band .section-group-header,
.spacing-compact.section-style-band .poster-lead .module-header:has(.section-label) {{ margin-bottom: 1mm; padding: 1.5mm 3mm 2mm; }}
.spacing-compact.section-style-outlined .poster-section-group,
.spacing-compact.section-style-outlined .poster-lead {{ padding: 2mm; }}
.module-style-open .poster-module.priority-focal {{ padding: 3mm 3mm 4mm; border-left: 0; background: transparent; }}
.lead-placement-center-lane .poster-column-stage {{ padding: 0 4mm; border-inline: .7mm solid color-mix(in srgb, {palette["accent"]} 55%, transparent); }}
.lead-placement-center-lane .poster-column-stage .poster-module {{ background: color-mix(in srgb, {palette["accent"]} 5%, {palette["surface"]}); }}
.lead-placement-full-width .poster-body {{ display: grid; grid-template-rows: auto minmax(0, 1fr); gap: 8mm; }}
.lead-placement-full-width .poster-lead {{ display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, .9fr); align-items: start; column-gap: 14mm; padding: 6mm 8mm; border: .8mm solid {palette["accent"]}; background: color-mix(in srgb, {palette["accent"]} 7%, {palette["surface"]}); }}
.lead-placement-full-width .module-lead-copy,
.lead-placement-full-width .module-lead-evidence {{ min-width: 0; align-self: start; }}
.lead-placement-full-width .module-lead-copy {{ grid-column: 1; }}
.lead-placement-full-width .module-lead-evidence {{ grid-column: 2; }}
.lead-placement-full-width .module-lead-copy .module-text {{ font-size: {max(metrics["body_target_mm"], 12.0):g}mm; }}
.lead-placement-full-width .module-lead-evidence .figure-grid {{ margin-top: 0; }}
.lead-placement-full-width .poster-lead h2 {{ font-size: {max(metrics["section_heading_min_mm"], 16.0):g}mm; }}
.lead-placement-full-width .poster-module.priority-focal {{ border-left: 0; }}
.lead-placement-full-width .poster-lead.lead-text-visual {{ grid-template-columns: minmax(0, .85fr) minmax(0, 1.15fr); align-items: stretch; column-gap: 0; padding: 0; overflow: hidden; }}
.lead-placement-full-width .poster-lead.lead-text-visual {{ background: {palette["background"]}; }}
.lead-placement-full-width .lead-text-visual .module-lead-copy {{ display: flex; align-items: center; padding: 8mm 10mm; background: {palette["accent"]}; }}
.lead-placement-full-width .lead-text-visual .module-lead-copy .module-header {{ width: 100%; margin: 0; padding: 0; border: 0; background: transparent; }}
.lead-placement-full-width .lead-text-visual .module-lead-copy h2, .lead-placement-full-width .lead-text-visual .module-lead-copy .section-label {{ color: #fff; }}
.lead-placement-full-width .lead-text-visual .module-lead-evidence {{ padding: 5mm 7mm; background: {palette["background"]}; }}
.lead-placement-full-width .lead-text-visual .module-lead-evidence .evidence-points {{ margin-top: 0; }}
.lead-placement-full-width .lead-text-visual.kind-method-flow .module-lead-evidence .evidence-points:has(> li:nth-child(2)) {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.lead-placement-full-width .lead-text-visual.kind-method-flow .module-lead-evidence .evidence-points::before,
.lead-placement-full-width .lead-text-visual.kind-method-flow .module-lead-evidence .evidence-points li::after {{ display: none; }}
""".strip()


def measured_lane_stylesheet(
    scales: tuple[float, ...],
    *,
    width_mm: float,
    modules: list[dict[str, Any]],
    column_count: int,
) -> str:
    """Bind measured lane scales to the renderer's stable lane grammar."""

    metrics = _content_typography_metrics(
        width_mm,
        modules,
        column_count=column_count,
    )
    caption_size = max(metrics["provenance_min_mm"], 6.0)
    rules = []
    for index, scale in enumerate(scales, start=1):
        if scale == 1.0:
            continue
        body_size = max(metrics["body_min_mm"], metrics["body_target_mm"] * scale)
        table_size = max(metrics["body_min_mm"], metrics["body_min_mm"] * scale)
        scaled_caption = max(caption_size, caption_size * scale)
        rules.append(
            f'.poster-column[data-poster-lane="{index}"] {{ '
            f'--lane-body-size: {body_size:g}mm; '
            f'--lane-table-body-size: {table_size:g}mm; '
            f'--lane-caption-size: {scaled_caption:g}mm; '
            f'--lane-table-padding: {2.5 * scale:g}mm; }}'
        )
    return "\n".join(rules)


def _font_families(typography: str) -> tuple[str, str]:
    """Map the documented typography profiles to offline-safe system stacks."""

    cjk_sans = '"Noto Sans SC", "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Microsoft YaHei"'
    cjk_serif = '"Songti SC", "Noto Serif CJK SC", "Microsoft YaHei"'
    sans = f"Arial, {cjk_sans}, Helvetica, sans-serif"
    serif = f'Georgia, "Times New Roman", {cjk_serif}, serif'
    if typography == "serif":
        return serif, serif
    if typography == "hybrid":
        return serif, sans
    return sans, sans


def _figure_height_limits(
    column_count: int,
    *,
    figure_emphasis: str = "balanced",
) -> tuple[int, int, int]:
    """Share physical evidence limits between packing estimates and rendered CSS."""

    if column_count <= 1:
        limits = (260, 210, 150)
    elif column_count == 2:
        limits = (220, 180, 150)
    elif column_count == 3:
        limits = (235, 135, 105)
    else:
        limits = (190, 160, 110)
    factor = {
        "copy-led": 0.86,
        "balanced": 1.0,
        "figure-led": 1.14,
    }.get(figure_emphasis, 1.0)
    scaled = tuple(round(value * factor) for value in limits)
    return scaled[0], scaled[1], scaled[2]


def _require_visual_design(
    value: visual_design.VisualDesignPlan | None,
) -> visual_design.VisualDesignPlan:
    """Reject authoring or visual revision without a bound design decision."""

    if value is None:
        raise ValueError("a bound visual design plan is required")
    return value


def _typography_guidance(page_plan: dict[str, Any] | None) -> str:
    """Describe physical type targets without turning them into a static gate."""

    width = (page_plan or {}).get("width_mm")
    if isinstance(width, bool) or not isinstance(width, (int, float)) or width <= 0:
        return (
            "Use viewing-distance body type and readable line measures. Resolve crowding "
            "through hierarchy, reflow, or evidence selection before reducing type."
        )
    metrics = planning.typography_metrics(float(width))
    return (
        "At this physical page width, use a clear viewing-distance hierarchy: title at "
        f"least about {metrics['title_min_mm']:g} mm, section headings at least about "
        f"{metrics['section_heading_min_mm']:g} mm, normal body copy at least "
        f"{metrics['body_min_mm']:g} mm and near {metrics['body_target_mm']:g} mm, and "
        f"subordinate captions or provenance no smaller than about "
        f"{metrics['provenance_min_mm']:g} mm. These are advisory authoring targets, not "
        "a static pass/fail gate. Resolve crowding through line measure, hierarchy, "
        "reflow, or evidence selection before reducing type."
    )


def html_repair_system(*, revision_mode: str = "draft") -> str:
    """Return a compact repair boundary for a complete inert poster document."""

    content_replan = revision_mode == "content-replan"
    evidence_boundary = (
        "The immutable repair manifest permits rewriting only the existing modules' "
        "grounded explanatory visible text and text-by-role copy. Preserve their "
        "identity, source labels, module ids and roles, priority, focal roles, figure "
        "bindings, equations, and page contract exactly."
        if content_replan
        else "Preserve the exact scientific content, identity, source labels, module ids "
        "and roles, figure bindings, and page contract supplied in the immutable repair "
        "manifest."
    )
    return f"""Repair one complete inert HTML/CSS scientific poster.
Return only the full document from <!doctype html> through </html>.
Apply every validator issue and no unrelated rewrite. The one body-level poster root must enclose the
complete physical page, with exactly one data-poster-title-band inside it before the internal evidence
body. The title band stays outside generic evidence-module selector effects, never outside the poster
root. Match @page, html, body, and that root to the manifest's explicit physical width and height;
when it owns padding or borders, use border-box. Never use auto, min-height, or max-height on the root.
Use exactly the content modules listed in the immutable manifest; remove any unlisted provenance,
references, footer, or decorative data-poster-module wrapper rather than inventing a source label.
{evidence_boundary} Use the supplied immutable
identity render slots instead of retyping title or authors; the runtime replaces them with verified
text. Never invent claims, numbers, equations, citations, authors, affiliations, logos, or source
figures. Use inline CSS only; no scripts, event handlers, remote resources, external fonts, comments,
duplicate declarations, repeated inline styles, or decorative SVG noise. Keep native semantic MathML.
Validator-supplied expected_data_latex values are JSON strings: decode JSON escaping exactly once
before placing them in data-latex, and encode a literal < in the quoted HTML attribute as &lt;."""


def revision_prompt(
    *,
    source_html: str,
    feedback: str,
    selection: dict[str, Any] | None,
    page_plan: dict[str, Any] | None = None,
    allow_adaptive_height: bool = False,
    visual_design_plan: visual_design.VisualDesignPlan | None = None,
    content_brief: dict[str, Any] | None = None,
    revision_mode: str = "full-layout",
    content_replan_targets: list[str] | None = None,
) -> tuple[str, str]:
    """Build the complete poster-revision prompt."""

    design = _require_visual_design(visual_design_plan)
    if revision_mode not in {"full-layout", "content-replan"}:
        raise ValueError("revision_mode must be full-layout or content-replan")
    adaptive = allow_adaptive_height and (page_plan or {}).get("strategy") == "auto"
    if adaptive:
        width = float((page_plan or {})["width_mm"])
        minimum = float((page_plan or {})["min_height_mm"])
        maximum = float((page_plan or {})["max_height_mm"])
        page_instruction = (
            f"Keep page width exactly {width:g} mm and preserve landscape/portrait "
            f"orientation. You may change only page height to any physical height between "
            f"{minimum:g} mm and {maximum:g} mm, when the screenshot feedback requires "
            "removing accidental empty canvas or resolving genuine crowding. Reflow the "
            "content first, then choose the most compact height that contains it with "
            "modest breathing room; never use extra page height to hide an imbalanced "
            "composition."
        )
    else:
        page_instruction = "Keep physical page dimensions unchanged."
    content_replan = revision_mode == "content-replan"
    targets = sorted(
        {
            str(target).strip()
            for target in content_replan_targets or []
            if str(target).strip()
        }
    )
    if content_replan and not targets:
        raise ValueError("content-replan requires at least one target module id")
    evidence_boundary = (
        "You may curate, compress, reorder, or rewrite only the existing grounded "
        f"explanatory copy in these target modules: {', '.join(targets)}. Preserve each "
        "module's central takeaway and every qualifier or value attached to a retained "
        "claim. You may omit secondary examples or repeated details when needed for a "
        "readable poster, but may not add a claim, number, or implication absent from the "
        "grounded authority. Keep every equation, identity field, source label, figure "
        "inventory, module id, semantic role, priority, and focal role unchanged. Do not "
        "delete modules or replace figures."
        if content_replan
        else "Preserve the exact scientific snapshot: module ids, visible module content, "
        "source labels, priorities, focal role, source-figure hashes, equations and "
        "data-latex, authors, venue identity, and logo binding."
    )
    typography_guidance = _typography_guidance(page_plan)
    system = f"""Revise a complete inert HTML/CSS scientific poster.
Return the entire corrected HTML document beginning with <!doctype html>, with no Markdown or commentary.
{page_instruction}
The bound executable visual contract is the complete reference-derived design authority for the
revision. Preserve its layout tokens, typography profile, and palette roles. For a whole-page request,
reconstruct layout wrappers and placement as needed; do not substitute a generic scaffold or style
inferred from venue identity or page dimensions.
Do not infer or reconstruct the original reference image or seed. The reference was interpreted once before
authoring; only the transferable visual design plan below may influence this revision.
Treat visual-review targets as observation anchors, not a whitelist of wrappers that may move. Its
visible evidence and whole-page acceptance outcome define the problem; any suggested exact placement
is advisory. Repack other intact modules when needed, and verify the completed composition rather than
transferring a void, crowding, or weak hierarchy from one zone to another.
Section membership is semantic metadata, not a placement constraint; move intact modules across lanes
when rendered depth requires it while keeping their section cues understandable.

Keep @page, html, body, and the body-level poster root on the same explicit physical width and height.
Never use auto height or min-height on the poster root; remove rigid sizing only from internal content
containers, and make their intrinsic flow fit inside the unchanged physical page. Use border-box when
the root owns padding or borders. Keep audit wrappers
directly placeable by the macro layout; `display: contents` is allowed only on optional grouping parents,
never on `[data-poster-module]` itself. Do not use it to flatten unequal lanes into one global grid with
shared numbered rows; keep lane rows local so one tall figure cannot push another lane below the page.
Use the supplied rendered measurements to decide whether flex
growth, fixed tracks, or intrinsic flow is appropriate; remove those constraints only where they cause
equal-height stretch, clipping, or transferred overflow. Give small intrinsic figures an explicit
responsive rendered width when the screenshot shows them under-scaled.
Keep data-content-role="equation" and data-latex only on each <math> root. Put panel styling on an
unmarked wrapper and preserve the MathML root's native formatting context.
The poster root must enclose exactly one title band followed by the internal evidence body. Keep the
title band outside the evidence-module selector cascade, never outside the poster root. If it is
unexpectedly tall, restore its intended horizontal flow before shrinking body content. Do not append
a spanning footer after
already full-height stack wrappers; place that module in available body space or reserve its row when
packing the rest of the page.

{evidence_boundary}
Do not add scripts, active content, remote resources, fonts, claims, numbers, citations, or figures.
Keep native MathML formatting and explicit physical math sizes. Keep content rows intrinsic, prose
children shrinkable, and every item inside the poster. Never repair fit with overlap, clipping, empty
filler, repeated content, or fixed rows for unknown content.
{typography_guidance}
When one visual zone overflows while another has substantial usable space, relocate intact movable
module wrappers before reducing type or figure scale. When several lanes retain genuine spare capacity,
use it to improve reading-distance scale for the smallest body or caption text and under-scaled
evidence before adding decorative gaps; never add filler or stretch low-information blocks merely to
reach the page edge. If one lane ends conspicuously earlier than neighbouring lanes, rebalance by
reassigning or reordering intact modules where their semantic grouping remains clear; do not force
equal-height tracks, padding, filler, or decorative stretching. {"Rewrite only grounded explanatory copy after layout options are exhausted." if content_replan else "Preserve every module's frozen scientific copy and evidence while allowing visual wrappers, emphasis, grouping, order, span, or placement to change."}"""
    selection_text = (
        json.dumps(selection, ensure_ascii=False, sort_keys=True)
        if selection is not None
        else "No element selection; interpret the feedback at poster level."
    )
    visual_design_text = json.dumps(
        design.executable_dict(), ensure_ascii=False, sort_keys=True
    )
    revision_content_brief = (
        _content_replan_brief(content_brief, targets)
        if content_replan
        else (content_brief if isinstance(content_brief, dict) else {})
    )
    content_brief_text = json.dumps(
        revision_content_brief,
        ensure_ascii=False,
        sort_keys=True,
    )
    page_plan_text = json.dumps(
        page_plan if isinstance(page_plan, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
    )
    revision_authority = (
        "The visual reviewer may request grounded-copy edits only in the listed target "
        "modules; it cannot authorize new facts. Treat the supplied grounded authority "
        "as the only scientific authority: shorten, select, or paraphrase its existing "
        "copy without extending its meaning. Structural, source-binding, and full-source "
        "validation remain before publication."
        if content_replan
        else "This is a full-layout composition-only revision. Do not rewrite, compress, "
        "or reorder visible scientific copy; preserve the exact scientific snapshot."
    )
    user = (
        f"Revision mode: {revision_mode}\n\nVisual repair brief:\n{feedback}\n\n"
        f"Selected DOM context:\n{selection_text}\n\n"
        "Bound executable visual contract:\n"
        f"{visual_design_text}\n\n"
        "Bound page plan:\n"
        f"{page_plan_text}\n\n"
        "Grounded visual content brief:\n"
        f"{content_brief_text}\n\n" + revision_authority + "\n\n"
        f"Current complete HTML:\n{source_html}"
    )
    return system, user


def _content_replan_brief(
    content_brief: dict[str, Any] | None,
    targets: list[str],
) -> dict[str, Any]:
    """Keep only copy authority needed by the explicitly targeted modules."""

    brief = dict(content_brief) if isinstance(content_brief, dict) else {}
    authority = brief.get("grounded_authority")
    if isinstance(authority, dict):
        scoped_authority = dict(authority)
        modules = authority.get("content_modules")
        if isinstance(modules, list):
            target_ids = set(targets)
            scoped_authority["content_modules"] = [
                module
                for module in modules
                if isinstance(module, dict)
                and str(module.get("id") or "").strip() in target_ids
            ]
        brief["grounded_authority"] = scoped_authority
    # The complete current HTML below already supplies the displayed snapshot.
    brief.pop("displayed_content_snapshot", None)
    return brief


def stylesheet_revision_prompt(
    *,
    source_html: str,
    feedback: str,
    page_plan: dict[str, Any] | None = None,
    allow_adaptive_height: bool = False,
    visual_design_plan: visual_design.VisualDesignPlan | None = None,
) -> tuple[str, str]:
    """Build a compact visual-revision prompt that cannot rewrite poster evidence."""

    design = _require_visual_design(visual_design_plan)
    adaptive = allow_adaptive_height and (page_plan or {}).get("strategy") == "auto"
    if adaptive:
        page_instruction = (
            f"Keep width exactly {float((page_plan or {})['width_mm']):g} mm. Height may "
            f"use any physical height between {float((page_plan or {})['min_height_mm']):g} mm and "
            f"{float((page_plan or {})['max_height_mm']):g} mm. Reflow the content first, "
            "then use the most compact height that contains it with modest breathing "
            "room; never enlarge the canvas to conceal an imbalanced composition."
        )
    else:
        page_instruction = "Keep the physical page dimensions unchanged."
    capacity = (page_plan or {}).get("layout_capacity")
    maximum_columns = (
        capacity.get("maximum_readable_column_count")
        if isinstance(capacity, dict)
        else None
    )
    capacity_instruction = (
        f"Use no more than {maximum_columns} readable tracks; the visual design plan "
        "chooses the actual topology."
        if isinstance(maximum_columns, int)
        else "Choose the actual topology from the bound visual design plan."
    )
    typography_guidance = _typography_guidance(page_plan)
    system = f"""Restyle an editable HTML/CSS academic conference poster.
Return exactly one complete <style>...</style> element containing only the corrective override rules,
with no Markdown or commentary. Do not reproduce the existing base stylesheet or the HTML document.
{page_instruction}
{capacity_instruction}
The host will append only the returned override rules to the existing style element, so all scientific
text, figures, ids, semantic roles, source hashes, masthead content, DOM order, and unaffected base CSS
remain byte-for-byte unchanged. Return only selectors and declarations needed for the supplied feedback.
When the markup exposes independently movable module wrappers, CSS grid placement, order, and spans may
relocate them without changing their content. Do not flatten unequal lane wrappers with
`display: contents` into shared numbered grid rows; keep each lane's vertical flow local so a tall item
cannot displace unrelated modules in another lane.
Keep vertically stacked modules intrinsic: remove `flex-grow`, `flex: 1`, fixed row growth, and equal-height
stretch from content panels unless their own evidence intentionally needs that space. Give small
intrinsic source figures an explicit responsive width instead of leaving `width: auto`.
When several lanes retain genuine spare capacity, use it to improve reading-distance scale for the
smallest body or caption text and under-scaled evidence before adding decorative gaps; never add filler
or stretch low-information blocks merely to reach the page edge. If one lane ends conspicuously
earlier than neighbouring lanes, use grid placement to redistribute intact modules where their
semantic grouping remains clear; do not simulate balance with equal-height tracks, padding, filler,
or decorative stretching.
Keep broad module selectors from overriding the title band's layout. Do not append a spanning footer
below already full-height stack wrappers; place it in available body space or reserve its row first.
Always keep @page, html/body, and the poster root on the same explicit physical width and height. Never leave the poster root at auto height, min-height, or a height that differs from @page.
The poster root must enclose exactly one data-poster-title-band followed by the internal evidence body;
the title band is outside module selector effects, never outside the poster root. Use border-box when
the root owns padding or borders.
The bound executable visual contract guides the stylesheet. Preserve its layout tokens, typography
profile, and palette roles without changing the DOM or inventing a generic style. Keep content rows
intrinsic, prose children shrinkable, figures readable, and all content inside the poster. Judge
min-size and track choices by the rendered content; do not conceal clipping, overlap, or filler.
{typography_guidance}
Keep MathML in its native formatting context with an explicit physical font size; never set display
or overflow on the math[data-content-role="equation"] root, and style an unmarked wrapper instead. Use print-safe CSS
only and no external resources or generated content."""
    visual_design_text = json.dumps(
        design.executable_dict(), ensure_ascii=False, sort_keys=True
    )
    user = (
        f"Feedback:\n{feedback}\n\n"
        f"Bound executable visual contract:\n{visual_design_text}\n\n"
        "CSS-relevant body structure (visible text nodes omitted):\n"
        f"{_stylesheet_dom_outline(source_html)}"
    )
    return system, user


def _stylesheet_dom_outline(source_html: str) -> str:
    """Keep selector structure while removing scientific text from the model prompt."""

    match = re.search(
        r"<body\b[^>]*>.*?</body>",
        source_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise ValueError("stylesheet revision requires one body element")
    outline = re.sub(r">[^<]*<", "><", match.group(), flags=re.DOTALL)
    return re.sub(r"\s+", " ", outline).replace("><", ">\n<").strip()


def _prepared_figure_manifest(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose only source identity and geometry needed for planning or audit."""

    return [
        {
            "sha256": str(item.get("content_sha256") or ""),
            "description": str(item.get("description") or ""),
            "figure_number": item.get("figure_number"),
            "page": item.get("page"),
            "aspect_ratio": poster_assets.asset_aspect_ratio(item),
        }
        for item in assets
        if item.get("source_kind") == "pdf_figure"
    ]


def _concise_asset_description(item: dict[str, Any]) -> str:
    """Keep the extracted caption while dropping long paper-discussion context."""

    description = str(item.get("description") or item.get("filename") or "asset")
    description = description.split("paper discussion:", 1)[0].strip().rstrip(".")
    return description[:600].rstrip()
