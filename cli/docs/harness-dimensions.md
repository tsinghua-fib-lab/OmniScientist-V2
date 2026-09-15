# Harness by dimension: how OmniScientist wires its agent

> A technical analysis of OmniScientist's agent harness, organized by the nine responsibilities every mainstream agent harness has to cover: main loop, tool layer, context management, permissions/sandbox, subagent dispatch, session/state, observability, hooks, and error recovery. Each section ends with the design choices that distinguish this codebase, and the file:line references that back them. Complement to [agent-runtime-harness.md](agent-runtime-harness.md) (user-facing invariants) and [architecture.md](architecture.md) (module map).

The discussion that follows treats "agent" as a *harness* problem, not a *model* problem. OmniScientist is local-first, single-process, single-SQLite; the interesting work is in the seams between the model, the tools, and the durable record. Where a design choice is borrowed from a known open-source harness (Codex, Claude Code / openclaw, HelixForge), the comments in the code call it out explicitly — those callouts are quoted below when they matter.

---

## 0. Design philosophy, before the dimensions

Three threads run through every dimension and are worth naming up front:

1. **The model is not the authority.** Every wire format (tool schema, event payload, system prompt block) is hand-rolled, validated by host code, and never trusts the model's return value. A `ToolResultEnvelope` is sealed (`cli/src/omni/core/tool_result.py:144-168`) by `_host_seal` so a malicious tool can't forge a `succeeded` outcome; `classify_tool_error` (`cli/src/omni/core/tool_errors.py:86-127`) inspects host-controlled fields before it reads any model-produced strings.
2. **Reach and exposure are separate.** A tool can be *reachable* (dispatchable by name) without being *advertised* (carried in the per-iteration `tools` array). The split is what makes `deferred` tools a token-saving device rather than a "model doesn't know this exists" footgun (`cli/src/omni/core/react_agent.py:209-225`; see Dimension 2).
3. **Every state-mutating path has a host-owned analog.** The model can request a write, but the host snapshotted the inputs, hashed them, persisted the `start` event, took the file lock, ran the policy, and the `admitted_arguments_hash` is what the `done` event will be checked against (`cli/src/omni/runtime/tool_gateway.py:309-475`). The model never gets to decide post-hoc that it didn't ask for a write.

