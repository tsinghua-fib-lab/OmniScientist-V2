"""ReAct injects host facts as observations; it does not stage the turn."""

from __future__ import annotations

import pytest

from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from tests.conftest import ScriptedLLM


class _Capture(ScriptedLLM):
    def __init__(self, script: list[ChatWithToolsResult]) -> None:
        super().__init__(script)
        self.prompts: list[list] = []

    async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
        self.prompts.append(list(messages))
        return await super().chat_with_tools(messages, tools, **kwargs)


class _Feed:
    def __init__(self) -> None:
        self.deltas = ["[Task research state Δ]\n+ source:s1 Paper"]
        self.findings = [
            "[Task research finding]\nThis task still owes artifact.figure on this task_id."
        ]
        self.seen_user: list[str] = []

    async def after_tool_batch(self) -> str:
        return self.deltas.pop(0) if self.deltas else ""

    async def before_text_finish(self) -> str:
        return self.findings.pop(0) if self.findings else ""

    async def after_steer(self) -> str:
        return "[Task research state]\nsources (1): source:s1 Paper"


async def _invoke(name: str, arguments: dict) -> dict:
    return {"status": "ok", "source_ids": ["s1"]}


@pytest.mark.asyncio
async def test_delta_and_debt_finding_stay_in_the_same_loop() -> None:
    feed = _Feed()
    llm = _Capture(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("1", "run_skill", {"skill_name": "openalex-search"})]
            ),
            ChatWithToolsResult(content="Here is the figure."),
            ChatWithToolsResult(content="Figure saved on this task."),
        ]
    )
    result = await ReActLoopAgent(
        llm,
        _invoke,
        max_iterations=6,
        fact_feed=feed,
    ).run(
        system_prompt="s",
        user_message="draw the architecture",
        tools=[ToolSpec("run_skill", "run a skill", {"type": "object", "properties": {}})],
    )
    assert result.terminated_reason == "done"
    assert result.content == "Figure saved on this task."
    assert llm.calls == 3
    user_blobs = [
        str(message.get("content") or "")
        for call in llm.prompts
        for message in call
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    assert any("Task research state Δ" in blob for blob in user_blobs)
    assert any("still owes artifact.figure" in blob for blob in user_blobs)


@pytest.mark.asyncio
async def test_empty_feed_does_not_delay_ordinary_answers() -> None:
    llm = ScriptedLLM([ChatWithToolsResult(content="SQLite plus the filesystem.")])
    result = await ReActLoopAgent(llm, _invoke, max_iterations=3).run(
        system_prompt="s",
        user_message="how is storage implemented?",
        tools=[ToolSpec("docs_search", "search docs", {"type": "object", "properties": {}})],
    )
    assert result.terminated_reason == "done"
    assert llm.calls == 1
    assert "SQLite" in result.content


@pytest.mark.asyncio
async def test_steer_injects_current_research_snapshot() -> None:
    from omni.core.execution_control import ExecutionControl

    feed = _Feed()
    llm = _Capture([ChatWithToolsResult(content="Will add the figure.")])
    control = ExecutionControl()
    control.push_steer("补上图")
    result = await ReActLoopAgent(
        llm,
        _invoke,
        max_iterations=3,
        fact_feed=feed,
    ).run(
        system_prompt="s",
        user_message="continue",
        tools=[ToolSpec("run_skill", "run a skill", {"type": "object", "properties": {}})],
        execution_control=control,
    )
    assert result.terminated_reason == "done"
    user_blobs = [
        str(message.get("content") or "")
        for call in llm.prompts
        for message in call
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    assert any("User steering" in blob for blob in user_blobs)
    assert any("source:s1" in blob for blob in user_blobs)
