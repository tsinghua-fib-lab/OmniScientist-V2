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
from omni.core.react_agent import ToolSpec
from omni.core.system_prompt import build_system_prompt, render_self_knowledge
from tests.conftest import PlanningLLM, ScriptedLLM

_SCHEMA = {"type": "object"}


def test_self_knowledge_prompt_requires_docs_when_available():
    block = render_self_knowledge([ToolSpec("docs_search", "search", _SCHEMA)])
    assert "use docs_search first" in block
    # Grounding, not refusing: never tell the model to give up.
    assert "refus" not in block.lower()


def test_self_knowledge_prompt_never_names_absent_docs_tool():
    # A genuinely tool-less turn (needs_input / memory_update / trimmed agent):
    # the prompt must not order the model to use a tool it does not have, which is
    # what forced the truthful refusal.
    block = render_self_knowledge([ToolSpec("read_file", "read", _SCHEMA)])
    assert "docs_search" not in block
    assert "general knowledge of OmniScientist" in block
    assert "rather than refusing to answer" in block


def test_build_system_prompt_omits_docs_search_when_not_in_catalog():
    with_docs = build_system_prompt(role="R", tools=[ToolSpec("docs_search", "s", _SCHEMA)])
    without_docs = build_system_prompt(role="R", tools=[ToolSpec("read_file", "r", _SCHEMA)])
    assert "use docs_search first" in with_docs
    # The whole prompt never names docs_search when it is absent from this turn.
    assert "docs_search" not in without_docs


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


@pytest.mark.asyncio
async def test_meta_question_direct_answer_proposal_still_grounds_in_docs():
    # Regression for the live bug: the model planner classifies "你的存储架构" as a
    # short direct_answer. Previously that stripped every tool (allowed_tools=[]),
    # so the model refused ("docs_search unavailable") while the prompt still asked
    # for it. direct_answer is now capability-preserving, so the same proposal keeps
    # docs_search and grounds the answer. ScriptedLLM alone masked this because its
    # non-JSON chat() fell back to the deterministic react_fallback route; PlanningLLM
    # drives the real model -> direct_answer path.
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.llm = PlanningLLM(
        {
            "intent_type": "direct_answer",
            "outputs": ["answer"],
            "confidence": 0.86,
            "execution_mode": "direct",
            "rationale": "short product answer",
        },
        planner_gated=True,
        script=[
            ChatWithToolsResult(
                tool_calls=[ToolCall("c1", "docs_search", {"query": "存储 架构 记忆"})]
            ),
            ChatWithToolsResult(content="omni 的存储基于 SQLite + 文件系统（见 memory.md）。"),
        ],
    )
    try:
        turn = await agent.handle_turn(
            "你的存储架构是如何设计的？", channel="cli", drain_tasks=False
        )
        # The model planner path actually ran (a direct_answer proposal was parsed),
        # not the deterministic offline fallback.
        assert agent.llm.plan_calls >= 1
        assert "memory.md" in turn.text or "SQLite" in turn.text
        assert turn.terminated_reason != "max_iterations"
        tool_names = [getattr(r, "name", r) for r in (turn.tool_trace or [])]
        assert "docs_search" in tool_names
    finally:
        await agent.aclose()
