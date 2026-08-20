"""Web cancel force-settles a turn whose worker is gone."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.config import trust as trustmod
from omni.runtime.daemon import pid_alive
from omni.storage.models import SubtaskORM, TaskControlORM, _utcnow
from omni.web.turns import steer_or_cancel
from omni.web.workspace import WorkspaceHub

pytest.importorskip("starlette")


def _dead_pid() -> int:
    for pid in (2_147_483_647, 2_000_000, 1_000_000, 999_991):
        if not pid_alive(pid):
            return pid
    raise AssertionError("could not find a dead pid")


async def _open(tmp_path: Path) -> tuple[OmniAgent, WorkspaceHub, object, str]:
    work = tmp_path / "repo"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    hub = WorkspaceHub()
    rec = await hub.open_path(work)
    session_id = await agent.ensure_session(channel="web")
    return agent, hub, rec, session_id


@pytest.mark.asyncio
async def test_cancel_force_settles_an_unowned_running_turn(tmp_path: Path) -> None:
    agent, hub, rec, session_id = await _open(tmp_path)
    try:
        task = await agent.tasks.create_task(
            session_id=session_id,
            channel="web",
            user_input="stale research turn",
        )
        first = await steer_or_cancel(
            agent, hub, rec, session_id=session_id, action="cancel", task_id=task.id
        )
        assert first["settled"] is True
        assert first["status"] == "cancelled"
        refreshed = await agent.tasks.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == "cancelled"
        async with agent.db.session() as session:
            controls = list(
                (
                    await session.execute(
                        select(TaskControlORM).where(TaskControlORM.task_id == task.id)
                    )
                ).scalars().all()
            )
        assert controls
        assert all(row.status in {"consumed", "applied"} for row in controls)
    finally:
        await hub.aclose()
        await agent.aclose()


@pytest.mark.asyncio
async def test_cancel_stays_cooperative_while_this_process_owns_the_turn(
    tmp_path: Path,
) -> None:
    agent, hub, rec, session_id = await _open(tmp_path)
    try:
        task = await agent.tasks.create_task(
            session_id=session_id,
            channel="web",
            user_input="live web turn",
        )
        handle = await hub.runs.admit(rec, session_id=session_id)
        hub.runs.bind(handle, session_id=session_id, task_id=task.id)
        result = await steer_or_cancel(
            agent, hub, rec, session_id=session_id, action="cancel", task_id=task.id
        )
        assert result["settled"] is False
        assert result["control_id"]
        refreshed = await agent.tasks.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == "running"
        hub.runs.finish(handle)
    finally:
        await hub.aclose()
        await agent.aclose()


@pytest.mark.asyncio
async def test_cancel_stays_cooperative_while_a_child_executor_is_alive(
    tmp_path: Path,
) -> None:
    agent, hub, rec, session_id = await _open(tmp_path)
    try:
        task = await agent.tasks.create_task(
            session_id=session_id,
            channel="cli",
            user_input="live child turn",
        )
        async with agent.db.session() as session:
            session.add(
                SubtaskORM(
                    session_id=session_id,
                    task_id=task.id,
                    skill_name="research-pptx",
                    status="running",
                    started_at=_utcnow(),
                    owner_pid=os.getpid(),
                )
            )
            await session.commit()
        result = await steer_or_cancel(
            agent, hub, rec, session_id=session_id, action="cancel", task_id=task.id
        )
        assert result["settled"] is False
        refreshed = await agent.tasks.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == "running"
    finally:
        await hub.aclose()
        await agent.aclose()


@pytest.mark.asyncio
async def test_cancel_settles_a_turn_whose_child_owner_is_dead(tmp_path: Path) -> None:
    agent, hub, rec, session_id = await _open(tmp_path)
    try:
        task = await agent.tasks.create_task(
            session_id=session_id,
            channel="cli",
            user_input="dead child turn",
        )
        async with agent.db.session() as session:
            session.add(
                SubtaskORM(
                    session_id=session_id,
                    task_id=task.id,
                    skill_name="research-pptx",
                    status="running",
                    started_at=_utcnow(),
                    owner_pid=_dead_pid(),
                )
            )
            await session.commit()
        result = await steer_or_cancel(
            agent, hub, rec, session_id=session_id, action="cancel", task_id=task.id
        )
        assert result["settled"] is True
        refreshed = await agent.tasks.get_task(task.id)
        assert refreshed is not None
        assert refreshed.status == "cancelled"
    finally:
        await hub.aclose()
        await agent.aclose()


@pytest.mark.asyncio
async def test_cancel_of_an_already_terminal_turn_unlocks(tmp_path: Path) -> None:
    agent, hub, rec, session_id = await _open(tmp_path)
    try:
        task = await agent.tasks.create_task(
            session_id=session_id,
            channel="web",
            user_input="already done",
        )
        await agent.tasks.finish_task(task.id, status="succeeded", summary="done")
        result = await steer_or_cancel(
            agent, hub, rec, session_id=session_id, action="cancel", task_id=task.id
        )
        assert result["settled"] is True
        assert result["status"] == "succeeded"
    finally:
        await hub.aclose()
        await agent.aclose()
