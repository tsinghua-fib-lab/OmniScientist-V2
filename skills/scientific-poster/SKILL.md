---
name: scientific-poster
description: >-
  Create or revise a complete academic poster from a paper, with native editable
  PPTX, HTML and PNG delivery. Use for scientific, conference or research posters.
  Accepts paper PDFs, LaTeX projects, arXiv papers and text sources. New diagrams,
  text, tables and equations use native objects; paper-original figures remain
  intact. Owns whole-poster composition, visual revision and export. Drawing skills
  provide optional components; the poster author chooses which candidates to retain.
license: "Apache-2.0 for original code and documentation only; third-party visual assets are excluded (see NOTICE.md)"
metadata:
  helixforge:
    version: "1.0"
    dependencies: ["python>=3.11", "pymupdf>=1.28", "pymupdf4llm", "skia-pathops>=0.9.2,<1", "fonttools>=4", "lxml>=5,<7", "Pillow>=10", "httpx>=0.27", "playwright>=1.49,<2", "chromium", "tinycss2>=1.4,<2", "latex2mathml>=3.81,<4", "pyparsing>=3.2,<4", "pylatexenc>=2.10,<3", "markdown-it-py>=4,<5", "mdit-py-plugins>=0.6,<0.7", "python-pptx>=1.0.2", "mathml2omml==0.0.2"]
    runtime_requirements:
      python_modules: [tinycss2, pymupdf, pymupdf4llm, lxml, PIL, httpx, pylatexenc, playwright]
      dependency_setup_command: "omni update --force"
    allowed_tools: [write_file, bash, read_file, cite_source, record_claim, add_evidence, log_run]
    research_contract: portable_provenance_v1
    kind: python_engine
    tier: research
    role: task
    status: experimental
    priority: 90
    delivery_mode: async_task
    execution:
      effects: [user_files.write]
      max_seconds: 1800
    engine:
      module: engine
      class: ScientificPosterEngine
      method: execute
    capabilities: [poster.scientific]
    deliverables: [artifact.poster]
    default_for: [scientific poster, academic poster, conference poster, HTML poster]
    workflow:
      failure_policy: continue_with_partial
      allow_failed_dependencies: true
    input_schema:
      type: object
      required: [input]
      properties:
        action:
          type: string
          default: draft
          description: >-
            draft makes the complete native PPTX, HTML and PNG: it reads the paper,
            composes text and diagrams, reviews and revises. revise requires source_html_uri
            plus current input/instructions, feedback, visual_preferences, or visual_review_path.
          enum: [prepare, estimate, draft, revise, validate, preview, inspect, prepare-visual-review, submit-visual-review, export-pptx, approve]
        input:
          type: string
          description: complete current user request preserved verbatim; also supply the paper source for draft or source_html_uri for revise as structured arguments
          x-omni: {semantic_role: instruction}
        paper_path: {type: string, description: "local paper PDF path or artifact URI; used when latex_project_dir is absent, ahead of arxiv_id"}
        latex_project_dir: {type: string, description: "complete local LaTeX project directory; primary source when supplied, preserving formula source and original figure files"}
        main_tex: {type: string, description: "existing .tex root; project-relative, invocation-cwd-relative, or absolute; resolved path must stay within latex_project_dir; distinct matches are ambiguous"}
        arxiv_id: {type: string, description: "arXiv id or arxiv.org abstract/PDF URL; retrieves TeX then PDF when neither local LaTeX nor paper_path is supplied"}
        source: {type: string, description: local or artifact URI of a UTF-8 grounding file}
        source_text: {type: string, description: grounded scientific source when no file is supplied}
        page: {type: object, description: "Fixed physical size as {width_mm: number, height_mm: number}; omit for automatic sizing"}
        assets:
          type: array
          description: "Optional image paths or single-slide native PPTX records with path/uri and required preview_uri pointing to a producer-supplied SVG/PNG. Declared workflow PPTX dependencies retain their explicit preview_uri pairing; draft can also compose native diagrams."
        orientation: {type: string, enum: [auto, portrait, landscape]}
        reference_image: {type: string, description: optional exact custom visual reference}
        conference: {type: string, description: conference name and optional year explicitly requested for venue identity on the poster}
        title: {type: string, description: optional caller-supplied full paper title correction; structured paper inputs normally provide identity; takes precedence over extracted metadata}
        authors: {type: [string, array], items: {type: string}, description: verified complete author names in source order; a name array is preferred; affiliation markers and author notes belong to title-band composition; takes precedence over extracted metadata}
        affiliations: {type: array, items: {type: string}, description: verified affiliations; takes precedence over extracted metadata}
        instructions: {type: string, description: additional authoring direction; retained with input across revisions}
        research: {type: object, description: provenance identifiers and optional explicit text grounding; use source or source_text for a complete paper}
        visual_preferences:
          type: object
          description: Optional free-form visual overrides; current values merge with saved preferences. Put general revision goals and visible defects in input or feedback.
          properties:
            typography: {type: string, description: desired type treatment and readability}
            framing: {type: string, description: desired grouping and boundary treatment}
            accent_color: {type: string, description: desired emphasis color}
          additionalProperties: false
        content_budget: {type: object}
        source_figure_sha256s: {type: array}
        source_html_uri: {type: string, description: "Required for revise: the selected candidate's returned html_uri, or local html_path with its adjacent .poster-state.json checkpoint"}
        source_html_sha256: {type: string, description: optional returned html_sha256 to require the exact selected candidate bytes}
        visual_review_path: {type: string, description: saved visual-review receipt for the selected HTML; supplies review context for the current user request or its own revision diagnostics}
        visual_review_request: {}
        visual_review_result: {}
        feedback: {type: string, description: current revision goals, visible defects, or source-grounded prose corrections}
        revision_mode:
          type: string
          enum: [full-layout, content-replan]
          description: "Optional edit scope: full-layout preserves scientific content; content-replan permits prose edits with the requested layout changes. Current user requests default to content-replan. A receipt-only revision follows its issue operations."
        content_replan_targets:
          type: array
          items: {type: string}
          description: "Optional content-replan scope: existing data-poster-module ids, or poster for prose outside modules and verified identity. Omit for all current prose; a receipt-only revision uses its copy targets."
        author_model_role:
          type: string
          enum: [main, vision]
          description: Select the configured main or vision model for HTML authoring and revision. New drafts default to main; resumed drafts and revisions inherit the saved choice when omitted.
        max_repair_attempts: {type: integer, minimum: 0, maximum: 10}
        workflow_timeout_seconds: {type: number, exclusiveMinimum: 0, description: Optional caller timeout shared by the complete workflow; otherwise the host owns its runtime limit}
        authoring_timeout_seconds: {type: number, exclusiveMinimum: 0, description: Optional caller timeout for an authoring session within the shared workflow deadline}
        authoring_transport_retries: {type: integer, minimum: 0, maximum: 2, default: 0}
        output_dir: {type: string}
        scale: {type: number}
        approved: {type: boolean}
      additionalProperties: true
    output_schema:
      type: object
      required: [status]
      properties:
        status: {type: string, enum: [ok, partial, error]}
        outcome: {type: object}
        summary: {type: string}
        warning: {type: string}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        error_info: {type: object}
        workflow_issues: {type: object, description: unresolved stage failures keyed by planning or editing}
        research: {type: object}
        html_path: {type: string}
        html_uri: {type: string}
        html_sha256: {type: string}
        content_budget: {type: object}
        page_plan: {type: object}
        design_reference: {type: object}
        visual_design: {type: object}
        visual_review: {type: object}
        visual_review_path: {type: string}
        visual_review_mode: {type: string, enum: [vlm, not-run, deterministic-only]}
        visual_quality_state: {type: string, enum: [passed, revision-required, awaiting-review, not-reviewable]}
        screenshot_path: {type: string}
        pptx_path: {type: string}
        pptx_sha256: {type: string}
        pptx_visual_quality_state: {type: string, enum: [awaiting-review]}
        artifacts: {type: array}
        warnings: {type: array}
        error: {type: string}
      additionalProperties: true
    trigger:
      phrases: [conference poster, scientific poster, academic poster, research poster, HTML poster]
      when_to_use: "Complete poster: call draft with the paper source, page and optional assets. The skill owns paper reading, figure selection, whole-poster composition, visual revision and native PPTX export. Drawing specialists supply optional components when useful; continue poster revisions through this skill."
    notification:
      display_label: Scientific Poster
      title_field: summary
      preview_uri_field: preview_uri
  openclaw:
    emoji: "🪧"
    requires:
      bins: ["python3"]
