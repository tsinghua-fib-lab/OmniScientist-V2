# Harness architecture: the nine responsibilities

> A macro view of OmniScientist's agent harness. This document explains *why* the harness is shaped the way it is and *what design choices* the harness makes within each of the nine responsibilities every mainstream agent harness has to cover. It deliberately stops above the file:line layer.
>
> For the line-by-line implementation analysis, see [harness-dimensions.md](harness-dimensions.md). For the user-facing invariants and the as-built module map, see [agent-runtime-harness.md](agent-runtime-harness.md) and [architecture.md](architecture.md).

## 1 Why "the agent" is really a harness

The interesting work in any mature agent system is not the model. The model is data — it turns one prompt into the next prompt plus a small set of named tool calls. Everything around that — when to stop, what tools exist, what the model can remember, what the model is allowed to delete, how a sub-agent is dispatched, what survives a crash — is **harness**. The harness is the small operating system the model runs inside.

This is now a settled view across the major reference designs (Codex, Claude Code / openclaw, HelixForge). The OmniScientist codebase, like those references, spends most of its complexity budget on harness design rather than on prompt engineering.

The design space of "harness" decomposes into nine responsibilities that any serious implementation has to cover. Naming them is the first step; making defensible choices within each one is the rest of the work.

| # | Responsibility | What it answers |
|---|---|---|
| 1 | **Main loop** | When does the model get a turn? When does it stop? When does it escalate? |
| 2 | **Tool layer** | How are capabilities registered as callable interfaces? How is the model told about them? What can the model *do* with them? |
| 3 | **Context manager** | What goes in the model's context window? What gets thrown out when it fills? What persists across turns? |
| 4 | **Permissions and sandbox** | What is the model allowed to touch? What does it have to ask for? What is physically impossible? |
| 5 | **Subagent dispatch** | When does the parent model hand work to a specialist? What does the specialist get to see? What comes back? |
| 6 | **Session and state** | What is a session? What is a task? What survives a crash? What is resumable? |
| 7 | **Observability** | How does a human see what the model did? Where do the traces live? How is cost measured? |
| 8 | **Hooks and interceptors** | Where can the owner insert policy without rewriting the loop? What can a hook see, and what can it stop? |
| 9 | **Error recovery and retry** | When a tool call fails, what happens? When a model hallucinates, what happens? When the system runs out of budget, what happens? |

Every modern agent harness — Claude Code, Codex, openclaw, HelixForge, OmniScientist — covers these nine. They differ in *how* they cover them. The rest of this document walks through each one in turn, presents the design space, and explains the choice OmniScientist made.

If you want a one-screen summary, jump to §3 (Cross-cutting properties). If you want the data flow that ties the nine together, jump to §4.

---

## 2 The nine responsibilities

### 2.1 Main loop

**The problem.** A model is a function: `messages → next_message`. A useful agent is a *process*: it has to iterate (call a tool, read the result, call another tool), to bound its own work (by time, by step count, by cost), and to know when to stop.

**The design space.** Three structural choices dominate:

1. **Bounded vs unbounded iteration.** An unbounded loop is a liability. Every mainstream harness caps iterations; the question is *what to do at the cap* — refuse to continue, or make one more wrap-up pass with no tool use allowed.
2. **Where stop decisions live.** Stop can be a host decision (turn clock, tool-call count, cost ceiling) or a model decision (the model says "done"). Host-controlled is the norm; model-controlled alone is unsafe.
3. **Reflection granularity.** Does the model "reflect" on every step, only on stall, or only on termination? Cheap prompt-level steering is usually enough; expensive meta-prompts that ask the model "are you stuck?" are a common anti-pattern.

