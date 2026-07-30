---
name: scientific-poster
description: "One-call poster. Always copy the complete user request verbatim into input; do not decompose design, page, venue, or content direction into invented fields. For revise, emit action=revise with source_html_uri and visual_review_path and omit revision_mode when a receipt is supplied. Use paper_path for PDF and optional conference for one explicit venue. Never emit paper_source, style, venue_hint, existing_html, or visual_review_receipt."
license: "Apache-2.0 for original code and documentation only; third-party visual assets are excluded (see NOTICE.md)"
metadata:
  helixforge:
    version: "1.0"
    dependencies: ["python>=3.11", "playwright + chromium (preview/export)", "python-pptx (editable PPTX export)"]
    allowed_tools: [write_file, bash, read_file, cite_source, record_claim, add_evidence, log_run]
    research_contract: portable_provenance_v1
    kind: python_engine
    tier: research
    role: task
    status: experimental
    priority: 90
    delivery_mode: async_task
    execution:
      max_seconds: 600
    engine:
      module: engine
      class: ScientificPosterEngine
      method: execute
    capabilities:
      - poster.scientific
    deliverables:
      - artifact.poster
    default_for:
      - scientific poster
      - academic poster
      - conference poster
      - HTML poster
    workflow:
      failure_policy: continue_with_partial
      allow_failed_dependencies: true
    input_schema:
      type: object
      required: [input]
      properties:
        action:
          type: string
          enum: [estimate, draft, revise, validate, preview, inspect, prepare-visual-review, submit-visual-review, export-pptx, approve]
        input:
          type: string
          description: exact complete user request, preserved verbatim as the authoring authority
          x-omni:
            semantic_role: instruction
        paper_path: {type: string, description: local PDF path or artifact URI; the Skill extracts text and caption-grounded figures before authoring}
        instructions: {type: string, description: optional authoring preferences when source_text is supplied separately}
        conference: {type: string, description: one explicit conference name and optional year; supported venues bind a vetted local logo}
        research: {type: object}
        source: {type: string, description: local or artifact URI of a UTF-8 scientific grounding file}
        source_text: {type: string}
        source_figure_sha256s: {type: array, description: machine-owned prepared PDF figure hashes for approval}
        source_html_uri: {type: string, description: exact existing poster HTML path or artifact URI; use this field for revise, never existing_html}
        source_html_sha256: {type: string}
        html: {type: string}
        feedback: {type: string}
        revision_mode: {type: string, enum: [style-only, full-layout, content-replan], description: optional manual revision mode; omit it when visual_review_path is supplied because the receipt derives mode and targets; never send a boolean or the string true}
        content_replan_targets: {type: array, minItems: 1, uniqueItems: true, items: {type: string}, description: exact grounded module ids whose explanatory copy the user authorizes the main model to curate or compress}
        selection_state: {type: object}
        page: {type: object}
        orientation: {type: string, description: "page orientation; canonical values are auto, portrait, and landscape; horizontal normalizes to landscape and vertical normalizes to portrait"}
        content_budget: {type: object, description: prevalidated grounded evidence budget for deterministic portable estimate or controlled draft}
        visual_preferences:
          type: object
          description: optional user-owned typography, section framing, and accent direction; these guide design but never scientific content
          properties:
            typography: {type: string}
            framing: {type: string}
            accent_color: {type: string, pattern: "^#[0-9a-fA-F]{6}$"}
        venue_identity:
          type: object
          description: explicit conference identity overrides exact single-venue request resolution; distinctions are never inferred
          required: [label, evidence_uri]
          properties:
            venue_id: {type: string, enum: [icml, neurips, iclr, cvpr], description: optional supported venue id used to bind the vetted local logo}
            label: {type: string}
            evidence_uri: {type: string}
            distinction: {type: string}
            logo_asset_sha256: {type: string, pattern: "^[0-9a-f]{64}$"}
        authoring_timeout_seconds: {type: number, exclusiveMinimum: 0, maximum: 900}
        authoring_transport_retries: {type: integer, minimum: 0, maximum: 2}
        assets: {type: array, description: optional local, artifact, data-URI, or harness-prepared visual assets; remote URLs are not fetched by the portable core}
        scale: {type: number}
        source_html_path: {type: string}
        screenshot: {type: string, description: local full-poster screenshot for a bound visual-review request}
        reference: {type: object, description: exact serialized design_reference returned by draft; required by prepare-visual-review}
        content_brief: {type: object, description: grounded poster content structure supplied to the visual reviewer}
        visual_evidence: {type: object, description: exact Chromium visual-evidence bundle supplied to prepare-visual-review}
        iteration: {type: integer, minimum: 0, maximum: 2}
        visual_review_request: {description: visual-review request object or local JSON path}
        visual_review_result: {description: image-capable reviewer result object or local JSON path; revise issues declare restyle, reflow, or content-replan operations for the complete authoring model}
        visual_review_path: {type: string, description: exact visual-review receipt used for revision or approval; use this field for revise, never visual_review_receipt}
        approved: {type: boolean}
        operator_confirmation: {type: string}
        session_id: {type: string}
        host_event_id: {type: string}
        output_dir: {type: string}
      additionalProperties: false
    output_schema:
      type: object
      required: [status]
      properties:
        status: {type: string, enum: [ok, partial, error]}
        outcome:
          type: object
          required: [code]
          properties:
            code:
              type: string
              enum:
                - invalid_action
                - invalid_json
                - invalid_payload
                - runner_failed
                - missing_input
                - missing_html
                - source_too_large
                - llm_unavailable
                - llm_error
                - host_agent_required
                - estimate_complete
                - invalid_content_budget
                - invalid_page
                - candidate_validation_failed
                - source_not_found
                - source_read_failed
                - source_html_invalid
                - stale_selection
                - invalid_selection
                - live_preview_update_failed
                - preview_ready
                - poster_filename_required
                - poster_valid
                - poster_invalid
                - pptx_export_complete
                - pptx_export_failed
                - inspection_complete
                - inspection_unavailable
                - invalid_inspection_options
                - html_not_found
                - inspection_output_failed
                - chromium_inspection_failed
                - dom_evaluation_failed
                - screenshot_failed
                - inspection_blocked
                - inspection_failed
                - visual_review_unavailable
                - visual_revision_required
                - visual_review_passed
                - visual_review_failed
                - visual_review_invalid
                - missing_capability
                - capabilities_ready
                - capability_probe_failed
                - approval_required
                - poster_approval_recorded
                - approval_receipt_untrusted
                - approval_source_mismatch
          additionalProperties: false
        summary: {type: string}
        requires_approval: {type: boolean}
        html_path: {type: string}
        html_uri: {type: string}
        html_sha256: {type: string}
        grounding_source_sha256: {type: string}
        source_figure_manifest_sha256: {type: string}
        live_html_path: {type: string}
        preview_uri: {type: string}
        preview_argv: {type: array}
        selection_state_path: {type: string}
        approval_path: {type: string}
        approved_html_path: {type: string}
        visual_review_request_path: {type: string}
        visual_review_request: {type: object}
        visual_review_path: {type: string}
        visual_review_uri: {type: string}
        visual_review_sha256: {type: string}
        visual_review: {type: object}
        visual_evidence_path: {type: string}
        visual_evidence_sha256: {type: string}
        decision_request: {type: object, description: non-blocking hash-bound user choice returned only after automatic visual repair cannot safely continue}
        visual_quality_state: {type: string}
        visual_review_mode: {type: string, enum: [not-run, pending, vlm, deterministic-only]}
        reference_source_kind: {type: string, enum: [generated, seed]}
        revision_feedback: {type: string}
        pptx_path: {type: string}
        pptx_uri: {type: string}
        scene_path: {type: string}
        scene_uri: {type: string}
        rubric_path: {type: string}
        rubric_uri: {type: string}
        pptx_export: {type: object}
        rubric: {type: object}
        openxml: {type: object}
        editable_object_count: {type: integer}
        artifacts: {type: array}
        warnings: {type: array}
        paper_source: {type: object}
        content_budget: {type: object}
        page_plan: {type: object}
        design_reference: {type: object, description: hash-bound non-authoritative generated reference or built-in seed selected before authoring}
        warning: {type: string}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        error: {type: string}
        error_info: {type: object}
        research: {type: object}
      additionalProperties: true
    trigger:
      phrases: [conference poster, scientific poster, academic poster, research poster, HTML poster, poster preview]
      when_to_use: Use for a complete poster - HTML preview, visual feedback, and an editable PPTX all close in a single call; do not split this into several poster steps.
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

