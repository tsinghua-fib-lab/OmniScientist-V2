# HTML authoring

`poster.html` is the sole poster authoring state. Produce it directly; do not ask a user or model to fill a per-poster JSON form.

## Read the paper before composing

Build a mental evidence map from the complete supplied source:

1. What problem motivates the work, and why is it hard?
2. What is the central contribution in the authors' own evidentiary terms?
3. Which method detail is genuinely novel or necessary for interpretation?
4. Which results most directly establish the contribution?
5. What conditions, baselines, units, uncertainty, and scope qualify those results?
6. Which figure, table, or compact HTML/SVG diagram best carries the argument?

Prefer evidence stressed in the paper's result discussion, principal tables, and figure captions. Do not select content merely because it fits a convenient panel.

Audit every planned claim at its exact experimental scope. Keep base, large, ablated, and task-specific configurations distinct; retain the dataset, hardware, protocol, estimate status, uncertainty, and exceptions that qualify a comparison. Preserve hypotheses and words such as “may”, “appears”, and “seems” at the same epistemic strength. If narrative prose conflicts with a displayed table, report the table's literal values without repeating the disputed ranking. Interpret only prepared figure hashes that are actually visible with the discussion; otherwise show the missing evidence or omit the observation.

When the source is a local PDF, give `paper_path` to the Skill instead of reading the binary through a text-file tool. PDF preparation is a distinct pre-authoring stage: page-aware text and caption-anchored figures become the model's source and optional visual assets. Preserve a covering raster XObject without re-encoding, preserve mixed or vector content as a self-contained SVG crop, and use a 450 DPI PNG only as fallback. If the optional PDF reader is absent, stop with the returned installation command; never treat the user's request string as paper content.

## Choose composition from the evidence

- Figure-led is useful when one decisive visual deserves the largest uninterrupted region.
- Method-led is useful when architecture, procedure, or study design needs the clearest spatial flow.
- Result-led, balanced, asymmetric, or multi-entry compositions may fit other evidence distributions.

These are descriptive emphases, not closed layout modes. Semantic roles never become fixed slots. The returned visual design transfers grammar from the selected reference: masthead relationship, topology, hierarchy, density, typography, section treatment, figure scale, and palette relationships. Use it with the actual text and figure geometry rather than reconstructing the reference as a template.

The page plan also reports physical capacity and a balance estimate. Treat them as advisory measurements on an automatically selected page, not a semantic grid or a pre-render rejection: choose the track count, grouping, spans, and local subgrids that best fit the evidence and reference, then use rendered Chromium geometry to decide whether bounded semantic evidence replanning is actually needed. A user-specified physical page remains a hard capacity contract.

Make scan groups immediately distinguishable through group-level cues such as title treatment, a thin rule, local background tone, shared boundary, or deliberate inter-group whitespace. Choose cues from the content and overall visual language. Do not encode every group with the same fixed color or shape, and do not turn its individual modules into repeated cards.

Before authoring, create a grounded evidence map with the central contribution, the method detail needed for interpretation, complementary decisive evidence, provenance, literal source locators, and only prepared source-figure hashes. Let evidence determine the number and granularity of scan groups and modules. `scan-first`, `figure-led`, `method-led`, `result-led`, and `narrative` are optional organizational descriptions, not schemas. Groups do not need to form a linear problem-method-result sequence. Include a limitation, boundary condition, or negative result only when the source supports it; never invent one to satisfy structure. Locators are audit metadata: visible copy explains the evidence and captions tell the reader what to notice. Do not print `Figure N`, `Table N`, page/section labels, or a `Source:` line beside every visual; consolidate literal locators in provenance/references. Combine closely related material rather than creating one panel per paragraph or metric. Select source figures by distinct evidentiary function; there is no global figure-count cap and no fixed module placement. Run `estimate` when no exact venue page is supplied. Its page, occupancy, and balance measurements never justify manufactured content; only a measured failure at the largest automatic page triggers bounded semantic replanning.

