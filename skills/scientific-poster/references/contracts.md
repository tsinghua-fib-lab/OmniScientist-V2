# Runtime contracts

Public JSON fields, outcomes, issue codes, and warning codes use `snake_case`. Action names use verb-led `kebab-case`; HTML attributes and CSS properties retain their platform spelling.

## Static HTML gate

Static validation requires a complete HTML5 document, one body-level poster root enclosing exactly one title band, exact verified title and author identity, unique source-grounded content-module ids, a valid physical millimetre page declaration, stable evidence ids and non-empty source metadata, correct prepared-asset hashes, and exact equation bindings. A newly extracted affiliation is rendered deterministically when available and then frozen by the existing scientific identity snapshot; its absence does not add an approval gate for older poster bytes. Static validation rejects active HTML/CSS, remote references, unsafe SVG, unresolved asset tokens, duplicate ids and authored CSS-generated content outside the scientific DOM. Native list-style identifiers and `display:list-item` are supported; Chromium supplies their actual marker text and geometry for editable export. Custom counter-style definitions, list strings/functions/images, and authored `content` remain outside that contract; `content:normal`, suppression and inert counter state are allowed. Every candidate passes this same local CSS rule, while revision snapshots freeze scientific DOM content without a second selector or declaration fingerprint. Static validation does not infer scientific validity or incompleteness from visible English phrases; grounding and selected evidence remain the authority. Each module is one independently placeable evidence job; multiple modules may share a section. Module, section, and evidence bindings do not prescribe visible panels, section count, ordering, rail count, or spans.

Static content collection reads explicit inline hiding hints in declaration order and by importance. Content with unresolved CSS values remains in the DOM snapshot; Chromium determines its computed visibility during inspection.

Page-size reading resolves the generated single-page contract: unqualified, unconditional `@page` rules with two explicit millimetre dimensions. The parser retains stylesheet boundaries and resolves valid `size` declarations by importance and source order, so an appended override is not mistaken for the old page. This is not a general CSS cascade engine for named/pseudo pages, conditional rules, layers, or computed dimensions.

The deterministic gate checks exact paper identity, prepared-figure bindings and native equation structure. Prepared equation references restore their exact source expressions. The source-aware author checks variant scope, qualifiers, comparisons and paraphrases against the complete paper.

Planned author identity is a source-ordered array of names. The shared identity boundary
normalizes it to the existing name line and also accepts legacy name strings. Affiliation
markers and author notes are composed from the complete source in the title band; the
verified author marker encloses the names and their pointers, with explanatory notes in
separate nodes. Superscript pointers retain their full content in the scientific snapshot
and remain outside the name comparison. Persisted complete author lines may match the
same container's full displayed text. Exactly one verified author container owns both
projections. Bibliographic identity and its visual arrangement have distinct roles.

Prepared identity may include `author_notes`, with each note containing its `authors`
and `text`. LaTeX preparation resolves these associations inside the author block.
The author uses those associations to compose clear markers or concise named notes.
Author notes are ordinary title-band copy and follow the requested copy or layout edit
scope. Contact addresses and body footnotes retain their own source scope.

Source-aware budget normalization checks each locator against the supplied source, folding Unicode presentation forms and extraction whitespace. Labels retain the source's actual wording. Prepared figure labels require the corresponding selected image hash; prepared equation labels require the corresponding transcription. Model planning, caller-supplied budgets and recovery use this check. Locator presence establishes a source location. The author reads its surrounding evidence when composing a claim. Explicit layout-only revisions retain the selected HTML's scientific snapshot.

Visual review records the reviewer's location hints and requested operation unchanged.
Measurements bind to known modules where possible. Current user requests allow prose
and layout edits together through content-replan. Optional module targets or the
reserved poster target narrow its copy scope; omitted targets cover all current prose.
Explicit full-layout edits preserve the scientific snapshot. Receipt-only revisions
follow their issue operations and targets. Initial drafting keeps the complete source
with its author.

## Paper-source gate

Multiple representations of one paper use one selection policy: local LaTeX,
local PDF, then arXiv retrieval. The selected source supplies the content and the
returned source path. A preparation failure stays attached to that selection.