---

# Scientific Poster

Build one designed scientific argument from a complete paper. HTML/CSS is the visual
source of truth. Models own content selection and visual composition; code prepares
evidence, binds assets, measures rendered output, validates delivery invariants, and
exports PowerPoint. Code must never choose a grid, palette, font pairing, module
placement, or reference-specific layout.

This file contains the operational workflow and delivery contract. Consult
[structured authoring](references/structured-authoring.md) for carrier, MathML, table,
or CSS guidance needed by the current edit; consult [poster quality](references/poster-quality.md)
when evaluating a rendered candidate. Read [contracts](references/contracts.md) only when
validation or bindings fail, and [preview and approval](references/preview-and-approval.md)
when revising, inspecting, approving, or exporting a candidate.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- **Copy-only mode:** copy this complete directory into the external agent's skill
  collection. The current host model plans content and authors the poster according
  to the contracts below.
- **Portable runner mode:** use `python3 scripts/run.py --json-file request.json`
  for the documented preparation, validation, inspection, review-receipt, and export
  actions. Run `python3 scripts/run.py --self-test` to check the portable entrypoint.
- **Omni enhanced mode:** the configured main role selects scientific content and
  composes it with the layout. Select the `main` or `vision` role for HTML editing
  with `author_model_role`; resumed drafts and revisions inherit the saved choice
  when omitted. The selected author and independent VLM review share durable
  candidate checkpoints and artifact registration.