If source equations are necessary for understanding the contribution, retain only the essential source-located LaTeX expressions that remain readable at poster scale. Render them as native semantic MathML with structural elements such as `mfrac`, `msub`, `msup`, `msubsup`, `munderover`, and `msqrt`; keep the exact source LaTeX in `data-latex`. Use `<math display="block">` but do not override its inner display type with ordinary CSS `display:block`, flex, grid, or inline-block; doing so turns math children into normal flow boxes. Give a long equation more inline measure or a wider span before reducing type. If its source semantics permit line breaks at relation operators, use `mtable`/`mtr`/`mtd` to align the source-equivalent lines. Escape a less-than operator as `&lt;` inside MathML. Raw LaTeX text, remote MathJax/KaTeX, scripts, and equation screenshots are not valid authoring. PPTX export converts each complete MathML expression into native editable Office Math and retains a visual fallback only for viewers without native equation support.

Start dense landscape work on the full common format, normally A0. When the rendered screenshot proves that the full height is wasteful, a visual revision may shorten the page only within the planned common academic-poster proportion (landscape no wider than 3:2). Compact means content-bounded, not small type: enlarge strong existing evidence or shorten the page before adding text.

Choose locally available font stacks that express the reference's typographic relationship and the user's preference; serif display with sans body, all-sans, or a restrained hybrid can all work. Never fetch a font. Treat role sizes and line measures as advisory conference-viewing evidence evaluated from the screenshot. Add `data-focal-role` only when a sourced element genuinely acts as a dominant entry point; do not manufacture one or limit the poster to a single visual emphasis.

Size prominent copy from its rendered shape, not from a fixed character-width template. When a title or central claim wraps awkwardly, widen its measure or rebalance adjacent groups before reducing type.

## Common layout pitfalls

- `flex: 1` includes positive `flex-grow`; on semantic columns it often forces equal heights and leaves the shorter column empty at the bottom. For a bounded column of poster modules, use intrinsic items (`flex: none` or `flex: 0 0 auto`) so a module frame cannot shrink below its content; keep `min-width: 0` on inner prose wrappers for horizontal wrapping.
- A flex/grid parent defaults to cross-axis stretch in common configurations. Use `align-items: flex-start` when columns must remain independently compact.
- `gap` combined with growing children makes the location of spare space hard to predict. Keep gaps explicit and let content define column height.
- A prose-bearing flex child with `flex: 0 0 auto`, a content-sized minimum width, or `white-space: nowrap` can expand beyond a narrow rail. Put `min-width: 0` on text-bearing flex/grid children and use wrapping or `minmax(0, 1fr)` tracks for compact comparisons.
- Fractional rows inside content modules, fixed `min-height`, and `justify-content: space-between` can distribute blank space while wrappers appear full. Use fractional tracks only when every track contains real content; module interiors remain intrinsic.
- `min-height: 0` on a content-bearing grid item allows the item box to shrink below its prose and figures, so children visibly spill into the next row even when the grid itself appears to fit. Keep poster modules intrinsically sized.
- Giving several direct grid children only `grid-column` does not create a coherent macro composition: auto-placement keeps advancing the row cursor, so a later group can start halfway down the page. Assign both grid axes explicitly or nest modules inside intrinsic scan-section groups.
- Do not mix direct modules and track wrappers in the same body grid, or place later modules in rows whose height is fixed by an unrelated focal item. The tallest item then stretches shorter content into visible voids. Prefer independently flowing groups when module heights differ.
- Choose column breaks from estimated intrinsic height, not equal module counts. Distribute tall figures and equations across the readable columns while preserving the planned section order; several tall figures in one column and short text in another is not a balanced scaffold.
- Use a track-wide section header only when the modules beneath that track have comparable intrinsic depth. Otherwise attach section cues to compact local groups so one short section cannot create an empty column beside an overflowing one.
- Focal prominence does not always require a span. It may come from evidence scale, module depth, position, measure, or contrast; a colored heading alone is insufficient.
- `max-width: 100%` prevents overflow but does not enlarge a prepared SVG or raster figure beyond its intrinsic size. Give evidence figures an explicit responsive width when the available module measure should be used; otherwise an A0 poster can contain technically valid but unreadable thumbnails and a large unused lower field.
- Treat figure extent and page-area measurements as advisory conference-viewing evidence. For prepared scientific figures, valid hashes, nonzero rendering, visibility, and an unclipped in-page image remain hard source-evidence requirements; these do not make general layout geometry a deterministic aesthetic gate.
- A poster with `width: 841mm` may still shrink to the browser viewport because flex items default to `flex-shrink: 1`. Keep the body-level poster root outside a shrinking flex context or set `flex: none`; Chromium must measure the declared physical dimensions.
- Do not fix these traps by stretching an empty wrapper. Chromium records geometry and type measurements; the screenshot reviewer judges whether the remaining space, hierarchy, and readability are purposeful.

