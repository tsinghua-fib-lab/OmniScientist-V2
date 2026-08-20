"""Cancel persist must finish after Task.cancel() on Python 3.11+."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from omni.agent.task_controller import TaskController
from omni.config import load_settings
from omni.runtime.cancel_persist import (
    complete_despite_cancel,
    exclusive_persist,
    ignore_cancellation,
    persist_best_effort,
    persist_lock,
    persist_scope,
    run_uncancelled,
)
from omni.runtime.skill_execution_store import SkillExecutionStore
from omni.runtime.task_recorder import TaskRecorder
from omni.runtime.workflow_lifecycle import persist_workflow_done, write_workflow_finish
from omni.storage.db import busy_retry_budget, get_database, retry_while_busy
from omni.storage.models import (
    SubtaskORM,
    TaskEventORM,
    TaskORM,
    WorkflowRunORM,
    WorkflowStepORM,
)


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
async def test_parent_settle_retries_when_the_first_write_finds_the_store_busy() -> None:
    db = await _db("cancel-parent-settle-busy")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.flush()
        session.add(
            SubtaskORM(id="exec-1", task_id="task-1", skill_name="slow", status="running")
        )
        await session.commit()

    recorder = TaskRecorder(db, project="cancel-parent-settle-busy")
    write = recorder._write_open_children_cancelled
    attempts: list[int] = []

    async def busy_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise _locked()
        return await write(*args, **kwargs)

    recorder._write_open_children_cancelled = busy_once  # type: ignore[method-assign]
    await recorder.settle_open_children_for_cancel("task-1")

    assert len(attempts) == 2
    async with db.session() as session:
        child = await session.get(SubtaskORM, "exec-1")
    assert child is not None and child.status == "cancelled"


@pytest.mark.asyncio
async def test_parent_settle_does_not_fail_the_turn_when_the_store_stays_busy() -> None:
    db = await _db("cancel-parent-settle-still-busy")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.flush()
        session.add(
            SubtaskORM(id="exec-1", task_id="task-1", skill_name="slow", status="running")
        )
        await session.commit()

    recorder = TaskRecorder(db, project="cancel-parent-settle-still-busy")

    async def always_busy(*_args, **_kwargs):
        raise _locked()

    recorder._write_open_children_cancelled = always_busy  # type: ignore[method-assign]
    with busy_retry_budget(2):
        await recorder.settle_open_children_for_cancel("task-1")

    async with db.session() as session:
        child = await session.get(SubtaskORM, "exec-1")
    assert child is not None and child.status == "running"


@pytest.mark.asyncio
async def test_parent_settle_writes_after_the_busy_queue_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _db("cancel-parent-settle-after-queue")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.flush()
        session.add(
            WorkflowRunORM(id="run-1", task_id="task-1", goal="cancel me", status="running")
        )
        session.add(
            SubtaskORM(id="exec-1", task_id="task-1", skill_name="slow", status="running")
        )
        await session.commit()

    recorder = TaskRecorder(db, project="cancel-parent-settle-after-queue")

    async def fail_queue(_write, **_kwargs):
        raise _locked()

    monkeypatch.setattr("omni.runtime.task_recorder.retry_while_busy", fail_queue)
    await recorder.settle_open_children_for_cancel("task-1")

    async with db.session() as session:
        run = await session.get(WorkflowRunORM, "run-1")
        child = await session.get(SubtaskORM, "exec-1")
    assert run is not None and run.status == "cancelled"
    assert child is not None and child.status == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_session_releases_lock_for_parent_settle() -> None:
    db = await _db("cancel-session-release")
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

    entered = asyncio.Event()

    async def hold_write_lock() -> None:
        async with db.session() as session:
            run = await session.get(WorkflowRunORM, "run-1")
            assert run is not None
            run.trace_log = [{"stage": "hold"}]
            entered.set()
            await asyncio.sleep(30)
            await session.commit()

    holder = asyncio.create_task(hold_write_lock())
    await entered.wait()
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    recorder = TaskRecorder(db, project="cancel-session-release")
    await recorder.settle_open_children_for_cancel("task-1")

    async with db.session() as session:
        run = await session.get(WorkflowRunORM, "run-1")
        step = await session.get(WorkflowStepORM, "step-1")
    assert run is not None and run.status == "cancelled"
    assert step is not None and step.status == "cancelled"


@pytest.mark.asyncio
async def test_persist_lock_held_by_other_is_false_for_the_owner() -> None:
    async with persist_lock():
        assert persist_lock().locked()
        assert persist_lock().held_by_other() is False

        seen: list[bool] = []

        async def other() -> None:
            seen.append(persist_lock().held_by_other())

        await asyncio.create_task(other())
        assert seen == [True]


@pytest.mark.asyncio
async def test_run_uncancelled_serialize_false_leaves_the_lock_free() -> None:
    seen: list[bool] = []

    async def work() -> None:
        seen.append(persist_lock().locked())

    await run_uncancelled(work, serialize=False)
    await run_uncancelled(work)
    assert seen == [False, True]


@pytest.mark.asyncio
async def test_append_event_drops_when_another_task_holds_persist_lock() -> None:
    db = await _db("event-yield-to-cancel")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.commit()
    recorder = TaskRecorder(db, project="event-yield-to-cancel")
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with persist_lock(db):
            holding.set()
            await release.wait()

    worker = asyncio.create_task(holder())
    await holding.wait()
    event = await recorder.append_event(
        "task-1",
        event_type="react.tool.done",
        status="succeeded",
        name="run_workflow",
    )
    release.set()
    await worker
    assert event is None


@pytest.mark.asyncio
async def test_persist_scope_binds_the_unkeyed_lock_to_the_store() -> None:
    db = await _db("persist-scope")
    async with persist_scope(db):
        assert persist_lock() is persist_lock(db)
    assert persist_lock() is not persist_lock(db)


@pytest.mark.asyncio
async def test_append_event_does_not_drop_when_another_database_holds_its_lock() -> None:
    """Isolated eval/black-box attempts share a loop, not a store."""
    db_a = await _db("event-isolated-a")
    db_b = await _db("event-isolated-b")
    async with db_b.session() as session:
        session.add(TaskORM(id="task-b", user_input="self-knowledge", status="running"))
        await session.commit()
    recorder_b = TaskRecorder(db_b, project="event-isolated-b")
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with persist_lock(db_a):
            holding.set()
            await release.wait()

    worker = asyncio.create_task(holder())
    await holding.wait()
    event = await recorder_b.append_event(
        "task-b",
        event_type="react.finished",
        status="succeeded",
        name="react",
    )
    release.set()
    await worker
    assert event is not None
    assert event.event_type == "react.finished"


@pytest.mark.asyncio
async def test_append_event_writes_while_current_task_holds_persist_lock() -> None:
    db = await _db("event-same-owner")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.commit()
    recorder = TaskRecorder(db, project="event-same-owner")
    async with persist_lock(db):
        event = await recorder.append_event(
            "task-1",
            event_type="execution.cancelled",
            status="cancelled",
            name="execution",
        )
    assert event is not None
    assert event.event_type == "execution.cancelled"


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
async def test_exclusive_persist_is_reentrant_on_the_same_task() -> None:
    seen: list[str] = []

    async def inner() -> None:
        async with exclusive_persist():
            seen.append("inner")

    async def work() -> None:
        await inner()
        seen.append("outer")

    await persist_best_effort(work)
    assert seen == ["inner", "outer"]


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


@pytest.mark.asyncio
async def test_finish_task_retries_when_the_first_write_finds_the_store_busy() -> None:
    db = await _db("finish-task-busy")
    rec = TaskRecorder(db, project="finish-task-busy")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.commit()

    write = rec._write_terminal_task
    attempts: list[int] = []

    async def busy_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise _locked()
        return await write(*args, **kwargs)

    rec._write_terminal_task = busy_once  # type: ignore[method-assign]
    await rec.finish_task("task-1", status="cancelled", summary="stopped")

    assert len(attempts) == 2
    async with db.session() as session:
        task = await session.get(TaskORM, "task-1")
        assert task is not None and task.status == "cancelled"


@pytest.mark.asyncio
async def test_finish_task_waits_for_the_cancel_persist_queue() -> None:
    from omni.runtime.cancel_persist import persist_lock

    db = await _db("finish-task-queue")
    rec = TaskRecorder(db, project="finish-task-queue")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.commit()

    lock = persist_lock(db)
    await lock.acquire()
    settling = asyncio.create_task(
        rec.finish_task("task-1", status="cancelled", summary="stopped")
    )
    await asyncio.sleep(0.05)
    try:
        async with db.session() as session:
            task = await session.get(TaskORM, "task-1")
            assert task is not None and task.status == "running"
    finally:
        lock.release()
    await settling
    async with db.session() as session:
        task = await session.get(TaskORM, "task-1")
        assert task is not None and task.status == "cancelled"


@pytest.mark.asyncio
async def test_finish_task_does_not_fail_cancel_when_the_store_stays_busy() -> None:
    db = await _db("finish-task-still-busy")
    rec = TaskRecorder(db, project="finish-task-still-busy")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.commit()

    async def always_busy(*_args, **_kwargs):
        raise _locked()

    rec._write_terminal_task = always_busy  # type: ignore[method-assign]
    with busy_retry_budget(2):
        await rec.finish_task("task-1", status="cancelled", summary="stopped")

    async with db.session() as session:
        task = await session.get(TaskORM, "task-1")
        assert task is not None and task.status == "running"


@pytest.mark.asyncio
async def test_finish_task_still_raises_busy_for_a_successful_settle() -> None:
    db = await _db("finish-task-success-busy")
    rec = TaskRecorder(db, project="finish-task-success-busy")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="done", status="running"))
        await session.commit()

    async def always_busy(*_args, **_kwargs):
        raise _locked()

    rec._write_terminal_task = always_busy  # type: ignore[method-assign]
    with busy_retry_budget(2), pytest.raises(OperationalError):
        await rec.finish_task("task-1", status="succeeded", summary="done")


@pytest.mark.asyncio
async def test_finish_turn_ensures_react_finished_on_cancel() -> None:
    db = await _db("finish-turn-react-finished")
    rec = TaskRecorder(db, project="finish-turn-react-finished")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.commit()

    await TaskController(rec).finish_turn(
        "task-1",
        kind="partial",
        text="stopped",
        task_status="cancelled",
    )

    async with db.session() as session:
        events = list(
            (
                await session.execute(
                    select(TaskEventORM)
                    .where(TaskEventORM.task_id == "task-1")
                    .order_by(TaskEventORM.seq.asc())
                )
            ).scalars().all()
        )
        task = await session.get(TaskORM, "task-1")
    assert task is not None and task.status == "cancelled"
    assert "react.finished" in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_ensure_event_is_idempotent_for_react_finished() -> None:
    db = await _db("ensure-react-finished")
    rec = TaskRecorder(db, project="ensure-react-finished")
    async with db.session() as session:
        session.add(TaskORM(id="task-1", user_input="cancel me", status="running"))
        await session.commit()

    first = await rec.ensure_event(
        "task-1",
        event_type="react.finished",
        status="cancelled",
        name="react",
        output_json={"kind": "partial", "terminated_reason": "cancelled"},
        summary="react partial: cancelled",
    )
    second = await rec.ensure_event(
        "task-1",
        event_type="react.finished",
        status="cancelled",
        name="react",
        summary="react partial: cancelled",
    )
    assert first is not None and second is not None
    assert first.id == second.id
