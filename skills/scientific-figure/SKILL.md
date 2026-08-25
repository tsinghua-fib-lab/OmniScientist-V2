---
name: scientific-figure
description: Default drawing skill for architecture, system, and workflow diagrams (DOT/SVG/PNG). Also use when the user names Graphviz, leaves a .dot, or asks for SVG-only / PNG-only. Use livefigure only for an editable single-slide PPTX. Do not bash `dot`.
license: Apache-2.0
metadata:
  helixforge:
    version: "1.3"
    dependencies: ["python>=3.11", "matplotlib (optional)", "graphviz (optional, `dot`)"]
    allowed_tools: [write_file, bash, read_file, cite_source, record_claim, add_evidence, log_run]
    tier: research
    role: task
    research_contract: portable_provenance_v1
    status: stable
    priority: 80
    capabilities:
      - artifact.figure
      - figure.scientific
      - figure.architecture
      - figure.workflow
      - artifact.dot
      - artifact.svg
      - artifact.png
      - research.provenance
    deliverables:
      - artifact.figure
    default_for:
      - scientific figure
      - architecture diagram
      - system diagram
      - graphviz diagram
      - DOT diagram
      - 架构图
      - 示意图
    delivery_mode: async_task
    kind: python_engine
    workflow:
      failure_policy: continue_with_partial
      allow_failed_dependencies: true
      failure_types: ["missing_input", "renderer_unavailable", "render_failed", "artifact_write_failed"]
    # Provider-owned component vocabulary mirrored by the engine's creation
    # quality gate. The host preserves it for provenance but does not infer or
    # override figure semantics from it.
    template_signatures:
      rag: [rag, query, retriev, rerank, vector, embedding, llm, grounded, citation]
      transformer: [transformer, encoder, decoder, attention, multi-head, self-attention, cross-attention]
    artifact_revision:
      contract: graphviz-dot
      source_formats: ["dot"]
      derived_formats: ["svg", "png"]
      supports:
        - minor_style_edit
        - major_revision
      renderer: graphviz
      provenance: record_run
      revision_inputs:
        source_task_id: true
        source_artifact_uri: true
        source_artifact_path: true
    engine:
      module: engine
      class: ScientificFigureEngine
      method: execute
    input_schema:
      type: object
      properties:
        input:
          type: string
          description: "figure requirement / natural-language prompt"
          x-omni:
            semantic_role: instruction
        title:
          type: string
          description: "optional figure title"
        figure_kind:
          type: string
          enum: [generic, rag, transformer]
          description: "normalized built-in template selected by the semantic planner"
          x-omni:
            semantic_key: figure_kind
            binding_owner: model
            expectation:
              kind: template_signature
        source_artifact_dot:
          type: string
          description: "Graphviz DOT to render as the figure. Without revision_mode this graph is the figure (not a template stamp or an append-only revision)."
        source_artifact_path:
          type: string
          description: "Path to a .dot file with the same meaning as source_artifact_dot."
        revision_mode:
          type: string
          description: "When set (e.g. major), treat source DOT as a revision base and append engineering detail. Omit it to render the source graph as-is."
      required: ["input"]
    output_schema:
      type: object
      properties:
        status: {type: string, enum: ["ok", "partial", "error"]}
        outcome: {type: object, description: "domain-specific result metadata such as code, reason, counts, or classification"}
        title: {type: string}
        figure_kind: {type: string, enum: [generic, rag, transformer]}
        requested_figure_kind: {type: string, enum: [generic, rag, transformer]}
        effective_inputs:
          type: object
          description: "sanitized provider inputs actually used to render the figure"
        deliverable_assessment:
          type: object
          description: "provider-owned semantic quality assessment consumed by task verification"
          required: [schema, deliverable_id, provider_binding_id, provider, contract_hash, step_id, feedback, status, retryable, effective_inputs, criteria]
          properties:
            schema: {type: string, const: "omni.deliverable-assessment/v1"}
            deliverable_id: {type: string, minLength: 1}
            provider_binding_id: {type: string, minLength: 1}
            provider: {type: string, const: "scientific-figure"}
            provider_authority_fingerprint: {type: string}
            contract_hash: {type: string, minLength: 1}
            step_id: {type: string, minLength: 1}
            feedback: {type: string, minLength: 1}
            status: {type: string, enum: [passed, degraded, failed, unknown]}
            retryable: {type: boolean}
            effective_inputs: {type: object}
            criteria:
              type: array
              minItems: 1
              items:
                type: object
                required: [criterion_id, status]
                properties:
                  criterion_id: {type: string, minLength: 1}
                  status: {type: string, enum: [passed, degraded, failed, unknown]}
                  summary: {type: string}
                  evidence_refs: {type: array, items: {type: string}}
            evidence_refs: {type: array, items: {type: string}}
            summary: {type: string}
        caption: {type: string}
        preview_uri: {type: string}
        artifacts: {type: array}
        dot_uri: {type: string}
        svg_uri: {type: string}
        png_uri: {type: string}
        run_id: {type: string}
        research: {type: object}
        summary: {type: string}
        warning: {type: string}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        error: {type: string}
        error_info: {type: object}
      required: ["status"]
    migrated_from: "omniscientist-helixforge/skills/research/scientific_figure"
    trigger:
      phrases:
        - scientific figure
        - architecture diagram
        - graphviz diagram
        - DOT diagram
        - as DOT
        - source_artifact_dot
        - 架构图
        - 示意图
      when_to_use: "Default drawing skill for ordinary figures, architecture diagrams, and Graphviz DOT/SVG/PNG. Use livefigure only when the user asked for an editable single-slide PPTX."
      when_not_to_use: "Editable single-slide PPTX (livefigure). Complete multi-slide decks (research-pptx). Do not invent a .dot file unless the user left one."
    notification:
      display_label: "Scientific figure"
      title_field: "title"
      preview_uri_field: "preview_uri"
  openclaw:
    emoji: "🖼️"
    requires:
      bins: ["python3"]
