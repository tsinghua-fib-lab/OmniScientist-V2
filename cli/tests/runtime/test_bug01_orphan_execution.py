"""BUG-01: a lost foreground executor must not leave a skill execution stuck.

After the in-process foreground executor dies, a standalone skill execution used
to stay ``running``. Cancel stayed ``pending`` (nobody consumed it), and
retry/resume/requeue refused the active status. Codex treats process death as
the end of that turn and lets resume start from a terminal record; Omni keeps
durable SQLite rows, so the same idea is a PID-aware reconcile to
``interrupted`` / ``cancelled`` plus idempotent recovery verbs.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import update

from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.runtime.daemon import pid_alive
from omni.runtime.execution_ownership import execution_owner_lost
from omni.runtime.task_object_resolver import TaskObjectResolution
from omni.runtime.task_recovery import TaskRecoveryCoordinator
from omni.storage.models import SubtaskORM, TaskORM, _utcnow


def test_owner_lost_rules() -> None:
    now = _utcnow()
    assert execution_owner_lost(_dead_pid(), now, now, stale_after_s=1800, explicit=False)
    assert not execution_owner_lost(os.getpid(), now, now, stale_after_s=1800, explicit=True)
    assert not execution_owner_lost(0, now, now, stale_after_s=1800, explicit=False)
    assert execution_owner_lost(0, now, now, stale_after_s=1800, explicit=True)
    from datetime import timedelta

    old = now - timedelta(hours=2)
    assert execution_owner_lost(0, old, old, stale_after_s=1800, explicit=False)


def _dead_pid() -> int:
    for pid in (2_147_483_647, 2_000_000, 1_000_000, 999_991):
        if not pid_alive(pid):
            return pid
    raise AssertionError("could not find a dead pid for the orphan fixture")


def _resolution(kind: str, object_id: str, task_id: str) -> TaskObjectResolution:
    return TaskObjectResolution(
        status="ok",
        object_kind=kind,  # type: ignore[arg-type]
        object_id=object_id,
        task_id=task_id,
        settings=load_settings(),
    )


async def _orphan_foreground(
    agent: OmniAgent,
    *,
    owner_pid: int,
    plan_in_flight: bool = True,
) -> tuple[str, str]:
    """Leave a standalone execution ``running`` under a dead (or live) owner."""
    session_id = await agent.ensure_session(channel="cli")
    task = await agent.tasks.create_task(
        session_id=session_id,
        channel="cli",
        user_input="run a long standalone skill",
        title="orphan foreground",
    )
    execution_id = await agent.runtime.enqueue(
        "orphan-skill",
        {"q": "stuck"},
        "",
        session_id=session_id,
        task_id=task.id,
    )
    async with agent.db.session() as session:
        row = await session.get(TaskORM, task.id)
        assert row is not None
        if plan_in_flight:
            # A planned turn with no turn-end event is the realistic crash:
            # refresh_from_executions used to refuse to settle the parent.
            row.plan_json = {"intent": "single_skill_task"}
        execution = await session.get(SubtaskORM, execution_id)
        assert execution is not None
        execution.status = "running"
        execution.started_at = _utcnow()
        execution.owner_pid = owner_pid
        await session.commit()
    return task.id, execution_id


@pytest.mark.asyncio
async def test_retry_recovers_orphan_even_after_cancel_was_left_pending() -> None:
    """The user-facing recovery verbs must settle a lost executor themselves."""
    agent = await OmniAgent.create(load_settings())
    try:
        task_id, execution_id = await _orphan_foreground(agent, owner_pid=_dead_pid())
        control = await agent.tasks.request_control(task_id, action="cancel")
        assert control.status == "pending"

        retry = await TaskRecoveryCoordinator(agent).retry(
            _resolution("skill_execution", execution_id, task_id)
        )
        assert retry.status == "ok", retry.message
        assert await agent.tasks.control_status(control.id) == "applied"
        child = await agent.runtime.get_subtask(retry.new_id)
        assert child is not None and child.retry_of == execution_id
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_cancel_applies_when_foreground_executor_is_gone() -> None:
    agent = await OmniAgent.create(load_settings())
    try:
        task_id, execution_id = await _orphan_foreground(agent, owner_pid=_dead_pid())
        control = await agent.tasks.request_control(task_id, action="cancel")
        report = await agent.runtime.reconcile_lost_executors(
            task_id=task_id, explicit=True
        )
        assert execution_id in report.cancelled_ids
        assert control.id in report.applied_control_ids
        assert await agent.tasks.control_status(control.id) == "applied"

        execution = await agent.runtime.get_subtask(execution_id)
        task = await agent.tasks.get_task(task_id)
        assert execution is not None and execution.status == "cancelled"
        assert execution.owner_pid == 0
        assert task is not None and task.status == "cancelled"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_housekeep_interrupts_legacy_stale_execution_without_owner_pid() -> None:
    from datetime import timedelta

    agent = await OmniAgent.create(load_settings())
    try:
        task_id, execution_id = await _orphan_foreground(agent, owner_pid=0)
        old = _utcnow() - timedelta(hours=2)
        async with agent.db.session() as session:
            execution = await session.get(SubtaskORM, execution_id)
            assert execution is not None
            execution.started_at = old
            execution.created_at = old
            await session.commit()
        await agent.runtime.housekeep()
        execution = await agent.runtime.get_subtask(execution_id)
        task = await agent.tasks.get_task(task_id)
        assert execution is not None and execution.status == "interrupted"
        assert task is not None and task.status == "interrupted"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_housekeep_interrupts_orphan_and_unlocks_retry() -> None:
    agent = await OmniAgent.create(load_settings())
    try:
        task_id, execution_id = await _orphan_foreground(agent, owner_pid=_dead_pid())
        await agent.runtime.housekeep()

        execution = await agent.runtime.get_subtask(execution_id)
        task = await agent.tasks.get_task(task_id)
        assert execution is not None and execution.status == "interrupted"
        assert task is not None and task.status == "interrupted"

        outcome = await TaskRecoveryCoordinator(agent).retry(
            _resolution("skill_execution", execution_id, task_id)
        )
        assert outcome.status == "ok", outcome.message
        assert outcome.new_id
        child = await agent.runtime.get_subtask(outcome.new_id)
        assert child is not None
        assert child.retry_of == execution_id
        assert child.status in {"pending", "scheduled"}
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_retry_of_orphan_is_idempotent_and_requeue_works() -> None:
    agent = await OmniAgent.create(load_settings())
    try:
        task_id, execution_id = await _orphan_foreground(agent, owner_pid=_dead_pid())
        coordinator = TaskRecoveryCoordinator(agent)
        first = await coordinator.retry(_resolution("skill_execution", execution_id, task_id))
        second = await coordinator.retry(_resolution("skill_execution", execution_id, task_id))
        assert first.status == "ok", first.message
        assert second.status == "ok", second.message
        assert first.new_id == second.new_id

        other_task, other_exec = await _orphan_foreground(agent, owner_pid=_dead_pid())
        queued = await coordinator.requeue(_resolution("skill_execution", other_exec, other_task))
        assert queued.status == "ok", queued.message
        row = await agent.runtime.get_subtask(other_exec)
        assert row is not None
        assert row.status in {"recovering", "pending", "scheduled"}
        assert row.resume_of == other_exec
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_serve_recover_interrupts_dead_owner_and_leaves_consumed_cancel() -> None:
    """``omni update --local`` restarts serve; leftover cancel must not win."""
    agent = await OmniAgent.create(load_settings())
    try:
        task_id, execution_id = await _orphan_foreground(agent, owner_pid=_dead_pid())
        control = await agent.tasks.request_control(task_id, action="cancel")
        assert len(await agent.tasks.consume_controls(task_id, actions={"cancel"})) == 1
        assert await agent.tasks.control_status(control.id) == "consumed"

        n = await agent.runtime.recover()
        assert n >= 1
        execution = await agent.runtime.get_subtask(execution_id)
        task = await agent.tasks.get_task(task_id)
        assert execution is not None and execution.status == "interrupted"
        assert task is not None and task.status == "interrupted"
        assert await agent.tasks.control_status(control.id) == "consumed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_serve_recover_leaves_fresh_unstamped_execution_running() -> None:
    agent = await OmniAgent.create(load_settings())
    try:
        task_id, execution_id = await _orphan_foreground(agent, owner_pid=0)
        n = await agent.runtime.recover()
        execution = await agent.runtime.get_subtask(execution_id)
        task = await agent.tasks.get_task(task_id)
        assert execution is not None and execution.status == "running"
        assert task is not None and task.status == "running"
        assert n == 0 or execution.status == "running"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_live_owner_is_not_stolen_by_recover_or_retry() -> None:
    agent = await OmniAgent.create(load_settings())
    try:
        task_id, execution_id = await _orphan_foreground(
            agent, owner_pid=os.getpid(), plan_in_flight=False
        )
        n = await agent.runtime.recover()
        execution = await agent.runtime.get_subtask(execution_id)
        assert execution is not None and execution.status == "running"
        assert execution.owner_pid == os.getpid()
        assert n == 0 or execution.status == "running"

        control = await agent.tasks.request_control(task_id, action="cancel")
        report = await agent.runtime.reconcile_lost_executors(
            task_id=task_id, explicit=True
        )
        assert execution_id not in report.cancelled_ids
        assert control.id not in report.applied_control_ids
        assert await agent.tasks.control_status(control.id) == "pending"

        retry = await TaskRecoveryCoordinator(agent).retry(
            _resolution("skill_execution", execution_id, task_id)
        )
        assert retry.status == "wrong_state"
        assert "running" in retry.message
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_claim_records_owner_pid() -> None:
    agent = await OmniAgent.create(load_settings())
    try:
        session_id = await agent.ensure_session(channel="cli")
        execution_id = await agent.runtime.enqueue(
            "missing-on-purpose", {}, "", session_id=session_id
        )
        await agent.runtime.process(execution_id)
        row = await agent.runtime.get_subtask(execution_id)
        assert row is not None
        # Failed (unknown skill) releases the claim after the process stamps it.
        assert row.owner_pid == 0
        assert row.status == "failed"
        # Re-claim path: a fresh running row keeps the live pid until finish.
        fresh = await agent.runtime.enqueue(
            "missing-on-purpose", {}, "", session_id=session_id
        )
        async with agent.db.session() as session:
            await session.execute(
                update(SubtaskORM)
                .where(SubtaskORM.id == fresh)
                .values(status="pending")
            )
            await session.commit()
        claimed = await agent.runtime._claim(fresh)  # noqa: SLF001
        assert claimed is not None
        row = await agent.runtime.get_subtask(fresh)
        assert row is not None
        assert row.status == "running"
        assert row.owner_pid == os.getpid()
    finally:
        await agent.aclose()
