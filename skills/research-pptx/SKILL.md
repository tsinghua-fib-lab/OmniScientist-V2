---
name: research-pptx
description: >
  Generate a complete scientific presentation (PPTX slide deck) from a research
  paper (PDF), a text document, or a topic prompt. Use when the user asks for
  slides, a slide deck, a conference talk, a thesis defense, a group‑meeting
  report, or a seminar. Renders in one shot by default; only pauses for a human
  review checkpoint when the user explicitly asks to approve the outline first.
  Do not use for one editable single-slide scientific figure; use LiveFigure.
  Ordinary architecture / flowchart figures are scientific-figure.
license: Apache-2.0
metadata:
  helixforge:
    version: "2.4"
    dependencies:
      - "python>=3.11"
      - "httpx>=0.27"
      - "pydantic>=2.5"
      - "pymupdf>=1.23"
      - "pymupdf4llm"
      - "python-pptx"
      - "node>=20.9"
    allowed_tools: [arxiv-fetch, log_run, cite_source, record_claim, add_evidence]
    tier: research
    role: task
    research_contract: portable_provenance_v1
    delivery_mode: async_task
    kind: python_engine
    priority: 100
    execution:
      # Own wall clock: a full deck (PDF/topic → outline → Node render) routinely
      # exceeds the undeclared 600s fallback. 15 minutes stays under the
      # python_engine ceiling; do not inherit skills.default_seconds.
      max_seconds: 900
    deliverables:
      - artifact.slides
    capabilities:
      - slides.generate
      - artifact.slides
      - slides.from_pdf
      - slides.from_markdown
      - slides.from_outline
    default_for:
      - generate slides
      - create slides
      - make slides
      - slide deck
      - conference talk
      - thesis defense
      - group meeting
      - seminar
      - presentation
      - outline review
      - check outline
      - review outline
      - approve outline
      - generate ppt
      - make ppt
    engine:
      module: engine
      class: ResearchPptxEngine
      method: execute
    workflow:
      failure_policy: continue_with_partial
      allow_failed_dependencies: true
      failure_types: ["missing_input", "node_unavailable", "pdf_parse_failed", "render_failed", "artifact_write_failed"]
    quality_contract:
      checks: [slides_rendered_and_quality_checked]
      assessment_required: true
      assessment_schema: "omni.deliverable-assessment/v1"
      retry:
        max_attempts: 0
        reason: "The engine already performs bounded layout repair before storing its artifact."
    input_schema:
      type: object
      properties:
        topic:
          type: string
          description: >
            Core topic or instruction (kept verbatim). Required for a NEW deck.
            Omit ONLY when resuming a review with resume_token.
          x-omni:
            semantic_role: instruction
        pdf_uri:
          type: string
          description: >
            Absolute or relative path (or artifact:// uri) of a PDF to parse.
            Pass the path directly; the skill opens and parses it itself. Never
            pre‑read the PDF with read_file/bash — it is binary.
        paper_uris:
          type: array
          items: { type: string }
          description: >
            Multiple PDF paths / artifact:// uris merged as one corpus. Use this
            when the deck should synthesize several papers rather than one.
        corpus_query:
          type: string
          description: >
            Semantic query against omni's local corpus. Retrieved chunks + their
            source metadata become the deck's source (requires an omni ctx).
        source_ids:
          type: array
          items: { type: string }
          description: >
            ROM source ids (typically produced by an upstream literature.search
            step) to build the deck from. Their citations become footnotes.
        reference_text:
          type: string
          description: "Primary text source, including pasted content or a structured summary of prior discussion."
        language:
          type: string
          enum: [en, zh]
          x-omni:
            semantic_key: language
            binding_owner: model
            expectation:
              kind: language
              signatures:
                zh: [Chinese, "\u4e2d\u6587", "\u6c49\u8bed"]
                en: [English, "\u82f1\u6587", "\u82f1\u8bed"]
          description: >
            Output language. MUST be exactly 'zh' or 'en'. Map Chinese → 'zh',
            English → 'en'. Default 'en'. (Aliases like 'Chinese' are auto‑corrected
            but you should pass the enum value directly.)
        output_language:
          type: string
          description: "Compatibility alias mapped to language; prefer language."
        talk_type:
          type: string
          enum: [conference, seminar, group_meeting, defense]
          x-omni:
            semantic_key: talk_type
            binding_owner: model
            expectation:
              kind: explicit_enum
              signatures:
                conference: [conference, conference talk, "\u4f1a\u8bae\u62a5\u544a"]
                seminar: [seminar, seminar talk, "\u7814\u8ba8\u4f1a"]
                group_meeting: [group meeting, lab meeting, "\u7ec4\u4f1a"]
                defense: [defense, thesis defense, defense talk, "\u7b54\u8fa9"]
          description: >
            Presentation type. MUST be exactly one of conference, seminar,
            group_meeting, or defense. Map accordingly (e.g., defense talk,
            thesis defense → defense). Default conference.
        duration_minutes:
          type: integer
          description: "Talk length 5-90 (default 15)."
        target_slides:
          type: integer
          description: "Explicit slide count 3-80 (only if the user states it)."
        color_theme:
          type: string
          enum: [midnight_executive, teal_trust, forest_moss, charcoal_minimal]
          x-omni:
            semantic_key: color_theme
            binding_owner: model
            expectation:
              kind: explicit_enum
              explicit_only: true
              signatures:
                midnight_executive: [midnight executive, midnight theme, "\u6df1\u8272\u5546\u52a1"]
                teal_trust: [teal trust, teal theme, "\u9752\u7eff\u8272\u4e3b\u9898"]
                forest_moss: [forest moss, forest theme, "\u68ee\u6797\u7eff\u4e3b\u9898"]
                charcoal_minimal: [charcoal minimal, charcoal theme, "\u70ad\u9ed1\u6781\u7b80"]
        mode:
          type: string
          enum: [auto, agentic]
          x-omni:
            semantic_key: generation_mode
            binding_owner: model
            expectation:
              kind: explicit_enum
              explicit_only: true
              signatures:
                auto: [auto mode, deterministic pipeline, "\u81ea\u52a8\u6a21\u5f0f", "\u786e\u5b9a\u6027\u6d41\u7a0b"]
                agentic: [agentic mode, let the model decide, "\u667a\u80fd\u4f53\u6a21\u5f0f"]
          description: >
            'auto' (default) runs the deterministic pipeline and renders in one
            shot. 'agentic' lets the model decide the slide‑type mix, but it
            STILL renders directly unless the user explicitly sets review_mode.
        review_mode:
          type: string
          enum: [none, plan, interactive]
          x-omni:
            semantic_key: review_mode
            binding_owner: model
            expectation:
              kind: explicit_enum
              default: none
              explicit_only: true
              signatures:
                plan:
                  - review the outline
                  - approve the outline
                  - "\u5148\u770b\u5927\u7eb2"
                  - "\u5ba1\u6279\u5927\u7eb2"
                interactive:
                  - interactive review
                  - "\u4ea4\u4e92\u5f0f\u5ba1\u9605"
          description: >
            'none' (default) renders directly. Set 'plan'/'interactive' ONLY
            when the user explicitly asks to review/approve the outline first;
            it returns the outline for approval, then resume with resume_token.
        resume_token:
          type: string
          description: "Returned by a review checkpoint; pass back to continue."
        approved_plan:
          type: object
          description: "Complete replacement plan to render when resuming."
        plan_edits:
          type: array
          items: {type: object}
          description: >
            Ordered surgical edits applied to the cached plan. Indices refer to
            the plan produced by the preceding edit; see references/omni.md.
        template_uri:
          type: string
          description: "artifact:// or path of a PPTX whose theme (colours+fonts) to adopt."
        outline:
          type: string
          description: "A user outline; slides follow it top‑to‑bottom (source_type=outline)."
        markdown_uri:
          type: string
          description: >
            artifact:// or an existing absolute path of a .md source. A bare
            filename in topic is a deliverable name, not this field — pass a
            durable handle here only when the file should drive the deck.
      required: []
    output_schema:
      type: object
      properties:
        status: {type: string, enum: ["ok", "partial", "error"]}
        outcome: {type: object, description: "domain result code, e.g. awaiting_review / node_unavailable / pdf_parse_failed / empty_results"}
        title: {type: string}
        pptx_uri: {type: string}
        slide_count: {type: integer}
        figures_used: {type: integer}
        resume_token: {type: string}
        report_uri: {type: string}
        strategy: {type: object}
        summary: {type: string}
        warning: {type: string}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        research: {type: object}
        run_id: {type: string}
        metadata: {type: object}
        error: {type: string}
        error_info: {type: object}
        deliverable_assessment:
          type: object
          required: [schema, deliverable_id, provider_binding_id, provider, contract_hash, step_id, feedback, status, retryable, effective_inputs, criteria]
          properties:
            schema: {type: string, const: "omni.deliverable-assessment/v1"}
            deliverable_id: {type: string, minLength: 1}
            provider_binding_id: {type: string, minLength: 1}
            provider: {type: string, const: "research-pptx"}
            contract_hash: {type: string, minLength: 1}
            step_id: {type: string, minLength: 1}
            feedback: {type: string, minLength: 1}
            status: {type: string, enum: [passed, degraded, failed, unknown]}
            retryable: {type: boolean}
            effective_inputs: {type: object}
            criteria: {type: array, minItems: 1}
        artifacts:
          type: array
          items:
            type: object
            properties:
              title: {type: string}
              format: {type: string, const: pptx}
              uri: {type: string}
              path: {type: string}
              mime: {type: string}
              size_bytes: {type: integer, minimum: 0}
            required: [title, format, uri, path, mime, size_bytes]
      required: ["status"]
    trigger:
      phrases: ["ppt", "slides", "slide", "presentation", "deck",
                "slide deck", "conference talk", "thesis defense",
                "group meeting", "seminar", "make slides", "create slides",
                "generate slides", "generate a slide deck", "make a deck",
                "create a presentation", "from an outline", "from a markdown",
                "from a pdf", "from a paper", "review outline",
                "approve before rendering", "outline review",
                "check outline first", "show outline", "outline first",
                "approve outline", "make ppt", "generate ppt", "create pptx"]
      when_to_use: >
        Build a slide deck from a paper/topic/outline/markdown; OR draft an
        outline for the user to review first (review_mode=plan). Read the user's
        intent and set the matching input fields — never paste intent phrases
        like 'action=export' or 'review_mode=plan' into topic.
        For outline-first requests invoke this skill with review_mode=plan; the
        skill owns planning, plan_edits, and typed resume_token continuation.
    notification:
      display_label: "Slide Generation"
      title_field: "title"
  openclaw:
    requires:
      bins: ["python3", "node"]
      env: ["OPENAI_BASE_URL", "OPENAI_API_KEY"]
---

# research-pptx

## Intent routing — map the user's words to `input` fields FIRST

Before calling, classify the user's request and set the corresponding `input`
fields. Do **NOT** copy phrases like "review_mode=plan" into `topic`; this skill
generates decks and does not export existing PPTX files.

| The user says | Set these `input` fields | Do NOT |
|---------------|--------------------------|--------|
| Show me the outline first / let me review / approve before rendering | `review_mode="plan"` (plus the normal source fields) | do NOT render directly |
| From this outline / based on this outline | `outline=<the outline text or file content>` | — |
| From this markdown / convert this .md to slides | `markdown_uri=<absolute path or artifact://>` | Do not put a bare filename only in `topic` and expect it to be a cwd path |
| From this paper / PDF / research paper | `pdf_uri=<path>`, `topic=<user's ask>` | do NOT read the PDF yourself |
| Use this template / apply this theme | `template_uri=<pptx path>` | — |
| Just make me a deck / create a presentation (no review words) | `topic=<the ask>` (+ source), `review_mode="none"` | do NOT set review_mode=plan |

**Review means stop‑and‑ask.** If the request contains "review"/"approve"/"outline
first", set `review_mode="plan"`; the skill returns `status:"partial"` with
`outcome.code:"awaiting_review"` and a `resume_token` — surface the outline and
wait, do **NOT** render.

## What it does

Build a professional scientific slide deck (PPTX) from a paper, text, or topic.

Pipeline: parse the PDF (text, figures, tables, equations) → plan the deck with
an LLM → render with PptxGenJS (Node.js) → auto‑fix overflow → run a text‑domain
structured visual critique (layout report + figure/section alignment) → repair
and re‑render → store the artifact and return `pptx_uri`.

## Lifecycle status vs. domain outcome

`status` is ALWAYS one of the lifecycle values `ok | partial | error`. Any
domain‑specific state is reported under `outcome.code`. In particular, a review
checkpoint returns `status: "partial"` with `outcome.code: "awaiting_review"`
(plus a `resume_token`), NOT a bespoke status. This keeps the deck composable in
durable workflows where the scheduler only reasons over the lifecycle status.

## One‑shot by default

For a direct request like **"make me a 15‑page conference deck"** or **"generate
a deck from this paper"**, call the skill **once** with `topic` (and `pdf_uri` if a
PDF is given) and let it render end‑to‑end. Do **not** set `review_mode`. Do
**not** use `submit_task`'s two‑phase inspect flow unless you truly need to read
the full spec first.

