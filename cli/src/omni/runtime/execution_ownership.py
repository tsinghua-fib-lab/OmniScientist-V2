"""PID-aware ownership for durable skill executions.

Codex treats process death as the end of the in-flight turn: resume starts
from a terminal record, not a stuck ``Working`` state. Omni persists
executions in SQLite, so the equivalent is a reconcile: a lost owner moves
the row to ``interrupted`` (or ``cancelled`` only when a *pending* user
cancel is waiting), and retry/requeue stay the explicit recovery verbs.
Consumed cancel rows are not replayed.

Live owners are never stolen. A missing ``owner_pid`` (legacy row) is lost
immediately only on an explicit user or daemon recovery; periodic
housekeeping waits for ``tasks.interrupt_stale_after_s``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from omni.runtime.daemon import pid_alive
from omni.runtime.task_results import _aware_dt
from omni.storage.db import Database
from omni.storage.models import SubtaskORM, TaskControlORM, _utcnow

logger = logging.getLogger(__name__)

_RUNNING = "running"


@dataclass(slots=True)
class ReconcileReport:
    """What one ownership sweep settled."""

    interrupted_ids: list[str] = field(default_factory=list)
    cancelled_ids: list[str] = field(default_factory=list)
    requeued_workflow_ids: list[str] = field(default_factory=list)
    applied_control_ids: list[str] = field(default_factory=list)
    settled_task_ids: list[str] = field(default_factory=list)

    @property
    def settled_ids(self) -> list[str]:
        return [*self.interrupted_ids, *self.cancelled_ids]


def execution_owner_lost(
    owner_pid: int,
    started_at: datetime | None,
    created_at: datetime | None,
    *,
    stale_after_s: float,
    explicit: bool,
) -> bool:
    """Whether this claim can be taken over without stealing a live process."""
    if owner_pid > 0:
        return not pid_alive(owner_pid)
    if explicit:
        return True
    if stale_after_s <= 0:
        return False
    mark = _aware_dt(started_at or created_at)
    return (_utcnow() - mark).total_seconds() >= stale_after_s


def _row_owner_lost(row: SubtaskORM, *, stale_after_s: float, explicit: bool) -> bool:
    return execution_owner_lost(
        int(getattr(row, "owner_pid", 0) or 0),
        row.started_at,
        row.created_at,
        stale_after_s=stale_after_s,
        explicit=explicit,
    )


def _has_live_owner(row: SubtaskORM) -> bool:
    pid = int(getattr(row, "owner_pid", 0) or 0)
    return pid > 0 and pid_alive(pid)


async def list_lost_executors(
    db: Database,
    *,
    stale_after_s: float,
    explicit: bool = False,
) -> list[SubtaskORM]:
    """Running executions whose owner is gone (for doctor / hygiene)."""
    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(SubtaskORM).where(SubtaskORM.status == _RUNNING)
                )
            ).scalars().all()
        )
    return [
        row
        for row in rows
        if _row_owner_lost(row, stale_after_s=stale_after_s, explicit=explicit)
    ]


async def reconcile_lost_executors(
    *,
    db: Database,
    task_recorder: Any | None,
    stale_after_s: float,
    task_id: str | None = None,
    execution_id: str | None = None,
    explicit: bool = False,
    requeue_workflow: bool = False,
) -> ReconcileReport:
    """Settle or requeue running executions whose owner is gone.

    Standalone orphans become ``cancelled`` when a cancel control is waiting,
    otherwise ``interrupted``. Workflow-linked orphans are returned to
    ``pending`` when ``requeue_workflow`` is set so a durable workflow can
    continue; they are left alone otherwise.
    """
    report = ReconcileReport()
    async with db.session() as session:
        query = select(SubtaskORM).where(SubtaskORM.status == _RUNNING)
        if task_id:
            query = query.where(SubtaskORM.task_id == task_id)
        if execution_id:
            query = query.where(SubtaskORM.id == execution_id)
        rows = list((await session.execute(query)).scalars().all())

    by_task: dict[str, list[SubtaskORM]] = {}
    for row in rows:
        if not _row_owner_lost(row, stale_after_s=stale_after_s, explicit=explicit):
            continue
        owner = str(row.task_id or "")
        by_task.setdefault(owner, []).append(row)

    for owner, orphans in by_task.items():
        if owner and task_recorder is not None:
            await task_recorder.recover_consumed_controls(owner)
        has_cancel = bool(owner) and await _task_has_cancel(db, owner)
        for row in orphans:
            if row.workflow_run_id:
                if requeue_workflow and await _cas_requeue_workflow(db, row):
                    report.requeued_workflow_ids.append(row.id)
                continue
            status = "cancelled" if has_cancel else "interrupted"
            if await _cas_finish_standalone(db, row, status=status):
                if status == "cancelled":
                    report.cancelled_ids.append(row.id)
                else:
                    report.interrupted_ids.append(row.id)
                await _record_settlement(task_recorder, row, status=status)
        if owner:
            applied = await _close_parent_if_unowned(
                db,
                task_recorder,
                owner,
                prefer_cancel=has_cancel,
            )
            if applied.task_settled:
                report.settled_task_ids.append(owner)
            report.applied_control_ids.extend(applied.control_ids)
    if report.settled_ids or report.requeued_workflow_ids:
        logger.info(
            "reconciled lost executors: interrupted=%d cancelled=%d "
            "workflow_requeued=%d tasks=%d",
            len(report.interrupted_ids),
            len(report.cancelled_ids),
            len(report.requeued_workflow_ids),
            len(report.settled_task_ids),
        )
    return report


@dataclass(slots=True)
class _ParentClose:
    task_settled: bool = False
    control_ids: list[str] = field(default_factory=list)


async def _task_has_cancel(db: Database, task_id: str) -> bool:
    """True only for a user cancel that was never claimed.

    A consumed cancel already reached an execution; replaying it after
    process death would mark the next serve/schedule attempt cancelled.
    Pending is the durable "user asked to stop" that nobody applied yet.
    """
    async with db.session() as session:
        row = (
            await session.execute(
                select(TaskControlORM.id)
                .where(
                    TaskControlORM.task_id == task_id,
                    TaskControlORM.action == "cancel",
                    TaskControlORM.status == "pending",
                )
                .limit(1)
            )
        ).first()
    return row is not None


async def _cas_finish_standalone(
    db: Database, row: SubtaskORM, *, status: str
) -> bool:
    if status == "cancelled":
        result = {
            "status": "cancelled",
            "summary": f"{row.skill_name} was cancelled after its executor was lost.",
            "recoverable": True,
        }
        error = ""
    else:
        result = {
            "status": "interrupted",
            "summary": (
                f"{row.skill_name} was interrupted; the owning executor is gone."
            ),
            "recoverable": True,
        }
        error = (
            "interrupted: owning executor exited before the skill execution finished"
        )
    async with db.session() as session:
        changed = await session.execute(
            update(SubtaskORM)
            .where(
                SubtaskORM.id == row.id,
                SubtaskORM.status == _RUNNING,
                SubtaskORM.owner_pid == int(getattr(row, "owner_pid", 0) or 0),
            )
            .values(
                status=status,
                result_json=result,
                error=error,
                finished_at=_utcnow(),
                owner_pid=0,
                recovery_policy="lost_executor",
            )
        )
        await session.commit()
    return int(changed.rowcount or 0) == 1


async def _cas_requeue_workflow(db: Database, row: SubtaskORM) -> bool:
    async with db.session() as session:
        changed = await session.execute(
            update(SubtaskORM)
            .where(
                SubtaskORM.id == row.id,
                SubtaskORM.status == _RUNNING,
                SubtaskORM.owner_pid == int(getattr(row, "owner_pid", 0) or 0),
            )
            .values(status="pending", owner_pid=0)
        )
        await session.commit()
    return int(changed.rowcount or 0) == 1


async def _record_settlement(
    task_recorder: Any | None, row: SubtaskORM, *, status: str
) -> None:
    if task_recorder is None or not row.task_id:
        return
    result = {
        "status": status,
        "summary": (
            f"{row.skill_name} was cancelled after its executor was lost."
            if status == "cancelled"
            else f"{row.skill_name} was interrupted; the owning executor is gone."
        ),
        "recoverable": True,
        "recovery_policy": "lost_executor",
    }
    await task_recorder.append_event(
        row.task_id,
        event_type=f"subtask.{status}",
        status=status,
        name=row.skill_name,
        skill_name=row.skill_name,
        workflow_run_id=row.workflow_run_id or "",
        workflow_step_id=row.workflow_step_id or "",
        subtask_id=row.id,
        output_json=result,
        error="" if status == "cancelled" else result["summary"],
        summary=result["summary"],
    )


async def _close_parent_if_unowned(
    db: Database,
    task_recorder: Any | None,
    task_id: str,
    *,
    prefer_cancel: bool,
) -> _ParentClose:
    """Settle the parent turn when no live executor remains."""
    outcome = _ParentClose()
    async with db.session() as session:
        remaining = list(
            (
                await session.execute(
                    select(SubtaskORM).where(
                        SubtaskORM.task_id == task_id,
                        SubtaskORM.status == _RUNNING,
                    )
                )
            ).scalars().all()
        )
    if any(_has_live_owner(row) for row in remaining):
        return outcome
    if remaining:
        # Other running rows still exist (legacy pid=0 on a periodic sweep, or
        # a sibling this pass did not target). Leave the parent active.
        return outcome
    if task_recorder is None:
        return outcome
    if prefer_cancel:
        controls = await task_recorder.consume_controls(task_id, actions={"cancel"})
        if controls:
            await task_recorder.mark_controls_applied(
                [str(item["id"]) for item in controls]
            )
            outcome.control_ids = [str(item["id"]) for item in controls]
        await task_recorder.finish_task(
            task_id,
            status="cancelled",
            summary="Execution was cancelled after its executor was lost.",
        )
    else:
        await task_recorder.finish_task(
            task_id,
            status="interrupted",
            summary="Execution was interrupted; the owning executor is gone.",
            error=(
                "interrupted: owning executor exited before the skill execution "
                "finished"
            ),
        )
    outcome.task_settled = True
    return outcome


__all__ = [
    "ReconcileReport",
    "execution_owner_lost",
    "list_lost_executors",
    "reconcile_lost_executors",
]
