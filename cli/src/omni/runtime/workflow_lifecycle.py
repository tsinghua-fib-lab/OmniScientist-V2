"""Terminal lifecycle operations shared by workflow execution paths."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from omni.runtime.workflow_state import workflow_step_record
from omni.storage.models import WorkflowRunORM, WorkflowStepORM, _utcnow

_OPEN_STEP_STATUSES = frozenset({"pending", "scheduled", "running", "recovering"})


class WorkflowExecutionError(RuntimeError):
    """Workflow failed after persisting a structured partial result."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


def cancelled_workflow_result() -> dict[str, Any]:
    """User-facing payload when a workflow stops at the user's request."""
    return {
        "status": "cancelled",
        "summary": (
            "The workflow stopped at the user's request; completed results "
            "and recovery state were retained."
        ),
        "warning": "workflow cancelled by user after preserving partial state",
        "recoverable": True,
    }


def mark_workflow_cancelled(result: dict[str, Any], *, cancelled: bool) -> None:
    """Apply the user-facing terminal summary without losing partial state."""
    if not cancelled:
        return
    result.update(cancelled_workflow_result())


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


async def persist_cancelled_wave(
    *,
    persist_state: Any,
    **settle_kwargs: Any,
) -> None:
    """Settle an in-flight wave and write the cancelled checkpoint."""
    from omni.storage.db import sqlite_busy

    try:
        await settle_cancelled_wave(**settle_kwargs)
        await persist_state("cancelled")
    except OperationalError as exc:
        if not sqlite_busy(exc):
            raise


async def close_open_steps_for_cancel(session: Any, workflow_run_id: str) -> None:
    """Mark leftover open steps cancelled or skipped in the finish transaction.

    In-wave persist can lose the SQLite lock after the asyncio task is gone.
    The run still finishes cancelled; these rows must not stay pending/running.
    """
    rows = list(
        (
            await session.execute(
                select(WorkflowStepORM).where(
                    WorkflowStepORM.workflow_run_id == workflow_run_id
                )
            )
        ).scalars().all()
    )
    now = _utcnow()
    for row in rows:
        if row.status not in _OPEN_STEP_STATUSES:
            continue
        row.status = "cancelled" if row.status == "running" else "skipped"
        row.error = row.error or "cancelled"
        row.warning = row.warning or "cancelled by user"
        row.recoverable = True
        row.finished_at = now


async def persist_workflow_done(
    *,
    progress: Any,
    persist_checkpoint: Any,
    cancelled: bool,
    total: int,
    result: dict[str, Any],
) -> None:
    """Write the terminal progress/checkpoint without failing a busy cancel."""
    from omni.runtime.cancel_persist import persist_best_effort
    from omni.storage.db import sqlite_busy

    try:
        await progress(
            "workflow.done",
            1.0,
            total_steps=total,
            skills_used=result["skills_used"],
            status=result["status"],
        )
        if cancelled:
            await persist_best_effort(persist_checkpoint)
        else:
            await persist_checkpoint()
    except OperationalError as exc:
        if not sqlite_busy(exc):
            raise


async def write_workflow_finish(
    db: Any,
    workflow_run_id: str,
    *,
    status: str,
    result: dict[str, Any],
    error: str,
    trace: list[dict[str, Any]],
) -> None:
    """Persist the run's terminal row, and close open steps on cancel."""
    async with db.session() as session:
        run = await session.get(WorkflowRunORM, workflow_run_id)
        if run is None:
            return
        run.status = status
        run.result_json = result
        run.error = error
        run.trace_log = trace
        run.current_step_id = ""
        run.finished_at = _utcnow()
        if status == "cancelled":
            await close_open_steps_for_cancel(session, workflow_run_id)
        await session.commit()


__all__ = [
    "WorkflowExecutionError",
    "cancelled_workflow_result",
    "close_open_steps_for_cancel",
    "mark_workflow_cancelled",
    "persist_cancelled_wave",
    "persist_workflow_done",
    "settle_cancelled_wave",
    "write_workflow_finish",
]
