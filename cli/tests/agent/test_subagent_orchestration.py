"""Persona regression: a scientist turn where the coordinating agent delegates
to parallel specialists and synthesizes their summaries (three-layer flow through
the real orchestrator, mock provider, fully offline)."""

from __future__ import annotations

from typing import Any

import pytest

from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, LLMClient, ToolCall


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str((tool.get("function") or {}).get("name") or tool.get("name"))
    return str(getattr(tool, "name", ""))


def _last_user(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""


class DelegatingLLM(LLMClient):
    """Coordinator delegates two parallel reads, then synthesizes.

    Routing is content-addressed so it is stable under concurrent specialists:
    ``SPEC::`` goals are specialist turns; a coordinator turn with a
    ``spawn_subagents`` catalog and no tool observations yet fires the delegation;
    once a tool observation is present it writes the final synthesis.
    """

    def __init__(self) -> None:
        self.model = "delegating"
        self.coordinator_tools: list[str] = []
        self.specialist_goals: list[str] = []
        self._spawned = False

    async def chat_with_tools(self, messages, tools, **kw: Any) -> ChatWithToolsResult:  # noqa: ANN001
        names = [_tool_name(t) for t in tools]
        last_user = _last_user(messages)
        if last_user.startswith("SPEC::"):
            self.specialist_goals.append(last_user)
            return ChatWithToolsResult(content=f"读毕:{last_user.splitlines()[0]}")
        self.coordinator_tools = names
        has_tool_obs = any(m.get("role") == "tool" for m in messages)
        if "spawn_subagents" in names and not has_tool_obs and not self._spawned:
            self._spawned = True
            return ChatWithToolsResult(tool_calls=[ToolCall(
                id="call-1",
                name="spawn_subagents",
                arguments={"subtasks": [
                    {"goal": "SPEC::读论文A", "role": "reader"},
                    {"goal": "SPEC::读论文B", "role": "reader"},
                ]},
            )])
        return ChatWithToolsResult(content="综合结论：已对比论文A与论文B的方法差异。")

    async def chat(self, system: str, user: str, **kw: Any) -> str:
        return "summary:offline"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0, 0.0, 0.5] for _ in texts]


@pytest.mark.asyncio
async def test_coordinator_delegates_to_parallel_specialists():
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = await OmniAgent.create(settings)
    llm = DelegatingLLM()
    agent.llm = llm
    agent.memory._llm = llm
    try:
        turn = await agent.handle_turn(
            "请对比论文A与论文B的方法差异", channel="cli", drain_tasks=False
        )

        # Coordinator was offered the delegation tool and fired it.
        assert "spawn_subagents" in llm.coordinator_tools
        # Two specialists ran, each on its own isolated goal.
        assert sorted(g.splitlines()[0] for g in llm.specialist_goals) == [
            "SPEC::读论文A", "SPEC::读论文B",
        ]
        # Coordinator synthesized a final answer after the summaries came back.
        assert turn.text == "综合结论：已对比论文A与论文B的方法差异。"
        assert "spawn_subagents" in {r.name for r in turn.tool_trace}
    finally:
        await agent.aclose()
