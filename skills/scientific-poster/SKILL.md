---
name: scientific-poster
description: >-
  Create or revise a publication-quality academic poster from a complete paper.
  Preserve the full user request. The main model plans grounded content; the selected
  main or vision author model creates complete HTML/CSS. An image-capable model converts one exact reference into a
  ReferenceAnalysis and reviews rendered drafts. Use paper_path for a PDF,
  latex_project_dir for a LaTeX project, arxiv_id for an arXiv paper,
  reference_image for a user reference, and conference only for an explicit venue.
license: "Apache-2.0 for original code and documentation only; third-party visual assets are excluded (see NOTICE.md)"
metadata:
  helixforge:
    version: "1.0"
    dependencies: ["python>=3.11", "pymupdf>=1.28", "pymupdf4llm", "lxml>=5,<7", "Pillow>=10", "playwright>=1.49,<2", "chromium", "tinycss2>=1.4,<2", "latex2mathml>=3.81,<4", "pyparsing>=3.2,<4", "markdown-it-py>=4,<5", "mdit-py-plugins>=0.6,<0.7", "python-pptx>=1.0.2", "mathml2omml==0.0.2"]
    runtime_requirements:
      python_modules: [tinycss2, pymupdf, pymupdf4llm, lxml, PIL, playwright]
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
            Use draft once for a complete Omni poster request; it owns preparation,
            planning, authoring, visual review, revision, and PPTX delivery. Other
            actions are explicit continuation or portable single-stage operations.
          enum: [prepare, estimate, draft, revise, validate, preview, inspect, prepare-visual-review, submit-visual-review, export-pptx, approve]
        input:
          type: string
          description: complete user request preserved verbatim
          x-omni: {semantic_role: instruction}
        paper_path: {type: string, description: local paper PDF path or artifact URI}
        latex_project_dir: {type: string, description: local LaTeX project directory}
        main_tex: {type: string, description: document root within latex_project_dir when ambiguous}
        arxiv_id: {type: string, description: arXiv id or arxiv.org abstract/PDF URL}
        source: {type: string, description: local or artifact URI of a UTF-8 grounding file}
        source_text: {type: string, description: grounded scientific source when no file is supplied}
        instructions: {type: string}
        research: {type: object}
        reference_image: {type: string, description: optional exact custom visual reference}
        conference: {type: string, description: one explicit conference name and optional year}
        orientation: {type: string, enum: [auto, portrait, landscape]}
        page: {type: object}
        assets: {type: array}
        visual_preferences: {type: object}
        content_budget: {type: object}
        source_figure_sha256s: {type: array}
        source_html_uri: {type: string}
        source_html_sha256: {type: string}
        visual_review_path: {type: string}
        visual_review_request: {}
        visual_review_result: {}
        feedback: {type: string}
        revision_mode: {type: string, enum: [full-layout, content-replan]}
        content_replan_targets: {type: array}
        author_model_role:
          type: string
          enum: [main, vision]
          default: main
          description: Select the configured main or vision model for HTML authoring and revision.
        max_repair_attempts: {type: integer, minimum: 0, maximum: 10}
        workflow_timeout_seconds: {type: number, exclusiveMinimum: 0, maximum: 3600, default: 3590}
        authoring_timeout_seconds: {type: number, exclusiveMinimum: 0, maximum: 900, default: 900}
        authoring_transport_retries: {type: integer, minimum: 0, maximum: 2, default: 0}
        output_dir: {type: string}
        scale: {type: number}
        approved: {type: boolean}
        operator_confirmation: {type: string}
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
        artifacts: {type: array}
        warnings: {type: array}
        error: {type: string}
      additionalProperties: true
    trigger:
      phrases: [conference poster, scientific poster, academic poster, research poster, HTML poster]
      when_to_use: Use for a complete poster from grounded research, including visual review and editable PPTX export.
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
when inspecting, approving, or exporting a candidate.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- **Copy-only mode:** copy this complete directory into the external agent's skill
  collection. The current host model plans content and authors the poster according
  to the contracts below.