`latex_project_dir` reads the project source directly, following included TeX files
outside literal environments and preserving display equations and captions. Known
text commands and accents use the semantic TeX parser. Bibliography declarations
include the main document's available `.bbl`, or the local `.bib` files declared by
`\bibliography` and `\addbibresource`. Supplemental bibliography files enter the
authoring source once, within the same project boundary and source-byte budget. Missing
bibliography files produce warnings while the available paper content remains usable.
Identity that requires document-class or custom-macro interpretation remains available to the complete-source
planner with a warning. Numeric figure rotations preserve the full image. Original
assets retain intrinsic proportions; the poster owns their placement dimensions.
Source-paper width and height expressions stay in the retained TeX source. Content
transformations outside the supported rotation path produce a preparation diagnostic.
Use `main_tex`
when multiple document roots exist. `arxiv_id` accepts an arXiv id or arxiv.org
paper URL and first retrieves the submitted TeX package. Extraction preserves regular
files and applies bounded decompression and tarfile's data filter without executing
submission contents. Source acquisition or interpretation failures produce a warning
and use the corresponding PDF. PDF source figures produce SVG previews; `source_path`
retains their original file. Bitmap originals retain their image content. TikZ source
remains available in the project and requires compilation for a preview.

An explicit paper, project, arXiv identifier or grounded text source takes precedence
over the host's automatically attached file. The first attachment supplies the PDF
source when no explicit source is supplied.

`paper_path` accepts one local PDF path or host artifact URI. Before authoring, the engine verifies the PDF signature, extracts page-aware text, and prepares a bounded native-Figure provenance manifest plus physically anchored Table or Algorithm visual carriers from the required layout pages. The main model may author source-grounded inline or display LaTeX from the complete prepared paper. Any structured equation inventory is an optional transcription shortcut and never a rendering permission gate. Evidence planning selects only carriers that earn readable poster area; a text- or structured-table-led paper may select none. Formal Table/Algorithm carriers are not silently truncated by type and reuse the existing `figure_sha256s` binding contract. Ordinary layout tables are not converted to images. A single covering raster XObject is extracted without re-encoding when its pixels reproduce the visible page region; transforms, transparency, overlays, and vector content use a self-contained SVG crop, with 450 DPI PNG rendering as the final fallback. A horizontal PDF separator that covers the caption-owned graphic component bounds text ownership before padding, so a preceding table row cannot leak into the next figure while labels inside the new ownership cell remain attached. Extracted captions and bounded discussion context describe each `asset://N`; selected image bytes are embedded into final HTML. Missing PyMuPDF returns a recoverable `missing_capability` result and no authoring model call is made.

Draft source preparation invokes this extractor once in a fresh process of the active Python runtime. Host PDF-reader globals cannot change the prepared source, and cancellation of asynchronous preparation terminates its owned child. This is a runtime isolation boundary, not a second parser or validation pass.

Image-region candidates use PyMuPDF's native clipped page bounds. Vector paths
intersect their active PDF clip stack before figure clustering and crop selection.
The shared PDF runtime requires PyMuPDF 1.28 or later. Original raster bytes are
retained when their geometry and pixels match the page's visible figure. Rotation,
masking, opacity, overlays and clipping are preserved through the existing
vector/raster page-rendering path when raw extraction would change that appearance.

Source SVG dimensions resolve absolute CSS units and intrinsic viewBox ratios.
Context-dependent or invalid explicit dimensions produce a preparation diagnostic;
the figure can be supplied again with absolute dimensions.

## Design-reference gate

After evidence and page planning, every draft resolves one visual reference before HTML authoring. A supplied reference image takes precedence; otherwise the planned page orientation selects a bundled published academic poster with real body text and evidence. Published examples carry source attribution and separate licenses in NOTICE.md. Earlier synthetic boards retain their identities for saved-draft recovery. Reference observations describe typographic roles and spatial relationships; the author chooses the current composition from the paper's evidence and page dimensions.

The initial ContentBudget proposes evidence and a starting physical page. One optional VLM observation describes selected figures' panel structure, legible panel/axis/legend reading keys, embedded-label density and optional width recommendations. Figure IDs preserve the image associations; absent or unrecognized width advice remains unspecified while valid reading observations reach the author. Response parsing and checkpoint restoration share this normalization. The author receives the complete source, all prepared figure references, starter HTML and figure observations. It selects evidence and jointly writes explanations, diagram labels and HTML/CSS. The initial budget supplies context; the actual HTML owns the resulting modules and text. Identity from the paper and intact source images retain their existing output checks. Native mathematical expressions retain their semantic representation.