Model calls use the supplied service's `max_output_tokens` allowance when exposed.
Other hosts retain their model service's default output policy. The skill sets no
fixed output-token ceiling.

The skill remains usable without a VLM. A configured image-capable model supplies
reference observations and visual QA; without one, `draft` and `revise` still deliver
the grounded, deterministically inspected artifact with visual state pending and no
claim of visual approval.

All hosts use the same artifacts and semantics:

1. ContentBudget: an initial evidence proposal with identity, candidate modules, values,
   equations, prepared figures, source labels, and a starting page orientation.
   Fixed-size drafts with prepared titles and authors start from a source carrier
   and compose directly. Evidence planning supplies automatic page estimates and
   missing text-source identity; explicit `estimate` retains its planning output.
   The complete paper remains the author's scientific authority.
2. ReferenceAnalysis: hash-bound, transferable spatial grammar plus observed hierarchy,
   reading path, typography, color and surfaces, boundaries and spacing, and figure
   treatment. It contains no fixed target topology.
3. Complete self-contained poster.html.
4. Chromium inspection plus, when visual review runs, a provenance-bound receipt.
5. When export succeeds, an editable one-slide poster.pptx bound to the exact inspected
   HTML. Export completion and visual review have separate states: a successful export
   reports `ok` / `pptx_export_complete`, with `pptx_visual_quality_state: awaiting-review`.
   HTML review remains scoped to that HTML. The overall result retains any unfinished
   workflow or HTML-review issues. Export failure leaves the HTML deliverable available
   and marks the package partial.

