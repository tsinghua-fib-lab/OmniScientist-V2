# Runtime contracts

Public JSON fields, outcomes, issue codes, and warning codes use `snake_case`. Action names use verb-led `kebab-case`; HTML attributes and CSS properties retain their platform spelling.

## Static HTML gate

Static validation requires a complete HTML5 document, one body-level poster root enclosing exactly one title band, exactly the grounded content-module ids selected during planning, a valid physical millimetre page declaration, stable evidence ids and non-empty source metadata, correct prepared-asset hashes, and exact equation bindings. It rejects active HTML/CSS, remote references, unsafe SVG, unresolved asset tokens, duplicate ids, and invented modules. It does not infer scientific validity or incompleteness from visible English phrases; grounding and selected evidence remain the authority. Each module is one independently placeable evidence job; multiple modules may share a section. Module, section, and evidence bindings do not prescribe visible panels, section count, ordering, rail count, or spans.

The deterministic gate checks exact paper identity, prepared-figure hashes, and source-bound equation LaTeX. It does not infer grounding from English locator syntax, legal keywords, or token-level number matching. Evidence planning remains responsible for variant scope, qualifiers, comparisons, and faithful paraphrase from the complete source.

## Paper-source gate

`paper_path` accepts one local PDF path or host artifact URI. Before authoring, the engine verifies the PDF signature, extracts page-aware text, and prepares a bounded provenance manifest of caption-anchored figures. Evidence planning selects only figures that earn readable poster area; a text-, equation-, or table-led paper may select none. A single raster XObject that covers the region is extracted without re-encoding; mixed or vector content becomes a self-contained SVG crop; 450 DPI PNG rendering is the final fallback. Extracted captions and discussion context describe each `asset://N`; selected image bytes are embedded into final HTML. Missing PyMuPDF returns a recoverable `missing_capability` result and no authoring model call is made.

## Design-reference gate

After evidence and page planning, every draft resolves exactly one visual reference before HTML authoring. Complete `OMNI_IMAGE_GEN_*` configuration calls the Omni-normalized image endpoint for a content-free reference; missing, incomplete, invalid, failed, or budget-ineligible image generation uses deterministic content signals to select one of five bundled real conference-poster seeds. Image generation and VLM review use independent endpoint/model/key triples. VLM availability does not change the resolved reference. These examples provide design grammar, not templates, semantic slots, fixed column layouts, or scientific evidence.

The VLM configuration is not consulted during evidence planning, page estimation, seed selection, or reference generation. When usable, it first derives the bounded executable visual contract from the exact resolved reference pixels and later performs the rendered reference-versus-candidate review loop. The contract contains only renderer-consumed typography, palette, and layout primitives; free-form observations and directives are not hidden authoring inputs or review gates. Palette hues are retained when their assigned roles are usable; role colors are minimally blended toward their existing background or text endpoint when needed to keep renderer-owned body, caption, surface, and band-text combinations readable. This normalization repairs functional role assignment and never rejects a reference on aesthetic grounds.

The returned `design_reference` is an exact hash-bound object used by authoring and visual review. Its pixels and text cannot source claims, numbers, authors, affiliations, equations, figures, citations, logos, or venue identity. Its image hash is never a prepared source-figure hash and never contributes to `source_figure_manifest_sha256`.

## Chromium gate

Chromium verifies that the poster can be reviewed safely. Its hard boundary covers root/id/page measurement, blocked network requests, missing visible title/content/modules, any selected source figure that is invalid, hidden, unrendered, clipped, or outside the poster, poster or element-content scroll overflow, overlap between independently movable poster modules, and visible poster elements outside the physical page. Other clipping and geometry diagnostics, physical type size, figure scale, and page-fit measurements are warnings for visual review rather than deterministic aesthetic blockers.

Chromium does not decide whether the poster is attractive, readable at conference distance, dense enough, too card-like, balanced, or visually hierarchical. It records measurements and repair context for the reference-and-screenshot-bound visual review. If Playwright or Chromium is unavailable, return `missing_capability`; never pretend rendered QA passed.

## Reference-aware visual-review gate

`prepare-visual-review` requires the complete serialized `design_reference` and the exact Chromium `visual_evidence` bundle. It creates `scientific-poster.visual-review-request.v5`, binding the HTML, reference image, candidate screenshot, evidence bundle, grounded `content_brief`, current iteration, and visual criteria.

An image-capable harness returns `scientific-poster.visual-review-result.v5` with the same bindings; the receipt is `scientific-poster.visual-review-receipt.v5`. The adapter sends the reference, full candidate overview, and optional labeled high-resolution evidence atlas. Every selected deterministic observation requires one `acceptable`, `actionable`, or `uncertain` assessment; pass requires all observations to be acceptable and no issue or directive. `OMNI_VLM_ENDPOINT` is a complete OpenAI-compatible chat-completions URL; the Skill uses bearer auth and never infers a provider. Scores are diagnostic rather than pass thresholds. A revise result contains observed issues, global directives, and one or more operations: `restyle`, `reflow`, or `content-replan`. Iterations 0 and 1 may request another revision; a revise verdict at iteration 2 becomes `visual_review_failed`.