## Decision model (LLM‑driven, human‑reviewable)

This skill is **not a fixed pipeline**, but it renders in one shot unless the
**user** explicitly asks to review the outline. Two knobs govern behaviour:

- `mode`:
  - `auto` (default, recommended for direct requests) — deterministic
    slide‑type selection; renders in one shot.
  - `agentic` — the model first decides a **strategy** (slide‑type mix,
    emphasis) and records it for offline analysis. It **still renders directly**;
    it will **not** pause on its own. Only the user's explicit `review_mode`
    triggers a checkpoint.
- `review_mode`:
  - `none` (default) — render directly.
  - `plan` / `interactive` — **only when the user explicitly asks** to review /
    approve the outline first. Stops after planning and returns
    `{status: "partial", outcome: {code: "awaiting_review"}, resume_token, plan,
    strategy, report_uri}`.

Every decision point (strategy choice, review checkpoint, outline approval, and
the structured visual critique) is instrumented so choices can be analysed later —
the model plans, the user retains final say only at the nodes the user opted
into.

## Text‑domain visual critique (no multimodal model required)

The default model has no image understanding, so the skill does not "look at"
the rendered slides. Instead, after rendering it builds a **structured layout
report** (per‑slide figure aspect / layout mode / fill ratio / bullet lengths /
overflow) plus each figure's `caption`, `related_text`, and `section` tag, and
asks the LLM to review that **textual** report. The critique catches
figure/section mismatches, sparse slides, over‑long bullets, and figure↔bullet
inconsistencies, proposes minimal edits (reassign/drop figure, trim bullet), and
the deck is repaired and re‑rendered once. This gives a visual‑feedback loop
without any multimodal capability.

