# Architecture (as-built)

For executable agent and skill validation scenarios, see
[`agent-validation-guide.md`](agent-validation-guide.md). For command-line and REPL validation,
see [`cli-validation-guide.md`](cli-validation-guide.md). For the executable runtime contract
(modes, hooks, steer, recovery, isolation, and quality evaluation), see
[`agent-runtime-harness.md`](agent-runtime-harness.md).

OmniScientist is a layered, local-first distillation of the HelixForge research-agent OS.
This document describes the code as actually implemented under `src/omni/`.

## Layers & packages

```
src/omni/
├── cli/                 # Typer CLI: command tree, REPL, render, init/doctor/serve
│   └── commands/        #   config/skills/mcp/project/memory/task/session/profile/channel/cite +
│                        #   status/resume/exec/replay/serve/web/init/doctor/update +
│                        #   research: lit/verify/bench/hypo/claim/evidence/run/source
├── agent/               # OmniAgent orchestrator — turn execution, figure fill, revision router
├── core/                # Agent core
│   ├── llm/             #   LLMClient + providers (mock, openai_compatible)
│   ├── react_agent.py   #   bounded ReAct tool-loop
│   └── system_prompt.py #   system prompt assembly (+ research-workflow block & live research brief)
├── skills_runtime/      # SKILL.md parsing, registry, 4-form executor, capability resolver, skill tools
│   └── builtin_tools/   #   read/write/edit/grep/glob/bash/web_fetch (CC-compatible) + research tools
├── research/            # arXiv client · ROM · corpus · domain packs · rich artifacts
│                        #   · literature/trial connectors · tools · verify · bench
├── memory/              # 5-layer (M1–M5) file/SQLite memory + recall + notebook
├── runtime/             # durable runs/tasks + hooks + isolation + DAG/checkpoints + presentation
├── eval/                # deterministic behavior, coverage, and research-quality evaluation
├── web/                 # loopback HTTP surface (Starlette): directory RPC, per-store agent cache, SSE
├── compat/              # MCP server bridge, MCP client, Codex/Claude integration writers
├── channels/            # Channel abstraction + CLI + optional WeChat/Feishu/DingTalk adapters
├── storage/             # SQLAlchemy async models + SQLite engine + file artifact store
├── config/              # layered TOML settings + path resolution
└── data/                # packaged resources (built-in SKILL.md packages live in top-level skills/)
```

> Built-in skill *content* lives in the top-level [`skills/`](../../skills/) directory (bundled into
> the wheel), not under `src/omni/`. A `python_engine` skill keeps its `engine.py` inside its own
> skill folder and reuses only omni's small public runtime (`omni.research` and, where needed,
> `omni.memory.library`); there is no `skills_builtin/` package.

## Request flow (one turn)

```mermaid
flowchart TD
  User["CLI / REPL / Feishu / WeChat / DingTalk / MCP / Web"] --> Run["Create AgentRun + ack"]
  Run --> Context["Session + memory + ROM + domain packs + skill registry"]
  Context --> Plan["IntentPlan + skill selection reasons"]
  Plan --> Validate["PlanValidator + recovery ladder"]
  Validate --> Input["needs_input"]
  Validate --> Contract["ExecutionContract + ToolPolicy + lifecycle hooks"]
  Contract --> Execute["PlanExecutor"]
  Execute --> Inline["inline synthesis / bounded ReAct"]
  Execute --> SkillExec["Skill Execution attempt"]
  Execute --> Artifact["artifact transaction"]
  Inline --> Tools["model tool calls: run_skill / run_workflow / spawn_subagents / update_plan"]
  Tools --> DAG["WorkflowRun + stable WorkflowSteps"]
  Tools --> Delegated["Child Task (delegated agent request)"]
  DAG --> Checkpoint["step result + checkpoint + partial state"]
  SkillExec --> Checkpoint
  Delegated --> Checkpoint
  Checkpoint --> Settle["settlement: children · claimed events · required outputs"]
  Inline --> Settle
  Artifact --> Settle
  Settle --> Present["shared TurnPresentation"]
  Present --> Render["CLI table / IM markdown-card-file fallback / Web SSE"]
  Run -.-> Events["append-only planner + tool + hook + child + progress + task + delivery events"]
  Plan -.-> Events
  Execute -.-> Events
  Present -.-> Events
  Events --> Inspect["task show / subtask / step / retry / resume"]
```

`OmniAgent.handle_turn()` (see `agent/orchestrator.py`):

1. ensure a `Session` row, persist the user message, create an `AgentRun`, and emit the channel ack
   before planning so `/task` immediately shows `running`
2. recall relevant memories (`memory.recall`), build a live **research brief** (open hypotheses /
   unsupported-claim count from the ROM) + intent-matched skill suggestions + the compact loaded
   skill catalog + enabled domain-pack guidance, and assemble the system prompt
3. build and persist the `IntentPlan` — route, context policy, tool policy, task contract, and
   `VerificationPlan`; validate, then route a recoverable finding to `needs_input` or a bounded
   ReAct handoff (only a safety finding stops the turn)
4. build the policy-filtered tool surface: builtin tools (incl. the **research tools** `record_hypothesis` /
   `record_claim` / `cite_source` / `add_evidence` / `search_corpus` / `log_run`, and the model's own
   `update_plan` checklist) + engine/exec sync skills + `find_skill` / `run_skill` /
   `run_workflow` (+ any external MCP tools)
5. execute through `PlanExecutor` — a schedule registration, a capability runner, a memory write, or
   (for everything else) the bounded ReAct turn, from which the model itself calls `run_skill`,
   `run_workflow`, and `spawn_subagents`; lifecycle hooks and durable steer/cancel controls apply at
   their safe boundaries
6. persist step results/checkpoints, assistant output, artifacts, provenance, and session memory;
   retry starts a linked child attempt while resume reuses a persisted checkpoint. If the task
   still owes `artifact.figure` (and on resume, only a figure and/or writing), the host may fill
   those deliverables before a full retry
7. settle the task against its durable record and render one shared `TurnPresentation` through the
   CLI or channel renderer; on an outbound channel settlement stays `pending` and re-runs once the
   delivery is durable
8. append the full planner/tool/hook/task/delivery chain to the run event stream and
   drain inline tasks (one-shot CLI) or leave them for `omni serve`. IM turns pass
   `drain_tasks=False` and do not drain child skill work inline

### CLI live progress (Claude Code / Codex-style transcript)

The same event stream that lands in the run log is the durable source for every surface. The CLI
narrates it directly while the turn is running; the Web surface can replay and follow the same
durable activity stream, including work started by the CLI or an IM channel.
`handle_turn(on_tool_event=…)` receives phase events — `plan` (boundary/model planning, validation
summary, recovery decisions, workflow dispatch), `start`/`done` (every gateway tool call with
arguments, result, duration), `task_start`/`task_progress`/`task_done` (subtasks, hierarchical
`workflow.step.*` stages incl. nested tool calls), plus `budget`/`transcript` notices.

`cli/live_display.py` (`TurnDisplay`) renders that stream as a running transcript: `◆ plan …` lines
for intent/steps/warnings, a live `☐ / ▸ / ✔` checklist whenever the model calls `update_plan`
(replaced wholesale on each call, since the model owns the steps), `⚙ tool(args)` /
`✓ tool · 1.2s · preview` lines with sensitive argument
values masked, `[2/5] skill ▸ start … ✓ done · 3.4s` step hierarchy, and a transient Rich status
line (spinner · stage · elapsed · tool count) between events. Token streaming and the status line
share the terminal cooperatively: the first streamed token stops the status line and event lines
close any open stream line first. Verbosity is `display.verbosity` in config (`quiet`/`normal`/
`verbose`), `-v`/`-q` on `omni chat`/`omni exec`, or `/verbose` in the REPL.

IM channels (WeChat / Feishu / DingTalk) call `handle_turn` without `on_tool_event`, so their own
reply behavior is structurally unaffected. They still write the authoritative task log used by
`/task show`, `/why`, and the Web activity timeline.

The web surface (`omni web`, extra `[web]`) is the same turn, projected over loopback HTTP.
Settings and the first-run modal write the same user `config.toml` / `secrets.toml` as
`omni config` (shared `omni.config.user_edits`); theme and language stay in the browser.
After a save the web process drops cached agents so the next turn reloads settings.
It does not own a process-wide CWD. Choosing a directory in the UI calls `get_paths(cwd=D)` /
`load_settings(cwd=D)` and caches `OmniAgent` by `paths.project_dir`, so a repo the CLI already
used shows the same `sessions.sqlite3`, artifacts, and tool root. New chats in that store use
`channel=web`; continuing an existing row keeps that row's channel. The selected workspace and
session live in the URL hash (`#/w/named/<name>/s/<id>` or `#/w/path/<encoded-path>…`), with
`sessionStorage` / `localStorage` as a backup. Refresh revalidates that locator against the
server: a named project uses `workspace.select` (never `workspace.open` of
`~/.omni/projects/<name>`, which `control_store` rejects as Omni-home). Browser state is not
execution authority. While the tab is visible, `workspace.inbox` polls cheap session
fingerprints (no full user-message scan). Changed transcripts are fetched once via
`session.timeline` (user → executions → answer). Only a followable latest task
(`pending`/`queued`/`running`/`recovering`/`awaiting_approval`) attaches `task.watch`, which
replays durable `task_events` until the task leaves that set. Web-origin turns also
stream their in-process partial text. Provisional token deltas from a different CLI or IM process
are deliberately not persisted: Web shows their durable activity live and their final assistant
message as soon as it is committed. A task's `worker=external` label means only that no Web-local
run handle owns it; channel connectivity is reported independently by the Home Service/channel
health projection.

Task and artifact inspectors use the same durable hierarchy as CLI task detail: Task, Workflow,
Step, Execution/attempt, child task, activity, and artifacts attributed by task/execution/workflow.
While their focused task is active, an open inspector refreshes that hierarchy and its artifact
inventory; terminal settlement triggers one final refresh. Artifact `presentation_role` keeps
primary deliverables ahead of collapsible support files without changing artifact ownership or the
agent runtime.

The SPA lives in top-level
`web/` and ships as `omni/data/web` inside the wheel (same lifetime as the CLI
package). Release packaging runs `cli/scripts/build_web_ui.sh` before `uv build`;
`check_dist.py` requires `index.html` plus a `version.json` that matches the
package version. `omni update` replaces `site-packages/omni/` — including that
SPA — then tells the user to restart `omni web`. The running `omni web` process
keeps the old files in memory until it exits; it never runs Vite for an
installed wheel. `index.html` is served `Cache-Control: no-store` so a restarted
server is enough; hashed `/assets/*` may be cached forever. A checkout without a
packaged SPA resolves `web/dist` and may build it once if Node is on PATH.
Explicit local/editable deployment rebuilds that SPA before package replacement
and fails closed rather than reusing an unverified older `dist`.
`omni doctor` and `GET /health` report the stamped UI version. Bind is loopback
only (`127.0.0.1:1088`); `0.0.0.0` is rejected.

## Planning, Scheduling, and Workflow State

The model remains the semantic planner. OmniScientist gives it a bounded catalog of currently
loaded capability contracts, descriptions, delivery modes, and output contracts. No lexical
pre-filter chooses a subset from the user's language. The model proposes capabilities and
deliverables without needing the full `SKILL.md`; the runtime resolves providers and validates the
locked plan.

**Multi-step work is sequenced by the model, not sealed by the planner.** There is no pre-execution
DAG builder: `planner.plan_from_proposal` routes a multi-step proposal to the capable ReAct turn,
which owns the ordering tools and revises them against live results. It can call:

- `update_plan` to publish and keep updating a short checklist of the steps it intends to take. The
  handler (`skills_runtime/builtin_tools/plan.py`) is deliberately inert — it normalizes the steps
  and hands them back for display — so the next `update_plan` call *is* the repair when reality
  disagrees with the previous list.
- `run_skill` for one skill. `mode=auto` runs sync skills inline, waits for async skills in the
  foreground when the CLI is waiting, and returns a durable background Skill Execution when the
  caller detached or the channel should not block.
- `run_workflow` for two or more skills, ordered dependencies, or cases where upstream results must
  feed downstream steps. The user request is a `tasks` row, the execution is a
  `workflow_runs` row, each logical node is a stable `workflow_steps` row, and every skill-backed
  step creates its own retryable `subtasks` Skill Execution attempt. A step may name a *capability*
  instead of a provider; `runtime/workflow_plan._normalise_workflow_steps` resolves it against the
  live `SkillRegistry` at this tool boundary, which is the last point before the work starts.