Author a complete HTML/CSS scientific poster, compare its rendered screenshot with a non-authoritative visual reference, export an editable one-slide PowerPoint, and snapshot the exact approved bytes. The model designs the page directly; JSON is machine state for evidence, review bindings, selections, and approval receipts, never the poster authoring language.

## Normal Omni path

Ask Omni in natural language to use the `scientific-poster` skill; do not rely on shell-style skill sigils. Omni normalizes `paper_path`, `source_text`, `research`, or a UTF-8 grounding file in `source`, plus the user's poster direction, into one `draft` call. It then prepares the source, plans evidence and page size, resolves a reference, authors HTML, renders and inspects it, runs any configured visual loop, activates live preview, and exports the eligible editable PPTX. Do not split that normal flow into manual actions.

## Required workflow

1. Read [HTML authoring](references/structured-authoring.md), [poster quality](references/poster-quality.md), [contracts](references/contracts.md), and [preview/approval](references/preview-and-approval.md).
2. Use the complete source, not only an abstract. For a local paper, pass `paper_path` so page-aware text and caption-grounded figures are prepared before authoring. Build a compact evidence map around the contribution, necessary method, decisive evidence, qualifications, and provenance; never invent or repeat content to fill space. `estimate` exposes this stage, while `draft` runs it automatically.
3. Let the estimator choose a full common academic format when the user did not specify one; prefer standard A0 landscape for a dense conference poster, while retaining a bounded common-proportion height range for later screenshot-driven revision. Never begin from an unusually wide custom canvas merely to make the first layout easier. Estimated pressure on an automatically selected page is advisory because text flow and figure packing are not known until rendering; let Chromium geometry and the visual-review loop decide whether reflow, bounded height reduction, or grounded content replanning is actually needed. A user-specified physical page remains a hard capacity contract. Treat column count, grouping, spans, hierarchy, typography, density, and section treatment as design choices informed by actual content geometry. Three columns are common for landscape conference posters, not a contract.
4. Resolve one non-authoritative visual reference before authoring. Complete `OMNI_IMAGE_GEN_*` independently produces a content-free generated reference. Missing, incomplete, invalid, failed, or budget-ineligible image generation selects the deterministic conference-poster seed for the same content signals. VLM availability never changes which reference is resolved. Reference content is never scientific evidence.
5. Author one complete inert HTML document with inline CSS, physical millimetres, embedded figures, a compact title band, and stable evidence/source bindings. Render the verified author list once as one identity block; wrapping is a visual choice. Venue identity uses only explicit or unambiguous supplied evidence. Bindings support grounding, selection, and export but never prescribe cards, rails, panels, module count, or topology.
6. Run static validation and Chromium inspection. Hard gates protect grounding, identity, byte provenance, offline safety, and delivery integrity: the page must be measurable, required evidence must render, content boxes must not overflow, and visible poster elements must stay inside the physical page. Type size, figure scale, occupancy, density, whitespace, hierarchy, section distinction, and non-lossy clipping diagnostics remain visual-review evidence rather than aesthetic vetoes.
7. With a usable host-injected `ctx.vlm`, or complete `OMNI_VLM_*` fallback configuration when that host service is absent or unavailable, inspect the exact resolved reference pixels before authoring, whether generated or seeded; without a VLM, a seed uses its structured fallback grammar and a generated reference uses content-adaptive fallback guidance. After rendering, bind the full-resolution screenshot to Chromium text, figure, typography, physical-page extent, out-of-page modules, module-internal spacing, inter-module visual-lane gaps, lane-to-page trailing space, and page-bottom observations plus a small high-resolution evidence atlas. These are descriptive measurements, not equal-height rules or numeric aesthetic gates; physical out-of-page evidence identifies content absent from the delivered canvas. Require the VLM to assess every selected observation. When placement alone cannot produce readable fit, the VLM may direct the main model to curate or compress only named grounded modules while retaining their central takeaways and source-bound facts. Review staged revisions before commit, keeping independent composition and physical-delivery incumbents so a zero-overflow but visually regressed candidate cannot replace a stronger composition. Permit at most two staged revisions inside the shared workflow deadline. A valid VLM revise/fail verdict leaves the candidate unaccepted and blocks automatic PPTX export. No configured VLM yields `visual_review_mode: deterministic-only`; a configured but unavailable or timed-out VLM remains `visual_review_mode: vlm` with `awaiting-review` and blocks automatic export. Use the provider-neutral review actions when another image-capable harness supplies feedback.
8. Keep HTML as the authoring source of truth. `preview_argv` serves the active HTML with an ephemeral selection overlay and does not alter its bytes. A deterministic-only candidate may still return an editable PPTX when Chromium inspection passes; a candidate rejected by a configured VLM does not. PowerPoint export maps native text, shapes, tables, equations, and scientific images from the exact HTML rather than placing a full-poster screenshot.
9. Do not wait for user input inside a background execution. If configured visual review or bounded automatic repair cannot safely continue, return a non-blocking `decision_request` bound to the exact HTML. A harness may show its preview and options, then issue a normal `revise` call with the returned continuation fields plus the user's feedback. Absence of a VLM alone is not a human-decision checkpoint.
9. After a passing visual receipt, request the exact returned `operator_confirmation`. Approval reruns grounding and Chromium checks and snapshots `poster.html`, `visual-review.json`, and `approval.json`. Final visual receipt, approval, HTML, and PPTX must bind the same HTML bytes; any HTML change requires another review.

