"""Deleting a session also deletes the turns that belong to it."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.storage.db import get_database
from omni.storage.models import (
    ActionCheckpointORM,
    ArtifactORM,
    ComputeJobORM,
    ScheduleActionProposalORM,
    ScheduleORM,
    SessionORM,
)


@pytest.mark.asyncio
async def test_delete_session_removes_associated_tasks():
    agent = await OmniAgent.create(load_settings())
    try:
        sid = await agent.ensure_session(channel="web")
        await agent.conversations.persist_message(sid, "user", "summarize this")
        task = await agent.tasks.create_task(
            session_id=sid, channel="web", user_input="summarize this"
        )
        await agent.tasks.finish_task(task.id, status="cancelled", summary="stopped")
        other = await agent.ensure_session(channel="web")
        kept = await agent.tasks.create_task(
            session_id=other, channel="web", user_input="keep me"
        )
        await agent.tasks.finish_task(kept.id, status="cancelled", summary="kept")

        outcome = await agent.delete_session(sid[:8])
        assert outcome.deleted is True
        assert outcome.session_id == sid
        assert task.id in outcome.deleted_task_ids
        assert await agent.get_session(sid) is None
        assert await agent.tasks.get_task(task.id) is None
        assert await agent.get_session(other) is not None
        assert await agent.tasks.get_task(kept.id) is not None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_delete_session_refuses_running_task():
    agent = await OmniAgent.create(load_settings())
    try:
        sid = await agent.ensure_session(channel="cli")
        task = await agent.tasks.create_task(
            session_id=sid, channel="cli", user_input="still running"
        )
        outcome = await agent.delete_session(sid)
        assert outcome.deleted is False
        assert outcome.code == "busy"
        assert await agent.get_session(sid) is not None
        assert await agent.tasks.get_task(task.id) is not None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_delete_missing_session_is_not_found():
    agent = await OmniAgent.create(load_settings())
    try:
        outcome = await agent.delete_session("does-not-exist")
        assert outcome.deleted is False
        assert outcome.code == "not_found"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_delete_sessions_rejects_an_ambiguous_prefix():
    agent = await OmniAgent.create(load_settings())
    try:
        async with agent.db.session() as session:
            session.add_all(
                [
                    SessionORM(id="shared-prefix-alpha", channel="web"),
                    SessionORM(id="shared-prefix-beta", channel="web"),
                ]
            )
            await session.commit()

        outcome = await agent.delete_sessions(["shared-prefix"])

        assert outcome.deleted is False
        assert outcome.code == "ambiguous"
        assert outcome.ambiguous_session_ids == ("shared-prefix",)
        assert await agent.get_session("shared-prefix") is None
        assert await agent.get_session("shared-prefix-alpha") is not None
        assert await agent.get_session("shared-prefix-beta") is not None
    finally:
        await agent.aclose()


async def _cancelled_task(agent: OmniAgent, session_id: str, user_input: str):
    task = await agent.tasks.create_task(
        session_id=session_id,
        channel="web",
        user_input=user_input,
    )
    await agent.tasks.finish_task(task.id, status="cancelled", summary="stopped")
    return task


@pytest.mark.asyncio
async def test_delete_sessions_removes_batch_atomically_and_preserves_artifacts():
    agent = await OmniAgent.create(load_settings())
    try:
        first = await agent.ensure_session(channel="web")
        second = await agent.ensure_session(channel="web")
        kept = await agent.ensure_session(channel="web")
        await agent.conversations.persist_message(first, "user", "delete one")
        await agent.conversations.persist_message(second, "user", "delete two")
        first_task = await _cancelled_task(agent, first, "delete one")
        second_task = await _cancelled_task(agent, second, "delete two")
        kept_task = await _cancelled_task(agent, kept, "keep me")
        artifact_path = agent.paths.artifacts_dir / "kept-paper.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("deliverable", encoding="utf-8")
        artifact = ArtifactORM(
            session_id=first,
            task_id=first_task.id,
            title="Kept paper",
            uri=str(artifact_path),
            rel_path=artifact_path.name,
        )
        async with agent.db.session() as session:
            session.add(artifact)
            await session.commit()
            artifact_id = artifact.id

        outcome = await agent.delete_sessions([first, second])

        assert outcome.deleted_session_ids == (first, second)
        assert set(outcome.deleted_task_ids) == {first_task.id, second_task.id}
        assert outcome.retained_artifact_count == 1
        assert outcome.code == ""
        assert await agent.get_session(first) is None
        assert await agent.get_session(second) is None
        assert await agent.tasks.get_task(first_task.id) is None
        assert await agent.tasks.get_task(second_task.id) is None
        assert await agent.get_session(kept) is not None
        assert await agent.tasks.get_task(kept_task.id) is not None
        assert await agent._task_index.resolve(first_task.id) is None
        async with agent.db.session() as session:
            retained = await session.get(ArtifactORM, artifact_id)
        assert retained is not None
        assert retained.task_id is None
        assert artifact_path.read_text(encoding="utf-8") == "deliverable"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_delete_sessions_busy_member_rolls_back_entire_batch():
    agent = await OmniAgent.create(load_settings())
    try:
        settled = await agent.ensure_session(channel="web")
        busy = await agent.ensure_session(channel="web")
        settled_task = await _cancelled_task(agent, settled, "settled")
        busy_task = await agent.tasks.create_task(
            session_id=busy,
            channel="web",
            user_input="still running",
        )

        outcome = await agent.delete_sessions([settled, busy])

        assert outcome.deleted_session_ids == ()
        assert outcome.deleted_task_ids == ()
        assert outcome.code == "busy"
        assert dict(outcome.blocked_tasks) == {busy_task.id: "running"}
        assert await agent.get_session(settled) is not None
        assert await agent.get_session(busy) is not None
        assert await agent.tasks.get_task(settled_task.id) is not None
        assert await agent.tasks.get_task(busy_task.id) is not None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_delete_sessions_missing_member_rolls_back_entire_batch():
    agent = await OmniAgent.create(load_settings())
    try:
        session_id = await agent.ensure_session(channel="web")
        task = await _cancelled_task(agent, session_id, "keep on missing peer")

        outcome = await agent.delete_sessions([session_id, "does-not-exist"])

        assert outcome.deleted_session_ids == ()
        assert outcome.deleted_task_ids == ()
        assert outcome.code == "not_found"
        assert outcome.missing_session_ids == ("does-not-exist",)
        assert await agent.get_session(session_id) is not None
        assert await agent.tasks.get_task(task.id) is not None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_delete_sessions_database_failure_rolls_back_tasks_and_artifact_detach():
    agent = await OmniAgent.create(load_settings())
    try:
        session_id = await agent.ensure_session(channel="web")
        task = await _cancelled_task(agent, session_id, "rollback me")
        artifact = ArtifactORM(
            session_id=session_id,
            task_id=task.id,
            title="Rollback artifact",
            uri="artifact://rollback",
        )
        async with agent.db.session() as session:
            session.add(artifact)
            await session.flush()
            artifact_id = artifact.id
            await session.execute(
                text(
                    "CREATE TRIGGER abort_test_session_delete "
                    "BEFORE DELETE ON sessions "
                    f"WHEN OLD.id = '{session_id}' "
                    "BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
                )
            )
            await session.commit()

        with pytest.raises(SQLAlchemyError, match="test rollback"):
            await agent.delete_sessions([session_id])

        assert await agent.get_session(session_id) is not None
        assert await agent.tasks.get_task(task.id) is not None
        assert await agent._task_index.resolve(task.id) is not None
        async with agent.db.session() as session:
            retained = (
                await session.execute(
                    select(ArtifactORM).where(ArtifactORM.id == artifact_id)
                )
            ).scalar_one()
        assert retained.task_id == task.id
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_delete_sessions_blocks_live_durable_dependencies_as_one_batch():
    agent = await OmniAgent.create(load_settings())
    try:
        sessions = [await agent.ensure_session(channel="web") for _ in range(4)]
        tasks = [
            await _cancelled_task(agent, session_id, f"settled {index}")
            for index, session_id in enumerate(sessions)
        ]
        async with agent.db.session() as session:
            session.add(
                ScheduleORM(
                    id="enabled-schedule",
                    session_id=sessions[0],
                    title="still enabled",
                    enabled=True,
                )
            )
            session.add(
                ActionCheckpointORM(
                    id="open-checkpoint",
                    session_id=sessions[1],
                    task_id=tasks[1].id,
                    state="open",
                )
            )
            session.add(
                ComputeJobORM(
                    id="running-compute",
                    session_id=sessions[2],
                    task_id=tasks[2].id,
                    status="running",
                )
            )
            await session.commit()
        control_db = get_database(agent.paths.control_db)
        await control_db.init()
        async with control_db.session() as session:
            session.add(
                ScheduleActionProposalORM(
                    id="pending-proposal",
                    session_id=sessions[3],
                    origin_project_dir=str(agent.paths.project_dir),
                    state="pending",
                )
            )
            await session.commit()

        outcome = await agent.delete_sessions(sessions)

        assert outcome.deleted_session_ids == ()
        assert outcome.code == "busy"
        assert {
            (item.kind, item.object_id, item.status, item.session_id)
            for item in outcome.blocked_dependencies
        } == {
            ("schedule", "enabled-schedule", "enabled", sessions[0]),
            ("action_checkpoint", "open-checkpoint", "open", sessions[1]),
            ("compute_job", "running-compute", "running", sessions[2]),
            ("schedule_action_proposal", "pending-proposal", "pending", sessions[3]),
        }
        for session_id, task in zip(sessions, tasks, strict=True):
            assert await agent.get_session(session_id) is not None
            assert await agent.tasks.get_task(task.id) is not None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_delete_sessions_blocks_disabled_schedule_reference():
    agent = await OmniAgent.create(load_settings())
    try:
        session_id = await agent.ensure_session(channel="web")
        task = await _cancelled_task(agent, session_id, "disabled schedule owns me")
        async with agent.db.session() as session:
            session.add(
                ScheduleORM(
                    id="disabled-schedule",
                    session_id=session_id,
                    title="disabled but still references the session",
                    enabled=False,
                )
            )
            await session.commit()

        outcome = await agent.delete_sessions([session_id])

        assert outcome.deleted is False
        assert outcome.code == "busy"
        assert [
            (item.kind, item.object_id, item.status, item.session_id)
            for item in outcome.blocked_dependencies
        ] == [("schedule", "disabled-schedule", "disabled", session_id)]
        assert await agent.get_session(session_id) is not None
        assert await agent.tasks.get_task(task.id) is not None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_delete_sessions_rejects_task_tree_owned_by_unselected_session():
    agent = await OmniAgent.create(load_settings())
    try:
        selected = await agent.ensure_session(channel="web")
        unselected = await agent.ensure_session(channel="web")
        parent = await _cancelled_task(agent, selected, "selected parent")
        child = await agent.tasks.create_task(
            session_id=unselected,
            channel="web",
            user_input="foreign child",
            parent_task_id=parent.id,
            kind="subagent",
            depth=1,
        )
        await agent.tasks.finish_task(child.id, status="cancelled", summary="stopped")

        outcome = await agent.delete_sessions([selected])

        assert outcome.deleted is False
        assert outcome.code == "conflict"
        assert outcome.blocked_tasks == ((child.id, "cancelled"),)
        assert await agent.get_session(selected) is not None
        assert await agent.get_session(unselected) is not None
        assert await agent.tasks.get_task(parent.id) is not None
        assert await agent.tasks.get_task(child.id) is not None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_delete_sessions_allows_terminal_dependencies():
    agent = await OmniAgent.create(load_settings())
    try:
        session_id = await agent.ensure_session(channel="web")
        task = await _cancelled_task(agent, session_id, "settled dependencies")
        async with agent.db.session() as session:
            session.add(
                ActionCheckpointORM(
                    id="resolved-checkpoint",
                    session_id=session_id,
                    task_id=task.id,
                    state="resolved",
                )
            )
            session.add(
                ComputeJobORM(
                    id="finished-compute",
                    session_id=session_id,
                    task_id=task.id,
                    status="succeeded",
                )
            )
            await session.commit()
        control_db = get_database(agent.paths.control_db)
        await control_db.init()
        async with control_db.session() as session:
            session.add(
                ScheduleActionProposalORM(
                    id="denied-proposal",
                    session_id=session_id,
                    origin_project_dir=str(agent.paths.project_dir),
                    state="denied",
                )
            )
            await session.commit()

        outcome = await agent.delete_sessions([session_id])

        assert outcome.deleted is True
        assert outcome.blocked_dependencies == ()
        assert await agent.get_session(session_id) is None
        assert await agent.tasks.get_task(task.id) is None
    finally:
        await agent.aclose()