**OmniScientist's choice.** A single bound ReAct loop with three time horizons (a stall watchdog, a wall-clock deadline, and a soft-notice threshold), 14 distinct termination reasons, and a final wrap-up LLM call that runs with `tool_choice="none"` so it cannot spawn more tool calls. Reflection is at the prompt level (a set of named steer strings: opening-tool nudge, contract-hunt nudge, lookup-pressure nudge), not at the loop level. The reason for prompt-level steering is recorded in the code itself: "We never send a provider `tool_choice="required"` because not every upstream honors it. We steer with a prompt nudge and verify the call landed, mirroring openclaw's tool_choice contract; codex likewise always sends `"auto"` on the wire."

### 2.2 Tool layer

**The problem.** A model needs to act. The harness must register actions as named interfaces the model can call, give the model a schema for each one's arguments, route the call to real code, validate the result, and return it to the model in a form the model can use.

**The design space.**

1. **Wire format.** OpenAI function-calling, Anthropic tool-use, MCP, or a custom protocol. The choice is mostly driven by the model family, but the harness is what parses and validates.
2. **Schema source.** Schemas can be hand-written, generated from runtime types, or declared alongside the skill. The "declared alongside the skill" pattern is what makes third-party skills viable.
3. **What the model can name vs what it sees.** Whether the model can call a tool is *reach*; whether the model knows the tool exists is *exposure*. Confusing them turns a token-cost optimization (omit a rarely-used schema) into a "model says `unknown tool 'write_file'`" footgun.
4. **How untrusted is the tool's word on success.** Does the model get to declare a call "succeeded" or does the host seal the outcome one-way?

**OmniScientist's choice.** OpenAI-style function-calling on the wire (other formats are translated at the client layer). Schemas are declared alongside skills and compiled once at registration (`Draft202012Validator`, with external `$ref` locked down and all `$recursiveRef` pre-resolved). **Reach and exposure are separate**: a tool can be reachable by name without being in the per-iteration `tools` array. The first time a deferred tool is called, it is automatically promoted to advertised for subsequent iterations. Tool outcomes are sealed one-way by the host: a tool cannot forge a `succeeded` verdict; the host mints the verdict with a `_host_seal`.

### 2.3 Context manager

**The problem.** The model has a finite context window. The harness must decide what to put in it, what to drop when it fills, what to keep on the side, and what to surface from side-storage when relevant.

**The design space.**

1. **What goes in the system prompt.** Identity, tool catalog, plan rules, environment, examples, recalled memory, and project context are all candidates. The composition is a budgeted mix; what gets dropped on overflow is a policy decision.
2. **What goes in the history.** The conversation itself, plus tool calls and observations. The model sees a *transcript*, not the run framework; any meta-state injected into the transcript is a leak.
3. **Compression.** When the context fills, three options: discard (lose data), summarize (LLM or heuristic), or rollover (write a checkpoint, restart from it). Each is appropriate for a different scale of overflow.
4. **Cross-session memory.** What survives a session, and what does not. Treating conversation asides as durable knowledge is a common bug; excluding them is a deliberate design choice.

**OmniScientist's choice.** A six-segment system prompt, three of whose segments are *conditional on the turn catalog* — the prompt the model sees depends on which tools are in scope this turn. The history is loaded as `system + filtered_history + user`, with only five keys carried through (no metadata injection). Compression is two-phase: a per-iteration *microcompact* trims old tool observations (head/tail, with failure observations preserved and research anchors protected) and a cross-iteration *rollover* writes a model-authored JSON checkpoint when the window approaches the limit, with a host-owned `evidence_checkpoint` as the fallback when the model cannot write a valid one. Cross-session memory is a five-layer model (M1 SESSION → M2 TASK → M3 EPISODIC → M4 SEMANTIC → M5 ARTIFACT), and **M1 never crosses sessions** — this is the single most important rule in the memory module.

### 2.4 Permissions and sandbox

**The problem.** The model is a function with no innate judgment. The harness must decide what it is allowed to do freely, what it has to ask permission for, and what is physically impossible.

**The design space.**

