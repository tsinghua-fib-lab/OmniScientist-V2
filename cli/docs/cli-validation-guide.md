# CLI Validation Guide

This guide gives copy-pasteable validation scenarios for OmniScientist's command-line
surface. Every scenario has two trigger forms:

- **Shell command**: run from a terminal with `omni ...`.
- **Interactive CLI**: start `omni -P skill-verify`, then type the shown prompt or slash command.

Use a dedicated project so verification artifacts do not mix with real work:

```bash
omni -P skill-verify status
omni -P skill-verify skills list --no-pager
omni -P skill-verify skills examples
```

> Real multi-step intent recognition and workflow planning require a real model. The `mock`
> provider is useful for deterministic tool tests, but it is not a full planner.
>
> `omni exec` in these scenarios is workspace-auto: in-workspace writes and sandboxed
> `bash` / `run_compute` run without a TTY prompt. Use `omni exec --ask` when you want
> to exercise the approval loop.

## Core Validation Loop

After each natural-language workflow prompt, inspect the task and its structured state:

```bash
omni -P skill-verify task list
omni -P skill-verify task show <task_id>
omni -P skill-verify task show <task_id> --json
```

Inside the interactive CLI:

```text
/task
/task show <id>
/task show <id> --json
/task attach <id>
```

Expected result:

- `task show <id>` renders a readable step/artifact view.
- `task show <id> --json` preserves the full machine-readable trace.
- Bash events distinguish transport `status` from
  `output_json.command_status`/`exit_code`; consumers never parse `[exit=N]` text.
- Completed steps keep their structured results even if a later step fails.
- Recoverable failures include `status`, `error`, `summary`, and partial outputs.

## Objective Provider Binding and Settlement

This suite validates the boundary between model planning and execution without a central semantic
preference interpreter. The host decides only objective legality, provenance, policy, and replay
safety. The model chooses semantics from exact provider schemas and judges its own results; the host
settles the turn against what the durable record shows.

### Runtime controls

Objective JSON Schema and resolver-evidence validation are always fail-closed, and neither is
configurable.

### Revision truth and exact provider binding

Run an ordinary workflow with verbose progress:

```bash
omni -P skill-verify chat --verbose \
  "Fetch arXiv 1706.03762, draw a RAG figure with query, retriever, reranker, and LLM, then write a short report."
omni -P skill-verify task show <task_id> --json
```

Expected event order:

```text
plan.model.proposed (or deterministic boundary selection)
plan.revision.proposed
plan.revision.candidate (zero or more)
plan.revision.accepted (exactly one final authority)
plan.validated
plan.execution.bound
plan/tool/workflow execution events
task.<succeeded|degraded|failed|needs_input|cancelled|interrupted>
```

Check the JSON rather than display text:

- Every accepted revision has a monotonic revision number, content hash, parent hash, finding ids,
  and deterministic diff. Only the final accepted revision is dispatched.
- Every workflow consumer has one exact provider binding: consumer/step id, capability, provider
  name, source, version, and contract hash. Same-named providers from different sources are not
  interchangeable.
- `plan.validated.output_json.revision_hash`,
  `plan.execution.bound.output_json.revision_hash`, the final accepted content hash, and the
  persisted plan revision hash are identical.
- A final bind rematerializes the provider and resolver evidence. Source/version/contract drift
  fails closed instead of silently acquiring new code.

Repeat in plan mode:

```bash
omni -P skill-verify chat --mode plan \
  "Fetch arXiv 1706.03762 and produce a cited summary."
omni -P skill-verify task show <task_id> --json
omni -P skill-verify task approve <task_id>
```

Expected result: approval binds the exact reviewed plan, catalog, provider contracts, and
prospective sensitive grants. Any persisted-plan, catalog, contract, or grant drift fails closed.

Automated checks:

```bash
.venv/bin/pytest -q \
  cli/tests/agent/test_plan_revision_contract.py \
  cli/tests/agent/test_provider_binding_contract.py \
  cli/tests/runtime/test_plan_revision_audit.py
```

### Objective schema and ResolverEvidence

The planner receives the complete input schema for each shortlisted exact provider, including
nested properties, enum values, descriptions, and `x-omni` guidance. The accepted candidate is
then compiled against that same schema. Unknown properties, missing values, nested type/enum/
format errors, and invalid additional properties are rejected before execution.

