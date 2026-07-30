---
name: scientist-kg-distiller
description: Build, resume, audit, improve, or atomically install a traceable scientist-personality knowledge graph from a scientist name or academic corpus. Use when Codex must identify a scientist, collect papers/talks/code, normalize SourceObjects, extract L1 evidence, induce seven L2 cognitive patterns, abstract three L3 stances, extract a verbatim P04 tone node, complete graph edges, produce and validate a `.kg.json`, or publish the canonical KG into SoulAgent's active scanner root.
license: Apache-2.0
allowed-tools: [read_file, bash, write_file, web_fetch, package_artifact, attach_provenance]
metadata:
  helixforge:
    version: "1.0"
    dependencies: ["python>=3.11", "jsonschema>=4", "openai>=2", "pypdf>=6"]
    tier: research
    research_contract: portable_provenance_v1
    role: task
    capabilities:
      - kg.distill
      - kg.distill.build
      - kg.distill.resume
      - kg.distill.audit
      - kg.distill.capsule
      - persona.kg.encode
    kind: prompt_only
    delivery_mode: async_task
    execution:
      max_iterations: 24
      max_tool_calls: 48
      # Bounded by the turn / workflow envelope this runs inside; asking for more
      # than the envelope only produces a number the run can never reach.
      max_seconds: 1800
    workflow:
      failure_policy: continue_with_partial
      allow_failed_dependencies: true
    input_schema:
      type: object
      properties:
        input: {type: string, description: "scientist name, source corpus, or distillation request"}
        scientist: {type: string, description: "scientist name when starting from identity discovery"}
        scientist_id: {type: string, description: "verified scientist id for a resumed run"}
        project_root: {type: string, description: "workspace for checkpoints and result artifacts"}
        install_root: {type: string, description: "optional exact SoulAgent scanner root for atomic KG installation"}
        step: {type: string, description: "optional identity, collect, ingest, evidence, l2, l3, edges, kg, or capsule stage"}
        resume: {type: boolean, description: "reuse hash-validated checkpoints"}
      required: [input]
    output_schema:
      type: object
      properties:
        status: {type: string, enum: ["ok", "partial", "error"]}
        outcome: {type: object}
        summary: {type: string}
        warning: {type: string}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        artifacts: {type: array}
        sources: {type: array}
        research: {type: object}
        error: {type: string}
        error_info: {type: object}
      required: ["status"]
    trigger:
      phrases: [scientist distillation, scientist KG, academic persona KG, 科学家蒸馏, 学术人格知识图谱]
      when_to_use: "Use to build or audit a traceable scientist research-judgment KG from a named scientist or an academic corpus."
    notification:
      display_label: "Scientist KG distillation"
---

# Scientist KG Distiller

Construct a scientist KG through two controlled phases: material ingestion and
KG extraction/encoding. Treat source traceability and identity accuracy as
hard requirements.

## Required references

Read both files before changing schemas, prompts, node semantics, edge
semantics, or pipeline ordering:

- `references/设计稿02-学术人格KG设计.md`: authoritative KG structure and final
  JSON contract.
- `references/设计稿03-蒸馏器与KG编码器.md`: authoritative two-phase workflow.

Do not silently change the fixed seven L2 categories, three L3 stance
questions, verbatim P04 tone node, or five edge types.

## Operating workflow

1. Resolve and verify the scientist identity. Never select a same-name candidate
   from name similarity alone. Persist occupations, research fields, education
   history, employment trajectory, and their biography sources when available.
2. Discover and deduplicate papers, talks, interviews, code, and official pages.
3. Fetch full text, preserve provenance, detect author role, and write audited
   SourceObjects.
4. Extract source-anchored L1 evidence cards. Reject non-verbatim excerpts.
5. Assign every accepted L1 card to exactly one of C01-C07, then induce the
   seven L2 descriptions as seven independent parallel tasks.
6. Abstract P01-P03 as three independent parallel tasks. Each task reads all
   seven L2 descriptions and all accepted L1 cards, but selects only the
   question-relevant patterns and decisive evidence for its explanation. Never
   use the seven patterns as a checklist or reuse the same overview across P01,
   P02, and P03.
   P03 additionally reads the verified identity profile and states who the
   scientist is, their education, employment trajectory, and research fields.
   Treat the resulting L3 stances as automatic KG output.