1. **The location of the gate.** The gate can be in the system prompt (do not do X), in a policy check at call time (refuse calls matching patterns), or in the OS itself (sandbox, container, VM). Each layer has different costs and guarantees.
2. **Approval granularity.** A binary "always ask" is hostile to flow. A grant-shaped "yes to X" (an exact command, a validated argv prefix, or a turn-scoped workspace trust) lets a user be precise.
3. **What counts as a sensitive path.** Credentials, VCS directories, runtime state. The list is short but non-negotiable; symlink-resolved checks close the TOCTOU bypass where a benign-looking symlink points at `.env`.
4. **Sandbox posture.** Fail-closed (raise if no backend) or fail-open (warn and continue). Production systems almost universally pick fail-closed; the cost of one sandbox escape is larger than the cost of an occasionally-broken session.

**OmniScientist's choice.** A single `ToolGateway` is the choke point for every tool call (model, skill, subagent — no bypasses). The gate has three layers: a `ToolPolicyGuard` that enforces allow/block lists and per-tool budgets, an `ApprovalGate` that asks the user for the genuinely sensitive calls, and an OS-level sandbox (Darwin seatbelt, Linux bwrap, Linux firejail) that physically constrains file writes. Approvals are grant-shaped (exact / argv prefix / task-bash) and IM channels (wechat/feishu/ dingtalk) never inherit task-bash grants. The sandbox is fail-closed: if no backend is available, the harness raises; the fallback is the explicit "no sandbox + WARNING" path, not a silent degradation. The VCS directory (`.git`) is denied at every layer and no `output_roots` configuration can relax it.

### 2.5 Subagent dispatch

**The problem.** Some tasks are too large or too focused for the main loop. The harness must let the parent model delegate work to specialists, share enough context for the specialist to act, and get back a self-contained result.

**The design space.**

1. **Whether the harness has a fixed taxonomy of subagent types** (an "explore" subagent, a "writer" subagent, a "reviewer" subagent) or treats subagent as a generic primitive with free-text role. The fixed taxonomy is easier to reason about; the generic primitive is more flexible.
2. **What the specialist sees.** A full transcript, a summary, a context pointer, or nothing. More context is helpful but expensive and risks the specialist recapitulating the parent's reasoning.
3. **Nesting.** Can a specialist spawn a specialist? Up to what depth? Unlimited nesting is a recipe for infinite loops; no nesting is a missed opportunity for composition.
4. **Quality control.** Is the specialist's output reviewed before it returns? By what — a hard-coded check, an LLM judge, a human? A cheap LLM-as-judge pass catches most of the obvious failures.

**OmniScientist's choice.** No fixed taxonomy. Subagent is a generic primitive with a free-text `role` field; the "reviewer" is an LLM-as-judge loop *inside* the specialist runner, not a separate agent class. The specialist sees no transcript — only the assigned goal, the role, an optional `file_uris` inbox, and an explicit system prompt instructing it to return a self-contained final answer (because "the coordinator sees only the final answer"). The specialist's return is capped at 6 000 characters. Nesting is bounded to depth 2 (parent + one level of specialist). Each subagent gets its own `task_id`, its own resource-lock pool reference, and its own budget. The reviewer gate is one extra loop at most — a cheap quality check, not a deep audit.

### 2.6 Session and state

**The problem.** A real agent workstream spans many turns, many sessions, many background tasks, and crashes. The harness must keep the right state in the right place, survive restarts, and let a human pick up where they left off.

**The design space.**

1. **Where state lives.** In memory, in a file, in a database, in a remote service. Each choice has different durability, scaling, and operational properties.
2. **What a "session" is vs what a "task" is.** Sessions are chat threads; tasks are units of work. Conflating them is a common bug — sessions are user-facing abstractions; tasks are durable records.
3. **Cancel semantics.** Process-local cancel is fragile (a server restart loses it). Durable cancel survives restarts but must be implemented carefully so a benign restart does not accidentally cancel a long-running turn.
4. **Retry lineage.** When a task is retried, what stays the same (the input, the goal) and what is fresh (the attempt number, the events). Without a stable `input_snapshot_json` and a `root_task_id` chain, you cannot reason about a retried task.

