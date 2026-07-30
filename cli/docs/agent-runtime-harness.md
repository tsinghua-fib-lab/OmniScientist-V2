# Agent runtime and research harness

This is the authoritative guide to Omni's interaction modes, execution boundaries, recovery,
research-quality evaluation, and domain extensions. It documents behavior implemented in the
runtime, not a future roadmap.

## Runtime invariants

Every CLI, REPL, Feishu, WeChat, and DingTalk turn follows the same harness:

1. Create an `AgentRun` before planning and emit the acknowledgement with its `run_id`.
2. Build and persist an `IntentPlan`, including selected skills, reasons, rejected candidates,
   execution mode, tool policy, and the events the turn's claims must leave behind.
3. Validate the plan and recover by `needs_input` or bounded ReAct handoff; a safety finding is the
   only hard stop.
4. Enforce the execution contract in code: allowed tools, phase budgets, context, provenance,
   failure policy, lifecycle hooks, and interaction mode.
5. Execute inline, as a Skill Execution, as a WorkflowRun the model submitted, as a Child Task, or
   as an artifact transaction.
6. Append planner, tool, workflow, step, Skill Execution, Child Task, artifact, hook, and
   presentation events to the same task event stream.
7. Render one `TurnPresentation` through the channel-specific renderer, then settle the task against
   the durable record.
8. Finish as `succeeded`, `degraded`, `needs_input`, `failed`, `cancelled`, or `interrupted`; a
   usable partial result is not converted into a bare exception.

`/task` lists Tasks (user requests). `/task show <task-id>` shows the readable chain and
`/task show <task-id> --json` returns the Task, plan, events, WorkflowRuns, WorkflowSteps, Skill
Executions, and Child Tasks. Each object is independently inspectable; retryable skill attempts are
stored separately from stable workflow nodes.

Step sequencing inside a turn belongs to the model. It publishes a checklist with the `update_plan`
tool — a list of `{step, status}` items, where `status` is `pending`, `in_progress`, or `completed` —
and replaces it wholesale whenever it learns something new. The tool asks for one `in_progress` step
at a time, but the host only normalizes and renders the list; it enforces nothing, so a revised
checklist needs no validation or repair pass. Trivial single-step requests skip it.

## Interaction modes

| Mode | Behavior | Entry point |
|---|---|---|
| `auto` | Validate and execute the plan | `omni chat "..." --mode auto` or `/mode auto` |
| `plan` | Persist the plan and stop in `awaiting_approval` | `omni chat "..." --mode plan` or `/plan ...` |
| `review` | Use a read-only tool surface and force output review | `omni chat "..." --mode review` or `/review ...` |

Approve a stored plan without changing its task id:

```bash
omni task approve <task-id>
```

In the REPL the equivalent is `/task approve <task-id>`.

## Lifecycle hooks

Hooks are owner-controlled commands in the user configuration. Project configuration cannot add
hooks or raise execution defaults. Commands are parsed with `shlex`, executed without a shell,
receive redacted JSON on stdin, and are bounded by a timeout and output cap.

Supported lifecycle points are:

- `run_start`, `run_resume`
- `pre_plan`, `post_plan`
- `pre_tool`, `post_tool`
- `pre_present`, `post_present`

`pre_plan` and `pre_tool` are deny-capable. A hook can print
`{"action":"deny","reason":"policy reason"}`. Hook execution and its policy decision are written
to task events. The same pre/post tool wrapper covers coordinator tools, prompt-only skill tools,
workflow steps, durable subtasks, and delegated specialists.

```toml
[hooks]
enabled = true
timeout_s = 5.0
failure_policy = "warn" # warn | fail
max_output_bytes = 65536

[hooks.commands]
pre_plan = ["/absolute/path/check-plan"]
pre_tool = ["/absolute/path/check-tool"]
post_present = ["/absolute/path/record-delivery"]
```

Hooks are trusted local extensions, not arbitrary commands supplied by a model or repository.

## Steering and cancellation

Run controls are durable and consumed once at the next safe boundary:

```bash
omni task steer <run-id> "prioritize cited ablation evidence"
omni task cancel <run-id>
```

The ReAct loop applies steering before its next iteration. Deterministic workflows have no semantic
model boundary and therefore accept cancellation only; detached steering is rejected with a
follow-up hint, while the foreground composer transfers an unapplied instruction once to its
next-turn queue. An already-running external engine or compute call reaches its next coordinator
boundary before cancellation takes effect.

