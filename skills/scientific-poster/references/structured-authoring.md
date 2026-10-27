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

When the source is a local PDF, give `paper_path` to the Skill instead of reading the binary through a text-file tool. PDF preparation is a distinct pre-authoring stage: page-aware text and bounded visual carriers become the model's source and optional visual assets. The same first-page geometry may recover optional verified affiliations only from the author-to-Abstract identity band or a numbered organization footnote at the page bottom; it stops before contact, proceedings, copyright, or body text, and never guesses venue branding from an institution. Associate complete text objects with the artwork and its nearest caption; preserve labels above or beside a figure and legends before the caption without pulling in clipped neighbouring prose. A horizontal separator spanning the owned graphic component starts a new object-ownership cell: text above it belongs to the preceding object, while complete labels below it may join the figure, and crop padding never crosses back over that boundary. Caption discovery works at native line level even when prose precedes it in the same text block, and aligned short panels are grouped under that caption before figure-size filtering. Distinct physical captions keep distinct asset paths even when figure numbers repeat. Preserve a covering raster XObject without re-encoding when its pixels reproduce the visible page region; use a self-contained SVG crop for transforms, transparency, overlays, and vector content, with a 450 DPI PNG fallback for unsupported page rendering. After native Figure extraction, the same isolated worker uses the local layout reader to complete captions and recover bounded `pdf_table` and `pdf_algorithm` visual carriers. A Table carrier requires a preceding picture with no intervening caption; an ordinary structured table remains text. An Algorithm carrier ends at the next section boundary. Never put a PDF formula crop into the poster. When the paper carries a first-page arXiv version stamp, the source adapter may retrieve title-matched arXiv HTML and expose renderable LaTeX as optional `source_ref` transcription shortcuts. Missing enrichment preserves the native result and does not disable source-grounded mathematical authoring by the main model. If the PDF reader itself is absent, stop with the returned installation command; never treat the user's request string as paper content.

## Choose composition from the evidence

- Figure-led is useful when one decisive visual deserves the largest uninterrupted region.
- Method-led is useful when architecture, procedure, or study design needs the clearest spatial flow.
- Result-led, balanced, asymmetric, or multi-entry compositions may fit other evidence distributions.

These are descriptive emphases, not closed layout modes. Semantic roles never become fixed slots. The reference interpreter observes one exact image without target content shape or role weights. It returns transferable spatial grammar grounded in the source's actual regions, count, arrangement, approximate relative proportions, dominance, grouping, rhythm, and open versus enclosed treatment. These are source observations, not a code-prescribed target topology. It distinguishes heading-only treatments from whole-region surfaces and records local exceptions and uncertain type details rather than inventing them. The host text model receives the target content, page and user direction separately. It reproduces the observed design relationships when replication is requested, or adapts topology, type, color and surfaces to new content when adaptation is requested. The interpreter does not compile lanes, colors, CSS, or coordinates for the target, and reference pixels never supply its scientific content or identity. If pixel interpretation is unavailable, authoring continues from the source carrier and user direction. Rendered visual review determines the final visual state.

The page plan reports physical capacity and a coarse balance estimate. The model chooses track count, grouping, spans, and local subgrids from actual copy depth, figure density, and semantic importance. Let each heading, carrier and interpretation form a group with intrinsic depth and explicit local gaps. Give table and comparison rows their natural cell height with consistent padding, allowing wrapped labels to expand locally. Place groups by their measured depths; reserve additional continuous area for readable evidence and larger intervals between distinct reader questions. A figure's width need sets its horizontal measure; its intact proportions and explanation set group depth. Public `estimate` remains browser-independent and supplies an orientation and starting-size heuristic. Chromium measures the authored result's exact geometry, while the model owns placement. A user-specified physical page remains a hard capacity contract.