**OmniScientist's choice.** A single SQLite database per workspace, with a separate home-level SQLite for control state (schedules, the global task index). WAL mode for concurrent daemon / CLI access; `ApplicationId=0x4F4D4E33` ("OMN3") to stamp the current store shape. Sessions and tasks are distinct objects; a session is a chat thread, a task is a unit of work, and a long-running effort is a task with a `Schedule`. Cancel is durable: process-local cancel vs DB-backed cancel are different things, and a restart does not cancel a long-running turn. Every task carries an immutable `input_snapshot_json`, a retry lineage (`retry_of_task_id / root_task_id / attempt`), and two content-addressed fingerprints (plan + catalog + contract + pre-grants).

### 2.7 Observability

**The problem.** A harness that cannot be inspected is a harness that cannot be debugged. The harness must record what happened in enough detail that a human (or a future agent) can reconstruct the run, attribute cost, find errors, and explain the model's choices.

**The design space.**

1. **What the trace records.** Events, tool calls, plans, errors, costs, latencies. The minimum useful set is small; the maximum useful set is large.
2. **Where it lives.** In a file (text or JSONL), in a database, in a metrics system, in a remote service. Local-first systems pick file + database; SaaS systems pick remote service.
3. **How the model sees it.** Does the model have access to its own trace (for self-correction), or only the harness does? Both are useful for different reasons.
4. **Cost attribution.** Per-call, per-task, per-session, per-component. Without this, you cannot answer "what did this run cost?".

**OmniScientist's choice.** A single append-only `task_events` table, with event names following a `<component>.<verb>` convention. Hook success / failure is itself an event, so a single lifecycle hook's timing and stdout are in the same stream as model calls. A separate `control.sqlite3` holds a cross-workspace `TaskIndex` so `omni task --all` does not have to scan every workspace. Token usage is recorded as a `cost.usage` event per call; metering failures are swallowed so they never block a turn. Logs are emitted as single-line JSON-ish records, with token redaction on by default and a hard rule against `basicConfig` (caller handlers are preserved). The lab notebook (`<workspace>/NOTEBOOK.md`) is a human-readable, git-friendly parallel to the DB events. There is no Prometheus or OpenTelemetry exporter and no web dashboard — the DB and the log file are the interface.

### 2.8 Hooks and interceptors

**The problem.** The owner of a system needs to be able to insert policy without rewriting the loop. The harness must expose a small, well-named set of extension points and make sure those extension points cannot be used to compromise the host.

**The design space.**

1. **What can a hook see?** The tool call (name, arguments, context), the tool result (or just the call), nothing at all. More visibility is more powerful.
2. **What can a hook do?** Allow, deny, modify, log, send an external notification. "Deny" is the powerful one; the rest are observability.
3. **What is the failure mode of a hook?** Silently ignoring a hook failure is the secure default; a hook that crashes the loop is a footgun.
4. **Who can install hooks?** The owner only, or anyone with a config file? Owner-only is the only safe answer for anything that can deny.

**OmniScientist's choice.** A small set of named interception points — pre-tool, post-tool, and a few LLM-event hooks (notices, token deltas) — exposed through `invoke_tool_with_hooks`, which is the choke point every tool call goes through. Hooks are owner-controlled commands (no in-process code patching), executed via `asyncio.create_subprocess_exec` (no shell), with redacted JSON on stdin, single-object JSON on stdout, a hard output cap, and a strict timeout. The decision returned by a hook can `allow` or `deny`; a hook failure is logged as a warning and the main loop continues. After the pre-tool hook, an `ExecutionPolicyFrame` keyed on `sha256(canonical_args)` ensures that the authorization applies to a specific call instance and cannot be replayed by a different task. Other interceptors exist for the same reason: `defend_observation` neutralizes prompt-injection patterns in tool observations; `execution_ownership` pins every subtask to a PID so a live process is never "stolen" by a settler; the MCP client treats an unreachable server as a warning, not a catalog-wide failure.