- **Omni:** the coordinating model selects specialist drawing skills from the host
  catalog when a component will improve the scientific explanation. Skill capability,
  availability and priority remain host-owned; native PowerPoint components use
  `figure.editable.pptx`. Pass the relationships, labels, aspect and editing needs to
  that producer. Existing `run_workflow` dependencies and explicit `assets` both carry
  components into the poster. Drawing results are candidates. Reject poor candidates
  and retain informative paper-original figures when they communicate more clearly.
  The poster author makes the final selection in the context of the whole page and
  can compose native HTML/SVG diagrams when needed.
  Call `action: draft` with the complete source and available components; it owns paper
  preparation, planning, HTML authoring, rendered review, revisions, and PPTX export
  with the configured main model and VLM. Pass the source (`paper_path`,
  `latex_project_dir`, `arxiv_id`, or `source`), the requested physical `page`, and
  explicit `assets` alongside the verbatim `input`. For multiple representations of
  the same paper, preparation selects local LaTeX first, local PDF second, and arXiv
  retrieval third. The selected source owns the scientific content; its preparation
  failure remains visible. Source selection and its path accompany the result. One `draft`
  call owns this workflow. Continue through `action: revise` with the returned
  `html_uri` as `source_html_uri` and the current change in `input` or `feedback`.
  The candidate checkpoint restores its source and saved design direction.
  Whole-page layout, visual revision and export
  stay with this skill; specialist outputs remain components. Deliver the returned
  artifact references and their actual review state. Portable actions support external
  hosts and explicit continuation.
- **Codex and Claude Code:** the current host model performs content planning and HTML
  authoring directly. Use scripts/run.py for portable prepare, estimate, validate, inspect,
  prepare-visual-review, submit-visual-review, and export-pptx actions. Do not invoke
  an additional hidden author model. Apply revisions with the host's exact file-edit
  tool; preserve the same local-edit semantics used by Omni. Do not write a passing
  review result for a poster you authored in the same context. When `OMNI_VLM_ENDPOINT`,
  `OMNI_VLM_MODEL`, and `OMNI_VLM_API_KEY` are configured, run
  `scripts/review_poster.py` on the bound request. Without an independent reviewer,
  keep the visual state pending while still delivering explicitly unapproved artifacts.
  Use prepared source figures exactly as emitted and inspect the reference as one complete
  image. Do not create alternate crops, thumbnails, atlases, or hand-built review payloads;
  `inspect` and `prepare-visual-review` own those derived review artifacts.

Portability means identical artifacts and contracts, not three copies of the
orchestrator. Host-specific metadata must remain under metadata.helixforge.

### Native drawing components

An existing single-slide PPTX can be supplied through `assets` in any host. The
component contains explicit slide objects with local resources. Layout placeholders,
inherited layout artwork, macros, external resources and animation require a
self-contained drawing export from the producer.
Native tables require explicit cell formatting; inherited PowerPoint table styles
are outside the component importer's supported format.

The producer supplies a full-slide SVG or PNG preview alongside the PPTX. Pass
`{"path": "/path/drawing.pptx", "preview_uri": "/path/drawing.svg"}` in `assets`;
both fields also accept artifact URIs. Declared workflow PPTX artifacts carry their
own `preview_uri`; a result-level `preview_uri` pairs with its single PPTX artifact.
Multiple PPTX artifacts each require their own explicit pairing. Component preparation
requires this preview and validates it through the image asset preparation path.
The portable HTML retains the preview and original native package. The author places its asset
token like a figure; PPTX export imports its native objects as an editable group,
with uniform physical scaling and preserved connection references. Preview images
are used for inspection only. Unavailable optional components are excluded with warnings; retained components keep their native import checks.

Inline SVG drawings export as solid-filled native paths with editable text labels.
Strokes become editable outlines. Use explicit arrowhead geometry and a single,
unrotated position for each label. Complex clipping, compositing and per-glyph
layout are outside this SVG adapter's supported format and produce export feedback.