Default to a white (`#FFFFFF`) canvas with white or transparent content regions. Choose boundaries locally: open regions when proximity, alignment and headings establish membership, light dividers between adjacent reader questions, and restrained complete outlines where related elements need a shared region. Gather each heading, evidence carrier and interpretation inside its visual group. A poster can combine these treatments, using the lightest boundary that makes the relationship clear. Keep color in intact source figures, data encoding and restrained heading emphasis. Borders and rules also serve information-bearing structures such as tables and axes. Decorative single-side borders, accent rails, heading stripes, colored section cards and whole-page color washes fall outside the default treatment. Explicit user direction and custom references can override these defaults within their requested scope. Built-in references provide reading hierarchy, proximity and evidence scale; their canvas tint and decorative framing remain observations.

Use native list styling for bullets and numbering; Chromium's actual markers export as editable text. Use sequence numbers for actual ordered processes, not unordered claims or provenance. Authored symbols and connectors belong in real DOM elements, not CSS pseudo-content. `aria-hidden="true"` controls accessibility exposure, not visual visibility or permission to change printed text.

Visible copy serves the research argument and scholarly attribution. The workflow owns production records and delivers them separately; requests for traceability use that record channel. Accounts of assembling, sourcing, checking or exporting the poster belong in those records, with evidence bindings in HTML data attributes and source metadata. Preserve research provenance, experimental conditions and scientific qualifications when editing a passage that also contains production notes. In module fields, `provenance` means scholarly attribution. `footer` describes reading priority. Give each body or attribution group, including footer copy, a module owner.

Compose wording and visual relationships together from the complete paper. A fixed
page with prepared identity starts directly from its source carrier. Use `estimate`
when a starting page size is needed. The evidence budget proposes content for that
estimate; the author selects and combines it during composition.

Each module groups material around a reader question. It may have several semantic
roles and combine related figures, native tables, equations, flow steps and prose.
`visual_kind` identifies its primary carrier. Priorities describe emphasis; a balanced
composition or several coordinated focal entries are valid. Reuse a source figure
where it helps readers compare related material, keeping its pixels intact. Prefer
concise reading keys and qualifications that add meaning to the visual evidence.

A native table uses `table={"columns":[...],"rows":[[...],...]}` with literal string
cells and rectangular rows. Labels, units, conditions and baselines explain its
comparison frame. Related table data may accompany another primary carrier. Native
flow steps represent a sequence or relationship, with explanatory prose alongside
when useful. The initial renderer retains every supplied carrier for composition.

Keep source locators in `source_label`, `data-source-label` and delivery metadata.
Visible copy explains the research; scholarly attribution uses conventional
references, acknowledgments or reproduction credits. Select limitations and boundary
conditions supported by the paper.

Available figure observations describe panels, readable labels and units, density,
surfaces and continuous width needs. Use those observations with the complete source
to select figures and allocate space. Descriptions follow the plotted quantities and
their scope. Chromium measures the composed page; visual review assesses the reading
impact at its physical size.

Use `visual_kind: figure` for a prepared process or architecture image. Use `visual_kind: method-flow` only for a native editable sequence, represented as `flow_steps: [{"label":"short node label","detail":"one direct transition or result"}]`. A method-flow may include related source figures and explanatory details. The flow steps record the ordered relationship. The HTML author can organize its actual nodes, branches, groups and labels from the complete source.