## Running the skill

- Omni agents: read [references/omni.md](references/omni.md) for background
  execution, review/resume, PDF handling, workflow, and corpus examples.
- Before the first real render: read
  [references/runtime-setup.md](references/runtime-setup.md). Omni installation,
  init, and update prepare the owner-managed renderer cache; copied portable
  Skills use local `npm ci`. Runtime execution only validates dependencies.
- Other agents: use the portable runner described under **External agent
  portability** below.

### Missing figures → placeholders (not silent drops)

When the plan references `figure_N` but the file is missing (e.g. an outline
mentioning `![](fig.png)` before the image is placed on disk), the skill now
renders a labelled grey **placeholder** so the slide layout stays intact and
the caption is prefixed with `[placeholder]`. Search for `[placeholder]` in
the generated deck to spot every slide that still needs a real figure. The
old "downgrade the whole slide to plain content" behaviour is kept only as a
last‑resort fallback (e.g. Pillow unavailable).

### Markdown / outline sources have full parity

An `outline` or `markdown_uri` source is parsed for:
- section headings (`#`, `##`, `###`) → deck section skeleton,
- inline `![alt](path)` figures → resolved and passed to the planner,
- pipe tables `| a | b |` → surfaced as `table` slides,
- `[N] Author, Year, Title` lines → seeded into the References slide.

