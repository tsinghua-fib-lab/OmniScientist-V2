"""Deleting a session also deletes the turns that belong to it."""

from __future__ import annotations

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings


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
