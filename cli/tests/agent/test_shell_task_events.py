"""End-to-end contracts for Bash outcomes in the task event ledger."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from omni.cli.commands.tasks_cmd import render_task_json
from omni.cli.render import console
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent
from omni.runtime.task_recorder import TaskRecorder
from omni.runtime.tool_gateway import ToolGateway
from omni.skills_runtime.builtin_tools.shell import build_shell_tools
from omni.skills_runtime.context import ExecContext
from omni.storage.db import get_database
from tests.conftest import ScriptedLLM, python_shell_command


async def _task_runtime(tmp_path):
    settings = load_settings(cwd=tmp_path)
    settings.security.bash_sandbox = "workspace-write"
    settings.security.os_sandbox = "off"
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    recorder = TaskRecorder(db, project=settings.paths.project_name)
    task = await recorder.create_task(
        session_id="",
        channel="cli",
        user_input="Run false with Bash and report whether it succeeded.",
    )
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        channel="cli",
        task_id=task.id,
        task_recorder=recorder,
        working_dir=tmp_path,
    )
    return recorder, task, ctx


@pytest.mark.asyncio
async def test_false_is_structured_from_react_through_sqlite_and_task_json(tmp_path):
    recorder, task, ctx = await _task_runtime(tmp_path)
    bash = build_shell_tools(ctx)[0]
    failing_command = python_shell_command("raise SystemExit(1)")
    gateway = ToolGateway(
        task_id=task.id,
        tools=[bash],
        tasks=recorder,
        event_family="react",
    )
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "bash-false",
                        "bash",
                        {"command": failing_command},
                    )
                ]
            ),
            ChatWithToolsResult(content="The command failed with exit code 1."),
        ]
    )
    react = ReActLoopAgent(llm, gateway.react_invoker(), max_iterations=2)

    result = await react.run(
        system_prompt="Use Bash and report its result.",
        user_message="Run false.",
        tools=gateway.tool_specs,
        on_tool_event=gateway.emit,
    )

    assert result.content == "The command failed with exit code 1."
    assert result.tool_trace[0].status == "succeeded"
    assert result.tool_trace[0].error is None
    assert result.tool_trace[0].to_observation() == "[exit=1]\n"

    events = await recorder.list_events(task.id)
    shell_event = next(
        event
        for event in events
        if event.tool_name == "bash" and event.event_type == "react.tool.done"
    )
    assert shell_event.status == "succeeded"
    assert shell_event.lifecycle_status == "completed"
    assert shell_event.result_success is True
    assert shell_event.output_json["result_schema"] == "omni.command-result.v1"
    assert shell_event.output_json["command_status"] == "failed"
    assert shell_event.output_json["reason"] == "nonzero_exit"
    assert shell_event.output_json["exit_code"] == 1

    refreshed = await recorder.get_task(task.id)
    assert refreshed is not None
    with console.capture() as capture:
        render_task_json(refreshed, events, [], [], [], [])
    payload = json.loads(capture.get())
    persisted = next(
        event
        for event in payload["events"]
        if event["tool_name"] == "bash" and event["event_type"] == "react.tool.done"
    )
    assert persisted["status"] == "succeeded"
    assert persisted["lifecycle_status"] == "completed"
    assert persisted["result_success"] is True
    assert persisted["output_json"]["command_status"] == "failed"
    assert persisted["output_json"]["exit_code"] == 1


@pytest.mark.asyncio
async def test_task_json_preserves_legacy_string_and_new_command_result(tmp_path):
    recorder, task, _ = await _task_runtime(tmp_path)
    await recorder.append_event(
        task.id,
        event_type="react.tool.done",
        status="succeeded",
        tool_name="bash",
        output_json="[exit=1]\n",
        summary="[exit=1]",
    )
    await recorder.append_event(
        task.id,
        event_type="react.tool.done",
        status="succeeded",
        tool_name="bash",
        output_json={
            "result_schema": "omni.command-result.v1",
            "command_status": "failed",
            "reason": "nonzero_exit",
            "exit_code": 1,
            "output": "",
            "output_truncated": False,
            "summary": "Command exited with code 1",
        },
        summary="Command exited with code 1",
    )

    refreshed = await recorder.get_task(task.id)
    events = await recorder.list_events(task.id)
    assert refreshed is not None
    with console.capture() as capture:
        render_task_json(refreshed, events, [], [], [], [])
    payload = json.loads(capture.get())

    legacy, current = payload["events"][-2:]
    assert legacy["output_json"] == "[exit=1]\n"
    assert current["output_json"]["result_schema"] == "omni.command-result.v1"
    assert current["output_json"]["command_status"] == "failed"


@pytest.mark.asyncio
async def test_escaped_long_output_keeps_command_fields_after_sqlite_storage(
    tmp_path,
    monkeypatch,
):
    raw_output = ('"\\\0\n' * 5_000).encode()
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(raw_output, None))
    proc.returncode = 37
    monkeypatch.setattr(
        "omni.skills_runtime.builtin_tools.shell.spawn_user_shell",
        AsyncMock(return_value=proc),
    )
    recorder, task, ctx = await _task_runtime(tmp_path)
    bash = build_shell_tools(ctx)[0]
    gateway = ToolGateway(
        task_id=task.id,
        tools=[bash],
        tasks=recorder,
        event_family="react",
    )
    react = ReActLoopAgent(
        ScriptedLLM(
            [
                ChatWithToolsResult(
                    tool_calls=[
                        ToolCall("bash-long", "bash", {"command": "synthetic"})
                    ]
                ),
                ChatWithToolsResult(content="The command failed."),
            ]
        ),
        gateway.react_invoker(),
        max_iterations=2,
    )

    await react.run(
        system_prompt="Inspect the command outcome.",
        user_message="Run it.",
        tools=gateway.tool_specs,
        on_tool_event=gateway.emit,
    )

    events = await recorder.list_events(task.id)
    shell_event = next(
        event for event in events if event.event_type == "react.tool.done"
    )
    output = shell_event.output_json
    assert output["result_schema"] == "omni.command-result.v1"
    assert output["command_status"] == "failed"
    assert output["reason"] == "nonzero_exit"
    assert output["exit_code"] == 37
    assert output["output_truncated"] is True
    encoded = json.dumps(output, ensure_ascii=False)
    assert len(encoded) <= 7_000
    assert len(encoded.encode("utf-8")) <= 7_000