Keep mathematical content grounded in the complete source and preserve its operators, grouping, subscripts, exponents, conditions, and qualifiers. Prepared equation references are optional transcription shortcuts, not permission gates. Write source-grounded inline mathematics with explicit delimiters and display equations as LaTeX with source labels. Code, paths and language syntax remain literal native text when that is their scientific role. The shared converter determines supported mathematical syntax. Inline math is for short notation that reads with a sentence or table row. When a contribution, mechanism, or guarantee depends on parsing a complete expression, use one equation carrier and keep nearby prose to its interpretation. If an expression is not legible enough to transcribe reliably, state only its supported qualitative meaning. Retain only essential expressions that remain readable at poster scale. The shared converter renders exact LaTeX as native semantic MathML and keeps it in `data-latex`; native hosts use the [equation-fragment command](#native-equation-fragments) rather than hand-transcribing the math tree. Use `<math display="block">` for standalone equations and `<math display="inline">` for notation within prose. The neutral content-carrier serializer recognizes `\(...\)` and `$...$` in text-grounded scientific copy through maintained delimiter parsers and the same MathML backend; it never infers equations from unmarked text. Do not override MathML's inner display type with ordinary CSS block, flex, grid, or inline-block. Give a long equation more inline measure or a wider span before reducing type. If its source semantics permit line breaks at relation operators, use `mtable`/`mtr`/`mtd` to align the source-equivalent lines. Escape a less-than operator as `&lt;` inside MathML. Unconvertible inline source remains visible for correction rather than creating another draft gate. Remote MathJax/KaTeX, scripts, and equation screenshots are not valid authoring. PPTX export retains complete Office Math expressions and ordinary editable text. A continuous inline paragraph shares native text-and-math wrapping; positioned, preformatted, column-fragmented or float-shaped content retains the measured object path. Microsoft PowerPoint is the supported target for native layout and editing.

`table.view="comparison"` is a carrier choice, not a formatting upgrade. Use it only when aligned magnitudes are the module's primary reading task and every displayed numeric column belongs to the same comparison frame. Its existing columns and rows remain the only data: finite nonnegative value columns render as aligned bars with independent zero-based scales, unchanged row identities, literal values, and units from the headers. Choose a literal table by omitting view when the cells require mixed or qualified notation. An explicit comparison requires finite nonnegative scalar columns with positive maxima. Incompatible cells return planning feedback through the existing repair loop; the renderer preserves the selected carrier. Do not remove qualifications to obtain a chart. Marks and labels export through the existing editable shape/text path, not a second chart renderer or a linked spreadsheet chart. As with scientific figures, visual revisions must preserve the mapping between values, scales, labels and marks; changing a displayed value requires updating its plot, not just editing the label. The text snapshot does not independently certify that geometric mapping.

For automatic sizing, start from common academic bounds, including landscape formats up to 2:1, and treat the first area estimate only as an orientation and maximum-capacity heuristic. Estimate the initial page from the evidence proposal. Predicted occupancy remains context for composition. The author organizes a complete, inspectable scientific argument, then uses Chromium measurements and visual feedback in the same editing loop. Preserve an explicit page unchanged. The estimator selects one direction from coarse predicted overflow, occupancy, common-page area, and selected-figure shape; those signals do not accept or reject the rendered poster. Preserve an explicit orientation unchanged. Select one attributed reference for that page orientation. The author model chooses the complete topology from the transferable spatial grammar and the grounded modules' actual depth. Chromium measures the authored result, and its diagnostics return to that same model for a bounded edit sequence. A `design-ready` recovery recreates authoring from the saved plan; `author-ready` recovery reuses the saved HTML. Compact means content-bounded, not small type.

Choose locally available font stacks that express the reference's typographic relationship and the user's preference; serif display with sans body, all-sans, or a restrained hybrid can all work. Body text uses regular weight and a comfortable reading width. Display styling and strong weight belong to brief headings or selected emphasis. Read each treatment in its observed role; placeholder marks leave body typography unspecified. Keep the heading, evidence carrier and reading key close enough to read as one group, including inside shared panels. Concrete CSS colors follow the current design requirements while scientific figure colors remain intact. Treat role sizes and line measures as advisory conference-viewing evidence evaluated from the screenshot. Footer priority controls reading order while substantive limitations retain readable scale. Give wide multi-panel plots enough physical width for axes and legends or omit them. Use `focal` priority for prominent entry points when the argument benefits from them; balanced compositions can use `primary` throughout.

Size prominent copy from its rendered shape, not from a fixed character-width template. When a title or central claim wraps awkwardly, widen its measure or rebalance adjacent groups before reducing type.

Establish the physical reading roles during the first composition. Essential equations, table labels and explanatory copy support a visitor standing about one metre from the printed page. Necessary conditions and limitations retain readable scale wherever they appear. Use the supplied page-dependent type references when allocating space; choose concise supporting content and sufficient measure within the current edit authority. Express principal role sizes in CSS millimetres, or convert points using `1pt = 25.4/72mm`. Inspect computed sizes after composition, including inherited and inline styles. Typography review records each complete MathML expression at its base size. PPTX capture also records browser-computed sizes of literal mathematical text so native Office equations preserve its script scaling. Page and module typography observations carry the actual role distributions into visual review, receipts and author feedback.

## Common layout pitfalls

- `flex: 1` includes positive `flex-grow`; on semantic columns it often forces equal heights and leaves the shorter column empty at the bottom. For a bounded column of poster modules, use intrinsic items (`flex: none` or `flex: 0 0 auto`) so a module frame cannot shrink below its content; keep `min-width: 0` on inner prose wrappers for horizontal wrapping.
- A flex/grid parent defaults to cross-axis stretch in common configurations. Use `align-items: flex-start` when columns must remain independently compact.
- `gap` combined with growing children makes the location of spare space hard to predict. Keep gaps explicit and let content define column height.
- A prose-bearing flex child with `flex: 0 0 auto`, a content-sized minimum width, or `white-space: nowrap` can expand beyond a narrow rail. Put `min-width: 0` on text-bearing flex/grid children and use wrapping or `minmax(0, 1fr)` tracks for compact comparisons.
- Fractional rows inside content modules, fixed `min-height`, and `justify-content: space-between` can distribute blank space while wrappers appear full. Use fractional tracks only when every track contains real content; module interiors remain intrinsic.
- `min-height: 0` on a content-bearing grid item allows the item box to shrink below its prose and figures, so children visibly spill into the next row even when the grid itself appears to fit. Keep poster modules intrinsically sized.
- Giving several direct grid children only `grid-column` does not create a coherent macro composition: auto-placement keeps advancing the row cursor, so a later group can start halfway down the page. Assign both grid axes explicitly or nest modules inside intrinsic scan-section groups.
- Do not mix direct modules and track wrappers in the same body grid, or place later modules in rows whose height is fixed by an unrelated focal item. The tallest item then stretches shorter content into visible voids. Prefer independently flowing groups when module heights differ.
- Choose column breaks from rendered intrinsic height and actual section headers, not equal module counts. Preserve a clear reading entry and ending while distributing independent evidence by measured demand; several tall figures in one column and short text in another is not a balanced scaffold.
- A reference's literal lane count is observed design evidence, not a code-selected quota. Follow the user's requested fidelity; for a new composition, adapt its dominance, grouping, rhythm and asymmetry to the target evidence and actual module depth.
- Use a track-wide section header only when the modules beneath that track have comparable intrinsic depth. Otherwise attach section cues to compact local groups so one short section cannot create an empty column beside an overflowing one.
- Focal prominence does not always require a span. It may come from evidence scale, module depth, position, measure, or contrast; a colored heading alone is insufficient.
- `max-width: 100%` prevents overflow but does not enlarge a prepared SVG or raster figure beyond its intrinsic size. Give evidence figures an explicit responsive width when the available module measure should be used; otherwise an A0 poster can contain technically valid but unreadable thumbnails and a large unused lower field.
- Report physical figure dimensions, image-box dimensions, painted fractions, and actual rendered typography distributions as conference-viewing context. Figure area supplies internal crop-selection salience. Page-dependent typography references guide authoring; their advisory browser diagnostics accompany the measured page and module distributions into review. The reviewer assesses actual marks and type in the full candidate overview, using detail crops for closer inspection and physical measurements for scale. Valid hashes, nonzero rendering, visibility, and an unclipped in-page image remain hard source-evidence requirements.
- A poster with `width: 841mm` may still shrink to the browser viewport because flex items default to `flex-shrink: 1`. Keep the body-level poster root outside a shrinking flex context or set `flex: none`; Chromium must measure the declared physical dimensions.
- Do not fix these traps by stretching an empty wrapper. Chromium records geometry and type measurements; the screenshot reviewer judges whether the remaining space, hierarchy, and readability are purposeful.

## Complete HTML contract

- Begin with `<!doctype html>` and return the entire document without Markdown fences.
- Use prepared author-note associations to compose clear markers or concise named notes in the title band. Wording and arrangement follow the composition. Contact information and body footnotes retain their original scope.
- Use one body-level `main`, `article`, or `div` poster root with `data-poster-id` and inline CSS with `@page` size in millimetres. The root encloses the complete poster: exactly one compact `data-poster-title-band` followed by the internal evidence body. “Outside module styling” never means outside this root. The root itself must render at the declared physical width and height; use `@page` margin `0`, put print margins in root padding, and use `box-sizing: border-box` when the root owns padding or borders. Prefer semantic `main` or `article`.
- Give every independently placeable evidence module one stable `data-poster-module` wrapper and `data-poster-id`. The wrapper is an audit, selection, and export anchor, not necessarily a visible panel. Several modules may share one section and group-level heading; the author model may regroup intact modules while preserving their bindings.
- Assign meaningful carrier metadata to every authored module: `data-semantic-roles`, `data-module-priority`, and `data-visual-kind`. The module with priority `focal` provides the distance-level entry. Each sourced region carries a data-source-label. Drafting may revise the proposal's module roles and priorities to match the actual scientific composition.
- Give meaningful selectable elements stable unique `data-poster-id` values. Every evidence-bearing `data-poster-module` needs a non-empty `data-source-label` copied from an actual source heading, caption label or short excerpt. Machine validation does not infer grounding from one English locator regex; prepared figure hashes and equation bindings remain the authoritative identities.
- Embed supplied PNG, JPEG, GIF, WebP, or safe SVG figures as data URIs. Treat prepared source figures as a provenance manifest and render the grounded subset selected for the composition; a poster may remain text-, equation-, or table-led when no figure earns readable area. Place each selected figure wherever it best supports its grounded interpretation; its hash and provenance travel with it rather than binding it to an original layout slot. A network-capable harness may download an authorized asset and pass its local path through `assets`; portable authoring itself never fetches remote content. `asset://N` is only a transient host token resolved before persistence.
- Place a short grounded interpretation beside or below each selected figure or coherent figure group. When a module already supplies that reader-facing interpretation, do not repeat the paper-original caption beneath its figures; the complete caption remains in image alt/provenance metadata. A multi-figure module uses one shared interpretation only when the relationship among those carriers is itself the evidence. The paper locator stays in `data-source-label` and the separate provenance record. Apply decorative corners to surrounding panels, not to source-figure pixels: retain the complete rectangular source image in HTML and editable export.
- Do not include scripts, event handlers, forms, animation, imports, external fonts, remote URLs, placeholders, or executable SVG. Scientific copy stays explicit DOM text. Native list-style identifiers are supported; custom counter styles, list strings/functions/images and authored CSS `content` are not. `content:normal` or suppression does not author new copy.

- Native PPTX paint uses solid fills, borders, lines and supported SVG geometry. Represent chart tracks and other marks with these editable primitives. CSS background images, including gradients, require an authored replacement before delivery; browser inspection returns their element owners through the existing edit feedback. Paper-original figures use image elements with their source bindings.

## Native equation fragments

The Omni authoring loop exposes `render_poster_equations`. Supply an `equations`
array with each expression's exact source `latex`, its `display` (`inline` or
`block`), and an optional `source_label`. The tool returns MathML fragments without
changing the poster. Prepare them before a layout or local edit, then insert the
returned fragments unchanged within the current editing authority. Existing
equations remain available through their formula references.

