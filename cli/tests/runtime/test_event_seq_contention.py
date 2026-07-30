"""Two events appended at once must both survive.

``(task_id, seq)`` is unique and the number comes from reading the current max,
so concurrent appends under one task choose the same one and an insert loses. A
skill reporting progress from a worker pool does this routinely, and the loser
used to surface as a raw IntegrityError traceback in the middle of a live turn.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import OperationalError

from omni.agent import OmniAgent
from omni.config import load_settings


@pytest.mark.asyncio
async def test_progress_appended_from_several_workers_keeps_every_event() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(
        session_id="", channel="cli", user_input="ideate with a worker pool"
    )
    try:
        await asyncio.gather(
            *[
                agent.tasks.append_event(
                    run.id,
                    event_type="subtask.progress",
                    status="running",
                    name=f"stage-{index}",
                    pct=index / 20,
                )
                for index in range(20)
            ]
        )
        events = await agent.tasks.list_events(run.id)
    finally:
        await agent.aclose()

    progress = [e for e in events if e.event_type == "subtask.progress"]
    assert len(progress) == 20
    sequences = [e.seq for e in progress]
    assert len(set(sequences)) == len(sequences), "two events shared one seq"


@pytest.mark.asyncio
async def test_append_event_retries_when_the_store_is_busy() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(
        session_id="", channel="cli", user_input="cancel me"
    )
    real_session = agent.tasks._db.session
    attempts = {"n": 0}

    @asynccontextmanager
    async def busy_once():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OperationalError(
                "INSERT task_events", {}, Exception("database is locked")
            )
        async with real_session() as session:
            yield session

    try:
        agent.tasks._db.session = busy_once
        event = await agent.tasks.append_event(
            run.id,
            event_type="react.tool.failed",
            status="cancelled",
            name="run_skill",
            error="Tool execution was cancelled by the user.",
        )
        assert event is not None
        assert event.event_type == "react.tool.failed"
        assert attempts["n"] == 2
    finally:
        agent.tasks._db.session = real_session
        await agent.aclose()


@pytest.mark.asyncio
async def test_append_event_survives_a_windows_length_busy_queue() -> None:
    """Five 10ms retries lose when a cancelled cli_exec still holds SQLite."""
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(
        session_id="", channel="cli", user_input="cancel me"
    )
    real_session = agent.tasks._db.session
    attempts = {"n": 0}

    @asynccontextmanager
    async def busy_six_times():
        attempts["n"] += 1
        if attempts["n"] <= 6:
            raise OperationalError(
                "INSERT task_events", {}, Exception("database is locked")
            )
        async with real_session() as session:
            yield session

    try:
        agent.tasks._db.session = busy_six_times
        event = await agent.tasks.append_event(
            run.id,
            event_type="react.tool.failed",
            status="cancelled",
            name="run_workflow",
            error="Tool execution was cancelled by the user.",
        )
        assert event is not None
        assert attempts["n"] == 7
    finally:
        agent.tasks._db.session = real_session
        await agent.aclose()


@pytest.mark.asyncio
async def test_append_event_does_not_retry_a_non_busy_operational_error() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(
        session_id="", channel="cli", user_input="cancel me"
    )
    real_session = agent.tasks._db.session
    attempts = {"n": 0}

    @asynccontextmanager
    async def broken():
        attempts["n"] += 1
        raise OperationalError("INSERT task_events", {}, Exception("no such column: x"))
        yield  # pragma: no cover

    try:
        agent.tasks._db.session = broken
        with pytest.raises(OperationalError, match="no such column"):
            await agent.tasks.append_event(
                run.id,
                event_type="react.tool.failed",
                status="cancelled",
                name="run_skill",
            )
        assert attempts["n"] == 1
    finally:
        agent.tasks._db.session = real_session
        await agent.aclose()