Reference analysis observes the selected example independently of target content. It records hierarchy, regions, typography and spacing rather than target CSS or coordinates. Missing analysis leaves the corresponding visual-review state pending while the source-aware author continues.

The returned `design_reference` is an exact hash-bound object used by authoring and its durable checkpoint. Final visual review binds the selected reference image in its own request; the interpreted design remains author context. Reference pixels and text supply design observations. Scientific claims, numbers, identity, equations, figures and citations come from the paper. The reference hash stays separate from the prepared source-figure manifest.

## Chromium gate

Chromium verifies that the poster can be reviewed safely. Its hard boundary covers root/id/page measurement, blocked network requests, missing visible title/content/modules, any selected source figure that is invalid, hidden, unrendered, clipped, or outside the poster, rendered text or media outside the physical page, self-clipping or scroll-constrained element overflow, overlap between independently movable poster modules, source-figure overlap with editable text, native-table overlap with its neighboring heading or caption, and visible poster elements outside the physical page. Physical-page containment uses the browser's text ranges and media bounds, including identity outside scientific modules; an empty layout container extending the root's scroll area does not by itself prove content loss. A source-figure/text collision reports the colliding text element, exact line and figure rectangles, and which figure edge is crossed; it does not choose the repair. A table collision likewise reports only the measured table and neighboring text boxes. Ordinary auto-height, `overflow:visible` module flow is not a separate blocker; the poster root owns physical-page containment. Physical figure extents and image-box ratios, other geometry diagnostics, physical type size, and page-fit measurements remain local context rather than deterministic aesthetic blockers. Whole-page figure area percentage is used only to prioritize detail crops. Code-derived typography targets remain authoring context and advisory browser diagnostics. Neither is exposed to the VLM as an acceptance signal; the reviewer receives actual physical type distributions instead. No universal figure-label target or derived readable flag is exposed to the VLM as an acceptance target.

Chromium does not decide whether the poster is attractive, readable at conference distance, dense enough, too card-like, balanced, or visually hierarchical. It records measurements and repair context for the candidate-and-screenshot-bound visual review. If Playwright or Chromium is unavailable, return `missing_capability`; never pretend rendered QA passed.

Text collision inspection includes content within a module and page-level material
such as footers. Browser Range rectangles locate candidate intersections; browser
font ink metrics exclude unpainted advance space. Text-node line fragments include
wrapped lines and MathML text; each mathematical expression owns its internal layout.
Per-owner collision counts and areas enter the existing editor repair loop so partial
repairs remain measurable before visual review.

Residual-space observations use actual rendered container boxes and painted content, not inferred page lanes. Nested or coincident measurements retain their module ownership when consolidated before selection. These are local spatial facts for review, not a complete map of whitespace or a layout acceptance threshold.

## Reference-aware visual-review gate

Reviewer `strengths` and `global_directives` are optional audit fields. The shared result boundary normalizes absent fields to empty arrays without another model call; supplied values, verdict consistency, actionable issues, and exact artifact bindings remain validated.

`inspect` writes one canonical `inspection.json` beside its screenshot and visual-evidence bundle. `prepare-visual-review` requires that file and the exact candidate HTML, then derives the physical page, current grounded module bindings, rendered-inspection issues, screenshot, and Chromium evidence without a second caller-authored summary. It creates `scientific-poster.visual-review-request.v9`, binding the candidate screenshot, current evidence, sequence number, optional incumbent screenshot, and visual criteria. The built-in operational reviewer receives current rendered copy and measurements. Persisted v9 request instructions retain their existing exact-match contract; the VLM execution prompt carries copy-ownership and review-scope clarification.

Design requirements travel separately from reference observations. The bound `content_brief.design_requirements` records the reference source kind, current `user_direction` and `visual_preferences`. Composition and review share the white-canvas/open-section defaults in `posterlib/visual/design_direction.py`; explicit direction and custom-reference identity take precedence within their scope. Manual revision direction persists with the draft checkpoint. The portable `prepare-visual-review` action accepts `user_direction` and `visual_preferences` alongside `reference_image`, so the same requirements reach its reviewer.