### 2.9 Error recovery and retry

**The problem.** Things fail. The model hallucinates non-existent tools. The network blips. The user types Ctrl-C. The disk fills. The harness must classify failures, decide which to retry, and decide when to give up.

**The design space.**

1. **How errors are classified.** A small stable set of error classes that the model can branch on (invalid args, retryable I/O, partial success, unpayable, fatal-turn) is much more useful than a free-text error string.
2. **What to retry and how.** Network-level retries are cheap; semantic retries (re-prompt the model) are expensive. Mutating operations should never be retried on network failure alone.
3. **Circuit breaking.** When a particular (tool, args) pair has failed N times, refuse to call it again. The question is what N is and what the key is (tool name alone, or tool + args, or tool + subject argument for meta-tools).
4. **Escalation.** When the loop cannot make progress, what is the failure mode? A wrap-up call, an "escalate" to a background task, a hard stop, or fallthrough to a different skill.

**OmniScientist's choice.** Five host-owned error classes (`INVALID_ARGS` / `RETRYABLE_IO` / `SKILL_FAILED_PARTIAL` / `UNPAYABLE` / `FATAL_TURN`), stable on the wire so model strategy code can branch on them. LLM-side classification is independent (transcript-invalid, authentication, output-cap-truncated) because LLM failures are a different shape. Network-level retries: LLM client gets up to 2 with `Retry-After` preference; tool calls get at most 1, and only on `replay_safe` tools. Two circuit breakers running independently: an executed-failure counter (5 trips the circuit, keyed on `(tool, args)` for most tools, on the *subject argument* for meta-tools so one broken skill doesn't poison the router) and an unexecuted-refusal counter (5 trips a `no_progress` termination). Escalation is a first-class capability: a dedicated `escalate_run` tool the model can call to hand the conversation off to a durable background task, with explicit depth limits to prevent an empty tool loop.

---

## 3 Cross-cutting properties

The nine responsibilities are not independent. Several properties span all of them — they are the design constraints that hold the system together. If you only have time to read one section of this document, read this one.

1. **The model is not the authority.** Every wire format is host-owned and validated by host code. The model's return value is data, not verdict. This appears in the tool layer (sealed outcomes), the permissions layer (the harness is the gate, not the prompt), the error recovery layer (the host classifies failures), and everywhere else.

2. **Reach and exposure are separate.** A tool can be reachable by name without being advertised in the per-iteration schema array. This single distinction is what makes token-cost optimization not break reach.

3. **Every state-mutating path has a host-owned analog.** The host snapshotted the inputs, hashed them, persisted the `start` event, took the file lock, ran the policy, and the post-execution check uses the *same* hash. The model never gets to decide post-hoc that it didn't ask for a write.

4. **Cancel is durable, not process-local.** A server restart must not cancel a long-running turn; a cancel must survive a restart. The harness distinguishes the two and makes sure the right one is used in the right place.

5. **Approvals are grant-shaped, not binary.** "Yes to X" is granted by granting X — exact, argv prefix, or task-bash. A binary "always ask" is hostile to flow; an "ask every time" is hostile to consistency.

6. **Security boundaries fail closed.** When a sandbox backend is unavailable, the harness raises. The fallback is the explicit "no sandbox + warning" path, not a silent degradation. The `.git` directory is denied at every layer and no `output_roots` configuration can relax it.

7. **Memory is layered, and M1 never crosses sessions.** The conversational aside stays in the conversation that produced it. This single rule prevents the most common bug in long-running agent harnesses.

8. **The harness is a small set of well-named primitives, not a framework.** Reach, exposure, replay_safe, mutating, hunt window, contract hunt, lookup pressure, leftover skill pressure — these are the names. Reading the code, you can predict the next design choice because the vocabulary is consistent.