### Figure choice and editing scope

Choose a visual for the relationship it explains at its final printed size. Compare
scientific fidelity, explanatory clarity, label density, fit with nearby evidence and
required editing scope. Preserve an informative paper-original figure when it serves
that job; reject an inaccurate, generic or unreadable generated candidate. Use a
redraw when it improves that specific explanation and the source supports its detail.
Skill capability and priority guide the coordinator's available producers; assess each
actual output on its merits.

Paper-original bitmap and mixed vector/bitmap figures may remain intact image assets.
Their placement and size are editable; their internal labels and marks retain image
semantics. Obtain original vector files from available TeX submissions and retain the
source package. PDF, SVG or editable DOT source alone establishes neither individually
editable PowerPoint objects nor verified readability. New diagrams, poster text and
mathematics use the native export paths. Report editing scope and verification for
each actual delivered asset, using that asset's own results. Rendered inspection
establishes appearance; native-software checks establish the tested editing behavior.

### Paper preparation and text inputs

`source` names a UTF-8 grounding file; `source_text` carries its contents inline.
`research` carries provenance identifiers. An explicit `research.text` can supply
a grounding brief when neither text input is supplied; identifiers alone describe
provenance. A supplied `source` file takes precedence over that brief.
For text sources, scientific planning extracts `paper_identity` with the complete
`title`, an `authors` array of names in source order, and an `affiliations` array. The shared identity boundary normalizes names into the existing author line and accepts legacy name strings. Affiliation markers and author notes are composed separately from the complete source. Callers may
supply verified `title`, `authors`, and `affiliations` directly for every source type;
those fields take precedence over extraction and planning output. Missing title or author metadata is reported in
the warnings; affiliations are included when supplied or found in the source.

For a deterministic PDF preparation check, the portable runner accepts:

```json
{
  "action": "prepare",
  "paper_path": "/absolute/path/paper.pdf",
  "output_dir": "/absolute/path/poster-materials"
}
```

This action extracts paper text and writes `poster-materials.json` with caption-bound
figure paths and SHA-256 identities. It runs with the extraction libraries and is
suitable for checking PDF ingestion before model-backed planning. Portable `estimate`
uses the prepared text, figure hashes, and a caller-supplied `content_budget`.

## Portable research provenance

This skill remains portable across OmniScientist, Claude Code, Codex, and OpenClaw.

- When provenance tools such as `cite_source`, `record_claim`, `add_evidence`, or
  `log_run` are available, use their returned identifiers in `research.source_ids`,
  `research.claim_ids`, `research.evidence_ids`, or `research.run_id`.
- When those tools are absent, retain human-readable paper identifiers, source paths,
  figure bindings, review receipts, and artifact paths in the delivered provenance.
- Never invent provenance identifiers or treat a visual reference as scientific evidence.

## Required workflow

1. Read the complete source. Use `paper_path` for a local PDF,
   `latex_project_dir` for a LaTeX project, or `arxiv_id` for an arXiv id or
   arxiv.org paper URL. A LaTeX project uses `main_tex` when more than one
   document root exists. `main_tex` accepts a path relative to the project or
   invocation cwd, or an absolute path. The invocation cwd is the host-provided
   `cwd` when supplied, otherwise the process working directory. Resolve both
   relative interpretations and require one existing `.tex` file inside the
   resolved project, including after symlink resolution. If they identify distinct
   files, supply an absolute path to select the root. Read its included source
   files, display equations, captions, and figures directly. arXiv preparation first retrieves its submitted
   TeX package and original figure files without executing submission code. When the
   source is unavailable or cannot be interpreted, prepare the corresponding PDF
   and report the reason. Local PDFs use page-aware extraction. Read the full paper.
