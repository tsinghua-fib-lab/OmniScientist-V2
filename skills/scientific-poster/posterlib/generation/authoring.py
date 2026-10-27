"""Grounded authoring prompts and initial scientific-content HTML."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from html import escape
from typing import Any

import image_assets
from lxml import html as lxml_html

from posterlib.content import planning
from posterlib.content.equations import (
    latex_to_mathml,
    render_inline_math,
)
from posterlib.sources import paper_identity
from posterlib.visual import design_direction, reference_analysis

_COPY_SCOPE = """Visible poster copy serves the research argument and scholarly attribution:
identity and publication metadata, bibliographic references, acknowledgments and required
reproduction credits. Judge each passage by its subject: attribution identifies the study
and its published version; production records describe the process of creating this poster.
Retain source-supported research provenance, experimental conditions and scientific
qualifications wherever they occur. Paraphrases preserve the source's scope, aggregation
level and degree of certainty, including its limitations. Shared headings, units and
labels apply to every comparison item they govern; differing conditions stay with
their corresponding items.
The workflow owns production records and delivers them as separate records. Bind evidence
through HTML data attributes and source metadata. Descriptions of how this poster was
assembled, checked or exported belong to those records. A request for traceable deliverables
uses that existing record channel."""

_ACADEMIC_COMPOSITION = _COPY_SCOPE + "\n\n" + """Compose for reading a scientific argument. Use a compact identity band,
clear section labels and regular-weight body text with comfortable line measures. Reserve
display styling and strong weight for brief headings and selected emphasis. Interpret each
reference treatment in its observed role: a display face describes display text; body and
table text need their own readable treatment. Missing body text in a reference leaves that
role unspecified. The reference informs relationships; the current evidence sets the layout.
Allocate enough width to the source figures and tables for their labels and comparisons.
Choose a restrained, consistent emphasis system, with the scientific evidence carrying the
visual interest.

Read the current scientific HTML: text, table cells, method steps and
notation explain what the retained carrier must communicate. Make the focal relationship
visually inspectable and give it a distinct scale and continuous area. Compose the insides of
the scientific modules as well as the page around them. A method-flow is a native schematic:
nodes, connectors and spatial grouping express the source-supported relationships.
Use concise labels as the nodes' reading entries and keep explanatory detail subordinate.
Build new schematics with native drawing components or HTML text and CSS boxes and lines.
Use solid fills, borders and explicit vector geometry for painted marks and connectors.
CSS background images, including gradients, require replacement with these editable
primitives. Keep paper-original images in img elements with their source bindings.
Inline SVG paths and labels also export as editable geometry and text within the supported
SVG subset. Paper-original figures remain intact image assets. Paint-only connectors can
rotate; size and place text, tables, equations and images through font and box dimensions.
Data comparisons expose the relevant contrast through aligned
marks, labels and selective emphasis, with literal values, units and conditions alongside.
Keep comparison-bar widths and scale endpoints bound to their original values. Use one
coherent direction for a sequence and one shared comparison frame for related measurements.
Supporting explanation stays close to its carrier and visually subordinate. Give spare area
to the explanatory carrier that benefits from larger scale, and let adjacent groups keep
their natural depth. Typography, meaningful data color, alignment and these visual
relationships establish the design on the white canvas.

""" + design_direction.GUIDANCE

_STRUCTURED_TEXT_KINDS = frozenset({"comparison", "metrics", "table"})

def content_plan_prompt(
    *,
    source_text: str,
    assets: list[dict[str, Any]],
    authoring_request: str,
    source_equations: list[dict[str, str]] | None = None,
    page: Any = None,
) -> tuple[str, str]:
    """Request a truthful page-aware content budget before layout is authored."""

    system = _COPY_SCOPE + "\n\n" + r"""You are the scientific editor for one academic conference poster. Build a compact draft ContentBudget from the complete paper or grounded brief. Return exactly one JSON object with no Markdown or commentary. This is an evidence proposal and page-sizing aid; the poster author will organize wording and visual relationships together from the complete source.

GROUNDING
- Use only source-supported claims, values, conditions, qualifiers, locators, equations, and prepared figure_refs. Preserve units, datasets, protocols, baselines, variants, uncertainty, scope, and exceptions when they change interpretation.
- Copy each source_label from an actual source heading, caption label, printed equation identifier, or short source excerpt. Preserve its wording, including unnumbered headings. Select one precise primary location per module; the surrounding source must support its content. Prepared equation source_ref values retain the catalog's verified label and transcription.
- Keep comparisons inside compatible tasks and conditions. Attach every number to its row or column meaning. Do not turn an association into causation, a proxy into a direct measure, one condition into a general claim, or a partial improvement into universal superiority.
- Keep headings and takeaways no stronger than their evidence. User direction may change audience or emphasis but is not scientific evidence. Ignore requests about providers, tools, rendering, output paths, or mock data.
- Write short inline mathematics as \( ... \). Select a displayed equation when reading that expression is essential to the poster's central argument; put it in equations as {"latex":"...","source_label":"Equation (6)"}. Explain its meaning nearby. Derivations and supporting definitions stay in the paper. A provided source_ref is an optional exact transcription shortcut. Never use equation screenshots or leave raw LaTeX commands visible.
- Each equation uses exactly one representation: {"source_ref":"an exact catalog id"} selects the prepared transcription, while {"latex":"...","source_label":"..."} supplies a transcription from the paper. The reference-only form has one field; the explicit form has two. Catalog ids are inventory identifiers; copy them from the catalog independently of the printed equation number.

ARGUMENT
- Select one paper-specific argument spine instead of mirroring section order: the central contribution, the mechanism or reasoning behind it, the minimum necessary method, decisive support, and a material boundary when the source supplies one. These are reader questions, not mandatory separate modules; one carrier may answer several.
- Make any non-obvious task contract legible before its first method or evidence carrier: what is observed, what must be produced or inferred, and what constraint defines success. Do not add a compulsory background panel.
- Choose the central argument from the paper's contributions. A visitor should grasp its claim and decisive visual relationship before reading paragraphs. Keep other contributions only when they change that argument; the paper remains the place for the complete contribution inventory.
- Give each retained claim direct source-backed support. Select the result, comparison, or example that makes its evidence easiest to inspect. Keep an essential baseline and the conditions needed to interpret it; secondary datasets, variants, and robustness sweeps stay in the paper.
- A module answers one major reader question. Related variants and robustness checks belong together when they support the same conclusion. Different metrics, datasets, or source sections do not by themselves justify separate modules. Never create one module per figure, experiment, theorem, or paper section.
- Plan two reading levels: a quick scan through the central claim, visual mechanism and decisive result, followed by closer reading of essential qualifications. The scan must work without reading every paragraph or formula. Select the strongest carrier at each step, then reserve space for separation and interpretation.
- Write each module as a short descriptive heading, a directly inspectable carrier, and only the reading key or qualification it needs. Body copy uses short complete sentences, each with one job. A figure explains its visible mechanism; a results table supplies the comparison; nearby prose adds the condition or meaning missing from that carrier.

