"""Parallel skill progress must not fail a sibling when SQLite is busy."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import OperationalError

from omni.config import load_settings
from omni.runtime.notifications import InboxNotifier
from omni.runtime.subtask_runtime import SubtaskRuntime
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database


@pytest.mark.asyncio
async def test_persist_trace_swallows_sqlite_busy() -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    runtime = SubtaskRuntime(
        db,
        settings,
        SkillRegistry(settings),
        lambda session_id, channel: ExecContext(
            settings=settings,
            paths=settings.paths,
            session_id=session_id,
            channel=channel,
        ),
        notifier=InboxNotifier(settings.paths.project_dir / "inbox.jsonl"),
    )
    attempts = {"n": 0}

    @asynccontextmanager
    async def always_busy():
        attempts["n"] += 1
        raise OperationalError("UPDATE", {}, Exception("database is locked"))
        yield  # pragma: no cover

    runtime._db.session = always_busy
    await runtime._persist_trace("sub-1", [{"stage": "skill.done"}])
    assert attempts["n"] == 3
