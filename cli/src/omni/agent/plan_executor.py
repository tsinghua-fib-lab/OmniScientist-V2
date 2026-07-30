"""Execute high-confidence IntentPlans without falling back to wide ReAct."""

from __future__ import annotations

from typing import Any

from omni.agent.capability_runners import DEFAULT_CAPABILITY_RUNNERS, CapabilityRunnerRegistry
from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.plan_result import PlanExecutionResult
from omni.agent.plan_runner_utils import emit_tool_event, plan_summary
from omni.core.react_agent import ToolInvocationRecord
from omni.memory.service import MemoryLayer
from omni.skills_runtime.context import ExecContext, Tool


class PlanExecutor:
    """Deterministic executor for plan routes that should not enter broad ReAct."""

    def __init__(
        self,
        runtime: Any,
        tasks: Any,
        registry: Any,
        memory: Any = None,
        capability_runners: CapabilityRunnerRegistry | None = None,
    ) -> None:
        self._runtime = runtime
        self._tasks = tasks
        self._registry = registry
        self._memory = memory
        self._capability_runners = capability_runners or DEFAULT_CAPABILITY_RUNNERS

    async def execute(
        self,
        plan: IntentPlan,
        *,
        ctx: ExecContext,
        tools: list[Tool],
        drain_tasks: bool,
        on_tool_event: Any = None,
        active_target: Any | None = None,
    ) -> PlanExecutionResult:
        if (
            plan.intent_type == IntentType.SCHEDULE
            and plan.execution_mode == "direct"
        ):
            return await self._schedule(
                plan,
                ctx=ctx,
                tools=tools,
                on_tool_event=on_tool_event,
            )
        if plan.intent_type in {IntentType.QA_PLUS_ARTIFACT, IntentType.SINGLE_SKILL_TASK}:
            runner = self._capability_runners.for_plan(plan, registry=self._registry)
            if runner is None:
                return PlanExecutionResult(handled=False)
            return await runner.run(
                plan,
                ctx=ctx,
                tools=tools,
                runtime=self._runtime,
                tasks=self._tasks,
                registry=self._registry,
                drain_tasks=drain_tasks,
                on_tool_event=on_tool_event,
                active_target=active_target,
            )
        if plan.intent_type == IntentType.MEMORY_UPDATE:
            return await self._memory_update(plan, ctx=ctx)
        return PlanExecutionResult(handled=False)

    async def _schedule(
        self,
        plan: IntentPlan,
        *,
        ctx: ExecContext,
        tools: list[Tool],
        on_tool_event: Any = None,
    ) -> PlanExecutionResult:
        proposal = plan.task_contract.get("schedule_proposal")
        if not isinstance(proposal, dict) or not proposal:
            return PlanExecutionResult(handled=False)
        tool = next((item for item in tools if item.spec.name == "schedule_task"), None)
        if tool is None:
            return PlanExecutionResult(
                handled=True,
                text="Scheduling is unavailable in this execution context.",
                kind="error",
                terminated_reason="schedule_tool_unavailable",
                error="schedule_task tool unavailable",
                plan_summary=plan_summary(plan),
                settlement_status="failed",
            )

        gateway = ctx.tool_gateway
        if gateway is None:
            return PlanExecutionResult(
                handled=True,
                text="Scheduling is unavailable outside the execution gateway.",
                kind="error",
                terminated_reason="schedule_gateway_unavailable",
                error="schedule execution gateway unavailable",
                plan_summary=plan_summary(plan),
                settlement_status="failed",
            )
        arguments = dict(proposal)
        try:
            payload = await gateway.invoke_operation(
                "schedule_task",
                arguments,
                invoke=lambda: tool.handler(dict(arguments)),
                sensitive=tool.sensitive,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
            )
        except Exception as exc:  # noqa: BLE001 - convert gateway failures to a durable outcome
            payload = {
                "status": "error",
                "outcome": "error",
                "summary": "Scheduling failed before completion.",
                "error": f"{type(exc).__name__}: {exc}",
            }
        result = dict(payload) if isinstance(payload, dict) else {
            "status": "error",
            "error": "schedule_task returned an invalid result",
        }
        status = str(result.get("status") or "error")
        outcome = str(result.get("outcome") or status)
        text = str(
            result.get("summary")
            or result.get("message")
            or result.get("error")
            or "Schedule request resolved."
        )
        if status == "needs_input":
            kind, verification = "needs_input", "needs_input"
            trace_status = "rejected"
        elif status in {"ok", "awaiting_approval"}:
            kind, verification = "text", "passed"
            trace_status = "succeeded"
        else:
            kind, verification = "error", "failed"
            trace_status = "rejected"
        terminated_reason = f"schedule_{outcome}"

        trace = ToolInvocationRecord(
            name="schedule_task",
            arguments=dict(proposal),
            result=result,
            status=trace_status,
            error=str(result.get("error") or "") or None,
        )
        await self._tasks.append_event(
            ctx.task_id,
            event_type="plan.executed",
            status=outcome,
            name=plan.intent_type.value,
            tool_name="schedule_task",
            output_json={
                "intent_type": plan.intent_type.value,
                "outcome": outcome,
                "schedule_id": str(result.get("schedule_id") or ""),
                "proposal_id": str(result.get("proposal_id") or ""),
            },
            summary=text[:400],
        )
        await self._tasks.append_event(
            ctx.task_id,
            event_type="execution.finished",
            status=(
                "needs_input"
                if kind == "needs_input"
                else "succeeded"
                if kind != "error"
                else "failed"
            ),
            name="schedule",
            output_json={
                "kind": kind,
                "terminated_reason": terminated_reason,
                "outcome": outcome,
            },
            summary=f"schedule execution {outcome}",
        )
        await emit_tool_event(
            on_tool_event,
            "plan",
            {
                "event_type": "plan.executed",
                "name": plan.intent_type.value,
                "summary": text[:400],
                "task_id": ctx.task_id,
                "outcome": outcome,
            },
        )
        return PlanExecutionResult(
            handled=True,
            text=text,
            kind=kind,
            tool_trace=[trace],
            terminated_reason=terminated_reason,
            error=str(result.get("error") or "") if kind == "error" else "",
            plan_summary=plan_summary(plan),
            settlement_status=verification,
        )

    async def _memory_update(self, plan: IntentPlan, *, ctx: ExecContext) -> PlanExecutionResult:
        memory_input = plan.capability_inputs.get("memory.update") or {}
        summary = str(memory_input.get("content") or "").strip()
        if not summary:
            return PlanExecutionResult(
                handled=True,
                text="I need the exact fact or preference that should be remembered.",
                kind="needs_input",
                terminated_reason="missing_memory_content",
                plan_summary=plan_summary(plan),
                settlement_status="needs_input",
            )
        memory_id = ""
        if self._memory is not None and summary:
            memory_id = await self._memory.record(
                layer=MemoryLayer.SEMANTIC,
                scope="project",
                scope_id="",
                summary=summary,
                memory_type="user_note",
                tags=["user-confirmed"],
                importance=0.75,
                pinned=True,
            )
        text = f"Remembered: {summary}" + (f"\nMemory id: `{memory_id[:8]}`" if memory_id else "")
        await self._tasks.append_event(
            ctx.task_id,
            event_type="plan.executed",
            status="succeeded",
            name=plan.intent_type.value,
            output_json={"intent_type": plan.intent_type.value, "memory_id": memory_id, "summary": summary},
            summary=f"recorded memory {memory_id[:8] if memory_id else ''}".strip(),
        )
        return PlanExecutionResult(
            handled=True,
            text=text,
            kind="text",
            terminated_reason=plan.intent_type.value,
            plan_summary=plan_summary(plan),
            degraded_warnings=list(plan.degraded_warnings),
            settlement_status="succeeded",
        )