For Codex, Claude Code, or an explicitly selected native Omni authoring session,
copy the selected inventory expression's `latex` value into a UTF-8 file without
changing its notation. Convert it with the same `latex2mathml` backend used by the
engine's content carrier:

```bash
python "<skill-dir>/posterlib/content/equations.py" --latex-file equation.tex > equation.mathml
```

Use `--display inline` for inline notation. On success, insert the returned `<math>`
fragment unchanged into the model-authored HTML; retain its `data-latex` and the
owning module's source locator. The model chooses its size, surrounding space and
placement. This preserves grouping, fences, limits and editable Office Math without
equation images or a second renderer. Hand-written unsupported `<mfenced>` is not a
browser-supported substitute: its implicit brackets and separators disappear in
Chromium. A conversion error goes back to the exact source, not a guessed formula.

## Revision

Carry the original user direction through revision checkpoints and supply it to the
author alongside the current request. It defines unchanged requirements, not a command
to redo earlier edits. A current explicit change takes precedence over conflicting
earlier requirements. The operational reviewer receives current screenshots and the
selected quality reference. Existing reference analysis remains author context.

Omni also offers `replace_poster_stylesheet` for a complete CSS reflow without
retyping the scientific DOM. Supply all page CSS, including `@page`; the HTML parser
replaces existing style elements while preserving body content and inline attributes.
Exact edits remain available for local changes. For structural work,
`replace_poster_layout` accepts composed module HTML, surrounding wrappers and a complete
stylesheet. A `data-poster-ref` placeholder retains a complete current title band or module;
image and formula references insert exact protected nodes into newly composed module interiors.
Drafting assigns metadata and copy roles to the current content; a public layout-only revision preserves its existing wording and scientific relationships.
Retain each source figure with its associated caption in its semantic figure group;
the author can reposition and style the group together. Explicit copy roles delimit
scientific text independently of inline or block presentation tags.
The same static and Chromium inspection runs after each operation. Codex and Claude Code
perform the equivalent wrapper and stylesheet edit with their native file tools.