## Visual reference and review configuration

Reference generation and visual review are separate. Reference generation reads only its process-environment configuration. Visual review first uses a usable host-injected `ctx.vlm`; only when that service is absent or unavailable does it read the environment fallback:

```bash
export OMNI_IMAGE_GEN_MODEL='<image-generation-model>'
export OMNI_IMAGE_GEN_ENDPOINT='https://gateway.example/v1/images/generations'
export OMNI_IMAGE_GEN_API_KEY='<image-generation-key>'

export OMNI_VLM_MODEL='<vision-capable-model>'
export OMNI_VLM_ENDPOINT='https://gateway.example/v1/chat/completions'
export OMNI_VLM_API_KEY='<vlm-key>'
```

Each environment endpoint is a complete HTTPS POST URL implementing the Omni-normalized OpenAI-compatible contract and uses bearer authentication. The CLI or gateway owns provider adaptation; the Skill never guesses a provider from a URL. Each environment group is enabled only when its own three variables are complete, neither borrows credentials or models from the other, and the Skill does not auto-load `.env` files.

`OMNI_IMAGE_GEN_*` alone decides whether the reference is generated or falls back to a seed. Host `ctx.vlm` and fallback `OMNI_VLM_*` independently enable pixel interpretation and the automatic visual-feedback loop. Once a usable host service is selected, a request failure remains a host-service failure and is never replayed through the environment endpoint. Without either VLM path, keep the resolved reference unchanged: seed references use their structured grammar and generated references use unanchored content-adaptive fallback guidance. Return the exact manual review request and leave visual quality pending. The VLM cannot change scientific evidence planning or page estimation.

