"""Prompt contracts for grounded scientific-poster authoring and repair."""

from __future__ import annotations

import json
from typing import Any

import poster_assets

from posterlib.content import html_contract, planning
from posterlib.visual import reference_seeds, visual_design


def repair_guidance(issues: list[dict[str, Any]]) -> str:
    """Return bounded model-repair guidance for static validation issues."""

    guidance: list[str] = []
    if any(item.get("code") == "ungrounded_rights_claim" for item in issues):
        guidance.append(
            "Remove every unsupported copyright, rights, reproduction, or permission "
            "statement; do not replace it with another legal claim."
        )
    return "\n".join(guidance) + ("\n\n" if guidance else "")


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

ORGANIZATION
Use as many scan sections as the evidence needs. Sections are source-grounded scan groups, not rails
or a mandatory problem/method/results story. A module is one independently placeable communication
job that a reader can understand as one local scan action. Multiple modules may share one section.
Split an omnibus topic when its claim, mechanism, equations, or evidence need different reading
actions; combine only fragments that cannot stand on their own. Narrative ordering is optional and
only for a genuinely sequential argument; otherwise prefer scan-first. organization_mode, focal_role,
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
id, a declared section_id, a literal machine-verifiable source_label, and some visible evidence. Use
locators such as "Figure 2", "Table 1", "Section 3.1", or "p. 4"; avoid vague labels. These locators
are audit provenance, not visible poster copy: do not write Figure N or Table N into title, text,
detail_points, or takeaway. Include a limitation only when the source supports a real boundary or
failure condition.

PHYSICAL CAPACITY AND FIGURES
Treat the requested page as a soft one-page physical-capacity budget, not a target to fill. Choose a
complementary, non-redundant set of prepared figures that explains the contribution, mechanism, and
decisive evidence. This is not a fixed count of figures, modules, sections, rails, or columns. Omit
redundant figures when labels would be unreadable; do not invent or repeat content when the page is
sparse. Respect the supplied figure-readability reference: plan the display scale before the count,
and consolidate or omit secondary plots when they cannot remain interpretable without zoom. Bind each
selected SHA-256 to the module that explains it, and include the matching Figure N in that module's
machine source_label. A source_label may cite additional evidence used for the module's grounded
claim without forcing every cited figure onto the poster. Never visibly describe an unbound prepared
figure.

Before returning, review the whole budget against the physical page envelope. If it is too dense,
edit the evidence plan semantically: keep the main claim, key method, and decisive evidence; remove
repeated explanations and omit secondary figures or modules. Do not solve capacity by mechanically
truncating prose, weakening scientific qualifiers, shrinking figure intent, or inventing replacement
content.

Required top-level shape: {"sections":[...],"content_modules":[...]}.
Each module requires id, section_id, and source_label. Add only the useful optional fields among
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