The whole harness is engineered to keep these properties intact as the surface grows. Where a feature would erode them, the feature is gated (e.g. deferred tools cannot be policy-blocked while staying deferred; mutating tools are rejected if the start event can't be persisted; sandbox failure is fail-closed).

---

## 1. Main loop (agent loop / ReAct)

### 1.1 Entry point and control flow

`ReActLoopAgent` in `cli/src/omni/core/react_agent.py:323` is the bound ReAct-style loop. It is instantiated by `orchestrator._handle_turn_impl` in `cli/src/omni/agent/orchestrator.py:1410-1437` and given the per-session knobs (`max_iterations`, `max_tool_calls`, `max_seconds`, `stall_timeout_s`, `soft_timeout_s`, `finalization_timeout_s`, `finalization_attempts`, `soft_token_limit`, `context_rollover_token_limit`, `microcompact_keep_tool_results`, `no_progress_threshold`, `require_opening_tool`, `owes_scientific_outputs`, `bound_skills`, `fact_feed`).

A single iteration of the loop, in order:

1. `register_clock(TurnClock(...))` in `react_agent.py:452-494`; the loop body runs inside `ExecutionControl.run` so the durable `cancel` row is polled.
2. While `iteration < self._max_iterations` (`react_agent.py:564`):
   - `take_steering` drains any `ExecutionControl.steer` lines and prepends them as a `user` message (`react_agent.py:575-582`).
   - If the soft-timeout window has been reached, emit one `notice` `kind=soft_timeout` (`react_agent.py:586-603`).
   - If `clock.expired()`, switch to `_terminate_or_synthesize(reason="timeout")` (`react_agent.py:607-618`).
   - `normalize_tool_transcript(messages)` patches unclosed `tool_call` / `tool_result` pairs so the next LLM call can't see a broken transcript (`react_agent.py:619-630`).
   - `_maybe_microcompact` shrinks old tool observations (`react_agent.py:631` → `compaction.microcompact_tool_results:148-184`).
   - `_maybe_rollover_context` folds a near-overflow window into a model-written JSON checkpoint (`react_agent.py:632-642` → `run_context.py:13-153`).
   - Cumulative token/cost check: `_usage_limit_reason` (`react_agent.py:1722-1728`), called at row 643-669 (pre-iter) and 1130-1142 (post-iter).
   - If `require_opening_tool`, inject `_OPENING_TOOL_DIRECTIVE` (`react_agent.py:692-699`).
   - `chat_with_tools_stream` or `chat_with_tools` runs one round of the model (`react_agent.py:718-740`).
   - Three branches: pure text terminates (`react_agent.py:828-903`), an `escalate_run` tool call becomes an `AgentLoopResult(kind="escalated")` (`react_agent.py:914-939`), a batch of tool calls is dispatched (`react_agent.py:946-1276`).
3. Tool batch: `_preflight_rejection` (unknown tool / bad args / circuit trip) at `react_agent.py:1815-1877`, then `ToolExecutionBudget.admit` (`react_agent.py:956-960`), then the `start` event is written. If the start event fails to persist *and* the tool is mutating, the call is blocked with `tool_start_not_persisted` (`react_agent.py:974-987`).
4. `_dispatch_batch` issues the surviving calls concurrently (`react_agent.py:1760-1813`); read-only tools run in parallel, write tools are serialized through `_SERIAL_TOOLS` (`react_agent.py:2270-2294`).
5. Each call writes a `done` event; tool messages are appended to the transcript for the next round (`react_agent.py:1060-1078`).

`replay_safe` tools are retried at most `_TOOL_RETRY_MAX = 1` times (`react_agent.py:67-68`, `1900-1901`) with exponential backoff (`_TOOL_RETRY_BASE_DELAY * 2**attempt`, `react_agent.py:1936-1938`). Mutating tools are never replayed on network jitter — the start event already recorded intent, and double-firing a write would be worse than failing.

### 1.2 Termination reasons

All termination is funnelled through `cli/src/omni/core/termination.py`. The loop's own `terminated_reason` and `kind` are mapped to user-visible `TERMINATION_LABELS` (line 105-150), to `BUDGET_EXHAUSTED_REASONS → _NEXT_ACTIONS` ("re-run with a larger X budget"), and to a final `execution_outcome_status ∈ {succeeded, degraded, failed, cancelled, interrupted}` (line 77-89). The concrete reasons produced by the loop are:

| `terminated_reason` | Where it fires | Notes |
|---|---|---|
| `done` | Pure text after `_OPENING_TOOL_*` satisfied | `react_agent.py:899-903` |
| `max_iterations` | `while iteration < self._max_iterations` exits | `react_agent.py:1278-1286` |
| `max_tool_calls` | `ToolExecutionBudget.admit` returns no slots; recorded as `run_hard_budget_exhausted` | `react_agent.py:1019-1038`, `1165-1177` |
| `max_total_tokens` / `max_cost` | `_usage_limit_reason`; also wraps up via `_terminate_or_synthesize` | `react_agent.py:122, 1722-1728` |
| `no_progress` | `stalled_patterns` repeated ≥ `no_progress_threshold` (default 2) | `react_agent.py:1080-1108`, `1179-1190` |
| `timeout` | `TurnClock.expired()`; or LLM `TimeoutError` while `clock.expired()` | `react_agent.py:607-618`, `755-779` |
| `stalled` | `IdleWatchdog` silence beyond `stall_timeout_s`; or LLM `TimeoutError` while still `trace`-able | `react_agent.py:762-779`; `core/llm/idle.py:30-123` |
| `cancelled` | `ExecutionControl.request_cancel` or durable `cancel` row | `execution_control.py:113-141`; `react_agent.py:741-754, 2029-2053` |
| `interrupted` | `dispatch_cancelled` while a tool was in flight | `react_agent.py:992-1014`; `tool_result.py:36-60` (`TOOL_NOT_STARTED` / `TOOL_OUTCOME_UNKNOWN`) |
| `escalated` | model called `escalate_run` | `react_agent.py:914-939` |
| `malformed_tool_calls` | `function.name` empty; corrected up to `_MAX_MALFORMED_CORRECTIONS = 2` | `react_agent.py:845-866` |
| `required_opening_tool_missing` | `require_opening_tool` on, but no productive observation | `react_agent.py:867-881` |
| `output_cap_truncated` | `truncated_by_output_cap` | `react_agent.py:882-893`; also re-checked in `_synthesize_final` (`react_agent.py:1422-1433`) |
| `synthesized_<reason>` | `_terminate_or_synthesize` ran the wrap-up LLM | `react_agent.py:1288-1438`; `_COMPACT_WRAP_REASONS` triggers pre-compression |

The wrap-up LLM is the safety net for every "spend" reason. Budget exhaustion, stall, and timeout all land in `_COMPACT_WRAP_REASONS = _BUDGET_REASONS | {"stalled", "timeout"}` (`react_agent.py:126`) and trigger a `microcompact_tool_results(keep_last=2, max_chars=400)` before the final synthesis call (`react_agent.py:1367-1374`). The final synthesis uses `tool_choice="none"` so it cannot spawn more tool calls; if it still fails after `finalization_attempts` rounds, `_salvage_content` produces a stub result rather than empty output.

### 1.3 Reflection, steer, and naming pressure

The loop never sends a wire `tool_choice="required"`. The reason is given in `react_agent.py:150-154`:

> We never send a provider `tool_choice="required"` because not every upstream honors it (some reject it with a hard 4xx). Instead we steer with a prompt nudge and verify the call landed, mirroring openclaw's tool_choice contract; codex likewise always sends `"auto"` on the wire.

The prompt nudges that act as "reflection":

- `_OPENING_TOOL_DIRECTIVE` + `_OPENING_TOOL_CORRECTION` (`react_agent.py:155-162`); a single corrective re-prompt (`_MAX_OPENING_CORRECTIONS = 1`).
- `_MALFORMED_TOOL_CALL_CORRECTION` (`react_agent.py:173-178`); up to `_MAX_MALFORMED_CORRECTIONS = 2`.
- `CONTRACT_HUNT_STEER` (`react_agent.py:94-99`) — triggered when trailing `_CONTRACT_HUNT_TOOLS = {find_skill, docs_search, docs_read, glob, search_tasks, list_dir}` appears ≥ 2 times.
- `CONTRACT_NATIVE_WRITE_STEER` (`react_agent.py:103-107`) — for an empty `find_skill` after a card has been returned; the right answer is `write_file` or `run_skill` with the input_schema already returned.
- `CONTRACT_HUNT_CONSUME` / `CONTRACT_HUNT_STOP` (`react_agent.py:111-118`) — the Codex Stop-hook analog, fired by `_replay_hunt_consume` (`react_agent.py:1678-1694`) when a research feed reports debt.
- `LOOKUP_STEER` in `cli/src/omni/core/scientific_progress.py:66-72` — `LOOKUP_TOOLS = {memory_search, memory_get, search_tasks, list_recent_tasks, get_task, get_subtask, open_artifact, list_session_artifacts}` accumulating, gated by `lookup_pressure` (line 122-141).
- `bound_skill_steer` (`scientific_progress.py:82-89`) plus `leftover_skill_pressure` intercepts a `bash`/`run_compute` attempt to write a deliverable that's already bound to a skill (`react_agent.py:1254-1259`).

Stall detection: signature is hashed as `<name>:<json-dump-args-sorted-key>` (`react_agent.py:1082-1085`) so long error texts don't drift the key. `_hunt_window` + `_contract_hunt_pressure` (`react_agent.py:2115-2204`) distinguish a disjoint second `find_skill` for a different deliverable (setup for `figure` then `slides`) from a repeat lookup of the same contract — the latter is BUG-11 territory and gets steered.

### 1.4 Background escalation

`ESCALATE_RUN_TOOL_NAME = "escalate_run"` (`react_agent.py:65`) is a purpose-built tool. Schema is `build_escalate_run_tool_spec` (`react_agent.py:304-320`); it is only injected into the catalog when `allow_escalation=True` (`react_agent.py:510-511`). When the model calls it, `AgentLoopResult.kind = "escalated"` with `escalated_goal` / `escalated_reason` carried through.

The handoff is in `cli/src/omni/agent/turn_escalate.py:12-61`:

- `maybe_escalate_run` calls `agent.tasks.create_task(kind="escalated", depth=parent.depth+1)`.
- If the parent is already `escalated` *or* depth ≥ 2, recursion is rejected (an empty turn with no tools is the failure mode the guard exists for).
- `inherit_research_ledger` propagates the Research Object Model (ROM) pointers — claims, sources, evidence, artifacts — so the background turn builds on the same record.
- `asyncio.create_task(_run_escalated_turn, name="escalate:<id>")` runs `agent.handle_turn(drain_tasks=True, origin="schedule")` in the background, routed through the same scheduler so the cron/IM channels see one durable task instead of an in-memory task that disappears on reload.

### 1.5 Three-layer time budget

The runtime distinguishes three time horizons (`react_agent.py:370-380` plus `_react_max_seconds` in `orchestrator.py:1532-1544`):

- **Stall watchdog.** `IdleWatchdog` + `await_with_idle` (`cli/src/omni/core/llm/idle.py:30-123`). The loop's `_on_delta` (`react_agent.py:703-706`) and `on_activity=watchdog.tick` (`react_agent.py:723`) reset it on every token. Streaming responses require a wire token sink or the watchdog to be enabled; `use_stream` (`react_agent.py:715-717`) chooses. `retries_on_idle` in the client sets `wait_stall = 0` for reconnect attempts (`react_agent.py:730-734`).
- **Hard wall clock.** `TurnClock(max_seconds)` (`cli/src/omni/core/turn_clock.py:38-42`) is a single monotonic deadline. `clock.expired()` (line 56-58) trips `_terminate_or_synthesize`. The `pause_enter` / `pause_exit` reference counter (line 65-76) keeps the approval-gate wait from consuming the wall clock; `pause_clocks` + `ContextVar` (line 89-118) share the pause across tasks.
- **Soft notice.** At the top of each iteration the loop checks `clock.remaining() <= max_seconds - soft_timeout_s`; if true, one `notice kind=soft_timeout` is emitted (the `soft_notified` flag prevents repeat) and the loop continues — soft timeout does not stop the loop, it just informs the model that time is short.

The `finalization_timeout_s = 45` and `finalization_attempts = 2` knobs (`react_agent.py:380-381`) are the reserves for the wrap-up LLM call; if the synthesis fails twice, `_salvage_content` writes a stub so the user gets a non-empty terminal state.

### 1.6 Cancellation

`ExecutionControl.run` (`cli/src/omni/core/execution_control.py:166-201`) launches the coroutine via `asyncio.ensure_future` and polls the durable control table for a `cancel` row. `request_cancel` is process-local; the durable side (`_durable_cancel`, `execution_control.py:132-136`) is what gives the cancel authority across process restarts. `delivered_control_ids` (line 73-86) lets the upper layer distinguish "owner pressed stop" from "server restarted" — the latter should *not* cancel a background turn.

`asyncio.CancelledError` is funnelled to `_cancelled_result` (`react_agent.py:741-754, 2029-2053`). For tools already in flight, the loop fills an `interrupted_tool_payload` for in-budget calls (`react_agent.py:992-1014`) using `TOOL_NOT_STARTED` / `TOOL_OUTCOME_UNKNOWN` from `tool_result.py:36-60` — this is what lets the model see that *something* happened without fabricating a fake result.

### 1.7 What the loop deliberately doesn't do

- **No per-tool timeout.** The turn clock and the per-LLM-call timeout wrap individual tool calls. Adding a per-tool wall would mostly catch the failure modes the wrap-up LLM already catches, at the cost of an extra cancellation axis that the `cancel_persist` machinery then has to thread through.
- **No per-iteration "should I give up" reflection.** A single `no_progress_threshold` (default 2) catches the repeating-signature pattern; the hunt-consume steers are issued at the prompt level. The design bet is that prompt-level steering converges faster than an expensive "meta-prompt: are you stuck?" round.

---

## 2. Tool layer (tool registry / function calling)

### 2.1 Surface assembly

`ToolSurfaceBuilder.build` (`cli/src/omni/agent/tool_surface.py:47-93`) is the catalog assembler. The order is meaningful and enforced by tests:

1. Builtin tools (filesystem, docs, shell, compute, web, plan, research, recall, delegate) — `build_builtin_tools` in `cli/src/omni/skills_runtime/builtin_tools/__init__.py:29-57`.
2. Sync skills registered through the skill registry.
3. `find_skill` / `run_skill` / `run_workflow` (the meta-tools).
4. Schedule tools (cron surface).
5. MCP servers.
6. External integrations.

Every Omni-owned tool is tagged with `outcome_resolver=owned_result_outcome` (line 54-56), which routes the OpenAI `command_result` schema into the internal `ToolCallOutcome` (`tool_result.py:194-221`). The same surface is filtered by `filter_tools_for_policy` (`tool_policy.py:10-22`) before the loop sees it, so policy denial is *not* an "unknown tool" surprise at runtime.

### 2.2 ToolSpec and host-only metadata

`ToolSpec` (`react_agent.py:204-235`) is the wire-vs-host split:

```python
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    replay_safe: bool = False
    exposure: Literal["direct", "deferred"] = "direct"

    def to_openai_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }
```

The comment is explicit (`react_agent.py:209-225`):

> `replay_safe` and `exposure` are host-only execution metadata. They are intentionally excluded from the provider-facing tool schema below: a model cannot grant replay authority.
>
> Advertising and reachability were previously the same list, so withholding a tool to save tokens also removed it, and the model was told `unknown tool 'write_file'` after doing the work it needed to save. Keeping them separate is what makes that outcome unrepresentable rather than merely unlikely: only `ToolPolicy` denial removes reach. Codex draws the same line — `build_model_visible_specs` filters advertised specs by exposure while `ToolRegistry::tool` dispatches by name without consulting it, so a deferred tool the model names anyway still executes.

At run time, `tool_specs` (advertised, `exposure == "direct"`, `react_agent.py:518-519`) and `tools_by_name` (reach, all names, `react_agent.py:520`) are built independently. Promotion to advertised happens the first time a deferred tool is called (`react_agent.py:1055-1057, 1114-1117`).

### 2.3 JSON-Schema compilation

`cli/src/omni/core/tool_contracts.py:252-333` defines `prepare_json_schema`, which compiles a skill's `input_schema` once, offline:

1. `copy.deepcopy` (so provider copies can't mutate the registry's source).
2. `check_schema` against the JSON-Schema spec.
3. `referencing.Registry(retrieve=_deny_external_schema_retrieval)` — locks down external `$ref` resolution; no skill can pull a schema from the network at validation time.
4. `crawl` to pre-resolve all `$ref`, `$dynamicRef`, `$recursiveRef` (`tool_contracts.py:410-464`).
5. Failure returns `PreparedJSONSchema(validator=None, definition_errors=...)` and is fail-closed.

`ProviderInputCompiler.compile_entry` (`tool_contracts.py:47-71`) does "declared schema → resolver fills critical fields → required/type/enum check → strict unknown check"; `_resolve_declared_fields` (`tool_contracts.py:73-104`) lets root-level `field_resolver` auto-inject time, attachments, and other ambient context. `admit_provider_arguments` (`tool_contracts.py:572-600`) re-validates arguments the plan has already seen, so an LLM can't sneak a field past the runtime that the planner rejected.

### 2.4 Validation chain (in order)

A tool call from the model passes through these gates; any one rejecting the call is enough to stop it:

1. **Preflight** (`react_agent.py:1815-1877`): name must be in `tools_by_name` (reach), else `error_code="unknown_tool"`. Crucially, the tool is *not* removed from the loop's `tools_by_name` because it was unknown — exposure is decoupled from reach.
2. **Circuit breaker**: per-tool counter `self._circuit[_circuit_key(tc)]` (`react_agent.py:138, 2081-2095`); for meta-tools the key is computed from the *subject* argument (`_META_TOOL_SUBJECT_ARGS` at `react_agent.py:138-142` — `run_skill` keys on `skill.name`, not on `run_skill` itself). Above `_CIRCUIT_BREAKER_MAX = 5` the call is rejected as `tool_circuit_open`. This is what stops one broken skill from disabling the router for the rest of the catalog.
3. **Argument parse**: `arguments_invalid` vs `arguments_truncated`. The latter is output-token-cap truncation and gets a different steer ("use a smaller argument"), because the model can self-correct it (`react_agent.py:1848-1855`).
4. **Tool policy**: `ToolPolicyGuard` (`tool_policy.py:98-156`) does `authorization_rejection` (allowed/blocked lists) and `budget_rejection` (`max_tool_calls`, `per_tool_limits`). Refused calls do not consume budget — the comment is explicit: "A refused call executes nothing, so it must cost nothing" (`tool_policy.py:142`).
5. **Contract / input schema**: `tool_contracts.admit_provider_arguments` projects model JSON onto the skill's `input_schema`.
6. **Approval gate**: `orchestrator._build_tools` (`orchestrator.py:1360-1372`) hands the tools to `ToolGateway(task_id, tools=..., approval_gate=...)`. `approval_tools_for_plan` and `SENSITIVE_TOOLS` (`core/approval.py:91-95`) list `bash/write_file/edit_file/apply_patch/run_compute/log_run` etc.
7. **Mutating guard**: `_emit_event("start", ...)` returning `False` blocks the call with `tool_start_not_persisted` (`react_agent.py:974-987`).
8. **Result adapter**: `owned_result_outcome` / `fs_result_outcome` / `recall_result_outcome` parse Omni / FS / Recall schemas (`tool_result.py:194-270`). `HostToolRejection(_host_seal)` mints one-way trust on the outcome (`tool_result.py:144-168`).
9. **Error classification**: `classify_tool_error` (`tool_errors.py:86-127`) produces one of five stable codes: `INVALID_ARGS` / `RETRYABLE_IO` / `SKILL_FAILED_PARTIAL` / `UNPAYABLE` / `FATAL_TURN`. `short_skill_observation` (`tool_errors.py:130-168`) attaches a `_remediation_hint` so the model's next attempt isn't "guess again".

### 2.5 Mutating and replay_safe

`tool_is_mutating` (`cli/src/omni/runtime/execution_policy.py:204-211`) enumerates the three mutating families:

- `_FILESYSTEM_MUTATIONS = {write_file, edit_file, apply_patch}`
- `_EXECUTION_TOOLS = {bash, run_compute}`
- `_STORE_MUTATIONS = {add_evidence, build_research_artifact, cite_source, log_run, record_claim, record_hypothesis, record_run, remember, run_skill, run_workflow, search_literature, submit_task}`

The start-event-persistence rule (above) applies uniformly to all three.

`replay_safe` is a manifest field on the skill itself (`replay_safe=sk.replay_safe`, `agent/subagents.py:207`); the ReAct invoker attaches `owned_result_outcome` only when the tool is safe to replay. The mutating tools are never marked `replay_safe`, so the retry budget is spent only on read-side calls that can be safely re-issued.

### 2.6 Exposure vs reach — why it matters

`cli/src/omni/core/tool_exposure.py:50-71` defines `DEFERRED_TOOLS`, a set of 18 family tools that account for ~35 % of a 50-tool surface's schema tokens but only ~4.2 % of invocations. `apply_default_exposure` (line 74-84) mutates `tool.spec.exposure = "deferred"` at the tail of catalog assembly but **keeps the spec in the list**, so the system prompt catalogue (`render_tool_catalog` in `core/system_prompt.py:184-199`) shows the name with "schemas omitted" and `find_skill` can pull the full schema back.

The reach side is unchanged. `tools_by_name` is built from the full surface at `react_agent.py:520`; deferred tools dispatch normally. A tool that the model names anyway still executes (it just paid the schema tokens on the *next* iteration, when the loop promotes it).

`spawn_subagents` is the one exception (`tool_exposure.py:17-25`): even though its schema is large, it's "discretionary" — without it the model won't think to delegate — so it stays in the advertised set.

### 2.7 Retry, circuit breaker, and graceful degradation

- **Network-level retry** is keyed on the exception name (`_RETRYABLE` in `react_agent.py:143-148`) plus HTTP status 429 / 5xx. The retry budget is `1 if replay_safe else 0`, and exponential backoff uses `Retry-After` header if present.
- **Per-tool circuit breaker** (`_CIRCUIT_BREAKER_MAX = 5`, `react_agent.py:69`) trips on a (tool, args) pair. Meta-tool keys are rewritten through `_META_TOOL_SUBJECT_ARGS` so a broken skill name doesn't poison every other skill.
- **Unexecuted-call streak** (`_MAX_UNEXECUTED_CALL_STREAK = 5`, `react_agent.py:70-76, 1265-1276`) is the symmetric guard for refused calls (`unknown_tool`, `tool_arguments_invalid`, `tool_arguments_truncated`, `tool_policy_rejected`, `tool_contract_violation`, `tool_approval_required`). Five in a row and the loop terminates with `no_progress` — the model is hallucinating tools or arguments, and the budget shouldn't be drained trying to teach it not to.
- **Provider HTTP 5xx/429** is classified as `RETRYABLE_IO`; unpayable conditions (`vlm_unavailable`, `node_unavailable`, `pptx_unavailable`) are split out as `UNPAYABLE` so they don't trigger futile retries (`tool_errors.py:115-119`); `_looks_config_unpayable` (`tool_errors.py:263-281`) checks signature, so a transient gateway 503 isn't mistaken for "VLM not configured".
- **Context overflow** falls back through three layers: model-written JSON checkpoint → `evidence_checkpoint` (host-owned, deterministic) → `microcompact_tool_results` (Codex-style head/tail trim of old tool observations; see Dimension 3).
- **Slot fallback** (`slot_routing.allow_slot_fallback`, `skills_runtime/slot_routing.py:71-89`): when `livefigure` admission fails, `scientific-figure` is allowed to take the `artifact.figure` slot. Editable / named slots never fall back; the names are authoritative for the deliverable identity.
- **Single-skill failure fallthrough**: `SkillTaskRunner.run` (`capability_runners.py:97-252`) returns `handled=False, terminated_reason="single_skill_failed"` so the ReAct loop finishes the turn cleanly (`capability_runners.py:211-228`); routing failure is not confused with a verdict.
- **`needs_input` collection**: `run_skill` returns `{"status":"needs_input",...}` on `WorkflowNeedsInput` (`tool_surface.py:402-410`); the loop detects the outcome and calls `_compose_terminal_needs_input` so the model rewrites the question in user-facing language (`react_agent.py:1144-1163, 1440-1514`).
- **Unknown tool fallback**: the preflight rejection payload lists the available tools sorted by name (`react_agent.py:1822-1828`) and bumps the unexecuted-streak counter; the model can self-correct on the next iteration.

### 2.8 What the tool layer deliberately doesn't do

- **No remote schema validation.** A skill that fails `prepare_json_schema` is rejected at registration. The runtime doesn't try to repair schemas on the fly.
- **No shadow registry.** A tool is either in `tools_by_name` (reachable) or it isn't. There's no concept of "registered but disabled by config" — that's a `ToolPolicy` concern, and policy operates on a known list.
- **No model-controlled replays.** `replay_safe` is a manifest field, not a model-supplied hint. The model can ask the same tool to be called again, but the loop will not retry mutating tools on its behalf.

---

## 3. Context management (context manager)

### 3.1 System prompt assembly

`build_system_prompt` (`cli/src/omni/core/system_prompt.py:261-323`) is six-segment, with three of the six being **conditional on the turn catalog**:

1. `role` (identity) + optional `persona_overlay` from `persona_stoma.load_turn_persona_overlay` (`turn_prompt.py:51`).
2. `render_tool_catalog(tools)` (`system_prompt.py:170-199`) — names only; full schemas are on the wire. Deferred tools are listed under "Also available, with schemas omitted".
3. `render_tool_guidance(tools)` (`system_prompt.py:24-55`) — `[Tool use]` rules. The `has_docs` flag (line 53) switches the "rediscover via `docs_search`" sentence on or off depending on whether the tool is in the catalog this turn.
4. `render_planning(tools)` (`system_prompt.py:129-145`) — only emitted when `update_plan` is in the catalog. Codex-style plan tool.
5. `render_local_environment(tools, working_dir)` (`system_prompt.py:202-258`) — only emitted when `bash` / `write_file` / `apply_patch` are present.
6. `render_self_knowledge(tools)` (`system_prompt.py:102-127`) — the *most* conditional segment: with `docs_search` available, the prompt tells the model to ground answers in docs; without it, the prompt switches to "answer from general knowledge, flag unverified parts". `render_behavior(tools)` (line 84-88) makes the same swap for `write_file` (longform / shortform guidance).

After the six segments come:

- `project_memory` from `load_curated_memory` (`memory/files.py`) — `MEMORY.md`, `AGENTS.md`, etc. Authoritative, lands *before* recalled memory so operator-written knowledge wins.
- `memory_block` from `assemble_react_system_prompt` (`cli/src/omni/agent/turn_prompt.py:20-84`): `clarification`, `react_context_block`, `assumption_block`, `unpayable`, `bound_skill`, `context_summary`, `referenced`, `thread_brief`, `research_brief`, `compiled_memory`, `skill_catalog`.
- `recent_activity` — six most recent principal-scoped tasks (`agent/recent_activity.py:43-100`).
- `repo_history`.
- `[Session context]`: cwd, OS, `OMNI_OUTPUT_DIR`, `TMPDIR`, lab notebook summary (`memory/notebook.py:11-26`).

The "switch on catalog" pattern is the same one Codex uses: the prompt narrative is shaped by what the model can *do* this turn, not by what the model *could* do in some other turn.

### 3.2 History loading

`messages = [system, *history_filter, user]` in `react_agent.py:522-532`. `history_filter` keeps only `{role, content, name, tool_call_id, tool_calls}` — five keys, no metadata injection. The model sees the conversation, not the run framework.

### 3.3 Two-phase truncation

The runtime distinguishes two compression events that share a name ("compaction") but do different work:

#### 3.3.1 Per-iteration microcompact

`_maybe_microcompact` (`react_agent.py:1516-1535`) calls `compaction.microcompact_tool_results(messages, keep_last=N, max_chars=M)` (`compaction.py:148-184`). The strategy is head/tail trim of *old* `role="tool"` messages, with two carve-outs:

- `_keep_failed_observation` (line 187-194) skips `status ∈ {failed, error, rejected, timed_out}` so a failure isn't re-compressed into "everything is fine".
- `_preserved_research_tokens` (line 197-203) protects `[S#]`, `source_id=`, `task_id=`, `artifact://X` anchors so the model can still cite.

The pair is what guarantees `tool_call ↔ tool_result` stays valid through compression: the tool_call is *not* touched, only the long result string the call is associated with.

#### 3.3.2 Cross-iteration rollover

`RunContextWindow` (`cli/src/omni/core/run_context.py:13-153`) is the hard-cap layer. `should_rollover` (line 34-45) fires when wire-serialized tokens ≥ `limit` *and* there's a tool message in the history. The pressure estimator (`run_context.py:21-32`) is a `json.dumps` + `estimate_tokens` call so the *serialized* size is what counts — message objects can be much smaller than their wire form.

`continue_with` (line 47-122) is a binary search that fills the window to the threshold. When the model is asked to write a JSON checkpoint (`react_agent.py:1555-1603`), the schema is parsed by `parse_rollover_checkpoint` (`compaction.py:206-269`) and re-rendered by `format_rollover_checkpoint` (same line). If the model can't write a valid checkpoint, the runtime falls back to `evidence_checkpoint` (`run_context.py:156-179`): a host-owned, deterministic ledger derived from the trace, capped at 16 000 chars. The fallback exists because **the only thing worse than a bad checkpoint is no checkpoint**.

### 3.4 Per-observation hard ceiling

`observation_max_chars` defaults to 8 000 (`react_agent.py:351`). Overflow goes through `compact_observation(value, max_chars, spill_dir=observation_spill_path)` (`cli/src/omni/core/observation.py`, called from `react_agent.py:1654-1661`): the body is written to `~/.omni/spill/...`, and the observation the model sees is a head/tail preview with the recovery path inline. The model can read the full file back with `read_file` if it needs to.

`truncation.formatted_truncate_text` (`cli/src/omni/core/truncation.py:51-83`) preserves head 1/3 + tail 2/3, with an `original token count` and `Total output lines` warning. `command_output_window` (`tool_result.py:410-426`) uses the same head/tail cut for process output.

### 3.5 Token estimation

`compaction.py:117-145` supports two estimators:

- `tiktoken` `cl100k_base` (provider-token-accurate) when installed.
- A 3-byte bucket (`ascii-word 5.0`, `ascii-punct 1.5`, `non-ascii 1.9`) when not. The comment is explicit that each number is calibrated to *over-estimate* slightly, so the compaction threshold never silently lets an over-budget request through.

### 3.6 Cross-turn session compaction

`SessionCompactor` (`cli/src/omni/agent/session_compactor.py`) is the durable layer. Trigger is `maybe_compact` at `session_compactor.py:57-71`:

```python
if sum(estimate_tokens(row.content) for row in rows) > session_compact_token_budget():
    ...
```

The threshold is *token*-budget, not message-count. The comment (`session_compactor.py:27-29`) calls this out: Codex-style, "compact when the cost matters, not when the count is round".

The flow (`compact` at line 73-184):

1. `MemoryService.extract_session(older_msgs, principal, on_llm_call)` flushes durable facts to M3/M4 (`service.py:689-750`); only "real" user messages are seeded, and `kind=error/partial` plus `terminated_reason ∈ _DEGRADED_TERMINATED` are filtered (`_is_low_value_assistant_turn`, line 107-123).
2. `summarize_messages` (`compaction.py:289-337`): if a real provider is available, ask it for ≤ 8 bullets preserving the research goal, decisions, `artifact://X`, and task ids. On output-cap truncation, fall back to `_heuristic_summary` (line 368-396): extract user asks, the most recent assistant turns, and any `artifact://` / `task_id` references.
3. Write a `compaction` message to the store and mark the covered rows as `compacted` — they are hidden from the prompt but kept for replay.
4. `bridge_budget` (line 172-174) folds the new bridge into the remaining budget; on multiple folds the prior bridge is folded into the new summary while the first window is preserved (line 104-115).

`_COMPACT_THRESHOLD = 30` (`session_compactor.py:29`) is **not** a trigger; it's a hint to the `/context` report so a user can see "you'll compact in N more turns".

### 3.7 Turn-end memory

`TurnMemory` (`cli/src/omni/agent/turn_memory.py`) runs every 8 turns (`_CONSOLIDATE_EVERY` at line 32, called at line 102-107). At session end, interactive channels enqueue a maintenance task (`enqueue_session_maintenance`, line 141-153); CLI calls `end_session` synchronously. The maintenance task runs `decay_and_dedup` + `rebuild_user_profile` + `compact_memory_file` + `refresh_global_summary` (line 279-302). Cross-process safety is provided by `global_memory_lock` (`memory/locks.py`).

`redact_secrets` (`memory/sanitize.py:29-36`) runs before anything touches `MEMORY.md` or `memory_entries` so an API key the model happens to surface in its reply doesn't get persisted.

### 3.8 Five-layer memory

`cli/src/omni/memory/service.py:140-154` defines the layers:

| Layer | Name | Scope | Cross-session? |
|---|---|---|---|
| M1 | SESSION | raw dialog | **No** (line 149-154 — explicitly excluded) |
| M2 | TASK | task results | task-scoped |
| M3 | EPISODIC | session summaries | yes |
| M4 | SEMANTIC | durable facts | yes |
| M5 | ARTIFACT | artifact references | yes |

M1's exclusion is the most important rule in the memory module. Without it, "the user told me they're going to add a section" would be recalled two weeks later as if it were durable. With it, the dialog row stays inside the conversation that produced it.

### 3.9 Principal isolation

`PRINCIPAL_OWNER = "local"` (`memory/service.py:170-201`) is the CLI / MCP / shared baseline. With `channel_identity=owner`, all IM peers share the owner memory. With `per_peer` (default), each IM identity maps to `<channel>:<external_key>` and is its own principal. `_principal_visible` (line 204-212) makes sure one peer can't see another's memory.

The store router (`service.py:281-307`) writes by scope:

- `scope == "user"` / `memory_type == "user_profile"` / `layer == EPISODIC` → global (cross-workspace identity)
- everything else → workspace

`open_global_store` (line 39-61) is the global handle, separate from the per-workspace DB.

### 3.10 Recall (hybrid retrieval)

`cli/src/omni/memory/service.py:453-558`:

- **Bounded candidate.** Hard cap `memory.recall_candidate_limit` default 200 (line 481-487). The cap exists to defuse `limit`-injection attacks.
- **Scope filter.** Pin / `(session, session_id)` / `(task, {subtask_id, session_task_ids})` / cross-session `_cross_session_layers(requested)` (M3/M4/M5) / explicit `scope` list. `principal ∈ {self, OWNER}` is enforced.
- **Score.** `_score` is recency + importance + pin + cosine (when embeddings exist). The vector side lives in `cli/src/omni/memory/vectors.py:23-31` for the pure-Python path and `vectors.py:56-65` for the opt-in `sqlite-vec` `vec0` KNN. Both produce the same `similarity_scores` (line 112-135), so swapping doesn't change ranking.
- **Graph spread.** `MemoryGraph.spread(seeds)` (`service.py:560-602`): from top hit, walk one or two hops, boost neighbour scores, and pull the boosted neighbours back into the candidate set. Per-store walks are merged with a max-boost reduce.
- **Render.** `build_recall_block` (`service.py:663-687`) formats as `[layer/type·scope]📌stale - summary[:240]` and clips to a char budget.

### 3.11 Lab notebook and library

`memory/notebook.py` writes a human-readable `## stamp — title #tag\nbody` block to `<workspace>/NOTEBOOK.md`. It's git-friendly and serves as the "summary view" the system prompt can quote (the `Lab notebook summary` line in `[Session context]`). `read_recent(max_chars=800/1500)` is the loader.

`memory/library.py:1-100` is the `library.jsonl` citation table, keyed by arxiv_id / DOI / normalized title; it's the M5 artifact's human-readable counterpart, feeding `omni cite export` for BibTeX / JSON / CSV.

### 3.12 Staleness and decay

`memory/policy.py` defines half-lives:

- `NON_DECAYING_TYPES = {preference, user_preference, user_profile, decision, methodology, constraint}` — these never decay.
- `finding = 45d`, `dead_end = 365d`, `idea_evolution = 180d`, `episode = 30d`, `note = 60d`, `user_note = 120d`.
- `is_stale` tags recalled entries with `stale`; pinned entries never decay.
- `decayed_importance` floors at 0.1 (line 77-88) so keyword recall can't drop a fact below visibility.

### 3.13 What context management deliberately doesn't do

- **No fine-grained token accounting per tool call.** Per-call cost matters less than cumulative spend. The loop is bounded by totals, not per-step ceilings.
- **No automatic rewriting of the system prompt.** The prompt is rebuilt every iteration from the live catalog; there's no cached "user prompt" to drift out of sync. The cost is recomputation; the benefit is no "why doesn't the model see the new tool?" class of bug.
- **No soft-delete of memory.** A `compacted` row is hidden from the prompt but kept on disk for replay. Re-promotion is what `replay_hunt_consume` uses to feed the research ledger.

---

## 4. Permissions and sandbox (sandbox / permissions)

### 4.1 The single gateway

Every tool call — from the model, from a skill, from a subagent — flows through `ToolGateway.invoke_operation` (`cli/src/omni/runtime/tool_gateway.py:164`). The pipeline is uniform (`tool_gateway.py:290-510`):

1. Snapshot `admitted_arguments_hash = copy.deepcopy(arguments)`.
2. Validate input against the tool's schema.
3. Authorization (policy + approval + preauthorizer).
4. Budget check (`ToolPolicyGuard`).
5. Persist the `start` event.
6. If mutating, the persist must succeed — otherwise `policy_violation("start_event_not_persisted")` and the call is rejected.
7. Execute.
8. Validate output against the tool's result schema.
9. Persist the `done` event; the post-execution hash check uses the *same* `admitted_arguments_hash` so the call is rejected if the args were swapped between start and done.

This is the "the model never gets to decide post-hoc that it didn't ask for a write" property in mechanical form.

### 4.2 Mutating and replay_safe (recap)

See Dimension 2 §2.5. The summary: three explicit mutating sets (`_FILESYSTEM_MUTATIONS`, `_EXECUTION_TOOLS`, `_STORE_MUTATIONS`), `replay_safe` is a manifest field, and mutating tools are never replayed on transient network failure.

### 4.3 Sensitive paths

`cli/src/omni/core/sensitive_paths.py:22-58` is the single authority. Three categories, each with a different enforcement:

- **`SENSITIVE_GLOBS`** — case-insensitive filename matches: `secrets.toml`, `.env`, `.env.*`, `*.key`, `*.pem`, `*.pfx`, `*.p12`, `id_rsa`, `id_rsa.*`, `id_ed25519`, `.netrc`, `.pgpass`, `*.credentials`, `credentials.json`, `*.secret`, `*_secret`.
- **`SENSITIVE_DIRS`** — `.ssh`, `.gnupg`, `.aws`, `.gpg`.
- **`VCS_PROTECTED_DIRS`** — `.git`, `.hg`, `.svn`. **Writing here is code execution.**
- **`STATE_PROTECTED_DIRS`** — `.omni`, `.agents`, `.codex`. These are omni's own control surfaces.

`is_write_protected_path` (`sensitive_paths.py:84-92`) is the consumer. The comment is precise: no `output_roots` configuration can relax VCS protection — `.git` is always denied, regardless of where the workspace sits.

`is_sensitive_target` (`sensitive_paths.py:116-131`) checks both the *name* and the symlink-resolved *target*, so a benign-looking symlink pointing at `.env` is caught. This closes the TOCTOU bypass where a model writes a symlink first and then "reads" a sensitive file through it.

`bash` gets an equivalent at `cli/src/omni/skills_runtime/control_store_guard.py:31-48`: `command_writes_frozen_control_store` extracts quoted and bare paths, matches against a regex of mutating verbs (`touch/rm/rmdir/mv/cp/mkdir/ chmod/chown/tee/...`) plus interpreters (`python/sqlite3/ipython`) plus redirection, and returns the list of frozen-control-store paths that would be touched. The bash tool then returns a structured refusal.

### 4.4 Approval gate

`ApprovalGate` (`cli/src/omni/core/approval.py:321-803`) is the single consent layer. It wraps the ReAct invoker (line 379-383), pass-through for safe tools, blocking for sensitive ones.

Decision order (`approval.py:399-582`):

1. `policy == "never"` → return `None`, no check (autonomous mode).
2. `classify_tool_call` against `SENSITIVE_TOOLS = {bash, write_file, edit_file, apply_patch, run_compute}` (line 58); the `force_sensitive` manifest flag lets a non-builtin tool opt in.
3. `SessionApprovalStore.match` — exact grant / rule / task-bash (see §4.5). If matched, auto-approve and emit `approval.auto` (line 421-437).
4. Persistent `security.approval_allowlist` (supports `*`, `name`, `name:prefix`; line 264-277, 438-442). Auto-approve.
5. `_write_stays_inside` — write path under workspace root, `policy != "always"`, and not sensitive. Auto-approve (line 444-446, 692-732).
6. `_reports_without_changing` — `command_is_known_safe` (read-only verbs like `git log`). Auto-approve (line 448-450).
7. `_on_request_allows_exec` — `policy == "on-request"` + non-destructive write + sandbox write enabled + `bash`/`run_compute`. Auto-approve (line 452-454, 639-652).
8. `_workspace_auto_exec` — `workspace_auto=True` + non-IM channel + sandbox writable + `bash`/`run_compute`. Auto-approve (line 456-458, 625-637).
9. `_preauthorizer` — from schedule/granted tools. Auto-approve (line 468-475).
10. No match: call `approver` (UI prompt). **No approver = fail closed.** Returns `_no_approver_reason` with concrete remediation hints ("rerun from terminal", "add to allowlist", "set require_approval=false") — never throws (line 280-301, 477-480).

The modal UI is constructed in `_with_choices` (`approval.py:584-615`): default `Approve once`; plus `Approve '<argv-prefix>' for this session` (only for `bash`, only when the metadata-proposed argv-prefix passes `_supported_rule_prefix`); plus `Approve this turn's workspace` for task-bash-eligible tools. Always `Deny`.

`pause_clocks()` is wired through `approval.py:49` so the approver's human wait time doesn't burn the turn wall clock.

### 4.5 Approval grant shapes

`cli/src/omni/core/approval_rules.py` defines the three persistence shapes:

- **`ExactApprovalGrant`** (line 108-120) — binds a command *verbatim* to an `ApprovalContext` (cwd / workspace / channel / sandbox). For `bash` the comparison is the full script, *including* quotes, escapes, and `$` — so `/bin/sh -c "..."` doesn't get normalized past a shorter-match (line 88-92).
- **`SessionApprovalRule`** (line 138-193) — validated argv prefix. `_supported_rule_prefix` (line 318-399) only accepts reviewed families: `npm run <script>`, `uv run <...>`, `pytest ...` (rejects `--basetemp`), `ruff check/format`, `cargo {bench/build/check/clippy/doc/fmt/metadata/run/test}`, `git <safe verb>` (rejects `-c/--config-env/--exec-path/--git-dir/ --namespace/--work-tree`), `omni task {list/ls/show/status/watch}`.
- **`TaskBashApprovalGrant`** (line 402-431) — one turn's workspace trust. Explicitly cannot let the write leave the workspace. **IM channels (wechat/feishu/dingtalk) never inherit** (line 423-425).

`SessionApprovalStore` (line 434-504) is the turn-scoped in-memory store; three sets for the three grant shapes; `prompt_lock: asyncio.Lock` guarantees one modal dialog at a time (line 442, 486-510).

### 4.6 Sandbox

`cli/src/omni/skills_runtime/sandbox.py:1-415` is the OS-level confinement:

- `_sandbox_works` runs four backend probes at startup (`sandbox-exec -p "(version 1)(allow default)" /usr/bin/true` and friends, line 47-61); result is cached.
- `detect_sandbox` prefers Darwin `sandbox-exec`, then Linux `bwrap`, then Linux `firejail` (line 64-71).
- `resolve_sandbox("auto")` → auto-detect; `off`/`none`/`` → off; explicit `sandbox-exec`/`bwrap`/`firejail` not available → `SandboxUnavailableError` (fail-closed; line 74-89).
- **Fallback**: auto with no backend → empty prefix (direct run), but `_warn_unsandboxed_once` (line 255-277) emits a one-time WARNING: "WITHOUT kernel confinement — only the coarse denylist applies".

Profiles per backend:

- **Darwin seatbelt** (`_seatbelt_profile`, line 221-248): `(allow default) (deny file-write*) (allow file-write* (subpath <each-root>...) (literal /dev/null /dev/stdout /dev/stderr))`. Each write root is wrapped by `_seatbelt_write_atom` (line 200-218) as `(require-all (subpath <root>) (require-not (regex #"^<root>/<protected-name>(/.*)?$")))` so writing `<workspace>/.git/...` is also denied.
- **bwrap** (`_bwrap_prefix`, line 371-405): `--ro-bind / /`, `--dev-bind /dev /dev`, `--proc /proc`, per write root `--bind root root`, scratch `--bind <persist_tmp> /tmp`. `_safe_persist_tmp` (line 355-368) refuses to resolve a scratch path into `.omni` ("exec scratch opens Omni control state").
- **firejail** (line 320-330): `--whitelist=<root>` and `--read-only=<denied>` for metadata paths.

`_metadata_deny_paths` (line 150-178) enumerates `.git` / `.omni` / `.agents` / `.codex` under user source roots into the deny set. `PROTECTED_METADATA_NAMES` (line 125-138) is the explicit-allow override when the user really did grant write to `.git` (rare; mostly for the worktree isolation case).

`WRITE_PROTECTED_DIRS = VCS_PROTECTED_DIRS | STATE_PROTECTED_DIRS` (`sensitive_paths.py:63`) is double-checked: `approval._write_stays_inside` uses `is_write_protected_path` *and* the fs tool handler re-checks itself. Two reads, same answer required.

### 4.7 Container and worktree isolation

`cli/src/omni/runtime/isolation.py`:

- `container` requires `docker_image` (line 35-47).
- `worktree` requires `.git` to exist and creates a branch under `project_dir/worktrees/` (line 60-92).

Used by `subagents` (Dimension 5) and by `compute` skills that need hermetic execution.

### 4.8 Node renderer setup

`cli/src/omni/skills_runtime/runtime_setup.py:142-199` only runs `npm ci --omit=dev` from three owner-controlled entry points: `omni init`, `omni update`, `omni skills setup`. The engine inside a turn only reads the cache; package managers are never called from a turn. The header comment is explicit: "Task execution is deliberately side-effect free".

### 4.9 What the permission layer deliberately doesn't do

- **No capability tokens.** There is no per-tool capability token issued to the model. The gate is in the host; the model only sees whether the call went through.
- **No escape hatch via the system prompt.** The system prompt can *describe* the tools; it cannot grant or revoke them. The surface builder runs after the prompt is rendered.
- **No "trust this session" all-or-nothing.** Approvals are grant-shaped (exact / argv prefix / task-bash), not binary. A user can grant `npm run test` without granting `rm -rf`.

---

## 5. Subagent dispatch (routing / delegation)

### 5.1 There is no fixed "explore / worker / verifier" trio

`cli/src/omni/agent/subagents.py:60-74` defines `SubagentSpec` with a free-text `role` field. The "reviewer" is *not* a subagent class — it's an LLM-as-judge loop inside `run_subagent` (`subagents.py:588-649`), gated by `cfg.reviewer_enabled` and a `reviewer_min_score=0.5` threshold. The domain packs in `cli/src/omni/data/domain_packs/*.toml` list role *suggestions* ("ml-method-reviewer", "biomedical-evidence-reviewer", "evidence-auditor", etc.) but these are strings the model writes into the `role` field, not type tags.

This is a deliberate departure from the "three named subagents" pattern: the harness is meant to be role-agnostic, with the model's intent shaping the role.

### 5.2 Two dispatch surfaces

The model sees two groups of delegation tools, both defined in `cli/src/omni/skills_runtime/builtin_tools/delegate.py`:

**Blocking batch** (default): `spawn_subagents` — takes `subtasks: [{goal, role, context, tools, model, compute_profile, isolation}]`, calls `run_subagents` (`delegate.py:181-217`); concurrency is capped at `cfg.concurrency` (`subagents.py:698-704`).

**Async fire-and-collect** (`async_enabled=True`): `spawn_subagent` / `wait_subagent` / `list_subagents` / `interrupt_subagent` / `message_subagent` / `followup_subagent` (`delegate.py:68-169, 255-340`).

`SubagentControl.spawn` (`cli/src/omni/agent/subagent_control.py:79-97`) launches via `asyncio.create_task(self._run_one(live, seed))`. `wait` blocks until one of the live handles signals a `done_event`. `wait(None)` waits for "any", but already-collected subagents are not re-signalled. `interrupt` calls `live.control.request_cancel()`. `message` calls `live.control.push_steer(text)`. `followup` spawns a fresh specialist for a finished one, passing the old summary as context (`subagent_control.py:79-288`).

The orchestrator creates `ctx.subagent_control = SubagentControl(ctx, cfg=self.settings.subagents, depth=0)` at turn start (`orchestrator.py:1338-1341`) and `await ctx.subagent_control.aclose(grace_s=2.0)` at turn end (`orchestrator.py:1474-1476`).

### 5.3 No "register a subagent" API

There's no user-facing API to register a subagent. Customization is either:

- **Inline in `spawn_subagents`**: pass `tools=[...]` as an allowlist. The default is `builtin + research` with `_MUTATION_TOOLS = {write_file, edit_file, apply_patch, bash, run_compute}` stripped (`subagents.py:56, 215-223`). The spec can `+` mutation tools back in.
- **In a domain pack `[[specialists]]` block**: this defines `role + description + tools` metadata; it's still treated as a normal skill in the registry, not a separate subagent registry.

`SkillRegistry.register(entry: SkillEntry)` (`cli/src/omni/skills_runtime/registry.py:274-395`) is the only entry point. System skills get a separate index (`_index_system_skills`, line 362-376) but they're still skills.

`container` isolation locks the surface to `builtin + research + run_compute` (`subagents.py:217-218`): a `worktree`-isolation spec can't accidentally run inside the host Python engine.

### 5.4 Context propagation and isolation

`_child_context` (`subagents.py:124-142`) builds the per-child context:

- `task_id = f"{ctx.task_id}::sub-{uuid4().hex[:8]}"` — every event and artifact can be attributed.
- `subagent_control=None` — a subagent cannot open its own `SubagentControl` (depth-limited; see §5.5).
- `subagent_depth = depth + 1` — drives the depth gate in `build_delegation_tools`.
- `file_uris` is copied (inbox references), but the *transcript* is *not* inherited.

The system prompt is `_specialist_system` (`subagents.py:226-233`):

```
You are the coordinator's {role} subagent. Complete only the assigned
subtask and return a self-contained final answer. The coordinator sees
only the final answer, so it must be complete, directly usable, and
evidence-grounded.
```

The "coordinator sees only the final answer" is enforced by `_summary_of` truncating to 6 000 chars (`subagents.py:251-255`). Costs are recorded per child task with `component="subagent.initial" / "subagent.revision.{n}" / "reviewer.subagent.{n}"` (`subagents.py:381-431`). The research ledger is merged back via `merge_research_ledger(parent_id, child_row)` (`subagents.py:666`).

The default tool surface is `_specialist_tools` (`subagents.py:145-223`): `builtin + research + PYTHON_ENGINE / CLI_EXEC skill` (with `provider_authority` validation, line 188-200), minus mutation tools. A spec-provided `tools` list is treated as an allowlist on top.

### 5.5 Nesting, cancel, timeout

**Nesting.** `build_delegation_tools` (`cli/src/omni/skills_runtime/builtin_tools/delegate.py:177-179`) returns no `spawn_subagents` when `depth >= cfg.max_depth`. Default `max_depth=2` (settings:287) means "coordinator → specialist one level deep". `SubagentControl._depth` is propagated to children (`subagent_control.py:69-97`).

**Cancel** has five paths:

1. **User**: `ExecutionControl.request_cancel` (REPL / TUI / IM interrupt).
2. **End of turn**: `SubagentControl.aclose(grace_s=2.0)` (`subagent_control.py:291-307`): wait `grace_s`; then `control.request_cancel()`; then 1s; then `task.cancel()`.
3. **Single subagent**: `SubagentControl.interrupt(nickname)` calls `live.control.request_cancel()` (`subagent_control.py:235-242`).
4. **Loop-internal**: `run_subagent._run_once` propagates `execution_control`, so a child loop sees the cancel.
5. **Hard cancel**: `asyncio.CancelledError` is caught at `subagents.py:566-573`; child task is marked "cancelled" and re-raised.

**Timeout.** `wait_subagent` defaults to `cfg.wait_default_s=30.0`, capped at `cfg.max_seconds=90.0` ("never wait longer than a specialist could possibly run", `subagent_control.py:183-187`). The specialist's own `ReActLoopAgent` is constructed with `max_seconds=cfg.max_seconds, max_iterations=cfg.max_iterations, max_tool_calls=cfg.max_tool_calls, stall_timeout_s=...` (`subagents.py:532-552`). Tool budget is shared across the loop and any revise pass via a `subagent_tool_budget = ToolExecutionBudget(cfg.max_tool_calls)` (`subagents.py:532-538, 605-642`). `usage_budget_exhausted(result)` breaks the revise loop (line 592, 631).

**Resource locks**. Specialists inherit the parent's `resource_locks` (`tool_gateway.py:101` reads `getattr(ctx, "resource_locks", None)`); `ToolResourceLockPool` (`cli/src/omni/runtime/execution_policy.py:48-154`) orders locks by key (`fs:<path>` / `exec:<scope>` / `store:<scope>`) to avoid deadlocks; mutating fs operations use `fcntl` / `msvcrt` file locks (line 90-201).

### 5.6 Reviewer gate

After the specialist loop finishes, if `cfg.reviewer_enabled and status in (ok, partial)`, `review_output(llm, goal, output)` runs. Below `reviewer_min_score=0.5`, the gate appends `[Review feedback] {notes}` as a user message and runs another pass; up to `reviewer_max_revises=1` (`subagents.py:588-647`). The reviewer is an LLM call, not a separate agent — it's a cheap quality check that costs roughly one extra loop.

### 5.7 What subagent dispatch deliberately doesn't do

- **No nested subagent graphs.** `max_depth=2` is the ceiling. The harness treats this as enough: the parent is a coordinator, the child is a specialist, and that's it.
- **No shared mutable state across specialists.** Each subagent has its own `task_id`, its own resource lock pool reference, and its own budget. The only shared resource is the database and the parent task's event stream.
- **No model-controlled subagent lifecycle.** A subagent can only be started by `spawn_subagents` / `spawn_subagent` (model), interrupted by `interrupt_subagent` (model), or reaped by `aclose` (host). The parent can't reach into a child's loop.

---

## 6. Session and state (session / state)

### 6.1 Lifecycle and turn boundary

A turn's entry point is `orchestrator.run_turn` (`cli/src/omni/agent/orchestrator.py:978`). The whole turn runs inside `async with persist_scope(self.db)` so cancellation is atomic at the DB level — see `cli/src/omni/runtime/cancel_persist.py:107-117` for the persist scope. The bookkeeping is "turn writes commit together or not at all".

`SessionLifecycleService` (`cli/src/omni/agent/session_lifecycle.py:181-386`) guards session-level operations:

- `delete_many(session_ids)` runs `_delete_workspace` in a single transaction (line 239-378). It starts with `_begin_task_delete_snapshot` (line 248-254) to reserve the write set; queries active children, `_task_descendant_closure` to find cross-session descendants (line 280-291); enumerates blockers (schedules, open action_checkpoints, active compute_jobs, pending schedule proposals; line 102-178); if no blocker, `stage_task_deletion(force=True)` + flush + delete `ConversationMessageORM` / `SessionFocusORM` / `SessionORM`, then commit (line 366-378).
- Failure modes: `code ∈ {concurrent_write, ambiguous, not_found, conflict, busy}` (line 258-264, 293-302, 330-335, 348-352).
- The machine-level `control_db` is also init'd so cross-workspace schedule proposals get the write reservation (line 210-222).

### 6.2 Conversation store

`cli/src/omni/agent/conversation_store.py:76-449`:

- `ensure_session(channel, external_key, reuse_latest)`: IM same-key reuses; CLI defaults to new (line 87-110).
- `history(session_id, limit=12)` for the ReAct prompt; `extraction_history(limit=40)` for memory/compaction.
- `touch_session` / `set_session_title` / `delete_session` / `get_session` / `resolve_session` are the standard five.
- `principal_of(channel, external_key)` returns `"<channel>:<external_key>"` or `"local"`; per-channel cached (line 112-143).

Schema (`cli/src/omni/storage/models.py:63-95`):

- `SessionORM`: `id, project, channel, external_key, title, status, forked_from, created_at, updated_at` (`forked_from` supports session fork).
- `ConversationMessageORM`: `session_id, role, content, content_type, name, tool_call_id, metadata, created_at`.

### 6.3 Task controller

`TaskController` (`cli/src/omni/agent/task_controller.py:17-199`):

- `create_turn_task`: build the task + fire `on_task_ack` (line 23-48).
- `finish_turn`: append the `assistant.message` event; if children have been submitted, `refresh_from_executions` waits for child terminal before settling. `terminal_status` derivation:
  - `kind == "error"` → `failed`
  - `kind == "needs_input"` → `needs_input`
  - else `succeeded`, demoted to `degraded` if `turn_degradation_warnings` is non-empty.
- `apply_settlement` reconciles orchestrator-reported status with the durable record (line 159-199). The comment captures the principle: "settle from the turn's own end once children are terminal".

### 6.4 Persistence

`Database` (`cli/src/omni/storage/db.py:70-200`) is a single SQLAlchemy async engine over `sqlite+aiosqlite`. Pragmas: `synchronous=NORMAL, busy_timeout=1000ms, foreign_keys=ON` (line 91-100). WAL mode (docstring line 4-6) lets the daemon and short-lived commands read concurrently. `ApplicationId=0x4F4D4E33 ("OMN3")` stamps the current store shape (line 53); `user_version=1` is reserved (line 52).

ORM tables (`cli/src/omni/storage/models.py`, 27 tables):

- Session / task: `SessionORM, ConversationMessageORM, TaskORM, WorkflowRunORM, WorkflowStepORM, SubtaskORM, WorkflowCheckpointORM, TaskEventORM, TaskControlORM`
- Delivery: `OutboundDeliveryORM, ComputeJobORM, SessionFocusORM`
- Memory: `MemoryEntryORM, MemoryEdgeORM`
- Artifacts / research: `ArtifactORM, SourceORM, ChunkORM, CitationORM, HypothesisORM, ClaimORM, EvidenceORM, RunORM`
- Scheduling: `ScheduleORM, ScheduleActionProposalORM, ActionCheckpointORM`
- Index: `TaskIndexORM`

`TaskORM` (`models.py:105-184`) has the lineage that makes retry safe:

- `parent_task_id` (FK self, `ondelete="CASCADE"`)
- `origin_workflow_run_id / step_id`
- `schedule_id`
- `retry_of_task_id / root_task_id / attempt` (retry lineage)
- `input_snapshot_json` (immutable turn input)
- `approved_tools: list` — per-task grant, set at materialisation (e.g. a scheduled run calls `recorder.grant_tools(task.id, grants, ...)` at `scheduler.py:493-498`)
- `current_authority_fingerprint` + `approval_authority_fingerprint` — content hash binding plan + catalog + contract + pre-grants (`models.py:167-172`)

### 6.5 Cancel-safe writes

`cli/src/omni/runtime/cancel_persist.py` solves a Python 3.11+ wart: `Task.cancel()` can recursively cancel child waiters, so a parent cancel kills the writer that was supposed to record `react.finished`. The answer is `run_uncancelled(work)` — run the work in a sibling task and wait for it (line 151-180+); `pause_cancellation` / `resume_cancellation(held)` keeps the approval-gate wait from being treated as a cancel (line 120-148); `persist_lock` is reentrant per loop per-DB (two workspaces don't deadlock; line 26-96).

### 6.6 Scheduler and cron

`cli/src/omni/runtime/scheduler.py:187-591` is a hand-rolled 5-field cron parser (`parse_cron` / `cron_matches` / `next_cron_fire`) with the full cron feature set: `*`, `*/n`, `a-b`, `a-b/n`, `a,b`; DST and the day-of-month / day-of-week OR rule per standard cron (line 95-186).

`Scheduler.run_due(now, limit)` (line 375-462):

- One transaction: `SELECT enabled AND next_due_at <= now`.
- Stamp `_Fire` as a write reservation; advance `next_due_at = _compute_next(...)`; bump `run_count`; disable `once`.
- **SQLite serialised write** means two tickers can't double-fire.

`_materialise_owning_task` (line 464-500) creates a `TaskORM` per fire, with `schedule_id` pointing back. `grant_tools` injects `approved_tools`; the preauthorizer passes them through to `ApprovalGate`, so OS-sandbox + write-root enforcement still applies.

Trigger kinds: `interval` / `cron` / `once` (`ScheduleORM.kind`, `models.py:744`). Missed fires during a long outage are **not** back-filled — outage doesn't stampede into a flood (line 386).

`cli/src/omni/scheduling/service.py:73-552` is the schedule-creation surface:

- Local CLI creates directly (`omni schedule add` no prompt).
- IM channel creation requires local approval: a `ScheduleActionProposalORM` (payload_digest = `sha256(payload)`, `idempotency_key` blocks resend); `omni schedule approve <id>` re-runs the *stored* payload (never re-derived from prose; `models.py:766-820`).
- 24h `_PROPOSAL_TTL` expires stale proposals; near-term `_NEAR_TERM_APPROVAL` (15 min) auto-rejects proposals whose window has already mostly passed (`service.py:53-62`).

`cli/src/omni/scheduling/temporal.py` resolves 8-segment Chinese / English time expressions (`resolve_temporal`): `resolve_cron`, `resolve_interval`, `resolve_once`, `resolve_zone` translate "tomorrow 6 am", "every 2h", `cron 0 18 * * *` to trigger parameters. `ZoneInfo` aware-DST throughout.

### 6.7 Daemon and web

- `omni serve` writes `serve.pid` via `os.replace` (atomic) with startup metadata (version, argv, channels, workers) so `omni update` can re-launch with the same args (`cli/src/omni/runtime/daemon.py:30-67`). 30s `HEARTBEAT_STALE_SECONDS` is the liveness window (line 27).
- `omni web` writes `web.pid` (`cli/src/omni/runtime/web_service.py:23-82`); no heartbeat window ("a healthy UI older than 30s is still live"); `pid_alive` auto-clears the pidfile on death (line 86-100). `STOP_GRACE_S=3.0 / STOP_KILL_S=2.0` is the two-stage shutdown.

### 6.8 Update convergence

`cli/src/omni/runtime/update_state.py`:

- `InstallationFingerprint` (line 38-54) hashes the sanitised PEP 610 `direct_url.json` (credentials stripped).
- `STATE_FILE=update-state.json` schema v2; venv-out `omni update` uses this to decide "the package changed, home should reconverge".

### 6.9 Resume and cancel

**Resume**:

- Same session new turn → `ConversationStore.ensure_session(channel, external_key)` reuses by `external_key`.
- `TaskController.create_turn_task` passes `file_uris` so `task retry` can reproduce the input (line 35-41).
- `TaskORM.input_snapshot_json` is immutable; retry lineage is in `retry_of_task_id / root_task_id / attempt` (`models.py:130-137`).
- `WorkflowStepORM.step_key` is stable across retries; `execution_ids` is the per-attempt subtask id list (`models.py:228-261`).
- `recent_activity_digest` (`cli/src/omni/agent/recent_activity.py:43-100`) renders the 6 most recent principal-scoped tasks for the planner — "regenerate the last figure" no longer needs a re-prompt.

**Cancel**:

- `TaskController.finish_turn` checks `terminal_status ∈ {cancelled, interrupted}` → calls `settle_open_children_for_cancel(task_id)` to close children, then `ensure_event` to force `react.finished` into the stream (Windows busy can drop the event otherwise; `task_controller.py:91-107`).
- Top-level `ExecutionControl.request_cancel` is held by the orchestrator; each subagent in `_child_context` has `subagent_control=None` but a real `execution_control` (`subagent_control.py:84-92`).
- `cancel_persist.pause_cancellation` / `ignore_cancellation` keep the `react.finished` write alive (see §6.5).
- `SubagentControl.aclose(grace_s=2.0)` is the turn-end reap (see §5.5).
- IM-channel children never inherit `task-bash` grants (`TaskBashApprovalGrant.matches`, `approval_rules.py:423`).

### 6.10 What the session layer deliberately doesn't do

- **No cross-device sync.** The harness is local-first. AGENTS.md is explicit: SQLite + filesystem only; no MySQL, Redis, MinIO, Chroma. The control store is home-level (`control.sqlite3`) but still single-machine.
- **No "session becomes a long-running task" boundary.** Tasks and sessions are separate concepts. A long-running work unit is a `Task` with `kind ∈ {turn, subagent, maintenance, escalated}` plus a `Schedule` if it needs cron; sessions are chat threads, not jobs.
- **No "per-session tool policy".** Policy is per-task, recorded in `TaskORM.approved_tools` at materialisation. A session that wants to grant a tool does so by creating a task with the grant — not by the model asking nicely mid-conversation.

---

## 7. Observability (observability)

### 7.1 Trace and step records

The durable spine is `task_events`, an append-only table (`cli/src/omni/storage/models.py:343-381`); every row has `seq`, `event_type`, `status`, `lifecycle_status`, `result_success`, `name`, `tool_name`, `skill_name`, `input_json`, `output_json`, `error`, `summary`, `duration_ms`. Event names follow `<component>.<verb>`. Hook success / failure is itself an event (`cli/src/omni/runtime/hooks.py:217-227`):

```python
await self._tasks.append_event(
    task_id,
    event_type="hook.done" if status == "succeeded" else "hook.failed",
    status=status, name=event,
    input_json={"event": event, "command": _command_label(command)},
    output_json=output, error=error, duration_ms=duration_ms,
    summary=f"hook {event} {status}",
)
```

`TaskRecorder.append_event` wraps `retry_while_busy` so SQLite "database is locked" is transient.

A separate `control.sqlite3` (machine-global) carries `TaskIndex` — cross-workspace task metadata — so `omni task --all` doesn't have to scan every workspace. `TaskRecorder` dual-writes (`cli/src/omni/runtime/task_index.py:144-168`).

### 7.2 Token and cost tracking

`cli/src/omni/agent/cost.py:25-50` is a USD-per-1M rate table (`gpt-4o / deepseek-v4 / claude-sonnet-4` etc.). `rate_for` uses longest substring match with date-aliased names. `estimate_cost` first reads the provider's `usage` block; otherwise estimates `len(text) // 4` chars-per-token (`cost.py:104-133`). The final event is `cost.usage`:

```python
await tasks.append_event(
    task_id,
    event_type="cost.usage",
    status="succeeded",
    name=component,
    output_json={**estimate.to_dict(), "currency": currency, "component": component},
    summary=f"{component}: cost ~{estimate.cost_usd:.4f} {currency} · tokens {estimate.total_tokens}",
)
```

`record_cost_event` swallows the exception (`cost.py:172-173`) so metering never blocks a turn. `react_usage_limits` translates `settings.cost` into the ReAct's `max_total_tokens / max_cost_usd / warn_*`; `usage_budget_exhausted` looks at `result.terminated_reason` or `usage_budget.enforced` (`cost.py:303-342`). The `task show` page calls `summarize_cost_events` to roll events up without re-querying (`cost.py:228-271`).

### 7.3 Logging

`cli/src/omni/runtime/logging_config.py:1-6` opens with a hard rule:

> Never call `basicConfig`, never replace caller handlers.

`OmniLogFormatter` (line 173-194) emits a single-line JSON-ish record: `ts level component=<c> logger=<n> pid=<p> event=<e> message=<json>`. Token redaction runs `redact_secrets` + `_UNSAFE_TOKEN_RE` against the message before it lands.

`_PrivateRotatingFileHandler` (`logging_config.py:197-207, 40-44`) is the file sink: 10 MB × 10 files, `os.fchmod(0o600)` immediately on POSIX. `ProcessLogging` (`logging_config.py:284-371`) is an enter/exit reversible attach: enter records the logger's prior level and quiets `httpx / httpcore / uvicorn.access`; exit restores. It only removes handlers it created — caller handlers are preserved. `UvicornShutdownFilter` (`logging_config.py:100-122`) cleans the `CancelledError` noise from ASGI graceful-shutdown stderr but leaves the truth in the file.

### 7.4 Debug entry points

- **CLI**: `omni status` / `omni task show` / `omni task list --all`.
- **TUI footer**: `turn_outcome.classify_turn_outcome` + `header_state` (`cli/src/omni/runtime/turn_outcome.py:80-87`), `exec_exit_code` decides the process exit code.
- **Cross-workspace**: `cli/src/omni/runtime/aggregate.py` `_indexed_task_page` reads `control.sqlite3`'s `TaskIndex`; empty index triggers `reconcile_index`. `list_schedules_all_workspaces` iterates `iter_catalog_workspaces` (line 166-205).
- **Recent activity**: `cli/src/omni/agent/recent_activity.py:43-99` renders the 60 most recent terminal tasks as a `Recent activity` block for the planner, so a follow-up turn can resolve "the last figure" without an extra round.
- **Lab notebook**: `<workspace>/NOTEBOOK.md` (`memory/notebook.py:11-26`) is the human-readable, git-friendly trace parallel to the DB's `task_events`.

### 7.5 What observability deliberately doesn't do

- **No Prometheus / OpenTelemetry exporter.** No metrics scrape, no distributed-trace protocol. The DB and the log file are the interface. External dashboards (if any) read the same SQLite or the same JSONL.
- **No web dashboard.** The web SPA is a workspace view, not a metrics view. It shows tasks and artifacts, not charts.
- **No automatic alerting.** A long stall surfaces as `terminated_reason="stalled"` on the task; nothing pages anyone.

---

## 8. Hooks and interceptors (hook / 拦截点)

### 8.1 LLM event hook

`cli/src/omni/core/llm/client.py:36-46` uses a `ContextVar` for a single-slot injection, so the same process can serve two conversations without cross-talk:

```python
_llm_event_hook: ContextVar[...] = ContextVar("omni_llm_event_hook", default=None)

def bind_llm_event_hook(hook): return _llm_event_hook.set(hook)
def reset_llm_event_hook(token): _llm_event_hook.reset(token)
```

`emit_llm_notice` carries retry notices (`Reconnecting n/5`); `_emit_delta` is the token-stream sink (sync/async OK). The loop binds the hook at iteration start (`react_agent.py:700`) and resets after — the same callback sees both token deltas and notices.

### 8.2 Tool pre/post hooks

`invoke_tool_with_hooks` (`cli/src/omni/runtime/hooks.py:367-509`) is the interceptor every path goes through:

```python
if hooks is not None:
    decision = await hooks.emit("pre_tool", task_id=task_id,
        payload={"tool_name": tool_name, "arguments": ..., "family": family},
        deny_capable=True)
    if not decision.allowed:
        raise HookDeniedError(decision.reason)
...
try: ... except Exception as exc:
    if hooks is not None:
        await hooks.emit("post_tool", task_id=task_id,
            payload={..., "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    raise
```

`pre_tool` is followed by an `ExecutionPolicyFrame` (`hooks.py:417-442`) that hashes canonical args via `sha256`. The frame is copied into the child via `ContextVar`; the child checks `asyncio.current_task() is frame.owner_task` to refuse cross-task reuse of the grant — a denial of the "task A's grant, task B's execution" attack.

### 8.3 Custom hook registration

`settings.hooks` is a config object whose commands are bucketed by event name, with `*` for "all":

```python
commands = [
    *self._cfg.commands.get("*", []),
    *self._cfg.commands.get(event, []),
]
```

The command runner (`hooks.py:240-277`) is strict:

- `asyncio.create_subprocess_exec` only — no shell.
- `stdin` is a redacted JSON envelope.
- `stdout` must be a single JSON object.
- `max_output_bytes` defaults to 1024.
- `timeout_s` is enforced.
- `payload` is run through `_redact` — `api_key / secret / password / token / credential / authorization` become `[REDACTED]` (`hooks.py:280-289`).
- `action: "deny"` is the only true deny; `failure_policy == "fail"` is also treated as deny (`hooks.py:236-237`).
- **Priority**: wildcard commands run first, then event-specific. First deny short-circuits.

### 8.4 Failure isolation

- A hook exception is caught (`except Exception`), logged as a warning, and the main loop continues (`hooks.py:208-213`).
- The subprocess runs in its own process group (`processes.process_group_options` + `stop_process_tree`, `cli/src/omni/runtime/processes.py:13-63`), so cancelling a hook kills the whole tree.
- `is_cancelled` re-raises `asyncio.CancelledError` rather than swallowing it.

### 8.5 Other interceptors

- **`defend_observation`** (`cli/src/omni/core/injection.py:46-79`) runs before an observation lands in the transcript. `mode="flag"` adds a banner; `mode="strip"` replaces 11 classes of manipulation phrases with `[suspected injected instruction neutralized]`. This is the second line of defense after the sensitive-paths check; the first line of defense is "the model can't write to .env", this is "the model can't trick itself into writing to .env by paraphrasing the prompt".
- **`execution_ownership`** (`cli/src/omni/runtime/execution_ownership.py`) pins every `SubtaskORM` to a PID. `pid_alive` is `os.kill(pid, 0)` on POSIX and `OpenProcess` + `GetExitCodeProcess` on Windows. A process must be dead before its subtask can be settled — a live process is never stolen.
- **`ExecutionControl`** (`cli/src/omni/core/execution_control.py:43-201`) splits `cancel_requested` (process-local) from `durable_cancel` (DB-backed). `request_cancel` doesn't write a control row; `poll` only sets `durable_cancel=True` when it sees one. This is what makes a `omni update` restart *not* cancel a long-running turn.
- **MCP client** (`cli/src/omni/compat/mcp_client.py:23-56`): session-per-call (stdio or SSE); an unreachable server logs `logger.warning` and is skipped — one bad MCP can't blackhole the whole tool surface.

### 8.6 What the hook layer deliberately doesn't do

- **No "after model, before tool" arbitrary code hook.** The only interception points are the ones the runtime defines. Owners who want more can configure `settings.hooks` with their own command list; they cannot patch the loop in-process.
- **No remote-loaded hook code.** A hook is a command name + argv; the harness spawns it. There is no in-process plugin loader.

---

## 9. Error recovery and retry (错误恢复与重试)

### 9.1 Error classification

`cli/src/omni/core/tool_errors.py:13-25` defines five host-owned error classes. They are *stable on the wire* — model strategy code can branch on them:

```python
INVALID_ARGS         # unknown_tool / tool_arguments_invalid / tool_contract_violation / tool_policy_rejected
RETRYABLE_IO         # tool_circuit_open / tool_timeout / 429 / 503
SKILL_FAILED_PARTIAL # failed but artifacts survived
UNPAYABLE            # unpayable / vlm_unavailable / node_unavailable / pptx_unavailable
FATAL_TURN           # sandbox_escape / storage_corrupt
```

`classify_tool_error` (`tool_errors.py:86-127`) reads `result.error_class` first, then status / error_code, and finally falls back to substring matching against the result text. `short_skill_observation` (`tool_errors.py:130-168`) attaches a `_remediation_hint` so the next attempt isn't "guess again" — VLM 503 gets a "retry" hint, not a "VLM not configured" message (`tool_errors.py:312-335`).

LLM-side classification is separate (`cli/src/omni/core/llm/errors.py:69-139`):

- `400 + tool_call` → `transcript_invalid` (no retry; replaying the same transcript gives the same error).
- `429` / `5xx` → retryable, fallback allowed.
- `401` / `403` → `authentication`.
- `output_cap_truncated` is its own branch (`OUTPUT_CAP_TRUNCATED_REASON`); it's a provider-side truncation signal, not a failure.

### 9.2 Retry strategy

- **LLM client**: `RetryPolicy(max_retries=2, base_delay=0.5, max_delay=8.0, jitter=0.1)` (`client.py:99-155`); prefers `Retry-After` header (`client.py:117-144`); else exponential backoff with symmetric jitter.
- **ReAct tool call**: `_TOOL_RETRY_MAX = 1`, only on `replay_safe` tools (`react_agent.py:67-68, 1900-1901`); backoff `_TOOL_RETRY_BASE_DELAY * (2 ** attempt)` (`react_agent.py:1936-1938`).
- **Background skill**: `cli/src/omni/runtime/subtask_retry.py:27-40` `_TRANSIENT_SIGNS` matches `timeout/429/503/connection reset/...`; on `recovery_policy="auto_retry_transient"`, `is_transient_error` fires; `record_auto_retry` writes `original_error + recovery_attempt` to `SubtaskORM` and emits `subtask.retry`, backing off by `settings.tasks.retry_backoff_s * attempt` (`subtask_retry.py:51-104`).
- **Workflow envelope timeout**: `cli/src/omni/runtime/skill_timeout.py:23-36` `skill_exception_status`: workflow_envelope is always `failed`; per-skill budget/stall is `degraded` only when there's a durable output, otherwise `failed` so SINGLE_SKILL can fall through.

### 9.3 Circuit breakers

`cli/src/omni/core/react_agent.py:69-76, 135-148, 1837-1894, 1990-1995` runs two independent counters:

```python
_CIRCUIT_BREAKER_MAX = 5
_MAX_UNEXECUTED_CALL_STREAK = 5
```

- `self._circuit[_circuit_key(tc)]` — *executed* failures. Above 5 for a given `(tool, args)` pair, the call is short-circuited as `tool_circuit_open`. Meta-tool keys use `_META_TOOL_SUBJECT_ARGS` so a broken `run_skill` for one skill doesn't kill the rest.
- `self._unexecuted_calls[code]` — *unexecuted* refusals (`unknown_tool`, `tool_arguments_invalid`, `tool_arguments_truncated`, `tool_contract_violation`, `tool_policy_rejected`, `tool_approval_required`). Above 5, the loop terminates with `no_progress`.

A successful call resets both: `self._circuit.pop(circuit_key, None)` and `self._unexecuted_calls.clear()` (`react_agent.py:1981-1984`).

### 9.4 Escalation paths

- **`escalate_run` tool** (`react_agent.py:65, 304-320`): registered as a meta-tool; only injected when `allow_escalation=True`. Model invocation → `agent.turn_escalate.maybe_escalate_run` (`turn_escalate.py:12-61`).
- **`maybe_escalate_run`** creates `kind="escalated"` child task, inherits parent ROM via `tasks.inherit_research_ledger`, `asyncio.create_task` background-runs `handle_turn`. Rejects recursion (`depth ≥ 2` or parent already escalated).
- **Plan ladder** (`cli/src/omni/agent/plan_recovery.py:67-118`): five rungs. Safety → hard stop. Reference-marker `needs_input` → ReAct falls back to "what's the referent?" lookup. Single missing field → `needs_input` question. Otherwise → `build_react_recovery_plan` (rung 4, feed findings into a capable assistant, still tool-policy bounded).
- **Fallthrough** (`cli/src/omni/agent/plan_fallthrough.py:30-42`): on single-skill failure, `policy_after_failed_route` clears `allowed_tools / max_tool_calls / max_iterations` back to `None` (deny list preserved); `history_with_failed_attempt` injects the failure into history so the model doesn't retry the same call.

### 9.5 Resume and checkpoint

- **Durable checkpoint**: `cli/src/omni/runtime/schedule_checkpoint_resume.py:107-200` `resolve_schedule_checkpoint` does CAS, `required_decider` validation, `pick:/repair_next_day:` prefix resolution, and `run_now / cancel / other_time` keyword routing.
- **Recovery coordinator**: `cli/src/omni/runtime/task_recovery.py:160+` `TaskRecoveryCoordinator` distinguishes three semantics:
  - `retry`: new attempt with the same immutable `input_snapshot`.
  - `resume`: re-open ReAct on the same ROM, or continue a workflow checkpoint.
  - `requeue`: re-enqueue a standalone skill execution.
- **Lost-executor reconcile**: `cli/src/omni/runtime/execution_ownership.py:49-173` `execution_owner_lost` checks `pid_alive` + `stale_after_s`. `reconcile_lost_executors` partitions by state: pending cancel → `cancelled`; otherwise `interrupted`; workflow-bound requeueable runs go through CAS `_cas_requeue_workflow`; otherwise `_cas_finish_standalone` with `owner_pid` as the optimistic lock (`execution_ownership.py:204-260`).
- **Cancel-persist**: see §6.5.
- **Stream-stall watchdog**: `cli/src/omni/core/llm/idle.py:30-123` `IdleWatchdog` records the last activity timestamp; `await_with_idle` watches both wall clock and silence; `provider_http_timeout` splits httpx `read` into the idle window so a long stream isn't killed by a short timeout. `StreamIdleTimeout` is caught by `RetryingLLMClient` for reconnect.
- **Periodic housekeeping**: `cli/src/omni/runtime/housekeeping.py:30-57` `run_housekeeping` first reconciles lost owners, then prunes `failed/cancelled/interrupted` tasks by `tasks.retention_days` (`succeeded/degraded` are provenance and are kept); artifacts are never pruned by the runtime.
- **Wrap-up terminate**: see §1.2 — every spend reason triggers a `tool_choice="none"` synthesis call, with `microcompact_tool_results(keep_last=2, max_chars=400)` first so the synthesis can read the transcript.

### 9.6 What error recovery deliberately doesn't do

- **No automatic replanning across turn boundaries.** A turn ends; the next turn starts fresh. Cross-turn recovery is via the durable record (workflow checkpoints, recovery plans) and the model itself, not via the loop.
- **No exception squashing at the top.** Every exception is typed and classified; the only "swallow" is in places where the failure mode is known and well-defined (cost-event write, hook execution).

---

## 10. Cross-cutting observations

### 10.1 What the harness does that other harnesses often don't

- **Reach and exposure as separate axes.** Most harnesses conflate "the model can call this" with "the model knows this exists". OmniScientist splits them so token-cost optimisation can't break reach (`react_agent.py:209-225`).
- **Admission control by host-owned sealed outcomes.** The `HostToolRejection(_host_seal)` pattern (`tool_result.py:144-168`) is the same shape as a cryptographic signature: the host is the only entity that can mint a "this tool call succeeded" verdict, and the seal is one-way (a tool can't forge a success).
- **Fail-closed sandbox.** When `sandbox-exec`/`bwrap`/`firejail` is requested but unavailable, the harness raises `SandboxUnavailableError`. The fallback path is "no sandbox + a WARNING" — not "pretend we have a sandbox" (see §4.6).
- **Cancellation is durable.** Process-local cancel vs DB-backed cancel are different things; the harness can be restarted without losing a cancel request, and a restart does not accidentally cancel a long-running turn (`execution_control.py:43-201`).
- **Approvals are grant-shaped, not binary.** Exact / argv prefix / task-bash — three shapes that let a user say "yes, `npm run test`" without saying "yes, `rm -rf`" (`approval_rules.py:108-431`).
- **Five-layer memory with explicit non-cross-session M1.** The single biggest bug in long-running agent harnesses is "the model recalls a conversational aside as durable knowledge". M1's exclusion is the fix (`service.py:149-154`).
- **Sandbox profile denies VCS by default and cannot be relaxed.** Even with `output_roots` configured, `.git` is rejected (`sensitive_paths.py:84-92`). The comment is the design rule: "no output_roots can relax VCS".

### 10.2 What the harness doesn't do (and why that may be deliberate)

- **No cross-device sync.** The product is local-first; the AGENTS.md says so explicitly. Cross-device sync would imply a server, which contradicts the local-first premise.
- **No web dashboard for metrics.** A web UI exists for the workspace view, not for charts. Operators read the SQLite or the JSONL log directly.
- **No Prometheus / OpenTelemetry.** Same reason. If you want metrics, you tail the log file or query the DB.
- **No "register a subagent" API.** The design treats subagents as ephemeral, model-spawned. Specialisation is via `role` strings and `tools` allowlists, not via type tags. This is what keeps the depth gate simple.
- **No fine-grained per-iteration "are you stuck?" reflection.** A single `no_progress_threshold` and the prompt-level hunt-consume steers are what the harness has; it deliberately doesn't add an expensive meta-prompt round.
- **No fine-grained capability tokens for the model.** The harness doesn't issue the model a token that the host then validates; the gate is a synchronous in-process check. The model can't replay the check later.

### 10.3 Files of interest (one-screen reading list)

| Concern | File | Lines |
|---|---|---|
| Main loop | `cli/src/omni/core/react_agent.py` | full file |
| Termination vocabulary | `cli/src/omni/core/termination.py` | 245 |
| Tool surface | `cli/src/omni/agent/tool_surface.py` | 47-93 |
| JSON-Schema compile | `cli/src/omni/core/tool_contracts.py` | 252-333, 410-464 |
| Tool gateway | `cli/src/omni/runtime/tool_gateway.py` | 164, 290-510 |
| Approval | `cli/src/omni/core/approval.py` | 321-803 |
| Approval rules | `cli/src/omni/core/approval_rules.py` | 108-504 |
| Sandbox | `cli/src/omni/skills_runtime/sandbox.py` | 47-405 |
| Subagents | `cli/src/omni/agent/subagents.py` | 60-707 |
| Subagent control | `cli/src/omni/agent/subagent_control.py` | 79-307 |
| Compaction | `cli/src/omni/memory/compaction.py` | 117-396 |
| Memory service | `cli/src/omni/memory/service.py` | 39-750 |
| Sensitive paths | `cli/src/omni/core/sensitive_paths.py` | 22-131 |
| Session lifecycle | `cli/src/omni/agent/session_lifecycle.py` | 181-386 |
| Conversation store | `cli/src/omni/agent/conversation_store.py` | 76-449 |
| Scheduler | `cli/src/omni/runtime/scheduler.py` | 95-500 |
| Schedule service | `cli/src/omni/scheduling/service.py` | 73-552 |
| Cost | `cli/src/omni/agent/cost.py` | 25-342 |
| Hooks | `cli/src/omni/runtime/hooks.py` | 217-509 |
| Tool errors | `cli/src/omni/core/tool_errors.py` | 13-335 |
| Task recovery | `cli/src/omni/runtime/task_recovery.py` | 160+ |
| Idle watchdog | `cli/src/omni/core/llm/idle.py` | 30-123 |
| Turn clock | `cli/src/omni/core/turn_clock.py` | 38-118 |
| Execution control | `cli/src/omni/core/execution_control.py` | 43-201 |
| Cancel-persist | `cli/src/omni/runtime/cancel_persist.py` | 26-198 |
| Logging | `cli/src/omni/runtime/logging_config.py` | 1-371 |
| Storage | `cli/src/omni/storage/db.py`, `cli/src/omni/storage/models.py` | 70-200, full |

---

## Appendix A — Design principles extracted from the code

These are the principles the code keeps re-asserting in its comments and structure; they're the "why" of every decision above.

1. **The model is not the authority.** Wire formats are host-owned and sealed; the model's return value is data, not verdict.
2. **Reach and exposure are separate.** Withholding a tool from the per-iteration catalog is a token-cost decision; removing its reach is a policy decision. Conflating them makes token optimisation break reach.
3. **Every state-mutating path has a host-owned analog.** The host snapshotted, hashed, persisted, locked, ran policy, and the post-execution check uses the *same* hash.
4. **Cancellation is durable, not process-local.** A restart must not cancel a long-running turn; a cancel must survive a restart.
5. **Approvals are grant-shaped, not binary.** A user says "yes to X" by granting X, not by toggling a flag.
6. **Fail-closed is the default for security boundaries.** When a sandbox backend is unavailable, the harness raises; the fallback is the explicit "no sandbox + warning" path.
7. **Memory is layered, and M1 never crosses sessions.** The conversational aside stays in the conversation that produced it.
8. **The harness is a small set of well-named primitives, not a framework.** Reach, exposure, replay_safe, mutating, exposure, circuit key, hunt window, contract hunt, lookup pressure, leftover skill pressure — these are the names. Reading the code, you can predict the next design choice because the vocabulary is consistent.
9. **The harness cites its peers.** The comments compare against Codex, Claude Code / openclaw, and HelixForge when a design choice is shared. This is what makes the code auditable against known reference designs.
