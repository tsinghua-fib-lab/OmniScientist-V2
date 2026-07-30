"""Historical user requests are retrieved directly from the AgentRun source of truth."""

from __future__ import annotations

import pytest

from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.skills_runtime.builtin_tools.recall import build_recall_tools
from omni.storage.models import SubtaskORM
from tests.conftest import PlanningLLM, ScriptedLLM


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
async def test_cancelled_historical_task_read_persists_completed_invocation() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await _historical_run(agent)
    async with agent.db.session() as session:
        stored = await session.get(type(run), run.id)
        assert stored is not None
        stored.status = "cancelled"
        stored.summary = "Partial result: The user cancelled execution."
        await session.commit()
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall("get-1", "get_task", {"task_id": f"task:{run.id}"})
                ]
            ),
            ChatWithToolsResult(content="The historical task was cancelled."),
        ]
    )
    try:
        turn = await agent.handle_turn(
            f"Inspect task:{run.id}",
            channel="cli",
            drain_tasks=False,
        )
        events = await agent.tasks.list_events(turn.task_id)
    finally:
        await agent.aclose()

    record = next(item for item in turn.tool_trace if item.name == "get_task")
    terminal = next(
        event
        for event in events
        if event.tool_name == "get_task" and event.event_type.endswith((".done", ".failed"))
    )
    assert record.status == "succeeded"
    assert record.result["task_status"] == "cancelled"
    assert terminal.event_type == "react.tool.done"
    assert terminal.status == "succeeded"
    assert terminal.lifecycle_status == "completed"
    assert terminal.result_success is True


@pytest.mark.asyncio
async def test_failed_historical_subtask_read_persists_completed_invocation() -> None:
    agent = await OmniAgent.create(load_settings())
    execution_id = "failed-subtask-1234567890"
    async with agent.db.session() as session:
        session.add(
            SubtaskORM(
                id=execution_id,
                skill_name="paper-review",
                status="failed",
                error="historical provider failure",
                result_json={"status": "error", "summary": "review stopped"},
            )
        )
        await session.commit()
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "get-subtask-1",
                        "get_subtask",
                        {"subtask_id": execution_id},
                    )
                ]
            ),
            ChatWithToolsResult(content="The historical execution failed."),
        ]
    )
    try:
        turn = await agent.handle_turn(
            f"Inspect subtask:{execution_id}",
            channel="cli",
            drain_tasks=False,
        )
        events = await agent.tasks.list_events(turn.task_id)
    finally:
        await agent.aclose()

    record = next(item for item in turn.tool_trace if item.name == "get_subtask")
    terminal = next(
        event
        for event in events
        if event.tool_name == "get_subtask"
        and event.event_type.endswith((".done", ".failed"))
    )
    assert record.status == "succeeded"
    assert record.result["subtask_status"] == "failed"
    assert record.result["failure_reason"] == "historical provider failure"
    assert terminal.event_type == "react.tool.done"
    assert terminal.status == "succeeded"
    assert terminal.lifecycle_status == "completed"
    assert terminal.result_success is True


@pytest.mark.asyncio
async def test_task_inspect_projects_degraded_status_instead_of_model_claim() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await _historical_run(agent)
    artifact = await agent.artifacts.put_bytes(
        b"# Review\n",
        kind="report",
        title="Paper review report",
        ext="md",
        task_id=run.id,
        session_id=run.session_id,
    )
    async with agent.db.session() as session:
        stored = await session.get(type(run), run.id)
        assert stored is not None
        stored.status = "degraded"
        stored.summary = "0/1 succeeded, 1 degraded, and 0 failed."
        stored.artifact_ids = [artifact.id]
        await session.commit()

    agent.llm = PlanningLLM(
        {
            "intent_type": "react_fallback",
            "required_capabilities": ["task.inspect"],
            "outputs": ["answer"],
            "confidence": 0.95,
            "rationale": "inspect the referenced task",
        },
        script=[
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall("get-1", "get_task", {"task_id": f"task:{run.id}"})
                ]
            ),
            # The provider contradicts its tool observation. The host projection
            # must discard this sentence and render the durable state instead.
            ChatWithToolsResult(content="是的，刚才的任务已经成功完成。"),
        ],
    )
    streamed: list[str] = []
    try:
        turn = await agent.handle_turn(
            f"task:{run.id} 的审稿成功了吗？结果在哪里？",
            channel="cli",
            drain_tasks=False,
            on_token=streamed.append,
        )
    finally:
        await agent.aclose()

    assert [record.name for record in turn.tool_trace] == ["get_task"]
    assert "**degraded**" in turn.text
    assert "not a full success" in turn.text
    assert "已经成功完成" not in turn.text
    assert streamed == []
    assert str(artifact.path) in turn.text
    assert artifact.uri in turn.text


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
