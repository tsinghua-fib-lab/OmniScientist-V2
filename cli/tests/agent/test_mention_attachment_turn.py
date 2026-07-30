"""End-to-end: an attached file is readable for the turn that attached it.

Each layer is unit-tested elsewhere; this covers the seam between them. The
grant is only useful if ``handle_turn(file_uris=...)`` actually reaches the fs
tools' admission check, and that path crosses the orchestrator, the interaction
lifecycle, the exec context and the tool builder — exactly where a rename
quietly breaks the feature while every unit test stays green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from tests.conftest import ScriptedLLM

_SENTINEL = "SENTINEL findings about protein folding"


async def _tool_observations(target: Path, *, attach: bool) -> str:
    """Run one turn whose only tool call reads ``target``; return its events."""
    agent = await OmniAgent.create(load_settings())
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("call-read", "read_file", {"path": str(target)})]
            ),
            ChatWithToolsResult(content="done"),
        ]
    )
    turn = await agent.handle_turn(
        f"read the file {target} and summarize it",
        channel="cli",
        drain_tasks=False,
        file_uris=[str(target)] if attach else None,
    )
    events = await agent.tasks.list_events(turn.task_id)
    return "\n".join(f"{event.event_type}|{event.summary or ''}" for event in events)


@pytest.mark.asyncio
async def test_attached_file_outside_the_roots_is_readable(tmp_path: Path) -> None:
    target = tmp_path / "paper.md"
    target.write_text(_SENTINEL, encoding="utf-8")

    observations = await _tool_observations(target, attach=True)

    assert "react.tool.done" in observations
    assert _SENTINEL in observations


@pytest.mark.asyncio
async def test_the_same_file_is_refused_without_the_attachment(tmp_path: Path) -> None:
    target = tmp_path / "paper.md"
    target.write_text(_SENTINEL, encoding="utf-8")

    observations = await _tool_observations(target, attach=False)

    assert "SENTINEL" not in observations
    assert "outside the accessible roots" in observations