CARRIERS
- Choose the most direct carrier for each module: claim, prepared figure, method-flow, metrics, comparison, native table, short text, bibliographic attribution (provenance), or an exact equation family. Prefer one concise prose channel among text, takeaway, detail_points, or flow_steps. Use another only for a source-supported qualifier that cannot fit with the primary carrier, and never repeat the same message across channels.
- Bind a prepared figure only when its visual relationship carries necessary method or evidence. Use exact supplied figure_refs. Do not bind a figure merely because it exists, and do not use a source figure as decoration. A second figure belongs in the same module only when the pair communicates one inseparable relationship.
- Native drawing components are prepared visual carriers selected through figure_refs. Their labels and relationships explain the grounded source; the supplied paper or brief remains the scientific evidence.
- Compare original figures and upstream components for the same reader question. Select by source fidelity, explanatory clarity, likely label density and required editing scope. Reject a generic or inaccurate candidate even when its producer succeeded. Paper-original bitmap or vector images remain intact assets with image-level editing; native component import preserves slide objects. Confirm physical readability from rendered observations during composition.
- visual_kind names the primary carrier. Related figures, tables, flow steps and explanatory copy may share a module when they answer the same reader question. Use figure for a prepared-image-led module and method-flow for a native sequence with flow_steps=[{"label":"short node","detail":"one direct transition or result"}]. Preserve the source's relationship when choosing connections and direction.
- Method labels name the scientific stages; native list markers own any displayed sequence numbering. Keep details short enough to read as nodes in one relationship.
- Use visual_kind=table for a table-led module. A table is {"columns":[...],"rows":[[...],...]}; rows are non-empty and rectangular. Omit view for a literal table that preserves all cell notation. Choose table.view="comparison" when aligned numeric magnitudes under shared measures are the primary reading task: the first column names the rows, every later cell is a finite nonnegative scalar, and each value column has a positive maximum. The renderer preserves that choice; incompatible cells return planning feedback.
- For a decisive magnitude comparison, select the coherent rows and measures that reveal the contrast. Numeric cells carry the source values and headers carry their shared units; nearby copy preserves the conditions needed to interpret them. Keep uncertainty, missingness and qualifications explicit in a literal table when they are essential to its comparison frame. Independent headline values use metrics; a method sequence uses method-flow. Choose the carrier for the reader's inspection task before writing its explanation.
- Use metrics for independent headline values and comparison for one compact contrast. If a figure and table answer the same question, keep both only when the table supplies a threshold, reversal, or ranking the figure cannot communicate.
- A source figure and a table may serve different reading tasks. Retain both when the central argument needs both tasks. Select a scientifically coherent subset of source table rows or columns when it carries the decisive comparison, preserving labels, units, conditions and baselines.
- If a takeaway compares against a named baseline or prior method, retain that comparator in the visible evidence carrier so the comparison can be checked on the poster.
- A focal carrier must be understandable as the first distance-level entry. A definition or equation that needs earlier context stays primary. A prepared overview or comparison figure may be focal when it directly exposes the paper's new object or mechanism.

