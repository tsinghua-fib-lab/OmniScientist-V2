"""User-request runs: parent run, tool events, and child skill tasks."""

from __future__ import annotations

import json
import sys

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from tests.conftest import ScriptedLLM


def _echo_async_skill() -> SkillEntry:
    script = (
        "import json,sys;"
        "d=json.load(sys.stdin);"
        "print(json.dumps({'status':'ok','summary':'echo '+d.get('input','')}))"
    )
    return SkillEntry(
        name="echo-async",
        description="echo async skill",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


def _error_async_skill() -> SkillEntry:
    script = "import json; print(json.dumps({'status':'error','error':'boom'}))"
    return SkillEntry(
        name="error-async",
        description="error async skill",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


def _recoverable_async_skill() -> SkillEntry:
    script = (
        "import json,sys;"
        "d=json.load(sys.stdin);"
        "print(json.dumps({'status':'ok','summary':'recovered '+d.get('input','')}))"
    )
    return SkillEntry(
        name="recoverable-async",
        description="recoverable async skill",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


@pytest.mark.asyncio
async def test_user_turn_creates_parent_run_and_child_task_events():
    from omni.cli.commands.tasks_cmd import render_task_list
    from omni.cli.render import console

    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_echo_async_skill())
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "call-run-skill",
                        "run_skill",
                        {
                            "skill_name": "echo-async",
                            "mode": "background",
                            "input": {"input": "RAG figure"},
                        },
                    )
                ]
            ),
            ChatWithToolsResult(content="已提交绘图任务。"),
        ]
    )
    try:
        turn = await agent.handle_turn("请提交一个 echo 后台任务", channel="wechat", drain_tasks=False)
        assert turn.task_id

        run = await agent.tasks.get_task(turn.task_id)
        assert run is not None
        assert run.status == "running"
        assert run.channel == "wechat"
        assert len(run.submitted_subtask_ids) == 1
        with console.capture() as capture:
            render_task_list(agent.paths, [run])
        task_list = capture.get()
        assert run.id[:8] in task_list
        assert "running" in task_list

        events = await agent.tasks.list_events(run.id)
        event_types = [event.event_type for event in events]
        assert "user.message" in event_types
        assert "react.tool.start" in event_types
        assert "subtask.submitted" in event_types
        assert "react.tool.done" in event_types
        assert "assistant.message" in event_types

        task = await agent.runtime.get_subtask(run.submitted_subtask_ids[0])
        assert task is not None
        assert task.task_id == run.id
        assert task.status == "pending"

        await agent.runtime.drain()
        task = await agent.runtime.get_subtask(task.id)
        assert task.status == "succeeded"
        run = await agent.tasks.get_task(run.id)
        assert run is not None
        assert run.status == "succeeded"
        events = await agent.tasks.list_events(run.id)
        assert "subtask.done" in [event.event_type for event in events]
        assert "verification.passed" in [event.event_type for event in events]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_react_tools_and_child_task_are_recorded_under_parent_run():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_echo_async_skill())
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall("call-search", "search_corpus", {"query": "RAG hallucination", "k": 2}),
                    ToolCall(
                        "call-claim",
                        "record_claim",
                        {"text": "RAG can reduce hallucination by grounding answers.", "confidence": 0.7},
                    ),
                    ToolCall("call-evidence", "add_evidence", {"claim_id": "missing", "quote": "retrieved context"}),
                    ToolCall(
                        "call-run-skill",
                        "run_skill",
                        {"skill_name": "echo-async", "mode": "background", "input": {"input": "RAG figure"}},
                    ),
                ]
            ),
        ]
    )
    try:
        turn = await agent.handle_turn("请使用工具链检索、记录论断并提交后台任务", channel="cli", drain_tasks=False)
        assert turn.task_id

        events = await agent.tasks.list_events(turn.task_id)
        done_tools = [event.tool_name for event in events if event.event_type in {"react.tool.done", "react.tool.failed"}]
        assert "search_corpus" in done_tools
        assert "record_claim" in done_tools
        assert "add_evidence" in done_tools
        assert "run_skill" in done_tools

        run = await agent.tasks.get_task(turn.task_id)
        assert run is not None
        assert run.status == "running"
        assert len(run.submitted_subtask_ids) == 1
        child = await agent.runtime.get_subtask(run.submitted_subtask_ids[0])
        assert child is not None
        assert child.task_id == run.id
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_parent_run_rendering_json_child_lookup_and_progress():
    from omni.cli.commands.tasks_cmd import (
        render_subtask_detail,
        render_task_detail,
        render_task_json,
    )
    from omni.cli.render import console

    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_error_async_skill())
    try:
        run = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="生成 RAG 架构图",
        )
        workflow_run_id = await agent.runtime.enqueue_workflow(
            "progress smoke",
            [
                {
                    "id": "failing",
                    "skill_name": "error-async",
                    "input": {"input": "x"},
                }
            ],
            session_id=run.session_id,
            notify_channel="cli",
            task_id=run.id,
        )
        await agent.runtime.drain()

        events = await agent.tasks.list_events(run.id)
        assert any(event.event_type == "subtask.progress" for event in events)
        assert any((event.output_json or {}).get("stage") == "workflow.start" for event in events)

        children = await agent.runtime.list_subtasks(limit=10)
        children = [task for task in children if task.task_id == run.id]
        assert len(children) == 1
        execution = children[0]
        assert execution.id != workflow_run_id
        assert execution.skill_name == "error-async"
        assert execution.workflow_run_id == workflow_run_id
        workflows = await agent.runtime.list_workflow_runs(task_id=run.id)
        assert [workflow.id for workflow in workflows] == [workflow_run_id]
        steps = await agent.runtime.list_workflow_steps(workflow_run_id)
        assert len(steps) == 1
        assert steps[0].step_key == "failing"
        assert steps[0].current_execution_id == execution.id

        run = await agent.tasks.get_task(run.id)
        assert run is not None
        with console.capture() as capture:
            render_task_detail(run, events, workflows, steps, children, [])
        view = capture.get()
        assert "recent progress" in view
        assert workflow_run_id[:8] in view
        assert execution.id[:8] in view
        assert "error-async" in view

        with console.capture() as capture:
            render_task_json(run, events, workflows, steps, children, [])
        payload = json.loads(capture.get())
        assert payload["task_id"] == run.id
        assert any(event["event_type"] == "subtask.progress" for event in payload["events"])
        assert payload["workflows"][0]["workflow_run_id"] == workflow_run_id
        assert payload["subtasks"][0]["subtask_id"] == execution.id

        child = await agent.runtime.get_subtask(execution.id)
        assert child is not None
        with console.capture() as capture:
            render_subtask_detail(child)
        child_view = capture.get()
        assert run.id[:8] in child_view
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_child_task_failure_degrades_deliverable_parent_run():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_echo_async_skill())
    agent.registry.register(_error_async_skill())
    try:
        run = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="一部分成功一部分失败也要交付",
        )
        await agent.runtime.enqueue("echo-async", {"input": "ok"}, "cli", session_id=run.session_id, task_id=run.id)
        await agent.runtime.enqueue("error-async", {"input": "bad"}, "cli", session_id=run.session_id, task_id=run.id)

        await agent.runtime.drain()

        run = await agent.tasks.get_task(run.id)
        assert run is not None
        assert run.status == "degraded"
        assert "1/2" in run.summary
        events = await agent.tasks.list_events(run.id)
        assert "task.degraded" in [event.event_type for event in events]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_retry_reopens_parent_run_and_recalculates_after_child_success():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        run = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="需要恢复的科研任务",
        )
        failed_id = await agent.runtime.enqueue(
            "recoverable-async",
            {"input": "ok"},
            "cli",
            session_id=run.session_id,
            task_id=run.id,
        )
        await agent.runtime.drain()

        run = await agent.tasks.get_task(run.id)
        assert run is not None
        assert run.status == "failed"

        agent.registry.register(_recoverable_async_skill())
        retry_id = await agent.runtime.retry_subtask(failed_id)
        assert retry_id
        run = await agent.tasks.get_task(run.id)
        assert run is not None
        assert run.status == "recovering"

        await agent.runtime.drain()
        retry = await agent.runtime.get_subtask(retry_id)
        assert retry is not None
        assert retry.status == "succeeded"
        run = await agent.tasks.get_task(run.id)
        assert run is not None
        assert run.status == "succeeded"
        assert retry_id in run.submitted_subtask_ids
        events = await agent.tasks.list_events(run.id)
        event_types = [event.event_type for event in events]
        assert "task.recovering" in event_types
        assert "task.succeeded" in event_types
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["cli", "wechat", "feishu"])
async def test_cli_wechat_feishu_entries_use_same_run_recorder(channel: str):
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.llm = ScriptedLLM([ChatWithToolsResult(content=f"answer from {channel}")])
    try:
        turn = await agent.handle_turn("直接回答即可", channel=channel, drain_tasks=False)

        run = await agent.tasks.get_task(turn.task_id)
        assert run is not None
        assert run.channel == channel
        assert run.status == "succeeded"
        events = await agent.tasks.list_events(run.id)
        event_types = [event.event_type for event in events]
        assert event_types[0] == "user.message"
        assert event_types[1] == "task.ack"
        assert event_types.index("context.assembled") < event_types.index("plan.created")
        assert event_types.index("plan.created") < event_types.index("plan.validated")
        assert event_types[-3:] == ["assistant.message", "verification.passed", "task.succeeded"]
    finally:
        await agent.aclose()
