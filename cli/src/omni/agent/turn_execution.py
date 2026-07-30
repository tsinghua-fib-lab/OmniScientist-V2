"""Controlled execution, cancellation, and result settlement for one agent turn."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from omni.agent.intent_plan import IntentPlan
from omni.agent.plan_result import PlanExecutionResult
from omni.agent.plan_runner_utils import last_tool_step, loop_result_event, plan_summary
from omni.core.execution_control import ExecutionCancelled, ExecutionControl
from omni.core.react_agent import AgentLoopResult


@dataclass
class TurnResult:
    text: str
    session_id: str
    task_id: str = ""
    kind: str = "text"
    tool_trace: list[Any] = field(default_factory=list)
    submitted_workflow_ids: list[str] = field(default_factory=list)
    submitted_subtask_ids: list[str] = field(default_factory=list)
    drained_results: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    terminated_reason: str = "done"
    plan_summary: str = ""
    degraded_warnings: list[str] = field(default_factory=list)
    verification_status: str = ""


class TurnCompletion:
    """Own persistence, task settlement, verification, and presentation hooks."""

    def __init__(
        self,
        *,
        tasks: Any,
        task_controller: Any,
        hooks: Any,
        runtime: Any,
    ) -> None:
        self._tasks = tasks
        self._task_controller = task_controller
        self._hooks = hooks
        self._runtime = runtime

    async def complete_plan(
        self,
        *,
        plan: IntentPlan,
        result: PlanExecutionResult,
        session_id: str,
        user_message: str,
        drain_tasks: bool,
        persist_message: Any,
        record_turn_memory: Any,
        apply_verifier_outcome: Any,
    ) -> TurnResult:
        """Settle a deterministic plan result and project it to the turn API."""
        task_id = plan.task_id
        await self._hooks.emit(
            "pre_present",
            task_id=task_id,
            payload={"kind": result.kind, "text": result.text},
        )
        await persist_message(
            session_id,
            "assistant",
            result.text,
            tools=[record.name for record in result.tool_trace],
            kind=result.kind,
            terminated_reason=result.terminated_reason,
            submitted_workflow_ids=result.submitted_workflow_ids,
            submitted_subtask_ids=result.submitted_subtask_ids,
        )
        await record_turn_memory(
            session_id,
            user_message,
            AgentLoopResult(
                kind="needs_input" if result.kind == "needs_input" else "text",
                content=result.text,
                tool_trace=result.tool_trace,
                terminated_reason=result.terminated_reason,
            ),
            task_id=task_id,
        )
        await self._task_controller.finish_turn(
            task_id,
            kind=result.kind,
            text=result.text,
            submitted_workflow_ids=result.submitted_workflow_ids,
            submitted_subtask_ids=result.submitted_subtask_ids,
            drain_tasks=drain_tasks,
            error=result.error,
            task_status=(
                result.terminated_reason
                if result.terminated_reason in {"cancelled", "interrupted"}
                else ""
            ),
        )
        await apply_verifier_outcome(task_id, result)
        await self._hooks.emit(
            "post_present",
            task_id=task_id,
            payload={"kind": result.kind, "status": result.verification_status},
        )
        return TurnResult(
            text=result.text,
            session_id=session_id,
            task_id=task_id,
            kind=result.kind,
            tool_trace=result.tool_trace,
            submitted_workflow_ids=result.submitted_workflow_ids,
            submitted_subtask_ids=result.submitted_subtask_ids,
            drained_results=result.drained_results,
            terminated_reason=result.terminated_reason,
            plan_summary=result.plan_summary or plan_summary(plan),
            degraded_warnings=list(
                dict.fromkeys([*plan.degraded_warnings, *result.degraded_warnings])
            ),
            verification_status=result.verification_status,
        )

    async def complete_react(
        self,
        *,
        plan: IntentPlan,
        result: AgentLoopResult,
        session_id: str,
        user_message: str,
        channel: str,
        drain_tasks: bool,
        emit_tool_event: Any,
        maybe_escalate: Any,
        persist_message: Any,
        record_turn_memory: Any,
        apply_verifier_outcome: Any,
    ) -> TurnResult:
        """Settle a ReAct result, including any foreground child executions."""
        task_id = plan.task_id
        final_status, final_payload = loop_result_event(result)
        await self._tasks.append_event(
            task_id,
            event_type="react.finished",
            status=final_status,
            name="react",
            output_json=final_payload,
            summary=f"react {result.kind}: {result.terminated_reason}",
        )
        submitted = [
            str(record.result["subtask_id"])
            for record in result.tool_trace
            if record.name in {"run_skill", "run_workflow"}
            and isinstance(record.result, dict)
            and record.result.get("subtask_id")
            and record.result.get("status") == "submitted"
        ]
        submitted_workflows = [
            str(record.result["workflow_run_id"])
            for record in result.tool_trace
            if record.name == "run_workflow"
            and isinstance(record.result, dict)
            and record.result.get("workflow_run_id")
        ]
        if result.kind == "escalated" and result.escalated_goal:
            escalated_id = await maybe_escalate(
                result.escalated_goal,
                session_id,
                channel,
                task_id=task_id,
            )
            if escalated_id:
                submitted.append(escalated_id)

        await self._hooks.emit(
            "pre_present",
            task_id=task_id,
            payload={"kind": result.kind, "text": result.content},
        )
        await persist_message(
            session_id,
            "assistant",
            result.content,
            tools=result.tool_names(),
            kind=result.kind,
            terminated_reason=result.terminated_reason,
            iterations=result.total_iterations,
            tool_call_count=result.total_tool_calls,
            failed_or_last_step=last_tool_step(result),
        )
        await record_turn_memory(
            session_id,
            user_message,
            result,
            task_id=task_id,
        )
        drained = await self._drain_submitted(
            task_id=task_id,
            submitted_workflow_ids=submitted_workflows,
            submitted_subtask_ids=submitted,
            drain_tasks=drain_tasks,
            emit_tool_event=emit_tool_event,
        )
        await self._task_controller.finish_turn(
            task_id,
            kind=result.kind,
            text=result.content,
            submitted_workflow_ids=submitted_workflows,
            submitted_subtask_ids=submitted,
            drain_tasks=drain_tasks,
            error=result.content if result.kind == "error" else "",
            task_status=final_status,
        )
        turn_result = TurnResult(
            text=result.content,
            session_id=session_id,
            kind=result.kind,
            task_id=task_id,
            tool_trace=result.tool_trace,
            submitted_workflow_ids=submitted_workflows,
            submitted_subtask_ids=submitted,
            drained_results=drained,
            usage=result.total_usage,
            terminated_reason=result.terminated_reason,
            plan_summary=plan_summary(plan),
            degraded_warnings=list(plan.degraded_warnings),
            verification_status=(
                "skipped"
                if result.terminated_reason == "cancelled"
                else "needs_input"
                if result.kind == "needs_input"
                else "salvaged"
                if result.kind == "partial"
                else "failed"
                if result.kind == "error"
                else "passed"
            ),
        )
        await apply_verifier_outcome(task_id, turn_result)
        await self._hooks.emit(
            "post_present",
            task_id=task_id,
            payload={
                "kind": turn_result.kind,
                "terminated_reason": turn_result.terminated_reason,
            },
        )
        return turn_result

    async def _drain_submitted(
        self,
        *,
        task_id: str,
        submitted_workflow_ids: list[str],
        submitted_subtask_ids: list[str],
        drain_tasks: bool,
        emit_tool_event: Any,
    ) -> list[dict[str, Any]]:
        if not drain_tasks or not (submitted_workflow_ids or submitted_subtask_ids):
            return []
        await self._runtime.drain(on_event=emit_tool_event)
        drained: list[dict[str, Any]] = []
        for workflow_id in submitted_workflow_ids:
            workflow = await self._runtime.get_workflow_run(workflow_id)
            if workflow:
                drained.append(
                    {
                        "workflow_run_id": workflow_id,
                        "task_id": task_id,
                        "object_kind": "workflow_run",
                        "object_id": workflow_id,
                        "kind": "workflow",
                        "status": workflow.status,
                        "result": workflow.result_json,
                        "error": workflow.error,
                        "trace": workflow.trace_log,
                    }
                )
        for subtask_id in submitted_subtask_ids:
            task = await self._runtime.get_subtask(subtask_id)
            if task:
                drained.append(
                    {
                        "subtask_id": subtask_id,
                        "task_id": task_id,
                        "object_kind": "skill_execution",
                        "object_id": subtask_id,
                        "skill": task.skill_name,
                        "status": task.status,
                        "result": task.result_json,
                        "error": task.error,
                        "trace": task.trace_log,
                    }
                )
        return drained


class TurnExecution:
    """Run a turn under durable controls and settle cancellation once."""

    def __init__(self, tasks: Any, task_controller: Any, persist_message: Any) -> None:
        self._tasks = tasks
        self._task_controller = task_controller
        self._persist_message = persist_message

    async def run(
        self,
        *,
        execute: Callable[..., Awaitable[TurnResult]],
        user_message: str,
        session_id: str | None,
        existing_task_id: str,
        on_task_ack: Any,
        execute_kwargs: dict[str, Any],
    ) -> TurnResult:
        acknowledged = {
            "task_id": existing_task_id,
            "session_id": session_id or "",
        }
        if existing_task_id:
            recover_controls = getattr(
                self._tasks,
                "recover_consumed_controls",
                None,
            )
            if callable(recover_controls):
                await recover_controls(existing_task_id)

        async def capture_ack(data: dict[str, Any]) -> None:
            acknowledged["task_id"] = str(data.get("task_id") or acknowledged["task_id"])
            acknowledged["session_id"] = str(
                data.get("session_id") or acknowledged["session_id"]
            )
            if on_task_ack is not None:
                result = on_task_ack(data)
                if inspect.isawaitable(result):
                    await result

        async def read_controls() -> list[dict[str, str]]:
            task_id = acknowledged["task_id"]
            return await self._tasks.consume_controls(task_id) if task_id else []

        async def acknowledge_controls(control_ids: list[str]) -> None:
            marker = getattr(self._tasks, "mark_controls_applied", None)
            if callable(marker):
                await marker(control_ids)

        control = ExecutionControl(
            read_controls,
            acknowledge_controls=acknowledge_controls,
        )
        try:
            result = await control.run(
                execute(
                    user_message,
                    **execute_kwargs,
                    on_task_ack=capture_ack,
                    execution_control=control,
                )
            )
            # Process-local delivery evidence closes the foreground UI race
            # when durable acknowledgement fails after a steer was already
            # injected at a ReAct boundary.
            result._delivered_control_ids = control.delivered_control_ids  # type: ignore[attr-defined]
            return result
        except (ExecutionCancelled, asyncio.CancelledError):
            result = await self._cancelled_result(
                acknowledged["task_id"],
                acknowledged["session_id"] or session_id or "",
                user_message,
            )
            result._delivered_control_ids = control.delivered_control_ids  # type: ignore[attr-defined]
            return result
        except BaseException as exc:
            exc._delivered_control_ids = control.delivered_control_ids  # type: ignore[attr-defined]
            raise

    async def _cancelled_result(
        self,
        task_id: str,
        session_id: str,
        user_message: str,
    ) -> TurnResult:
        text = "Execution cancelled. Completed results and artifacts were preserved."
        task = await self._tasks.get_task(task_id) if task_id else None
        if task is not None and task.status in {"running", "recovering"}:
            if session_id:
                await self._persist_message(
                    session_id,
                    "assistant",
                    text,
                    kind="partial",
                    terminated_reason="cancelled",
                )
            await self._tasks.append_event(
                task_id,
                event_type="execution.cancelled",
                status="cancelled",
                name="execution",
                output_json={"kind": "partial", "terminated_reason": "cancelled"},
                summary="execution cancelled by user",
            )
            await self._task_controller.finish_turn(
                task_id,
                kind="partial",
                text=text,
                task_status="cancelled",
            )
        return TurnResult(
            text=text,
            session_id=session_id,
            task_id=task_id,
            kind="partial",
            terminated_reason="cancelled",
            verification_status="skipped",
            degraded_warnings=[f"Cancelled before completing: {user_message[:160]}"],
        )


__all__ = ["TurnCompletion", "TurnExecution", "TurnResult"]
