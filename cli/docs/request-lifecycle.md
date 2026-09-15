# Request lifecycle: from user message to durable record

> A longitudinal view of one user request as it travels through the harness. The nine-dimension analysis in [harness-architecture.md](harness-architecture.md) is a *cross-section* — what each responsibility looks like at rest. This document is a *trace* — one request, six phases, what crosses each boundary, and how the harness keeps the system consistent under failure.
>
> For the as-built module map, see [architecture.md](architecture.md). For the user-facing runtime invariants, see [agent-runtime-harness.md](agent-runtime-harness.md). For the line-by-line implementation, see [harness-dimensions.md](harness-dimensions.md).

## 1 Why a lifecycle view

A real user request does not arrive at a single function. It arrives at a CLI parse, or an IM webhook, or a REPL command. It is normalized into a session, attached to a principal, and given a `Task`. It is planned, validated, executed, observed, settled, rendered, and persisted. The same request might spawn subagents, schedule background work, trigger human approvals, and survive a server restart.

The nine-dimension analysis is necessary but not sufficient for understanding this. It tells you what each responsibility *is*; it does not tell you what survives a crash, what crosses a process boundary, or where the cancel hooks live. Those are *longitudinal* properties — they only make sense in the context of time.

This document follows one user request from arrival to durable record in six phases. Each phase has explicit inputs, outputs, the boundary it sits at, and the failure modes the harness handles. At the end is a table that maps each phase back to the nine responsibilities, so a reader of either document can navigate to the other.

## 2 The six phases

```
   Phase 1         Phase 2         Phase 3         Phase 4         Phase 5         Phase 6
   Intake          Plan            Execute         Settle          Render          Persist
   ──────          ────            ───────         ──────          ──────          ──────
   channel ──▶  IntentPlan  ──▶  ReAct loop  ──▶  terminal   ──▶  TurnPresent  ──▶ committed
   principal        capability      tool dispatch    status          channel         + global
   session          validation      subagent         children         notification    updates
   user msg         needs_input     observability    settle_*
                                    escalate_run
                                    hooks
                                    circuit break
```

The diagram is a simplification. In practice the phases are re-entrant — a `Plan` can produce a `needs_input` that loops back to `Intake`; an `Execute` can `escalate_run` into a new background task that runs all six phases again; a `Settle` can fail and re-execute; a `Persist` can lose a race and roll back. The arrows above are the happy path.

### 2.1 Phase 1 — Intake

**Inputs.** A user message from one of the entry channels (`omni chat`, the REPL, IM platforms: Feishu / WeChat / DingTalk, or a programmatic API call). The message is a string plus a channel-specific envelope (sender id, thread id, attachments, time).

**Boundary.** The channel-specific adapter turns the envelope into a normalized `Session` request. This is the boundary between the *outside* of the system and the *inside*.

**What the harness does here.**