A resolver-owned field has a separate proof obligation derived from its exact provider contract.
An explicit normalized id may be `user_exact`; an existing path may be `local_exists`; an id
derived from a title needs matching `grounded_search` evidence. A plausible format alone is not
proof, and the model cannot patch or manufacture resolver evidence.

```bash
.venv/bin/pytest -q \
  cli/tests/agent/test_objective_provider_contract.py \
  cli/tests/agent/test_contract_driven_boundaries.py \
  cli/tests/agent/test_resolver_evidence.py
```

Required assertions:

- findings identify the exact provider source/contract and a precise JSON Pointer;
- nested enum findings include allowed and actual values;
- provider-neutral scalar aliases still compile, while exact full contracts reject unknown keys;
- resolver evidence is scoped to provider binding + field + value and is rechecked before dispatch;
- stale, mismatched, missing, or insufficient evidence blocks provider execution.

There is no pre-execution repair rung: a rejected plan goes to the deterministic recovery ladder
(safety hard stop, `needs_input` for one user-suppliable field, otherwise the ReAct floor), never
back to the model for a patch. A schema-valid semantic preference never opens a finding at all.

### Settlement, not deliverable grading

The host does not grade the produced output. `runtime/settlement.py` reads durable rows and decides
a terminal status; the model, which can see the tool results, owns everything about quality.

Settlement rules are intentionally narrow:

- submitted subtasks or workflow runs still active ⇒ `pending`, and the task stays running;
- on an outbound channel, no `presentation.sent|degraded|failed` event yet ⇒ `pending`;
- a `VerificationPlan.required_events` name with no matching event is an unfounded claim ⇒ `failed`;
- lost, cancelled, interrupted, or unaccounted-for children ⇒ `failed`; `degraded` children ⇒
  `degraded`;
- a budget-bounded stop on `execution.finished`/`react.finished` ⇒ `degraded`;
- after the turn ends, a `VerificationPlan.required_outputs` name with no matching artifact on
  *this* task ⇒ `degraded` (`undelivered_outputs`). A sidecar `.dot`/`.json` does not satisfy
  `artifact.figure`.

A skill engine may still put a `deliverable_assessment` block in its own result. It is provider
self-reporting for the model to read; nothing in the host matches it against a contract.

Check `task show <id> --json`: the terminal outcome is the `task.<status>` event plus the task row
status. There are no `verification.*` events.

```bash
.venv/bin/pytest -q \
  cli/tests/agent/test_turn_completion.py \
  cli/tests/agent/test_trust_closure.py \
  cli/tests/agent/test_workflow_runtime.py
```

### Model-owned plan checklist

`update_plan` is the model's own step list, not a host contract. Run a multi-step request with
`--verbose` and watch the `☐ / ▸ / ✔` checklist appear and update in place; the model replaces it
wholesale each call, and a single-step request should produce no checklist at all.

```bash
.venv/bin/pytest -q cli/tests/agent/test_update_plan_tool.py
```

### Legacy persisted-plan compatibility

Old task snapshots may contain the retired `requested_constraints` and `binding_records` arrays.
Validate by loading an old fixture and inspecting `task show --json`: the historical bytes/hash
remain readable, but the arrays are opaque. They must not create resolver evidence, provider
identity, recovery behavior, or a settled status. New plan-schema-v2 snapshots keep the
compatibility arrays empty and never populate them.

### Execution gateway and replay safety

```bash
.venv/bin/pytest -q \
  cli/tests/core/test_tool_contracts.py \
  cli/tests/runtime/test_execution_gateway_contract.py \
  cli/tests/agent/test_replay_safety_contract.py
```

Expected result: unknown tools and invalid schemas fail before handler start; policy, hooks,
approval, resource locks, and output validation share one gateway; invalid output schemas cannot
reach provider code; and non-replay-safe operations execute once. Replay authority is host metadata
and is absent from model-facing schemas.

### Codex-style busy input and low-noise display

Start a long-running turn in the interactive CLI. While it is active:

1. During a ReAct turn, press Enter to steer the active turn.
2. Press Tab or enter `/queue <prompt>` to queue exactly one next turn.
3. Press Esc or enter `/stop`; a repeat in the same turn force-cancels it.
4. Race a steer with completion; it must apply to the current task or queue once, never both/neither.
5. During a deterministic workflow, Enter becomes one next-turn item; detached steering creates no
   orphan control.

Normal verbosity shows planning, executing, and the terminal result. A finding a recovery rung
absorbed must not expose internal finding codes or a failure-looking warning. Use verbose
mode or task JSON for revision and finding details.

```bash
.venv/bin/pytest -q \
  cli/tests/cli/test_codex_turn_input_contract.py \
  cli/tests/cli/test_reactive_plan_display_contract.py
```

### Focused offline acceptance

Run the migrated end-to-end corpus and focused contracts:

```bash
.venv/bin/pytest -q \
  cli/tests/eval/test_objective_provider_quality_offline_corpus.py \
  cli/tests/agent/test_objective_provider_contract.py \
  cli/tests/agent/test_provider_binding_contract.py \
  cli/tests/agent/test_resolver_evidence.py \
  cli/tests/agent/test_turn_completion.py
```

The corpus must include non-vacuous cases for exact-source collisions, nested objective errors,
ungrounded resolver values, schema-valid semantic preferences that must *not* block execution, and
legacy read-only plans. It also requires accepted/persisted/execution-bound hash equality and zero
duplicate non-replay-safe executions.

## Under-Specified Workflow Check

This prompt should create the user-request Task immediately, ask a follow-up, and avoid creating a
WorkflowRun or Skill Execution until the missing context is supplied:

```bash
omni -P skill-verify exec "Prepare a submission section with search, fetch, ideation, editable figure, complete slides, and writing."
omni -P skill-verify task list
```

Inside the REPL, type the same prompt and then `/task`.

Expected result:

- The assistant asks for the research topic, target paper, figure/deck type, and writing goal.
- The first turn is visible in `/task` as `needs_input`, but has no WorkflowRun or Skill Execution.
- After you provide the missing details, the workflow should plan only active providers from
  `arxiv-fetch`, `openalex-search`, `scientific-figure`, `livefigure`,
  `research-ideation`, and `research-pptx`, plus native synthesis when requested.
- If a planned step is missing a required schema field, `run_workflow` returns `needs_input` instead
  of creating a half-failed task.

## 1 to 7 Workflow Capability Triggers

These examples are research-shaped prompts, not artificial skill names. The count includes real
skills plus native workflow deliverables such as `draft.section`. They are also exposed by:

```bash
omni skills examples
```

And in the REPL:

```text
/skills examples
```

| Capability count | Shell trigger | Interactive CLI trigger | Expected workflow steps | Validation |
|---:|---|---|---|---|
| 1 | `omni -P skill-verify exec "Fetch the abstract of arXiv 1706.03762."` | Type the same prompt after `omni -P skill-verify` | `paper.fetch.arxiv` | Output mentions `Attention Is All You Need`. |
| 2 | `omni -P skill-verify exec "Search OpenAlex for RAG factuality papers and propose research directions."` | Same prompt in REPL | `literature.search`, `research.ideation` | OpenAlex owns search; research-ideation owns ideation. |
| 3 | `omni -P skill-verify exec "Search RAG literature, propose research directions, and draw a lightweight architecture figure."` | Same prompt in REPL | `literature.search`, `research.ideation`, `artifact.figure` | Figure artifacts are DOT/SVG/PNG. |
| 4 | `omni -P skill-verify exec "Fetch arXiv 1706.03762, propose follow-ups, draw a lightweight figure, and write a section."` | Same prompt in REPL | fetch, ideation, `artifact.figure`, native synthesis | Verify metadata, ideas, figure, and draft. |
| 5 | `omni -P skill-verify exec "Search RAG papers, ideate, make one editable PPT figure, generate a complete deck, and synthesize notes."` | Same prompt in REPL | search, ideation, `figure.editable.pptx`, `slides.generate`, synthesis | Verify one-slide and full-deck outputs remain distinct. |
| 6 | `omni -P skill-verify exec "Search, fetch arXiv 1706.03762, ideate, draw a lightweight figure, generate a complete deck, and synthesize a draft."` | Same prompt in REPL | five skill capabilities plus synthesis | Inspect sources, artifacts, and synthesis. |
| 7 | `omni -P skill-verify exec "Run the full RAG workflow with search, fetch, ideation, lightweight and editable figures, a complete deck, and notes."` | Same prompt in REPL | all active capabilities plus native synthesis | Confirm the validated plan, partial-state behavior, and artifacts. |