Keep the editor's native assistant/tool exchanges intact, including call ids and a
result for each requested operation. A bounded context window starts with the HTML
checkpoint from before its retained exchanges; those exchanges determine the current
state. Recasting completed calls as fresh user prose loses this execution context
and can make the model repeat an already-applied edit.

Revise the exact source HTML after verifying its hash. The current HTML is the operational design state. During a full-layout edit, the author reads the actual headings and scientific copy in the current HTML so it can compose around their meaning and length. Only embedded image bytes become transient asset tokens; figure identity, aspect ratio, alt text, and DOM ownership stay visible, and the exact image bytes are restored before validation and render. The existing scientific snapshot protects copy and bindings without hiding text from the author. Physical readability references expose measures such as usable width and type scale, not a code-chosen column count or target topology. The already interpreted ReferenceAnalysis remains author context during every revision, including reflow and local edits. Its observations preserve the requested design relationship rather than authorizing a new restyle or freezing a target topology; current explicit user changes take precedence. The selected reference image accompanies operational review as a visual-quality comparison. Built-in references supply communication quality while the bound design requirements govern palette and framing. The author receives the reviewer's concise visible-defect summary, issue criterion, target, operation, and host-bound observations, then chooses the visual solution from the current HTML. The summary diagnoses the current screenshot and reading impact only; strengths and any solution topology remain human-facing receipt context. A selection may contain only stable poster id, nearest evidence module, optional audit hints, text sample, rectangle, and styles. Each visual edit batch uses unique exact replacements for local work, a complete stylesheet for visual reflow, or an atomic layout plus stylesheet for internal scientific graphics and surrounding topology. The layout may mix authored module HTML with whole-module references and protected image/formula references; the existing scientific snapshot validates the restored candidate. Each reference is an empty replacement marker with no other attributes, so layout classes and styles belong on its retained parent wrapper. The editor may continue from a statically valid proposal that reduces a blocker or changes the governing parent relationship without adding or increasing another blocker, while the best checked delivery checkpoint remains available. A proposal with the same blocked relationship and no measured improvement is discarded. Never accumulate duplicate selector overrides. Presentation-only title `<br>`/`<wbr>` tags may be inserted, moved, or removed while the decoded verified title remains exact; a wide unoccupied masthead should not retain manual breaks that leave every line needlessly short. Table span follows the table's scientific role and comparison task rather than column count alone: a supporting numeric table must not displace primary figures merely because it has many columns. Content edits use each named module as its prose scope, including headings, captions, diagram labels and unannotated text. The reserved `poster` copy target covers prose outside modules and verified identity. The bound paper supplies scientific context. Equations, table data, figures, identity and non-target content retain their existing protections. Data-table cells keep their semantics across module boundaries; layout tables declare `role="presentation"` or `role="none"`. The editor lists available structured copy fields for convenient updates; exact replacements cover other prose within the same copy authority. The host stages each batch, validates and renders it, then returns Chromium diagnostics and the current HTML for another batch when necessary. Source-figure/text collision feedback identifies the colliding text element, exact rendered text-line and figure boxes, and the crossed edge so the model repairs the owning boundary rather than unrelated downstream spacing; these facts do not prescribe a layout. When baseline measurements are available, each edit result compares current physical source-figure extents with the inspected revision source, or with the latest reviewed candidate after a visual-review receipt; the feedback identifies its baseline. The editor owns HTML edits and deterministic browser measurements only. The first candidate that clears deterministic blockers returns to the workflow automatically. The workflow reviews the incumbent and proposal in one VLM call and promotes only an overall improvement. An unchanged or regressed proposal leaves the incumbent selected and triggers a different-strategy edit while the shared deadline permits; the loop stops on pass, unavailable review or authoring, or resource exhaustion. A visually imperfect but geometrically valid candidate remains available as an explicitly unapproved HTML/PPTX. Failed geometry and visually unchanged or regressed proposals never replace the published candidate. Each staged candidate carries its own source-bound sidecar; the task checkpoint records completed stages, not the latest unselected proposal. A later revision restores the selected HTML's sidecar, never an unrelated workspace checkpoint.