The verbatim `input` and caller `feedback` remain authoring instructions. Final visual review independently judges the current screenshot, displayed copy and measured evidence. Its conclusions cover visual communication and internal consistency within that evidence; the source-aware author owns fidelity to the complete paper during composition. Scholarly attribution identifies the study and its published version, including identity and publication metadata. Production records describe the process of creating this poster, including generation, checking and delivery. The reviewer determines a passage's role from its subject and referent in context and preserves research attribution and qualifications. Reference hashes remain provenance and receipt bindings; historical authoring context remains with the author.

An image-capable harness returns `scientific-poster.visual-review-result.v9` with the same candidate and evidence bindings; the receipt is `scientific-poster.visual-review-receipt.v9`. The adapter verifies the original full-resolution screenshot hash, then sends a bounded whole-page overview so local vision models do not spend the review budget decoding poster-scale pixels. A revision review sends the incumbent and proposal together, followed by the optional communication-quality reference, so one judgment records `improved`, `unchanged`, or `regressed`. A local gain does not count as improvement when another material dimension becomes worse. A bounded labeled detail atlas covers scientific carriers and their module context, including source figures and native diagrams, tables or equations. The adapter arranges these panes in a labeled image for the configured visual transport. The custom reference is not scientific evidence or a required layout. The reviewer must reject a clearly material and correctable gap in scanning hierarchy, visual explanation, or finish without requiring the reference's exact objects or styling. A pass cannot simultaneously report a noticeable quality deficit against that reference. The overview establishes whole-page hierarchy and placement; exact Chromium observations and any atlas support closer detail reading without changing the delivered physical scale. The central claim, explanatory copy and decisive evidence must work for a standing reader at about one metre; image zoom cannot establish print readability. Source figures remain immutable image leaves: they may be moved or resized intact, never cropped, redrawn, or internally edited. A reviewer may cite their observations as evidence, but an editable target is an owning module or the poster root; restyle/reflow cannot redraw internal legends, axes, labels, panels, or pixels, while `content-replan` may compress surrounding grounded copy without editing figure pixels. Each model-authored issue contains only criterion, target ids, and operation. The host binds relevant immutable observations by criterion and target when it creates the receipt, so the vision model never copies machine ids or repeated measurements. Layout-solution prose is intentionally absent from the issue contract. A pass requires no critical issue or global directive. `OMNI_VLM_ENDPOINT` accepts an OpenAI-compatible API base URL or a complete chat-completions URL; the Skill appends the standard path to a base URL, uses bearer auth, and never infers a provider. Scores and strengths are optional diagnostics. Available numeric scores, including fractional values, are retained; missing diagnostics leave the verdict and actionable issues intact. A revise result routes one or more issues through `restyle`, `reflow`, or `content-replan`. Sequence numbers are audit metadata; they do not turn resource exhaustion into a quality verdict.

Each review uses the selected HTML's screenshot and browser measurements. Geometry defects remain explicit and cannot become a visual pass. The author receives the review's current-state diagnosis, target modules and immutable observations, then chooses the repair. During drafting, all feedback returns to the same source-aware editor: copy, diagram labels and layout remain editable together. Explicit public layout revisions preserve their scientific snapshot; requested content edits use the named target scope.

Planning, authoring and visual review share the host execution envelope. `workflow_timeout_seconds` and `authoring_timeout_seconds` are optional caller limits. Each stage uses the remaining shared window, and cancellation propagates to its active request.

Durable drafts save completed planning, reference, design and authoring stages through atomic file replacement. A fresh invocation with the same host task and workflow-step identity resumes that checkpoint and repeats the unfinished stage. Initial authoring also retains its best available authored candidate on cooperative cancellation; recovery remeasures that HTML before continuing. The neutral content scaffold still requires composition. Cancellation during proposal publication or comparison restores the selected candidate and its saved state before propagating to the host. An in-flight model generation restarts on recovery; automatic retry scheduling belongs to the host. A separate task can continue a published candidate through `revise` with its HTML and adjacent `.poster-state.json` file.