- `spawn_subagents` (and the async `SubagentControl` surface) to delegate focused subtasks.

Those durable records are a ledger of what the model did, not a contract it must follow: they are
what makes `omni task show` show step-by-step progress and step-level retry work.

The durable object graph is deliberately explicit:

```text
Task (one user request)
├── WorkflowRun (one run_workflow call)
│   ├── WorkflowStep (stable logical node)
│   │   ├── Skill Execution attempt 1
│   │   └── Skill Execution attempt 2 (retry_of attempt 1)
│   └── WorkflowStep (delegation) ──> Child Task
├── direct Skill Execution
└── direct Child Task
```

A Child Task is a bounded delegated agent request and may own its own workflow; it is not stored as
a Skill Execution and a workflow is never represented by `skill_name="workflow"`. A retry preserves
the WorkflowStep id and creates a new Skill Execution id. This keeps orchestration, logical progress,
execution attempts, and recursive delegation independently inspectable.

The workflow runtime executes a dependency DAG rather than assuming one global serial list.
Dependency-ready steps form deterministic waves; only a step/skill contract that explicitly sets
`concurrent_safe=true` may share a wave. Every wave persists completed ids, pending work, step
results, and recovery metadata. `task retry` creates a linked fresh attempt, while `task resume`
continues the same child from its checkpoint. See
[`agent-runtime-harness.md`](agent-runtime-harness.md) for the execution and recovery contract.

Provider choice is deterministic wherever it happens. At plan time, `SkillArbitrator`
(`agent/skill_arbitrator.py`) turns an explicitly requested skill or a single capability into an
auditable `SkillSelection` — matched capabilities, contract level, and the rejected candidates and
why — from registry contracts and provider priority. Inside a turn, a `run_workflow` step that names
a capability instead of a provider is resolved the same way against the live registry, at the tool
boundary (`runtime/workflow_plan.py`), because that is the earliest point where the choice is not a
guess. The validated plan is not rewritten by the executor.

### Scheduling: one contract, durable approval, honest readiness

Two surfaces create schedules — the `omni schedule add` CLI and the `schedule_task` agent tool —
and they used to diverge (the incident: the model composed `omni schedule add --at …`, an option the
CLI did not have, and naive one-time times meant UTC on the CLI but local via the tool). The
scheduling control plane (`scheduling/`) removes that whole class of drift by funnelling every
surface through **one canonical request and one application service** (the "one core, thin adapters"
shape Codex uses):

- **One contract (`scheduling/contracts.py`).** A `ScheduleCreateRequest` (trigger + goal/skill +
  actor) is the only thing any surface builds. `to_cli_argv()` deterministically serialises a request
  into the exact `omni schedule add` argv, and a test round-trips it through the *real* Typer parser —
  so a surfaced fallback command is guaranteed to parse. Nothing hand-composes a schedule command.
- **One time-policy.** `resolve_once_instant()` is the single place a one-time time is interpreted: a
  naive `at` is the operator's local wall-clock (or an explicit IANA `--timezone`), converted to a UTC
  instant for storage — never face-value UTC. A one-time trigger already in the past is refused with a
  structured `needs_input` + recovery choices instead of being silently created to never fire.
- **One service, two consent paths (`scheduling/service.py`).** `ScheduleService.create()` validates
  the target skill, normalises the trigger, then decides consent by *origin*: a local/CLI request is
  created directly (the owner typed it), while an IM-originated request — whose approver is the owner
  on a *different* process and whose turn cannot block — is persisted as a **durable approval
  proposal** (`ScheduleActionProposalORM`). This is Codex's request/response-by-id approval made
  durable: `omni schedule approve <id>` later executes the *exact stored, sha256-digest-checked*
  payload (never prose re-composed into a command), idempotently. Because the service owns consent,
  `schedule_task` is no longer a blanket-`sensitive` tool that the daemon flat-denies; it stays
  available on IM and routes to a proposal.
- **Truthful outcome (L5).** Every terminal result (created / awaiting-approval /
  needs-input / rejected) is recorded as one `schedule.resolved` event, and the SCHEDULE plan lists
  that event in `VerificationPlan.required_events` — so a turn that claims success in prose without
  actually scheduling settles `failed` instead of reporting a schedule that never existed.
- **Honest readiness (L6).** A schedule only fires when it is registered **and** `schedules.enabled`
  **and** a runner (`omni serve` / the home service) is live. `ScheduleCreateResult` keeps these as
  independent axes and the deterministic summary states them plainly ("no runner is active: start
  `omni serve` …") rather than over-promising "it will run".

#### One brain, headless door: how a due goal executes

The deployed regression the user hit was *execution* asymmetry, not scheduling: a due goal ran as a
single, bounded `agent-goal` ReAct loop (a prompt sub-agent), so a multi-deliverable goal ("fetch an
abstract, draw a RAG figure, write a paper") hit that loop's iteration ceiling after one deliverable
and returned degraded — a completely different code path from the interactive orchestrator turn that
would have decomposed it. The fix removes the second brain: a due **goal** schedule
(`schedules.execution_mode="headless_turn"`, the default) fires a full **headless orchestrator turn**
through the same `handle_turn` → planner → execution → settlement pipeline an interactive turn uses.
A multi-deliverable goal is therefore decomposed by the model into separately-budgeted steps instead
of one flat loop. An explicit-skill schedule (`omni schedule add <skill>`) keeps the direct
durable enqueue.

- **One door (`Orchestrator.run_scheduled_goal`), wired as the scheduler's `goal_runner`.** When
  `Scheduler.run_due` claims a due goal occurrence it materialises the run's owning task (carrying the
  schedule's autonomy grant) and launches the turn *detached from the poller tick* (`asyncio.create_task`,
  tracked in `_inflight`) so planning/execution never blocks the ticker. `drain_fires()` awaits those
  detached turns for one-shot callers (`omni schedule run`, tests); `shutdown()` cancels them so none
  outlives the agent/DB.
- **Unattended, and it stays that way.** The turn runs with `origin="schedule"` ⇒ `ExecContext.autonomous`
  (no interactive approver: sensitive tools clear only through the owning task's pre-authorised grant
  via the approval-gate preauthorizer, exactly as a human "approve for this task") and
  `allow_scheduling=False` (the coordinator surface omits `schedule_task`, so a scheduled run cannot
  recursively spawn schedules).
- **Settlement-driven bounded auto-continuation (Phase 3).** If the turn finishes `degraded`/`failed`,
  up to `schedules.max_continuations` (default 1) follow-up turns are enqueued with a "finish only the
  missing deliverables, do not redo delivered work" directive carrying the outstanding items forward;
  `needs_input`/`succeeded` are never continued (nobody is there to answer; done is done).
- **Always accounted for.** The settled outcome — or, if the run crashes, an honest error note — is
  delivered to the origin channel's inbox as a `scheduled_goal` `TaskNotification`; `run_scheduled_goal`
  never raises into the scheduler tick. Observability (`schedule show`/`list`) binds the schedule's
  "last run" to the turn's newest subtask so a headless run is not a black box.
- **Contained iteration-cliff mitigation (Phase 2).** Prompt sub-agents (including `agent-goal` when
  used as a subagent) now get the coordinator's cheap first-tier microcompaction (shrink the oldest
  tool observations once the transcript passes the model-window budget), so a long run keeps making
  progress instead of starving its own context.

### Reactive typed-plan control plane

Omni's planner is semantic, but its output is not execution authority. The control plane turns the
model proposal into a content-addressed typed plan, validates objective contracts, and then binds
approval and execution to the same accepted revision. It deliberately does not try to prove at
planning time that a schema-valid choice is the user's intended semantic choice.

```mermaid
flowchart LR
  U["User turn"] --> C["Planning contract snapshot"]
  C --> M["Semantic proposal<br/>(exact shortlisted provider schemas visible)"]
  M --> R0["Proposed PlanRevision"]
  R0 --> B["Bind exact provider<br/>(source + version + contract hash)"]
  B --> X["Compile + validate detached candidate"]
  X --> V["JSON Schema + ResolverEvidence findings"]
  V -- "clean" --> A["Accepted PlanRevision"]
  V -- "open finding" --> F["Deterministic recovery ladder /<br/>ReAct / needs_input"]
  F --> A
  A --> H["Approval authority fingerprint,<br/>if plan mode"]
  H --> E["Plan + live contract snapshot check"]
  E --> T["ToolGateway contract + policy gate"]
  T --> S["Provider execution"]
  S --> D["Settlement against the durable record"]
```

#### One execution truth

`IntentPlan` carries `revision`, `revision_hash`, `parent_revision_hash`, and `revision_source`,
plus exact `provider_bindings` and resolver-owned `resolver_evidence`. `PlanRevision`
(`agent/plan_revision.py`) stores a detached full plan snapshot, parent hash, finding ids, and a
deterministic diff. Its SHA-256 content hash covers provider and resolver provenance, workflow
inputs, provider inputs, policies, the verification plan, and the actual execution plan; the four
revision metadata fields are excluded so the hash cannot refer to itself.

Planning history is append-only:

- `plan.revision.proposed` records the raw typed proposal (`source` is `planner`, or `approved` when
  a stored plan is resumed).
- `plan.revision.candidate` records the `compiler` candidate and, when recovery changed the plan, a
  `recovery` candidate. Exactly one final `plan.revision.accepted` becomes authoritative.
- `plan.revision.rejected` retains a malformed, stale, or invariant-breaking
  candidate without making it executable.
- `plan.validated` is emitted once, after recovery selects the final accepted revision. It is not an
  optimistic event emitted before recovery.
- `plan.execution.bound` records the final revision number and hash immediately before dispatch.

The task's `plan_json` is the current full authoritative projection. Revision events retain a full
historical snapshot when its final serialized JSON fits the 256 KiB event budget, measured in UTF-8
bytes after JSON escaping. An oversized revision retains its content hash, parent hash, source/stage,
finding ids, catalog/contract hashes, validation status, bounded diff metadata, and a bounded plan
preview. That clipped event remains tamper-evident provenance, not a reconstructable full historical
body. Before execution, Omni recomputes the accepted plan hash and requires it to equal the persisted
plan hash. It also recomputes the live catalog and provider-contract snapshots and requires both to
match the accepted revision.

Plan mode binds more than plan content. `ExecutionAuthority` fingerprints the canonical plan hash,
catalog hash, contract hash, and the sorted exact sensitive-tool grants. The task stores
`current_authority_fingerprint` and `approval_authority_fingerprint`; presenting a new approval
atomically copies current authority into approval authority and clears old grants. Approval is one
conditional update over task status, both fingerprints, and the exact persisted JSON. Its winner
installs the new grant set by replacement, never union, so concurrent approval, contract drift,
same-tick plan replacement, and privilege residue all fail closed. The invariant is:

```text
accepted revision hash
  == persisted current-plan hash
  == execution-bound hash

approval authority fingerprint
  == hash(accepted plan + catalog + contracts + exact prospective grants)
  == current authority fingerprint at claim
  == bound authority fingerprint at execution
```

Every concrete plan consumer also carries a provider authority. For a skill this includes its exact
source, manifest/contract, engine or CLI binding, admission/replay metadata, primary executable, and
a deterministic digest of every regular file reachable under the provider root. Only VCS metadata
and explicit transient cache directories (`__pycache__`, `.mypy_cache`, `.pytest_cache`, and
`.ruff_cache`) are excluded. Build products, generated artifacts, weights, templates, manifests, and
other runtime assets are therefore part of the authority if they live below that root; providers
must write mutable output elsewhere or intentionally invalidate queued authority. A symbolic link
at any included path outside those excluded directories, or an unreadable/incomplete tree, is
non-executable because Omni cannot seal its target deterministically. Static CLI argument files are
hashed as well. Dotted
modules are resolved through filesystem finders without importing parent packages, so merely
planning an approval-gated provider cannot execute its `__init__`.

Native synthesis, delegated child agents, and ReAct bind a versioned I/O contract and Omni runtime
build identity. Workflow steps and skill-execution rows persist the exact expected provider
snapshot at enqueue. Ordinary dispatch recomputes it immediately before execution; source removal,
same-name source substitution, code/schema/admission drift, or an Omni local update fails closed with
a re-plan/re-submit instruction. No queued operation silently acquires the new implementation under
an old approval. An explicit user `task retry` or `task resume` is a new reauthorization to the
currently resolved provider: it preserves the immutable authority root and appends a
content-addressed, hash-linked renewal. Retry creates a linked fresh execution attempt; resume
continues the persisted attempt/checkpoint in place and may host-fill a remaining figure or
writing deliverable before falling back to that checkpoint. Dispatch validates the root and every renewal
link, resolves the latest applicable provider for the exact consumer, and requires both the logical
workflow step and execution row to equal that renewal head before provider code can run.
An explicit `skill_source` is authority rather than a lookup preference: planning, contract
materialization, validation, repair-schema projection, recovery, authority sealing, and dispatch all
resolve the exact `(source, name)` entry. If it disappears, Omni rejects the plan or execution instead
of falling back to the same-named catalog winner. Legacy `input._skill_source` is promoted to the
top-level step authority during validation and removed before provider input compilation.