## Complete HTML contract

- Begin with `<!doctype html>` and return the entire document without Markdown fences.
- Use one body-level `main`, `article`, or `div` poster root with `data-poster-id` and inline CSS with `@page` size in millimetres. The root encloses the complete poster: exactly one compact `data-poster-title-band` followed by the internal evidence body. “Outside module styling” never means outside this root. The root itself must render at the declared physical width and height; use `@page` margin `0`, put print margins in root padding, and use `box-sizing: border-box` when the root owns padding or borders. Prefer semantic `main` or `article`.
- Give every independently placeable evidence module one stable `data-poster-module` wrapper and `data-poster-id`, and retain `data-section-id` where the evidence map supplies one. The wrapper is an audit, selection, and export anchor, not necessarily a visible panel. Several modules may share one section and group-level heading; layout-only wrappers may freely regroup, reorder, or span modules.
- Use optional priority, semantic-role, and focal attributes only as audit or design hints. Any sourced focal element must carry or inherit a verifiable `data-source-label`, but neither focal count nor visual hierarchy is a static contract.
- Give meaningful selectable elements stable unique `data-poster-id` values. Every evidence-bearing `data-poster-module` needs a `data-source-label` with a concrete locator such as `p.3`, `§3`, `Figure 1`, `Table 2`, `Abstract`, `Title page`, or `References`; PDF locators must exist in the extracted source. For a complete non-paginated brief, use `Grounded brief` instead of inventing a page or section.
- Embed supplied PNG, JPEG, GIF, WebP, or safe SVG figures as data URIs. Treat all prepared source figures as the provenance manifest and render only the grounded subset explicitly selected by evidence planning; include at least one when usable prepared figures exist. Place each selected figure wherever it best supports its grounded interpretation; its hash and provenance travel with it rather than binding it to an original layout slot. A network-capable harness may download an authorized asset and pass its local path through `assets`; portable authoring itself never fetches remote content. `asset://N` is only a transient host token resolved before persistence.
- Place a short interpretive caption beside or below each figure. The caption explains the comparison, trend, mechanism, or uncertainty visible in the image; its paper locator stays in `data-source-label` and the consolidated provenance/references module.
- Do not include scripts, event handlers, forms, animation, imports, external fonts, remote URLs, placeholders, or executable SVG.

## Revision

Revise the complete source HTML after verifying its exact hash. A selection may contain only stable poster id, nearest evidence module, optional audit hints, text sample, rectangle, and styles. Preserve scientific identity, grounded visible content, source/figure bindings, equations, and stable evidence ids. A full-layout revision may regroup, reorder, or span those modules; a style-only revision preserves DOM order and content. A VLM-directed `content-replan` remains a complete-HTML revision, but may rewrite explanatory copy only in the explicitly targeted grounded module ids. It receives the immutable grounded evidence budget, a current displayed-content snapshot, and the original source as scientific authority; it cannot delete modules, replace figures, modify displayed MathML, or alter identity, source labels, roles, priorities, focal roles, or equations. The reviewer proposes targets and repair outcomes; it does not authorize new facts. If a staged revision regresses deterministic inspection, keep the prior bytes and return the rejected candidate's measured geometry feedback to one bounded retry.

Before a model-backed revision, replace embedded image data URIs with transient `asset://N` tokens and restore the exact bytes afterward so figures do not consume the model context. If revision runs in a new background process, pass the same `paper_path` to reconstruct grounding text instead of relying on in-memory engine state.
