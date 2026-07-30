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
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from omni.core.execution_budget import ToolExecutionBudget
from omni.core.execution_control import ExecutionCancelled, ExecutionControl
from omni.core.llm.client import ChatWithToolsResult, LLMClient, ToolCall
from omni.core.llm.errors import classify_llm_exception
from omni.core.tool_result import (
    ToolResultEnvelope,
    command_result_status,
    is_tool_rejection,
    tool_event_output,
    tool_observation,
    tool_rejection_error,
    tool_result_failure,
)
from omni.core.tool_transcript import normalize_tool_transcript
from omni.core.turn_clock import TurnClock, register_clock

logger = logging.getLogger(__name__)

ESCALATE_RUN_TOOL_NAME = "escalate_run"

_TOOL_RETRY_MAX = 1
_TOOL_RETRY_BASE_DELAY = 0.5
_CIRCUIT_BREAKER_MAX = 5
_RETRYABLE = frozenset(
    {
        "TimeoutError", "ConnectError", "ConnectionError", "ReadError", "WriteError",
        "RemoteProtocolError", "HTTPStatusError", "RateLimitError", "ServiceUnavailable",
    }
)

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

    def to_observation(self) -> str:
        if self.error:
            return json.dumps(
                {
                    "status": self.status,
                    "error": self.error,
                    "reason": self.error_code or "tool_error",
                    "retryable": self.retryable,
                },
                ensure_ascii=False,
            )
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
    tool_budget: dict[str, int | bool] = field(default_factory=dict)
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
    """Bounded iterative tool-use agent. Stateless across :meth:`run` calls."""

    def __init__(
        self,
        llm_client: LLMClient,
        tool_invoker: ToolInvoker,
        *,
        max_iterations: int = 6,
        max_tool_calls: int = 12,
        max_seconds: float = 120.0,
        finalization_timeout_s: float = 30.0,
        shared_tool_budget: ToolExecutionBudget | None = None,
        max_total_tokens: int = 0,
        max_cost_usd: float = 0.0,
        input_cost_per_mtok: float = 0.0,
        output_cost_per_mtok: float = 0.0,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        soft_token_limit: int = 0,
        microcompact_keep_tool_results: int = 0,
        no_progress_synthesis: bool = True,
        no_progress_threshold: int = 2,
        parallel_tools: bool = True,
    ) -> None:
        self._llm = llm_client
        self._invoke = tool_invoker
        self._max_iterations = max(0, max_iterations)
        self._max_tool_calls = max(0, max_tool_calls)
        self._max_seconds = max(1.0, max_seconds)
        self._finalization_timeout_s = max(1.0, finalization_timeout_s)
        self._shared_tool_budget = shared_tool_budget
        self._max_total_tokens = max(0, int(max_total_tokens))
        self._max_cost_usd = max(0.0, float(max_cost_usd))
        self._input_cost_per_mtok = max(0.0, float(input_cost_per_mtok))
        self._output_cost_per_mtok = max(0.0, float(output_cost_per_mtok))
        self._temperature = temperature
        self._max_tokens = max_tokens
        # When the model emits several tool calls in one turn, dispatch them
        # concurrently (Claude/Codex-style) instead of strictly serially — the
        # latency win on multi-tool turns. A single-call batch is unaffected
        # (awaited directly) so existing sequential semantics/tests are preserved.
        self._parallel_tools = parallel_tools
        # Microcompact (Claude-style, P2): once the running context passes
        # ``soft_token_limit``, shrink older tool observations before the next
        # model call, keeping the most recent N intact. 0 on either disables it.
        self._soft_token_limit = max(0, soft_token_limit)
        self._microcompact_keep = max(0, microcompact_keep_tool_results)
        # Progress control: when tool calls stop making progress (repeated
        # errors / empty results / identical calls) or the budget is spent,
        # force one tool-free synthesis turn so the user gets a real answer
        # instead of a stub. Circuit breaker is per-instance (not process-wide).
        self._no_progress_synthesis = no_progress_synthesis
        self._no_progress_threshold = max(1, no_progress_threshold)
        self._circuit: dict[str, int] = {}

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
        tool_specs = [t.to_openai_spec() for t in effective_tools]
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
        # Pausable wall-clock: approval waits are excluded via ``clock.pause``
        # in the approval gate, so a long human decision no longer times out a
        # turn whose command actually succeeded.
        budget = ToolExecutionBudget(self._max_tool_calls, parent=self._shared_tool_budget)
        transcript_repairs: list[str] = []
        last: ChatWithToolsResult | None = None
        seen_observations: dict[str, str] = {}
        stalled_patterns: dict[tuple[str, str], int] = {}
        no_progress = 0

        for iteration in range(1, self._max_iterations + 1):
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
            if clock.expired():
                return self._finalize("error", trace, total_usage, iteration - 1, budget.completed,
                                      terminated_reason="timeout",
                                      content="This turn reached its time limit; existing results were retained.",
                                      budget=budget, transcript_repairs=transcript_repairs)
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
            try:
                # Stream the answer to the caller when a token sink is wired
                # (progressive render); the loop is otherwise identical. The
                # streaming call degrades to non-streaming inside the provider.
                if on_token is not None:
                    call = self._llm.chat_with_tools_stream(
                        messages, tool_specs, tool_choice="auto",
                        temperature=self._temperature, max_tokens=self._max_tokens,
                        on_delta=on_token,
                    )
                else:
                    call = self._llm.chat_with_tools(
                        messages, tool_specs, tool_choice="auto",
                        temperature=self._temperature, max_tokens=self._max_tokens,
                    )
                last = await asyncio.wait_for(call, timeout=max(1.0, clock.remaining()))
            except asyncio.CancelledError:
                return self._cancelled_result(
                    trace,
                    total_usage,
                    iteration - 1,
                    budget,
                    user_message=user_message,
                    transcript_repairs=transcript_repairs,
                )
            except TimeoutError:
                return self._finalize(
                    "error", trace, total_usage, iteration - 1, budget.completed,
                    terminated_reason="timeout", content="The model call timed out; existing results were retained.",
                    budget=budget, transcript_repairs=transcript_repairs,
                )
            except Exception as exc:  # noqa: BLE001
                info = classify_llm_exception(exc)
                logger.info(
                    "[react] LLM call failed iter=%d category=%s status=%s request_id=%s",
                    iteration, info.category, info.status_code, info.request_id or "-",
                )
                logger.debug("[react] provider failure detail: %s", info.internal_detail, exc_info=True)
                return self._finalize(
                    "error", trace, total_usage, iteration - 1, budget.completed,
                    terminated_reason=info.terminated_reason, content=info.user_message,
                    budget=budget, transcript_repairs=transcript_repairs,
                )

            _accumulate_usage(total_usage, last.usage)
            usage_limit_reason = self._usage_limit_reason(total_usage)

            if not last.has_tool_calls:
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

            # Admission and transcript closure are separate concerns. Calls that
            # exceed the hard execution budget are not dispatched, but still get
            # a structured result before another provider request is possible.
            batch = last.tool_calls
            if usage_limit_reason:
                budget.reject(len(batch))
                admitted_count = 0
            else:
                admitted_count = budget.admit(len(batch))
            in_budget = batch[:admitted_count]
            rejected = batch[admitted_count:]

            signatures: list[str] = []
            for tc in in_budget:
                signature = (
                    f"{tc.name}:"
                    f"{json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False, default=str)}"
                )
                signatures.append(signature)

            for tc in in_budget:
                await _emit_event(on_tool_event, "start", {"name": tc.name, "arguments": tc.arguments})

            try:
                records = await self._dispatch_batch(in_budget, tools_by_name)
            except asyncio.CancelledError:
                records = [
                    ToolInvocationRecord(
                        name=tc.name,
                        arguments=tc.arguments,
                        call_id=tc.id,
                        error="Tool execution was cancelled by the user.",
                        status="cancelled",
                        error_code="user_cancelled",
                    )
                    for tc in in_budget
                ]
                budget.mark_completed(len(records))
                trace.extend(records)
                for tc, record in zip(in_budget, records, strict=True):
                    await _emit_event(
                        on_tool_event,
                        "done",
                        {
                            "name": tc.name,
                            "arguments": tc.arguments,
                            "result": None,
                            "error": record.error,
                            "status": record.status,
                            "call_id": tc.id,
                            "duration_ms": 0.0,
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": record.to_observation(),
                        }
                    )
                return self._cancelled_result(
                    trace,
                    total_usage,
                    iteration,
                    budget,
                    user_message=user_message,
                    transcript_repairs=transcript_repairs,
                )

            terminal_record: ToolInvocationRecord | None = None
            for tc, record, signature in zip(in_budget, records, signatures, strict=True):
                record.call_id = tc.id
                if record.status not in {"rejected", "cancelled", "timed_out"}:
                    record.status = "failed" if record.error else "succeeded"
                budget.mark_completed()
                trace.append(record)
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
                    },
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "name": tc.name,
                     "content": record.to_observation()}
                )
                if terminal_record is None and _is_terminal_tool_result(record.result):
                    terminal_record = record
                observation = record.to_observation()
                repeated = seen_observations.get(signature) == observation
                seen_observations[signature] = observation
                pattern = (signature, observation)
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

            for tc in rejected:
                rejected_reason = usage_limit_reason or "max_tool_calls"
                error_code = {
                    "max_total_tokens": "run_token_budget_exhausted",
                    "max_cost": "run_cost_budget_exhausted",
                }.get(rejected_reason, "run_hard_budget_exhausted")
                record = ToolInvocationRecord(
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
                )
                trace.append(record)
                await _emit_event(
                    on_tool_event,
                    "done",
                    {
                        "name": tc.name,
                        "arguments": tc.arguments,
                        "result": None,
                        "error": record.error,
                        "status": record.status,
                        "call_id": tc.id,
                        "duration_ms": 0.0,
                    },
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": record.to_observation(),
                    }
                )

            if rejected:
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
                return self._finalize(
                    terminal_kind, trace, total_usage, iteration, budget.completed,
                    terminated_reason=_terminal_tool_reason(terminal_record.result),
                    content=_terminal_tool_content(terminal_record.result),
                    budget=budget,
                    transcript_repairs=transcript_repairs,
                )

            if rejected:
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

            if self._no_progress_synthesis and no_progress >= self._no_progress_threshold:
                return await self._terminate_or_synthesize(
                    messages, trace, total_usage, iteration, budget.completed,
                    user_message=user_message,
                    reason="no_progress",
                    salvage="Repeated tool calls made no progress, so further calls stopped.",
                    budget=budget,
                    transcript_repairs=transcript_repairs,
                )

        return await self._terminate_or_synthesize(
            messages, trace, total_usage, self._max_iterations, budget.completed,
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
        if self._no_progress_synthesis and reason not in {"max_total_tokens", "max_cost"}:
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
        directive = (
            "[System] Tools cannot make further progress "
            f"(reason: {reason}). Answer the user directly and completely using the conversation, existing "
            "tool observations, and prior knowledge. Clearly identify any unverified portions. Do not request "
            "more tools or invent citations or data. If an Omni product question could not be grounded in the "
            "built-in documentation, say so explicitly."
        )
        normalized = normalize_tool_transcript(messages)
        transcript_repairs.extend(
            repair for repair in normalized.repairs if repair not in transcript_repairs
        )
        synth_messages = [*normalized.messages, {"role": "user", "content": directive}]
        try:
            result = await asyncio.wait_for(
                self._llm.chat_with_tools(
                    synth_messages, [], tool_choice="none",
                    temperature=self._temperature, max_tokens=self._max_tokens,
                ),
                # Finalization owns a reserve independent from the exploration
                # deadline, so a useful answer is still attempted at the bound.
                timeout=self._finalization_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — synthesis is best-effort; fall back to stub
            info = classify_llm_exception(exc)
            logger.info(
                "[react] final synthesis failed reason=%s category=%s status=%s request_id=%s",
                reason, info.category, info.status_code, info.request_id or "-",
            )
            logger.debug("[react] final synthesis detail: %s", info.internal_detail, exc_info=True)
            return None
        content = (result.content or "").strip()
        if not content:
            return None
        _accumulate_usage(usage, result.usage)
        return self._finalize(
            "text", trace, usage, iteration, tool_calls,
            terminated_reason=f"synthesized_{reason}", content=content,
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
        return {
            "max_total_tokens": self._max_total_tokens,
            "max_cost_usd": self._max_cost_usd,
            "total_tokens": int(usage.get("total_tokens") or 0),
            "cost_usd": round(self._usage_cost_usd(usage), 6),
            "enforced": bool(self._max_total_tokens or self._max_cost_usd),
        }

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

    async def _dispatch_tool(
        self,
        tc: ToolCall,
        tools_by_name: dict[str, ToolSpec],
    ) -> ToolInvocationRecord:
        record = ToolInvocationRecord(name=tc.name, arguments=tc.arguments, call_id=tc.id)
        tool_spec = tools_by_name.get(tc.name)
        if tool_spec is None:
            record.error = (
                f"unknown tool '{tc.name}'; available: "
                f"{', '.join(sorted(tools_by_name))}"
            )
            record.status = "rejected"
            record.error_code = "unknown_tool"
            return record
        if self._circuit.get(tc.name, 0) >= _CIRCUIT_BREAKER_MAX:
            record.error = f"tool '{tc.name}' is temporarily unavailable after repeated failures"
            record.status = "rejected"
            record.error_code = "tool_circuit_open"
            return record
        if tc.arguments_error:
            # The provider could not parse the model's raw arguments as JSON.
            # Surface it as a retryable failed observation (instead of invoking
            # with empty ``{}``) so the model re-issues the call with valid JSON.
            record.error = (
                f"could not parse tool arguments as JSON ({tc.arguments_error}); "
                "re-issue this call with a valid JSON object for 'arguments'"
            )
            record.status = "rejected"
            record.error_code = "tool_arguments_invalid"
            record.retryable = True
            return record

        started = time.monotonic()
        last_error: str | None = None
        retry_limit = _TOOL_RETRY_MAX if tool_spec.replay_safe else 0
        for attempt in range(1 + retry_limit):
            record.attempts = attempt + 1
            try:
                raw_result = await self._invoke(tc.name, tc.arguments)
                record.result = tool_event_output(raw_result)
                if isinstance(raw_result, ToolResultEnvelope):
                    record.observation = tool_observation(raw_result)
                last_error = None
                break
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
        rejected_result = is_tool_rejection(record.result)
        result_failure = tool_result_failure(record.result)
        if contract_violation is not None:
            record.error = str(
                contract_violation.get("error") or "tool contract validation failed"
            )
            started = contract_violation.get("execution_started") is True
            record.status = "failed" if started else "rejected"
            record.error_code = str(
                contract_violation.get("reason") or "tool_contract_violation"
            )
        elif rejected_result:
            record.error = tool_rejection_error(record.result)
            record.status = "rejected"
            record.error_code = (
                "tool_policy_rejected"
                if record.result.get("policy_violation") is True
                else "tool_approval_required"
            )
        elif result_failure is not None:
            record.status, record.error = result_failure
            record.error_code = "tool_result_failed"
        elif last_error is not None:
            record.error = last_error
            record.status = "failed"
            record.error_code = "tool_execution_failed"
            self._circuit[tc.name] = self._circuit.get(tc.name, 0) + 1
        else:
            record.status = "succeeded"
            self._circuit.pop(tc.name, None)
        record.duration_ms = (time.monotonic() - started) * 1000
        return record

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


def _accumulate_usage(total: dict[str, int], delta: dict[str, int] | None) -> None:
    if not delta:
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance(delta.get(key), int):
            total[key] = total.get(key, 0) + delta[key]


def _is_unproductive(record: ToolInvocationRecord) -> bool:
    """Whether a tool observation carried no forward progress.

    Transport errors, controlled command blocks/timeouts, empty results, explicit
    ``status: empty/error``, and the string ``(no matches ...)`` /
    ``(empty directory)`` markers count as no progress. A non-zero process exit
    remains useful evidence; a productive observation resets the streak.
    """
    if record.error:
        return True
    result = record.result
    if isinstance(result, dict):
        if result.get("error"):
            return True
        command_status = command_result_status(result)
        if command_status in {"blocked", "invalid", "timed_out"}:
            return True
        if command_status in {"succeeded", "failed"}:
            return False
        status = result.get("status")
        if status:
            return status in {"empty", "error", "failed"}
    obs = (record.to_observation() or "").strip()
    if not obs:
        return True
    low = obs.lower()
    return low.startswith("error") or "(no matches" in low or obs in {"(none)", "(empty directory)"}


def _is_terminal_tool_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if isinstance(result.get("action_required"), dict):
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
    if isinstance(result, dict) and isinstance(result.get("action_required"), dict):
        action_kind = str(result["action_required"].get("kind") or "").lower()
        return "needs_input" if action_kind == "configure" else "error"
    return "text"


def _terminal_tool_reason(result: Any) -> str:
    if not isinstance(result, dict):
        return "terminal_tool_result"
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