No validator, resolver, or recovery rung is allowed to mutate an already-recorded revision. It works
on `deep_clone_plan(...)`; a successful change creates a child revision, and an unsuccessful change
is only an audit event.

#### Two judges, each at the boundary where it has evidence

The host contains no central semantic constraint interpreter, and no deliverable grader. Schema-valid
preferences such as `figure_kind=generic` versus `figure_kind=rag` are not objectively decidable from
a generic planning layer; adding host phrase detectors merely moves provider knowledge into an
incomplete enumeration. Whether the *produced* output is any good is likewise not the host's call:
the model can see the tool results, and re-grading them from outside only produced a verdict that
could disagree with the answer the user was already shown. Omni therefore judges only what it has
objective evidence for:

| Question | Authority | Failure posture |
|---|---|---|
| Are provider arguments objectively legal? | Exact provider JSON Schema | Fail closed before execution |
| Is a resolver-owned value grounded? | `ResolverEvidence` derived from that exact schema | Fail closed until matching evidence exists |

**Objective provider schema.** The shortlisted exact provider's complete input schema is visible to
the planner, including nested properties, enums, descriptions, and `x-omni` selection guidance.
After provider selection, `ProviderInputCompiler` and `PlanValidator` validate the actual arguments
against that same schema. Full contracts reject unknown keys and report precise JSON Pointers,
schema keywords, allowed values, actual values, and the exact provider identity. This mechanism
applies to every new skill and field without adding field-specific host code.

**Resolver evidence.** A provider schema may mark an input as resolver-owned. The host derives a
`ResolverEvidence` slot from the exact `(source, name, version, contract hash, consumer, field)`
binding. Locally normalised user ids can achieve `user_exact`, existing paths `local_exists`, and a
title/model-derived identifier requires `grounded_search`. Syntax alone never proves that an id
belongs to a named paper. Evidence is rematerialized at validation and at the final execution bind;
stale, mismatched, or absent evidence produces a blocking `grounded_binding_unverified` finding and
cannot be repaired by the model.

#### Settlement: bookkeeping, not grading

`runtime/settlement.py` replaces the former host-side deliverable verifier. `settlement_for(store,
task_id)` reads rows that already exist and answers four questions no single execution can answer
for itself; it never reads the answer text or forms an opinion about quality.

- **Are the children done?** A turn that submitted subtasks or workflow runs still in flight returns
  `pending`, and the caller leaves the task running rather than publishing a status it has not
  earned. An outbound channel also stays `pending` until a `presentation.sent|degraded|failed` event
  shows the message actually left; CLI and REPL write to stdout inside the turn and record no send.
- **What did the children come back with?** Failed, cancelled, interrupted, or unaccounted-for
  children settle `failed`; `degraded` children settle `degraded`. `aggregate_outcome_status` takes
  the strongest outcome, so one failed step cannot be averaged into a green run. An empty
  literature funnel (`n_kept=0`) that a later retrieve on this task already superseded is leftover
  churn — the same Codex rule as a retried tool — and does not paint the parent. Lost or failed
  children still win.
- **Did the claimed side effect happen?** `VerificationPlan.required_events` names the durable trace
  a claim must leave. Over IM and in headless runs the user sees prose, not tool calls, so a turn
  that says it created a schedule must have left a `schedule.resolved` event. A required event with
  no matching row is an *unfounded claim* and settles `failed`.
- **Are the named scientific outputs on this task?** `VerificationPlan.required_outputs` names
  contract deliverables (`artifact.figure`, a manuscript, slides). After the turn has produced its
  end event, `remaining_deliverables` looks at artifacts already on *this* task. A missing contract
  output settles `degraded` (`undelivered_outputs`). A sidecar `.dot` or `.json` does not satisfy
  `artifact.figure`; a PNG/SVG on a sibling task does not either. This is presence bookkeeping, not
  quality grading.

A budget-bounded stop reported on `execution.finished` / `react.finished` settles `degraded`.

**Only the turn settles the turn, and only once.** The unfounded-claim check reads an absent row, and
absence is ambiguous: a required event can be missing because the turn skipped it or because the turn
has not got there yet. Three rules keep the second from being read as the first (incident `949be04f`,
where a background Skill finishing mid-turn published `task.failed — the turn claimed work that left
no record: react.finished` on a run that then completed normally):

- **The reader declares which it is.** `settlement_for(..., turn_in_flight=True)` is a caller saying
  "I am not the turn"; it gets `pending` instead of a verdict on a record still being written. A turn
  settling itself has finished producing evidence and leaves the flag false.
- **A child's completion is an observation, not a verdict.** `refresh_from_executions` skips a task
  whose turn is still in flight — the durable execution epoch runs from `record_plan` to a turn-end
  event — so a child can never settle a live parent. This is Codex `trigger_turn: false` applied to
  Skill executions and workflow runs, matching what subagents already do. A drain arriving after the
  turn, or a task whose work was enqueued with no turn at all, still settles here.
- **A terminal status is reached once.** `_finish_task_unchecked` refuses to overwrite one terminal
  status with a different one; moving off a terminal status goes through `reopen_task_for_recovery`,
  so a correction is recorded rather than silently replacing what the user was already shown. This is
  also what confines sealing steering and stamping `finished_at` to a decision that is actually final.

`VerificationPlan` (`agent/intent_plan.py`) is what remains of the old eight-field acceptance
contract, and it now has exactly two fields: `required_events` (enforced as unfounded claims, as
above) and `required_outputs` (enforced as presence debts after the turn ends; also rendered in
plan summaries).

`TaskRecorder.settle_task` calls `settlement_for` and commits
`aggregate_outcome_status(proposed_status, settled.status)` — a `pending` settlement leaves the task
`running` instead, and `needs_input` is a protected suspend that is never ranked on the
success/degraded/failed axis. `OmniAgent._apply_settlement` copies the settled status onto
`TurnResult.settlement_status` and, when the record contradicts a non-error answer, rewrites the
turn to `kind="error"` with `terminated_reason="settlement_failed"` plus a warning naming what was
missing (the unfounded claim, or how many background tasks never completed). The durable outcome is
the `task.<status>` event and the task row status — there are no `verification.*` events. Terminal
statuses are `succeeded`, `degraded`, `failed`, `needs_input`, `cancelled`, and `interrupted`.

Skill engines may still emit a `deliverable_assessment` object in their own output (the
`scientific-figure`, `paper-review`, and `research-pptx` engines do, and so does native final
synthesis). That is provider-local self-reporting for the *model* to read. There is no declared
`quality_contract` for the host to match it against, and no host-driven quality retry.

#### Execution gate and replay authority

`ToolGateway` remains the final shared boundary for coordinator tools, skills, workflow steps,
native runners, and subagents. Invocation order is:

```text
exact tool lookup
  → deep-freeze the admission argument snapshot
  → strict input-schema validation
  → local output-schema definition preflight
  → resolve any generic wrapper to its concrete target
  → tool policy and per-tool limits
  → pre-tool hook / approval / resource lock (detached copies only)
  → compare the canonical argument hash and revalidate the actual execution value
  → execute
  → output-instance validation
  → post-tool hook with the final validated outcome
  → structured event
```

An input-contract violation is rejected before execution. The argument object used for admission is
deep-frozen before any asynchronous observer runs. Start/done events, hooks, approvals, resource
selection, and the task recorder each receive detached data; they cannot mutate the value that a
catalog tool receives. Compatibility callers that provide a zero-argument execution closure are
checked again immediately before provider start: both schema validity and the canonical argument
hash must still equal the admission snapshot, so even a schema-valid A→B rewrite fails closed with
`execution_started=false`. The output schema itself is also compiled
from a private deep snapshot before authorization, budget charging, hooks, approval, locks, or
provider code. Preflight and post-execution validation use that same snapshot, so provider code
cannot change the contract after admission. A scope-aware in-memory registry resolves local JSON
Pointers, anchors, dynamic anchors, and bundled `$id` resources; its retrieval callback rejects
every non-bundled network, file, or custom URI. This also follows a local Pointer target that becomes
an active schema, instead of letting an external reference hide under an extension key. A malformed
schema, unresolved local reference, or unavailable external reference therefore fails with
`execution_started=false` and `side_effect_maybe_committed=false`; external references are never
fetched over network or file I/O. Only a provider result that violates an already valid output
schema is failed with `execution_started=true` and `side_effect_maybe_committed=true`; the runtime
does not pretend the operation was safely undone. The post-tool hook observes that final failure,
not a provisional success emitted before output validation. Automatic retry authority is host metadata
(`replay_safe`) on the concrete `ToolSpec`, deliberately omitted as a model-writable argument.
Non-replay-safe calls get no automatic execution retry; replay-safe transient failures use the
bounded retry policy.

`run_skill` is a routing transport, not an authority boundary. The wrapper and its
resolved concrete target must both pass allow/block policy; one logical call consumes budget once,
while the concrete skill receives the owner hook, approval, resource lock, and schema checks. The
model-facing trace still shows one lifecycle. Native final synthesis is also a typed gateway
provider, with its input and deliverable output validated before the workflow step can succeed.
Explicit skill/tool domain failures are recorded as failed rather than transport-successful; the
versioned command-result envelope deliberately keeps a non-zero process outcome separate because
its output can still be useful evidence.

The gateway's optimized inner paths prove their own preconditions: contract-only execution requires
the exact active name/arguments/sensitivity frame, and nested concrete execution requires an active
outer frame. Each frame owns a shared, revocable one-shot lease: an `asyncio.create_task` child may
copy the context but cannot reuse it after the parent invocation returns; contract-only authority is
exact in name, arguments, and sensitivity, while delegated authority is exact to one concrete
target and consumable once. Future providers therefore cannot bypass policy or budget by choosing
an internal convenience method. Pre-execution rejection markers carry a module-private host seal;
constructing the public compatibility mapping, or returning a dictionary that merely spells
`approval_required` or `policy_violation`, conveys no rejection authority. Such values remain
post-execution provider output and must pass the output contract. A sealed rejection from a nested
concrete gateway retains its provenance through `run_skill`.

#### Codex-style interaction and low-noise presentation

Normal mode presents the stable user lifecycle — planning, executing, terminal result — instead of
exposing internal rung numbers or transient findings as failures. A finding a recovery rung absorbed
is silent or a neutral progress replacement; rejected revisions and complete
finding detail remain available in `--verbose`, `/verbose verbose`, task JSON, and the event stream.
`needs_input` is a resumable pause with one actionable question, never a
`settlement_failed`-looking terminal error.

The busy composer has an explicit destination contract:

- **Enter** steers a semantic ReAct turn at its next safe boundary.
- **Tab** or `/queue <message>` queues exactly one next turn.
- **Esc** or `/stop` requests cooperative cancellation; repeating stop in the same turn
  force-cancels it, and completion resets the sequence.
- Read-only/UI commands that are safe during a turn run immediately; unsafe commands are refused
  instead of being silently deferred.
- If the task finishes in the same tick that a steer is submitted, the atomic active-task insert
  rejects the stale steer and the exact text is reclassified into the next-turn queue once — no loss,
  duplication, or cross-task delivery.
- A steer is durably marked applied only after a ReAct boundary drains it. Terminal settlement
  retries a transient acknowledgement failure; a process-local delivery receipt prevents the
  foreground composer from duplicating already-injected text while durable storage catches up.
- Task steering has an execution-epoch state independent of display/audit stage:
  `closed -> open -> sealed`. `execution.finished` or `react.finished` seals it monotonically;
  later cost, audit, or plan writes cannot reopen it. The same `steering_status="open"` predicate
  participates in the SQLite control insert, so a stale writer loses atomically and creates no row.
- A deterministic workflow or single-skill runner has no model boundary where natural-language
  steering can take effect. The foreground transfers such an unapplied Enter submission once to the
  next-turn queue. Detached `task steer`/IM commands reject it up front with a follow-up hint instead
  of claiming that an orphaned instruction was accepted; cooperative cancel remains available.