Reference pixels and text are never evidence. Do not source or copy claims, numbers, authors, affiliations, equations, figures, citations, logos, or venue identity from them. `design_reference.image_sha256` binds only the review reference; it never enters `source_figure_sha256s` or `source_figure_manifest_sha256`.

## Scientific content contract

- Ground every visible claim, value, condition, identity, limitation, caption, and citation in the supplied material. Preserve qualifiers, variant scope, baselines, uncertainty, units, protocol, and exceptions.
- Find the paper's argumentative center instead of mirroring its sections. Use the method detail and evidence needed to understand and test the contribution; include limitations or negative results only when supplied.
- Keep evidence locally interpretable. Displayed figures are the evidence plan's selected subset of the prepared source-provenance manifest. Bind quantitative copy, equations, and every selected figure to its evidence module and exact locator/hash. Captions explain what to notice; consolidate literal paper locators in compact provenance rather than printing them beside every visual.
- Render necessary equations as semantic MathML and retain exact source LaTeX in `data-latex`. Do not expose raw LaTeX or flatten mathematical structure into prose.
- Treat priority, visual kind, organizational hints, and focal roles as optional audit/design signals. Let the paper and reference determine visible groups and hierarchy; make the page purposeful by allocating area to existing evidence or changing page size, never with filler.