The initial content budget remains planning history. Review and edit the actual HTML with the complete source available to the author. During drafting, visual feedback can lead to rewritten explanations, different diagram labels or a revised evidence grouping in the same editing session. A separate scientific replanning pass is unnecessary. The incumbent remains selected until pairwise review establishes an improvement.

Ordinary user revisions allow prose and layout changes together through
`content-replan`. Describe the change in `input` or `feedback`; optional
`content_replan_targets` narrows the editable prose to current module ids or `poster`
for unowned copy. Otherwise all current prose is available. These targets describe
permission to edit. The author changes the passages needed by the request and keeps
sound content intact. Explicit `full-layout` preserves existing copy. Receipt-only
revisions follow their issue operations and targets; a passing receipt remains useful
context for a new user request. Figures, table data, equations and verified identity
retain their existing binding contract. The configured author role handles the edit
with the complete bound source. Structured copy roles enable convenient tools; exact
edits handle other prose within the same scope.

Before a model-backed revision, replace embedded image data URIs with transient `asset://N` tokens and restore the exact bytes afterward so figures do not consume the model context. Revision recovers source text and prepared assets from the selected HTML sidecar, including in a new process without a task id. Overflow returns the existing geometric feedback; publication does not silently shrink type or run a second fit mutation.

## Runtime and transport controls

