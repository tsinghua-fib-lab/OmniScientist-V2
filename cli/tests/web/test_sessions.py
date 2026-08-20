"""Session display_title, rename sort, and incremental events."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.config import trust as trustmod
from omni.web.app import create_app
from omni.web.protocol import utc_iso

pytest.importorskip("starlette")


async def _rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    res = await client.post(
        "/api",
        headers={"X-Omni-Web": "1"},
        json={"method": method, "params": params or {}},
    )
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.asyncio
async def test_display_title_uses_first_user_input(tmp_path: Path) -> None:
    work = tmp_path / "title-repo"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        sid = await agent.ensure_session(channel="web", title="")
        await agent.conversations.persist_message(
            sid, "user", "\u5e2e\u6211\u8c03\u7814\u9690\u7a7a\u95f4\u5e72\u9884"
        )
    finally:
        await agent.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        listed = await _rpc(client, "session.list", {"workspace": str(work)})
        row = next(item for item in listed["sessions"] if item["id"] == sid)
        assert row["title"] == ""
        assert "\u9690\u7a7a\u95f4" in row["display_title"]
        created = utc_iso(datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")))
        assert created is not None
        assert row["created_at"].endswith("Z")


@pytest.mark.asyncio
async def test_rename_does_not_reorder_by_updated_at(tmp_path: Path) -> None:
    work = tmp_path / "rename-repo"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        older = await agent.ensure_session(channel="web", title="")
        await agent.conversations.persist_message(older, "user", "older thread")
        newer = await agent.ensure_session(channel="web", title="")
        await agent.conversations.persist_message(newer, "user", "newer thread")
    finally:
        await agent.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        before = await _rpc(client, "session.list", {"workspace": str(work)})
        ids_before = [row["id"] for row in before["sessions"]]
        renamed = await _rpc(
            client,
            "session.rename",
            {"workspace": str(work), "session_id": older, "title": "renamed-old"},
        )
        assert renamed["ok"] is True
        assert renamed["session"]["title"] == "renamed-old"
        assert renamed["session"]["display_title"] == "renamed-old"
        after = await _rpc(client, "session.list", {"workspace": str(work)})
        ids_after = [row["id"] for row in after["sessions"]]
        assert ids_after == ids_before
        old_row = next(item for item in after["sessions"] if item["id"] == older)
        assert old_row["last_activity_at"] == next(
            item for item in before["sessions"] if item["id"] == older
        )["last_activity_at"]


@pytest.mark.asyncio
async def test_session_single_flight_via_api(tmp_path: Path) -> None:
    work = tmp_path / "busy-repo"
    work.mkdir()
    trustmod.set_trusted(work)
    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    hub = app.state.hub
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        created = await _rpc(client, "session.create", {"workspace": str(work)})
        sid = created["session"]["id"]
        rec = hub.selected()
        assert rec is not None
        live = await hub.runs.admit(rec, session_id=sid)
        busy = await _rpc(
            client,
            "turn.start",
            {"workspace": str(work), "session_id": sid, "text": "hello"},
        )
        assert busy["ok"] is False
        assert busy["error"]["code"] == "busy"
        hub.runs.finish(live)


@pytest.mark.asyncio
async def test_session_delete_removes_row_and_tasks(tmp_path: Path) -> None:
    work = tmp_path / "delete-repo"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        sid = await agent.ensure_session(channel="web", title="")
        await agent.conversations.persist_message(sid, "user", "throw away")
        task = await agent.tasks.create_task(
            session_id=sid, channel="web", user_input="throw away"
        )
        await agent.tasks.finish_task(task.id, status="cancelled", summary="done")
    finally:
        await agent.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        deleted = await _rpc(
            client, "session.delete", {"workspace": str(work), "session_id": sid}
        )
        assert deleted["ok"] is True
        assert deleted["session_id"] == sid
        assert task.id in deleted["deleted_task_ids"]
        listed = await _rpc(client, "session.list", {"workspace": str(work)})
        assert sid not in {row["id"] for row in listed["sessions"]}


@pytest.mark.asyncio
async def test_session_delete_refuses_live_web_run(tmp_path: Path) -> None:
    work = tmp_path / "busy-delete"
    work.mkdir()
    trustmod.set_trusted(work)
    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    hub = app.state.hub
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        created = await _rpc(client, "session.create", {"workspace": str(work)})
        sid = created["session"]["id"]
        rec = hub.selected()
        assert rec is not None
        live = await hub.runs.admit(rec, session_id=sid)
        blocked = await _rpc(
            client, "session.delete", {"workspace": str(work), "session_id": sid}
        )
        assert blocked["ok"] is False
        assert blocked["error"]["code"] == "busy"
        hub.runs.finish(live)
        gone = await _rpc(
            client, "session.delete", {"workspace": str(work), "session_id": sid}
        )
        assert gone["ok"] is True


@pytest.mark.asyncio
async def test_external_active_task_is_reported_as_background_not_lost(
    tmp_path: Path,
) -> None:
    work = tmp_path / "external-active"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        sid = await agent.ensure_session(channel="wechat", title="wechat thread")
        await agent.conversations.persist_message(sid, "user", "continue in wechat")
        task = await agent.tasks.create_task(
            session_id=sid,
            channel="wechat",
            user_input="continue in wechat",
        )
    finally:
        await agent.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        listed = await _rpc(client, "session.list", {"workspace": str(work)})
        row = next(item for item in listed["sessions"] if item["id"] == sid)
        assert row["latest_task_id"] == task.id
        assert row["latest_task_status"] == "running"
        assert row["worker"] == "external"


@pytest.mark.asyncio
async def test_interrupted_task_is_not_reported_as_external(tmp_path: Path) -> None:
    work = tmp_path / "external-interrupted"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        sid = await agent.ensure_session(channel="wechat", title="interrupted thread")
        task = await agent.tasks.create_task(
            session_id=sid,
            channel="wechat",
            user_input="interrupted work",
        )
        await agent.tasks.finish_task(task.id, status="interrupted", summary="owner stopped")
    finally:
        await agent.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        listed = await _rpc(client, "session.list", {"workspace": str(work)})
        row = next(item for item in listed["sessions"] if item["id"] == sid)
        assert row["worker"] == "interrupted"


@pytest.mark.asyncio
async def test_workspace_inbox_reports_focus_fingerprint(tmp_path: Path) -> None:
    work = tmp_path / "inbox-repo"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        sid = await agent.ensure_session(channel="wechat", title="")
        await agent.conversations.persist_message(sid, "user", "inbox from wechat")
        task = await agent.tasks.create_task(
            session_id=sid,
            channel="wechat",
            user_input="inbox from wechat",
        )
        await agent.tasks.append_event(
            task.id,
            event_type="step.progress",
            status="running",
            name="research",
            summary="started",
        )
    finally:
        await agent.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        inbox = await _rpc(
            client,
            "workspace.inbox",
            {"workspace": str(work), "session_id": sid},
        )
        assert inbox["ok"] is True
        row = next(item for item in inbox["sessions"] if item["id"] == sid)
        assert row["worker"] == "external"
        assert "inbox from wechat" in row["display_title"]
        focus = inbox["focus"]
        assert focus["session_id"] == sid
        assert focus["message_count"] == 1
        assert focus["last_message_id"]
        assert focus["latest_task_id"] == task.id
        assert focus["latest_task_status"] == "running"
        assert focus["latest_event_seq"] >= 1


@pytest.mark.asyncio
async def test_session_timeline_includes_compact_executions(tmp_path: Path) -> None:
    from omni.storage.artifacts import ArtifactStore
    from omni.storage.models import SubtaskORM

    work = tmp_path / "timeline-repo"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        sid = await agent.ensure_session(channel="cli", title="timeline")
        await agent.conversations.persist_message(sid, "user", "write the report")
        task = await agent.tasks.create_task(
            session_id=sid,
            channel="cli",
            user_input="write the report",
        )
        execution_id = "execution-timeline-1"
        async with agent.db.session() as session:
            session.add(
                SubtaskORM(
                    id=execution_id,
                    session_id=sid,
                    task_id=task.id,
                    skill_name="scientific-writing",
                    status="succeeded",
                    attempt=1,
                    step_attempt=1,
                )
            )
            await session.commit()
        store = ArtifactStore(settings.paths, agent.db)
        await store.put_bytes(
            b"# Report",
            kind="document",
            title="report",
            ext="md",
            mime="text/markdown",
            session_id=sid,
            task_id=task.id,
            subtask_id=execution_id,
        )
        await agent.conversations.persist_message(sid, "assistant", "report ready")
        await agent.tasks.finish_task(task.id, status="succeeded", summary="done")
    finally:
        await agent.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        timeline = await _rpc(
            client,
            "session.timeline",
            {"workspace": str(work), "session_id": sid},
        )
        assert timeline["ok"] is True
        assert timeline["followable"] is False
        assert [row["role"] for row in timeline["messages"]] == ["user", "assistant"]
        assert timeline["turns"][0]["id"] == task.id
        assert timeline["turns"][0]["user_input"] == "write the report"
        execution = timeline["turns"][0]["executions"][0]
        assert execution["id"] == execution_id
        assert execution["skill_name"] == "scientific-writing"
        assert execution["artifact_count"] == 1
        assert "input_json" not in execution
        assert timeline["fingerprint"]["latest_task_id"] == task.id
        assert timeline["fingerprint"]["message_count"] == 2