## Portable commands

```bash
python3 scripts/run.py --json '{"action":"inspect","html":"/absolute/path/poster.html","output_dir":"/absolute/path/inspection"}'
python3 scripts/run.py --json '{"action":"submit-visual-review","visual_review_request":"/absolute/path/review/visual-review-request.json","visual_review_result":"/absolute/path/review/model-result.json","output_dir":"/absolute/path/review"}'
python3 scripts/run.py --json '{"action":"export-pptx","html":"/absolute/path/poster.html","output_dir":"/absolute/path/editable-poster"}'
```

For manual preparation, copy the complete `design_reference` and exact `inspection.visual_evidence` objects returned by `draft` into the payload's `reference` and `visual_evidence` fields, save that compact payload, and pass it on standard input:

```bash
python3 scripts/run.py < /absolute/path/prepare-visual-review.json
```

For a standalone visual review, keep the API key outside task input and run the exact request produced above:

```bash
export OMNI_VLM_ENDPOINT='https://gateway.example/v1/chat/completions'
export OMNI_VLM_MODEL='<vision-capable-model>'
export OMNI_VLM_API_KEY='<vlm-key>'
python3 scripts/vlm_review.py --request /absolute/path/review/visual-review-request.json
```

The script sends the bound visual reference, exact candidate overview, and optional labeled evidence atlas, then writes `model-result.json`. It does not alter HTML, invent machine bindings, or approve the poster; pass that result to `submit-visual-review` for contract validation.

## External agent portability

### Copy-only mode

Copy this directory into an external agent's skill folder and follow `SKILL.md`; deterministic inspection, validation, preview, and export actions remain available without Omni.

### Portable runner mode

Call `python3 scripts/run.py --json '<payload>'` (or pass JSON on standard input) for the same filesystem-only contracts. Run `python3 scripts/run.py --self-test` for the offline smoke test.

### Omni enhanced mode

Omni invokes `ScientificPosterEngine.execute` and adds durable task ownership, artifact persistence, provenance, resumable checkpoints, and host-provided model/VLM services.

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Omni calls `ScientificPosterEngine.execute` for the normal model-backed flow. Durable tasks checkpoint the grounded plan, resolved reference, and best accepted HTML; retries resume rather than restarting accepted work. Rejected staged revisions never replace the active candidate.
- Direct Codex, Claude Code, and inline engine use the same portable contracts. They may author complete HTML with file tools, run deterministic actions through `scripts/run.py`, and satisfy the provider-neutral review boundary with an image-capable harness.
- The Skill imports no Omni CLI module. Text authoring needs only Python 3.11; the standalone VLM reviewer additionally needs `httpx`; evidence crops and host VLM montage review additionally need Pillow; local PDF ingestion additionally needs `pymupdf>=1.24`; browser inspection and scene capture need Playwright with Chromium; editable PowerPoint export additionally needs `python-pptx>=1.0`, `mathml2omml==0.0.2`, and `pymupdf>=1.24` for inline SVG rasterization in the active Python environment. MathML expressions export as editable Office Math with a visual fallback for viewers that do not support the native equation extension. The Skill never installs dependencies into its own directory.
- `python3 scripts/check_environment.py` probes PDF ingestion and browser inspection without installing anything. With explicit authority, `--install` executes only returned allowlisted argv without a shell and then re-probes.
- `python3 scripts/run.py --self-test` runs the offline portability smoke test.

## Portable research provenance

This skill must remain portable across OmniScientist, Claude Code, Codex, and
OpenClaw.

- In OmniScientist, use available source, claim, evidence, hypothesis, and run
  recording tools, and return only the real ids they produced under `research`.
- In other runtimes, do not fail because Omni research tools are absent. Include
  human-readable source metadata and save a `provenance.json` sidecar beside the
  poster when file writing is available.
- Never invent provenance ids or promote poster reference imagery into scientific
  evidence. Preserve source locators, hashes, commands, and artifact paths needed
  to audit the delivered poster.

The Skill bundles five downsampled real conference-poster seed images. They provide visual grammar for authoring and review, not templates, starter science, or paper evidence. Their text, figures, formulas, identities, logos, and claims must never be copied into an output poster.
