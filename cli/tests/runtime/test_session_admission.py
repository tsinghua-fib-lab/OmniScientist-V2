"""Cross-process-safe admission for turns that belong to deletable Sessions."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.storage.models import ConversationMessageORM, SessionORM, TaskORM


@pytest.mark.asyncio
async def test_task_and_message_writes_reject_a_deleted_session() -> None:
    settings = load_settings()
    deleting_agent = await OmniAgent.create(settings)
    writing_agent = await OmniAgent.create(settings)
    try:
        session_id = await deleting_agent.ensure_session(channel="cli")
        outcome = await deleting_agent.delete_sessions([session_id])
        assert outcome.deleted is True

        with pytest.raises(LookupError, match="session not found"):
            await writing_agent.tasks.create_task(
                session_id=session_id,
                channel="cli",
                user_input="must not become an orphan task",
                require_session=True,
            )
        await writing_agent.conversations.persist_message(
            session_id,
            "user",
            "must not become an orphan message",
        )

        async with writing_agent.db.session() as db_session:
            task_count = int(
                (
                    await db_session.execute(
                        select(func.count(TaskORM.id)).where(
                            TaskORM.session_id == session_id
                        )
                    )
                ).scalar_one()
            )
            message_count = int(
                (
                    await db_session.execute(
                        select(func.count(ConversationMessageORM.id)).where(
                            ConversationMessageORM.session_id == session_id
                        )
                    )
                ).scalar_one()
            )
        assert task_count == 0
        assert message_count == 0
    finally:
        await writing_agent.aclose()
        await deleting_agent.aclose()


@pytest.mark.asyncio
async def test_task_admission_writer_reservation_blocks_session_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings()
    admitting_agent = await OmniAgent.create(settings)
    deleting_agent = await OmniAgent.create(settings)
    release_admission = asyncio.Event()
    admission_holds_writer = asyncio.Event()
    original_get = AsyncSession.get
    paused = False

    async def pause_after_session_validation(
        db_session: AsyncSession,
        entity: object,
        ident: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal paused
        row = await original_get(db_session, entity, ident, *args, **kwargs)
        if entity is SessionORM and ident == session_id and not paused:
            paused = True
            admission_holds_writer.set()
            await release_admission.wait()
        return row

    try:
        session_id = await admitting_agent.ensure_session(channel="cli")
        monkeypatch.setattr(AsyncSession, "get", pause_after_session_validation)
        admission = asyncio.create_task(
            admitting_agent.tasks.create_task(
                session_id=session_id,
                channel="cli",
                external_key="race-test",
                user_input="admit while another process attempts deletion",
                require_session=True,
            )
        )
        await asyncio.wait_for(admission_holds_writer.wait(), timeout=1.0)

        outcome = await deleting_agent.delete_sessions([session_id])
        assert outcome.deleted is False
        assert outcome.code == "concurrent_write"

        release_admission.set()
        task = await asyncio.wait_for(admission, timeout=2.0)
        assert task.status == "running"
        assert await deleting_agent.get_session(session_id) is not None
        assert await deleting_agent.tasks.get_task(task.id) is not None
    finally:
        release_admission.set()
        await deleting_agent.aclose()
        await admitting_agent.aclose()


@pytest.mark.asyncio
async def test_task_resume_rejects_a_deleted_owning_session() -> None:
    settings = load_settings()
    first_agent = await OmniAgent.create(settings)
    second_agent = await OmniAgent.create(settings)
    try:
        session_id = await first_agent.ensure_session(channel="cli")
        task = await first_agent.tasks.create_task(
            session_id=session_id,
            channel="cli",
            user_input="pause then delete",
        )
        await first_agent.tasks.finish_task(task.id, status="cancelled", summary="paused")
        stale_task_id = task.id

        outcome = await second_agent.delete_sessions([session_id])
        assert outcome.deleted is True
        assert await first_agent.tasks.mark_running(stale_task_id) is False
    finally:
        await second_agent.aclose()
        await first_agent.aclose()
