"""Cancel persist must finish after Task.cancel() on Python 3.11+."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy.exc import OperationalError

from omni.config import load_settings
from omni.runtime.cancel_persist import (
    complete_despite_cancel,
    ignore_cancellation,
    persist_best_effort,
    run_uncancelled,
)
from omni.runtime.skill_execution_store import SkillExecutionStore
from omni.runtime.task_recorder import TaskRecorder
from omni.runtime.workflow_lifecycle import persist_workflow_done, write_workflow_finish
from omni.storage.db import get_database, retry_while_busy
from omni.storage.models import SubtaskORM, TaskORM, WorkflowRunORM, WorkflowStepORM


async def _db(name: str):
    settings = load_settings(project=name)
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    return db


async def _run_after_cancel(work) -> None:
    """Deliver Task.cancel(), then persist the way production except-handlers do."""

    async def runner() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            with ignore_cancellation():
                await work()
            raise

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(runner())


@pytest.mark.asyncio
async def test_ignore_cancellation_finishes_the_inner_work() -> None:
    seen: list[str] = []

    async def work() -> None:
        await asyncio.sleep(0)
        seen.append("wrote")

    await _run_after_cancel(work)
    assert seen == ["wrote"]


@pytest.mark.asyncio
async def test_finish_cancelled_from_a_cancelled_task() -> None:
    db = await _db("cancel-skill-persist")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.flush()
        session.add(
            SubtaskORM(id="exec-1", task_id="task-1", skill_name="slow", status="running")
        )
        await session.commit()
    store = SkillExecutionStore(db)

    async def work() -> None:
        await store.finish_cancelled(
            "exec-1",
            result={"status": "cancelled", "recoverable": True},
            trace=[],
        )

    await _run_after_cancel(work)
    async with db.session() as session:
        row = await session.get(SubtaskORM, "exec-1")
    assert row is not None and row.status == "cancelled"
    assert row.result_json["recoverable"] is True


@pytest.mark.asyncio
async def test_write_workflow_finish_from_a_cancelled_task() -> None:
    db = await _db("cancel-workflow-persist")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.flush()
        session.add(
            WorkflowRunORM(id="run-1", task_id="task-1", goal="cancel me", status="running")
        )
        session.add(
            WorkflowStepORM(
                id="step-1",
                workflow_run_id="run-1",
                task_id="task-1",
                step_key="slow",
                status="running",
            )
        )
        await session.commit()

    async def work() -> None:
        await write_workflow_finish(
            db,
            "run-1",
            status="cancelled",
            result={"status": "cancelled"},
            error="",
            trace=[],
        )

    await _run_after_cancel(work)
    async with db.session() as session:
        run = await session.get(WorkflowRunORM, "run-1")
        step = await session.get(WorkflowStepORM, "step-1")
    assert run is not None and run.status == "cancelled"
    assert step is not None and step.status == "cancelled"


@pytest.mark.asyncio
async def test_parent_turn_settles_leftover_running_children() -> None:
    db = await _db("cancel-parent-settle")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.flush()
        session.add(
            WorkflowRunORM(id="run-1", task_id="task-1", goal="cancel me", status="running")
        )
        session.add(
            WorkflowStepORM(
                id="step-1",
                workflow_run_id="run-1",
                task_id="task-1",
                step_key="slow",
                status="running",
            )
        )
        session.add(
            SubtaskORM(id="exec-1", task_id="task-1", skill_name="slow", status="running")
        )
        await session.commit()

    recorder = TaskRecorder(db, project="cancel-parent-settle")
    await recorder.settle_open_children_for_cancel("task-1")

    async with db.session() as session:
        run = await session.get(WorkflowRunORM, "run-1")
        step = await session.get(WorkflowStepORM, "step-1")
        child = await session.get(SubtaskORM, "exec-1")
    assert run is not None and run.status == "cancelled"
    assert step is not None and step.status == "cancelled"
    assert child is not None and child.status == "cancelled"
    assert child.result_json["recoverable"] is True


@pytest.mark.asyncio
async def test_run_uncancelled_finishes_when_parent_cancels_the_waiter() -> None:
    """Parent cancel also cancels the awaited child; persist must outlive that."""
    wrote: list[str] = []

    async def work() -> None:
        await asyncio.sleep(0.05)
        wrote.append("ok")

    async def child() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await run_uncancelled(work)
            raise

    async def parent() -> None:
        await asyncio.create_task(child())

    running = asyncio.create_task(parent())
    await asyncio.sleep(0.01)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert wrote == ["ok"]


@pytest.mark.asyncio
async def test_run_uncancelled_writes_workflow_when_parent_cancels_the_waiter() -> None:
    db = await _db("cancel-parent-waiter")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.flush()
        session.add(
            WorkflowRunORM(id="run-1", task_id="task-1", goal="cancel me", status="running")
        )
        await session.commit()

    async def work() -> None:
        await write_workflow_finish(
            db,
            "run-1",
            status="cancelled",
            result={"status": "cancelled"},
            error="",
            trace=[],
        )

    async def child() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await run_uncancelled(work)
            raise

    async def parent() -> None:
        await asyncio.create_task(child())

    running = asyncio.create_task(parent())
    await asyncio.sleep(0.01)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    async with db.session() as session:
        run = await session.get(WorkflowRunORM, "run-1")
    assert run is not None and run.status == "cancelled"


@pytest.mark.asyncio
async def test_complete_despite_cancel_finishes_cancelled_when_execute_dies() -> None:
    finished: list[str] = []

    async def execute() -> str:
        await asyncio.sleep(30)
        return "ok"

    async def finish(outcome: str) -> None:
        await asyncio.sleep(0.05)
        finished.append(outcome)

    async def child() -> None:
        await complete_despite_cancel(execute, finish, "cancelled")

    running = asyncio.create_task(child())
    await asyncio.sleep(0.01)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert finished == ["cancelled"]


def _locked() -> OperationalError:
    return OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))


@pytest.mark.asyncio
async def test_persist_best_effort_swallows_sqlite_busy() -> None:
    async def work() -> None:
        raise _locked()

    assert await persist_best_effort(work) is None


@pytest.mark.asyncio
async def test_complete_despite_cancel_keeps_cancelled_when_finish_is_locked() -> None:
    async def execute() -> str:
        await asyncio.sleep(30)
        return "ok"

    async def finish(outcome: str) -> None:
        del outcome
        raise _locked()

    running = asyncio.create_task(complete_despite_cancel(execute, finish, "cancelled"))
    await asyncio.sleep(0.01)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


@pytest.mark.asyncio
async def test_persist_best_effort_caps_nested_busy_retries() -> None:
    attempts = 0

    async def write() -> None:
        nonlocal attempts
        attempts += 1
        raise _locked()

    async def work() -> None:
        await retry_while_busy(write)

    assert await persist_best_effort(work) is None
    assert attempts == 2


@pytest.mark.asyncio
async def test_complete_despite_cancel_swallows_busy_when_execute_returns_cancelled() -> None:
    async def execute() -> tuple[str, dict, str]:
        return ("cancelled", {"status": "cancelled", "steps": []}, "")

    async def finish(outcome: tuple[str, dict, str]) -> None:
        del outcome
        raise _locked()

    result = await complete_despite_cancel(
        execute, finish, ("cancelled", {"status": "cancelled"}, "")
    )
    assert result[0] == "cancelled"


@pytest.mark.asyncio
async def test_persist_workflow_done_swallows_busy_checkpoint() -> None:
    async def progress(*_args, **_kwargs) -> None:
        return None

    async def persist_checkpoint() -> None:
        raise _locked()

    await persist_workflow_done(
        progress=progress,
        persist_checkpoint=persist_checkpoint,
        cancelled=True,
        total=2,
        result={"skills_used": ["slow-step"], "status": "cancelled"},
    )