So a markdown‑driven or outline‑driven deck now has the same feature surface
as a PDF‑driven one, minus the auto‑extracted PDF figures.

## Duration → slide count

| talk_type | minutes → slides |
|-----------|------------------|
| conference | 5→6, 10→11, 15→16, 20→22 |
| seminar | 30→27, 45→40, 60→52 |
| group_meeting | 15→14, 30→25, 45→35, 60→45 |
| defense | 30→30, 45→40, 60→52 |

## Returns

`title`, `pptx_uri`, `slide_count`, `figures_used`, a structured `artifacts`
entry (`uri`, local `path`, MIME type, and size), plus `metadata`. The
`artifact://` URI remains the stable identity; Omni CLI may show the resolved
local path while IM channels use it only for native file upload. When (and only
when) the user asked to review first, returns `status: "partial"` with
`outcome.code: "awaiting_review"`, `resume_token`, `plan`, `strategy`, and
`report_uri`. Tell the user the deck is ready (or awaiting their approval); do
not keep calling tools.

## Source formats

Besides a PDF (`pdf_uri`) or a bare `topic`, you can drive generation from:
- `outline` — a user outline; slides follow it top‑to‑bottom.
- `markdown_uri` — a `.md` file (`artifact://` or an existing absolute path);
  sections drive the deck. A bare name in `topic` (e.g. `综述.md`) is a
  deliverable mention: the skill binds it only when that file already exists
  in this task's reports/artifacts. A `.md` mistakenly
  passed as `pdf_uri` is auto‑detected and parsed as markdown.