- Durable control rows follow `pending -> consumed -> applied` or
  `pending|consumed -> requeued`. `requeued` transfers sole ownership to the next-turn queue, so a
  resumed old task cannot replay it. Each ownership transition and its lifecycle event commit in one
  SQLite transaction. A claimed row records its local consumer PID: a dead consumer
  is recoverable immediately, a live consumer cannot be preempted during its 30-second lease, and a
  legacy row without a PID becomes recoverable after that lease. These crash paths are intentionally
  at-least-once: Omni cannot prove whether an unacknowledged external side effect happened
  immediately before a hard process death, so skills with such effects still need
  idempotency/replay-safety contracts.

#### Operational controls and compatibility

Objective validation and resolver evidence are always enforced; neither has an observe/legacy
rollout mode, and neither is configurable.

The compatibility boundary is one-way. Persisted v1 plans from the retired semantic-binding
implementation may retain
`requested_constraints` and `binding_records` keys so historical hashes and audit views remain
stable. Deserialization preserves them as opaque read-only data; planning, validation,
approval, recovery, and execution do not consume them. New v2 plans use `provider_bindings` and
`resolver_evidence` and do not write the retired keys.

The focused offline acceptance corpus is
`cli/tests/eval/test_objective_provider_quality_offline_corpus.py`. It runs `PlanValidator` against
a registry-backed plan and proves:

- a schema-valid step validates clean, while a bad enum value or a misspelled property is a
  `provider_schema_invalid` finding before any provider runs;
- a schema-valid semantic *preference* (`figure_kind=generic` for a description that sounds like
  RAG) is not a host execution blocker — the provider resolves its effective kind at execution;
- every step of a realistic multi-provider plan — including the native synthesis step, which names
  no skill — is sealed offline to one exact `(provider_name, provider_source,
  provider_contract_hash)`, and `plan.provider_bindings` matches the step bindings one-for-one;
- retired semantic-binding findings never reappear.

Provider-authority, gateway, steering, persistence, and changed-code coverage gates remain
independent.

### Plan validation and the recovery ladder

Before a turn executes, `PlanValidator` (`agent/plan_validator.py`) *classifies* the plan into
structured `PlanFinding`s with a `severity` of `safety`, `blocking`, or `degraded`; the legacy
`errors`/`warnings`/`degraded_warnings` lists are derived views. `plan_recovery.recover`
(`agent/plan_recovery.py`) then *routes* to the next executable state, so a non-safety rejection is
never a dead end (no more `plan_validation_failed` for recoverable causes) — matching how Claude
Code / Codex / OpenClaw treat a skill failure as an observation to adapt to, not a terminal error.
This ladder is the deterministic floor after objective schema/resolver validation, and it is the
only recovery path — nothing asks the model to patch a rejected plan before execution. It is
deliberately short:

- **Rung 0 — safety hard stop.** Over-privilege policy findings (a tool both allowed and blocked,
  negative limits) stop the turn and are never swallowed by degradation.
- **Rung 3 — needs_input.** When the only blocker is a single user-suppliable field (an arXiv
  id/URL), the turn asks a concrete follow-up instead of failing. Provider-owned contract failures
  are never treated as user-suppliable.
- **Rung 4 — ReAct handoff (the floor).** Any other recoverable case is handed to the capable,
  safety-bounded assistant (the same default agent), with the findings injected as context. A
  step-input finding is rewritten into "look this value up — do not ask the user, do not invent it",
  and retained provider obligations are restated so the floor calls `run_workflow`/`run_skill` with
  the authorised providers rather than swapping in its own. The handoff stays under the normal
  `tool_policy` — no self-granted tools.

The numbering has a gap because rungs 1 and 2 are gone. They rewrote a rejected plan in place —
reroute an unbindable identifier, swap a step to a producer capability, prune unsatisfiable steps
and detach their dependents — and all three existed only to patch a DAG sealed before any tool ran.
The model now sequences multi-step work itself against live results, so there is no pre-sealed DAG
left to patch and a plan that will not execute goes straight to the floor that can look things up
and re-sequence. One case skips the ladder's ordering: a `needs_input` plan whose text refers to
prior work is downgraded to a tool-enabled lookup turn (`4_react_lookup`) rather than asking the
user to re-clarify something the agent already produced.

Each decision is recorded as a `plan.recovery` run event (`action`, `rung`, `findings`, `notes`),
and the recovery notes are surfaced through `degraded_warnings` in every channel.

### Intent routing: one semantic planner plus deterministic protocol boundaries

Semantic intent is the model's job; deterministic runtime code is reserved for four things only:
safety/permissions, parsing, execution, and output/edit contracts. OmniScientist keeps **one brain**:
there is no natural-language classifier racing the model and no keyword "rule brain" that fakes
intent when a model is absent. Crucially, the model still only proposes a
**capability**; the runtime picks the trusted provider and validates its input contract through
`skill_arbitrator` and the skill registry. That capability-indirection and contract layer is a
stronger anti-hallucination / policy boundary than raw tool-calling, and everything below builds on it
rather than flattening it away.

- **Editing a figure is a model-chosen capability.** `artifact.revise` (edit the attached figure)
  and `artifact.figure` (a brand-new figure) are the two capabilities the planner may propose. The
  model routes to `artifact.revise` for *any* change to the attached figure; the runtime then decides
  minor-vs-major mechanically — an in-place, contract-validated DOT patch when the target + colour are
  explicit (`artifact_revisions.revise_artifact`), otherwise a source-preserving redraw. The model
  does not decide "minor vs major."
- **Deterministic pre-emption is protocol-only.** `BoundaryRouter` recognizes explicit provider
  syntax such as `$skill`, `skill:name`, and `/skills run name`. Command parsers handle slash
  commands, IDs, URIs, and paths. Natural-language references, edits, questions, and research goals
  always go through the semantic planner.
- **No rule brain; offline degrades honestly.** Keyword classifiers, language-specific synonym
  tables, templated figure answers, and workflow recipes are absent from runtime routing. Without a
  model, OmniScientist can still honor machine-readable boundaries and provide a bounded assistant
  fallback, but it does not invent a domain workflow from words in one language.
- **The revision tool grounds, it does not route.** `revise_artifact` receives a normalized edit
  specification from the planner and resolves an exact artifact element purely to ground the DOT
  patch; it never consults an intent word list to decide minor/major. When it cannot ground a target it returns an *observation*
  (no persisted `artifact_revision_failed`); `_apply_artifact_revision` then auto-escalates through
  `ArtifactRevisionRouter` to a full source-preserving redraw (`_route_major`). Only if neither
  applies does the turn fall through to normal planning / ReAct — never a dead end, never a double
  message. A host major revision always sends `revision_mode=major` plus `revision_constraints`; if
  the figure engine sees constraints without a mode it treats the call as major.
- **Workflow materialization and task refs are not semantic routers.** Ordered steps come from the
  model's own `run_workflow` call; `runtime/workflow_plan` only normalizes them, resolves any
  capability-named step against the live registry, and validates inputs.
  `runtime/taskref` parses *explicit* signals only: `is_task_lookup` is a command
  alias that rewrites a bare status lookup to `/task show`, while `is_task_reference` merely enriches
  the model's context with a referenced task's output — the model still owns the turn.
- **Runtime vs. test are separated.** Offline *testability* lives entirely in the test domain: the
  real model path is exercised deterministically with `ScriptedLLM` / `PlanningLLM` replay fixtures
  (`conftest.py`, `test_artifact_revision_model_routing.py`). `runtime/artifact_intents.py` and
  artifact contract modules are pure runtime (contracts + parsing + structured grounding);
  they carry no offline-degradation "brain" and no test scaffolding.

### Single-pass healthy planning

The healthy planning path is a **single model pass** (Codex-aligned): the planner binds each step's
inputs itself, exactly as Codex/Claude Code let the model fill tool arguments in one decision stream
rather than running a separate parameter-binding round-trip. There is no per-step binding LLM call,
and no second model call to repair the plan before it runs. Three general mechanisms handle the
failure classes a single pass can produce:

- **Exact schema in context.** The semantic planner sees the shortlisted provider's complete input
  contract and chooses schema-valid values. The objective validator catches invalid values without
  trying to infer what a valid enum ought to mean.
- **Provider-local semantic authority.** A portable provider can normalize or resolve its own
  domain choice at execution and report the effective input in its own result. The
  `scientific-figure` engine, for example, resolves its creation kind locally; Omni does not duplicate
  that decision in a central plan-time template detector. When the caller supplies
  `source_artifact_dot` (or a task-owned unrendered `.dot`) *without* `revision_mode`, the engine
  renders that graph instead of restamping the `_rag_dot` template.
- **`missing_inputs` reconciliation (schema-driven).** A stale gap the model lists for a field it
  already bound is dropped deterministically (see below), so it cannot veto an executable plan.

Genuinely missing required fields are still caught by the validator's step-input contract and handled
by the recovery ladder: one user-suppliable field becomes a question, anything else goes to the ReAct
floor to be looked up. Once the turn is running, the model corrects its own course by calling
`update_plan` again and reordering the work it has left.

### Ask-last planning (a discoverable value is never a question)

Asking the user is an **intent the model chooses**, not a veto the planning boundary derives from
side-channel metadata — the same discipline Claude Code / Codex / OpenClaw / Hermes follow, where
"ask" is an action (`AskUserQuestion` / `clarify` / `request_user_input`) taken inside a single
decision stream, and Codex only asks for information that *cannot be discovered by non-mutating
exploration*. An arXiv id is discoverable (world knowledge or `literature.search`), so it must never
become a question. A regression (run `dfcb92bb`) violated this: the model emitted a self-contradictory
proposal — it bound the real id into the step **and** listed a `missing_inputs` gap for the same field;
a legacy `if proposal.missing_inputs:` short-circuit then discarded the fully-bound plan and asked
for the id it already had, *before* the recovery ladder could run. The repair makes `intent_type` the
only routing signal and demotes `missing_inputs` to advisory metadata:

- **Reconciliation (schema-driven, deterministic).** `ModelIntentPlanner._reconcile_missing_inputs`
  recomposes each provider step exactly as the builder will (`_compose_step_input`) and asks the same
  contract the validator will (`skill_input_contract_error`). When every step's required fields are
  satisfied, the stale `missing_inputs` are dropped and audited under
  `binding_audit.dropped_missing_inputs` (surfaced on the `plan.model.proposed` run event). It checks
  *schema satisfaction only* — never field-name matching — so the `arxiv_id`-vs-`identifier` alias gap
  never matters.
- **Placeholder hygiene.** Syntactic placeholders (`<…>`, `{{…}}`) are stripped from step inputs and
  capability inputs at parse time (language-neutral, string-level), so a placeholder neither counts as
  a binding nor flows into a provider.
- **Ask-last routing.** `planner.plan_from_proposal` short-circuits to `needs_input` only when the
  model *explicitly* chose it (`intent_type == "needs_input"`) or when a gap remains with **no
  workflow to run** (`missing_inputs and not workflow_steps`). With steps present, the plan is built
  and handed to validation + the recovery ladder, where **looking up precedes asking**: a genuine gap
  becomes a `step_input_contract` finding, and Rung 3 `needs_input` is reached only when that one
  field is the sole blocker. Anything else falls to the ReAct floor, which is told to resolve the
  value with tools rather than ask.
- **Observable.** Any reconciled-away gaps are recorded under `binding_audit.dropped_missing_inputs`
  on the `plan.model.proposed` run event, so the reconciliation is auditable from the event stream
  instead of reverse-engineered from the DB.

**Look before asking (references to prior work).** Codex never re-asks for something it can retrieve,
so the planner is given the full context it already retrieved and biased toward acting on it:

- **Full planner context.** `_plan_turn_with_model` passes the *whole* `context_summary` — the bounded
  turn context (active target + referenced tasks) **plus** the compiled planning memory **plus** a
  cross-session recent-activity digest — into `ModelIntentPlanner.propose`, not just the turn summary.
  A fresh session that says "regenerate the figure you made" therefore sees the referent and binds it.
- **Recent-activity digest.** `agent.recent_activity.recent_activity_digest` renders the caller's
  recent succeeded/degraded tasks and their artifacts (title + id), filtered to the turn's `principal`
  via `principal_of` (owner unifies CLI + authorised IM; `per_peer` stays isolated). It is injected
  into the planner context and, for lookup turns, into the ReAct system prompt.
- **Reference-aware downgrade.** When the model still returns `needs_input` but the request refers to
  its own prior work (`agent.reference_markers.references_prior_work`, a small bilingual marker set),
  `plan_recovery.recover` downgrades the question to a capable, tool-enabled ReAct turn (`4_react_lookup`)
  with the recent-activity digest surfaced, so the agent looks the referent up before asking. Requests
  with no resolvable referent still ask (no regression).