The editor returns the first changed, reviewable candidate to the outer workflow. Pairwise review compares it with the incumbent and promotes an overall improvement. A regressed or unchanged proposal retains the incumbent and its saved state. An unavailable call or exhausted deadline returns the actual partial result. No configured VLM yields deterministic-only pending review; a configured unavailable VLM remains pending in VLM mode.

The shared editor supports exact local edits, native DOM moves, complete stylesheets, and layout replacement with authored modules. Empty data-poster-ref markers restore retained title, module, image or formula nodes; classes and styles belong on surrounding wrappers. Draft authors may combine or split modules from the complete source. Explicit full-layout revisions preserve current copy and bindings. Content-replan allows current prose by default; optional targets narrow that scope. Source images remain intact and formulas remain native.

Content-replan edits use their named modules as the prose boundary for both user corrections and review feedback. Headings, captions, diagram labels and ordinary text are editable within that scope; the bound paper supplies their scientific context. Structured content roles support copy tools but are optional for ordinary DOM text. Native MathML remains the formula representation; a stored LaTeX transcription is retained when supplied. Saving and restoring a directly authored MathML tree preserves that representation. Authored source labels provide human-readable traceability and may combine section, table and equation references. Their scientific support is checked against the complete paper during joint authoring; static HTML validation requires a non-empty label and preserves bound source objects.

Every author receives the stored ReferenceAnalysis, original user direction and current edit request. Current explicit changes take precedence. Each staged candidate owns its sidecar; unselected proposals cannot change the selected version's checkpoint. Revisions can recover embedded figures and complete source text without the original asset paths. User-fixed page dimensions remain unchanged.

After authoring, HTML is the current editable copy. The checkpoint's `content_budget` is the initial plan, not current prose or an independent scientific authority. Review briefs derive current module/source bindings from the selected HTML and omit the body-text transcript and whole paper, so visual readability is judged from the rendered image. Candidate checkpoints store the contract belonging to those exact bytes, including when the editor falls back to an earlier candidate. Published results do not return the original budget as current content. Decision targets use `page_plan.content_contract.modules`, and an `author-ready` recovery reuses the exact saved HTML. `estimate` and pre-author recovery still return their content budget.

Each checked candidate retains its complete rendered inspection, including screenshots and visual evidence when geometry is blocked. Compact editor feedback is a separate projection. Visual review can diagnose the retained candidate while its deterministic errors continue to require revision.

Background execution never waits for an operator. When configured review requests a change, the result may include `scientific-poster.decision-request.v2`, bound to the current HTML hash. Each actionable option carries its own continuation: layout revision uses `full-layout`, while grounded copy compression uses `content-replan` plus exact `content_replan_targets`; `keep-draft` has no continuation. A later ordinary `revise` invocation copies the selected option's continuation and adds its required fields. Deterministic-only operation without a configured VLM does not create this checkpoint.

## Estimate and page-fit contract

`estimate` returns `estimate_complete` with the normalized `content_budget`, `page_plan`, source summary, expected figure hashes, and warnings. Source-only portable calls return `host_agent_required`; callers may supply a prevalidated grounded budget for deterministic portable estimation.

Explicit pages reject invalid dimensions through invalid_page. Initial evidence normalization protects the supplied structure, source locations and prepared figure identity. Page estimation is a starting-size heuristic. The author uses actual evidence and rendered measurements to organize wording, figures, tables and diagrams on that sheet. Code does not certify the meaning of paraphrases or select their layout.

An auto page plan starts from common academic bounds and estimates content pressure without compiling layout. The host author chooses one complete composition inside that canvas from the pixel-derived reference brief and the target modules' actual depth. Chromium remains authoritative for final overflow and containment. A user-specified physical page is a hard geometry and capacity contract. Final VLM review judges the actual candidate and current user direction, not conformity to initial planner values. Page estimation never asks for filler.

## Selection state

The checkpoint `authoring_request` preserves the original user direction
across revisions. The author receives it through the existing content brief's
`user_direction`, separately from the current caller and reviewer feedback. Current
explicit user changes take precedence; unchanged requirements and accepted design
decisions remain in effect. Versioned checkpoints require this field. This does not add
reference prose or historical authoring context to the operational image reviewer.

The preview proxy injects its overlay only into the served response. `poster.html` bytes remain unchanged. A valid machine-owned selection stores:

- source HTML SHA-256;
- selected stable poster id, nearest poster module, semantic roles, and module priority;
- bounded text sample, rectangle, and computed styles;
- capture timestamp.

The server rejects stale hashes and identities that do not exist in canonical HTML.

## Acceptance and handoff

Draft and revise return their artifacts with the actual review state. An optional approve action records explicit approved=true for a selected source_html_path and source_html_sha256. It saves a self-contained HTML copy, an optional matching review and approval.json. Acceptance preserves the independent review verdict, including revision-required or not-reviewed. It records the user decision for this version; scientific accuracy and native-layout quality keep their own evidence. Approval uses the saved artifacts directly, with one HTML version binding.

## Editable PPTX export

The direct `export-pptx` action accepts exact validated HTML and an output directory; it does not require an approval or visual-review receipt. In the normal Omni `draft`/`revise` flow, automatic export attempts the delivered statically valid draft even when deterministic inspection reports layout defects. Reviewer availability or a subjective revise verdict changes approval and revision state, not whether the authored candidate can be serialized as an explicitly unapproved editable PPTX. Inspection failures remain attached to the partial result; export success does not turn them into a visual pass.

Chromium captures a versioned scene with stable object ids, physical slide geometry, native text, native tables, semantic MathML equations, module/title shapes, and figure images. The Python renderer uses `python-pptx` to write one custom-size slide; it never places a full-poster screenshot on the slide. MathML becomes native editable Office Math without an image fallback. OpenXML verification confirms one slide, retained object names, and native equations.

Solid box paint retains supported two-dimensional rotation, scale and reflection as native
shape geometry, including ancestor transforms and transform origins. Slide-bound warnings
use the rotated frame. Disjoint solid shapes retain group opacity through native fill
alpha. Overlapping group composites, skew and perspective return capture diagnostics
when the native representation cannot preserve them. Text, tables, equations and images use their directly measured font and box
dimensions; their unsupported transforms also return diagnostics.

Native HTML cell spans become native PowerPoint table merges. Browser-measured cell edges determine column widths; covered grid cells remain empty so merging cannot duplicate or lose scientific text. Origin cells may carry ordered native text runs whose concatenation exactly reproduces the authoritative cell text, including authored line boundaries; covered cells never receive those runs. Plain scenes without cell runs retain table-cell styling compatibility. Math inside a cell retains its separately measured native Office Math object.

Outside ordinary native table-cell runs, text uses editable browser-measured fragments, including paragraphs, headings, multi-column flows and list/math/nested-table cells. A continuous inline paragraph containing mathematics uses one native paragraph with natural wrapping: ordinary DrawingML text runs and complete Office Math zones share its flow. Positioned and fragmented content retains its independently measured objects. Chromium supplies shaped text offsets; bounded DOM ranges supply physical fragment positions. Native list markers retain Chromium's marker content and geometry. Confirmed-font fragments keep their captured size and line boundaries. Editing a fragment changes that native text object; paragraph-wide reflow across separate objects requires layout editing. Visible pseudo-text without a native scene mapping, including styled first letters, returns an explicit capture error.

Paper-original SVG figures retain their exact SVG media in a PowerPoint picture,
using DrawingML and the Office SVG extension to reference the same resource.
Their vector paths and original embedded bitmap content remain intact; editing
applies to the whole image. The exporter creates no raster replacement for SVG.
New inline SVG diagrams use the separate native path/text adapter described in
the skill's drawing contract. Recipient-software rendering remains a distinct
verification step for both paths.

The visual-review receipt binds the HTML screenshot. OpenXML counts establish native object retention. Mathematical appearance, editing and font substitution are assessed separately in Microsoft PowerPoint, the supported PPTX target.

The export gate blocks failures of basic editability: no native text, an unbound image covering the complete canvas, text boxes that can grow beyond captured bounds, or broken required object identity. Bound paper-original figures retain their image editing scope at any allocated size. Slide bounds, missing portable-font preflight, and native-object overlap are warnings; the reference-aware visual review owns layout and readability judgments. Every failure or warning names the affected object and a suggested patch. The result returns `poster.pptx`, `poster-scene.json`, `poster-pptx-rubric.json`, and OpenXML counts.

HTML remains the canonical authoring state; the PPTX is its directly exported editable delivery companion.
