"""Workspace vs session filters for the web inspector drawers."""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.config import trust as trustmod
from omni.research.store import ResearchStore
from omni.web.projectors import (
    cost_snapshot,
    filter_notebook_for_session,
    notebook_text,
    rom_snapshot,
)


def test_filter_notebook_for_session_keeps_matching_sections_only() -> None:
    text = (
        "# Lab Notebook\n\n"
        "> header\n"
        "## 2026-08-19 10:00 — session-a title\n\n"
        "notes for the selected session\n"
        "## 2026-08-19 11:00 — other thread\n\n"
        "should drop\n"
    )
    filtered = filter_notebook_for_session(
        text, session_id="session-a", session_title="selected session"
    )
    assert "notes for the selected session" in filtered
    assert "should drop" not in filtered
    assert filter_notebook_for_session(text, session_id="missing") == ""


@pytest.mark.asyncio
async def test_rom_and_cost_snapshots_use_session_as_workspace_filter(tmp_path: Path) -> None:
    work = tmp_path / "inspector-scope"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        session_a = await agent.ensure_session(channel="cli", title="session-a")
        session_b = await agent.ensure_session(channel="cli", title="session-b")
        task_a = await agent.tasks.create_task(
            session_id=session_a,
            channel="cli",
            user_input="task a",
        )
        task_b = await agent.tasks.create_task(
            session_id=session_b,
            channel="cli",
            user_input="task b",
        )
        await agent.tasks.append_event(
            task_a.id,
            event_type="cost.usage",
            status="succeeded",
            name="planner",
            output_json={
                "component": "planner",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost_usd": 0.01,
            },
        )
        await agent.tasks.append_event(
            task_b.id,
            event_type="cost.usage",
            status="succeeded",
            name="planner",
            output_json={
                "component": "planner",
                "prompt_tokens": 40,
                "completion_tokens": 8,
                "total_tokens": 48,
                "cost_usd": 0.04,
            },
        )
        store = ResearchStore(agent.db)
        src = await store.add_source({"title": "Attention paper"})
        hyp_a = await store.add_hypothesis("A works", session_id=session_a)
        claim_a = await store.add_claim(
            "A is supported", session_id=session_a, hypothesis_id=hyp_a.id
        )
        await store.add_evidence(claim_a.id, source_id=src.id, quote="because")
        await store.add_hypothesis("B works", session_id=session_b)
        await store.add_run(title="run-b", session_id=session_b)

        workspace_rom = await rom_snapshot(agent)
        session_rom = await rom_snapshot(agent, session_id=session_a)
        assert workspace_rom["scope"] == "workspace"
        assert workspace_rom["counts"]["hypotheses"] == 2
        assert session_rom["scope"] == "session"
        assert session_rom["session_id"] == session_a
        assert [row["id"] for row in session_rom["hypotheses"]] == [hyp_a.id]
        assert [row["id"] for row in session_rom["sources"]] == [src.id]
        assert session_rom["runs"] == []

        workspace_cost = await cost_snapshot(agent)
        session_cost = await cost_snapshot(agent, session_id=session_a)
        assert workspace_cost["scope"] == "workspace"
        assert workspace_cost["total_tokens"] == 63
        assert session_cost["scope"] == "session"
        assert session_cost["session_id"] == session_a
        assert session_cost["total_tokens"] == 15
        assert [row["task_id"] for row in session_cost["tasks"]] == [task_a.id]

        settings.paths.notebook.write_text(
            "# Lab Notebook\n\n"
            f"## 2026-08-19 10:00 — {session_a}\n\n"
            "session a notes\n"
            f"## 2026-08-19 11:00 — {session_b}\n\n"
            "session b notes\n",
            encoding="utf-8",
        )
        assert "session a notes" in notebook_text(settings.paths)
        assert "session b notes" in notebook_text(settings.paths)
        scoped_notes = notebook_text(
            settings.paths, session_id=session_a, session_title="session-a"
        )
        assert "session a notes" in scoped_notes
        assert "session b notes" not in scoped_notes
    finally:
        await agent.aclose()
