"""Historical user requests are retrieved directly from the AgentRun source of truth."""

from __future__ import annotations

import pytest

from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.skills_runtime.builtin_tools.recall import build_recall_tools
from tests.conftest import ScriptedLLM


async def _historical_run(agent: OmniAgent):  # noqa: ANN202
    run = await agent.tasks.create_task(
        session_id="session-old",
        channel="cli",
        user_input="生成 RAG 系统综述与 Transformer 架构图",
    )
    async with agent.db.session() as session:
        stored = await session.get(type(run), run.id)
        assert stored is not None
        stored.title = "RAG 系统综述 & Transformer 架构图"
        stored.summary = "已生成带引用的综述和三种格式的架构图。"
        stored.status = "succeeded"
        stored.submitted_subtask_ids = ["task-figure"]
        stored.artifact_ids = ["artifact-rag"]
        stored.source_ids = ["source-rag"]
        await session.commit()
    return run


@pytest.mark.asyncio
async def test_search_and_get_task_return_typed_linked_outputs() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await _historical_run(agent)
    session_id = await agent.ensure_session(channel="cli")
    ctx = agent._make_ctx(session_id, "cli", None, task_id="current", principal="local")
    tools = {tool.spec.name: tool for tool in build_recall_tools(ctx)}
    try:
        searched = await tools["search_tasks"].handler(
            {"query": "我想了解前面的 RAG 系统综述和 Transformer 架构图产出"}
        )
        detail = await tools["get_task"].handler({"task_id": f"task:{run.id[:8]}"})
        wrong_type = await tools["get_run"].handler({"run_id": f"task:{run.id}"})
    finally:
        await agent.aclose()

    assert searched["matches"][0]["ref"] == f"task:{run.id}"
    assert detail["ref"] == f"task:{run.id}"
    assert detail["subtask_refs"] == ["subtask:task-figure"]
    assert detail["artifact_refs"] == ["artifact:artifact-rag"]
    assert detail["source_refs"] == ["source:source-rag"]
    assert wrong_type["error"] == "wrong_id_type"
    assert wrong_type["hint"] == "use get_task for a user-request task"


@pytest.mark.asyncio
async def test_human_title_followup_converges_after_search_and_get() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await _historical_run(agent)
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "search-1",
                        "search_tasks",
                        {"query": "RAG 系统综述 Transformer 架构图"},
                    )
                ]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall("get-1", "get_task", {"task_id": f"task:{run.id}"})
                ]
            ),
            ChatWithToolsResult(content="该任务已完成综述，并产出了架构图 artifact。"),
        ]
    )
    try:
        turn = await agent.handle_turn(
            "我想了解我前面问的 RAG 系统综述 & Transformer 架构图，看看这个任务的产出是什么",
            channel="cli",
            drain_tasks=False,
        )
    finally:
        await agent.aclose()

    assert turn.kind == "text"
    assert turn.terminated_reason == "done"
    assert len(turn.tool_trace) == 2
    assert [record.name for record in turn.tool_trace] == ["search_tasks", "get_task"]
    assert "架构图 artifact" in turn.text


@pytest.mark.asyncio
async def test_analysis_followup_is_not_preempted_by_historical_lookup() -> None:
    agent = await OmniAgent.create(load_settings())
    await _historical_run(agent)
    llm = ScriptedLLM([ChatWithToolsResult(content="这是对结果可靠性的分析。")])
    agent.llm = llm
    try:
        turn = await agent.handle_turn(
            "分析一下前面 RAG 系统综述 & Transformer 架构图的结果是否可靠",
            channel="cli",
            drain_tasks=False,
        )
        events = await agent.tasks.list_events(turn.task_id)
    finally:
        await agent.aclose()

    assert llm.calls == 1
    assert turn.text == "这是对结果可靠性的分析。"
    assert not any(event.event_type.startswith("work_item.") for event in events)
