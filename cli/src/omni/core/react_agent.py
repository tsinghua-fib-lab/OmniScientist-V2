"""Bounded ReAct tool-loop agent (ported & trimmed from HelixForge).

Receives an OpenAI-style tool catalog, iteratively calls
``LLMClient.chat_with_tools``, dispatches each requested tool through a
caller-supplied ``ToolInvoker``, and feeds observations back. Terminates on
final text, escalation, or a bound (iterations / tool-calls / seconds).

The agent is DB-free and decoupled from skill execution: the orchestrator
wires the real invoker behind the callback so this stays unit-testable with
a pure-python dict invoker.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from omni.core.execution_budget import ToolExecutionBudget
from omni.core.execution_control import ExecutionCancelled, ExecutionControl
from omni.core.funnel_facts import is_empty_literature_funnel
from omni.core.llm.client import (
    ChatWithToolsResult,
    LLMClient,
    ToolCall,
    _emit_delta,
    bind_llm_event_hook,
    reset_llm_event_hook,
)
from omni.core.llm.errors import classify_llm_exception
from omni.core.llm.idle import IdleWatchdog, await_with_idle
from omni.core.model_catalog import max_output_tokens_for
from omni.core.run_context import RunContextWindow, evidence_checkpoint
from omni.core.scientific_progress import LOOKUP_STEER, MIN_LOOKUP_STREAK, lookup_pressure
from omni.core.termination import OUTPUT_CAP_TRUNCATED, mark_truncated_output
from omni.core.tool_result import (
    ToolResultEnvelope,
    command_result_status,
    is_tool_rejection,
    tool_call_outcome,
    tool_event_output,
    tool_observation,
    tool_rejection_error,
    tool_result_failure,
)
from omni.core.tool_transcript import normalize_tool_transcript
from omni.core.turn_clock import TurnClock, register_clock
from omni.skills_runtime.admission import is_admission_action

logger = logging.getLogger(__name__)

ESCALATE_RUN_TOOL_NAME = "escalate_run"

_TOOL_RETRY_MAX = 1
_TOOL_RETRY_BASE_DELAY = 0.5
_CIRCUIT_BREAKER_MAX = 5
# Calls that were refused before any work happened: an unknown tool, arguments
# that will not parse, a host policy refusal. Feeding the error back is what lets
# the model self-correct, so allow several rounds — but not an unbounded stream,
# which is how a model that keeps inventing tool names, or keeps re-issuing a
# call the host has already refused, silently consumed an entire iteration
# budget. A host refusal is deterministic: re-issuing it can never start working.
_MAX_UNEXECUTED_CALL_STREAK = 5
# After find_skill has already returned a callable skill contract, further
# catalog/docs/filesystem probes of *that same unanswered card* are the BUG-11
# exploration loop. A second find_skill for a disjoint skill is setup for
# another consume (figure then slides), not a hunt. Two hunt tools is the
# floor so a single successful lookup can still be followed by run_skill.
_CONTRACT_HUNT_TOOLS = frozenset(
    {"find_skill", "docs_search", "docs_read", "glob", "search_tasks", "list_dir"}
)
_CONTRACT_CONSUME_TOOLS = frozenset({"run_skill", "run_workflow"})
_MIN_CONTRACT_HUNT_STREAK = 2
CONTRACT_HUNT_STEER = (
    "A skill input_schema was already returned for that skill. "
    "Call run_skill with those fields now. Do not find_skill, docs_search, "
    "or glob the same contract again. Looking up a different skill for another "
    "owed deliverable is fine."
)

# Bounded stops caused by spend rather than by the work being finished. They get
# a cheaper wrap-up call than other stops, but they still get one.
_BUDGET_REASONS = frozenset({"max_total_tokens", "max_cost"})
# Wrap-up calls that shrink old tool dumps first. Stall/timeout join the spend
# bounds: a long research turn that hits the idle window still has a bulky
# transcript, and the 45s finalization reserve cannot digest it otherwise.
_COMPACT_WRAP_REASONS = _BUDGET_REASONS | {"stalled", "timeout"}
_UNEXECUTED_CALL_CODES = frozenset(
    {
        "unknown_tool",
        "tool_arguments_invalid",
        "tool_arguments_truncated",
        "tool_policy_rejected",
    }
)
# Meta-tools take the real unit of work as an argument, so keying the circuit
# breaker on the tool name alone would let one broken skill disable the router
# for every other skill. Discriminate by the argument that names the callee.
_META_TOOL_SUBJECT_ARGS = {
    "run_skill": ("skill", "name"),
    "find_skill": ("skill", "name"),
    "run_workflow": ("workflow", "name"),
}
_RETRYABLE = frozenset(
    {
        "TimeoutError", "ConnectError", "ConnectionError", "ReadError", "WriteError",
        "RemoteProtocolError", "HTTPStatusError", "RateLimitError", "ServiceUnavailable",
    }
)

# Opening-turn tool constraint (see ``require_opening_tool``). We never send a
# provider ``tool_choice="required"`` because not every upstream honors it (some
# reject it with a hard 4xx). Instead we steer with a prompt nudge and verify the
# call landed, mirroring openclaw's tool_choice contract; codex likewise always
# sends ``"auto"`` on the wire.
_OPENING_TOOL_DIRECTIVE = (
    "[System] You must call one of the available tools to proceed before "
    "answering. Do not respond in prose on this turn."
)
_OPENING_TOOL_CORRECTION = (
    "[System] You answered in prose, but this request must be handled through a "
    "tool call. Call the appropriate tool now instead of replying in text."
)
# A single corrective re-prompt is enough to recover a stray prose turn; more
# would risk spinning, and the loop's iteration ceiling is the ultimate backstop.
_MAX_OPENING_CORRECTIONS = 1

# The provider drops structurally unusable tool calls (blank function name)
# before they reach the loop. When that leaves the turn with nothing at all —
# no admitted call and no prose — the model *tried* to act and the transport
# was malformed. Finishing here would emit an empty answer under reason
# ``done``; instead ask the model to re-issue the call properly, bounded so a
# persistently broken provider still converges on the ceiling.
_MALFORMED_TOOL_CALL_CORRECTION = (
    "[System] Your previous message contained a tool call with no function name, "
    "so it could not be executed. Re-issue the call with an explicit tool name "
    "from the available tools, or answer directly in text."
)
_MAX_MALFORMED_CORRECTIONS = 2

ToolInvoker = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def _emit_event(
    callback: Callable[[str, dict[str, Any]], Any] | None,
    phase: str,
    data: dict[str, Any],
) -> None:
    if callback is None:
        return
    result = callback(phase, data)
    if inspect.isawaitable(result):
        await result


def _is_retryable(exc: Exception) -> bool:
    name = type(exc).__name__
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    return name in _RETRYABLE


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    # Host-only execution metadata. It is intentionally excluded from the
    # provider-facing tool schema below: a model cannot grant replay authority.
    replay_safe: bool = False
    # Whether this turn *advertises* the tool, which is a separate question from
    # whether it may run. ``deferred`` keeps a tool fully dispatchable while
    # leaving its schema out of the per-iteration ``tools`` array — the cost of a
    # schema is paid on every iteration, so a rarely-needed one is worth omitting.
    #
    # Advertising and reachability were previously the same list, so withholding a
    # tool to save tokens also removed it, and the model was told
    # ``unknown tool 'write_file'`` after doing the work it needed to save.
    # Keeping them separate is what makes that outcome unrepresentable rather
    # than merely unlikely: only ``ToolPolicy`` denial removes reach. Codex draws
    # the same line — ``build_model_visible_specs`` filters advertised specs by
    # exposure while ``ToolRegistry::tool`` dispatches by name without consulting
    # it, so a deferred tool the model names anyway still executes.
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


@dataclass(slots=True)
class ToolInvocationRecord:
    name: str
    arguments: dict[str, Any]
    call_id: str = ""
    result: Any = None
    observation: str | None = None
    error: str | None = None
    status: Literal["succeeded", "failed", "rejected", "cancelled", "timed_out"] = "succeeded"
    error_code: str = ""
    attempts: int = 0
    duration_ms: float = 0.0
    retryable: bool = False
    lifecycle_status: Literal["completed", "failed", "blocked", "aborted", "timed_out"] = "completed"
    result_success: bool | None = True

    def to_observation(self) -> str:
        if self.error:
            payload: dict[str, Any] = {
                "status": self.status,
                "error": self.error,
                "reason": self.error_code or "tool_error",
                "retryable": self.retryable,
            }
            # A bare "input failed contract validation" string leaves the model no
            # signal to self-correct; the gateway already recorded *which* field
            # failed and why (e.g. path ``when.trigger_kind``, an enum violation), so
            # surface that so the next iteration can retry with a fixed argument
            # rather than thrash on the generic rejection.
            fields = _contract_violation_fields(self.result)
            if fields:
                payload["field_errors"] = fields
            return json.dumps(payload, ensure_ascii=False)
        if self.observation is not None:
            return self.observation
        if isinstance(self.result, str):
            return self.result
        try:
            return json.dumps(self.result, ensure_ascii=False, default=str)
        except Exception:
            return str(self.result)


@dataclass(slots=True)
class AgentLoopResult:
    kind: Literal["text", "escalated", "error", "partial", "needs_input"]
    content: str = ""
    tool_trace: list[ToolInvocationRecord] = field(default_factory=list)
    escalated_goal: str | None = None
    escalated_reason: str | None = None
    total_iterations: int = 0
    total_tool_calls: int = 0
    tool_budget: dict[str, int | bool | None] = field(default_factory=dict)
    usage_budget: dict[str, int | float | bool] = field(default_factory=dict)
    total_usage: dict[str, int] = field(default_factory=dict)
    terminated_reason: str = "done"
    transcript_repairs: list[str] = field(default_factory=list)

    def tool_names(self) -> list[str]:
        return [r.name for r in self.tool_trace if r.status != "rejected"]


def build_escalate_run_tool_spec() -> ToolSpec:
    return ToolSpec(
        name=ESCALATE_RUN_TOOL_NAME,
        description=(
            "Escalate the conversation into a long-running background research "
            "task when it needs multi-stage orchestration (full paper drafting, "
            "long literature synthesis) that a few direct tool calls cannot finish."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal_summary": {"type": "string", "description": "One-sentence research goal."},
                "reasoning": {"type": "string", "description": "Why escalation is needed."},
            },
            "required": ["goal_summary"],
        },
    )


class ReActLoopAgent:
    """Progress-driven tool-use agent with optional scoped limits."""

    def __init__(
        self,
        llm_client: LLMClient,
        tool_invoker: ToolInvoker,
        *,
        max_iterations: int | None = 6,
        max_tool_calls: int | None = 12,
        max_seconds: float = 120.0,
        stall_timeout_s: float = 0.0,
        soft_timeout_s: float = 0.0,
        finalization_timeout_s: float = 45.0,
        finalization_attempts: int = 2,
        shared_tool_budget: ToolExecutionBudget | None = None,
        max_total_tokens: int = 0,
        max_cost_usd: float = 0.0,
        warn_total_tokens: int = 0,
        warn_cost_usd: float = 0.0,
        input_cost_per_mtok: float = 0.0,
        output_cost_per_mtok: float = 0.0,
        temperature: float = 0.2,
        max_tokens: int = 0,
        soft_token_limit: int = 0,
        context_rollover_token_limit: int = 0,
        context_checkpoint_max_tokens: int = 4096,
        microcompact_keep_tool_results: int = 0,
        observation_max_chars: int = 8000,
        observation_spill_dir: str | None = None,
        compose_needs_input: bool = True,
        no_progress_threshold: int = 2,
        parallel_tools: bool = True,
        require_opening_tool: bool = False,
        owes_scientific_outputs: bool = False,
        fact_feed: Any | None = None,
    ) -> None:
        self._llm = llm_client
        self._invoke = tool_invoker
        self._max_iterations = (
            None if max_iterations is None else max(0, int(max_iterations))
        )
        self._max_tool_calls = (
            None if max_tool_calls is None else max(0, int(max_tool_calls))
        )
        self._max_seconds = max(1.0, max_seconds)
        # Progress watchdog (layer 1): the longest a *single* model call may go
        # *quiet* (no SSE / delta / tool-call fragment) before the loop forces a
        # graceful synthesis (reason ``stalled``). Activity resets the window, so
        # a long streaming draft is never clipped — only a hung/stuck call is.
        # 0 disables it (unit tests that construct the loop directly).
        self._stall_timeout_s = max(0.0, stall_timeout_s)
        # Soft foreground threshold (layer 3): once the turn passes this the loop
        # emits one ``notice``/``soft_timeout`` event so the surface reaffirms the
        # task id and long-running status. It never stops or fails the turn.
        self._soft_timeout_s = max(0.0, soft_timeout_s)
        self._finalization_timeout_s = max(1.0, finalization_timeout_s)
        self._finalization_attempts = max(1, int(finalization_attempts))
        self._shared_tool_budget = shared_tool_budget
        self._max_total_tokens = max(0, int(max_total_tokens))
        self._max_cost_usd = max(0.0, float(max_cost_usd))
        self._warn_total_tokens = max(0, int(warn_total_tokens))
        self._warn_cost_usd = max(0.0, float(warn_cost_usd))
        self._input_cost_per_mtok = max(0.0, float(input_cost_per_mtok))
        self._output_cost_per_mtok = max(0.0, float(output_cost_per_mtok))
        self._usage_warned = False
        self._temperature = temperature
        # A caller that says nothing gets the model's own cap rather than a flat
        # constant. A fixed default is invisible until a response is long enough
        # to hit it, and then it truncates a tool call instead of raising.
        self._max_tokens = max_tokens or max_output_tokens_for(
            getattr(llm_client, "model", "")
        )
        # When the model emits several tool calls in one turn, dispatch them
        # concurrently (Claude/Codex-style) instead of strictly serially — the
        # latency win on multi-tool turns. A single-call batch is unaffected
        # (awaited directly) so existing sequential semantics/tests are preserved.
        self._parallel_tools = parallel_tools
        # Microcompact (Claude-style, P2): once the running context passes
        # ``soft_token_limit``, shrink older tool observations before the next
        # model call, keeping the most recent N intact. 0 on either disables it.
        self._soft_token_limit = max(0, soft_token_limit)
        self._context_rollover_token_limit = max(0, int(context_rollover_token_limit))
        self._context_checkpoint_max_tokens = max(
            256, int(context_checkpoint_max_tokens)
        )
        self._microcompact_keep = max(0, microcompact_keep_tool_results)
        self._observation_max_chars = max(0, int(observation_max_chars))
        self._observation_spill_dir = observation_spill_dir
        # Stopping once calls stop advancing, and always writing a real final
        # answer at whatever bound was hit, are not options — a caller that turns
        # either off burns its whole budget and hands back a stub. They were
        # switchable only because one legacy flag conflated them with the one
        # thing a caller genuinely varies: whether a clarification is rendered as
        # the model's own question (default) or left as the tool's typed payload,
        # which the workflow layer depends on.
        self._compose_needs_input = compose_needs_input
        self._no_progress_threshold = max(1, no_progress_threshold)
        # A capability surface can require its *opening* model turn to go through
        # a tool. A scheduling turn uses this so an ambiguous time is resolved
        # *through* ``schedule_task`` (a structured ``needs_input`` clarification
        # the loop can suspend on) rather than the model dead-ending in a prose
        # question that produces no schedule event and mislabels a legitimate
        # clarification as a failed turn. We enforce it the provider-agnostic way
        # (prompt nudge + verify-and-self-correct), never by sending an upstream
        # ``tool_choice="required"`` some providers reject. Once a call is on the
        # trace the model is free to compose or self-correct, so it never blocks
        # convergence.
        self._require_opening_tool = require_opening_tool
        # When the plan still owes a figure / manuscript / slides / review,
        # memory and task probes are lookup, not progress. Answer-only and
        # inspect/review turns leave this false so those tools remain the work.
        self._owes_scientific_outputs = owes_scientific_outputs
        # Host-owned task facts (snapshot / delta / deterministic debt). The
        # loop only injects observations; it never stages or picks tools.
        self._fact_feed = fact_feed
        self._circuit: dict[str, int] = {}
        # Calls that never executed are counted separately from provider
        # failures. They never reach a provider, so the circuit breaker cannot
        # see them, and each attempt can carry different arguments — which also
        # hides them from the signature-keyed no-progress detector. Keyed by
        # *kind*, a model cycling through several invented tool names, or
        # re-issuing a refused write under fresh paths, is caught as one pattern.
        self._unexecuted_calls: dict[str, int] = {}

    async def run(
        self,
        *,
        system_prompt: str,
        user_message: str,
        tools: list[ToolSpec],
        history: list[dict[str, Any]] | None = None,
        allow_escalation: bool = False,
        on_tool_event: Callable[[str, dict[str, Any]], None] | None = None,
        on_token: Callable[[str], Any] | None = None,
        on_control: Callable[[], Any] | None = None,
        execution_control: ExecutionControl | None = None,
    ) -> AgentLoopResult:
        control = execution_control or ExecutionControl(on_control)
        # One pausable wall-clock for this turn. Registering it before
        # ``control.run`` (which schedules ``_run`` as a task via
        # ``ensure_future``) means the task's copied context carries the clock,
        # so the approval gate can pause it from deep in the dispatch stack.
        clock = TurnClock(self._max_seconds)
        try:
            with register_clock(clock):
                return await control.run(
                    self._run(
                        system_prompt=system_prompt,
                        user_message=user_message,
                        tools=tools,
                        history=history,
                        allow_escalation=allow_escalation,
                        on_tool_event=on_tool_event,
                        on_token=on_token,
                        control=control,
                        clock=clock,
                    )
                )
        except ExecutionCancelled:
            return self._cancelled_result(
                [],
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                0,
                ToolExecutionBudget(self._max_tool_calls, parent=self._shared_tool_budget),
                user_message=user_message,
                transcript_repairs=[],
            )

    async def _run(
        self,
        *,
        system_prompt: str,
        user_message: str,
        tools: list[ToolSpec],
        history: list[dict[str, Any]] | None,
        allow_escalation: bool,
        on_tool_event: Callable[[str, dict[str, Any]], None] | None,
        on_token: Callable[[str], Any] | None,
        control: ExecutionControl,
        clock: TurnClock,
    ) -> AgentLoopResult:
        effective_tools = list(tools)
        if allow_escalation and not any(t.name == ESCALATE_RUN_TOOL_NAME for t in effective_tools):
            effective_tools.append(build_escalate_run_tool_spec())
        # Two views of one list, and the asymmetry between them is the point.
        # ``tool_specs`` is what this iteration pays to advertise; ``tools_by_name``
        # is what it can run, and it stays complete. Deriving both from the same
        # filtered list is what turned a token saving into ``unknown tool
        # 'write_file'`` mid-turn. Only ``ToolPolicy`` removes reach, upstream of
        # here; exposure only decides what a schema costs.
        advertised = {t.name for t in effective_tools if t.exposure == "direct"}
        tool_specs = [t.to_openai_spec() for t in effective_tools if t.name in advertised]
        tools_by_name = {tool.name: tool for tool in effective_tools}

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for prior in history or []:
            if prior.get("role") in {"user", "assistant", "tool"}:
                messages.append(
                    {
                        k: v
                        for k, v in prior.items()
                        if k in {"role", "content", "name", "tool_call_id", "tool_calls"}
                    }
                )
        messages.append({"role": "user", "content": user_message})

        trace: list[ToolInvocationRecord] = []
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        # Operator-facing spend: coordinator calls plus nested engine usage.
        # ``total_usage`` stays ReAct-only so the coordinator ``cost.usage``
        # event does not double-count ``engine:<skill>`` events.
        reported_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        # Pausable wall-clock: approval waits are excluded via ``clock.pause``
        # in the approval gate, so a long human decision no longer times out a
        # turn whose command actually succeeded.
        budget = ToolExecutionBudget(self._max_tool_calls, parent=self._shared_tool_budget)
        transcript_repairs: list[str] = []
        last: ChatWithToolsResult | None = None
        seen_observations: dict[str, str] = {}
        stalled_patterns: dict[tuple[str, str], int] = {}
        no_progress = 0
        # Soft foreground threshold bookkeeping: emit the "still working" notice
        # at most once per turn (layer 3), without ever stopping the loop.
        soft_notified = False
        # Opening-turn tool constraint bookkeeping (see ``require_opening_tool``).
        opening_nudged = False
        opening_corrections = 0
        malformed_corrections = 0
        context_window = RunContextWindow(self._context_rollover_token_limit)

        iteration = 0
        lookup_steered = False
        hunt_steered_for: frozenset[str] = frozenset()
        while self._max_iterations is None or iteration < self._max_iterations:
            iteration += 1
            if control.cancel_requested:
                return self._cancelled_result(
                    trace,
                    total_usage,
                    iteration - 1,
                    budget,
                    user_message=user_message,
                    transcript_repairs=transcript_repairs,
                )
            steer = control.take_steering()
            if steer:
                messages.append(
                    {
                        "role": "user",
                        "content": "[User steering during execution]\n" + "\n".join(f"- {item}" for item in steer),
                    }
                )
                desk = await self._fact_text("after_steer")
                if desk:
                    messages.append({"role": "user", "content": desk})
            # Layer 3 — soft foreground threshold: emit one "still working"
            # notice so the surface reaffirms the task id and long-running status.
            # The turn keeps going; the reference agents never fail a turn here.
            if (
                self._soft_timeout_s > 0
                and not soft_notified
                and (self._max_seconds - clock.remaining()) >= self._soft_timeout_s
            ):
                soft_notified = True
                await _emit_event(
                    on_tool_event,
                    "notice",
                    {
                        "kind": "soft_timeout",
                        "elapsed_s": round(self._max_seconds - clock.remaining(), 1),
                        "soft_timeout_s": self._soft_timeout_s,
                    },
                )
            # Layer 2 — overall hard ceiling: never a bare "execution timed out"
            # failure that discards completed tool results. Force one graceful
            # final synthesis over what we already have and settle ``degraded``.
            if clock.expired():
                return await self._terminate_or_synthesize(
                    messages, trace, total_usage, iteration - 1, budget.completed,
                    user_message=user_message,
                    reason="timeout",
                    salvage=(
                        "The turn reached its overall time budget; this is the "
                        "best answer from the results gathered so far."
                    ),
                    budget=budget,
                    transcript_repairs=transcript_repairs,
                )
            normalized = normalize_tool_transcript(messages)
            messages = normalized.messages
            new_repairs = [
                repair for repair in normalized.repairs if repair not in transcript_repairs
            ]
            transcript_repairs.extend(new_repairs)
            if new_repairs:
                await _emit_event(
                    on_tool_event,
                    "transcript",
                    {"status": "repaired", "repairs": new_repairs},
                )
            self._maybe_microcompact(messages)
            messages = await self._maybe_rollover_context(
                messages,
                trace,
                total_usage,
                context_window,
                clock=clock,
                system_prompt=system_prompt,
                user_message=user_message,
                tool_specs=tool_specs,
                on_tool_event=on_tool_event,
            )
            rollover_limit_reason = self._usage_limit_reason(total_usage)
            if rollover_limit_reason:
                await _emit_event(
                    on_tool_event,
                    "budget",
                    {
                        "status": "wrap_up",
                        "reason": rollover_limit_reason,
                        "budget": budget.snapshot(),
                        "usage_budget": self._usage_budget_snapshot(total_usage),
                    },
                )
                return await self._terminate_or_synthesize(
                    messages,
                    trace,
                    total_usage,
                    iteration - 1,
                    budget.completed,
                    user_message=user_message,
                    reason=rollover_limit_reason,
                    salvage=(
                        "The configured cumulative quota was reached while "
                        "compacting context; this answer uses evidence gathered so far."
                    ),
                    budget=budget,
                    transcript_repairs=transcript_repairs,
                )
            if clock.expired():
                return await self._terminate_or_synthesize(
                    messages,
                    trace,
                    total_usage,
                    iteration - 1,
                    budget.completed,
                    user_message=user_message,
                    reason="timeout",
                    salvage=(
                        "The turn reached its overall time budget while compacting "
                        "context; this is the best answer from the evidence gathered."
                    ),
                    budget=budget,
                    transcript_repairs=transcript_repairs,
                )
            # Steer the *opening* turn through a tool when this surface requires
            # it (e.g. scheduling) with a prompt nudge — not a wire
            # ``tool_choice="required"`` some providers reject. The wire choice is
            # always "auto" (as codex sends); the nudge + the verify/self-correct
            # branch below do the enforcing, so an ambiguous schedule resolves
            # through ``schedule_task`` instead of a prose dead-end.
            if (
                self._require_opening_tool
                and tool_specs
                and not trace
                and not opening_nudged
            ):
                messages.append({"role": "user", "content": _OPENING_TOOL_DIRECTIVE})
                opening_nudged = True
            hook_token = bind_llm_event_hook(on_tool_event)
            watchdog = IdleWatchdog()

            async def _on_delta(piece: str, _watchdog: IdleWatchdog = watchdog) -> None:
                _watchdog.tick()
                if on_token is not None:
                    await _emit_delta(on_token, piece)

            try:
                # Stream whenever a token sink is wired *or* the idle watchdog is
                # armed, so SSE activity is visible. A non-streaming hang still
                # trips idle because no tick arrives. When the client retries
                # idle itself (RetryingLLMClient), this wait is only the turn
                # wall clock — otherwise a quiet first attempt would cancel the
                # reconnect loop.
                use_stream = hasattr(self._llm, "chat_with_tools_stream") and (
                    on_token is not None or self._stall_timeout_s > 0
                )
                if use_stream:
                    call = self._llm.chat_with_tools_stream(
                        messages, tool_specs, tool_choice="auto",
                        temperature=self._temperature, max_tokens=self._max_tokens,
                        on_delta=_on_delta,
                        on_activity=watchdog.tick,
                    )
                else:
                    call = self._llm.chat_with_tools(
                        messages, tool_specs, tool_choice="auto",
                        temperature=self._temperature, max_tokens=self._max_tokens,
                    )
                wait_stall = (
                    0.0
                    if getattr(self._llm, "retries_on_idle", False)
                    else self._stall_timeout_s
                )
                last = await await_with_idle(
                    call,
                    stall_s=wait_stall,
                    deadline=time.monotonic() + max(0.01, clock.remaining()),
                    watchdog=watchdog,
                )
            except asyncio.CancelledError:
                if not control.cancel_requested:
                    raise
                # Outer ExecutionControl cancelled this task. Consume that so
                # the orchestrator can still write execution.finished / react.finished.
                _clear_task_cancellation()
                return self._cancelled_result(
                    trace,
                    total_usage,
                    iteration - 1,
                    budget,
                    user_message=user_message,
                    transcript_repairs=transcript_repairs,
                )
            except TimeoutError:
                # A model call that overran a time layer is a *bounded* stop when
                # the turn already produced results: synthesize a best-effort
                # answer (overall ceiling → ``timeout``; idle watchdog →
                # ``stalled``) and settle ``degraded``. Only a first, zero-progress
                # call that never returned is an honest hard failure.
                if trace:
                    hard_expired = clock.expired()
                    reason = "timeout" if hard_expired else "stalled"
                    salvage = (
                        "The turn reached its overall time budget; this is the "
                        "best answer from the results gathered so far."
                        if hard_expired
                        else "The model stopped making progress within the "
                        "watchdog window; this is the best answer from the "
                        "results gathered so far."
                    )
                    return await self._terminate_or_synthesize(
                        messages, trace, total_usage, iteration - 1, budget.completed,
                        user_message=user_message,
                        reason=reason,
                        salvage=salvage,
                        budget=budget,
                        transcript_repairs=transcript_repairs,
                    )
                return self._finalize(
                    "error", trace, total_usage, iteration - 1, budget.completed,
                    terminated_reason="llm_timeout",
                    content=(
                        "The model did not respond within the time budget and no "
                        "intermediate results were produced."
                    ),
                    budget=budget, transcript_repairs=transcript_repairs,
                )
            except Exception as exc:  # noqa: BLE001
                info = classify_llm_exception(exc)
                logger.info(
                    "[react] LLM call failed iter=%d category=%s status=%s request_id=%s",
                    iteration, info.category, info.status_code, info.request_id or "-",
                )
                logger.debug("[react] provider failure detail: %s", info.internal_detail, exc_info=True)
                if trace and info.category in {"timeout", "unavailable"}:
                    hard_expired = clock.expired()
                    reason = "timeout" if hard_expired else "stalled"
                    salvage = (
                        "The turn reached its overall time budget; this is the "
                        "best answer from the results gathered so far."
                        if hard_expired
                        else "The model stopped making progress within the "
                        "watchdog window; this is the best answer from the "
                        "results gathered so far."
                    )
                    return await self._terminate_or_synthesize(
                        messages, trace, total_usage, iteration - 1, budget.completed,
                        user_message=user_message,
                        reason=reason,
                        salvage=salvage,
                        budget=budget,
                        transcript_repairs=transcript_repairs,
                    )
                return self._finalize(
                    "error", trace, total_usage, iteration - 1, budget.completed,
                    terminated_reason=info.terminated_reason, content=info.user_message,
                    budget=budget, transcript_repairs=transcript_repairs,
                )
            finally:
                reset_llm_event_hook(hook_token)

            _accumulate_usage(total_usage, last.usage)
            _accumulate_usage(reported_usage, last.usage)
            await self._emit_usage_progress(on_tool_event, reported_usage)
            usage_limit_reason = self._usage_limit_reason(reported_usage)

            if not last.has_tool_calls:
                # Verify the opening-turn tool constraint (openclaw rejects a
                # text-only turn while a constraint is active). In a stateful loop
                # we do better than reject: re-nudge and let the model self-correct
                # into the tool call, bounded so it can never spin. This is what
                # keeps an ambiguous schedule resolving *through* ``schedule_task``
                # without ever depending on a provider-side ``required``.
                opening_grounded = _has_productive_observation(trace)
                if (
                    self._require_opening_tool
                    and not opening_grounded
                    and opening_corrections < _MAX_OPENING_CORRECTIONS
                ):
                    opening_corrections += 1
                    messages.append({"role": "assistant", "content": last.content or ""})
                    messages.append({"role": "user", "content": _OPENING_TOOL_CORRECTION})
                    continue
                if last.malformed_tool_calls and not (last.content or "").strip():
                    if malformed_corrections < _MAX_MALFORMED_CORRECTIONS:
                        malformed_corrections += 1
                        messages.append(
                            {"role": "user", "content": _MALFORMED_TOOL_CALL_CORRECTION}
                        )
                        continue
                    # The model cannot produce a well-formed call on this
                    # surface. Finishing as ``done`` here would hand back an
                    # empty answer under a success reason; stop with a named
                    # cause and still force a best-effort answer.
                    return await self._terminate_or_synthesize(
                        messages, trace, total_usage, iteration, budget.completed,
                        user_message=user_message,
                        reason="malformed_tool_calls",
                        salvage=(
                            "The model repeatedly emitted tool calls with no function "
                            "name, so no tool could be executed."
                        ),
                        budget=budget,
                        transcript_repairs=transcript_repairs,
                    )
                if self._require_opening_tool and not opening_grounded:
                    return self._finalize(
                        "error",
                        trace,
                        total_usage,
                        iteration,
                        budget.completed,
                        terminated_reason="required_opening_tool_missing",
                        content=(
                            "The required tool call did not produce an authoritative "
                            "result, so I cannot safely complete this request."
                        ),
                        budget=budget,
                        transcript_repairs=transcript_repairs,
                    )
                if last.truncated_by_output_cap:
                    # Re-asking is the trap the tool-call side already walked
                    # into: the same prompt under the same ceiling stops at the
                    # same word, and the turn spends its budget proving it.
                    # Deliver what was written and let the reason say it is a
                    # fragment.
                    return self._finalize(
                        "partial", trace, total_usage, iteration, budget.completed,
                        terminated_reason=OUTPUT_CAP_TRUNCATED,
                        content=mark_truncated_output(last.content or ""),
                        budget=budget, transcript_repairs=transcript_repairs,
                    )
                finding = await self._fact_text("before_text_finish")
                if finding:
                    messages.append({"role": "assistant", "content": last.content or ""})
                    messages.append({"role": "user", "content": finding})
                    continue
                return self._finalize(
                    "text", trace, total_usage, iteration, budget.completed,
                    terminated_reason="done", content=last.content or "",
                    budget=budget, transcript_repairs=transcript_repairs,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": last.content or "",
                    "tool_calls": [tc.to_message_fragment() for tc in last.tool_calls],
                    **({"reasoning_content": last.reasoning_content} if last.reasoning_content else {}),
                }
            )

            esc = self._find_escalation(last.tool_calls, allow_escalation)
            if esc is not None:
                for tc in last.tool_calls:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": json.dumps(
                                {
                                    "status": "accepted" if tc is esc else "aborted",
                                    "reason": "run escalated to a durable background task",
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                return self._finalize(
                    "escalated", trace, total_usage, iteration, budget.completed,
                    terminated_reason="escalated",
                    escalated_goal=str(esc.arguments.get("goal_summary", "")) or user_message,
                    escalated_reason=str(esc.arguments.get("reasoning", "")),
                    content="This request was escalated to a background research task; completion will be reported.",
                    budget=budget,
                    transcript_repairs=transcript_repairs,
                )

            # A call must resolve against this turn's catalog before it can spend
            # execution budget. Unknown names and arguments that never parsed are
            # model-repair feedback, not executions (the same boundary Codex uses
            # before tool lifecycle and approval). Every call still receives one
            # result below, in the model's original order.
            batch = last.tool_calls
            outcomes: list[ToolInvocationRecord | None] = [None] * len(batch)
            dispatchable: list[tuple[int, ToolCall]] = []
            for index, tc in enumerate(batch):
                preflight = self._preflight_rejection(tc, tools_by_name)
                if preflight is None:
                    dispatchable.append((index, tc))
                else:
                    outcomes[index] = preflight

            if usage_limit_reason:
                budget.reject(len(dispatchable))
                admitted_count = 0
            else:
                admitted_count = budget.admit(len(dispatchable))
            admitted = dispatchable[:admitted_count]
            budget_rejected = dispatchable[admitted_count:]
            in_budget = [tc for _, tc in admitted]
            admitted_indices = {index for index, _ in admitted}
            budget_rejected_indices = {index for index, _ in budget_rejected}

            for tc in in_budget:
                await _emit_event(on_tool_event, "start", {"name": tc.name, "arguments": tc.arguments})

            dispatch_cancelled = False
            try:
                records = await self._dispatch_batch(in_budget, tools_by_name)
            except asyncio.CancelledError:
                dispatch_cancelled = True
                from omni.core.tool_result import interrupted_tool_payload

                records = []
                for tc in in_budget:
                    payload = interrupted_tool_payload(tc.name, started=True)
                    records.append(
                        ToolInvocationRecord(
                            name=tc.name,
                            arguments=tc.arguments,
                            call_id=tc.id,
                            result=payload,
                            error=str(payload.get("error") or ""),
                            status="cancelled",
                            error_code=str(payload.get("error_code") or "TOOL_OUTCOME_UNKNOWN"),
                            lifecycle_status="aborted",
                            result_success=None,
                        )
                    )

            for (index, _), record in zip(admitted, records, strict=True):
                outcomes[index] = record

            for index, tc in budget_rejected:
                rejected_reason = usage_limit_reason or "max_tool_calls"
                error_code = {
                    "max_total_tokens": "run_token_budget_exhausted",
                    "max_cost": "run_cost_budget_exhausted",
                }.get(rejected_reason, "run_hard_budget_exhausted")
                outcomes[index] = ToolInvocationRecord(
                    name=tc.name,
                    arguments=tc.arguments,
                    call_id=tc.id,
                    error=(
                        "The tool call was not executed because the turn reached its token or cost budget."
                        if usage_limit_reason else
                        "The tool call was not executed because the turn reached its hard tool budget."
                    ),
                    status="rejected",
                    error_code=error_code,
                    lifecycle_status="blocked",
                    result_success=None,
                )

            terminal_record: ToolInvocationRecord | None = None
            promoted = False
            batch_made_progress = False
            for index, (tc, record) in enumerate(zip(batch, outcomes, strict=True)):
                assert record is not None
                record.call_id = tc.id
                if index in admitted_indices:
                    if record.status not in {"rejected", "cancelled", "timed_out"}:
                        record.status = "failed" if record.error else "succeeded"
                    budget.mark_completed()
                # Reaching for a deferred tool proves this turn is the kind that
                # needs it, so stop withholding the schema: the next iteration
                # sees the real parameters rather than working from the name. The
                # guess about what a turn needs is only ever made once, and a turn
                # that disproves it pays the full cost from then on.
                if record.status != "rejected" and tc.name not in advertised:
                    advertised.add(tc.name)
                    promoted = True
                trace.append(record)
                _accumulate_usage(reported_usage, _nested_result_usage(record.result))
                await _emit_event(
                    on_tool_event,
                    "done",
                    {
                        "name": tc.name,
                        "arguments": tc.arguments,
                        "result": record.result,
                        "error": record.error,
                        "status": record.status,
                        "call_id": tc.id,
                        "duration_ms": record.duration_ms,
                        "lifecycle_status": record.lifecycle_status,
                        "result_success": record.result_success,
                    },
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "name": tc.name,
                     "content": record.to_observation()}
                )
                if terminal_record is None and _is_terminal_tool_result(record.result):
                    terminal_record = record
                if index not in budget_rejected_indices:
                    signature = (
                        f"{tc.name}:"
                        f"{json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False, default=str)}"
                    )
                    observation = record.to_observation()
                    repeated = seen_observations.get(signature) == observation
                    seen_observations[signature] = observation
                    pattern = (signature, _stall_outcome(record, observation))
                    if (
                        index in admitted_indices
                        and not _is_unproductive(record)
                        and not repeated
                    ):
                        batch_made_progress = True
                    if _is_unproductive(record):
                        stalled_patterns[pattern] = stalled_patterns.get(pattern, 0) + 1
                    elif repeated:
                        stalled_patterns[pattern] = stalled_patterns.get(pattern, 0) + 1
                    else:
                        # A changed, useful observation proves this call pattern can
                        # still advance. Forget only that signature's stale outcomes.
                        stalled_patterns = {
                            key: count
                            for key, count in stalled_patterns.items()
                            if key[0] != signature
                        }
                    no_progress = max(stalled_patterns.values(), default=0)

            delta = await self._fact_text("after_tool_batch")
            if delta:
                messages.append({"role": "user", "content": delta})

            if promoted:
                tool_specs = [
                    t.to_openai_spec() for t in effective_tools if t.name in advertised
                ]

            if dispatch_cancelled:
                return self._cancelled_result(
                    trace,
                    total_usage,
                    iteration,
                    budget,
                    user_message=user_message,
                    transcript_repairs=transcript_repairs,
                )

            await self._emit_usage_progress(on_tool_event, reported_usage)
            usage_limit_reason = usage_limit_reason or self._usage_limit_reason(reported_usage)
            budget_limited = bool(budget_rejected) or bool(usage_limit_reason)
            if budget_limited:
                await _emit_event(
                    on_tool_event,
                    "budget",
                    {
                        "status": "hard_limit",
                        "reason": usage_limit_reason or "max_tool_calls",
                        "budget": budget.snapshot(),
                        "usage_budget": self._usage_budget_snapshot(total_usage),
                    },
                )

            if terminal_record is not None:
                terminal_kind = _terminal_tool_kind(terminal_record.result)
                # A clarification suspend is surfaced as the *model's* composed
                # question (user's language, options laid out), not the tool's
                # raw payload — matching the rich clarification a reference agent
                # produces before awaiting input. Success/error terminals keep
                # their deterministic, truthful tool-derived content.
                if terminal_kind == "needs_input" and self._compose_needs_input:
                    return await self._compose_terminal_needs_input(
                        messages, trace, total_usage, iteration, budget.completed,
                        terminal_result=terminal_record.result,
                        budget=budget, transcript_repairs=transcript_repairs,
                    )
                return self._finalize(
                    terminal_kind, trace, total_usage, iteration, budget.completed,
                    terminated_reason=_terminal_tool_reason(terminal_record.result),
                    content=_terminal_tool_content(terminal_record.result),
                    budget=budget,
                    transcript_repairs=transcript_repairs,
                )

            if budget_limited:
                return await self._terminate_or_synthesize(
                    messages, trace, total_usage, iteration, budget.completed,
                    user_message=user_message,
                    reason=usage_limit_reason or "max_tool_calls",
                    salvage=(
                        "The answer converged on current results; exploration beyond the token or cost budget was not run."
                        if usage_limit_reason else
                        "The answer converged on current results; exploration beyond the execution budget was not run."
                    ),
                    budget=budget,
                    transcript_repairs=transcript_repairs,
                )

            if (
                not batch_made_progress
                and no_progress >= self._no_progress_threshold
            ):
                return await self._terminate_or_synthesize(
                    messages, trace, total_usage, iteration, budget.completed,
                    user_message=user_message,
                    reason="no_progress",
                    salvage="Repeated tool calls made no progress, so further calls stopped.",
                    budget=budget,
                    transcript_repairs=transcript_repairs,
                )

            hunt = _contract_hunt_pressure(trace)
            if hunt >= max(_MIN_CONTRACT_HUNT_STREAK, self._no_progress_threshold):
                active = _active_hunt_names(trace)
                if active and not (active & hunt_steered_for):
                    messages.append({"role": "user", "content": CONTRACT_HUNT_STEER})
                    hunt_steered_for = active
                else:
                    return await self._terminate_or_synthesize(
                        messages, trace, total_usage, iteration, budget.completed,
                        user_message=user_message,
                        reason="no_progress",
                        salvage=(
                            "A skill input contract was already returned; "
                            "call run_skill instead of searching again."
                        ),
                        budget=budget,
                        transcript_repairs=transcript_repairs,
                    )

            lookup = lookup_pressure(trace, owed=self._owes_scientific_outputs)
            if lookup > 0:
                if not lookup_steered:
                    messages.append({"role": "user", "content": LOOKUP_STEER})
                    lookup_steered = True
                elif lookup >= max(MIN_LOOKUP_STREAK, self._no_progress_threshold):
                    return await self._terminate_or_synthesize(
                        messages, trace, total_usage, iteration, budget.completed,
                        user_message=user_message,
                        reason="no_progress",
                        salvage=(
                            "Lookup tools do not produce this turn's required outputs; "
                            "do the scientific work instead."
                        ),
                        budget=budget,
                        transcript_repairs=transcript_repairs,
                    )

            # A model whose calls keep being refused before they run — unknown
            # tools, unparseable arguments, a quota the host will not lift — is
            # not converging. Each attempt may look unique to the signature-keyed
            # detector above, so this streak is tracked by refusal kind instead.
            if self._unexecuted_call_pressure() >= _MAX_UNEXECUTED_CALL_STREAK:
                return await self._terminate_or_synthesize(
                    messages, trace, total_usage, iteration, budget.completed,
                    user_message=user_message,
                    reason="no_progress",
                    salvage=(
                        "Repeated tool calls could not be executed as issued "
                        "(unknown tool or unparseable arguments), so further calls stopped."
                    ),
                    budget=budget,
                    transcript_repairs=transcript_repairs,
                )

        return await self._terminate_or_synthesize(
            messages, trace, total_usage, iteration, budget.completed,
            user_message=user_message,
            reason="max_iterations",
            salvage="The iteration limit was reached before full convergence.",
            last_content=(last.content if last else "") or "",
            budget=budget,
            transcript_repairs=transcript_repairs,
        )

    async def _terminate_or_synthesize(
        self,
        messages: list[dict[str, Any]],
        trace: list[ToolInvocationRecord],
        usage: dict[str, int],
        iteration: int,
        tool_calls: int,
        *,
        user_message: str,
        reason: str,
        salvage: str,
        last_content: str = "",
        budget: ToolExecutionBudget,
        transcript_repairs: list[str],
    ) -> AgentLoopResult:
        """Prefer a real tool-free synthesized answer; fall back to a stub.

        When tools can no longer make progress (repeated failures / empty
        results / budget spent), force one final model call with tools disabled
        so the user gets a genuine best-effort answer that names what could not
        be verified — instead of a "reached the iteration limit" non-answer.
        """
        if self._require_opening_tool and not _has_productive_observation(trace):
            return self._finalize(
                "error",
                trace,
                usage,
                iteration,
                tool_calls,
                terminated_reason="required_opening_tool_missing",
                content=(
                    "The required tool call did not produce an authoritative "
                    "result, so I cannot safely complete this request."
                ),
                budget=budget,
                transcript_repairs=transcript_repairs,
            )
        synth = await self._synthesize_final(
            messages, trace, usage, iteration, tool_calls,
            user_message=user_message, reason=reason,
            budget=budget, transcript_repairs=transcript_repairs,
        )
        if synth is not None:
            return synth
        content = _salvage_content(salvage, trace, user_message)
        if last_content:
            content += f"\nLast intermediate output: {last_content}"
        return self._finalize(
            "partial", trace, usage, iteration, tool_calls,
            terminated_reason=reason, content=content,
            budget=budget, transcript_repairs=transcript_repairs,
        )

    async def _synthesize_final(
        self,
        messages: list[dict[str, Any]],
        trace: list[ToolInvocationRecord],
        usage: dict[str, int],
        iteration: int,
        tool_calls: int,
        *,
        user_message: str,
        reason: str,
        budget: ToolExecutionBudget,
        transcript_repairs: list[str],
    ) -> AgentLoopResult | None:
        opening = (
            "[System] This turn has reached its budget and no more tools will run "
            f"(reason: {reason}). Write the final answer now from what you already gathered."
            if reason in _BUDGET_REASONS
            else "[System] Tools cannot make further progress "
            f"(reason: {reason}). Answer the user directly and completely using the conversation, "
            "existing tool observations, and prior knowledge."
        )
        directive = (
            f"{opening} Clearly identify any unverified portions. Do not request "
            "more tools or invent citations or data. If an Omni product question could not be grounded in the "
            "built-in documentation, say so explicitly."
        )
        if reason in _COMPACT_WRAP_REASONS:
            # The stop *is* the context being too expensive, so shrink the bulkiest
            # part of it (old tool dumps) before spending one more call on the
            # wrap-up. Truncating in place keeps every tool_call↔tool_result pair
            # linked, which providers reject a request without.
            from omni.memory.compaction import microcompact_tool_results

            microcompact_tool_results(messages, keep_last=2, max_chars=400)
        normalized = normalize_tool_transcript(messages)
        transcript_repairs.extend(
            repair for repair in normalized.repairs if repair not in transcript_repairs
        )
        synth_messages = [*normalized.messages, {"role": "user", "content": directive}]
        # Finalization owns a reserve independent from the exploration deadline,
        # so a useful answer is still attempted at the bound. A transiently slow
        # provider is retried (``finalization_attempts``) rather than collapsing a
        # bounded stop into the salvage stub — every reference agent always tries
        # to deliver a real final answer.
        result: ChatWithToolsResult | None = None
        for attempt in range(1, self._finalization_attempts + 1):
            try:
                result = await asyncio.wait_for(
                    self._llm.chat_with_tools(
                        synth_messages, [], tool_choice="none",
                        temperature=self._temperature, max_tokens=self._max_tokens,
                    ),
                    timeout=self._finalization_timeout_s,
                )
                break
            except TimeoutError:
                if attempt < self._finalization_attempts:
                    logger.info(
                        "[react] final synthesis timed out (attempt %d/%d, reason=%s); retrying",
                        attempt, self._finalization_attempts, reason,
                    )
                    continue
                logger.info(
                    "[react] final synthesis timed out after %d attempt(s) reason=%s",
                    self._finalization_attempts, reason,
                )
                return None
            except Exception as exc:  # noqa: BLE001 — synthesis is best-effort; fall back to stub
                info = classify_llm_exception(exc)
                logger.info(
                    "[react] final synthesis failed reason=%s category=%s status=%s request_id=%s",
                    reason, info.category, info.status_code, info.request_id or "-",
                )
                logger.debug("[react] final synthesis detail: %s", info.internal_detail, exc_info=True)
                return None
        if result is None:
            return None
        content = (result.content or "").strip()
        if not content:
            return None
        _accumulate_usage(usage, result.usage)
        if result.truncated_by_output_cap:
            # The wrap-up turn is where a long inline answer is most likely to
            # be written, since tools are off and everything left to say has to
            # fit in one response. Reporting it as ``synthesized_<bound>`` would
            # name the bound that stopped the tools and hide the one that
            # actually cut the text.
            return self._finalize(
                "partial", trace, usage, iteration, tool_calls,
                terminated_reason=OUTPUT_CAP_TRUNCATED,
                content=mark_truncated_output(content),
                budget=budget, transcript_repairs=transcript_repairs,
            )
        return self._finalize(
            "text", trace, usage, iteration, tool_calls,
            terminated_reason=f"synthesized_{reason}", content=content,
            budget=budget, transcript_repairs=transcript_repairs,
        )

    async def _compose_terminal_needs_input(
        self,
        messages: list[dict[str, Any]],
        trace: list[ToolInvocationRecord],
        usage: dict[str, int],
        iteration: int,
        tool_calls: int,
        *,
        terminal_result: Any,
        budget: ToolExecutionBudget,
        transcript_repairs: list[str],
    ) -> AgentLoopResult:
        """Suspend on a clarification with a *model-composed* question.

        A capability that fails closed to ``outcome == "needs_input"`` (an
        ambiguous scheduling time, a missing disambiguation) carries a
        structured result — a ``message`` and often ``recovery_choices``. Rather
        than surface that raw payload verbatim, give the model one tool-free turn
        to phrase the clarifying question in the user's language and lay out the
        offered options, matching the rich clarification a reference agent
        composes before it suspends. Falls back to the structured payload when
        synthesis is unavailable, so the suspend is never silent, and the turn
        still resolves as ``needs_input`` either way.
        """
        fallback = _terminal_tool_content(terminal_result)
        directive = (
            "[System] A tool reported that it needs one clarification from the "
            "user before it can finish (see its result above, including any "
            "offered options). Reply with a single concise clarifying question "
            "addressed to the user, in the user's language, and lay out the "
            "offered options plainly when the tool provided them. Do not claim "
            "the task is complete and do not call any tools."
        )
        normalized = normalize_tool_transcript(messages)
        transcript_repairs.extend(
            repair for repair in normalized.repairs if repair not in transcript_repairs
        )
        synth_messages = [*normalized.messages, {"role": "user", "content": directive}]
        composed = ""
        result: ChatWithToolsResult | None = None
        for attempt in range(1, self._finalization_attempts + 1):
            try:
                result = await asyncio.wait_for(
                    self._llm.chat_with_tools(
                        synth_messages, [], tool_choice="none",
                        temperature=self._temperature, max_tokens=self._max_tokens,
                    ),
                    timeout=self._finalization_timeout_s,
                )
                break
            except TimeoutError:
                if attempt < self._finalization_attempts:
                    continue
                result = None
            except Exception as exc:  # noqa: BLE001 — clarification compose is best-effort
                info = classify_llm_exception(exc)
                logger.info(
                    "[react] needs_input compose failed category=%s status=%s",
                    info.category, info.status_code,
                )
                result = None
                break
        if result is not None:
            _accumulate_usage(usage, result.usage)
            # A clarifying question exists to be answered, so half of one is
            # worse than none: the tool's own payload is short, complete and
            # already carries the options. Prefer it over a cut-off question.
            if not result.truncated_by_output_cap:
                composed = (result.content or "").strip()
        return self._finalize(
            "needs_input", trace, usage, iteration, tool_calls,
            terminated_reason="needs_input",
            content=composed or fallback,
            budget=budget, transcript_repairs=transcript_repairs,
        )

    def _maybe_microcompact(self, messages: list[dict[str, Any]]) -> None:
        """Trim older tool observations once the running context is large.

        Cheap first-tier compaction for a long single research turn: keeps the
        most recent ``_microcompact_keep`` tool results verbatim and truncates
        older ones. No-op unless both bounds are configured and the estimated
        context exceeds ``_soft_token_limit``.
        """
        if self._soft_token_limit <= 0 or self._microcompact_keep <= 0:
            return
        from omni.memory.compaction import (
            estimate_messages_tokens,
            microcompact_tool_results,
        )

        if estimate_messages_tokens(messages) <= self._soft_token_limit:
            return
        trimmed = microcompact_tool_results(messages, keep_last=self._microcompact_keep)
        if trimmed:
            logger.debug("[react] microcompacted %d old tool result(s)", trimmed)

    async def _maybe_rollover_context(
        self,
        messages: list[dict[str, Any]],
        trace: list[ToolInvocationRecord],
        usage: dict[str, int],
        window: RunContextWindow,
        *,
        clock: TurnClock,
        system_prompt: str,
        user_message: str,
        tool_specs: list[dict[str, Any]],
        on_tool_event: Callable[[str, dict[str, Any]], None] | None,
    ) -> list[dict[str, Any]]:
        """Fold a full active window into a checkpoint and continue the run."""
        if not window.should_rollover(messages, tool_specs):
            return messages

        before = window.pressure(messages, tool_specs)
        directive = (
            "[System] Create a continuation checkpoint for the same agent run. "
            "Do not answer the user. Preserve: the exact objective and constraints; "
            "verified findings with their tool provenance; completed checks; current "
            "plan/progress; unresolved checks and the next best actions. Never turn an "
            "unverified claim into a fact. Raw tool events remain available outside "
            "this prompt, so prefer a concise, operational checkpoint."
        )
        checkpoint = ""
        source = "model"
        steering = tuple(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "user"
            and str(message.get("content") or "").startswith(
                "[User steering during execution]"
            )
        )
        checkpoint_capacity = window.checkpoint_capacity(
            system_prompt=system_prompt,
            user_message=user_message,
            tool_specs=tool_specs,
            steering=steering,
        )
        checkpoint_tokens = min(
            self._context_checkpoint_max_tokens,
            checkpoint_capacity,
        )
        if self._max_tokens > 0:
            checkpoint_tokens = min(checkpoint_tokens, self._max_tokens)
        timeout = min(self._finalization_timeout_s, max(0.0, clock.remaining()))
        if checkpoint_tokens > 0 and timeout > 0:
            try:
                result = await asyncio.wait_for(
                    self._llm.chat_with_tools(
                        [*messages, {"role": "user", "content": directive}],
                        [],
                        tool_choice="none",
                        temperature=self._temperature,
                        max_tokens=checkpoint_tokens,
                    ),
                    timeout=timeout,
                )
                _accumulate_usage(usage, result.usage)
                if not result.truncated_by_output_cap:
                    checkpoint = (result.content or "").strip()
            except Exception as exc:  # noqa: BLE001 — deterministic ledger is the floor
                logger.info(
                    "[react] context checkpoint synthesis failed: %s",
                    type(exc).__name__,
                )
        else:
            source = "objective_only"
        if not checkpoint:
            if source != "objective_only":
                source = "evidence_ledger"
            checkpoint = evidence_checkpoint(
                trace,
                max_chars=min(16_000, checkpoint_capacity * 4),
            )

        desk = await self._fact_text("after_rollover")
        if desk:
            from omni.runtime.research_state import refresh_system_research_brief

            system_prompt = refresh_system_research_brief(system_prompt, desk)
        continued = window.continue_with(
            system_prompt=system_prompt,
            user_message=user_message,
            checkpoint=checkpoint,
            tool_specs=tool_specs,
            steering=steering,
        )
        after = window.pressure(continued, tool_specs)
        window.record(before=before, after=after)
        usage["context_rollovers"] = window.rollovers
        usage["context_last_before_tokens"] = before
        usage["context_last_after_tokens"] = after
        await _emit_event(
            on_tool_event,
            "notice",
            {
                "kind": "context_rollover",
                "status": "compacted",
                "source": source,
                "context_window": window.snapshot(),
            },
        )
        return continued

    def _compact_observation(self, value: Any) -> str:
        from omni.core.observation import compact_observation

        return compact_observation(
            value,
            max_chars=self._observation_max_chars,
            spill_dir=self._observation_spill_dir,
        )

    async def _fact_text(self, method: str) -> str:
        feed = self._fact_feed
        if feed is None:
            return ""
        fn = getattr(feed, method, None)
        if not callable(fn):
            return ""
        try:
            result = fn()
            if inspect.isawaitable(result):
                result = await result
        except Exception:  # noqa: BLE001 — facts must not abort the turn
            return ""
        return str(result or "").strip()

    async def _emit_usage_progress(
        self,
        on_tool_event: Callable[[str, dict[str, Any]], None] | None,
        usage: dict[str, int],
    ) -> None:
        snapshot = self._usage_budget_snapshot(usage)
        if int(usage.get("total_tokens") or 0) or float(snapshot.get("cost_usd") or 0.0):
            await _emit_event(on_tool_event, "notice", {"kind": "usage", **snapshot})
        reason = self._usage_warn_reason(usage)
        if not reason or self._usage_warned:
            return
        self._usage_warned = True
        await _emit_event(
            on_tool_event,
            "notice",
            {"kind": "usage_warn", "reason": reason, **snapshot},
        )

    def _usage_warn_reason(self, usage: dict[str, int]) -> str:
        total_tokens = int(usage.get("total_tokens") or 0)
        if self._warn_total_tokens and total_tokens >= self._warn_total_tokens:
            return "warn_total_tokens"
        if self._warn_cost_usd and self._usage_cost_usd(usage) >= self._warn_cost_usd:
            return "warn_cost"
        return ""

    def _usage_limit_reason(self, usage: dict[str, int]) -> str:
        total_tokens = int(usage.get("total_tokens") or 0)
        if self._max_total_tokens and total_tokens >= self._max_total_tokens:
            return "max_total_tokens"
        if self._max_cost_usd and self._usage_cost_usd(usage) >= self._max_cost_usd:
            return "max_cost"
        return ""

    def _usage_cost_usd(self, usage: dict[str, int]) -> float:
        prompt = max(0, int(usage.get("prompt_tokens") or 0))
        completion = max(0, int(usage.get("completion_tokens") or 0))
        return (
            prompt * self._input_cost_per_mtok
            + completion * self._output_cost_per_mtok
        ) / 1_000_000.0

    def _usage_budget_snapshot(self, usage: dict[str, int]) -> dict[str, int | float | bool]:
        snapshot: dict[str, int | float | bool] = {
            "max_total_tokens": self._max_total_tokens,
            "max_cost_usd": self._max_cost_usd,
            "total_tokens": int(usage.get("total_tokens") or 0),
            "cost_usd": round(self._usage_cost_usd(usage), 6),
            "enforced": bool(self._max_total_tokens or self._max_cost_usd),
        }
        if usage.get("context_rollovers"):
            snapshot.update(
                {
                    "context_rollovers": int(usage["context_rollovers"]),
                    "context_last_before_tokens": int(
                        usage.get("context_last_before_tokens") or 0
                    ),
                    "context_last_after_tokens": int(
                        usage.get("context_last_after_tokens") or 0
                    ),
                }
            )
        return snapshot

    async def _dispatch_batch(
        self, calls: list[ToolCall], tools_by_name: dict[str, ToolSpec]
    ) -> list[ToolInvocationRecord]:
        """Dispatch a turn's tool calls, concurrently when enabled.

        Each invocation is owned by a task so cancellation can wait for nested
        workflow and subprocess cleanup before the ReAct turn returns.
        """
        if not calls:
            return []
        if len(calls) == 1 or not self._parallel_tools:
            records: list[ToolInvocationRecord] = []
            for call in calls:
                task = asyncio.create_task(self._dispatch_tool(call, tools_by_name))
                try:
                    records.append(await task)
                except asyncio.CancelledError:
                    await asyncio.gather(task, return_exceptions=True)
                    raise
            return records

        tasks = [
            asyncio.create_task(self._dispatch_tool(call, tools_by_name))
            for call in calls
        ]
        try:
            return list(await asyncio.gather(*tasks))
        except asyncio.CancelledError:
            # Cancelling the await on gather has already propagated one
            # cancellation into every unfinished child.  A second cancel here
            # can interrupt the child's own checkpoint/finalization handler.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def _preflight_rejection(
        self,
        tc: ToolCall,
        tools_by_name: dict[str, ToolSpec],
    ) -> ToolInvocationRecord | None:
        """Reject calls that cannot start, before lifecycle, budget, or approval."""
        record = ToolInvocationRecord(name=tc.name, arguments=tc.arguments, call_id=tc.id)
        if tc.name not in tools_by_name:
            available = ", ".join(sorted(tools_by_name))
            record.error = (
                f"unknown tool '{tc.name}'; available tools: {available}"
                if available
                else f"unknown tool '{tc.name}'; this turn exposes no tools in its effective catalog"
            )
            record.status = "rejected"
            record.error_code = "unknown_tool"
            record.lifecycle_status = "blocked"
            record.result_success = None
            self._note_unexecuted_call(record.error_code)
            return record

        if self._circuit.get(_circuit_key(tc), 0) >= _CIRCUIT_BREAKER_MAX:
            return None
        if not tc.arguments_error:
            return None

        # The arguments did not parse. *Why* decides what the model should do,
        # and getting it wrong costs a whole turn: telling a model to re-send
        # valid JSON when the host truncated it invites the identical call,
        # cut at the identical place, until some other bound stops the run.
        raw = tc.raw_arguments or ""
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        if tc.arguments_truncated:
            record.error = (
                f"this call was cut off at the output-token limit after {len(raw)} "
                "characters of arguments, so it never arrived complete. Re-sending "
                "it unchanged will be cut at the same place. Send less in one call: "
                "write long content in successive chunks (write_file, then append "
                "the rest), or narrow the request."
            )
            record.error_code = "tool_arguments_truncated"
        else:
            record.error = (
                f"could not parse tool arguments as JSON ({tc.arguments_error}); "
                "re-issue this call with a valid JSON object for 'arguments'; "
                f"raw_length={len(raw)} raw_sha256={digest}"
            )
            record.error_code = "tool_arguments_invalid"
        record.status = "rejected"
        record.retryable = True
        record.lifecycle_status = "blocked"
        record.result_success = None
        logger.warning(
            "[react] unusable tool arguments tool=%s code=%s raw_length=%d raw_sha256=%s",
            tc.name,
            record.error_code,
            len(raw),
            digest,
        )
        self._note_unexecuted_call(record.error_code)
        return record

    async def _dispatch_tool(
        self,
        tc: ToolCall,
        tools_by_name: dict[str, ToolSpec],
    ) -> ToolInvocationRecord:
        record = ToolInvocationRecord(name=tc.name, arguments=tc.arguments, call_id=tc.id)
        circuit_key = _circuit_key(tc)
        tool_spec = tools_by_name[tc.name]
        if self._circuit.get(circuit_key, 0) >= _CIRCUIT_BREAKER_MAX:
            record.error = f"tool '{tc.name}' is temporarily unavailable after repeated failures"
            record.status = "rejected"
            record.error_code = "tool_circuit_open"
            record.lifecycle_status = "blocked"
            record.result_success = None
            return record

        started_at = time.monotonic()
        last_error: str | None = None
        transport_result: Any = None
        started = False
        retry_limit = _TOOL_RETRY_MAX if tool_spec.replay_safe else 0
        for attempt in range(1 + retry_limit):
            record.attempts = attempt + 1
            try:
                started = True
                raw_result = await self._invoke(tc.name, tc.arguments)
                transport_result = raw_result
                outcome = tool_call_outcome(raw_result)
                record.result = tool_event_output(raw_result)
                record.lifecycle_status = outcome.lifecycle
                record.result_success = outcome.result_success
                if isinstance(raw_result, ToolResultEnvelope):
                    record.observation = tool_observation(raw_result)
                if self._observation_max_chars > 0 and record.error is None:
                    raw_obs = (
                        record.observation
                        if record.observation is not None
                        else record.result
                    )
                    record.observation = self._compact_observation(raw_obs)
                last_error = None
                break
            except asyncio.CancelledError:
                from omni.core.tool_result import interrupted_tool_payload

                payload = interrupted_tool_payload(tc.name, started=started)
                record.result = payload
                record.error = str(payload.get("error") or "")
                record.status = "cancelled"
                record.error_code = str(payload.get("error_code") or "TOOL_OUTCOME_UNKNOWN")
                record.lifecycle_status = "aborted"
                record.result_success = None
                record.duration_ms = (time.monotonic() - started_at) * 1000
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if not _is_retryable(exc) or attempt >= retry_limit:
                    break
                await asyncio.sleep(_TOOL_RETRY_BASE_DELAY * (2**attempt))
        contract_violation = (
            record.result
            if isinstance(record.result, dict)
            and record.result.get("contract_violation") is True
            else None
        )
        rejected_result = is_tool_rejection(transport_result)
        result_failure = tool_result_failure(transport_result)
        if contract_violation is not None:
            record.error = str(
                contract_violation.get("error") or "tool contract validation failed"
            )
            execution_started = contract_violation.get("execution_started") is True
            record.status = "failed" if execution_started else "rejected"
            record.error_code = str(
                contract_violation.get("reason") or "tool_contract_violation"
            )
            record.lifecycle_status = "failed" if execution_started else "blocked"
            record.result_success = None
        elif rejected_result:
            record.error = tool_rejection_error(record.result)
            record.status = "rejected"
            record.error_code = (
                "tool_policy_rejected"
                if record.result.get("policy_violation") is True
                else "tool_approval_required"
            )
            self._note_unexecuted_call(record.error_code)
            record.lifecycle_status = "blocked"
            record.result_success = None
        elif result_failure is not None:
            record.status, record.error = result_failure
            record.error_code = "tool_result_failed"
        elif last_error is not None:
            record.error = last_error
            record.status = "failed"
            record.error_code = "tool_execution_failed"
            self._circuit[circuit_key] = self._circuit.get(circuit_key, 0) + 1
            record.lifecycle_status = "failed"
            record.result_success = None
        else:
            record.status = "succeeded"
            self._circuit.pop(circuit_key, None)
            # Real progress clears the streak: a call that landed proves the
            # model can address this tool surface correctly.
            self._unexecuted_calls.clear()
        record.duration_ms = (time.monotonic() - started_at) * 1000
        return record

    def _note_unexecuted_call(self, code: str) -> None:
        if code in _UNEXECUTED_CALL_CODES:
            self._unexecuted_calls[code] = self._unexecuted_calls.get(code, 0) + 1

    def _unexecuted_call_pressure(self) -> int:
        return max(self._unexecuted_calls.values(), default=0)

    @staticmethod
    def _find_escalation(calls: list[ToolCall], allow: bool) -> ToolCall | None:
        if not allow:
            return None
        return next((c for c in calls if c.name == ESCALATE_RUN_TOOL_NAME), None)

    def _finalize(
        self,
        kind,
        trace,
        usage,
        iterations,
        tool_calls,
        *,
        terminated_reason,
        content="",
        escalated_goal=None,
        escalated_reason=None,
        budget: ToolExecutionBudget | None = None,
        transcript_repairs: list[str] | None = None,
    ) -> AgentLoopResult:
        return AgentLoopResult(
            kind=kind, content=content, tool_trace=trace, escalated_goal=escalated_goal,
            escalated_reason=escalated_reason, total_iterations=iterations,
            total_tool_calls=tool_calls,
            tool_budget=budget.snapshot() if budget is not None else {},
            usage_budget=self._usage_budget_snapshot(usage),
            total_usage=dict(usage),
            terminated_reason=terminated_reason,
            transcript_repairs=list(transcript_repairs or []),
        )

    def _cancelled_result(
        self,
        trace: list[ToolInvocationRecord],
        usage: dict[str, int],
        iteration: int,
        budget: ToolExecutionBudget,
        *,
        user_message: str,
        transcript_repairs: list[str],
    ) -> AgentLoopResult:
        return self._finalize(
            "partial",
            trace,
            usage,
            iteration,
            budget.completed,
            terminated_reason="cancelled",
            content=_salvage_content(
                "The user cancelled execution; completed results were preserved.",
                trace,
                user_message,
            ),
            budget=budget,
            transcript_repairs=transcript_repairs,
        )


def _nested_result_usage(result: Any) -> dict[str, int] | None:
    """Pull host-attached engine usage out of a skill tool payload."""
    if not isinstance(result, dict):
        return None
    nested = result.get("result")
    candidates = [result.get("usage")]
    if isinstance(nested, dict):
        candidates.append(nested.get("usage"))
    for candidate in candidates:
        if isinstance(candidate, dict) and any(
            isinstance(candidate.get(key), int) and candidate.get(key)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        ):
            return candidate
    return None


def _accumulate_usage(total: dict[str, int], delta: dict[str, int] | None) -> None:
    if not delta:
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance(delta.get(key), int):
            total[key] = total.get(key, 0) + delta[key]


def _circuit_key(tc: ToolCall) -> str:
    """Circuit-breaker identity for one call.

    Plain tools are keyed by name. A meta-tool (``run_skill`` and friends) is
    keyed by name *and* the subject it dispatches to, so one failing skill
    cannot open the breaker on the router that every other skill goes through.
    """
    subject_args = _META_TOOL_SUBJECT_ARGS.get(tc.name)
    if not subject_args:
        return tc.name
    for arg in subject_args:
        subject = str((tc.arguments or {}).get(arg) or "").strip()
        if subject:
            return f"{tc.name}:{subject}"
    return tc.name


def _stall_outcome(record: ToolInvocationRecord, observation: str) -> str:
    """The part of an outcome that decides whether a call pattern is repeating.

    A failure message is prose written for the model, and prose carries incident
    detail — a byte count, a digest, a timestamp — that differs on every attempt
    even when the failure is identical. Keying the stall detector on the whole
    message therefore files each repeat under its own key, and a loop that is
    plainly going nowhere reads as a first occurrence forever. The failure's
    ``error_code`` is the stable identity of "this went wrong the same way".
    """
    if record.error_code:
        return f"code:{record.error_code}"
    return observation


def _hunt_window(trace: list[ToolInvocationRecord]) -> list[ToolInvocationRecord]:
    window: list[ToolInvocationRecord] = []
    for record in reversed(trace):
        if record.name in _CONTRACT_CONSUME_TOOLS:
            break
        if record.name not in _CONTRACT_HUNT_TOOLS:
            break
        window.append(record)
    return window


def _active_hunt_contract(
    window: list[ToolInvocationRecord],
) -> tuple[frozenset[str], int] | None:
    for index, record in enumerate(window):
        if record.name != "find_skill":
            continue
        names = _find_skill_contract_names(record.result)
        if names:
            return names, index
    return None


def _active_hunt_names(trace: list[ToolInvocationRecord]) -> frozenset[str]:
    if any(
        record.name in _CONTRACT_CONSUME_TOOLS and record.status == "succeeded"
        for record in trace
    ):
        return frozenset()
    found = _active_hunt_contract(_hunt_window(trace))
    return found[0] if found else frozenset()


def _contract_hunt_pressure(trace: list[ToolInvocationRecord]) -> int:
    """Trailing probes of one unanswered skill contract.

    Distinct queries look like progress to the signature-keyed stall detector.
    Once ``find_skill`` has handed back an ``input_schema``, further
    ``docs_search`` / ``glob`` / a repeat lookup that returns the same skill
    are the same loop: the next action is ``run_skill``. A disjoint second
    card is preparing another consume, so it resets the streak. An empty
    ``find_skill`` after a card is a miss, not another probe of that card.
    Docs-only retrieval (no contract in the trailing window) is how a product
    question reads ``architecture.md``; that is not a hunt.
    """
    if any(
        record.name in _CONTRACT_CONSUME_TOOLS and record.status == "succeeded"
        for record in trace
    ):
        return 0
    window = _hunt_window(trace)
    found = _active_hunt_contract(window)
    if found is None:
        return 0
    active, contract_idx = found
    trailing = 0
    for index, record in enumerate(window):
        if record.name == "find_skill":
            names = _find_skill_contract_names(record.result)
            if names and names & active:
                trailing += 1
                continue
            if names:
                break
            continue
        if index < contract_idx:
            trailing += 1
    return trailing


def _find_skill_contract_names(result: Any) -> frozenset[str]:
    """Skill names on a ``find_skill`` card that already include an input schema."""
    if not isinstance(result, dict):
        return frozenset()
    matches = result.get("matches")
    if not isinstance(matches, list):
        return frozenset()
    names: list[str] = []
    for item in matches:
        if not isinstance(item, dict) or not isinstance(item.get("input_schema"), dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return frozenset(names)


def _is_unproductive(record: ToolInvocationRecord) -> bool:
    """Whether a tool observation carried no forward progress.

    Transport errors, controlled command blocks/timeouts, empty results, explicit
    ``status: empty/error``, and the string ``(no matches ...)`` /
    ``(empty directory)`` markers count as no progress. A non-zero process exit
    remains useful evidence; a productive observation resets the streak.
    """
    if record.error:
        return True
    if record.result_success is False:
        return True
    result = record.result
    if isinstance(result, dict):
        command_status = command_result_status(result)
        if command_status in {"blocked", "invalid", "timed_out"}:
            return True
        if command_status in {"succeeded", "failed"}:
            return False
        status = result.get("status")
        if status == "empty":
            return True
        if is_empty_literature_funnel(result):
            return True
        if result.get("error"):
            return True
        if record.observation is not None:
            return False
    obs = (record.to_observation() or "").strip()
    if not obs:
        return True
    low = obs.lower()
    return low.startswith("error") or "(no matches" in low or obs in {"(none)", "(empty directory)"}


def _has_productive_observation(trace: list[ToolInvocationRecord]) -> bool:
    """Whether a required lookup produced evidence safe to answer from."""
    return any(
        record.status == "succeeded" and not _is_unproductive(record)
        for record in trace
    )


_MAX_CONTRACT_FIELD_ERRORS = 8


def _contract_violation_fields(result: Any) -> list[dict[str, str]]:
    """Compact, model-actionable projection of a contract violation's per-field errors.

    The gateway records exactly which argument failed and why (``{path, keyword,
    message}`` per field — e.g. path ``when.trigger_kind``, keyword ``enum``), but the
    model-facing observation historically collapsed all of it to a bare "input failed
    contract validation" string. That gave the model nothing to act on, so a trivially
    fixable schema slip (writing ``when.trigger_kind="at"`` instead of an allowed enum
    value) could not be self-corrected on retry. Threading the field list into the
    observation lets the self-correcting ReAct loop repair the one bad argument — the
    tool's own schema (already in the model's tool catalog) supplies the allowed values
    once it knows which field to look at. Capped so a pathological result cannot bloat
    the transcript.
    """
    if not isinstance(result, dict) or result.get("contract_violation") is not True:
        return []
    errors = result.get("errors")
    if not isinstance(errors, (list, tuple)):
        return []
    fields: list[dict[str, str]] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        issue = str(error.get("message") or error.get("keyword") or "invalid value")
        fields.append({"path": str(error.get("path") or "$"), "issue": issue})
        if len(fields) >= _MAX_CONTRACT_FIELD_ERRORS:
            break
    return fields


def _tool_result_needs_input(result: Any) -> bool:
    """True for a tool result whose decisive ``outcome`` is a user clarification.

    Keyed on ``outcome`` (not ``status``) on purpose: ``outcome`` is the field a
    capability sets to report its *final* decision, so ``outcome == "needs_input"``
    is a terminal "await the user" suspend — currently the scheduling clarification
    contract (``temporal_clarification_payload``). The ReAct loop treats it as a
    terminal turn outcome so the clarification pauses the loop for the user the same
    way an ``action_required`` prompt does — matching Codex/Claude Code, where
    awaiting input is a suspend state distinct from success or failure. A soft
    ``status: "needs_input"`` hint with no ``outcome`` (e.g. a workflow preflight
    that wants the model to phrase the question) is intentionally NOT terminal here.
    """
    if not isinstance(result, dict):
        return False
    return str(result.get("outcome") or "").strip().lower() == "needs_input"


def _is_terminal_tool_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    action = result.get("action_required")
    if isinstance(action, dict):
        # Owner-lifecycle admission (VLM / bins / modules) is a route
        # observation. Conversational confirms still suspend the turn.
        return not is_admission_action(action)
    if _tool_result_needs_input(result):
        return True
    control = result.get("_omni_control")
    return isinstance(control, dict) and control.get("terminal") is True


def _terminal_tool_content(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)
    for key in ("message", "summary", "error", "text"):
        if result.get(key):
            return str(result[key])
    return json.dumps(result, ensure_ascii=False, default=str)[:2000]


def _terminal_tool_kind(result: Any) -> Literal["text", "error", "needs_input"]:
    if _tool_result_needs_input(result):
        return "needs_input"
    if isinstance(result, dict) and isinstance(result.get("action_required"), dict):
        return "error" if is_admission_action(result["action_required"]) else "needs_input"
    return "text"


def _terminal_tool_reason(result: Any) -> str:
    if not isinstance(result, dict):
        return "terminal_tool_result"
    if _tool_result_needs_input(result):
        return "needs_input"
    error_info = result.get("error_info")
    if isinstance(error_info, dict) and error_info.get("code"):
        return str(error_info["code"])
    return "terminal_tool_result"


def _salvage_content(reason: str, trace: list[ToolInvocationRecord], user_message: str) -> str:
    lines = [
        f"Partial result: {reason}",
        "",
        "Completed tool steps (full inputs and outputs are stored in run events):",
    ]
    if trace:
        for record in trace[-6:]:
            status = "failed" if record.error else "succeeded"
            lines.append(f"- {record.name}（{status}）")
    else:
        lines.append("- No tool call completed.")
    lines += [
        "",
        "Not completed: further tool calls stopped; this recoverable result uses the information already available.",
        f"User request: {user_message[:220]}",
        "Next: add concrete constraints and retry, or use /task to inspect submitted background work.",
    ]
    return "\n".join(lines)


def _clear_task_cancellation() -> None:
    """Drop pending Task.cancel() so a cancelled LLM call can still persist."""
    task = asyncio.current_task()
    if task is None:
        return
    while task.cancelling() > 0:
        task.uncancel()


def _brief(value: Any, limit: int = 180) -> str:
    if isinstance(value, dict):
        for key in ("summary", "message", "title", "text", "note", "status", "error"):
            if value.get(key):
                return str(value[key])[:limit]
    if value not in (None, ""):
        return str(value)[:limit]
    return ""


__all__ = [
    "AgentLoopResult", "ReActLoopAgent", "ToolInvocationRecord", "ToolInvoker", "ToolSpec",
    "build_escalate_run_tool_spec", "ESCALATE_RUN_TOOL_NAME",
]