### Capability-preserving direct answers (never strip the tool the prompt asks for)

`direct_answer` is an *eager-answer bias*, not a zero-tool turn. It once carried
`ToolPolicy(allowed_tools=[], max_tool_calls=0, max_iterations=1)`, so whenever the model
planner classified a tool-needing request as a short answer — a self-knowledge question
("what is your storage architecture"), a file read, a task/corpus lookup — the ReAct turn started with an empty
catalog. The self-knowledge case failed loudest: the system prompt still said "use
`docs_search` first", so the model truthfully refused — it was told to use a tool that was
not there. `build_assistant_plan` now gives `direct_answer` the same read-only floor as
`react_fallback` (`allowed_tools=None`, `blocked_tools=ASSISTANT_BLOCKED_TOOLS`,
`final_reserve_enabled=True`); only `execution_mode="direct"` marks it as the fast path.
Trivial turns still answer in one shot because the model simply does not call a tool — the
same choice Codex, Claude Code, and OpenClaw make by keeping tools available and letting the
model decide, rather than clearing the catalog and then demanding a specific tool.

This is a per-turn **tool-visibility** change only; it does not touch skill arbitration.
"Built-in skills outrank imported ones" stays enforced by `_SOURCE_RANK` in the
`SkillRegistry`, which every resolution path goes through, and `docs_search` is a host
builtin *tool* (not a skill), so it never participates in that ranking.

**Prompt-honesty invariant.** The `[About OmniScientist]` block is rendered from the turn's
actual catalog (`render_self_knowledge(tools)`): with `docs_search` present it asks the model
to ground in the bundled docs and name them; without it (a genuinely tool-less turn such as
`memory_update`) it asks the model to answer from what it already knows and flag the
unverified parts — never naming an absent tool and never forcing a refusal. The rule is
general: never pair "no tools" with "you must use tool X".

### The universal executor ladder (writing as a deliverable)

Writing is a deliverable, not a mandatory skill implementation. When no dedicated writing provider
is selected, `runtime/final_synthesis.run_native_synthesis` still owes the user real content and
follows a universal ladder — the same "base model is the fallback executor" pattern Claude Code,
Codex, OpenClaw, and Hermes all rely on:

    dedicated writing skill  →  base model (LLM) draft  →  deterministic template

The base model writes the draft from the *full* upstream results (not the 240-char UI summaries),
grounded in the recorded source/claim/evidence objects, and the draft is persisted under
`artifacts/report/` so the user receives a file. The template rung is an offline fallback only: it
keeps the workflow contract stable without a model but is honestly downgraded to `status="partial"`
(→ workflow step `degraded`) so a skeleton never masquerades as a finished deliverable. When a model
is present but its rung fails (timeout, provider error, stub-length output), the reason is recorded
in `synthesis_error` so a degraded draft is diagnosable; running with no model at all is the
expected offline path and is not an error.

A written survey that is only `literature.search` plus `synthesis.final` stays on this host
closer (the same carve-out `_qa_figure_pair` already has for answer-plus-figure). Codex keeps
the produce path on the critical path (`apply_patch` in the current workspace). Omni's produce
path for a survey is retrieval plus native synthesis onto **this** `task_id`. Sibling-task
files inform a footnote; they do not settle the contract. ReAct may still sequence richer
turns (figure + draft, ideation, third-party skills). A first lookup-only batch is orientation
and is steered; a second lookup-only batch while a manuscript is owed is the empty loop. An
empty literature funnel is not this-turn research: the host lifts `queries` / `n_kept` onto
the `run_skill` observation (Codex `function_call_output`) and does not treat `n_kept=0` as
evidence that the manuscript can be written.

### Provider-reported deliverable quality

A provider knows things about its own output that the host cannot see from a completed row — that a
figure matches the instruction, that a draft has real content rather than a skeleton. Engines
therefore include a `deliverable_assessment` object in their result (`scientific-figure`,
`paper-review`, `research-pptx`, and native final synthesis all do), alongside the honest lifecycle
`status` that already downgrades a placeholder to `partial` → workflow step `degraded`.

That assessment is self-reporting the **model** reads as one more tool observation. The host does not
match it against a declared contract, does not aggregate its criteria into a verdict, and does not
retry a step because of it — deciding whether the produced deliverable is good enough belongs to the
turn that can see it.

The figure engine also has a narrow **topology gate** (`_topology_gate`): if the instruction names a
perceive/act/reflect loop (or equivalent loop-engineering wording) and the call has no authored
Graphviz source, the engine does not stamp a linear RAG/generic template. It synthesizes a weaker
control-loop schematic from the named stages (`status=partial`, `outcome.code=instruction_graph`)
and still emits DOT/SVG/PNG. Authored DOT is rendered as-is. That vocabulary is an accident check,
not a general "figure matches the instruction" grader. First-time creation is natural-language
`input` only; `source_artifact_dot` is for an exact graph or a revision. Do not shell out to
Graphviz after the engine returns.

### Host figure fill

When ReAct stops still owing `artifact.figure`, `turn_execution` calls `host_fill_figure`
(`agent/figure_runner.py`) on *this* task only. A PNG already on the task skips the fill; a
sibling-task file does not count. If the task owns an unrendered `.dot` (no sibling PNG/SVG), host
fill passes it as `source_artifact_path` so the engine renders that graph.

Resume of a retryable terminal status can do the same via
`task_recovery._fill_remaining_deliverables` when the leftover debts are only a figure and/or
writing. A leftover PPTX or poster is not host-fillable; that resume returns to a full retry.

Workflow execution is durable, bounded, and composable:

- each step has `id`, `skill_name`, `input`, optional hard `depends_on`, optional
  `optional_depends_on`, and optional failure policy metadata
- after the owning Task exists but before a WorkflowRun or Skill Execution is created, the runtime adapts common schema aliases from each skill's
  `input_schema.required` (for example `question`/`description`/`topic` into `input`) and validates
  required fields; if required plan inputs are still missing, `run_workflow` returns
  `status: needs_input` with the missing fields instead of creating a half-failed workflow.
  `action_required` is reserved for host configuration or dependencies discovered at actual Skill
  execution time
- downstream skill input receives `workflow_goal`, `workflow_step_id`, all `workflow_results`,
  filtered `depends_on_results`, and `dependency_failures` when upstream steps degraded
- after every step, `result_json` is updated with `status`, `summary`, `skills_used`, `steps`,
  `workflow`, `recoverable`, and any first `error` or `warning`
- skill result `status` is treated as lifecycle state (`ok`, `partial`, `error`).
  Domain-specific outcomes such as `empty_results`, `not_found`, or `no_evidence`
  live under `outcome.code`, so the scheduler stays generic instead of
  hard-coding per-skill statuses
- if a hard step fails, completed step results remain persisted and hard dependents are skipped
- if a skill or step declares `workflow.failure_policy: continue_with_partial`, that failure is
  recorded as a recoverable step failure; prompt-only boundary stops can also be recorded as
  degraded partial results; downstream skills that declare `workflow.allow_failed_dependencies: true`
  continue with the partial state; the WorkflowRun and owning Task finish as `degraded` when useful
  output remains, instead of reporting a misleading `succeeded`
- foreground `run_skill`/`run_workflow` calls persist and return the task result in the current turn
  without also sending a proactive task notification; background tasks still notify their originating
  CLI or IM channel
- prompt-only sub-agents enforce `allowed-tools` and optional
  `metadata.helixforge.execution` budgets (`max_iterations`, `max_tool_calls`, `tool_limits`).
  `max_tool_calls` is a hard execution ceiling; every rejected overflow call still receives a
  structured tool result before another model request. On timeout/tool-limit/max-iterations they
  make one independently timed no-tool salvage pass and return either a degraded
  `status: "partial"` contract with `warning`/`partial_outputs`, or a recoverable error contract
  when no useful partial answer can be formed
- the interactive coordinator is progress-driven by default: global `react.max_iterations=-1` and
  `react.max_tool_calls=-1` mean no count ceiling. Positive values opt into hard coordinator
  ceilings, while explicit zero remains an exact zero-work policy. A Plan/Skill value remains scoped and exact — including zero —
  so disabling a global guard never widens a declared contract. Prompt
  skills, workflows, and specialist subagents retain their separate owner-controlled envelopes
- workflows add an aggregate step/tool/time envelope across their Skill Executions. A skill manifest may
  request a smaller allowance but cannot raise its trusted skill ceiling, and the workflow deadline is
  propagated into Python, CLI, and prompt execution
- optional `cost.max_total_tokens` and `cost.max_cost_usd` limits are disabled at zero (a negative
  token value is also normalized to disabled). Accounting remains active. When a positive limit is enabled,
  provider-reported ReAct usage stops further tool admission, while workflow aggregation prevents a
  later prompt step from starting after the aggregate limit is reached
- python-engine skills (`research-ideation`, `research-pptx`, …) go through a host-side
  `UsageTrackingLLM` wrapper for the duration of `execute_skill`. Every real `chat` /
  `chat_with_tools` call is aggregated into one `cost.usage` event (`component=engine:<name>`).
  When the provider omits `usage`, the wrapper estimates from the request text and still
  records the event as `estimated`. Throttled `usage` progress snapshots update the live
  status line during the engine run. Portable adapters also forward provider `usage` in the
  OpenAI-shaped response; they do not import CLI cost internals
- `cost.warn_total_tokens` (default 200k) and `cost.warn_cost_usd` (default $0.50) emit a
  one-time live notice and keep running — long-horizon research is not hard-stopped by default.
  The live status line and the turn completion line show cumulative tokens and estimated USD
- `memory.tool_observation_max_chars` (default 8000) projects the latest tool result into the
  ReAct transcript. The full payload stays on the task event. Microcompact still trims *older*
  observations; this cap is what stops a single huge skill dump from being re-billed every
  later iteration

### A budget stop still writes an answer

Hitting a ceiling means "stop spending", not "stop talking". When the ReAct loop reaches
`max_total_tokens`, `max_cost`, the iteration limit, the tool-call limit, the wall-clock/stall
watchdogs, or a no-progress streak, `_terminate_or_synthesize` does not hand back a stub: for the
token/cost stops it first microcompacts the bulkiest old tool observations (the stop *is* the
context being too expensive), then makes one final call with tools disabled
(`tool_choice="none"`) over the results it already has and returns that as the answer. The salvage
stub survives only as the fallback for when that last call itself times out or comes back empty.

Such a turn is labelled honestly. Its `terminated_reason` carries a `synthesized_` prefix
(`synthesized_max_total_tokens`, `synthesized_max_iterations`, `synthesized_max_tool_calls`, …), and
`core/termination.py` strips the prefix and classifies every one of those base reasons as a
*bounded* stop — so the turn settles `degraded`, never `succeeded`. `TERMINATION_LABELS` and
`termination_next_action` give each one a user-safe label and the single action that lifts it
("re-run with a larger token budget"), because a bounded stop reported without that reads as "try
again" under the same ceiling. Rejected overflow tool calls still receive a structured result first,
so the transcript stays well-formed for the final call.

### Multi-agent delegation and async subagents (Codex AgentControl parity)

The coordinating ReAct loop delegates focused subtasks to specialist subagents (the
coordinating → specialist → reviewer pattern). A specialist runs its own bounded ReAct loop on a
cloned `ExecContext` — fresh `task_id`, no shared coordinator/sibling history, its own tool budget,
optional model/compute/isolation — and returns only a compact summary; an optional reviewer gates it.
Delegation is depth-bounded (`subagents.max_depth`): a specialist at the limit is not offered the
delegation tools, so fan-out never recurses without end. Each specialist is a first-class Child Task
(`kind=subagent`) with its own `subagent.*` events and component cost, not a `skill_name="workflow"`
record.

Two surfaces share the same specialist runtime:

- **Blocking fan-out** — `spawn_subagents` runs a batch in parallel (bounded by `concurrency`, capped
  at `max_subagents`) and returns when all finish. On by default.
- **Async fire-and-collect** — opt-in (`subagents.async_enabled`), mirroring Codex V2's `AgentControl`.
  A turn-scoped `SubagentControl` (the in-turn `AgentControl` analog) exposes `spawn_subagent` /
  `wait_subagent` / `message_subagent` / `followup_subagent` / `list_subagents` / `interrupt_subagent`.
  Each async subagent runs in its own `asyncio` task with its **own** `ExecutionControl`, so
  `interrupt`/`message` target exactly one child while a parent cancel fans out to all. A session-tree
  semaphore (`max_active`) caps concurrently executing subagents (the `AgentExecutionLimiter` analog).
  Completion posts a result the coordinator reads via `wait_subagent` — it never auto-triggers a
  parent turn (Codex `trigger_turn: false`). `message_subagent` reuses the steering path
  (`ExecutionControl.push_steer`); `followup_subagent` is a continuation seeded with the prior summary,
  since a specialist is single-shot. At turn end the control plane joins or cooperatively cancels every
  spawned subagent within a short grace, so no orphan `asyncio` task or unbilled child survives the turn.