- **Portable runner mode:** use `python3 scripts/run.py --json-file request.json`
  for the documented preparation, validation, inspection, review-receipt, and export
  actions. Run `python3 scripts/run.py --self-test` to check the portable entrypoint.
- **Omni enhanced mode:** the configured main role plans scientific content. Select
  the `main` or `vision` role for HTML editing with `author_model_role`; the selected
  author and independent VLM review share durable candidate checkpoints and artifact
  registration.

The skill remains usable without a VLM. A configured image-capable model supplies
reference observations and visual QA; without one, `draft` and `revise` still deliver
the grounded, deterministically inspected artifact with visual state pending and no
claim of visual approval.

All hosts use the same artifacts and semantics:

1. ContentBudget: grounded title, identity, visible modules, exact copy, numbers,
   equations, selected figure bindings, priorities, source labels, provenance, and
   model-selected page orientation when the user leaves orientation auto.
2. ReferenceAnalysis: hash-bound, transferable spatial grammar plus observed hierarchy,
   reading path, typography, color and surfaces, boundaries and spacing, and figure
   treatment. It contains no fixed target topology.
3. Complete self-contained poster.html.
4. Chromium inspection plus, when visual review runs, a provenance-bound receipt.
5. When export succeeds, an editable one-slide poster.pptx bound to the exact inspected
   HTML, retaining its current review and approval status. Export failure leaves the HTML
   deliverable available and marks the package partial.

- **Omni:** metadata.helixforge invokes ScientificPosterEngine. For a request to
  create a complete poster, call `action: draft` exactly once; that action owns paper
  preparation, planning, HTML authoring, rendered review, revisions, and PPTX export
  with the configured main model and VLM. Do not decompose that request into portable
  `prepare`, `estimate`, or delivery calls.
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
   document root exists; read its included source files, display equations,
   captions, and figures directly. PDF and arXiv inputs use page-aware PDF
   extraction. Never treat the abstract alone as sufficient.
2. Ask the main model to create one compact, structured ContentBudget. Select the
   contribution, necessary method, decisive evidence, an honest limitation or
   boundary, provenance, and an orientation suited to that evidence when none was
   explicitly requested. Prefer one strong explanatory path over a paper-section
   inventory. Do not repeat claims merely to fill space. When an image-capable model
   is available, inspect the selected source figures once and let it describe only their
   panel structure, embedded-text density, legibility pressure, and whether each intact
   figure needs a normal column, a wide region, or dominant page width; it must not rank
   scientific importance or choose content. Give those observations to the same main-model
   page audit and initial author. The audit may retain or omit a draft-selected figure but
   must not introduce a new one. A dense essential figure earns more continuous area by
   displacing secondary material; density alone is not a rejection rule.
   For automatic sizing, select a provisional maximum page from the draft budget, then
   let the same main model review that budget against the complete source and page
   capacity. Correct mistaken
   interpretations, preserve material qualifiers, combine repeated reader questions,
   and select the strongest readable carriers. Return one revised ContentBudget using
   the same source and asset contract. Let the reduced budget choose the final page
   without growing beyond the provisional pressure. Structural validity does not certify a claim;
   this is source-backed editorial judgment, not layout compilation.
3. Resolve exactly one content-free visual reference after content planning:
   use a supplied reference_image, otherwise generate one when configured, otherwise
   select a bundled seed from content shape rather than paper topic or seed identity.
4. Have an image-capable model inspect that exact reference once, without target
   content or role weights, and create a ReferenceAnalysis. Its transferable spatial
   grammar records the source's actual regions, approximate proportions, hierarchy,
   typography, surfaces and boundaries, including local exceptions and uncertainty.
   Source-image counts and positions are observations, not fixed slots for the target.
   The plan contains no target layout, CSS, HTML, or copied scientific content.
   If reference interpretation remains unavailable after its bounded transport retry,
   continue without pixel-derived reference observations and keep the delivered poster
   explicitly unapproved; never fabricate a ReferenceAnalysis or a visual pass.
