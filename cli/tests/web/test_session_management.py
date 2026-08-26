"""Global Session catalog and workspace-local batch deletion RPCs."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.config import trust as trustmod
from omni.web.app import create_app

pytest.importorskip("starlette")


async def _rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    response = await client.post(
        "/api",
        headers={"X-Omni-Web": "1"},
        json={"method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _seed_turn(work: Path, *, title: str, status: str) -> tuple[str, str, str]:
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="web", title=title)
        await agent.conversations.persist_message(session_id, "user", title)
        task = await agent.tasks.create_task(
            session_id=session_id,
            channel="web",
            user_input=title,
        )
        if status != "running":
            await agent.tasks.finish_task(task.id, status=status, summary=status)
        return str(settings.paths.project_dir), session_id, task.id
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_session_list_all_aggregates_and_filters_workspaces(tmp_path: Path) -> None:
    alpha_dir, alpha_session, _ = await _seed_turn(
        tmp_path / "alpha",
        title="alpha completed",
        status="succeeded",
    )
    _beta_dir, beta_session, _ = await _seed_turn(
        tmp_path / "beta",
        title="beta failed",
        status="failed",
    )

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        listed = await _rpc(client, "session.listAll", {"sort": "activity", "limit": 20})
        assert listed["ok"] is True, listed
        rows = {item["id"]: item for item in listed["sessions"]}
        assert rows[alpha_session]["status_group"] == "completed"
        assert rows[beta_session]["status_group"] == "error"
        assert rows[alpha_session]["project_dir"] == alpha_dir

        filtered = await _rpc(
            client,
            "session.listAll",
            {"status": ["error"], "limit": 20},
        )
        assert [item["id"] for item in filtered["sessions"]] == [beta_session]


@pytest.mark.asyncio
async def test_session_delete_many_is_atomic_for_live_web_turn(tmp_path: Path) -> None:
    work = tmp_path / "batch-delete"
    project_dir, first, _ = await _seed_turn(
        work,
        title="first",
        status="cancelled",
    )
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        second = await agent.ensure_session(channel="web", title="second")
        await agent.conversations.persist_message(second, "user", "second")
    finally:
        await agent.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    hub = app.state.hub
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        rec = hub.selected()
        assert rec is not None and rec.store_key == project_dir
        live = await hub.runs.admit(rec, session_id=second)

        blocked = await _rpc(
            client,
            "session.deleteMany",
            {"workspace": str(work), "session_ids": [first, second[:8]]},
        )
        assert blocked["ok"] is False
        assert blocked["error"]["code"] == "busy"
        remaining = await _rpc(client, "session.list", {"workspace": str(work)})
        assert {item["id"] for item in remaining["sessions"]} >= {first, second}

        hub.runs.finish(live)
        deleted = await _rpc(
            client,
            "session.deleteMany",
            {"workspace": str(work), "session_ids": [first, second]},
        )
        assert deleted["ok"] is True
        assert set(deleted["deleted_session_ids"]) == {first, second}
        assert deleted["retained_artifact_count"] == 0
        after = await _rpc(client, "session.list", {"workspace": str(work)})
        assert not ({first, second} & {item["id"] for item in after["sessions"]})