## Workflow steps and recovery

Multi-step work is sequenced by the model: it calls `run_workflow` with an ordered step list, and
that call creates the WorkflowRun. Nothing computes a sealed DAG before the turn starts. A step can
declare:

```json
{
  "id": "figure",
  "skill_name": "scientific-figure",
  "input": {"input": "Draw the evidence-backed architecture"},
  "required": false,
  "depends_on": ["search"],
  "optional_depends_on": ["analysis"],
  "allow_failed_dependencies": true,
  "failure_policy": "continue_with_partial",
  "fallback_skill": "",
  "concurrent_safe": true
}
```

The scheduler runs dependency-ready steps in deterministic waves. It only runs steps concurrently
when the step or skill contract explicitly declares `concurrent_safe=true`; unspecified steps stay
serial. `tasks.workflow_concurrency` caps each wave.

Each wave persists a checkpoint. A retry preserves the stable WorkflowStep id and creates a new
Skill Execution id linked through `retry_of`; a delegated step creates a Child Task instead of a
fake `workflow` skill execution. Recovery commands are:

```bash
omni task step <workflow-run-id> <step-id>
omni task retry <child-id>                  # fresh child attempt
omni task retry <workflow-run-id> --step <step-id>
omni task resume <child-id>                 # continue the same child
omni task resume <workflow-run-id> --step <step-id>
```

`retry` creates a new attempt linked by `retry_of`; `resume` keeps the child id and starts from the
persisted checkpoint. Completed upstream steps are reused for step-level recovery. A parent-task
resume that still owes only `artifact.figure` and/or writing may host-fill those deliverables
instead of replaying the checkpoint; a leftover PPTX or poster is not host-fillable and falls
through to a full retry.

## Multi-agent delegation (coordinating → specialist → reviewer)

The coordinating ReAct loop may hand focused subtasks to **specialist** subagents. Each specialist
runs its own bounded ReAct loop with an isolated context (a fresh `task_id`, no shared coordinator or
sibling history) and an independent tool budget, and hands back only a compact summary — the
transcript and raw tool observations are dropped so the coordinator's context stays small. An
optional **reviewer** (LLM-as-judge) scores each summary and can request one bounded revision.
Delegation is offered to the model as tools and is depth-bounded so specialists can fan out one more
level but never recurse without end. All keys below are owner (trusted) configuration; project-local
config cannot raise a trusted ceiling.

```toml
[subagents]
enabled = true            # offer delegation tools at all
max_subagents = 4         # specialists per blocking spawn_subagents call
max_depth = 2             # nesting bound; a specialist at the limit is not offered delegation
concurrency = 3           # parallelism inside one blocking spawn_subagents batch

# Per-specialist budgets (kept below the coordinator's so fan-out stays cheap).
max_iterations = 4
max_tool_calls = 8
max_seconds = 90.0

# Reviewer gate: score each output; below the threshold ask for one bounded revision.
reviewer_enabled = true
reviewer_min_score = 0.5
reviewer_max_revises = 1

# Per-specialist execution defaults (a spec may override these while staying bounded).
default_model = ""
default_compute_profile = "local"
default_isolation = "none" # none | worktree | container

# Async multi-agent (Codex V2 AgentControl parity) — off by default (gray rollout).
async_enabled = false     # also offer the async spawn/wait/message/followup/list/interrupt tools
max_active = 3            # session-tree cap on concurrently executing async subagents
wait_default_s = 30.0     # default wait_subagent timeout (bounded by max_seconds)

[tasks]
workflow_concurrency = 4

[compute_profiles.local]
backend = "local"
timeout_s = 600

[compute_profiles.gpu-container]
backend = "docker"
docker_image = "pytorch/pytorch:latest"
docker_gpus = "all"
fallback_local = false
```

By default the coordinator gets one delegation tool, `spawn_subagents`: a **blocking** fan-out that
runs a batch of specialists in parallel (bounded by `concurrency`, capped at `max_subagents`) and
returns all summaries together.

### Async delegation tools (opt-in)

When `async_enabled = true`, a coordinating turn also gets the fire-and-collect surface, mirroring
Codex V2's `AgentControl` verbs. These are offered only to the coordinator (a turn-scoped control
plane), never to a nested specialist:

- `spawn_subagent` — start one specialist in the background and return its handle immediately.
- `wait_subagent` — block until a named subagent finishes (or the timeout), or omit the name to take
  whichever finishes first. Collecting is explicit and idempotent; it does **not** start a new turn.
- `message_subagent` — deliver a steering instruction to a *running* subagent; it is consumed at the
  subagent's next safe step (it does not start a new turn).
- `followup_subagent` — continue a *finished* subagent: start a fresh specialist seeded with the
  previous result as context and return a new handle.
- `list_subagents` — list this turn's subagents with role, goal, and status.
- `interrupt_subagent` — request cooperative cancellation of one running subagent.

The control plane is **turn-scoped**: `max_active` bounds concurrently executing async subagents
across the whole turn's agent tree, and at turn end any subagent that was spawned but not collected
is joined within a short grace and otherwise cooperatively cancelled, so no background task or
unbilled child survives the turn. Cross-turn long-lived agents are out of scope; durable async work
stays with the skill subtask runtime.

### Model, compute, and isolation

Delegated specialists can select a model, compute profile, and isolation boundary; the owner defaults
above apply when a spec does not override them.

- `none` shares the workspace under the normal tool policy.
- `worktree` creates a detached Git worktree under the project store and retains it for inspection
  and recovery.
- `container` forces executable work through Docker `run_compute`, requires an image, and fails
  closed instead of falling back to local execution. Host `bash`/write tools and host-side
  Python/CLI skill providers cannot be reintroduced through a specialist allow-list.

Specialists are privilege-reduced by default: `write_file`, `edit_file`, `bash`, and `run_compute`
are withheld unless a spec's explicit tool allow-list re-grants them (and `container` mode cannot
re-grant host execution). Worktree filesystem/shell tools receive the selected working directory;
container compute receives the selected compute profile. Read-only host context tools may still
supply inputs to a container specialist. Isolation choices are recorded in run events, and each
specialist is an inspectable Child Task (`omni task list --kind subagent`) with its own `subagent.*`
events and component-level `cost.usage`.

## Research-quality evaluation

The deterministic quality harness is suitable for offline CI:

```bash
omni eval --research-quality
omni eval --quality-input quality.json --json
```

It evaluates:

- citation fidelity: citation resolution, claim coverage, and optional `supported_by` oracles;
- statistical correctness: declared numeric tolerances and common invariants such as probability,
  interval, nonnegative, and sample-size checks;
- reproducibility: `omni.repro_bundle/v1` schema, artifact hash, creation entrypoint, input snapshot,
  environment lock, Python version, and seed when stochastic.

These checks are structural and oracle-driven. They do not claim semantic entailment unless the
fixture supplies a support oracle. Domain-specific semantic judges can extend the result without
changing the stable common schema.

## Domain packs and rich research artifacts

Domain packs add registry-driven planner guidance, recommended connectors, specialist templates,
and artifact expectations. Bundled packs are `core`, `machine-learning`, and `life-sciences`.

```toml
[research]
domain_packs = ["core", "machine-learning"]
connectors = ["arxiv", "openalex", "crossref", "semanticscholar"]
```

The connector registry currently supports arXiv, OpenAlex, Crossref, Unpaywall, PubMed,
Semantic Scholar, bioRxiv, and ClinicalTrials.gov. Domain packs recommend a subset; they do not
bypass connector enablement, credentials, network policy, or tool budgets.

The `build_research_artifact` tool can produce:

- an evidence table in CSV and Markdown with claim/evidence/source identifiers;
- a portable research-notebook snapshot containing hypotheses, claims, sources, and experiment
  runs.

Both retain artifact metadata and provenance references.

## Behavior baseline

The repository's acceptance baseline is offline and deterministic:

```bash
.venv/bin/ruff check cli/src cli/tests
.venv/bin/pytest -q cli/tests
.venv/bin/omni eval --coverage
.venv/bin/omni eval --research-quality
```

Focused runtime regressions live in:

- `tests/agent/test_interaction_lifecycle.py`
- `tests/agent/test_workflow_runtime.py`
- `tests/agent/test_subagents.py`
- `tests/eval/test_research_quality.py`
- `tests/research/test_domain_packs.py`
- `tests/research/test_rich_research_artifacts.py`

Tests must use the mock/scripted LLM and must not require a live network model.
