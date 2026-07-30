---
name: scientific-poster
description: Use when the deliverable is a complete evidence-grounded scientific or top-conference poster, an HTML live preview, or element-targeted poster revision. Do not use for a standalone paper figure, diagram, schematic, or PPTX conversion.
license: Apache-2.0
metadata:
  helixforge:
    version: "1.0"
    dependencies: ["python>=3.11", "pymupdf>=1.24 (optional)", "playwright with chromium (optional)"]
    allowed_tools: [write_file, bash, read_file, cite_source, record_claim, add_evidence, log_run]
    kind: python_engine
    tier: research
    role: task
    research_contract: portable_provenance_v1
    status: experimental
    priority: 90
    delivery_mode: async_task
    engine:
      module: engine
      class: ScientificPosterEngine
      method: execute
    capabilities:
      - poster.scientific
      - poster.html_preview
      - poster.element_feedback
    default_for:
      - scientific poster
      - academic poster
      - conference poster
      - HTML poster
      - "\u79d1\u7814\u6d77\u62a5"
      - "\u5b66\u672f\u6d77\u62a5"
      - "\u8bba\u6587\u6d77\u62a5"
    workflow:
      failure_policy: continue_with_partial
      allow_failed_dependencies: true
    quality_contract:
      checks: [poster_html_valid, poster_render_inspected]
      assessment_required: true
      assessment_schema: "omni.deliverable-assessment/v1"
      retry:
        max_attempts: 0
        reason: "Draft/revise already perform bounded repair before publishing a version."
    input_schema:
      type: object
      properties:
        action:
          type: string
          enum: [draft, revise, validate, preview, inspect, approve, query-resource, propose-resource, promote-resource, rollback-resource]
        input:
          type: string
          description: user request, complete paper text, or a grounded poster brief
          x-omni:
            semantic_role: instruction
        pdf_uri: {type: string, description: local PDF path or artifact URI; the Skill extracts text and caption-grounded figures before authoring}
        instructions: {type: string, description: optional authoring preferences when source_text is supplied separately}
        research: {type: object}
        source: {type: string}
        source_text: {type: string}
        source_figure_sha256s: {type: array, description: machine-owned prepared PDF figure hashes for approval}
        source_html_uri: {type: string}
        source_html_sha256: {type: string}
        html: {type: string}
        feedback: {type: string}
        selection_state: {type: object}
        page: {type: object}
        assets: {type: array}
        scale: {type: number}
        source_html_path: {type: string}
        approved: {type: boolean}
        operator_confirmation: {type: string}
        session_id: {type: string}
        host_event_id: {type: string}
        output_dir: {type: string}
        cwd: {type: string}
        project_library: {type: string}
        user_library: {type: string}
        allow_candidates: {type: boolean}
        kind: {type: string, enum: [component, layout-policy]}
        semantic_roles: {type: array}
        page_mode:
          type: string
          enum: [portrait, landscape]
          x-omni:
            semantic_key: page_mode
            binding_owner: model
            expectation:
              kind: explicit_enum
              signatures:
                portrait:
                  - portrait
                  - vertical layout
                  - "\u7ad6\u7248"
                  - "\u7eb5\u5411"
                landscape:
                  - landscape
                  - horizontal layout
                  - "\u6a2a\u7248"
                  - "\u6a2a\u5411"
        resource_id: {type: string}
        version: {type: string}
        content_sha256: {type: string}
        scope: {type: string, enum: [project, user]}
        target_version: {type: string}
        reason: {type: string}
      additionalProperties: true
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
                - missing_capability
                - capabilities_ready
                - capability_probe_failed
                - approval_required
                - poster_approval_recorded
                - approval_receipt_untrusted
                - approval_source_mismatch
                - resource_query_complete
                - resource_candidate_created
                - resource_promoted
                - resource_rollback_complete
                - resource_conflict
                - resource_candidate_required
                - promotion_approval_required
                - resource_identity_changed
                - rollback_target_missing
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
        artifacts: {type: array}
        sources: {type: array}
        research: {type: object}
        warning: {type: string}
        warnings: {type: array}
        paper_source: {type: object}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        error: {type: string}
        error_info: {type: object}
        deliverable_assessment:
          type: object
          description: "provider-owned static-validation and rendered-inspection assessment"
          required: [schema, deliverable_id, provider_binding_id, provider, contract_hash, step_id, feedback, status, retryable, effective_inputs, criteria]
          properties:
            schema: {type: string, const: "omni.deliverable-assessment/v1"}
            deliverable_id: {type: string, minLength: 1}
            provider_binding_id: {type: string, minLength: 1}
            provider: {type: string, const: "scientific-poster"}
            provider_authority_fingerprint: {type: string}
            contract_hash: {type: string, minLength: 1}
            step_id: {type: string, minLength: 1}
            feedback: {type: string, minLength: 1}
            status: {type: string, enum: [passed, degraded, failed, unknown]}
            retryable: {type: boolean}
            effective_inputs: {type: object}
            criteria:
              type: array
              minItems: 2
              items:
                type: object
                required: [criterion_id, status]
                properties:
                  criterion_id: {type: string, enum: [poster_html_valid, poster_render_inspected]}
                  status: {type: string, enum: [passed, degraded, failed, unknown]}
                  summary: {type: string}
                  evidence_refs: {type: array, items: {type: string}}
      additionalProperties: true
    trigger:
      phrases: ["\u79d1\u7814\u6d77\u62a5", "\u5b66\u672f\u6d77\u62a5", "\u8bba\u6587\u6d77\u62a5", conference poster, scientific poster, academic poster, HTML poster, poster preview]
      when_to_use: Use when the final deliverable is a complete scientific, academic, or conference poster and its HTML preview, revision, or approval snapshot.
    notification:
      display_label: Scientific poster
      title_field: summary
      preview_uri_field: preview_uri
  openclaw:
    emoji: "🪧"
    requires:
      bins: [python3]