2. Prepare the author's starting canvas. A fixed page with prepared title and authors
   uses a source carrier; the Author selects evidence while composing. Automatic
   sizing or missing text-source identity uses one compact ContentBudget proposing
   contribution, method, decisive evidence, scope and provenance. Retain an explicitly
   supplied ContentBudget. An available image-capable model observes original figures
   and supplied native components in bounded batches within the shared deadline.
   Available observations survive an interrupted batch. Their structure, label density,
   surfaces and visible defects accompany the candidates into joint composition.
3. Resolve one visual reference after source preparation: use a supplied reference_image,
   otherwise select a bundled, attributed academic poster with real body text and
   evidence matching the page orientation. References supply design observations; the source paper supplies all
   scientific content. Earlier synthetic boards remain available for saved-draft recovery.
4. Have an image-capable model inspect that exact reference once, without target
   content or role weights, and create a ReferenceAnalysis. Its transferable spatial
   grammar records the source's actual regions, approximate proportions, hierarchy,
   typography, surfaces and boundaries, including local exceptions and uncertainty.
   Distinguish readable body text from display lettering and placeholder marks.
   Record the proximity of headings, explanatory copy, evidence and captions.
   Source-image counts and positions are observations, not fixed slots for the target.
   The plan contains no target layout, CSS, HTML, or copied scientific content.
   If reference interpretation remains unavailable after its bounded transport retry,
   continue from the paper and user direction without invented reference observations.
   Rendered visual review determines the final visual state.
5. Give the selected main or vision Author the complete source, initial ContentBudget
   and starter HTML, ReferenceAnalysis, physical page, verified identity, prepared assets
   and full user direction. The Author composes wording and HTML/CSS together:
   select, combine or split modules, write diagram labels for their visual roles,
   and organize explanations around the method and decisive comparison.
   Check retained claims against the source while composing, including values,
   conditions, units, baselines and uncertainty. Preserve verified identity,
   intact source images and native mathematics.
   First submit module interiors, layout and the complete stylesheet together.
   Whole-module references retain existing content; image and formula references
   restore exact source objects inside newly authored modules. Subsequent edit
   batches can change copy and composition together. The actual HTML supplies
   the current module index and revision state.
6. Render in Chromium. Treat overflow, clipping, missing assets, duplicate bindings,
   off-page content, and unreadable physical scale as measurements for the next model
   revision. These checks protect delivery; they do not prescribe a layout.
   Return blocking diagnostics to the selected Author in the same editing session. The
   first changed candidate that clears deterministic blockers advances automatically to
   step 7; advisory warnings may accompany it. If the bounded session ends first, deliver
   the latest statically valid authored draft and attempt editable export with its actual
   failed-check details, but do not spend a VLM call or claim visual approval.