def draft_prompt(
    *,
    assets: list[dict[str, Any]],
    content_budget: dict[str, Any],
    page_plan: dict[str, Any],
    authoring_request: str,
    paper_source: dict[str, Any] | None = None,
    design_reference: reference_seeds.ReferenceBundle | None = None,
    visual_design_plan: visual_design.VisualDesignPlan | None = None,
) -> tuple[str, str]:
    """Build the complete grounded HTML authoring prompt."""

    design = _require_visual_design(visual_design_plan)
    page_contract = _page_contract(page_plan)
    typography_guidance = _typography_guidance(page_plan)
    system = f"""Author one complete editable HTML/CSS academic conference poster.
Return exactly one inert HTML document from <!doctype html> through </html>, with one inline stylesheet
and no Markdown, scripts, event handlers, remote resources, external fonts, or generated content.

IMMUTABLE SCIENTIFIC EVIDENCE
Use only the supplied module copy, numbers, locators, equations, figure hashes, authors, and verified
identity. Preserve qualifiers, scope, protocols, and limitations. Within a module, omit a redundant
optional copy field when the same grounded point is already visible in another channel; never omit a
unique qualifier, selected figure, selected equation, or decisive result. Do not retype the title or
authors. Render the exact supplied title render slot once as the visible text of one h1 and the exact
authors render slot once as the visible text of one p, div, or span in the same compact title band; the
runtime replaces those slots with verified text. Render the supplied logo token once when present, at
a recognizable but subordinate masthead scale. Compose prose only from module title, text, takeaway,
and detail_points. Do not render every populated optional field. Select the smallest non-redundant
combination that preserves the module's claim, necessary qualification, and decisive evidence. When
a figure, table, metric comparison, method flow, or equation is the primary channel, that visual
evidence carries the detail; adjacent prose should only direct attention or state a qualification,
not restate the same information in paragraph, bullets, and caption.
Create exactly one data-poster-module wrapper for each supplied content module and no additional
data-poster-module wrappers. Machine locators remain in bound metadata; show consolidated provenance
only when the supplied content budget already contains that module. Never invent a provenance footer
or repeat paper figure numbers as visible captions.

Give every module one independently movable wrapper whose HTML id is the supplied module id. The
wrapper is an authoring anchor, not a card, rail, or prescribed panel; the runtime binds grounding
metadata after authoring. Section treatment belongs to coherent visible groups and section cues, not
automatically to every module wrapper; modules sharing a section may share one visual heading. Internal
helpers, unequal zones, spans, and subgrids remain free. Render each
selected source figure once in its bound module as an <img> whose src is the exact asset token, with an
accurate alt and short interpretive caption but no visible paper figure number. Mark every interpretive
caption with a semantic <figcaption> element or data-content-role="caption"; a CSS class alone is not
its semantic role. Never describe an unbound figure. Render supplied equations once, in their listed
order, as native semantic MathML.
The runtime binds exact equation provenance; put panel styling on a normal wrapper, not the <math> root.
Treat detail_points as evidence atoms, not as an instruction to create a bullet list. Render metrics
and comparisons as concise callouts or a semantic table when that better matches visual_kind; reserve
paragraphs and bullets for genuinely verbal reasoning. Let figures, tables, equations, and labels carry
their share of the explanation instead of repeating them in prose.

PHYSICAL PAGE
Match @page, html, body, and the body-level poster root to the exact millimetre page. The poster root
must enclose the complete poster: put one compact data-poster-title-band first, followed by an internal
evidence body. Keep the title band outside generic module selector effects, not outside the poster root.
When the root owns padding or borders, use border-box so its outer box remains exactly @page.
Allocate the whole page before styling details; fit every module without overlap, clipping, empty
stretch, repeated copy, or filler. Keep modules regroupable rather than locked to one scaffold, but do
not flatten unequal lane groups into one global grid with shared numbered rows: an unrelated tall item
will stretch the shared row and push later groups down the page. Prefer nested independently flowing
groups or local subgrids; regroup, reorder, or span modules as their real depth requires.
Keep content rows and vertical modules intrinsic; avoid `flex: 1` or equal-height stretch. Module
wrappers must render measurable boxes, not `display: contents`. Prose children need min-width: 0 and
wrapping. Give figures purposeful responsive widths: `width: auto` with `max-width: 100%` does not enlarge
a small intrinsic image. Treat maximum_readable_column_count as a ceiling.
{typography_guidance}

REFERENCE VISUAL PLAN
The bound visual design plan guides one coherent design. Apply its topology, density, typography,
palette relationships, section_treatment, focal_strategy, figure_strategy, reading_path, observations,
and directives to the actual content geometry. Do not infer style from venue or page size. Preserve
module evidence while letting the reference-led hierarchy determine grouping, placement, and scale.
Make decisive figures readable; use a legible small multiple or dominant-plus-companions composition
when a module binds several figures. Rebalance intact modules before changing page size or adding copy.

MINIMUM HTML SAFETY
Keep CSS concise and print-safe. Every module and child must remain inside the poster. Keep native
MathML in its formatting context with explicit physical math size; do not set display or overflow on
the MathML root. Validator details not stated here are enforced by the host."""
    identity_text = json.dumps(
        _paper_identity_payload(paper_source), ensure_ascii=False, sort_keys=True
    )
    reference_text = json.dumps(
        {
            "source_kind": design_reference.source_kind,
            "seed_id": design_reference.seed_id,
            "image_sha256": design_reference.image_sha256,
        }
        if design_reference is not None
        else {},
        ensure_ascii=False,
        sort_keys=True,
    )
    user = "\n\n".join(
        (
            "Author the complete poster now.",
            "User preferences (never evidence):\n"
            + (authoring_request or "No additional preferences."),
            "Verified paper identity:\n" + identity_text,
            "Required immutable identity render slots:\n"
            + json.dumps(
                _identity_render_slots(paper_source),
                ensure_ascii=False,
                sort_keys=True,
            ),
            "Physical page and content-integrity contract:\n"
            + json.dumps(page_contract, ensure_ascii=False, sort_keys=True),
            "Measured relative module depths (advisory; no tracks or coordinates):\n"
            + json.dumps(
                planning.module_depth_hints(
                    content_budget,
                    width_mm=float(page_plan["width_mm"]),
                ),
                ensure_ascii=False,
                sort_keys=True,
            ),
            "Source-validated evidence budget:\n"
            + json.dumps(content_budget, ensure_ascii=False, sort_keys=True),
            "Bound reference-aware visual design plan:\n"
            + json.dumps(
                design.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
            ),
            "Reference audit metadata (not scientific content):\n" + reference_text,
            "Available embedded assets:\n"
            + _asset_prompt(_selected_authoring_assets(assets, page_plan=page_plan)),
        )
    )
    return system, user


