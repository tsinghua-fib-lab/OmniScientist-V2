"""Web RunManager admission, unsubscribe, and shutdown."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.config import trust as trustmod
from omni.storage.models import TaskEventORM, TaskORM
from omni.web.app import create_app
from omni.web.protocol import RpcError, utc_iso
from omni.web.runs import RunHandle
from omni.web.turns import watch_task_sse
from omni.web.workspace import WorkspaceHub

pytest.importorskip("starlette")


@pytest.mark.asyncio
async def test_session_single_flight_and_unsubscribe_does_not_cancel(tmp_path: Path) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    trustmod.set_trusted(work)
    hub = WorkspaceHub()
    try:
        rec = await hub.open_path(work)
        first = await hub.runs.admit(rec, session_id="sess-a")
        first.task = asyncio.create_task(asyncio.sleep(30))
        with pytest.raises(RpcError) as caught:
            await hub.runs.admit(rec, session_id="sess-a")
        assert caught.value.code == "busy"
        queue = first.subscribe()
        first.unsubscribe(queue)
        assert not first.task.done()
        first.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first.task
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_capacity_reads_current_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    trustmod.set_trusted(work)
    hub = WorkspaceHub()
    try:
        rec = await hub.open_path(work)
        monkeypatch.setattr("omni.web.runs.max_inflight_turns", lambda _rec: 1)
        first = await hub.runs.admit(rec, session_id="s1")
        with pytest.raises(RpcError) as caught:
            await hub.runs.admit(rec, session_id="s2")
        assert caught.value.code == "capacity"
        hub.runs.finish(first)
        second = await hub.runs.admit(rec, session_id="s2")
        assert second.session_id == "s2"
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_task_handle_lookup_is_scoped_to_its_workspace(tmp_path: Path) -> None:
    first_root = tmp_path / "workspace-a"
    second_root = tmp_path / "workspace-b"
    first_root.mkdir()
    second_root.mkdir()
    trustmod.set_trusted(first_root)
    trustmod.set_trusted(second_root)
    hub = WorkspaceHub()
    try:
        first = await hub.open_path(first_root)
        second = await hub.open_path(second_root)
        handle = await hub.runs.admit(first, session_id="session-a")
        hub.runs.bind(handle, session_id="session-a", task_id="shared-task-id")

        assert hub.runs.by_task(first.key, "shared-task-id") is handle
        assert hub.runs.by_task(second.key, "shared-task-id") is None
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_unlimited_when_max_is_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    trustmod.set_trusted(work)
    hub = WorkspaceHub()
    try:
        rec = await hub.open_path(work)
        monkeypatch.setattr("omni.web.runs.max_inflight_turns", lambda _rec: 0)
        handles = [await hub.runs.admit(rec, session_id=f"s{i}") for i in range(12)]
        assert len(handles) == 12
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_shutdown_cancels_live_tasks(tmp_path: Path) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    trustmod.set_trusted(work)
    hub = WorkspaceHub()
    rec = await hub.open_path(work)
    handle = await hub.runs.admit(rec, session_id="s1")
    handle.task = asyncio.create_task(asyncio.sleep(60))
    await hub.aclose()
    assert handle.task.done()


def test_utc_iso_stamps_naive_as_z() -> None:
    from datetime import datetime

    assert utc_iso(datetime(2026, 8, 19, 3, 10, 0)).endswith("Z")
    assert utc_iso("2026-08-19T03:10:00") == "2026-08-19T03:10:00Z"


def test_partial_is_bounded() -> None:
    handle = RunHandle(client_run_id="r", workspace_key="w", session_id="s")
    handle.append_partial("x" * 40_000)
    assert len(handle.partial) == 32_768


@pytest.mark.asyncio
async def test_live_watch_subscribes_before_yielding_partial_snapshot(
    tmp_path: Path,
) -> None:
    work = tmp_path / "partial-subscribe-race"
    work.mkdir()
    trustmod.set_trusted(work)
    hub = WorkspaceHub()
    try:
        rec = await hub.open_path(work)
        agent = await hub.agent_for(rec)
        session_id = await agent.ensure_session(channel="web", title="stream")
        task = await agent.tasks.create_task(
            session_id=session_id,
            channel="web",
            user_input="stream without a gap",
        )
        handle = await hub.runs.admit(rec, session_id=session_id)
        hub.runs.bind(handle, session_id=session_id, task_id=task.id)
        handle.append_partial("before-snapshot")

        response = await watch_task_sse(
            hub,
            rec,
            agent,
            task_id=task.id,
            after_seq=1_000_000,
        )
        iterator = response.body_iterator.__aiter__()
        chunks: list[bytes] = []
        first = await anext(iterator)
        chunks.append(first)
        assert b"event: partial" in first

        # A token produced while the partial snapshot is in flight must be
        # queued for this subscriber instead of falling into a subscribe gap.
        handle.publish("token", {"text": "after-snapshot"})
        chunks.append(await anext(iterator))  # worker ownership
        hub.runs.finish(handle)
        while True:
            try:
                chunks.append(await asyncio.wait_for(anext(iterator), timeout=1))
            except StopAsyncIteration:
                break

        body = b"".join(chunks)
        assert b"after-snapshot" in body
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_watch_external_task_polls_durable_events_until_terminal(
    tmp_path: Path,
) -> None:
    work = tmp_path / "external-watch"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    seed = await OmniAgent.create(settings)
    try:
        sid = await seed.ensure_session(channel="wechat", title="external")
        task = await seed.tasks.create_task(
            session_id=sid,
            channel="wechat",
            user_input="run outside web",
        )
    finally:
        await seed.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    hub = app.state.hub
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        opened = await client.post(
            "/api",
            headers={"X-Omni-Web": "1"},
            json={"method": "workspace.open", "params": {"path": str(work)}},
        )
        assert opened.json()["ok"] is True
        rec = hub.selected()
        assert rec is not None
        agent = await hub.agent_for(rec)

        watching = asyncio.create_task(
            client.post(
                "/api",
                headers={"X-Omni-Web": "1"},
                json={
                    "method": "task.watch",
                    "params": {
                        "workspace": str(work),
                        "task_id": task.id,
                        "after_seq": 0,
                    },
                },
            )
        )
        await asyncio.sleep(0.1)
        assert not watching.done(), "an active external task watch must stay attached"

        await agent.tasks.append_event(
            task.id,
            event_type="step.progress",
            status="running",
            name="external progress",
            summary="durable update from another process",
            pct=0.5,
        )
        # Cancellation is an unconditional terminal transition; unlike a
        # synthetic success it does not require constructing the full runtime
        # settlement contract in this watcher-focused fixture.
        await agent.tasks.finish_task(task.id, status="cancelled", summary="external done")
        watched = await asyncio.wait_for(watching, timeout=4)
        assert watched.status_code == 200
        body = watched.text
        assert '"state": "external"' in body
        assert "durable update from another process" in body
        assert '"settlement_status": "cancelled"' in body
        assert '"state": "lost"' not in body

    await hub.aclose()


@pytest.mark.parametrize("waiting_status", ["pending", "queued", "awaiting_approval"])
@pytest.mark.asyncio
async def test_watch_external_followable_task_waits_for_later_transition(
    tmp_path: Path,
    waiting_status: str,
) -> None:
    work = tmp_path / f"followable-{waiting_status}"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    seed = await OmniAgent.create(settings)
    try:
        sid = await seed.ensure_session(channel="wechat", title="waiting")
        task = await seed.tasks.create_task(
            session_id=sid,
            channel="wechat",
            user_input="wait for a later transition",
        )
        async with seed.db.session() as session:
            row = await session.get(TaskORM, task.id)
            assert row is not None
            row.status = waiting_status
            await session.commit()
    finally:
        await seed.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    hub = app.state.hub
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        opened = await client.post(
            "/api",
            headers={"X-Omni-Web": "1"},
            json={"method": "workspace.open", "params": {"path": str(work)}},
        )
        assert opened.json()["ok"] is True
        rec = hub.selected()
        assert rec is not None
        agent = await hub.agent_for(rec)
        watching = asyncio.create_task(
            client.post(
                "/api",
                headers={"X-Omni-Web": "1"},
                json={
                    "method": "task.watch",
                    "params": {
                        "workspace": str(work),
                        "task_id": task.id,
                        "after_seq": 0,
                    },
                },
            )
        )
        await asyncio.sleep(0.1)
        assert not watching.done(), f"{waiting_status} must remain followable"

        await agent.tasks.finish_task(task.id, status="cancelled", summary="stopped")
        watched = await asyncio.wait_for(watching, timeout=4)
        assert '"state": "external"' in watched.text
        assert '"settlement_status": "cancelled"' in watched.text

    await hub.aclose()


@pytest.mark.asyncio
async def test_watch_already_terminal_task_does_not_claim_external_worker(
    tmp_path: Path,
) -> None:
    work = tmp_path / "terminal-watch"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    seed = await OmniAgent.create(settings)
    try:
        sid = await seed.ensure_session(channel="wechat", title="terminal")
        task = await seed.tasks.create_task(
            session_id=sid,
            channel="wechat",
            user_input="already done",
        )
        async with seed.db.session() as session:
            current_seq = int(
                (
                    await session.execute(
                        select(func.max(TaskEventORM.seq)).where(
                            TaskEventORM.task_id == task.id
                        )
                    )
                ).scalar_one()
                or 0
            )
            last_seq = current_seq + 205
            session.add_all(
                [
                    TaskEventORM(
                        task_id=task.id,
                        seq=seq,
                        event_type="step.progress",
                        status="running",
                        name=f"durable event {seq}",
                        summary=f"durable event {seq}",
                    )
                    for seq in range(current_seq + 1, last_seq + 1)
                ]
            )
            await session.commit()
        await seed.tasks.finish_task(task.id, status="cancelled", summary="stopped")
    finally:
        await seed.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        opened = await client.post(
            "/api",
            headers={"X-Omni-Web": "1"},
            json={"method": "workspace.open", "params": {"path": str(work)}},
        )
        assert opened.json()["ok"] is True
        watched = await client.post(
            "/api",
            headers={"X-Omni-Web": "1"},
            json={
                "method": "task.watch",
                "params": {
                    "workspace": str(work),
                    "task_id": task.id,
                    "after_seq": 0,
                },
            },
        )
        assert watched.status_code == 200
        assert '"state": "cancelled"' in watched.text
        assert '"state": "external"' not in watched.text
        assert f"durable event {last_seq}" in watched.text