Native comparison marks carry their normalized data fraction in `data-comparison-fraction`.
The scientific table snapshot binds each fraction to its cell. Chromium checks the rendered
mark length against the track's content box and checks that each measure uses aligned,
equally sized scales across rows. Layout may resize the shared scale while preserving the
data relationship. Inline MathML is recorded in order with its surrounding text; authorized
copy edits retain the existing role-scoped editing boundary.

Initial composition includes native scientific graphics, their surrounding layout and one
shared stylesheet. Each current module supplies editable HTML with its scientific metadata,
copy roles, text, ordered method steps and table cells. The author composes internal nodes,
connections, comparisons and accompanying copy from the complete source. Explicit layout-only revisions preserve the selected document's scientific snapshot.
`data-poster-ref` can retain a complete current title band or module, or insert an individual
image or formula into an authored module. Each scientific element appears once, through its
parent reference or its own reference. `protected_content` describes each image or formula;
their bytes and MathML trees remain host-owned. References are derived from the current HTML
on every edit. The same map builds the context and restores the nodes before validation.
Reasoning settings come from the configured model provider. Runtime limits apply
to the complete model call, including reasoning and visible output; token usage and
latency depend on that provider and the selected model.

Planning, authoring and visual review share one optional execution deadline. The host owns its runtime allowance and cancellation. `workflow_timeout_seconds` optionally sets a caller deadline for the action; `authoring_timeout_seconds` optionally limits an authoring session within that envelope. Both accept positive finite durations. `authoring_transport_retries` controls transient transport retries (0–2). Evidence-plan repairs continue while the response changes and the shared deadline permits; `max_repair_attempts` optionally caps those repairs (range 0–10). The stateful HTML editor validates every proposal before promotion.