7. Extract P04 after P01-P03. Read paper introductions and talk transcripts
   directly from SourceObjects, copy 3-5 first-person or evaluative sentences
   verbatim, and reject every candidate that is not an exact source substring.
   During comparison only, treat runs of whitespace such as spaces, tabs,
   `LF`, and `CRLF` as equivalent; persist the matched SourceObject slice with
   its original whitespace rather than the model-normalized string.
   P04 has only `tone_exemplars`; it has no stance, L2 provenance, exemplar L1,
   or graph edges.
8. Generate automatic supports/summarizes edges and evidence-backed
   reinforces/enables/tension edges.
9. Assemble the final KG and reject dangling references, duplicate IDs, invalid
   counts, and L1 nodes without exactly one parent.
10. After KG validation, optionally install `result/<scientist_id>/kg/` into the
   exact SoulAgent scanner root as `<install_root>/<scientist_id>/`. Hold an
   installation lock, validate the staged copy, use an atomic rename, and never
   overwrite an existing local persona directory.
11. Export a human-facing soul capsule as a multi-file, progressively disclosed
   visualization. Keep clean deliverables under `result/<scientist_id>/` and
   pipeline checkpoints outside that directory. Attempt portrait collection
   from verified profile/Wikidata sources; preserve a visible crawl audit.
12. Save checkpoints and audits at every phase. Resume valid artifacts rather
   than silently rebuilding them.

L1 source-chunk responses, L2 classification/induction responses, the three
independent L3 stance responses, and P04 source-level tone responses are
persisted with input hashes, so completed model calls survive a later request
failure.

Agent-facing L2/L3 prose must be self-explanatory. It must not expose `L1`,
`L2`, `L3`, `C01`-`C07`, or `l1_`/`l2_`/`l3_` identifiers. Keep identifiers
only in structured provenance fields. The pipeline translates resolvable model
references into paper titles, concrete observations, and the seven Chinese
“怎样……” labels, then rejects any opaque reference that remains.

## Execution

Start from a scientist name:

```powershell
python <skill-dir>/scripts/kg_distiller/main.py distill `
  --scientist "Kaiming He" `
  --project-root <output-workspace> `
  --install-root <soulagent-scanner-root>
```

Use `--field` or `--institution` when the name is common. If identity remains
ambiguous, stop and present `identity_candidates.json`; continue only after the
user selects one:

```powershell
python <skill-dir>/scripts/kg_distiller/main.py distill `
  --scientist "<name>" `
  --identity-candidate "<candidate-id>" `
  --project-root <output-workspace>
```

For an existing verified profile, use `--scientist-id`. Use
`--step identity|collect|ingest|evidence|l2|l3|edges|kg|capsule` for one stage and
`--resume` for hash-validated reuse. Evidence extraction also checkpoints every
source-chunk request. The three L2 horizontal-edge families run independently;
set `KG_DISTILLER_EDGE_CONCURRENCY` to control their parallelism (default `3`).

Final deliverables use this structure:

```text
result/<scientist_id>/
├── manifest.json
├── kg.json
├── kg/
│   ├── manifest.json
│   ├── meta.json
│   ├── identity.json
│   ├── l3-stances.json
│   ├── l2-patterns.json
│   ├── edges.json
│   └── l1-evidence/
│       ├── index.json
│       └── c01.jsonl ... c07.jsonl
├── README.md
└── capsule/
    ├── index.html
    ├── manifest.json
    ├── assets/
    ├── css/
    ├── js/
    └── data/
        └── patterns/c01.js ... c07.js
```

Treat `kg/` as the canonical progressively readable store. Generate `kg.json`
from the same in-memory graph as a portable compatibility bundle. Validate file
hashes, partition counts, graph references, and exact bundle/store equivalence
before reporting the KG step complete.

When `--install-root` is supplied, treat it as the scanner root itself, not the
scientist directory. Install only the canonical `kg/` contents at
`<install-root>/<scientist_id>/`. The default SoulAgent scanner is
`~/.omni/scientist-kg/` unless a project-local compatibility directory or an
explicit `kg_root` is active.

