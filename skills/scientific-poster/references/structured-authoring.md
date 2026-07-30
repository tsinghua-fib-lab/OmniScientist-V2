# HTML authoring

`poster.html` is the sole poster authoring state. Produce it directly; do not ask a user or model to fill a per-poster JSON form.

## Read the paper before composing

Build a mental evidence map from the complete supplied source:

1. What problem motivates the work, and why is it hard?
2. What is the central contribution in the authors' own evidentiary terms?
3. Which method detail is genuinely novel or necessary for interpretation?
4. Which one to three results most directly establish the contribution?
5. What conditions, baselines, units, uncertainty, and scope qualify those results?
6. Which figure, table, or compact HTML/SVG diagram best carries the argument?

Prefer evidence stressed in the paper's result discussion, principal tables, and figure captions. Do not select content merely because it fits a convenient panel.

When the source is a local PDF, give `pdf_uri` to the Skill instead of reading the binary through a text-file tool. PDF preparation is a distinct pre-authoring stage: page-aware text and caption-anchored figure crops become the model's source and optional visual assets. If the optional PDF reader is absent, stop with the returned installation command; never treat the user's request string as paper content.

## Choose composition from the evidence

- Figure-led: one decisive visual result deserves the largest uninterrupted region.
- Method-led: architecture, procedure, or experimental design needs a broad narrative flow.
- Balanced: method and evidence have comparable depth.

These policies guide spans, hierarchy, gutters, and density. The HTML may use grid, flex, or a combination. There is no required column count or semantic slot map.

Size the hero from its rendered copy, not from a fixed character-width template. A display-scale central claim that wraps to four or more lines should normally use at least 70% of its parent content width; widen the block or rebalance adjacent hero columns before reducing type.

## Complete HTML contract

- Begin with `<!doctype html>` and return the entire document without Markdown fences.
- Use one body-level `main`, `article`, or `div` poster root with `data-poster-id` and inline CSS with `@page` size in millimetres. The root itself must render at the declared physical width and height; use `@page` margin `0` and put print margins in root padding. Prefer semantic `main` or `article`.
- Mark exactly one visible `hero`, `method`, `evidence`, `limitations`, and `provenance` region.
- Give meaningful selectable elements stable unique `data-poster-id` values. Every semantic region and evidence-bearing component needs a `data-source-label` with a concrete locator such as `p.3`, `§3`, `Figure 1`, `Table 2`, `Abstract`, `Title page`, or `References`; PDF locators must exist in the extracted source. For a complete non-paginated brief, use `Grounded brief` instead of inventing a page or section.
- Optional reusable primitives carry `data-component-id` and `data-component-version` together.
- Embed supplied PNG, JPEG, GIF, WebP, or safe SVG figures as data URIs. When source-figure tokens are supplied, use at least one relevant token as a quoted `img src`. `asset://N` is only a transient host token resolved before persistence.
- Do not include scripts, event handlers, forms, animation, imports, external fonts, remote URLs, placeholders, or executable SVG.

## Revision

Revise the complete source HTML after verifying its exact hash. A selection may contain only stable poster id, region, text sample, rectangle, and styles; component identity is optional. Preserve the physical page, source grounding, stable ids, and unrelated regions unless feedback explicitly requests a broad redesign.

Before a model-backed revision, replace embedded image data URIs with transient `asset://N` tokens and restore the exact bytes afterward so figures do not consume the model context. If revision runs in a new background process, pass the same `pdf_uri` to reconstruct grounding text instead of relying on in-memory engine state.