Differences from Codex, on purpose: the control plane is **turn-scoped** (Codex threads can live
across turns within a session — durable async work in omni stays with the skill subtask runtime);
nesting stays behind `max_depth` (Codex V2 lets children spawn children freely); and omni keeps its
own advantages — an integrated reviewer gate and real filesystem/compute isolation
(`worktree`/`container`) where Codex subagents share the parent cwd/sandbox.

### Skill inventory and execution boundary

`skills/index.toml` is the fail-closed Active Manifest for bundled Skill content. Build, runtime
discovery, export, and uninstall read that same file; user/project/external Skill libraries remain
independently discoverable.

`execute_skill` is the single execution boundary. It admits only trusted skills and then runs the
engine through the shared `ToolGateway` (the same policy path used by every other tool), so there is
no parallel readiness lifecycle or submission-time gate. A skill that needs an owner action reports
it the ordinary way — the engine returns a structured `action_required` (e.g. research-pptx emits
`omni skills setup research-pptx` when Node is missing). Inline Agent execution maps that at its
presentation boundary to a friendly `needs_input`, while durable background tasks, workflows, and MCP
preserve the same action in their result.

VLM is an owner-controlled Host Service. The LiveFigure adapter uses the narrow
`generate_text` port and Omni's MCP response never serializes the endpoint or API
key, so Claude Code, Codex, and OpenClaw use LiveFigure through Omni MCP by
default. This is a transport and API boundary, not a sandbox for trusted
in-process Python Skills: their engine receives `ExecContext`, which includes
host settings and services, and trusted code could inspect those objects. Hard
secret isolation would require a sanitized out-of-process execution context.
The explicit standalone runner is the only supported path that reads
`OMNI_VLM_*` variables.

Every user input from CLI, REPL, WeChat, Feishu, or DingTalk creates a **Task** (`tasks`, one row per
user request) before the first tool call. ReAct tools such as `search_corpus`, `record_claim`,
`add_evidence`, `run_skill`, and `run_workflow` are recorded as append-only `task_events` with
input/output/status/timing. Workflows, stable steps, skill attempts, and delegated Child Tasks use
their own object types. This keeps the UX aligned with Claude Code/Codex-style agent traces:
`/task` shows the user request while it is running, `/task show <task_id>` shows the whole chain,
and `/task subtask <task_id>` lists only retryable Skill Executions. If a non-critical step or
execution degrades but the task still delivers, the Task finishes as `degraded` instead of collapsing
the whole request to `failed` or claiming complete success. Foreign keys use `ON DELETE CASCADE` for
the execution graph; `artifacts.subtask_id` uses `ON DELETE SET NULL` because produced files are user
deliverables and must outlive an execution record. Tasks carry a `kind` (`turn` for user requests,
`subagent`, `maintenance`); `/task` lists `turn` by default and `--kind` exposes system records.

Two housekeeping policies keep this history honest without manual sweeps (both run at `omni serve`
startup, every ~10 minutes on its poller, and once per `omni task drain`):

- **stale-task reconcile** — a task stuck in `running`/`recovering` with no event for
  `tasks.interrupt_stale_after_s` (default 30 minutes; `0` disables) lost its process and is settled
  as `interrupted` (terminal, prunable). `awaiting_approval` and `needs_input` wait on a human, not a
  worker, and are never reconciled. `omni doctor` reports stuck tasks under *Task hygiene*.
- **retention** — with `tasks.retention_days > 0` (default off), `failed`/`cancelled`/`interrupted`
  tasks whose completion is older than the window are deleted automatically, cascading to their
  subtasks and events; `succeeded`/`degraded` tasks are provenance and are never auto-deleted, and
  artifact files are never touched.

`omni task show <id>` defaults to the readable view for a Task, WorkflowRun, WorkflowStep, Skill
Execution, or Child Task id;
`omni task show <id> --json` keeps the full machine-readable payload. In the REPL, `/task show <id>`
and `/task show <id> --json` mirror the same behavior, while `/task attach <id>` brings the selected
result boundary back into the active session.

Tool event lifecycle and domain outcomes are deliberately separate. For example, the Bash handler
can execute correctly while the requested process exits non-zero. That event remains
`react.tool.done` with transport `status="succeeded"`, while its bounded `output_json` carries:

```json
{
  "result_schema": "omni.command-result.v1",
  "command_status": "failed",
  "reason": "nonzero_exit",
  "exit_code": 1,
  "output": "",
  "output_truncated": false,
  "summary": "Command exited with code 1"
}
```

`command_status` is one of `succeeded`, `failed`, `timed_out`, `blocked`, or `invalid`. Transport or
handler exceptions still produce `*.tool.failed`; policy/approval denials produce
`*.tool.rejected`. Consumers must use the structured command result instead of parsing the
model-facing legacy observation such as `[exit=1]`. Existing historical string event outputs stay
unchanged and should be classified as unknown legacy outcomes rather than inferred from display
text.

## Storage model

A single SQLite file per workspace (`<workspace>/sessions.sqlite3`, WAL mode) holds all structured
tables: `sessions`, `conversation_messages`, `tasks`, `task_events`, `workflow_runs`,
`workflow_steps`, `workflow_checkpoints`, `subtasks` (Skill Execution attempts),
`memory_entries`, `artifacts`, plus the **Research Object Model** (`sources`, `source_chunks`,
`hypotheses`, `claims`, `evidence`, `experiment_runs` — see below). Artifact bytes always have a
canonical `artifact://<id>` URI. In an untrusted or taskless execution they live in the durable
`<workspace>/artifacts/<kind>/` store. A recorded task launched from a trusted workspace publishes
directly into one task-scoped user directory instead:

```text
<trusted-output-root>/<collection>/<task-title>_<task8>/
├── <semantic-name>-<task8>-<artifact8>.md
├── <semantic-name>-<task8>-<artifact8>.svg
├── <semantic-name>-<task8>-<artifact8>.json
└── _omni-manifest.json
```

The first published artifact chooses the broad collection (`reports`, `figures`, `presentations`,
`reviews`, `notebooks`, `datasets`, or `outputs`); every later format and producer for that task
reuses the persisted scope. The title comes from `TaskORM`, so routing costs no model call. A live
`ExecContext` facade supplies missing task/session/workflow ownership to every skill store call, and
the filesystem tool sends only *new bare output names* through the same scope. Explicit paths and
edits of existing source files remain in place. Scope metadata stores the trusted root used at
publication time, allowing historical URIs to resolve after a later session changes its output
root without weakening path containment. Turn completion lists paths from canonical artifact rows,
not from model prose. No MySQL / Redis / MinIO / ChromaDB.

### Workspace identity (path-keyed, like Claude Code)

The workspace is resolved from the *absolute* working directory so every terminal opened in the
same repo shares one durable store (the active Omni data home is never itself a project). The data
home resolves from `OMNI_HOME`, then the persistent `omni config home` selection, then `~/.omni`.
The persistent selection is a bootstrap pointer outside the data home:
`$XDG_CONFIG_HOME/omni/home` (fallback `~/.config/omni/home`) on POSIX and
`%APPDATA%\omni\home` on Windows. Keeping the pointer outside the selected directory lets a new
process discover that directory before it can read `config.toml`.
Workspace resolution order:

1. `-P <name>` → `<OMNI_HOME>/projects/<name>` (named project)
2. in-place `.omni/` found walking up from CWD (excluding `$HOME`)
3. enclosing VCS root (`.git`/`.hg`), keyed by path → `<OMNI_HOME>/workspaces/<slug>-<hash8>`
4. otherwise the resolved CWD, keyed the same way

A best-effort registry (`<OMNI_HOME>/workspaces.json`, upserted on agent start) indexes every workspace
so the daemon can find in-place projects. Cross-workspace views (`omni task all`) also scan
`<OMNI_HOME>/workspaces/*/sessions.sqlite3` and named project DBs, so a missing registry cannot
hide tasks that are still on disk. Incompatible local
SQLite schemas are upgraded additively with a pre-migration snapshot and tracked by
`PRAGMA user_version`; legacy default/home-edge data is moved only by the explicit project
migration command.

```
<OMNI_HOME>/                    # ~/.omni by default
├── config.toml                # user config (layered)
├── secrets.toml               # api keys (never project-overridable)
├── role.md                    # optional system-role override
├── skills/                    # user OmniScientist skills
├── skills_install.json         # built-in skill exports tracked for safe unexport
├── channels/                  # per-channel config (wechat.toml, feishu.toml, …)
├── logs/                      # daemon logs, e.g. serve-<project>.log
├── workspaces.json            # registry of known workspaces (advisory; --all also scans disk)
├── projects/<name>/           # named (-P) projects
└── workspaces/<slug>-<hash8>/ # path-keyed workspaces (auto)
    ├── sessions.sqlite3        # all structured data for the workspace
    ├── artifacts/              # durable fallback for untrusted/taskless output
    ├── inbox.jsonl             # task-completion notifications
    ├── library.jsonl           # project citation/reference library
    ├── channel_inbound_seen.json # IM inbound idempotency cache
    ├── serve.pid               # daemon pidfile + heartbeat (when omni serve runs)
    └── NOTEBOOK.md             # human-readable lab notebook
```

### Identity: sticky base role vs. scientist-persona overlay

Runtime `role.md` files and the packaged default have distinct, non-overlapping roles:

- **Base identity — `<OMNI_HOME>/role.md`** (or the built-in default, or `settings.role`).
  Loaded **once** at agent construction and cached in `self._role`; it defines "You are
  OmniScientist…" and stays constant for the process. This is the deliberate analogue of Codex's
  session-static `base_instructions` — a *sticky* identity, not hot-reloaded.
- **Scientist persona — `<project-root>/role.md`** (the working directory). This is the
  [`soulagent`](../../skills/soulagent) skill's `omniscientist` **persona stoma**: a temporary,
  reversible scientist persona decoded from a `scientist-kg/` knowledge graph, guarded by a
  `.soulagent/` lock + state protocol. Before assembling each turn's prompt,
  `omni.agent.persona_stoma.load_persona_overlay()` reads the *ready* stoma and
  `build_system_prompt(persona_overlay=…)` splices it **after** the base role as an additive
  `[Active scientist persona]` block. It shapes judgment and voice while product identity, tool
  policy, safety, and citation duties remain the base role's.

The overlay is presence-gated and fail-open: with no active persona (or a stoma still being
written, or one committed for another host) nothing is injected and the prompt is byte-for-byte
what it is today. The adapter only ever reads inside the project root — it never reads or writes
`<OMNI_HOME>/role.md`. The `omni soul` command group exposes this boundary without importing Skill
internals: bare `soul`/`soul status` reports the active overlay, `soul list` inventories the exact
project-local-first scanner root, and `soul create <scientist>` submits a focused
`scientist-kg-distiller` task that installs a validated KG but deliberately leaves it inactive.
The same read-only forms are available as `/soul` and `/soul list` in the REPL. Activation,
switching, and unloading still go through the `soulagent` skill (e.g. "think like Kaiming He",
`$soulagent`, or "restore yourself"), never by editing the base role.

### Local file & shell tools (working directory + approval)

In an interactive CLI the agent is also a Claude-Code-style local operator, not only a research
tool. `read_file`, `write_file`, `edit_file`, `list_dir`, `grep`, `glob`, and `bash` act on the
**tool working directory** — the folder `omni` was launched from (`OmniPaths.invocation_cwd`),
guarded against the filesystem root. That directory is both the bash cwd and a write root, so
relative paths resolve where the user is. IM/daemon turns keep the path-keyed `workspace_root`
instead, so a remote channel never widens its scope to an arbitrary launch directory. `omni status`
prints the active *Tool working dir*.

`bash` runs under a two-tier guard selected by `security.bash_sandbox`:

| tier | delete/rewrite/publish inside the dir (`rm -rf`, `git reset --hard`, `git push`) | system ops that escape it (`sudo`, `mkfs`, `dd if=`, `curl \| sh`, `shutdown`) |
|---|---|---|
| `readonly` | blocked | blocked |
| `workspace-write` (default for interactive CLI) | allowed, approval-gated | blocked |
| `full` | allowed | allowed |

