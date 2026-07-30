"""Inbox merge (CWD ∪ channel anchor) + clarifications catalog (no principal=local)."""

from __future__ import annotations

import json

import pytest

from omni.cli.render import console
from omni.cli.state import AppState, make_agent, run_async
from omni.config.paths import get_paths
from omni.runtime.action_checkpoints import ActionCheckpointStore
from omni.runtime.aggregate import list_open_clarifications_all_workspaces
from omni.runtime.notifications import collect_inbox_notes
from omni.storage.db import get_database


def test_collect_inbox_notes_merges_channel_anchor(omni_home):

    local = get_paths(project="repo-ws")
    local.ensure_dirs()
    anchor = get_paths(project="default")
    anchor.ensure_dirs()

    (local.project_dir / "inbox.jsonl").write_text(
        json.dumps(
            {
                "task_id": "aaaaaaaa000000000000000000000001",
                "status": "succeeded",
                "summary": "local done",
                "created_at": "2026-07-29T10:00:00+00:00",
                "skill_name": "local-skill",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (anchor.project_dir / "inbox.jsonl").write_text(
        json.dumps(
            {
                "task_id": "bbbbbbbb000000000000000000000001",
                "status": "succeeded",
                "summary": "wechat done",
                "created_at": "2026-07-29T11:00:00+00:00",
                "skill_name": "wechat-skill",
                "channel": "wechat",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    notes = collect_inbox_notes(local)
    workspaces = {n["workspace"] for n in notes}
    assert "repo-ws" in workspaces
    assert "default" in workspaces
    ids = {n["task_id"] for n in notes}
    assert "aaaaaaaa000000000000000000000001" in ids
    assert "bbbbbbbb000000000000000000000001" in ids
    # Newest last (table shows the tail).
    assert notes[-1]["task_id"].startswith("bbbbbbbb")


def test_collect_inbox_notes_skips_duplicate_when_local_is_anchor(omni_home):

    anchor = get_paths(project="default")
    anchor.ensure_dirs()
    (anchor.project_dir / "inbox.jsonl").write_text(
        json.dumps(
            {
                "task_id": "cccccccc000000000000000000000001",
                "status": "succeeded",
                "created_at": "2026-07-29T10:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    notes = collect_inbox_notes(anchor)
    assert len(notes) == 1
    assert notes[0]["workspace"] == "default"


def test_render_inbox_shows_workspace_column(omni_home, monkeypatch):
    from omni.cli.commands import tasks_cmd

    paths = get_paths(project="view-ws")
    paths.ensure_dirs()

    notes = [
        {
            "task_id": "dddddddd000000000000000000000001",
            "object_kind": "skill_execution",
            "object_id": "",
            "subtask_id": "",
            "skill_name": "x",
            "status": "succeeded",
            "summary": "from default",
            "created_at": "2026-07-29T10:00:00+00:00",
            "workspace": "default",
            "_project_dir": str(omni_home / "projects" / "default"),
        }
    ]
    captured: dict = {}

    def _capture(title, headers, rows):  # noqa: ANN001
        captured["title"] = title
        captured["headers"] = headers
        captured["rows"] = rows

    monkeypatch.setattr(tasks_cmd, "data_table", _capture)
    tasks_cmd.render_inbox(
        paths, notes=notes, statuses={"dddddddd000000000000000000000001": "succeeded"}
    )
    assert captured["headers"][0] == "time"
    assert "workspace" in captured["headers"]
    assert captured["rows"][0][1] == "default"
    assert "dddddddd" in captured["rows"][0][2]


@pytest.mark.asyncio
async def test_list_open_clarifications_sees_wechat_decider_on_anchor(omni_home):

    # Seed a WeChat clarification on the unregistered channel anchor.
    anchor = get_paths(project="default")
    anchor.ensure_dirs()
    db = get_database(anchor.project_db)
    await db.init()
    store = ActionCheckpointStore(db)
    rec = await store.open_clarification(
        action_kind="schedule.create",
        contract_version="v1",
        policy_version="temporal-policy-v1",
        channel="wechat",
        session_id="wx-s1",
        actor_principal="wechat:user-1",
        required_decider="wechat:user-1",
        payload={"goal": "prep slides", "title": "slides", "when": {}},
        resolution={
            "status": "ambiguous",
            "raw_expression": "明天7点",
            "unresolved_fields": ["day_period"],
            "candidates": [
                {"id": "am", "value": {}, "label": "明天 07:00", "validity": "future"},
                {"id": "pm", "value": {}, "label": "明天 19:00", "validity": "future"},
            ],
        },
    )

    # Local principal filter would miss this; catalog scan with principal=None must not.
    rows = await list_open_clarifications_all_workspaces(limit=30, home=omni_home)
    assert any(r.record.id == rec.id for r in rows)
    hit = next(r for r in rows if r.record.id == rec.id)
    assert hit.workspace == "default"
    assert hit.record.required_decider == "wechat:user-1"
    assert hit.record.channel == "wechat"


def test_schedule_clarifications_cli_lists_wechat_draft(omni_home, monkeypatch):
    """CLI must not hard-filter principal=local — WeChat drafts on default appear."""
    from typer.testing import CliRunner

    from omni.cli.main import app

    async def _seed() -> str:
        agent = await make_agent(AppState(project="default"))
        try:
            store = ActionCheckpointStore(agent.db)
            rec = await store.open_clarification(
                action_kind="schedule.create",
                contract_version="v1",
                policy_version="temporal-policy-v1",
                channel="wechat",
                session_id="wx-cli",
                actor_principal="wechat:alice",
                required_decider="wechat:alice",
                payload={"goal": "digest", "title": "digest", "when": {}},
                resolution={
                    "status": "ambiguous",
                    "raw_expression": "今天8点",
                    "unresolved_fields": ["day_period"],
                    "candidates": [
                        {"id": "am", "value": {}, "label": "今天 08:00", "validity": "past"},
                        {"id": "pm", "value": {}, "label": "今天 20:00", "validity": "future"},
                    ],
                },
            )
            return rec.id
        finally:
            await agent.aclose()

    cid = run_async(_seed())
    monkeypatch.setattr(console, "_width", 200, raising=False)
    monkeypatch.setattr(console, "_height", 50, raising=False)

    # Invoke from a *different* named project so CWD-only listing would miss it.
    runner = CliRunner()
    res = runner.invoke(app, ["--project", "other-ws", "schedule", "clarifications"])
    assert res.exit_code == 0, res.output
    out = res.stdout
    assert cid[:8] in out
    assert "default" in out
    assert "今天8点" in out
    assert "wechat:alice" in out
    assert "original requester" in out


@pytest.mark.asyncio
async def test_list_open_principal_none_returns_all_deciders():
    """principal=None is the observability mode; scoped principal still filters."""
    agent = await make_agent(AppState(project="ckpt-principal-none"))
    try:
        store = ActionCheckpointStore(agent.db)
        await store.open_clarification(
            action_kind="schedule.create",
            contract_version="v1",
            policy_version="temporal-policy-v1",
            channel="wechat",
            session_id="s",
            actor_principal="wechat:x",
            required_decider="wechat:x",
            payload={"goal": "g", "when": {}},
            resolution={
                "status": "ambiguous",
                "raw_expression": "7点",
                "candidates": [{"id": "pm", "label": "19:00"}],
            },
        )
        assert await store.list_open(principal="local") == []
        assert len(await store.list_open(principal=None)) == 1
        assert len(await store.list_open(principal="wechat:x")) == 1
    finally:
        await agent.aclose()