9. **The harness cites its peers.** Comments compare against Codex, Claude Code / openclaw, and HelixForge when a design choice is shared. This is what makes the code auditable against known reference designs.

---

## 4 How the nine responsibilities fit together

The data flow of a single user request crosses all nine responsibilities, but not in a flat order. The same physical operation — calling a tool — has to be both *a turn in the main loop* and *a state-mutating event in the session* and *a permission-gated action* and *a tool-layer routing* and *an observability event* and *a potential hook trigger* and *a potential retry*.

```
                            ┌────────────────────────────────────┐
                            │ 1. Main loop (ReAct)               │
                            │   - bound, three time horizons     │
                            │   - 14 termination reasons         │
                            │   - prompt-level reflection        │
                            └──────────────┬─────────────────────┘
                                           │ calls tools
                                           ▼
   ┌─────────────────┐  pre/post  ┌────────────────────────────────┐
   │ 8. Hooks        │◀──────────▶│ 2. Tool layer                  │
   │   - owner-ctrl  │            │   - reach vs exposure          │
   │   - subprocess  │            │   - sealed outcomes            │
   │   - denyable    │            │   - reach via ToolGateway      │
   └─────────────────┘            └──────────────┬─────────────────┘
                                                  │  every call
                                                  ▼
   ┌─────────────────┐  every call  ┌────────────────────────────────┐
   │ 4. Permissions   │◀────────────│ Single ToolGateway             │
   │   - policy guard │             │   (choke point)                │
   │   - approval gate│             └──────────────┬─────────────────┘
   │   - OS sandbox   │                            │
   └─────────────────┘                            │
                                                  │  writes to
                                                  ▼
   ┌─────────────────┐  reconciles  ┌────────────────────────────────┐
   │ 6. Session/state │◀────────────│ 9. Error recovery              │
   │   - SQLite WAL   │             │   - 5 host-owned error classes │
   │   - lineage      │             │   - 2 circuit breakers         │
   │   - durable cancel│            │   - escalate_run fallback      │
   └─────────────────┘             └──────────────┬─────────────────┘
                                                  │  records
                                                  ▼
   ┌─────────────────┐  from all layers  ┌────────────────────────────────┐
   │ 7. Observability│◀──────────────────│ Append-only task_events        │
   │   - JSONL log   │                   │   - <component>.<verb> naming  │
   │   - cost events │                   │   - control.sqlite3 index      │
   │   - lab notebook│                   │   - log file + DB + notebook   │
   └─────────────────┘                   └────────────────────────────────┘

   Above all: ┌────────────────────────────────────────────────────┐
              │ 3. Context manager                                 │
              │   - 6-segment system prompt, 3 conditional          │
              │   - two-phase compression (microcompact + rollover)│
              │   - 5-layer memory; M1 never crosses sessions      │
              │   - recall is hybrid (keyword + vector + graph)     │
              └────────────────────────────────────────────────────┘

   And on demand: ┌────────────────────────────────────────────────┐
                  │ 5. Subagent dispatch                            │
                  │   - generic primitive, free-text role           │
                  │   - depth ≤ 2; specialist sees no transcript    │
                  │   - reviewer = LLM-as-judge loop (not a class)  │
                  └────────────────────────────────────────────────┘
```

The shape is dense but tractable. The two facts that make it navigable are: (a) every tool call goes through one choke point (`ToolGateway`), and (b) every state-mutating event is durable before it is allowed to run.

---

## 5 What this document is not

This is the macro view. It deliberately stops above the file:line layer. For the line-level implementation analysis (what file each choice lives in, what the exact constants are, what the implementation exceptions look like), see [harness-dimensions.md](harness-dimensions.md). For a chronological view of one user request through the system, see [request-lifecycle.md](request-lifecycle.md). For the as-built module map and request flow, see [architecture.md](architecture.md). For the user-facing runtime invariants, see [agent-runtime-harness.md](agent-runtime-harness.md).
