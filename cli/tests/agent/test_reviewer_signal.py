"""Reviewer verdicts are persisted as durable ``reviewer.*`` run events.

A rejected-then-revised subagent otherwise looks identical afterwards to one
that passed first time, because the judge's verdict lives only inside the
coordinator's tool result. Recording it on the parent run keeps it in
``omni task show`` and the event log.
"""

from __future__ import annotations

from typing import Any

import pytest

from omni.agent.subagents import SubagentSpec, run_subagent
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult
from omni.runtime.task_recorder import TaskRecorder
from omni.skills_runtime.context import ExecContext
from omni.storage.db import get_database


class _ScriptedLLM:
    """Specialist closes immediately; the judge rejects the output."""

    model = "scripted"

    async def chat(self, system: str, user: str, **kw: Any) -> str:
        if "review" in system.lower():
            return '{"verdict":"reject","score":0.1,"notes":"off-topic"}'
        return "summary"

    async def chat_with_tools(self, messages, tools, **kw: Any) -> ChatWithToolsResult:  # noqa: ANN001
        return ChatWithToolsResult(content="Final subtask answer.")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0] for _ in texts]


@pytest.mark.asyncio
async def test_reviewer_verdict_recorded_on_parent_run():
    s = load_settings()
    s.paths.ensure_dirs()
    s.subagents.enabled = True
    s.subagents.reviewer_enabled = True
    db = get_database(s.paths.project_db)
    await db.init()

    recorder = TaskRecorder(db, project="default")
    run = await recorder.create_task(session_id="sess1", channel="cli", user_input="do research")

    ctx = ExecContext(settings=s, paths=s.paths, task_id=run.id, db=db, llm=_ScriptedLLM())
    result = await run_subagent(SubagentSpec(goal="read paper X", role="reader"), ctx)

    assert result.status == "rejected"  # judge rejected → status downgraded
    events = await recorder.list_events(run.id)
    kinds = [e.event_type for e in events]
    assert "reviewer.reject" in kinds
    rejected_event = next(e for e in events if e.event_type == "reviewer.reject")
    assert rejected_event.output_json.get("score") == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_no_reviewer_event_without_run_id():
    s = load_settings()
    s.paths.ensure_dirs()
    s.subagents.enabled = True
    s.subagents.reviewer_enabled = True
    db = get_database(s.paths.project_db)
    await db.init()
    # no task_id on the context → recording is a safe no-op (never raises)
    ctx = ExecContext(settings=s, paths=s.paths, task_id="", db=db, llm=_ScriptedLLM())
    result = await run_subagent(SubagentSpec(goal="g", role="reader"), ctx)
    assert result.status == "rejected"
