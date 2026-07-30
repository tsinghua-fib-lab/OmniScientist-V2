"""Classification and child-agent execution for one workflow step."""

from __future__ import annotations

from typing import Any

from omni.runtime.deliverable_assessment import (
    bind_deliverable_assessment_identity,
)
from omni.runtime.task_results import (
    _result_error_message,
    _result_warning_message,
)
from omni.runtime.workflow_plan import _step_failure_recoverable
from omni.runtime.workflow_state import workflow_step_record
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.registry import resolve_step_entry


async def classify_workflow_outcome(
    registry: Any,
    step: dict[str, Any],
    outcome: dict[str, Any],
    progress: Any,
    *,
    completed_count: int,
    total: int,
) -> dict[str, Any]:
    """Project one provider outcome into the durable workflow step contract."""
    step_id = str(step["id"])
    skill_name = str(step.get("skill_name") or "")
    entry = resolve_step_entry(registry, step)
    result = outcome.get("result")
    if not isinstance(result, dict):
        result = {"result": result}
    bind_deliverable_assessment_identity(result, step)
    execution_id = str(outcome.get("execution_id") or "")
    child_task_id = str(outcome.get("child_task_id") or "")
    attempt = int(outcome.get("attempt") or 0)
    extras: dict[str, Any] = {}
    if execution_id:
        extras.update(execution_id=execution_id, subtask_id=execution_id)
    if child_task_id:
        extras["child_task_id"] = child_task_id
    if attempt:
        extras["execution_attempt"] = attempt
    result_status = str(result.get("status", "")).lower()
    runtime_status = str(outcome.get("status") or "").lower()
    if (
        runtime_status == "degraded"
        or result_status in {"partial", "degraded", "warning"}
    ):
        warning = _result_warning_message(result) or str(
            outcome.get("error") or ""
        )
        await progress(
            "workflow.step.degraded",
            min(0.99, (completed_count + 1) / total),
            step_id=step_id,
            skill=skill_name,
            execution_id=execution_id,
            child_task_id=child_task_id,
            warning=warning,
            recoverable=True,
        )
        record = workflow_step_record(
            {**step, **extras},
            status="degraded",
            result=result,
            warning=warning,
            recoverable=True,
        )
        return {"result": result, "record": record}
    if (
        runtime_status in {"failed", "cancelled"}
        or result_status in {"error", "failed"}
    ):
        error = str(outcome.get("error") or "") or _result_error_message(result)
        recoverable = _step_failure_recoverable(step, entry, result)
        await progress(
            "workflow.step.failed",
            min(0.99, (completed_count + 1) / total),
            step_id=step_id,
            skill=skill_name,
            execution_id=execution_id,
            child_task_id=child_task_id,
            error=error,
            recoverable=recoverable,
        )
        record = workflow_step_record(
            {**step, **extras},
            status="failed",
            result=result,
            error=error,
            recoverable=recoverable,
        )
        return {"result": result, "record": record}
    await progress(
        "workflow.step.done",
        min(0.99, (completed_count + 1) / total),
        step_id=step_id,
        skill=skill_name,
        execution_id=execution_id,
        child_task_id=child_task_id,
    )
    record = workflow_step_record(
        {**step, **extras},
        status="succeeded",
        result=result,
    )
    return {"result": result, "record": record}


async def execute_child_task(
    step: dict[str, Any],
    step_input: dict[str, Any],
    ctx: ExecContext,
) -> dict[str, Any]:
    """Delegate nested planning as an independently inspectable child task."""
    from omni.agent.subagents import SubagentSpec, run_subagent

    goal = str(
        step_input.get("goal")
        or step_input.get("input")
        or step_input.get("workflow_goal")
        or ""
    )
    spec = SubagentSpec(
        goal=goal,
        role=str(step_input.get("role") or "workflow specialist"),
        context=str(step_input.get("context") or ""),
        tools=tuple(str(item) for item in step_input.get("tools") or []),
        model=str(step_input.get("model") or ""),
        compute_profile=str(step_input.get("compute_profile") or ""),
        isolation=str(step_input.get("isolation") or ""),
    )
    result = await run_subagent(
        spec,
        ctx,
        settings=ctx.settings,
        depth=int(getattr(ctx, "subagent_depth", 0) or 0),
    )
    status = {
        "ok": "succeeded",
        "partial": "degraded",
        "escalated": "degraded",
        "rejected": "failed",
        "error": "failed",
    }.get(result.status, "failed")
    payload = result.to_dict()
    payload["status"] = (
        "partial"
        if status == "degraded"
        else "ok"
        if status == "succeeded"
        else "error"
    )
    return {
        "status": status,
        "result": payload,
        "error": result.summary if status == "failed" else "",
        "child_task_id": result.task_id,
    }


__all__ = ["classify_workflow_outcome", "execute_child_task"]
