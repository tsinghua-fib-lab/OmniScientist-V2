"""Terminal lifecycle operations shared by workflow execution paths."""

from __future__ import annotations

from typing import Any

from omni.runtime.workflow_state import workflow_step_record


class WorkflowExecutionError(RuntimeError):
    """Workflow failed after persisting a structured partial result."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


def mark_workflow_cancelled(result: dict[str, Any], *, cancelled: bool) -> None:
    """Apply the user-facing terminal summary without losing partial state."""
    if not cancelled:
        return
    result["status"] = "cancelled"
    result["warning"] = "workflow cancelled by user after preserving partial state"
    result["summary"] = (
        "The workflow stopped at the user's request; completed results and recovery state "
        "were retained."
    )


async def settle_cancelled_wave(
    *,
    workflow_run_id: str,
    wave: list[dict[str, Any]],
    step_records: list[dict[str, Any]],
    results_by_id: dict[str, Any],
    terminal_ids: set[str],
    failed_step_ids: set[str],
    state_store: Any,
    progress: Any,
    total: int,
) -> None:
    """Persist every in-flight step as recoverably cancelled."""
    for step in wave:
        step_id = str(step["id"])
        result = {
            "status": "cancelled",
            "warning": "cancelled by user during execution",
            "recoverable": True,
        }
        record = workflow_step_record(
            step,
            status="cancelled",
            result=result,
            warning="cancelled by user during execution",
            skip_reason="cancelled",
            recoverable=True,
        )
        step_records.append(record)
        results_by_id[step_id] = result
        terminal_ids.add(step_id)
        failed_step_ids.add(step_id)
        await state_store.persist_step_outcome(workflow_run_id, record)
        await progress(
            "workflow.step.cancelled",
            min(0.99, len(terminal_ids) / total),
            step_id=step_id,
            skill=step.get("skill_name", ""),
            reason="cancelled",
        )


__all__ = [
    "WorkflowExecutionError",
    "mark_workflow_cancelled",
    "settle_cancelled_wave",
]