Compute tools share one permission envelope with `write_file` (the turn working
directory, the project store, and managed output roots). Codex `workspace-write`
is the same shape — cwd + configured roots + persistent `/tmp`. Omni keeps a
separate ArtifactStore, so `bash`, `run_compute`, and CLI skill processes also
receive a durable `$OMNI_OUTPUT_DIR` (`<project>/artifacts/compute/<task_id>`)
and a workspace-scoped `$TMPDIR`. Files written to `$OMNI_OUTPUT_DIR` are
registered through `register_existing` and are what verification sees. Host
`/tmp` stays writable for scratch but is not readable by `read_file` and is not
an artifact sink. Linux bwrap bind-mounts the persistent exec tmp over `/tmp`
instead of a fresh `--tmpfs`, so successive calls see the same scratch.

Mutating/executing calls (`bash`, `write_file`, `edit_file`, `run_compute`) still enter the
**approval gate**, and destructive shell commands are classified `destructive` so a prompt names
their risk explicitly. The gate often auto-approves without a human: in-workspace writes clear via
`_write_stays_inside` only when the sandbox is write-capable (`workspace-write` /
`full`); known-safe reporting commands (`cd`, `pwd`, piped `head`, `git status`/
`log`/`diff`/`show`) clear under `untrusted`. `dot` is not on that known-safe
list. A trusted `on-request` turn auto-allows non-destructive `bash` (including
`dot` / `python3 -c`) inside `workspace-write`.

`security.approval_policy` (when `require_approval=true`) is:

| policy | behaviour |
|---|---|
| `untrusted` | Codex UnlessTrusted: ask for every non-known-safe exec |
| `on-request` | Codex OnRequest: `workspace-write` / `full` sandbox auto-allows non-destructive `bash` / `run_compute`; destructive and sandbox-escape still ask |
| `always` | ask before every tool call |
| `never` | auto-approve everything (same as `require_approval=false`) |

Effective defaults follow Codex presets, not a single factory value:

| surface | combination |
|---|---|
| Trusted interactive CLI (`omni`, `omni chat`) | `workspace-write` + `on-request` (Codex Auto) |
| `omni exec` in a trusted directory | `workspace-write` + Never (`workspace_auto`) |
| Untrusted directory | `read-only` + `on-request` — edits and commands ask or fail closed |
| Library / tests (`trusted` unset) | factory `untrusted` + `workspace-write` |

An explicit `security.approval_policy` in user config still wins on a trusted load. Untrusted always forces `bash_sandbox=readonly`; `omni trust` is the write gate.

`security.require_approval=false` is full autonomy. `security.approval_allowlist` pre-approves
entries such as `"bash:git "`, `"write_file"`, or `"*"` so those calls skip the prompt. Sensitive
files (`.env`, secrets, SSH keys) stay hidden by the fs tools regardless of tier. When no local
approver is wired (IM/daemon), `write_file` / `edit_file` stay in the catalog if the destination
can be assessed (policy ≠ `always`) and in-workspace writes auto-approve only in a
write-capable sandbox; `bash` / `run_compute` stay blocked unless `workspace_auto`
or a task grant is present. `require_sensitive_confirm` is enforced on `bash` /
`run_compute` only.

``omni exec`` is the Codex-``Never`` exception in a *trusted* write-capable
sandbox: it is non-interactive even on a TTY, offers sandboxed ``bash`` /
``run_compute``, auto-approves those calls plus in-workspace writes (including
workspace-destructive commands the sandbox still confines), persists the grant
on ``task.approved_tools`` so ``--detach`` / retry / recovery inherit it, and
still fail-closes escapes and IM. An untrusted directory stays read-only — Never
does not widen it. ``omni exec --ask`` restores the human prompt loop on a
terminal; without a TTY it warns and still fail-closes.

Interactive decisions use a separate, memory-only **session approval store**. Ordinary Bash prompts
follow Codex's decision order: **Approve once**, an optional reviewed operation rule, then **Deny**;
when the call belongs to a live CLI task a further **Approve this turn's workspace** choice is
offered. That grant covers later ``bash`` / ``run_compute`` on *this task only* — including
workspace-destructive commands such as ``git push`` / ``rm -rf`` — after a second confirmation
whose default is Cancel. It is also written to ``task.approved_tools`` so a later process does
not re-probe. It does not disable the sandbox, system hard-blocks, or tool policy, and it does
not follow the owner onto IM / scheduled runs. The first item is selected by default while Esc/Ctrl+C
always denies. Bash does not advertise its internal exact-command cache as a separate choice because
parameterised commands make that choice indistinguishable from a one-off approval in normal use.
Exact grants remain an internal compatibility surface and still bind the executed source to the
working directory, workspace, channel, configured sandbox envelope, and risk class.

For non-destructive command families, a shell call may propose an argv `prefix_rule`. The host offers
it only when the source is plain and expansion-free, the proposed tokens are its literal prefix, and
the family is narrowly reviewed. Destructive rules are never trusted from model metadata. The host
may instead derive a closed semantic rule for `omni ... task rm|delete`: project, verb, `--force`,
and `--yes` authority remain fixed, while only task ids vary. Every later match is parsed and checked
again; wrappers, compound commands, unknown flags, `clear`, and `prune` remain one-off approvals.
A redundant trailing `2>&1` is normalized because the Bash tool already merges stderr into stdout.
The prompt shows the exact operation pattern being granted. Session rules remain context-bound and
process-memory-only; they never alter `security.approval_allowlist` or bypass schema, policy, sandbox,
hook, or resource-lock checks.

Each session store serializes and re-checks its own requests, while the TUI approver serializes
prompts across stores before they reach its single modal. The owner's actual decision latency stays
outside turn/workflow clocks; a queued duplicate can inherit the preceding grant instead of being
denied because a modal is already open. This changes consent coordination only: schema/policy admission
still happens before it, while hooks, resource locks, provider execution, outcome recording,
planning, ReAct, task lifecycle, and memory stay on their existing paths.

### Background tasks across windows