7. Review the exact rendered screenshot as the complete operational visual state, using
   physical measurements, a module index, and the academic-poster rubric. Do not give
   the visual reviewer a plain-text transcript as a substitute for reading the image.
   Evaluate the main claim, method and explanation for a visitor standing about one metre
   from the printed page. Assess the quick-scan argument separately from closer-reading detail:
   legible type and valid geometry can still leave excessive reading effort. During drafting,
   every review diagnosis returns to the same source-aware author for joint editing.
   The author
   owns source fidelity using the complete paper; visual review does not reread that paper
   or certify scientific truth. The reference pixels and
   ReferenceAnalysis were interpreted once during initial authoring. The selected reference
   image also accompanies the candidate in the same review call to compare communication
   quality. Built-in references guide visual explanation, hierarchy and evidence scale;
   the bound design requirements continue to govern canvas, palette and framing.
   Retain the original user direction across revision checkpoints and pass it to the author
   with the current edit request; a current explicit change takes precedence while unchanged
   requirements remain in effect. Keep the already interpreted ReferenceAnalysis in the
   author's context for every revision, not only restyles; these observations preserve
   design intent without restarting reference analysis or freezing a target layout.
   Preserve strengths and solution prose in the receipt
   for human audit. Feed the concise visible-defect summary, issue criterion, target,
   operation, host-bound observations and current HTML back to
   the selected Author for the next edit. Drafting retains editorial authority across
   restyle, reflow and content diagnoses. The summary diagnoses the current screenshot but does not choose a repair.
   Give the
   model continuous edit batches: each batch may be exact local replacements, movement
   of existing DOM elements, a complete stylesheet, or an atomic layout replacement,
   whichever is more coherent. Omni's `move_poster_elements` tool relocates unique XPath-selected
   nodes to model-selected parents without retyping their content; it chooses no layout.
   Parser-backed edits return the resulting serialized HTML for subsequent exact edits.
   A stylesheet
   replacement preserves the DOM and existing inline attributes; the model supplies every
   CSS rule, including the physical page declaration. Omni applies each batch and
   automatically returns contract and Chromium feedback against the new exact state.
   The outer workflow alone calls the VLM after an editing session finishes, then starts
   another source-aware edit if required. Initial drafting retains editorial authority to
   select and explain source evidence; `full-layout` revisions freeze existing scientific
   content. Candidate review and revision continue while the shared deadline permits.
   Pairwise review promotes only an improved proposal; an unchanged or regressed proposal
   leaves the incumbent selected and returns different-strategy feedback for another bounded
   edit. Codex and Claude Code use their native read/edit/check
   loop with the same semantics. Preserve a few
   native assistant tool calls and their paired tool results when compressing context.
   Keep a checkpoint from before the retained exchanges, so applying those edits reconstructs
   the current HTML. Do not flatten completed operations into new user instructions or
   unpaired prose excerpts; the latest tool result describes the current checked state.
   Browser selector-match observations help identify CSS edits that missed their intended
   DOM targets; unused selectors are not validation failures. Preserve the best checked
   version and re-render each changed composition. Do not execute the same command
   again on unchanged HTML; return its current diagnostic state so the model can
   choose a different edit within the existing turn and time budget. Each editing session starts from
   the selected HTML, its bound source state and the current review feedback. A statically valid but temporarily Chromium-invalid proposal may remain the
   diagnostic working draft when it reduces a blocker or changes the governing layout relationship, while a public revision preserves the existing published version. If initial
   drafting stops, deliver the latest model-authored HTML that passed static validation
   with its preview and explicit failed-check details. If no model-authored composition exists,
   deliver the grounded carrier as an explicitly partial fallback. Unparseable, truncated, or no-visible-effect responses do not replace
   the working state.
8. Attempt PPTX export from the delivered, statically validated HTML even when
   layout or visual checks still require revision. Return it as an unapproved draft
   with those defects. Preserve the HTML review; report PPTX visual quality
   independently as awaiting native-software review. If export
   fails, retain the HTML and any available preview as deliverables. Preserve native editable text, shapes, tables,
   equations, and source images rather than flattening the poster into one screenshot.

## Scientific content standard

- A reader should understand the contribution, method, evidence, and limitation
  without reading the paper.
- Visible copy explains the research and its sources. Poster construction and export
  details belong in delivery metadata.
- Prefer roughly 450–700 English words on A0 landscape, less when dense figures,
  tables, or algorithms carry the explanation. This is authoring guidance, never a
  string-count gate.
- Preserve qualifications and causal strength. Do not turn association, proxy
  evidence, or a limited evaluation into a stronger claim.
- Use only verified title, authors, affiliations, figures, equations, values,
  citations, and provenance. Reference pixels are never scientific evidence.
- Do not infer venue branding from paper metadata or reference pixels. Use it only
  when the user explicitly requests it or supplies verified identity.

## Visual standard

The result must read as one designed argument, not a grid of notes.

- Establish a visible entry point, deliberate reading path, and one or two dominant
  scientific carriers.
- Default to a white (`#FFFFFF`) canvas with white or transparent content regions.
  Color serves intact scientific figures, meaningful data encoding and restrained
  heading emphasis. Explicit user direction and custom references override these
  defaults within their requested scope; an accent color applies to emphasis.