OUTPUT CONTRACT
Return exactly {"paper_identity":{"title":"exact paper title","authors":["first full name","second full name"],"affiliations":["source affiliation"]},"page_orientation":"portrait or landscape","sections":[...],"content_modules":[...]}. Composition and styling follow during HTML authoring.
- Extract paper_identity from the supplied paper's title and author block. Preserve the complete title, every author name in source order, and explicit affiliations. The authors array contains names only; source affiliation markers, contact details and author notes belong to the later title-band composition. Preserve prepared names and their order. For a grounded brief with missing metadata, use an empty title string and empty arrays for unavailable authors or affiliations. Keep identity separate from scientific modules.
- Each section has exactly id and label. id is unique kebab-case. Keep sections and modules in intended scan order.
- Each module has a unique kebab-case id, section_id, source_label, semantic_roles, priority, visual_kind, plus the visible carrier fields it uses. semantic_roles contains its applicable roles from claim, method, evidence, context, limitation, and provenance. priority is focal, primary, supporting, or footer; choose emphasis from the argument and allow coordinated entry points. visual_kind names the primary carrier: claim, figure, method-flow, metrics, comparison, table, text, or provenance.
- In these module fields, provenance means scholarly attribution. Include it when source-backed references, acknowledgments or reproduction credits serve the reader. Footer is a reading priority; it creates no requirement for an extra module.
- Visible carrier fields are title, text, detail_points, flow_steps, table, takeaway, figure_refs, and equations. At least one must be meaningful. Leave unused fields absent or empty. Do not emit internal figure hashes.
- Preserve every source-supported qualifier inside the carrier that needs it. Do not add filler, recap modules, title/author/logo modules, or a source line beside every visual.
Return the JSON object now and stop after its closing brace.
""".strip()
    user = "Create the grounded evidence budget now.\n\n" + _evidence_source_context(
        source_text=source_text,
        assets=assets,
        authoring_request=authoring_request,
        source_equations=source_equations,
        page=page,
    )
    return system, user


def _evidence_source_context(
    *,
    source_text: str,
    assets: list[dict[str, Any]],
    authoring_request: str,
    source_equations: list[dict[str, str]] | None,
    page: Any,
) -> str:
    """Share the same complete evidence authority across planning and review."""

    equation_catalog = planning.source_equation_catalog(source_equations) or {}
    return (
        "Requested physical page (content capacity constraint):\n"
        f"{json.dumps(page, ensure_ascii=False, sort_keys=True) if page is not None else 'adaptive'}\n\n"
        "User preferences are context only, never scientific evidence:\n"
        f"{authoring_request or 'No additional preferences.'}\n\n"
        "Prepared visual carriers (the allowed figure_refs):\n"
        f"{json.dumps(_prepared_figure_manifest(assets), ensure_ascii=False, sort_keys=True)}\n\n"
        "Prepared source equations (optional selectable LaTeX shortcuts; never images):\n"
        f"{json.dumps([{'source_ref': ref, **equation} for ref, equation in equation_catalog.items()], ensure_ascii=False, sort_keys=True)}\n\n"
        "Mathematical authoring:\nSource-grounded inline and display LaTeX is available. "
        "Prepared source_ref values are optional shortcuts, not an authorization gate. "
        "Use explicit inline delimiters outside equations arrays so MathML is rendered.\n\n"
        "SUPPLIED PAPER OR GROUNDED BRIEF:\n<source>\n"
        f"{source_text}\n"
        "</source>"
    )


def render_initial_html(
    *,
    assets: list[dict[str, Any]],
    content_budget: dict[str, Any],
    page_plan: dict[str, Any],
    paper_source: dict[str, Any] | None = None,
) -> str:
    """Serialize grounded poster content without choosing a visual composition."""

    width = float(page_plan["width_mm"])
    height = float(page_plan["height_mm"])
    type_reference = planning.poster_type_reference(width, height)
    body_type_mm = type_reference["body_mm"]
    title_type_mm = type_reference["title_mm"]
    identity = {
        **(paper_source or {}),
        **paper_identity.merge(paper_source or {}, content_budget.get("paper_identity", {})),
    }
    source_title = str(identity.get("title") or "Scientific poster")
    authors = str(identity.get("authors") or "").strip()
    author_markup = (
        f'<p class="poster-authors" data-poster-authors="verified">{escape(authors)}</p>'
        if authors
        else ""
    )
    raw_affiliations = identity.get("affiliations")
    affiliations = (
        [str(value).strip() for value in raw_affiliations if str(value).strip()]
        if isinstance(raw_affiliations, (list, tuple))
        else [str(raw_affiliations).strip()]
        if str(raw_affiliations or "").strip()
        else []
    )
    affiliation_markup = (
        '<p class="poster-affiliations" data-poster-affiliations="verified">'
        + escape(" · ".join(affiliations))
        + "</p>"
        if affiliations
        else ""
    )
    note_markup = "".join(
        '<p class="poster-author-notes">'
        + escape(", ".join(note["authors"]) + ": " + note["text"])
        + "</p>"
        for note in identity.get("author_notes", [])
    )
    sections = {
        str(item["id"]): str(item["label"])
        for item in content_budget["sections"]
    }
    asset_by_hash = {
        str(item.get("content_sha256") or ""): item
        for item in assets
        if str(item.get("content_sha256") or "")
    }
    seen_sections: set[str] = set()
    modules: list[str] = []
    for module in content_budget["content_modules"]:
        section_id = str(module["section_id"])
        modules.append(
            _render_module(
                module,
                section_label=sections[section_id],
                show_section_label=section_id not in seen_sections,
                asset_by_hash=asset_by_hash,
                body_type_mm=body_type_mm,
            )
        )
        seen_sections.add(section_id)
    css = (
        f"@page{{size:{width:g}mm {height:g}mm;margin:0}}"
        f"*{{box-sizing:border-box}}html,body{{width:{width:g}mm;height:{height:g}mm;margin:0}}"
        f"[data-poster-root]{{width:{width:g}mm;height:{height:g}mm;overflow:hidden;"
        "display:flex;flex-direction:column;padding:18mm;background:#fff;color:#182535;"
        "font-family:Arial,sans-serif}"
        "[data-poster-title-band]{flex:none;border-bottom:3mm solid #17638b;"
        "padding-bottom:6mm;margin-bottom:8mm}"
        "[data-poster-title-band] h1{line-height:1.08;margin:0 0 3mm}"
        f"[data-poster-title-band] p{{font-size:{body_type_mm:g}mm;margin:0 0 2mm}}"
        ".poster-body{flex:1;min-height:0;column-count:2;column-gap:12mm;"
        "column-fill:balance}"
        ".poster-module{display:inline-block;width:100%;vertical-align:top;"
        "break-inside:avoid;margin:0 0 6mm;padding:4mm;"
        "border-top:1.5mm solid #17638b}"
        f".poster-module h2{{font-size:{type_reference['heading_mm']:g}mm;"
        "line-height:1.15;margin:0 0 3mm;color:#17638b}"
        f".poster-module p,.poster-module li{{font-size:{body_type_mm:g}mm;"
        "line-height:1.25;margin:0 0 2mm}"
        ".poster-module figure{margin:2mm 0}.poster-module img{max-width:100%;height:auto}"
        f".poster-module table{{width:100%;border-collapse:collapse;font-size:{body_type_mm * 0.9:g}mm}}"
        ".poster-module th,.poster-module td{padding:1.5mm;border-bottom:0.3mm solid #bbc9d2}"
        ".poster-module figcaption,.poster-module .section-label{font-size:7mm}"
    )
    html_text = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(source_title)}</title><style>{css}</style></head><body>"
        '<main data-poster-root data-poster-id="poster" data-content-carrier>'
        '<header data-poster-title-band><div class="title-copy">'
        f'<h1 data-poster-title="verified" style="font-size:{title_type_mm:g}mm;text-wrap:balance">'
        f'{render_inline_math(source_title)}</h1>'
        f"{author_markup}{affiliation_markup}{note_markup}</div>{_render_venue_identity(identity)}</header>"
        f'<div class="poster-body">{"".join(modules)}</div>'
        "</main></body></html>"
    )
    return lxml_html.tostring(
        lxml_html.document_fromstring(html_text),
        encoding="unicode", method="html", pretty_print=True,
        doctype="<!doctype html>",
    )


def _render_module(
    module: dict[str, Any],
    *,
    section_label: str,
    asset_by_hash: dict[str, dict[str, Any]],
    body_type_mm: float,
    show_section_label: bool = True,
) -> str:
    """Serialize one grounded module without choosing its visual placement."""

    module_id = str(module["id"])
    priority = str(module["priority"])
    visual_kind = str(module["visual_kind"])
    roles = " ".join(str(item) for item in module["semantic_roles"])
    attributes = {
        "id": module_id,
        "data-poster-module": module_id,
        "data-poster-id": module_id,
        "data-semantic-roles": roles,
        "data-module-priority": priority,
        "data-visual-kind": visual_kind,
        "data-source-label": str(module["source_label"]),
    }
    attribute_text = " ".join(
        f'{name}="{escape(value, quote=True)}"'
        for name, value in attributes.items()
        if value
    )
    title = str(module.get("title") or section_label)
    section_markup = (
        f'<p class="section-label">{render_inline_math(section_label)}</p>'
        if section_label and show_section_label
        else ""
    )
    header = f"{section_markup}<h2>{render_inline_math(title)}</h2>"
    text = str(module.get("text") or "").strip()
    details = [str(item).strip() for item in module.get("detail_points") or []]
    takeaway = str(module.get("takeaway") or "").strip()
    figures = "".join(
        _render_figure(
            asset_by_hash[digest],
            digest=digest,
            show_caption=not _has_figure_interpretation(module),
        )
        for digest in (str(item) for item in module.get("figure_sha256s") or [])
        if digest in asset_by_hash
    )
    figure_markup = f'<div class="figure-group">{figures}</div>' if figures else ""
    text_markup = (
        f'<p class="module-text" data-poster-decor data-content-role="text">'
        f"{render_inline_math(text)}</p>"
        if text
        else ""
    )
    details_markup = render_detail_points(details, visual_kind=visual_kind)
    flow_markup = render_flow_steps(module.get("flow_steps") or [])
    if module.get("table"):
        details_markup = _render_table(module["table"]) + details_markup
    equations = "".join(
        _render_equation(item, body_type_mm=body_type_mm)
        for item in module.get("equations") or []
        if isinstance(item, dict) and str(item.get("latex") or "").strip()
    )
    takeaway_markup = (
        '<p class="takeaway module-deck" data-poster-decor '
        f'data-content-role="takeaway">{render_inline_math(takeaway)}</p>'
        if takeaway
        else ""
    )
    body_markup = (
        f"{takeaway_markup}{figure_markup}{text_markup}{details_markup}{equations}{flow_markup}"
    )
    classes = f"poster-module priority-{priority} kind-{visual_kind}"
    return (
        f'<section class="{escape(classes)}" {attribute_text}>'
        f'<header class="module-header">{header}</header>{body_markup}</section>'
    )
def _render_table(table: dict[str, Any]) -> str:
    """Emit shared headers and editable cells without inferring scientific data."""

    if table.get("view") == "comparison":
        return _render_table_comparison(table)
    header = "".join(
        f'<th scope="col">{render_inline_math(cell)}</th>' for cell in table["columns"]
    )
    rows = "".join(
        '<tr><th scope="row">'
        + render_inline_math(row[0])
        + "</th>"
        + "".join(f"<td>{render_inline_math(cell)}</td>" for cell in row[1:])
        + "</tr>"
        for row in table["rows"]
    )
    return (
        '<table class="evidence-table" data-content-role="table">'
        f"<thead><tr>{header}</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _render_table_comparison(table: dict[str, Any]) -> str:
    """Render the explicitly selected numeric carrier without changing its kind."""

    headers, rows = table["columns"], table["rows"]
    values = planning.comparison_values(table)
    maxima = [max(column) for column in zip(*values, strict=True)]
    endpoints = [
        rows[max(range(len(rows)), key=lambda row: values[row][column])][column + 1]
        for column in range(len(maxima))
    ]
    header = f'<strong data-poster-text role="columnheader">{render_inline_math(headers[0])}</strong>'
    header += "".join(
        '<div class="comparison-measure-header"><strong data-poster-text role="columnheader">'
        + render_inline_math(label)
        + '</strong><div class="comparison-scale"><span>0</span><span>'
        + escape(endpoint)
        + "</span></div></div>"
        for label, endpoint in zip(headers[1:], endpoints, strict=True)
    )
    body = []
    for row, numbers in zip(rows, values, strict=True):
        cells = "".join(
            '<div class="comparison-cell" role="cell">'
            '<div class="comparison-track" aria-hidden="true">'
            f'<div class="comparison-bar" data-comparison-fraction="{float(number / maximum):.8g}" '
            f'style="width:{float(number / maximum) * 100:.8g}%"></div>'
            '</div><span class="comparison-value">'
            + escape(cell)
            + "</span></div>"
            for cell, number, maximum in zip(row[1:], numbers, maxima, strict=True)
        )
        body.append(
            '<div class="comparison-row" role="row">'
            f'<span role="rowheader">{render_inline_math(row[0])}</span>{cells}</div>'
        )
    return (
        '<div class="evidence-comparison" data-content-role="table" '
        f'role="table" style="--comparison-count:{len(maxima)}">'
        f'<div class="comparison-header" role="row">{header}</div>'
        + "".join(body)
        + "</div>"
    )


def render_detail_points(
    details: list[str],
    *,
    visual_kind: str,
) -> str:
    """Render truthful evidence atoms using their declared presentation channel."""

    if not details:
        return ""
    class_name = (
        "evidence-points evidence-cards"
        if visual_kind in _STRUCTURED_TEXT_KINDS
        else "evidence-points evidence-callouts"
    )
    items = [
        '<li data-poster-decor><span class="evidence-copy">'
        f"{render_inline_math(item)}</span></li>"
        for item in details
    ]
    return (
        f'<ul class="{class_name}" data-content-role="detail-points">'
        f'{"".join(items)}</ul>'
    )


def render_flow_steps(steps: list[dict[str, str]]) -> str:
    """Render explicit method nodes without inventing their visual topology."""

    if not steps:
        return ""
    items = []
    for index, step in enumerate(steps):
        connector = (
            '<span class="method-flow-connector" aria-hidden="true"></span>'
            if index < len(steps) - 1
            else ""
        )
        items.append(
            '<li class="method-flow-step" data-poster-decor>'
            '<span class="evidence-copy">'
            '<strong class="method-flow-label" data-content-role="flow-label">'
            f'{render_inline_math(step["label"])}</strong>'
            '<span class="method-flow-detail" data-content-role="flow-detail">'
            f'{render_inline_math(step["detail"])}</span></span>{connector}</li>'
        )
    return (
        '<ol class="evidence-points evidence-cards method-flow" '
        f'data-content-role="flow-steps">{"".join(items)}</ol>'
    )


def _has_figure_interpretation(module: dict[str, Any]) -> bool:
    """A planned module interpretation replaces paper-original figure captions."""

    return bool(
        module.get("figure_sha256s")
        and (
            any(
                str(module.get(field) or "").strip()
                for field in ("text", "takeaway")
            )
            or any(str(item).strip() for item in module.get("detail_points") or [])
            or any(
                str(step.get("detail") or "").strip()
                for step in module.get("flow_steps") or []
                if isinstance(step, Mapping)
            )
        )
    )


def _render_figure(
    asset: dict[str, Any],
    *,
    digest: str,
    show_caption: bool = True,
) -> str:
    """Render one prepared carrier with its source or native-package identity."""

    structured_caption = str(asset.get("caption") or "").strip()
    caption = _brief_figure_caption(structured_caption) if structured_caption else ""
    raw_number = asset.get("figure_number")
    figure_label = (
        f"Fig. {int(raw_number)}"
        if isinstance(raw_number, (int, float)) and int(raw_number) > 0
        else str(asset.get("carrier_label") or "").strip()
    )
    description = " — ".join(item for item in (figure_label, caption) if item)
    visible_caption = caption if show_caption else ""
    native_component = asset.get("source_kind") == "native_component"
    if not description:
        description = (
            str(asset.get("description") or "Editable drawing component")
            if native_component else "Source visual from the supplied paper"
        )
    aspect_ratio = image_assets.asset_aspect_ratio(asset)
    aspect_ratio_attribute = (
        f' data-source-figure-aspect-ratio="{aspect_ratio:.12g}"'
        if aspect_ratio > 0 and not native_component
        else ""
    )
    identity_attribute = (
        "data-native-component-sha256" if native_component else "data-source-figure-sha256"
    )
    return (
        f'<figure class="{"native-component" if native_component else "source-figure"}">'
        f'<img src="{escape(str(asset.get("token") or ""), quote=True)}" '
        f'{identity_attribute}="{escape(digest, quote=True)}" '
        f"{aspect_ratio_attribute} "
        'style="width:100%;height:auto;display:block" '
        f'alt="{escape(description, quote=True)}">'
        + (
            f'<figcaption data-content-role="caption">{render_inline_math(visible_caption)}</figcaption>'
            if visible_caption
            else ""
        )
        + "</figure>"
    )


def _brief_figure_caption(caption: str) -> str:
    """Normalize one structured caption without reinterpreting its wording."""

    brief = " ".join(str(caption).split())
    brief = brief.replace(". . .", "…")
    for punctuation in ",;:":
        brief = brief.replace(" " + punctuation, punctuation)
    if len(brief) > 210:
        brief = brief[:207].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return brief


def _render_equation(
    equation: dict[str, Any],
    *,
    body_type_mm: float,
) -> str:
    latex = str(equation.get("latex") or "").strip()
    mathml = latex_to_mathml(
        latex,
        attributes={
            "data-content-role": "equation",
            "data-latex": latex,
            "data-source-label": str(equation.get("source_label") or "").strip(),
        },
    )
    return (
        '<div class="equation-shell" data-poster-decor '
        f'style="font-size:{body_type_mm:g}mm">{mathml}</div>'
    )


def _render_venue_identity(identity: dict[str, Any]) -> str:
    venue = identity.get("venue_identity")
    if not isinstance(venue, dict):
        return ""
    logo = str(venue.get("logo_asset_token") or "").strip()
    distinction = str(venue.get("distinction") or "").strip()
    description = " · ".join(
        str(venue.get(key) or "").strip()
        for key in ("label", "distinction")
        if str(venue.get(key) or "").strip()
    )
    logo_markup = (
        f'<img src="{escape(logo, quote=True)}" data-poster-venue-logo '
        f'alt="{escape(description or "Conference logo", quote=True)}">'
        if logo
        else ""
    )
    visible_label = distinction if logo else description
    label_markup = f"<span>{escape(visible_label)}</span>" if visible_label else ""
    return (
        f'<div class="venue-block" data-poster-venue="verified">{logo_markup}{label_markup}</div>'
        if logo_markup or label_markup
        else ""
    )


def _reference_analysis_text(plan: reference_analysis.ReferenceAnalysis) -> str:
    """Serialize model-owned design semantics without inventing layout tokens."""

    if not plan.design:
        return (
            "No pixel-derived reference observations are available. Compose from the "
            "grounded evidence, physical page, and academic-poster quality requirements; "
            "do not infer visual details from the unavailable reference analysis."
        )
    return json.dumps(plan.design, ensure_ascii=False, sort_keys=False, indent=2)


def _typography_guidance(page_plan: dict[str, Any] | None) -> str:
    """Describe physical type targets without turning them into a static gate."""

    width = (page_plan or {}).get("width_mm")
    if isinstance(width, bool) or not isinstance(width, (int, float)) or width <= 0:
        return (
            "Use viewing-distance body type and readable line measures. Resolve crowding "
            "through hierarchy and reflow while preserving the frozen content."
        )
    metrics = planning.typography_metrics(float(width))
    title_min = float(metrics["title_min_mm"])
    body_min = float(metrics["body_min_mm"])
    return (
        "Compose the main argument for a visitor standing about one metre from the printed "
        "page. Size essential equations, table labels and explanatory copy for that reading "
        "task. Necessary conditions and limitations retain readable scale even in a supporting "
        "or footer module. Native MathML subscripts and superscripts keep their mathematical "
        "scaling; assess the complete expression's base size and its essential marks together. "
        "At this physical page width, use a clear viewing-distance hierarchy. Keep the "
        f"title visually dominant and near {title_min:g} mm when its length "
        "and the actual masthead occupants allow; a balanced multi-line title may be "
        "clearer than shrinking a long title into one line. Compare the apparent type "
        "scale with the reference at equal page height; a physically larger poster may "
        "need larger type than these width-based starting sizes. Use section headings at least about "
        f"{metrics['section_heading_min_mm']:g} mm, normal body copy at least "
        f"{body_min:g} mm and near {metrics['body_target_mm']:g} mm, and "
        f"subordinate captions or bibliographic references no smaller than about "
        f"{metrics['provenance_min_mm']:g} mm. These are advisory authoring targets, not "
        "a static pass/fail gate. Declare principal role sizes in CSS mm so their physical "
        "scale is explicit. If you use pt, convert it correctly: 1pt is only 0.3528mm, so "
        f"{title_min:g}mm is about {title_min * 72 / 25.4:.1f}pt and {body_min:g}mm is about "
        f"{body_min * 72 / 25.4:.1f}pt. Resolve crowding through line measure, hierarchy, "
        "and reflow within the current edit authority. Check computed browser sizes, including "
        "inherited and inline styles, against each element's reading role after composing."
    )


def revision_prompt(
    *,
    source_html: str,
    feedback: str,
    selection: dict[str, Any] | None,
    page_plan: dict[str, Any] | None = None,
    content_brief: dict[str, Any] | None = None,
    revision_mode: str = "full-layout",
    content_replan_targets: list[str] | None = None,
    source_text: str = "",
    source_figure_observations: Mapping[str, Mapping[str, str]] | None = None,
    required_visual_operation: str = "",
    reference_brief: reference_analysis.ReferenceAnalysis | None = None,
    design_requirements: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Build a model-authored exact-edit prompt for the current poster."""

    if revision_mode not in {"full-layout", "content-replan"}:
        raise ValueError("revision_mode must be full-layout or content-replan")
    if required_visual_operation not in {"", "restyle", "reflow"}:
        raise ValueError(
            "required_visual_operation must be empty, restyle, or reflow"
        )
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
        raise ValueError("content-replan requires at least one copy target")
    if content_replan:
        copy_authority = (
            "Each named module target authorizes prose only inside that module, including "
            "headings, captions, diagram labels and ordinary DOM text. The 'poster' target "
            "authorizes only prose outside grounded modules and verified identity. Module "
            "copy requires its own module target. Use the bound source to clarify the requested "
            "scientific explanation while preserving its claims, conditions, limitations, "
            "uncertainty and attribution. Keep original figures, native drawing components, "
            "literal table data and native equations intact."
        )
        if required_visual_operation == "reflow":
            layout_authority = (
                "The same receipt also requests visual reflow, so recompose the full "
                "wrapper and CSS layout as needed while preserving non-target scientific "
                "copy and every binding. Use the layout-shell tool when wrapper topology and "
                "stylesheet must change together; the host reinserts exact scientific nodes. "
                "Keep each placeholder empty and put layout classes on its retained parent wrapper."
            )
        elif required_visual_operation == "restyle":
            layout_authority = (
                "The same receipt also requests restyling. Preserve wrapper topology and "
                "replace the complete stylesheet after the targeted copy edits."
            )
        else:
            layout_authority = (
                "Adjust layout and styling to support the requested revision, keeping "
                "related explanation and evidence together."
            )
        evidence_boundary = (
            f"{copy_authority} Authorized copy targets: "
            f"{', '.join(targets)}. Keep all non-target scientific copy unchanged. Keep "
            "every equation, identity field, source label, figure inventory, module id, "
            "semantic role, priority, and visual kind unchanged. Do not delete modules or "
            f"replace figures. {layout_authority}"
        )
    else:
        evidence_boundary = (
            "Preserve the exact scientific snapshot: module ids, visible module content, "
            "source labels, priorities, visual kinds, source-figure hashes, equations and "
            "data-latex, authors, venue identity, and logo binding."
        )
    reference_observations = (
        _reference_analysis_text(reference_brief)
        if reference_brief is not None and reference_brief.design
        else ""
    )
    design_state_guidance = (
        "Current HTML is the operational design state. The supplied initial reference "
        "observations retain the user's design context across edits; they are not a new "
        "restyle request or a fixed target layout. Follow the requested reference fidelity "
        "while repairing the current composition, and retain sound decisions outside the "
        "requested change. Current explicit user changes take precedence over earlier "
        "design context."
        if reference_observations
        else "Current HTML is the design state for this revision. Preserve its coherent "
        "visual identity and every accepted strength unless the bound feedback requires "
        "a change. The reference was interpreted during initial authoring; use the "
        "current composition rather than inventing a missing reference design."
    )
    typography_guidance = _typography_guidance(page_plan)
    copy_channels = (
        copy_channel_inventory(source_html, targets) if content_replan else {}
    )
    if content_replan:
        edit_sequence = (
            "Use update_poster_module_copy for a listed explanatory-copy channel. Use an "
            "exact replacement for other prose within the copy authority above.\n"
            "Copy-channel inventory: "
            + json.dumps(copy_channels, ensure_ascii=False, sort_keys=True)
        )
    else:
        edit_sequence = ""
    system = f"""Revise the current scientific poster using the editor tools. Read each returned check before
editing again or finishing. For authorized explanatory-copy changes, use update_poster_module_copy so
the host preserves the module structure. Use exact edits for other local changes, replace the governing
stylesheet for styling, and use replace_poster_layout when wrapper topology and CSS must change together.
Work from the latest HTML, not an earlier draft.

{design_state_guidance}
Explicit user scope takes precedence over visual suggestions; retain unchanged user requirements.
{page_instruction}
{evidence_boundary}
{edit_sequence}

Repair the stated reading problem using the current screenshot and measurements. Targets identify the
problem's owners; necessary surrounding wrappers may change. Choose the composition, grouping and sizes
that make the evidence clear on this physical page.
{_ACADEMIC_COMPOSITION}
Source images may be enlarged or reduced intact with contain fitting; preserve their pixels and bindings.
Give each image a real allocated box, rather than relying on small intrinsic dimensions or maximum-size caps.
Use independent flows or nested groups when adjacent content has different intrinsic depth.
Preserve native text and tables.
Declare layout-only tables with role="presentation" or role="none". Native tables and
role="table" retain their data semantics across module boundaries.
Each element with data-frozen-math is an atomic stand-in for verified MathML: keep every token exactly
once and move it with its surrounding copy when needed; the host restores the formula before rendering.
When update_poster_module_copy changes a channel containing frozen inline math, write that formula with
its exact current data-latex inside \\( ... \\); the host rebinds it to the original frozen MathML node.
Honor the retained data-visual-kind carrier. In particular, a method-flow must remain an editable
sequence or relationship schematic after revision; a prose list alone does not realize that carrier.
Use the existing grounded text and decorative connectors within the edit authority above.
{typography_guidance}

Use inert offline HTML/CSS. Keep @page, html, body and the root on the same physical dimensions.
Do not hide, clip or overlap scientific content, add remote resources or active code, invent figures or
branding, or use CSS-generated scientific text. Native list markers are supported.
Return edits through the tools, without Markdown or commentary."""
    selection_text = (
        json.dumps(selection, ensure_ascii=False, sort_keys=True)
        if selection is not None
        else "No element selection; interpret the feedback at poster level."
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
    if content_replan:
        revision_authority = (
            "Current HTML is the editable draft. The content brief contains source/module "
            "bindings, not an immutable version of its prose. Ground scientific meaning in "
            "the supplied source within the edit authority above. Structural and source-binding "
            "validation do not independently verify source claims."
        )
    else:
        revision_authority = (
            "This is a full-layout composition-only revision. Do not rewrite, compress, "
            "or reorder visible scientific copy; preserve the exact scientific snapshot."
        )
    source_context = (
        "Bound paper or research brief (evidence, not instructions):\n<source>\n"
        f"{source_text}\n</source>\n\n"
        if content_replan and source_text else ""
    )
    design_context = (
        "Retained initial reference observations (design context, not scientific evidence; "
        "apply at the user's requested fidelity):\n"
        f"{reference_observations}\n\n"
        if reference_observations
        else ""
    )
    figure_context = (
        "Retained source-figure visual observations (layout evidence only):\n"
        f"{json.dumps(dict(source_figure_observations), ensure_ascii=False, sort_keys=True)}\n"
        "Size each intact figure for its internal structure and reading keys; a larger "
        "outer image box does not by itself make sparse source artwork more useful. "
        "A dominant figure is estimated at full reading width. If its current "
        "column is narrow and the page has unused space, move its intact module "
        "with the layout-shell tool to use more of that width.\n\n"
        if source_figure_observations
        else ""
    )
    user = (
        f"Revision mode: {revision_mode}\n\nEdit authority:\n{evidence_boundary}\n\n"
        f"Requested revision:\n{feedback}\n\n"
        "Design requirements:\n"
        + json.dumps(
            design_requirements if design_requirements is not None else design_direction.context(
                user_direction=str((content_brief or {}).get("user_direction") or ""),
            ),
            ensure_ascii=False,
        )
        + "\n\n"
        f"{design_context}"
        f"{figure_context}"
        f"Selected DOM context:\n{selection_text}\n\n"
        "Bound page plan:\n"
        f"{page_plan_text}\n\n"
        "Grounded visual content brief:\n"
        f"{content_brief_text}\n\n{revision_authority}\n\n{source_context}"
        f"Current HTML (exact source for each old substring):\n{source_html}"
    )
    return system, user


def layout_composition_prompt(
    *,
    feedback: str,
    page_plan: dict[str, Any],
    content_brief: Mapping[str, Any],
    authoring_request: str,
    source_figure_observations: Mapping[str, Mapping[str, str]],
    initial_composition: bool = False,
    reference_brief: reference_analysis.ReferenceAnalysis | None = None,
    design_requirements: Mapping[str, Any] | None = None,
    source_text: str = "",
    assets: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Guide initial composition or edits from compact, measured layout state."""

    authority = content_brief.get("grounded_authority")
    modules = content_brief.get("content_modules")
    if not isinstance(modules, list):
        modules = authority.get("content_modules") if isinstance(authority, Mapping) else []
    figure_needs = {
        str(module["id"]): [
            source_figure_observations[digest]["width_need"]
            for digest in module.get("figure_sha256s") or []
            if source_figure_observations.get(digest, {}).get("width_need")
        ]
        for module in modules or [] if isinstance(module, Mapping)
    }
    dominant_modules = [
        module_id for module_id, needs in figure_needs.items()
        if "dominant" in needs
    ]
    design = (reference_brief.design or {}) if reference_brief else {}
    type_reference = planning.poster_type_reference(
        float(page_plan["width_mm"]), float(page_plan["height_mm"])
    )
    opening = (
        "Compose the initial academic poster, including its native scientific graphics. "
        "Submit the first composition with replace_poster_layout, including the modules, "
        "surrounding layout and complete stylesheet. Prepare new equation fragments "
        "with render_poster_equations before that layout call when needed. "
        if initial_composition else
        "Revise the existing academic poster from current_layout_html, current CSS and "
        "browser measurements. Choose apply_poster_edits for exact local changes, "
        "move_poster_elements to relocate existing nodes, or replace_poster_stylesheet "
        "for a complete styling change. Use replace_poster_layout when wrapper topology "
        "and the stylesheet need a coherent replacement. "
    )
    editorial_scope = (
        "Author the scientific explanation and its visual composition together from the complete "
        "source. The current modules are an editable starting proposal. Select, combine or split "
        "them as the argument requires; write concise headings, diagram labels, captions and "
        "explanations for their actual visual role. Express the method's relationships with "
        "native text, nodes and connections, and give decisive data an inspectable comparison. "
        "Retain the source's values, units, baselines, conditions and uncertainty in every "
        "comparison. Keep the paper's stated limits on comparability beside the resulting "
        "claim, including experimental differences that contribute to the reported gain. "
        "Describe source figures using their observed panel, axis and legend reading keys; "
        "keep surrounding benchmark summaries distinct from the quantities actually plotted. "
        "Verify claims against the paper while composing. "
        "Preserve author names and order. Use the prepared author-note associations and "
        "complete source to compose affiliations and author roles with clear markers or "
        "concise named notes. Choose their wording and arrangement for the title band. "
        "Contact addresses supply contact information; author roles require an explicit "
        "author-block note association. Body footnotes retain their body scope. "
        "Mark the name list and its affiliation pointers "
        "with data-poster-authors=\"verified\", using sup elements for the pointers; place affiliation and author-note explanations "
        "in separate title-band nodes. "
        "Assign each module a "
        "unique data-poster-module/data-poster-id, a traceable data-source-label, and appropriate "
        "data-semantic-roles, data-module-priority and data-visual-kind metadata. "
        "Give body and attribution groups, including footer copy, a module owner. "
        "Reconsider proposed figures using their visible defects and the complete source; "
        "choose another prepared candidate or a grounded native explanation when it better "
        "serves the reader. Keep original image content intact when retained. "
        if source_text else
        "Preserve the current modules' scientific wording, label associations, table "
        "relationships, figures, equations and identity within the requested layout edit. "
    )
    system = opening + editorial_scope + (
        "Image and formula placeholders restore exact source nodes inside authored modules; "
        "whole-module placeholders retain an existing module. Preserve verified paper identity "
        "and intact source image pixels. Scientific figures use prepared asset tokens; "
        "Upstream native drawing components also use asset tokens as img src; their SVG "
        "is the browser preview and the host imports the original editable PPTX objects. "
        "Choose supplied drawings by their visual observations and scientific fit. A supplied "
        "component is optional: omit a confusing or visually weak candidate and use a clearer "
        "paper-original figure or compose the relationship natively. Place a retained drawing "
        "at its intact aspect ratio and group its explanation with it. "
        "New mathematics uses source LaTeX through render_poster_equations; copy its "
        "returned MathML unchanged. Existing equations can use the supplied formula "
        "references. This preserves fences, scripts and operator limits through the "
        "shared renderer. Preserve the "
        "physical page dimensions. Read the browser measurements after each proposal "
        "and address the current reading problem through the governing layout relationship. "
        f"{_ACADEMIC_COMPOSITION}\n"
        "Give source images an explicit painted width in their evidence region; "
        "max-width alone leaves small intrinsic figures unreadable. Keep aspect ratio "
        "and uncropped pixels. "
        f"{_typography_guidance(page_plan)}\n"
        "Choose a continuous reading span for a dense source figure from its actual labels "
        "and marks, then group its explanation with it. "
        "Do not hide, crop, or shrink scientific material into tiny "
        "type. Return tool calls only."
    )
    user = "\n\n".join(filter(None, (
        f"{'Initial composition brief' if initial_composition else 'Current visual-review feedback'}:\n{feedback}",
        f"User direction:\n{authoring_request}" if authoring_request else "",
        (
            "Prepared figures and native components (use ref as img src):\n"
            + json.dumps(_prepared_figure_manifest(
                assets or [], observations=source_figure_observations,
            ), ensure_ascii=False)
            if assets else ""
        ),
        "Complete scientific source:\n<source>\n" + source_text + "\n</source>" if source_text else "",
        "Design requirements:\n" + json.dumps(
            design_requirements if design_requirements is not None else design_direction.context(
                user_direction=authoring_request,
            ),
            ensure_ascii=False,
        ),
        "Physical page (mm): " + json.dumps({
            "width": page_plan["width_mm"], "height": page_plan["height_mm"],
        }),
        "Viewing-distance type starting sizes (mm): " + json.dumps(type_reference),
        (
            "Observed reference relationships:\n" + json.dumps(design, ensure_ascii=False)
            if design else ""
        ),
        "Source-figure width needs by module:\n" + json.dumps(
            figure_needs, ensure_ascii=False,
        ),
        (
            "Modules whose figures were allocated continuous page width: "
            + ", ".join(dominant_modules)
            + ". Use their actual internal label density to choose the reading span; "
            "keep their headings and explanations adjacent to the intact figures."
            if dominant_modules else ""
        ),
    )))
    return system, user


def resize_page_height(source_html: str, old_mm: float, new_mm: float) -> str | None:
    """Adjust physical page height and vertical constraints, preserving horizontal CSS."""

    document = lxml_html.document_fromstring(source_html)
    from tinycss2 import parse_blocks_contents, parse_component_value_list, serialize
    from tinycss2.ast import DimensionToken

    old = Decimal(str(old_mm))
    replacement = f"{new_mm:g}"

    def resize(tokens: list[Any]) -> bool:
        changed = False
        for index, token in enumerate(tokens):
            if token.type == "dimension" and token.lower_unit == "mm" and Decimal(token.representation) == old:
                tokens[index] = DimensionToken(
                    token.source_line, token.source_column, float(new_mm), None, replacement, token.unit,
                )
                changed = True
            else:
                children = getattr(token, "arguments", getattr(token, "content", None))
                if children is not None:
                    changed = resize(children) or changed
        return changed

    def resize_rules(tokens: list[Any], *, page: bool = False) -> bool:
        changed = False
        for rule in tokens:
            if rule.type == "declaration":
                if page and rule.lower_name == "size":
                    values = [
                        token for token in rule.value
                        if token.type not in {"whitespace", "comment"}
                    ]
                    if len(values) == 2:
                        height = [values[1]]
                        if resize(height):
                            rule.value[rule.value.index(values[1])] = height[0]
                            changed = True
                elif rule.lower_name in {
                    "height", "min-height", "max-height",
                    "block-size", "min-block-size", "max-block-size",
                }:
                    changed = resize(rule.value) or changed
            elif getattr(rule, "content", None) is not None:
                nested = parse_blocks_contents(rule.content)
                if resize_rules(
                    nested,
                    page=rule.type == "at-rule" and rule.lower_at_keyword == "page",
                ):
                    rule.content = parse_component_value_list(serialize(nested))
                    changed = True
        return changed

    changed = False
    for style in document.xpath("//style"):
        tokens = parse_blocks_contents(style.text or "")
        if resize_rules(tokens):
            style.text = serialize(tokens)
            changed = True
    for node in document.xpath("//*[@style]"):
        tokens = parse_blocks_contents(node.get("style"))
        if resize_rules(tokens):
            node.set("style", serialize(tokens))
            changed = True
    if not changed:
        return None
    return lxml_html.tostring(
        document, encoding="unicode", method="html", doctype="<!doctype html>",
    )


def finish_poster_shell(source_html: str) -> str:
    """Balance the masthead and remove empty decorative footer shells."""

    document = lxml_html.document_fromstring(source_html)
    for title in document.xpath('//*[@data-poster-title]'):
        style = str(title.get("style") or "").strip().rstrip(";")
        if "text-wrap:" not in style:
            title.set("style", f"{style};text-wrap:balance" if style else "text-wrap:balance")
    for node in document.xpath("//*"):
        classes = str(node.get("class") or "").split()
        if node.tag != "footer" and not any("footer" in name for name in classes):
            continue
        if "".join(node.itertext()).strip() or node.xpath('.//img | .//*[@data-poster-module]'):
            continue
        if node.getparent() is not None:
            node.getparent().remove(node)
    return lxml_html.tostring(
        document, encoding="unicode", method="html", doctype="<!doctype html>",
    )


def copy_channel_inventory(
    source_html: str,
    target_module_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Describe the structured copy fields that actually exist in each target module."""

    document = lxml_html.document_fromstring(source_html)
    inventory: dict[str, dict[str, Any]] = {}
    for module_id in target_module_ids:
        modules = [
            node
            for node in document.xpath('//*[@data-poster-module]')
            if str(node.get("data-poster-module") or "").strip() == module_id
        ]
        if len(modules) != 1:
            continue
        module = modules[0]
        channels: dict[str, Any] = {}
        if len(module.xpath('.//*[@data-content-role="text"]')) == 1:
            channels["text"] = "existing body channel"
        detail_nodes = module.xpath('.//*[@data-content-role="detail-points"]')
        if len(detail_nodes) == 1:
            channels["detail_points"] = {
                "current_item_count": len(detail_nodes[0].xpath("./li")),
            }
        flow_details = module.xpath('.//*[@data-content-role="flow-detail"]')
        if flow_details:
            channels["flow_details"] = {
                "required_item_count": len(flow_details),
                "step_labels": [
                    " ".join(node.text_content().split())
                    for node in module.xpath('.//*[@data-content-role="flow-label"]')
                ],
            }
        inventory[module_id] = channels
    return inventory


def _content_replan_brief(
    content_brief: dict[str, Any] | None,
    targets: list[str],
) -> dict[str, Any]:
    """Keep only source bindings needed by the explicitly targeted modules."""

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
    return brief


def _prepared_figure_manifest(
    assets: list[dict[str, Any]],
    *,
    observations: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Expose source figures and upstream drawings with reading keys and geometry."""

    manifest: list[dict[str, Any]] = []
    for item in assets:
        if not image_assets.is_source_figure_asset(item) and item.get("source_kind") != "native_component":
            continue
        aspect_ratio = image_assets.asset_aspect_ratio(item)
        manifest.append(
            {
                "ref": str(item.get("token") or ""),
                **(
                    {"caption": str(item["caption"])}
                    if str(item.get("caption") or "").strip()
                    else {}
                ),
                **(
                    {"context": str(item["context"])}
                    if str(item.get("context") or "").strip()
                    else {}
                ),
                "figure_number": item.get("figure_number"),
                "carrier_label": item.get("carrier_label"),
                "source_kind": item.get("source_kind"),
                "edit_scope": (
                    "native slide objects; final appearance requires export review"
                    if item.get("source_kind") == "native_component"
                    else "intact source image; internal labels and marks remain image content"
                ),
                "page": item.get("page"),
                "aspect_ratio": aspect_ratio,
                **({"visual_observation": dict(observations[item["content_sha256"]])}
                   if observations and item.get("content_sha256") in observations else {}),
                **({"editable_format": "pptx", "description": item.get("description", ""),
                    "labels": item.get("labels", [])}
                   if item.get("source_kind") == "native_component" else {}),
            }
        )
    return manifest