- **Principal resolution.** Each channel maps to a `principal`: `local` for CLI, `<channel>:<external_key>` for IM. Memory isolation is keyed on this.
- **Session continuity.** Same-key IM messages reuse the most recent active session; CLI defaults to a new session unless one is named. The session id becomes the durable key for all subsequent state.
- **Project context.** The active workspace is resolved (CWD for CLI, the most recent `omni` invocation in this directory, or a named project under `~/.omni/projects/`). The workspace is the boundary for the harness's `output_root` and the sandbox's writable subpath list.
- **First prompt assembly.** The system prompt is built from the six segments described in [harness-architecture.md §2.3](harness-architecture.md#23-context-manager) — identity, tool catalog, planning rules, environment, self-knowledge, and behavior — with the three conditional segments reflecting which tools are in scope this turn.

**Outputs.** A `Session`, a `Principal`, a `Workspace`, an initial system prompt, and a `Task` row in the durable record.

**Failure modes.**

- Channel unreachable (IM webhook down). The harness queues the message and returns 5xx; it does not start a `Task`.
- Invalid message format. The harness returns a structured error to the channel; no `Task` is created.
- Workspace not found. The harness creates one in `~/.omni/workspaces/` keyed to CWD; the failure mode is the *user* not being in a VCS root, which the harness tolerates.

### 2.2 Phase 2 — Plan

**Inputs.** The user message, the session context, the project context, the workspace's installed skills, and the catalog of available tools.

**Boundary.** The boundary between "I know what the user wants" and "I have a concrete plan to do it". The output is an `IntentPlan` that has been validated, has explicit rejected candidates, and has explicit events the turn's claims must leave behind.

**What the harness does here.**

- **Capability selection.** The harness determines which skills and tools are needed and which are deliberately rejected (and why). The plan is *self-documenting*; a reader can see what was considered and not picked.
- **Mode selection.** The interaction mode is `auto` (run the plan), `plan` (persist the plan and stop in `awaiting_approval`), or `review` (read-only surface, force output review). The mode comes from the channel (CLI flag, REPL command, IM) or the user profile.
- **Policy binding.** A tool policy is bound to the plan (allow list / block list / per-tool budgets). The plan owns its policy; the policy cannot be widened at execute time.
- **Validation.** The plan is validated for safety (forbidden actions, unsafe scope), preconditions (required skills installed, required secrets present), and idempotency. A safety finding is the only hard stop.
- **Recovery paths.** If the plan is incomplete, the harness has three ordered responses: `needs_input` (the user must clarify), `bounded ReAct handoff` (the plan is good enough; let the model fill gaps), or `awaiting_approval` (persist the plan, stop).

**Outputs.** A validated `IntentPlan` persisted to the durable record, with a `TaskORM` row that has the plan, the rejected candidates, the execution mode, the tool policy, and the events the turn's claims must leave behind. If the mode is `plan`, the task is in `awaiting_approval`; the user must `omni task approve <task-id>` to advance.

**Failure modes.**

- **Safety finding.** A hard stop. The plan is not persisted; the user is told what was wrong and what to do.
- **Missing capability.** The plan either degrades gracefully (omit the affected step) or returns `needs_input` with the specific gap.
- **Plan too large.** The harness caps plan size; an over-large plan is split into multiple tasks, each with its own plan and approval boundary.

### 2.3 Phase 3 — Execute

**Inputs.** The validated `IntentPlan`, the workspace, the tool surface (built from the skill registry, the schedule registry, the MCP servers, and external integrations), the model binding (provider, model, harness-side knobs), and the system prompt.

**Boundary.** The boundary between "I have a plan" and "I have made progress". The output of this phase is a sequence of `TaskEventORM` rows recording each tool call, each cost event, each hook firing, each subagent spawn, and each model turn.

**What the harness does here.**

- **The main loop.** A bound ReAct loop with three time horizons (stall watchdog, wall-clock deadline, soft notice), prompt-level reflection (named steer strings), and 14 distinct termination reasons. The loop drives the whole phase.
- **Tool dispatch.** Every tool call goes through `ToolGateway` as the single choke point. The gateway enforces policy, runs approval for sensitive calls, persists the `start` event, executes, and persists the `done` event. Mutating calls require the start event to land; mutating calls are never retried on transient failure.
- **Subagent dispatch.** The model can spawn specialists via `spawn_subagents` (blocking batch) or `spawn_subagent` / `wait_subagent` / `interrupt_subagent` (async fire-and-collect). The parent loop is paused for blocking spawns and resumed on return. Async spawns are tracked in `SubagentControl` and harvested at turn end.
- **Escalation.** The model can call `escalate_run` to hand the conversation to a durable background task. The background task inherits the parent's ROM (Research Object Model) and runs through the same scheduler, so the IM / cron channels see one durable task instead of an in-memory continuation.
- **Hooks.** Pre-tool and post-tool hooks fire on every call. Owner-controlled commands with subprocess isolation, redacted JSON envelopes, and strict timeouts. The hook decision is `allow` / `deny`; failures are logged and the loop continues.
- **Circuit breaking.** Two independent counters: an executed-failure counter (5 trips the circuit on `(tool, args)`, keyed on the *subject argument* for meta-tools) and an unexecuted-refusal counter (5 trips `no_progress`).
- **Observability.** Every step appends a `task_event` row. Cost is recorded as a `cost.usage` event per call. Hook success/failure is itself an event. The transcript is being assembled in the `messages` array for the next LLM call.

**Outputs.** A sequence of `TaskEventORM` rows (tool calls, cost events, hook events, subagent events, model turns), a tool trace attached to the `Task`, an updated research ledger (sources, claims, evidence, artifacts), and an `AgentLoopResult` describing the termination.

**Failure modes.**

- **Tool error.** Classified into one of five host-owned error classes. Some retry (network), some don't (mutating, unknown tool, policy-rejected). The classification is host-owned; the model sees a remediation hint, not a free-text error.
- **Model hallucinates a tool name.** The preflight rejection bumps the unexecuted-refusal counter. Five in a row terminate the loop with `no_progress`.
- **Budget exhausted.** Wall clock, step count, cost, or context window. The loop runs a wrap-up call with `tool_choice="none"` and then terminates. A failed wrap-up is replaced by a salvage stub.
- **Stall or timeout.** Same wrap-up path; the model is given one more chance to land a final answer.
- **Cancel.** Process-local cancel or durable cancel (the latter survives restarts). In-flight tools are sent `interrupted_tool_payload`. The task settles as `cancelled` or `interrupted`.
- **Escalation.** The model called `escalate_run`. The loop terminates with `kind="escalated"`; the background task is created and tracked in the scheduler. The current turn's events are sealed; the background task starts fresh with a new event stream.

### 2.4 Phase 4 — Settle

**Inputs.** The `AgentLoopResult` from Phase 3, the research ledger, the subagent results, the schedule checkpoint (if any), and the durable task record so far.

**Boundary.** The boundary between "I have run the plan" and "I know what the answer is". The output is a `terminal_status` for the task, derived deterministically from the result and the durable state.

**What the harness does here.**

- **Child reconciliation.** If subagents were spawned, the harness waits for child tasks to reach a terminal state before settling the parent. The parent cannot be `succeeded` while a child is still in flight.
- **Terminal status derivation.** The status is derived from the `AgentLoopResult.kind`:
  - `kind="text"` with no degradation warnings → `succeeded`
  - `kind="text"` with degradation warnings → `degraded`
  - `kind="needs_input"` → `needs_input`
  - `kind="error"` → `failed`
  - `kind="escalated"` → the parent task is the background task; settle is deferred.
  - Cancelled / interrupted → `cancelled` / `interrupted`
- **Artifact transactions.** If the run produced artifacts (figures, slides, reports, papers), each is recorded as a durable `ArtifactORM` row with content hash, provenance, and an event trail back to the task.
- **Research ledger merge.** Sources cited, claims made, evidence gathered, and artifacts produced are folded into the durable record and become visible to future tasks via recall.

**Outputs.** A `terminal_status` on the `TaskORM` row; settled subagent results; an updated research ledger; a list of artifacts.

**Failure modes.**

- **Child never terminates.** The child has a deadline and a heartbeat; if the deadline is exceeded, the child is reconciled as `interrupted` and the parent is settled as `degraded` with a warning that a child did not finish.
- **Settlement conflict.** The orchestrator-reported status and the durable record may disagree. The durable record wins (the comment in the code: "settle from the turn's own end once children are terminal").

### 2.5 Phase 5 — Render

**Inputs.** The `Task` with its `terminal_status`, the `AgentLoopResult.content`, the list of artifacts, the list of citations, and the channel that originated the request.

**Boundary.** The boundary between "I have an answer" and "the user sees an answer". The output is a `TurnPresentation` specific to the channel.

**What the harness does here.**

- **Channel-specific rendering.** CLI gets a TUI or plain-text output; REPL gets an interactive output; IM gets a message formatted for the channel (with artifact attachments and citation formatting). The rendering is a function of the channel and the result, not a configuration.
- **Notification.** If the task produced artifacts or reached a noteworthy state, the harness may push a notification to the channel (or to an inbox if the channel is offline). The notification is *one* of five: `InboxNotifier` (default, JSONL in `~/.omni/inbox/`), or any owner-registered channel.
- **Citation rendering.** Citations are formatted according to the channel's conventions (Markdown links, plain text, or IM-friendly inline).

**Outputs.** A `TurnPresentation` written to the channel; optionally, a `TaskNotification` written to the inbox.

**Failure modes.**

- **Render error.** The harness logs the error; the user sees a fallback "answer is in your work directory" message and the task is still settled.
- **Channel offline.** The presentation is queued; the user sees it when the channel comes back. This is one of the few paths where the system intentionally becomes a "store and forward" queue.

### 2.6 Phase 6 — Persist

**Inputs.** Everything from Phases 1–5, plus any session-level maintenance (memory consolidation, notebook updates, schedule advancement).

**Boundary.** The boundary between "the turn is over" and "the system is ready for the next turn". The output is the durable record of the turn plus any cross-session state updates.

**What the harness does here.**

- **Atomic commit.** All writes from the turn commit together, or none do. The `persist_scope` context manager in `cancel_persist.py` is the mechanism; it makes parent-cancel-during-write safe.
- **Session memory extraction.** Substantive user messages are flushed to M3 (episodic) and M4 (semantic) by `MemoryService.extract_session`. Failures, retrieval-only turns, and degraded turns are filtered.
- **Notebook append.** The lab notebook is appended with a human-readable block summarizing the turn. The notebook is git-friendly and serves as the "summary view" the system prompt can quote.
- **Schedule advancement.** If the task was triggered by a schedule, the schedule's `next_due_at` is advanced and `run_count` is bumped. The advance is in the same transaction as the rest of the commit.
- **Memory maintenance (background).** Every 8 turns the session memory is consolidated. At session end the maintenance is enqueued (interactive channels) or run synchronously (CLI). Maintenance runs `decay_and_dedup`, `rebuild_user_profile`, `compact_memory_file`, and `refresh_global_summary`, with a `global_memory_lock` for cross-process safety.

**Outputs.** The committed turn, the updated memory layers, the advanced schedule, the appended notebook, the enqueued maintenance (or its result, if run synchronously).

**Failure modes.**

- **Commit failure.** The whole turn rolls back; the user sees an error and the system is in a clean state.
- **Memory extraction failure.** Extraction errors are logged but do not block commit; the turn is durable without extracted memories (the M1 row stays; recall is degraded, not broken).
- **Notebook append failure.** Same — logged, not blocking.

## 3 What crosses a phase boundary

State is constantly in motion, and what survives each boundary is the key to understanding durability.

| Boundary | What crosses | What is rebuilt |
|---|---|---|
| **Into Intake** | channel envelope | (none) |
| **Intake → Plan** | Session, Principal, Workspace, first prompt | (none — durable) |
| **Plan → Execute** | IntentPlan, Task row, mode, policy, model binding | (none — durable) |
| **Execute → Settle** | `AgentLoopResult`, tool trace, subagent results, research ledger, event stream | in-memory messages array |
| **Settle → Render** | `Task.terminal_status`, `result.content`, artifacts, citations | intermediate tool outputs |
| **Render → Persist** | `TurnPresentation`, `TaskNotification` (if any) | channel connection |
| **Persist → next Intake** | committed `Task` row, updated memory, advanced schedule | the whole `Task` is durable; only the `messages` array is rebuilt |

Two facts make the system tractable. First, the `Task` row is the canonical handoff: it is the one thing that every phase reads and writes, and it is durable before the next phase can see it. Second, the in-memory state (the `messages` array, the tool surface, the model binding) is *rebuilt* at every restart — the system is designed to be replayable from the durable record.

## 4 How the nine responsibilities manifest in each phase

This table is the navigation aid between this document and [harness-architecture.md](harness-architecture.md). A reader of one can find the relevant phase in the other.

| Responsibility | Intake | Plan | Execute | Settle | Render | Persist |
|---|---|---|---|---|---|---|
| **1. Main loop** | — | — | drives the phase | — | — | — |
| **2. Tool layer** | schema assembly | capability selection | dispatch via `ToolGateway` | result reconciliation | citation render | — |
| **3. Context manager** | first prompt assembly | plan recap | system prompt; context compression | — | — | memory extraction; notebook append |
| **4. Permissions/sandbox** | principal resolution | policy binding | approval gate; OS sandbox | — | — | — |
| **5. Subagent dispatch** | — | subagent plan entries | spawn/wait/interrupt | child reconciliation | subagent summaries in render | subagent events in event stream |
| **6. Session/state** | session + principal + workspace | Task row creation | event stream, lineage | terminal status | — | atomic commit, schedule advance |
| **7. Observability** | session open event | plan persisted event | every step, cost event, hook event | settle event | render event | commit event |
| **8. Hooks** | — | — | pre-tool / post-tool on every call | — | — | — |
| **9. Error recovery** | channel error handling | safety finding; needs_input | classification; circuit; escalate | settlement conflict; missing child | render fallback | commit rollback |

A blank cell means the responsibility has no specific work in that phase. A "—" means "the responsibility's properties are active but no specific work happens" (for example, permissions are active during Render in the sense that the renderer cannot call mutating tools, but no permission check fires because there is no tool call).

## 5 What this document is not

This is the lifecycle view. It is a longitudinal slice through the same nine responsibilities that [harness-architecture.md](harness-architecture.md) covers as a cross-section. It deliberately stops above the file:line layer and below the policy level. For the as-built module map, see [architecture.md](architecture.md). For the user-facing invariants, see [agent-runtime-harness.md](agent-runtime-harness.md). For the line-by-line implementation analysis, see [harness-dimensions.md](harness-dimensions.md).