---

# scientific-figure

Default drawing skill (DOT/SVG/PNG) for unspecified architecture, flow, and
system schematics. Also use it when the user names Graphviz, leaves a `.dot`,
or asks for SVG-only / PNG-only. A configured VLM does not move this work to
`livefigure`.

When not to use: an editable single-slide PPTX (`livefigure`) or a complete
multi-slide deck (`research-pptx`). Do not invent a `.dot` file and then look
for a renderer.

Use this skill when the user asked for a figure, DOT/SVG/PNG, left a `.dot` to
render, or named Graphviz. A closed
perceive/act/reflect (or other non-template) topology is synthesized as a weaker
Graphviz schematic (`status=partial`); the engine does not stamp a linear
placeholder or require the caller to author DOT.

Produce a clean, publication-quality figure from a description.

Procedure:
1. Clarify the figure's intent: the stages/components, their relationships, and
   the desired emphasis. Plan a simple, legible layout (avoid clutter).
2. Choose a renderer that is actually installed (check with `command -v`):
   - **graphviz (`dot`)** for boxes-and-arrows architecture/workflow diagrams —
     write a `.dot` file, then `dot -Tpng figure.dot -o figure.png` (and `-Tsvg`).
   - **matplotlib** for plots/annotated schematics — write a small Python script
     and run it with `python3` to emit `figure.png` (use `dpi=200`, a readable
     font size, and tight layout).
3. Save outputs as artifacts and verify the file was created (`read_file`/`ls`).
4. If neither renderer is available, fall back to a self-contained Mermaid or
   SVG draft inside this skill and say so.

Output (Markdown): the figure title, the rendered file path(s), and a one-line
caption describing what the figure shows.

OmniScientist execution contract:
- Return structured `artifacts` entries (`title`, `format`, `uri`, `path`, `mime`).
- For canonical scientific concepts, cite or record sources, claims, evidence,
  and a rendering run so the figure is auditable and reproducible.
- When the python_engine has already produced PNG/SVG, do not re-render with
  `bash` `dot`. The engine already calls Graphviz in-process.
- First-time creation takes natural-language `input` only. The engine generates
  DOT and renders SVG/PNG. Do not write a `.dot` and then shell out to
  `dot -Tpng`. Do not require `source_artifact_dot` for a new figure.
- Built-in templates are only `generic`, `rag`, and `transformer`. A closed
  perceive/act/reflect (or other non-template) topology is synthesized from the
  named stages (`status=partial`, `outcome.code=instruction_graph`): the engine
  does not stamp a linear RAG/generic placeholder or claim a full architecture.
- To render an exact Graphviz graph, pass `source_artifact_dot` or
  `source_artifact_path` **without** `revision_mode`.
- `revision_mode` is only for appending engineering detail onto an existing
  graph. It will not replace a linear RAG stamp with a closed agent loop.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Copy-only mode: copy this skill folder into a Claude Code, Codex, or OpenClaw
  skill directory. The agent can read this `SKILL.md` and create the figure with
  its normal file and shell tools.
- Portable runner mode: from this skill directory, run
  `python3 scripts/run.py --json '{"input":"RAG architecture with query, retriever, reranker, LLM","output_dir":"figure-out"}'`.
  The runner is self-contained, writes DOT/SVG artifacts, uses Graphviz when
  installed, falls back to built-in SVG when it is not, and does not require
  Omni to be installed. Unknown domains use a generic input/process/output
  flow instead of guessing domain-specific components from keywords.
- Omni enhanced mode: OmniScientist/HelixForge reads `metadata.helixforge`,
  calls `engine.py`, persists workflow state, stores artifacts, records source
  claims/evidence/render runs, and can pass the figure result to later steps.

## Portable research provenance

This skill must remain portable across OmniScientist, Claude Code, Codex, and
OpenClaw.

- In OmniScientist, if tools such as `cite_source`, `record_claim`,
  `add_evidence`, `record_hypothesis`, or `log_run` are available, use them and
  include returned ids in `research.source_ids`, `research.claim_ids`,
  `research.evidence_ids`, `research.hypothesis_ids`, and/or `research.run_id`.
- In other runtimes, do not fail because those tools are absent. Instead include
  a **Provenance** section in the Markdown answer and, when file writing is
  available, save `provenance.json` with the same shape plus artifact paths.
- Never invent provenance ids. Use real tool-returned ids; otherwise cite
  human-readable source metadata such as arXiv id, DOI, URL, title, run
  command, and artifact path.