The capsule must open directly without a build step. Keep overview, tone,
values, relations, and each detailed cognitive pattern in separate files. Its primary
view is a knowledge graph, not an attribute dashboard: start at the emergent
personality type, connect it to the three L3 cores, then use real `summarizes`
edges to reach the seven L2 patterns. Render P04 as a distinct selectable
tone node with its verbatim exemplars, but do not draw edges between P04 and
any L2 node. Reveal representative paper evidence as
leaf nodes only when the user expands a selected pattern. Human-facing labels
must stay in natural Chinese and hide internal layer/category/node IDs.

For DeepSeek, use its OpenAI-compatible endpoint with
`OPENAI_BASE_URL=https://api.deepseek.com` and `--model deepseek-v4-flash`.
Set `KG_DISTILLER_L1_CONCURRENCY` to control L1 parallel requests (default 8).
Set `KG_DISTILLER_L2_CONCURRENCY` for the seven L2 induction tasks (default 7)
and `KG_DISTILLER_L3_CONCURRENCY` for the three L3 tasks (default 3).
Set `KG_DISTILLER_TONE_CONCURRENCY` for P04 source-level extraction (default 8).
Keep API keys only in the current process environment. Non-verbatim L1 cards
and malformed card responses are recorded in the extraction audit and excluded
from the KG.

Collect up to 200 deduplicated, authority-bound materials by default. Use
`--max-sources` to set a larger or smaller explicit corpus budget. Full-text
ingestion persists each source and retries prior metadata-only failures on the
next run.

Identity confirmation is the only required human gate. Once the identity is
verified or explicitly selected after an ambiguity report, L1/L2, L3, and
horizontal-edge outputs are automatic.

## Completion checks

Validate the Skill folder after metadata or structure changes:

```powershell
python <skill-creator>/scripts/quick_validate.py <skill-dir>
```

Treat a real run as complete when identity is verified, the KG validates, and
every L1 excerpt is traceable. P03 must contain verified biographical context,
P04 must contain 3-5 source-verbatim sentences with no L2 links, and all
agent-facing L2/L3 prose must pass the opaque-reference check.

## OmniScientist progress & completion contract

This is a `prompt_only`, `async_task` Skill: it has no in-process progress
callback. Under OmniScientist its live progress is the bundled runner's
per-`--step` invocations, and its completion is read from the returned result —
not from an engine emitting typed stage events. Keep to this vocabulary so the
CLI renders it in the same shared language as the engine-backed Skills:

- Stages. The nine `--step` tokens are the Skill's stage vocabulary and the CLI
  normalizes them to canonical ids: `identity → kg.identity`, `collect →
  kg.collect`, `ingest → kg.ingest`, `evidence → kg.evidence`, `l2 → kg.l2`,
  `l3 → kg.l3`, `edges → kg.edges`, `kg → kg.validate`, `capsule → kg.capsule`.
  Run a phase under its documented token so that mapping stays exact.
- Milestone. The terminal `capsule` phase is the run's single durable
  milestone, surfaced as **"Soul capsule generated"**. Do not report it before
  the KG validates and the capsule opens without a build step.
- Deliverable. Report completion through the result `summary`, `outcome.code`,
  and `artifacts` (the canonical `kg/` store and the `capsule/` visualization);
  the CLI's closing deliverable block reads those fields. Prefer a one-line
  `summary` stating the scientist and that the capsule + KG are ready.
- Confirmation. Identity verification is the one human gate (see above). On an
  ambiguity report, stop and surface `identity_candidates.json`; do not proceed
  until the operator selects a candidate.

A thin `python_engine` wrapper was considered and rejected: the pipeline is a
resumable, checkpointed, portable runner, and promoting it only to synthesize
stage events would duplicate that runner while losing its copy-only portability.
The contract above delivers the same shared vocabulary without that cost.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Copy-only mode: copy this folder into a Claude Code, Codex, or OpenClaw Skill
  directory. The host reads `SKILL.md` and invokes the bundled Python pipeline.
- Portable runner mode: this prompt-only Skill uses
  `scripts/kg_distiller/main.py` as its portable command entry point. It writes
  checkpoints and validated results under the caller-selected project root.
- Omni enhanced mode: OmniScientist bounds the Skill run, exposes only the
  declared tools, persists task state, and can package the resulting KG and
  provenance as artifacts.

See `README.md` for installation, testing, environment variables, output layout,
and Host-specific invocation examples.