`tasks` and their `subtasks` are durable and shared by every terminal in the workspace
(omni's edge over Claude/Codex, which have no cross-window task view). When `omni serve` runs it
*owns* execution: it writes `serve.pid` (heartbeat every 5 s) and a poller picks up tasks enqueued by
other windows within ~2 s; a conditional `UPDATE … WHERE status='pending'` claim guarantees a child
task runs once even if a REPL drain and the daemon race. With no daemon, the REPL/one-shot drains
tasks inline. The same `task_events` stream feeds CLI rendering, IM summaries, JSON export, and
daemon logs, so a tool chain visible in the terminal is also inspectable through `/task show`.

The installed `omni` command does not start a resident agent by itself. CLI and REPL calls run in
the foreground; IM channels and cross-window background task execution need either `omni serve`
(foreground), `omni serve start` (background daemon), or `omni channel login <name> --start`.
Background daemon stdout/stderr is written to `<OMNI_HOME>/logs/serve-<project>.log`.

The always-on **home service** (`omni serve`) is one OS-supervised process per `OMNI_HOME`. Its
observed state lives in `<OMNI_HOME>/service/service.pid` and is three layers:

1. **Liveness / STARTING** — the process holds the singleton lock and has published identity.
2. **Control-plane READY** — hosted agents and task runtimes can accept schedules and inbound
   tasks. Written *before* WeChat `notify_start`, Feishu, or DingTalk finish connecting.
3. **Channel health** — each IM adapter is tracked separately in `channel_health`. A slow or
   failed handshake degrades that channel; it does not mark the gateway down.

`omni update` always restarts this process onto the newly installed code. Restore succeeds when
the new process has claimed the singleton. A timeout that is still STARTING is a warning
(exit 0), not an update failure. The process dying or never claiming is a hard failure and
includes phase, pid, version, and a tail of `home-service.log`.

## Channels, QR login, and IM safety

`channel add` only writes a local channel template. `channel login` is the binding path: it writes
channel config, stores platform secrets, prints a QR or setup URL, creates a short-lived pairing
code, and can start the daemon with `--start`.

Current channel behavior:

- **WeChat**: official ClawBot iLink QR only (`omni channel login wechat`). Omni talks to
  `ilinkai.weixin.qq.com` directly — scan the liteapp QR, store `bot_token`, then `getupdates` /
  `sendmessage`. The scanning account is auto-allowed; additional users send `/pair <code>`.
  Self-hosted `:8088` bridges and WeCom are not supported.
- **Feishu**: requires an app id and app secret (`--app-id`, `--app-secret`, or existing config).
  Inbound events use the official `lark-oapi` WebSocket client. The QR is an AppLink that opens the
  bot chat; the user sends `/pair <code>` there to add that conversation to the allowlist.
- **DingTalk**: requires enterprise Stream credentials (`--client-id`, `--client-secret`, or
  existing config). Inbound events use the DingTalk Stream SDK. If `--bot-url` is known the QR opens
  the bot chat; otherwise the QR points to the Stream setup page and binding still happens with
  `/pair <code>`.

Security defaults are deliberately conservative because IM messages enter the local agent loop:
allowlist and pairing are enabled by default, pairing codes expire after 600 seconds, inbound events
are deduplicated in `<workspace>/channel_inbound_seen.json`. In-workspace file writes from IM
auto-approve; `bash` and `run_compute` still require local confirmation
(`require_sensitive_confirm`) and stay out of the catalog unless granted. IM turns pass
`drain_tasks=False`, so child skill work is not drained inline. Channel secrets are stored
in macOS Keychain when available; where no encrypted store exists, `channel login` reports the
choice and stores them in `<OMNI_HOME>/secrets.toml` (mode 0600) instead.

After pairing, IM input first passes a safe command router before normal agent chat. The current
allowlist exposes `/stop`, `/steer`, `/plan`, `/task` (including `show` / `watch` / `attach` /
`retry` / `resume` / `approve` / `cancel` / `steer` / `subtask`), `/inbox`, and
`/verify --session` without invoking a shell. Strong task-lookup phrasing such as "show the
execution of task c98e4330" maps to `/task show c98e4330`; unclear or unsupported commands fall back to
agent chat or a local-CLI prompt instead of silently executing sensitive operations.

## Skills: four forms of consumption

One `SKILL.md` (Claude-Code compatible frontmatter) can be consumed as:

| form | `kind` | how it runs |
|---|---|---|
| Prompt | `prompt_only` | injected as instructions for a focused ReAct sub-agent |
| Python engine | `python_engine` | `import module.class; await execute(**input)` |
| CLI exec | `cli_exec` | subprocess; input as JSON on stdin; parse stdout |
| Remote MCP | `remote_mcp` | surfaced as a direct tool via the MCP client |

`delivery_mode` (`sync_tool` | `async_task`) is a scheduling hint, not a hard usability boundary.
`sync_tool` engine/exec skills are exposed as direct tools and can also be called through
`run_skill` or workflow steps. `async_task` skills can still run in foreground inside the durable
runtime when the CLI is waiting, or in background when the user detached or the inbound channel
should not block. Both paths use the same `execute_skill` contract, so prompt-only, python-engine
and cli-exec skills can participate in workflows.

`find_skill` is the coordinator's parameter lookup (Codex stage-2 analogue). A hit returns a
compact `input_schema` and a `run_skill` example, with an exact name ranked above a neighbour
that merely mentions it. After that card is returned, further `docs_search` / `glob` /
`search_tasks` probes — or another `find_skill` that returns the same skill — count as
no-progress (BUG-11). A second `find_skill` for a disjoint skill is setup for another
consume, not a hunt: Codex keeps the tool channel open so the model can run both.
A lone contracted capability
(for example `slides.generate` labelled as a one-step workflow) stays on the host
`single_skill_task` runner; settlement owes `artifact.slides` when the request or selected
skill names a deck. The coordinator does not re-implement a `python_engine` pipeline by
reading `SKILL.md` and calling bash.

## Memory (M1–M5)

`memory_entries` rows carry `layer` (M1 session … M5 artifact), `scope`/`scope_id`, a `principal`
(privacy owner), an inline embedding vector, importance, `recall_count` and a pin flag. Recall blends
cosine similarity + recency + importance + pinning + usage (`recall_count`) + a citation/grounding lift
(a `payload_ref`-anchored memory outranks an equally-similar ungrounded one). `memory.embeddings_enabled`
is an explicit master switch and is **off by default**. Onboarding explains semantic versus keyword
recall and, when enabled, records a dedicated `/embeddings` endpoint/model/key. When disabled, stored
embedding settings cannot override the switch and recall uses keyword overlap without a probe. An
enabled but unavailable endpoint still degrades safely to keyword recall for that process. No native vector DB required;
`sqlite-vec` is an optional acceleration. Keyword overlap is CJK-aware: whitespace has no word
boundaries in Chinese/Japanese/Korean, so those runs are tokenised into overlapping character bigrams
(single characters kept for length-1 runs) — otherwise a whole Chinese query collapses into one token
and lexical recall silently degrades to ~0 as the store grows. ASCII tokenisation is unchanged.

**Identity isolation (`principal`).** With `omni serve` fronting multiple IM peers, every row is
tagged with a `principal` — `"local"` for the CLI/machine owner, `"<channel>:<external_key>"` per IM
peer. `record()`/`recall*()` filter by it: a principal sees only *its own* memories plus the owner
baseline (`local`), never another peer's. `memory.channel_identity` selects how an authorized IM
identity maps: `owner` (default) folds every paired identity into `local` so what the owner says on
Feishu is recalled in the CLI (personal-assistant default); `per_peer` keeps each identity separate
for a shared multi-user bot (zero cross-talk). `principal_of()` is the single source of truth, shared
by the orchestrator and `subtask_runtime` so background task results never land under the wrong owner.

**Machine-global store (cross-workspace/CLI/channel).** Durable *identity* memory — `user`-scope
preferences, the synthesized `user_profile`, and episodic (M3) summaries — routes to one machine-global
store `<OMNI_HOME>/memory.sqlite3` shared by every workspace, CLI and the daemon, so the owner isn't
forgotten on `cd`. Everything else stays workspace-bound. Recall dual-reads (unions a bounded candidate
set from both stores) and the memory graph spreads across both, merging boosts. `memory.global_store`
(default on) is the gray switch (`off` = legacy per-workspace only); a one-time marker-guarded backfill
migrates legacy owner identity rows; WAL + an advisory file lock (`memory/locks.py`) keep concurrent
processes safe. A small, token-bounded, change-gated digest `<OMNI_HOME>/memories/memory_summary.md`
is injected first among the personal blocks. `memory.global_store`/`channel_identity` are owner-only
(a project config can't override them).

Memory is **two-tier** (conclusion-level write-up: [`memory.md`](memory.md)):

- **Tier A — working continuity (relational, deterministic, offline).** Completed task results are
  written back into the owning session transcript (`conversation_messages`, `content_type=task_result`)
  with an attached artifact list; `artifacts` are stamped with `session_id`/`task_id`. The agent
  pulls precise context back with explicit `memory_search`/`memory_get`, `list_session_artifacts`,
  `get_task`, `open_artifact`, `get_run` tools (search→get, à la Claude Code / Codex / OpenClaw;
  `memory_search` reads a bounded `recall_scoped` candidate set, not a full-table scan), and can
  persist a fact with `remember` (optionally source-anchored). Curated `AGENTS.md`/`CLAUDE.md` +
  `<OMNI_HOME>/memories/memory_summary.md` (small, always-injected global digest) +
  `<OMNI_HOME>/profile.md` (self-maintained persona) + `<OMNI_HOME>/MEMORY.md` and a "recent activity"
  block are injected at session start.
- **Tier B — long-term semantic (distilled).** At a turn threshold and at session end,
  `extract_session` asks a configured model for a typed preference/decision/finding contract (M4)
  and records an episodic summary (M3). Offline mode does not guess semantic facts from
  language-specific phrases; it may only preserve a mechanical conversation bridge. All persisted
  content is secret-redacted and de-duplicated. Degraded/partial/tool-limit
  turns and pure external-retrieval turns are **skipped** so they can't seed durable findings. Session
  end also runs hygiene: importance **decay** + near-duplicate **merge**, and rebuilds a
  **global self-maintained profile** — the owner's usage-aware M4 preferences are LLM-merged (diff-style
  forgetting of stale/contradicting items, deterministic fallback offline) into `<OMNI_HOME>/profile.md`,
  injected in every workspace so relevance can improve through use. Type-aware staleness
  (`memory/policy.py`) keeps preferences persistent while empirical findings age and are marked stale;
  `dead_end`/`negative_result`/`idea_evolution` are first-class long-lived types.

**Bounded context, progress-driven task.** Folding is model-aware: the trigger is a fraction
(`memory.autocompact_pct`) of the model's active prompt capacity, inferred from `model.model` and
reduced by the reply reservation. A shared window bounds prompt and response together: `o3` cannot
send 180k of prompt while asking for 32,768 back against a 200k window. Where a model's output cap
approaches its whole window (`gpt-4` at 8,192 for both), the window is split evenly. This per-request
capacity is deliberately independent from `cost.max_total_tokens`, which is optional owner policy
across the whole task and does not decide when context should compact. Provider windows are real
token counts, so the transcript has to be measured in the same units: `tiktoken` is the optional
`tokens` extra, and its absence is the normal case, so the offline estimator is calibrated to a real
tokenizer per byte class (non-ASCII / ASCII word / ASCII punctuation) rather than dividing all bytes
by one number — that single divisor ran 94% high on English research prose and 24% low on Chinese
markdown, which fired every threshold that much early. A cheap first tier —
`microcompact` (`memory.microcompact_pct`) — trims older tool observations in a long single
ReAct turn (keeping the last N, `tool_call`↔`tool_result` linkage intact). If the closed active tool
transcript still reaches the second tier, `RunContextWindow` asks the provider for a concise,
tool-free continuation checkpoint and resumes the same objective in a fresh window. A deterministic
tool-report ledger is the fallback if checkpoint synthesis fails; it remains explicitly untrusted
model context, and raw task/tool events are never
deleted. Checkpoint calls count toward usage but not an explicit semantic iteration allowance, and
`*.context.compacted` makes the rollover auditable without adding a storage table. Between user
turns, `compact_session` first *flushes* durable facts, then
folds older turns into one `content_type=compaction` bridge (originals flagged `compacted`, still in
`replay`); `_history` returns "bridge + last N turns". `/compact` forces it; `/context` reports the
budget. Consolidation runs under a per-session single-flight lock — no new tables, no daemon.

## Research subsystem (`research/`)

What makes omni a *research* agent rather than a coding one. All additive on the existing
per-workspace store + embedding surface (the design doc is
[`research-agent-design.md`](research-agent-design.md)):

- **ROM (`research/store.py`, `storage/models.py`)** — `ResearchStore` is the async CRUD layer over
  the six ROM tables. The graph is `hypothesis → claim → evidence → source/chunk` plus an
  `experiment_runs` ledger. Rows reference sessions/tasks by id (no FK cascades).
- **Corpus (`research/corpus.py`)** — `chunk_text` → embed → `ingest_source`/`ingest_many` (dedup by
  source key + content hash) → `search_corpus` (embedding similarity, keyword fallback offline,
  optional `as_of` date-pin). Chunk size is `research.chunk_target_words`.
- **Connectors (`research/connectors.py` + `research/registry.py`)** — arXiv / OpenAlex / Crossref /
  Unpaywall HTTP clients, normalized into the ROM. A **`ConnectorRegistry`** is the single catalogue
  of curated data sources (a layer distinct from `SkillRegistry`, which owns workflows): it holds each
  `ConnectorSpec` (name/description/capabilities/base URL), enforces the `research.connectors`
  allow-list (kill-switch), and applies **secret-scope** — a connector receives only the secrets it
  declares (`secret_scope`), e.g. Unpaywall/Crossref/OpenAlex get `research.contact_email` and nothing
  else. Skill engines resolve a connector via `engine_util.resolve_connector(ctx, name)` (returns the
  scoped secrets, or `None` when disabled) rather than reading settings directly; the enabled catalogue
  is also surfaced to the model planner so data sources are *discoverable*.
- **Research tools (`research/tools.py`)** — `record_hypothesis`, `record_claim`, `cite_source`,
  `add_evidence`, `search_corpus`, `log_run`. Appended to the builtin tool surface (gated on
  `ctx.db`) so the main loop and native `omni lit` command get them. Each write feeds a memory entry + a NOTEBOOK line
  (best-effort). Source / Claim / Evidence stay model- or skill-owned; inventing them from ReAct
  prose would fail the honesty pass.
- **Host exec recording (`research/host_record.py`)** — Codex records every shell call in the
  session transcript without a separate log step. Omni does the same for the ROM run ledger:
  `ToolGateway` writes an `experiment_runs` row after `bash` / `run_compute` actually ran
  (generic ReAct and skill bash share this boundary). Known-safe reporting commands stay off
  the ledger. File artifacts are already registered from `write_file` / `$OMNI_OUTPUT_DIR`.
- **Verify (`research/verify.py`)** — `verify_session` audits the claim/evidence graph for
  unsupported / contradicted / over-confident claims, and (P2.3) `audit_memory_findings` flags
  *memory* findings whose `payload_ref` doesn't resolve to a source/claim/run — the anti-hallucination
  moat for "remembered facts". It also reports run / source inventory so a compute turn is not
  "empty" merely because no claim was recorded. Surfaced by `omni verify` / `--verify`; it does
  **not** call the model.
- **Threads (`research/threads.py`)** — `build_thread_brief` / `latest_thread_session` rebuild a
  hypothesis-keyed brief (claims + runs + sessions) across conversations for `omni resume --thread`.
- **Bench (`research/bench.py`)** — `run_retrieval_bench` scores the real retrieval pipeline over a
  bundled gold set in a throwaway store (recall@k / MRR); surfaced by `omni bench`.
- **Memory bench (`eval/memory_bench.py`)** — `run_memory_benchmark` scores the persistent-memory
  contract offline on **injection hit + citation hit + zero leakage** across cross-session,
  cross-workspace, cross-channel, isolation, concurrency and offline dimensions; surfaced by
  `omni eval --memory` and gated in CI.
- **Final synthesis (`runtime/final_synthesis.py`)** — native writing deliverable. Beyond a
  document-level `evidence_level` (grounded/contextual/degraded), it labels **each conclusion** as
  `sourced` / `inferred` / `insufficient` from that step's own source/claim/evidence,
  rendering a conclusion-to-evidence block and returning structured
  `provenance_labels` — so a reader can tell evidence-backed claims from reasoning.
- **Artifact review (`cli/commands/artifacts_cmd.py`)** — `omni artifacts preview|diff|versions|review`
  (also as `/artifacts …`): preview text/metadata, unified-diff two text artifacts, list a
  revision/version family, and `review` for health (file/contract/render derivatives, `revision_of`
  link, owning task status) plus the recorded `source/claim/evidence` provenance.

These are reachable from the CLI/REPL as `omni lit/verify/bench` and the `hypo/claim/evidence/run/
source` groups (see [`commands.md`](commands.md)).

## Compatibility (both directions)

- **Our skills → Claude Code / Codex / OpenClaw**: skills are portable in three layers. Copy-only
  mode lets the host agent read `SKILL.md`; portable-runner mode lets it call `scripts/run.py`
  without importing Omni; Omni enhanced mode uses `omni mcp serve`, which exposes every skill plus
  `omni_ask` as MCP tools (`compat/mcp_server.py`). `omni mcp install` registers it into
  `~/.codex/config.toml` and `~/.claude.json`.
- **Their tools → our agent**: external `[mcp_servers.*]` are introspected and surfaced as ReAct
  tools (`compat/mcp_client.py`).
- **Their skills → our agent**: discovery includes Omni-managed roots by default and, with `--all`
  or configured sources, `~/.claude/skills`, `~/.codex/skills`, `~/.agents/skills`,
  `~/.openclaw/skills`, plus project `.claude/skills` / `.agents/skills`; prompt skills run via
  `run_skill` / a ReAct sub-agent using the CC-compatible builtin tools.

See [`compatibility.md`](compatibility.md) for details.

## Installation ownership and uninstall

The installer writes a small ownership record to `OMNI_HOME/install.json`. The uninstall planner
combines that record with the active Python prefix, PATH entry points, the managed Skill-export
manifest, workspace registry, daemon pidfiles, and MCP registration state. Planning is read-only;
execution happens only after confirmation. This keeps package removal separate from research-data
destruction and lets `omni uninstall --dry-run --json` expose the exact operation set.

`omni uninstall` preserves `OMNI_HOME` by default. `--purge` deletes user-level configuration,
secrets, tasks, memory, logs, artifacts, and managed workspaces; `--all-project-data` extends that
scope to registered in-place `.omni` stores. `--everything` combines purge, project-data removal,
all detected package installations, and byte-identical untracked built-in exports. Runtime guards
refuse unsafe home paths and re-check untracked Skill identity immediately before deletion. The
source checkout, unrelated MCP entries, modified external Skills, and external channel gateways
remain outside Omni's ownership boundary. See [`uninstall.md`](uninstall.md).
