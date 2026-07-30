"""Settling a workflow step must survive a busy store.

SQLite admits one writer, and a step reaches its terminal state while the rest
of the run is still writing. Losing that race used to lose the outcome: the
cancel that could not be written came back as the failure of the write, and the
run reported a tool error where the user had asked it to stop.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from omni.config import load_settings
from omni.runtime.workflow_state_store import WorkflowStateStore
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM, TaskORM, WorkflowRunORM, WorkflowStepORM


async def _store_with_one_running_step(name: str) -> tuple[WorkflowStateStore, str]:
    settings = load_settings(project=name)
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
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
                current_execution_id="exec-1",
            )
        )
        session.add(
            SubtaskORM(id="exec-1", task_id="task-1", skill_name="slow", status="scheduled")
        )
        await session.commit()
    return WorkflowStateStore(db), "run-1"


_CANCELLED = {
    "id": "slow",
    "status": "cancelled",
    "warning": "cancelled by user during execution",
    "error": "cancelled",
    "recoverable": True,
}


@pytest.mark.asyncio
async def test_a_step_settles_even_when_the_first_write_finds_the_store_busy():
    store, run_id = await _store_with_one_running_step("busy-store")
    attempts: list[int] = []
    write = store._write_step_outcome

    async def busy_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("UPDATE workflow_steps", {}, Exception("database is locked"))
        return await write(*args, **kwargs)

    store._write_step_outcome = busy_once

    await store.persist_step_outcome(run_id, dict(_CANCELLED))

    assert len(attempts) == 2
    async with store._db.session() as session:
        step = await session.get(WorkflowStepORM, "step-1")
        assert step.status == "cancelled"
        assert step.warning == "cancelled by user during execution"


@pytest.mark.asyncio
async def test_a_step_settles_after_a_long_windows_lock_queue():
    """Windows keeps an aiosqlite worker lock after the cancelled task is gone."""
    store, run_id = await _store_with_one_running_step("long-busy-store")
    attempts: list[int] = []
    write = store._write_step_outcome

    async def busy_then_write(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 8:
            raise OperationalError("UPDATE workflow_steps", {}, Exception("database is locked"))
        return await write(*args, **kwargs)

    store._write_step_outcome = busy_then_write

    await store.persist_step_outcome(run_id, dict(_CANCELLED))

    assert len(attempts) == 8
    async with store._db.session() as session:
        step = await session.get(WorkflowStepORM, "step-1")
        assert step.status == "cancelled"


@pytest.mark.asyncio
async def test_a_write_that_fails_for_another_reason_is_not_retried():
    """Only a busy store is a queue. Everything else is the caller's problem."""
    store, run_id = await _store_with_one_running_step("broken-store")
    attempts: list[int] = []

    async def broken(*_args, **_kwargs):
        attempts.append(1)
        raise OperationalError("UPDATE workflow_steps", {}, Exception("no such column: x"))

    store._write_step_outcome = broken

    with pytest.raises(OperationalError):
        await store.persist_step_outcome(run_id, dict(_CANCELLED))

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_the_step_is_written_after_its_lookups_not_between_them():
    """The pending step update must not flush during the execution lookup.

    Autoflush turned that read into a write, so the write lock was taken early
    and then held across the lookup that followed -- the longest hold, on the
    path with the most contention. Written at the commit instead, it is one
    short hold at the end. The order is the whole of the difference: either way
    the row is updated exactly once.
    """
    store, run_id = await _store_with_one_running_step("one-write")
    order: list[str] = []

    @event.listens_for(store._db.engine.sync_engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, *_args):  # noqa: ANN001, ANN202
        text = " ".join(statement.split()).upper()
        if text.startswith("UPDATE WORKFLOW_STEPS"):
            order.append("write step")
        elif text.startswith("SELECT") and "FROM SUBTASKS" in text:
            order.append("read execution")

    try:
        await store.persist_step_outcome(run_id, dict(_CANCELLED))
    finally:
        event.remove(store._db.engine.sync_engine, "before_cursor_execute", _record)

    assert order == ["read execution", "write step"]
    async with store._db.session() as session:
        execution = await session.get(SubtaskORM, "exec-1")
        assert execution.status == "skipped"


@pytest.mark.asyncio
async def test_a_result_persists_even_when_the_first_write_finds_the_store_busy():
    store, run_id = await _store_with_one_running_step("busy-result")
    attempts: list[int] = []
    write = store._write_result

    async def busy_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("UPDATE workflow_runs", {}, Exception("database is locked"))
        return await write(*args, **kwargs)

    store._write_result = busy_once
    await store.persist_result(run_id, {"status": "cancelled"}, current_step_id="slow")

    assert len(attempts) == 2
    async with store._db.session() as session:
        run = await session.get(WorkflowRunORM, run_id)
        assert run.result_json["status"] == "cancelled"
        assert run.current_step_id == "slow"


@pytest.mark.asyncio
async def test_cancel_finish_closes_leftover_running_and_pending_steps():
    """A swallowed in-wave persist must not leave steps pending or running."""
    from omni.runtime.workflow_lifecycle import write_workflow_finish

    store, run_id = await _store_with_one_running_step("finish-closes-steps")
    async with store._db.session() as session:
        session.add(
            WorkflowStepORM(
                id="step-2",
                workflow_run_id=run_id,
                task_id="task-1",
                step_key="after",
                status="pending",
            )
        )
        await session.commit()

    await write_workflow_finish(
        store._db,
        run_id,
        status="cancelled",
        result={"status": "cancelled"},
        error="",
        trace=[],
    )

    async with store._db.session() as session:
        run = await session.get(WorkflowRunORM, run_id)
        slow = await session.get(WorkflowStepORM, "step-1")
        after = await session.get(WorkflowStepORM, "step-2")
    assert run is not None and run.status == "cancelled"
    assert slow is not None and slow.status == "cancelled"
    assert after is not None and after.status == "skipped"