def _require_visual_design(
    value: visual_design.VisualDesignPlan | None,
) -> visual_design.VisualDesignPlan:
    """Reject authoring or visual revision without a bound design decision."""

    if value is None:
        raise ValueError("a bound visual design plan is required")
    return value


def _page_contract(page_plan: dict[str, Any]) -> dict[str, Any]:
    """Expose physical capacity and content integrity, never aesthetic planner state."""

    contract = {
        key: page_plan[key]
        for key in (
            "strategy",
            "width_mm",
            "height_mm",
            "min_height_mm",
            "max_height_mm",
            "orientation",
            "density_profile",
            "predicted_occupancy",
            "layout_capacity",
        )
        if page_plan.get(key) not in (None, "", [], {})
    }
    width = page_plan.get("width_mm")
    if isinstance(width, (int, float)) and not isinstance(width, bool) and width > 0:
        contract["readability"] = planning.typography_metrics(float(width))
    return contract


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


def _paper_identity_payload(
    paper_source: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return only verified title-band identity fields."""

    identity_payload = {
        key: str(paper_source.get(key) or "")
        for key in ("title", "authors")
        if paper_source and str(paper_source.get(key) or "").strip()
    }
    venue_identity = paper_source.get("venue_identity") if paper_source else None
    if isinstance(venue_identity, dict):
        identity_payload["venue_identity"] = {
            key: str(venue_identity.get(key) or "")
            for key in (
                "label",
                "distinction",
                "evidence_uri",
                "logo_asset_sha256",
                "logo_asset_token",
            )
            if str(venue_identity.get(key) or "").strip()
        }
    return identity_payload


def _identity_render_slots(
    paper_source: dict[str, Any] | None,
) -> dict[str, str]:
    """Return opaque slots that prevent model transcription errors in identity."""

    slots: dict[str, str] = {}
    if paper_source and str(paper_source.get("title") or "").strip():
        slots["title"] = html_contract.VERIFIED_TITLE_TOKEN
    if paper_source and str(paper_source.get("authors") or "").strip():
        slots["authors"] = html_contract.VERIFIED_AUTHORS_TOKEN
    return slots


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


def repair_manifest(
    *,
    assets: list[dict[str, Any]],
    content_budget: dict[str, Any],
    page_plan: dict[str, Any],
    paper_source: dict[str, Any] | None,
) -> str:
    """Serialize only immutable grounding and integrity needed for HTML repair."""

    page_contract = _page_contract(page_plan)
    raw_content_contract = page_plan.get("content_contract")
    content_contract = (
        dict(raw_content_contract) if isinstance(raw_content_contract, dict) else {}
    )
    selected_assets = _selected_authoring_assets(assets, page_plan=page_plan)
    asset_bindings = [
        {
            key: item.get(key)
            for key in (
                "token",
                "content_sha256",
                "source_kind",
                "figure_number",
                "page",
            )
            if item.get(key) not in (None, "")
        }
        for item in selected_assets
    ]
    payload = {
        "paper_identity": _paper_identity_payload(paper_source),
        "identity_render_slots": _identity_render_slots(paper_source),
        "page_contract": page_contract,
        "content_contract": content_contract,
        "content_budget": content_budget,
        "asset_bindings": asset_bindings,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def revision_prompt(
    *,
    source_html: str,
    feedback: str,
    selection: dict[str, Any] | None,
    page_plan: dict[str, Any] | None = None,
    allow_adaptive_height: bool = False,
    design_reference: reference_seeds.ReferenceBundle | None = None,
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
The bound visual design plan and reference pixels guide the revision. Reapply their topology, density,
typography, palette relationships, section_treatment, focal_strategy, figure_strategy, reading_path,
observations, directives, and hierarchy to the actual content geometry. For a whole-page request, reconstruct
layout wrappers and placement as needed; do not substitute a generic scaffold or style inferred from
venue identity or page dimensions.
Treat visual-review targets as observation anchors, not a whitelist of wrappers that may move. Its
visible evidence and whole-page acceptance outcome define the problem; any suggested exact placement
is advisory. Repack other intact modules when needed, and verify the completed composition rather than
transferring a void, crowding, or weak hierarchy from one zone to another.

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
    reference_text = json.dumps(
        {
            "source_kind": design_reference.source_kind,
            "seed_id": design_reference.seed_id,
            "image_sha256": design_reference.image_sha256,
            "non_authoritative_policy": design_reference.non_authoritative_policy,
        }
        if design_reference is not None
        else {},
        ensure_ascii=False,
        sort_keys=True,
    )
    visual_design_text = json.dumps(
        design.to_dict(), ensure_ascii=False, sort_keys=True
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
        "Non-authoritative design-reference metadata:\n"
        f"{reference_text}\n\n"
        "Bound reference-aware visual design plan:\n"
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
Return exactly one complete <style>...</style> element with no Markdown or commentary.
{page_instruction}
{capacity_instruction}
The host will replace only the current style element, so all scientific text, figures, ids, semantic roles, source hashes, masthead content, and DOM order remain byte-for-byte unchanged.
Treat this as a corrective edit: preserve every existing stylesheet rule that does not need to change for the supplied feedback, and do not restyle unaffected regions.
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
The bound visual design plan and reference-derived relationships guide the stylesheet. Correct it toward
their topology, density, typography, palette relationships, section_treatment, focal_strategy, figure_strategy, reading_path,
observations, and directives without changing the DOM or inventing a generic style. Keep content rows
intrinsic, prose children shrinkable, figures readable, and all content inside the poster. Judge
min-size and track choices by the rendered content; do not conceal clipping, overlap, or filler.
{typography_guidance}
Keep MathML in its native formatting context with an explicit physical font size; never set display
or overflow on the math[data-content-role="equation"] root, and style an unmarked wrapper instead. Use print-safe CSS
only and no external resources or generated content."""
    visual_design_text = json.dumps(
        design.to_dict(), ensure_ascii=False, sort_keys=True
    )
    user = (
        f"Feedback:\n{feedback}\n\n"
        f"Bound reference-aware visual design plan:\n{visual_design_text}\n\n"
        f"Current complete HTML:\n{source_html}"
    )
    return system, user


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


def _asset_prompt(assets: list[dict[str, Any]]) -> str:
    if not assets:
        return (
            "No supplied figure assets. Create diagrams only with inert HTML/CSS/SVG "
            "grounded in the source."
        )
    return (
        "Each line begins with an exact inert image src token. Copy that token verbatim "
        'inside a quoted <img src="asset://N"> attribute. Never use the filename, local '
        "path, description, placeholder, braces, or a data URI as src.\n"
        + "\n".join(
            f'- exact src="{item["token"]}" — '
            f"{_concise_asset_description(item)} ({item['mime']})"
            for item in assets
        )
    )


def _selected_authoring_assets(
    assets: list[dict[str, Any]],
    *,
    page_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    """Expose only selected paper figures plus caller-supplied assets."""

    contract = page_plan.get("content_contract")
    contract = contract if isinstance(contract, dict) else {}
    planned = contract.get("source_figure_sha256s")
    selected_figures = (
        {str(value) for value in planned if str(value)}
        if isinstance(planned, list)
        else set()
    )
    if not selected_figures:
        modules = contract.get("modules")
        for module in modules if isinstance(modules, list) else []:
            if not isinstance(module, dict):
                continue
            hashes = module.get("source_figure_sha256s")
            if isinstance(hashes, list):
                selected_figures.update(str(value) for value in hashes if str(value))
    return [
        item
        for item in assets
        if item.get("source_kind") != "pdf_figure"
        or str(item.get("content_sha256") or "") in selected_figures
    ]


def _concise_asset_description(item: dict[str, Any]) -> str:
    """Keep the extracted caption while dropping long paper-discussion context."""

    description = str(item.get("description") or item.get("filename") or "asset")
    description = description.split("paper discussion:", 1)[0].strip().rstrip(".")
    return description[:600].rstrip()