## Long-Running and Detached Tasks

Use `--detach` to verify durable task persistence:

```bash
omni -P skill-verify exec --detach "Prepare a Transformer/RAG report with search, fetch, ideation, an editable figure, a full deck, and notes."
omni -P skill-verify task list
omni -P skill-verify task drain
omni -P skill-verify task show <task_id>
```

Interactive CLI equivalent:

```text
Prepare a Transformer/RAG report with search, fetch, ideation, an editable figure, a full deck, and notes.
/task
/task watch   # press q to return to the CLI
/task show <id>
/task attach <id>
```

Expected result:

- Task status moves through `pending` / `running` / `succeeded` or `failed`.
- Workflow step state is durable in SQLite.
- Attached results become part of the current session context.

## Channel Login and Daemon Checks

These checks validate local config, QR/pairing generation, credential handling, and daemon
management. Full IM round-trips still require real platform apps/gateways and should be run as a
manual platform test. They run the same on macOS, Linux, and Windows. Real usage needs no storage
flag; `--credential-store file` is pinned below only so a validation run never writes to the
tester's Keychain. On Windows run the same commands in PowerShell (`$env:FEISHU_APP_SECRET`).

```bash
omni -P skill-verify channel add feishu
omni -P skill-verify channel login feishu \
  --app-id cli_xxx \
  --app-secret "$FEISHU_APP_SECRET" \
  --credential-store file \
  --no-wait \
  --no-qr
omni -P skill-verify channel test feishu
omni -P skill-verify serve start
omni -P skill-verify serve status
omni -P skill-verify serve stop
```

Expected result:

- `channel login` writes `<OMNI_HOME>/channels/feishu.toml`, stores only a credential reference in the
  channel config, and creates a short-lived `/pair <code>`.
- `channel test` confirms required config fields and SDK availability; it is not a live platform
  message round-trip.
- `serve start/status/stop` manages the single home service for this `OMNI_HOME` (not a
  per-workspace daemon) and writes logs under `<OMNI_HOME>/logs/`;
  enabled channel adapters are reconciled dynamically from channel config.

## Research Object Model Checks

Validate OmniScientist's research-specific object model:

```bash
omni -P skill-verify source list
omni -P skill-verify claim list
omni -P skill-verify evidence list <claim_id>
omni -P skill-verify hypo list
omni -P skill-verify run list
omni -P skill-verify verify
```

Interactive equivalents:

```text
/source list
/claim list
/evidence list <claim_id>
/hypo list
/run list
/verify
```

Expected result:

- Sources include arXiv ids, DOIs, URLs, or connector records.
- Claims show evidence counts; `0` means unsupported.
- Evidence edges record `supports`, `contradicts`, or `mentions`.
- `verify` flags unsupported, contradicted, or over-confident claims without calling the model.
- Runs contain command, seed/env lock where available, metrics, and output artifact URIs.

## Grounded Literature QA

Build or import a corpus, then ask grounded questions:

```bash
omni -P skill-verify exec '$openalex-search Transformer attention architecture'
omni -P skill-verify lit "What is the Transformer's encoder-decoder attention structure?" --verify
```

Interactive:

```text
$openalex-search Transformer attention architecture
/lit "What is the Transformer's encoder-decoder attention structure?" --verify
```

Expected result:

- Answers use `[S#]` citations when the corpus has relevant chunks.
- Recorded claims can be inspected with `claim list`.
- `verify` reports whether the answer's claims are grounded.

## Memory and Session Continuity

Add pinned project memory:

```bash
omni -P skill-verify memory add "This project evaluates RAG rerankers for factual consistency." --pin
omni -P skill-verify memory search reranker
omni -P skill-verify session list
omni -P skill-verify resume --last
```

Interactive:

```text
/memory reranker
/session list
/resume --last
```

Expected result:

- `memory search` returns the pinned memory.
- Later turns can use the memory as project context.
- `resume --last` or `/resume --last` continues the latest workspace session.
- `replay <session>` shows messages and tool traces.