5. Give the selected `main` or `vision` Author the source-grounded, page-audited ContentBudget and neutral content carrier,
   ReferenceAnalysis, physical page, verified identity, prepared assets, selected-figure
   observations, and complete user direction. The selected Author
   composes the entire HTML/CSS poster at the user's requested reference fidelity:
   reproduce observed design relationships when replication is requested; adapt them
   to the new content when adaptation is requested. The final main-model page audit is the
   scientific QA boundary: its modules, copy, figures, tables, equations, roles, priorities,
   source labels, and qualifiers remain exact during initial HTML authoring. The Author owns
   wrappers, grouping, order, spans, typography, surfaces, and figure size. Scientific changes
   return to the main scientific editor through automatic render-driven replanning, an
   explicit edit, or reviewer-targeted `content-replan`.
   Verified identity, prepared assets and selected mathematical content remain protected. A neutral
   content carrier is a grounded starting layout; it still requires Chromium inspection and
   independent visual review before it can pass as a finished poster.
   First submit a complete layout shell and stylesheet. The host reinserts the exact title
   band and selected scientific modules; use local edits after that composition exists.
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
   from the printed page; being readable after on-screen magnification is insufficient. The author
   owns source fidelity using the complete paper; visual review does not reread that paper
   or certify scientific truth. The reference pixels and
   ReferenceAnalysis were interpreted once during initial authoring. A custom reference
   image also accompanies the candidate in the same review call to compare communication
   quality; it neither supplies scientific facts nor imposes an identical layout.
   Retain the original user direction across revision checkpoints and pass it to the author
   with the current edit request; a current explicit change takes precedence while unchanged
   requirements remain in effect. Keep the already interpreted ReferenceAnalysis in the
   author's context for every revision, not only restyles; these observations preserve
   design intent without restarting reference analysis or freezing a target layout.
   Preserve strengths and solution prose in the receipt
   for human audit. Feed the concise visible-defect summary, issue criterion, target,
   operation, host-bound observations, current HTML, and frozen scientific bindings back to
   the selected Author for restyle or reflow. Route `content-replan` to the main scientific
   editor. The summary diagnoses the current screenshot but does not choose a repair.
   Give the
   model continuous edit batches: each batch may be exact local replacements, movement
   of existing DOM elements, a complete stylesheet, or an atomic layout-shell replacement,
   whichever is more coherent. Omni's `move_poster_elements` tool relocates unique XPath-selected
   nodes to model-selected parents without retyping their content; it chooses no layout.
   Parser-backed edits return the resulting serialized HTML for subsequent exact edits.
   A stylesheet
   replacement preserves the DOM and existing inline attributes; the model supplies every
   CSS rule, including the physical page declaration. Omni applies each batch and
   automatically returns contract and Chromium feedback against the new exact state.
   The outer workflow alone calls the VLM after an editing session finishes, then starts
   another source-bound edit if required. Initial drafting retains editorial authority to
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
   with those defects; export does not change review or approval status. If export
   fails, retain the HTML and any available preview as deliverables. Preserve native editable text, shapes, tables,
   equations, and source images rather than flattening the poster into one screenshot.

## Scientific content standard

- A reader should understand the contribution, method, evidence, and limitation
  without reading the paper.
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
- Transfer the reference's hierarchy, rhythm, type relationships, boundary language,
  color relationships, and restrained motifs at the fidelity the user requests.
  Adapt grouping and positions when making a new composition for different content.
- Choose a purposeful local display/body/utility type relationship with explicit
  offline fallbacks. Do not reuse one generic font profile for every poster.
- Use boundaries selectively: open fields, rules, bands, shared surfaces, partial
  outlines, soft or square panels, and whitespace are all available. Do not place
  every module in the same thin rectangular card.
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
  selection anchors, not mandatory visible cards.
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
