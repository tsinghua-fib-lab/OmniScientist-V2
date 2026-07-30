"""Self-knowledge routing: meta questions are answered from omni's own docs.

Proves the integrated fix end-to-end: a question about omni itself lets the
model consult ``docs_search`` and answer — instead of flailing on the
filesystem until the iteration cap (the original ``你的存储架构`` bug).
"""

from __future__ import annotations

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from tests.conftest import ScriptedLLM


@pytest.mark.asyncio
async def test_meta_question_uses_docs_and_not_iteration_cap():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    # Model consults the self-knowledge docs, then answers from them.
    agent.llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "docs_search", {"query": "存储 架构 记忆"})]),
        ChatWithToolsResult(content="omni 的存储基于 SQLite + 文件系统（见 memory.md）。"),
    ])
    try:
        turn = await agent.handle_turn(
            "你的存储架构是如何设计的？", channel="cli", drain_tasks=False
        )
        assert "memory.md" in turn.text or "SQLite" in turn.text
        assert turn.terminated_reason != "max_iterations"
        tool_names = [getattr(r, "name", r) for r in (turn.tool_trace or [])]
        assert "docs_search" in tool_names
    finally:
        await agent.aclose()