## Workspace and Storage Checks

```bash
omni -P skill-verify status
omni -P skill-verify project info
omni -P skill-verify task list --all
omni -P skill-verify cite export -f bibtex -o refs.bib
```

Expected result:

- `status` shows the workspace path, SQLite store, artifact directory, and daemon state.
- `task list --all` aggregates tasks across known workspaces.
- Exported citations are generated from the project library.

## Portable Runner Checks

These validate the no-Omni path used by Claude Code, Codex, and OpenClaw users:

```bash
cd skills/arxiv-fetch
python3 scripts/run.py --json '{"identifier":"1706.03762"}'

cd ../scientific-figure
python3 scripts/run.py --json-file payload.json

cd ../livefigure
python3 scripts/run.py --self-test

cd ../research-pptx
python3 scripts/run.py --self-test
```

On Windows PowerShell, write the payload as UTF-8 and pass `--json-file`. Do not
use `--json '{"..."}'` — PowerShell strips the inner quotes, and a legacy
console code page rewrites Chinese `output_dir` values to `????`.

```powershell
python3 scripts/run.py --json-file payload.json
Get-Content -Raw -Encoding utf8 payload.json | python3 scripts/run.py
```

Expected result:

- Runners print structured JSON.
- They do not import Omni.
- Successful figure/deck runners write local artifacts.
- Network failures or missing service requirements return structured `status: "error"` results.

## Final Acceptance Checklist

- The objective-provider offline corpus exercises exact schema, ResolverEvidence, exact provider
  sealing, and legacy-read compatibility without network access.
- Changed executable Python lines under `cli/src/omni/**/*.py` meet the independent 80% coverage
  gate with resolved baseline/candidate provenance and a complete uploaded report.
- Accepted, persisted, approval-bound, and execution-bound plan hashes agree.
- Every consumer is bound to the exact provider source/version/contract hash, and the final binding
  must match it.
- Planning adds no repair call; a rejected plan is handled by the deterministic recovery ladder, and
  resolver facts are never model-repairable.
- A turn that claims a side effect without the matching event settles `failed`; an active child
  keeps the task running instead of publishing an unearned status.
- Gateway contract and replay-safety tests prove malformed output schemas cannot reach
  authorization/hooks/provider execution, external references are never fetched, and
  non-replay-safe operations are never duplicated.
- Provider root/nested/renewal/latest authority, runtime closure, recovery continuity, and retry
  idempotency all meet their non-zero case floors with zero errors.
- Legacy persisted arrays are readable only; they create no evidence, binding, recovery authority,
  or settled status, and new plan-schema-v2 snapshots keep them empty.
- Revocable gateway leases prevent copied contexts from retaining authority; delegated authority is
  exact-target and one-shot.
- Busy Enter/Tab/Esc behavior and 10,000 independent finish/insert interleavings have no lost or
  duplicated input.
- Real SQLite controls prove exclusive terminal requeue ownership, live-PID lease protection, dead-
  PID immediate recovery, and legacy lease recovery; crash recovery is explicitly at-least-once,
  with no replay after durable acknowledgement.
- Failing-turn queue/steer fallback is exactly-once; deterministic workflows do not falsely apply
  steering; detached deterministic steering is rejected without creating a control; transient
  acknowledgement recovery neither loses nor duplicates delivered steering; generic skill wrappers
  preserve concrete approval, output contracts, and explicit domain-failure status; provider data
  cannot forge host rejection authority; native synthesis cannot bypass its typed output contract;
  durable domain failures cannot become successful subtasks.
- Final steering boundaries are monotonic: late audit/cost/plan writes cannot reopen a sealed task,
  and the SQL insert gate leaves zero orphan controls.
- Successful self-heal leaves no normal-mode warning; full revision/finding details remain auditable.
- `omni skills examples` shows 1 to 7 workflow capability prompts and validation commands.
- `/skills examples` shows the same content inside the interactive CLI.
- `task show <id>` defaults to a readable view; `--json` exposes full state.
- Workflows preserve partial structured results after failures.
- Research objects are visible via `source`, `claim`, `evidence`, `hypo`, `run`, and `verify`.
- Memory/session commands demonstrate cross-turn continuity.
- Portable runners prove copy-only external use; Omni enhanced mode proves durable workflow use.