- Choose boundaries locally by reading relationship. Open regions work when proximity,
  alignment and headings make membership clear. Light dividers can separate adjacent
  reader questions; restrained complete outlines can gather related evidence and its
  interpretation when neighboring material makes membership ambiguous. A poster may
  combine these treatments. Keep each visual group intact and use the lightest boundary
  that makes its relationship clear. Assess merged unrelated groups, fragmented related
  groups and frames that compete with the evidence.
- Built-in references guide reading hierarchy, proximity and evidence scale.
  Their canvas tint, palette and decorative framing remain source observations.
- Transfer observed relationships at the fidelity the user requests, with each
  typographic treatment assigned to its observed role. Body text uses a comfortable
  regular weight; strong weight and display styling mark brief headings and emphasis.
- Keep a heading, its evidence and its interpretation in one compact visual group.
  Let rendered copy and intact figure proportions determine intrinsic content depth.
  Keep table and comparison rows at their natural cell height with consistent padding;
  wrapped labels expand their own rows. Use explicit local gaps within groups and
  larger intervals between separate reader questions. Place groups by their actual
  depths on the fixed sheet, giving readable evidence the continuous area it needs.
  Assess distances between text, figures and row contents inside panels as well as
  between them.
- Choose a purposeful local display/body/utility type relationship with explicit
  offline fallbacks. Do not reuse one generic font profile for every poster.
- Use borders and rules for information-bearing structures such as method nodes,
  connections, tables and axes. Diagram grouping and semantic fills explain scientific
  relationships. White surfaces and local outlines can establish groups independently
  of colored backgrounds.
  Decorative single-side borders, accent rails, heading stripes, tinted section
  cards and page-wide color washes require explicit user or custom-reference
  authority. Coordinate boundaries with typography, alignment and whitespace.
- Give plots and tables enough physical width for their internal labels. A present but
  unreadable scientific figure is a failed carrier.
- Negative space must support hierarchy. A large accidental lower void beside dense
  content is not intentional whitespace.
- Different papers and references must not collapse to one blue three-column template.

## HTML and delivery contract

- Return one complete inert HTML5 document with inline print-safe CSS. No scripts,
  handlers, animation, remote resources, remote fonts, forms, or placeholders.
- Use one body-level root with data-poster-root and data-poster-id="poster".
  Declare the same physical millimetre dimensions in @page and on the root.
- Include exactly one data-poster-title-band.
- Give every independently placeable scientific region a unique kebab-case
  data-poster-module and non-empty data-source-label. These are provenance and
  selection anchors. A source label may combine human-readable section, figure,
  table or equation references; check their support in the complete paper. Keep
  these locators in metadata and the separate provenance record. Visible headings,
  explanations and captions serve the research argument. Attribution on the poster
  uses conventional bibliographic references, acknowledgments or required credits.
- Keep prepared image tokens and data-source-figure-sha256 bindings exact. Never crop,
  recolor, redraw, split, or regrid a source figure merely to imitate a reference.
- Use semantic MathML for source-grounded mathematical expressions. An equation must never
  be an image, SVG, canvas, screenshot, or PDF crop. Write inline mathematics with explicit
  LaTeX delimiters and display equations with LaTeX plus a source label; never leave caret
  notation, commands, or ASCII subscripts visible.
- A prepared equation inventory supplies optional `source_ref` transcription shortcuts.
  Its absence does not disable mathematical authoring. Normalization resolves a selected
  reference when present and otherwise validates the model's source-grounded LaTeX.
- Native file-editing hosts render the selected LaTeX through the shipped converter
  described in [structured authoring](references/structured-authoring.md#native-equation-fragments).
  Insert its MathML unchanged; the model controls the surrounding typography and layout.
- Keep every visible element inside the physical page without clipping, overlap,
  hidden overflow, filler, duplicated copy, or tiny type.
- Any change to HTML invalidates the previous screenshot, review, approval, and PPTX.