## Template (theme + master adoption)

Pass `template_uri` (a user PPTX) to reuse its branding. The skill adopts:
- the **colour scheme + fonts** (from the theme), and
- a **slide master**: background solid/image, a corner **logo**, and an optional
  title band — reconstructed via `defineSlideMaster`, so every generated slide
  inherits the template's look without copying the template's own slide content.

Editable placeholders and complex custom layouts are not copied; the skill
rebuilds a clean master from the extracted background/logo/geometry.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Copy-only mode: copy this skill folder into a Claude Code, Codex, or OpenClaw
  skill directory. The agent reads this `SKILL.md` and can drive the slide
  pipeline with its normal file/shell tools.
- Portable runner mode: from this skill directory, run
  `python3 scripts/run.py --json '{"topic":"My talk","reference_text":"..."}'`.
  The runner is self‑contained, writes the PPTX to `./out/`, requires Node.js for
  rendering, and does not import Omni. Single‑shot runners cannot hold a paused
  review or resume state, so `review_mode` is forced to `none`. Use Omni or its
  MCP server for durable outline review/resume. Set `OPENAI_BASE_URL` +
  `OPENAI_API_KEY` (and optionally `OMNI_MODEL`) to enable portable planning.
- Omni enhanced mode: OmniScientist/HelixForge reads `metadata.helixforge`,
  calls `engine.py`, persists task/workflow state, stores the PPTX as an
  artifact, records provenance (sources/claims/runs), exposes the review
  checkpoint through the durable runtime, and can pass the deck to later steps.

## Portable research provenance

This skill must remain portable across OmniScientist, Claude Code, Codex, and
OpenClaw.

- In OmniScientist, if tools such as `cite_source`, `record_claim`,
  `add_evidence`, `record_hypothesis`, or `log_run` are available, use them and
  include returned ids in `research.source_ids`, `research.claim_ids`,
  `research.evidence_ids`, and/or `research.run_id`.
- In other runtimes, do not fail because those tools are absent. Keep available
  source metadata, the render command, and local artifact paths with the result;
  the portable runner does not claim durable Omni provenance records.
- Never invent provenance ids. Use real tool‑returned ids; otherwise cite
  human‑readable source metadata such as PDF path, arXiv id, title, run command,
  and artifact path.