The editor prefers native tool calls. A chat-only host, or the first native request failing before any edit, uses one JSON command per ordinary non-streaming text response: `{"name":"apply_poster_edits","arguments":{"edits":[{"old":"exact old text","new":"new text"}]}}`. All editor commands retain the same dispatcher, scientific-content checks, Chromium measurements, and automatic handoff of the first reviewable candidate. A transport fallback never authorizes additional edits or relaxes acceptance.

Streaming and non-streaming message calls request the same 65,536-token authoring
allowance through the host's existing public interface. A minimal chat-only host
controls its own output limit. Reported output truncation fails the model call;
the workflow retains its last validated candidate and current review state.

## Runtime responsibilities

Files use `snake_case` and name their actual role. Workflow modules use the
`*_pipeline.py` suffix; `*_checkpoint.py` means persisted workflow state,
`*_request.py` constructs a request, and `*_feedback.py` prepares measured feedback.

| Module | Responsibility |
| --- | --- |
| `engine.py` | Dispatch actions, own the progress-based review/revision loop, publish and deliver results. |
| `workflows/draft_pipeline.py` | Prepare source, plan, author and resume a draft. |
| `workflows/revision_pipeline.py` | Edit one selected HTML version using its own grounded state. |
| `workflows/review_pipeline.py` | Review one candidate and persist or reuse its receipt. |
| `workflows/draft_checkpoint.py` | Validate and persist completed drafting stages. |
| `workflows/revision_state.py` | Bind revision state to the selected HTML and derive revision authority. |
| `workflows/decision_request.py` | Describe the available next actions for a reviewed version. |
| `generation/authoring.py` | Construct author prompts and the initial scientific-content HTML. |
| `generation/model_runtime.py` | Call the text model and run one stateful HTML editing session. |
| `visual/inspection_feedback.py` | Prepare browser measurements for the editor; impose no layout policy. |
| `visual/review_contract.py` | Validate review requests, results and receipts. |
| `visual/vlm_review.py` | Submit one bound review to the VLM and parse its response. |
| `delivery/approval.py` | Record explicit acceptance of one HTML version and retain its review state. |

Module paths in this table are relative to `posterlib/`, except `engine.py`.