---

# Scientific Poster

Author a complete HTML/CSS scientific poster, review it in a live preview, and snapshot the exact approved HTML. The model designs the page directly; JSON is machine state for resource indexes, selections, and approval receipts, never the poster authoring language.

## Required workflow

1. Read [HTML authoring](references/structured-authoring.md), [poster quality](references/poster-quality.md), [contracts](references/contracts.md), and [preview/approval](references/preview-and-approval.md). Read [resource evolution](references/component-evolution.md) only when changing a reusable resource.
2. Obtain a local `pdf_uri`, complete paper text, or a demonstrably complete grounded brief. For a PDF, pass the path directly: the Skill first extracts page-aware text and caption-grounded figure crops, and only then calls the HTML authoring model. Do not pre-read the binary as text and do not design from the abstract alone. Identify the problem, central contribution, method novelty, strongest one to three results, limitations, authorship, and provenance before laying out the page.
3. Choose the scientific story automatically. Prefer a figure-led composition when a decisive figure carries the result, method-led when architecture or procedure is the novelty, and balanced otherwise. These are adaptive layout policies, not page slots.
4. Author one complete HTML document beginning with `<!doctype html>`. Use inline CSS, physical millimetres, embedded figures, and no active or remote content. When PDF preparation yields source figures, use at least one relevant original figure rather than redrawing all evidence. Optional reviewed components are HTML/CSS atoms and composites that may be adapted, combined, or omitted.
5. Run static validation and Chromium inspection. Repair the complete HTML from concrete issues, with at most two repair calls per model boundary. Reject clipping, overflow, tiny type, untraceable numbers, excessive unused page area, large empty method/evidence regions, or a display-scale hero claim compressed into a narrow four-plus-line block.
6. Start the returned `preview_argv` and show the live preview to the user. The injected inspector captures a stable `data-poster-id`, nearest `data-poster-region`, text, rectangle, bounded computed styles, and optional component identity without mutating canonical HTML.
7. Revise the complete HTML from user feedback. Verify the source HTML SHA-256 and selection identity first; preserve page dimensions, scientific grounding, stable ids, and unrelated regions. A background revision should pass the same `pdf_uri` when the original grounded text is no longer in host memory. Embedded image bytes are tokenized only across the model boundary and restored exactly afterward.
8. Ask for the exact returned `operator_confirmation`, which binds the HTML hash, grounding-source hash, and prepared-figure manifest hash. Pass the original `pdf_uri` again when approving after a process restart; the deterministic runner reconstructs page-aware grounding and the figure manifest instead of trusting caller-supplied hashes. Approval snapshots exactly `poster.html` and `approval.json`; any HTML byte change needs a new approval.
9. Return `approved_html_path`, `approval_path`, and their hashes as the handoff. This Skill does not convert HTML to PPTX or claim PPTX fidelity; route that work to a dedicated conversion Skill.