Every successfully rendered candidate is eligible for VLM review. Its request carries the exact screenshot, stable Chromium observations (including physical-page extent and named out-of-page modules), and a bounded evidence atlas, so objective geometry and perceptual judgment share one evidence boundary. A final pass requires deterministic integrity and the VLM verdict on the same bytes. Visual-review targets locate observed problems for a later explicit `revise` action. `restyle` and `reflow` preserve content and DOM order and change only CSS; the canonical module wrappers remain stable while grid placement, ordering, spans, spacing, and typography may change. `content-replan` may additionally curate or compress only the exact grounded explanatory modules named by the review; it may omit secondary examples or repeated detail, but cannot alter a retained value or qualifier, invent content, delete the module, or replace evidence. A revision is rendered and reviewed before commit. The draft action does not run a hidden model rewrite loop after review. If authoring or revision cannot finish safely, return the exact prior bytes and durable checkpoint rather than replaying accepted work. With no configured reviewer, return `visual_review_unavailable` with `visual_review_mode: deterministic-only` and permit editable draft export after deterministic inspection. A configured but unavailable or non-passing VLM remains `vlm`/`awaiting-review` and blocks automatic final export.

VLM `restyle` and `reflow` operations replace only the stylesheet over the canonical deterministic DOM. `content-replan` may compress or rewrite existing grounded explanatory copy only in module ids explicitly targeted by VLM issues and present in the immutable grounded content budget; it must return a complete valid document and is never repaired by guessing missing wrappers or image containers. Deterministic comparison freezes identity, module instances, source bindings and figures, displayed MathML, equations, semantic roles, priorities, and focal roles. Manual `full-layout` remains explicit and must itself preserve every required marker. An explicitly requested physical page remains fixed. When the original page plan is `auto`, a reviewer-directed revision may change page height inside the planned bounds.

Background execution never waits for an operator. When configured review requests a change, the result may include `scientific-poster.decision-request.v2`, bound to the current HTML hash. Each actionable option carries its own continuation: layout revision uses `full-layout`, while grounded copy compression uses `content-replan` plus exact `content_replan_targets`; `keep-draft` has no continuation. A later ordinary `revise` invocation copies the selected option's continuation and adds its required fields. Deterministic-only operation without a configured VLM does not create this checkpoint.

## Estimate and page-fit contract

`estimate` returns `estimate_complete` with the normalized `content_budget`, `page_plan`, source summary, expected figure hashes, and warnings. Source-only portable calls return `host_agent_required`; callers may supply a prevalidated grounded budget for deterministic portable estimation.

Explicit pages reject non-object, missing, non-finite, non-positive, or out-of-range dimensions through `invalid_page`. Evidence-budget validation protects structure, grounding, and prepared-figure identity; approximate occupancy never rejects a candidate before rendering. The deterministic renderer removes only exact presentation duplicates across one module's optional copy channels. Section count, module count, focal emphasis, typography, density, and topology remain open design decisions. Optional priority and focal metadata may guide authoring but do not impose a unique dominant module.

An auto page plan starts on a full common academic format (normally A0 for a dense landscape poster) and retains a bounded revision height while keeping landscape pages within a common poster proportion (no wider than 3:2). Draft invokes the same planner before HTML authoring. Occupancy and balance estimates remain advisory and never prescribe tracks or reject a candidate before rendering; Chromium geometry is the authority for overflow and underfill, and only measured delivery evidence may trigger bounded semantic evidence replanning. A user-specified physical page remains a hard geometry contract, but its estimated occupancy is still advisory. A usable VLM derives visual direction from the exact pixels of either a seed or generated reference. Without a VLM, a seed supplies its structured grammar and a generated reference uses unanchored content-adaptive fallback guidance. In every case, configured VLM review later compares the rendered candidate with the exact reference pixels. Page estimation never asks for filler.

## Selection state

The preview proxy injects its overlay only into the served response. `poster.html` bytes remain unchanged. A valid machine-owned selection stores:

- source HTML SHA-256;
- selected stable poster id, nearest poster module, semantic roles, and module priority;
- bounded text sample, rectangle, and computed styles;
- capture timestamp.

The server rejects stale hashes and identities that do not exist in canonical HTML.

## Approval and handoff

The returned poster approval phrase is `APPROVE SCIENTIFIC-POSTER poster <html_sha256> source <grounding_source_sha256> figures <source_figure_manifest_sha256>`. Approval accepts exactly one grounding input and one passing visual-review receipt bound to the same HTML hash. With `paper_path`, it deterministically reconstructs page-aware text and prepared-figure hashes; with text input, the caller supplies `source` or `source_text` and machine-owned figure hashes. Approval repeats static grounding and prepared-figure validation, then verifies the already-rendered receipt against the exact HTML hash; it does not launch Chromium a second time. The content-addressed bundle contains exactly `poster.html`, `visual-review.json`, and `approval.json`.

The approval result returns the verified receipt path, exact HTML path, and content hashes for downstream handoff.

## Editable PPTX export

The direct `export-pptx` action accepts exact validated HTML and an output directory; it does not require an approval or visual-review receipt. In the normal Omni `draft`/`revise` flow, automatic export additionally requires passing deterministic inspection. No configured VLM leaves an explicitly unapproved deterministic-only draft eligible for export after inspection; a configured but unavailable or non-passing VLM remains `vlm`/`awaiting-review` and blocks automatic export.

Chromium captures a versioned scene with stable object ids, physical slide geometry, native text, native tables, semantic MathML equations, module/title shapes, and figure images. The Python renderer uses `python-pptx` to write one custom-size slide; it never places a full-poster screenshot on the slide. MathML becomes native editable Office Math, with a same-position visual fallback only for viewers that do not support the Office extension. OpenXML verification confirms one slide, retained object names, native equations, and their fallbacks.

The export gate blocks failures of basic editability: no native text, a raster image covering at least 80% of the page, unsafe text-fit policy, missing font preflight, or broken required object identity. Slide bounds, native-object overlap, and physical type size are warnings because the reference-aware visual review owns layout and readability judgments. Every failure or warning names the affected object and a suggested patch. The result returns `poster.pptx`, `poster-scene.json`, `poster-pptx-rubric.json`, and OpenXML counts.

HTML remains the canonical authoring state; the PPTX is its directly exported editable delivery companion.