## Scientific content contract

- Ground every visible claim, number, unit, condition, author, affiliation, limitation, caption, and citation in supplied material.
- Find the paper's argumentative center rather than copying section summaries. Use author-emphasized figures/tables and comparisons that directly support the main contribution.
- Keep baselines, uncertainty, sample size, evaluation conditions, and negative or limiting results when supplied.
- Explain method logic at the depth needed to understand the novelty. Prefer a compact architecture or flow over a generic bullet list.
- Show exactly one visible `hero`, `method`, `evidence`, `limitations`, and `provenance` region through `data-poster-region`. Meaningful selectable elements use unique stable `data-poster-id` values and source labels.
- Design for expert discussion at a top conference: restrained color, readable physical type, decisive evidence, coherent reading order, and purposeful density. It is not a dashboard, campaign, or decorative infographic.

## Components and evolution

Two optional resource kinds exist: `component` and `layout-policy`. Components are content-free HTML/CSS atoms or composites; layout policies describe adaptive conference composition. Both evolve through built-in, project candidate, and user-approved layers using exact content SHA-256 identities. Candidates never enter ordinary generation unless explicitly requested.

Use `query-resource`, `propose-resource`, `promote-resource`, and `rollback-resource`. Promotion requires `APPROVE SCIENTIFIC-POSTER <kind> <content_sha256>` and never mutates an existing version.

```bash
python3 scripts/run.py --json '{"action":"inspect","html":"/absolute/path/poster.html","output_dir":"/absolute/path/inspection"}'
python3 scripts/run.py --json '{"action":"propose-resource","kind":"component","source":"/absolute/path/component","cwd":"/absolute/project"}'
python3 scripts/run.py --json '{"action":"promote-resource","kind":"component","resource_id":"claim-variant","version":"1.0.0","content_sha256":"<sha256>","approved":true,"operator_confirmation":"APPROVE SCIENTIFIC-POSTER component <sha256>","session_id":"<session>"}'
python3 scripts/run.py --json '{"action":"rollback-resource","kind":"component","scope":"user","resource_id":"claim-variant","target_version":"1.0.0","reason":"restore reviewed default"}'
```

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Copy-only mode: copy this skill folder into a Claude Code, Codex, or OpenClaw
  skill directory. The host agent authors the same complete HTML directly and
  uses the bundled scripts for deterministic validation, inspection, preview,
  resource lifecycle, and approval.
- Portable runner mode: run `python3 scripts/run.py --self-test`, then invoke
  `python3 scripts/run.py --json '{...}'` for deterministic actions. The runner
  imports no Omni module. Text authoring needs Python 3.11; PDF ingestion also
  needs `pymupdf>=1.24`, and browser inspection needs Playwright with Chromium.
- Omni enhanced mode: OmniScientist/HelixForge reads
  `metadata.helixforge`, calls `ScientificPosterEngine.execute` for model-backed
  draft/revise actions, persists task state, stores poster artifacts, and
  records provenance.

`python3 scripts/check_environment.py` probes PDF and browser capabilities
without installing anything. Missing optional capabilities return
`missing_capability` with exact `install_argv`; a later call probes again.

## Portable research provenance

This skill must remain portable across OmniScientist, Claude Code, Codex, and
OpenClaw.

- In OmniScientist, use available provenance tools and return their real ids in
  `research.source_ids`, `research.claim_ids`, `research.evidence_ids`, and/or
  `research.run_id` together with poster artifact URIs and hashes.
- In other runtimes, do not fail when Omni tools are absent. Include a Markdown
  **Provenance** section and, when possible, write `provenance.json` containing
  source paths/identifiers, extracted-figure hashes, poster hashes, approval
  hashes, commands, and artifact paths.
- Never invent provenance ids or scientific grounding. Human-readable source
  metadata and deterministic hashes are the fallback.

The Skill contains no built-in `assets/`, finished poster, or starter scientific content.
