"""Structured command outcomes from the built-in Bash tool."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from omni.config import load_settings
from omni.core.tool_result import ToolResultEnvelope
from omni.skills_runtime.builtin_tools import shell
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.sandbox import SandboxUnavailableError
from tests.conftest import python_shell_command

pytestmark = pytest.mark.asyncio


def _bash_handler(tmp_path, *, mode: str = "workspace-write", channel: str = "cli"):
    settings = load_settings(cwd=tmp_path)
    settings.security.bash_sandbox = mode
    settings.security.os_sandbox = "off"
    context = ExecContext(
        settings=settings,
        paths=settings.paths,
        channel=channel,
        working_dir=tmp_path,
    )
    return shell.build_shell_tools(context)[0].handler


def _assert_serialized_within_budget(event_output: dict) -> None:
    encoded = json.dumps(event_output, default=str, ensure_ascii=False)
    assert len(encoded) <= 7_000
    assert len(encoded.encode("utf-8")) <= 7_000


async def test_bash_success_preserves_observation_and_emits_command_result(tmp_path):
    bash = _bash_handler(tmp_path)

    result = await bash(
        {"command": python_shell_command("import os; os.write(1, b'hello')")}
    )

    assert isinstance(result, ToolResultEnvelope)
    assert result.observation == "[exit=0]\nhello"
    assert result.event_output == {
        "result_schema": "omni.command-result.v1",
        "command_status": "succeeded",
        "reason": "ok",
        "exit_code": 0,
        "output": "hello",
        "output_truncated": False,
        "summary": "Command completed successfully",
    }


async def test_bash_nonzero_exit_is_a_structured_command_failure(tmp_path):
    bash = _bash_handler(tmp_path)

    result = await bash(
        {
            "command": python_shell_command(
                "import os; os.write(1, b'failed'); raise SystemExit(7)"
            )
        }
    )

    assert isinstance(result, ToolResultEnvelope)
    assert result.observation == "[exit=7]\nfailed"
    assert result.event_output == {
        "result_schema": "omni.command-result.v1",
        "command_status": "failed",
        "reason": "nonzero_exit",
        "exit_code": 7,
        "output": "failed",
        "output_truncated": False,
        "summary": "Command exited with code 7",
    }


async def test_bash_combines_stdout_and_stderr_in_observation_and_event_output(tmp_path):
    bash = _bash_handler(tmp_path)

    result = await bash(
        {
            "command": python_shell_command(
                "import os; os.write(1, b'stdout'); "
                "os.write(2, b'stderr'); raise SystemExit(3)"
            )
        }
    )

    assert result.observation == "[exit=3]\nstdoutstderr"
    assert result.event_output["output"] == "stdoutstderr"
    assert result.event_output["exit_code"] == 3
    assert result.event_output["command_status"] == "failed"


@pytest.mark.parametrize(
    ("args", "expected_status", "expected_reason", "expected_summary"),
    [
        ({}, "invalid", "empty_command", "Empty command"),
        (
            {"command": "sudo echo hi"},
            "blocked",
            "sandbox_blocked",
            "Command blocked by sandbox",
        ),
    ],
)
async def test_bash_controlled_rejections_are_structured(
    tmp_path,
    args,
    expected_status,
    expected_reason,
    expected_summary,
):
    bash = _bash_handler(tmp_path)

    result = await bash(args)

    assert isinstance(result, ToolResultEnvelope)
    assert result.observation.startswith("ERROR:")
    assert result.event_output["command_status"] == expected_status
    assert result.event_output["reason"] == expected_reason
    assert result.event_output["exit_code"] is None
    assert result.event_output["output"] == result.observation
    assert result.event_output["summary"] == expected_summary
    assert result.event_output["output_truncated"] is False


async def test_bash_channel_confirmation_block_is_structured(tmp_path, monkeypatch):
    monkeypatch.setattr(shell, "channel_requires_sensitive_confirm", lambda *_args: True)
    bash = _bash_handler(tmp_path, channel="feishu")

    result = await bash({"command": "printf unsafe"})

    assert result.event_output["command_status"] == "blocked"
    assert result.event_output["reason"] == "channel_confirmation_required"
    assert result.event_output["exit_code"] is None
    assert result.event_output["output"] == result.observation
    assert result.event_output["summary"] == "Shell command requires local confirmation"


async def test_bash_unavailable_sandbox_is_structured(tmp_path, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise SandboxUnavailableError("missing")

    monkeypatch.setattr(shell, "sandbox_prefix", unavailable)
    bash = _bash_handler(tmp_path)

    result = await bash({"command": "printf never-runs"})

    assert result.observation == "ERROR: OS sandbox required but unavailable: missing"
    assert result.event_output["command_status"] == "blocked"
    assert result.event_output["reason"] == "sandbox_unavailable"
    assert result.event_output["exit_code"] is None
    assert result.event_output["output"] == result.observation
    assert result.event_output["summary"] == "OS sandbox required but unavailable"


async def test_bash_timeout_is_structured(tmp_path):
    bash = _bash_handler(tmp_path)

    result = await bash(
        {"command": python_shell_command("import time; time.sleep(1)"), "timeout": 0.01}
    )

    assert result.observation == "ERROR: command timed out after 0.01s"
    assert result.event_output["command_status"] == "timed_out"
    assert result.event_output["reason"] == "timeout"
    assert result.event_output["exit_code"] is None
    assert result.event_output["output"] == result.observation
    assert result.event_output["summary"] == "Command timed out after 0.01s"


async def test_bash_spawn_oserror_still_propagates(tmp_path, monkeypatch):
    async def raise_oserror(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", raise_oserror)
    bash = _bash_handler(tmp_path, mode="full")

    with pytest.raises(OSError, match="spawn failed"):
        await bash({"command": "printf never-runs"})


async def test_bash_cancellation_stops_process_and_propagates(tmp_path, monkeypatch):
    proc = AsyncMock()
    proc.communicate = AsyncMock()
    proc.returncode = None
    stop = AsyncMock()

    async def cancel_wait_for(awaitable, **_kwargs):
        awaitable.close()
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "create_subprocess_shell", AsyncMock(return_value=proc))
    monkeypatch.setattr(asyncio, "wait_for", cancel_wait_for)
    monkeypatch.setattr(shell, "stop_process_tree", stop)
    bash = _bash_handler(tmp_path, mode="full")

    with pytest.raises(asyncio.CancelledError):
        await bash({"command": "printf interrupted"})

    stop.assert_awaited_once_with(proc)


@pytest.mark.parametrize(
    "raw_output",
    [
        ('"\\\0\n' * 5_000).encode(),
        ("汉🙂" * 10_000).encode(),
    ],
    ids=["json-control-characters", "unicode"],
)
async def test_bash_event_output_budget_handles_json_escaping_and_unicode(
    tmp_path,
    monkeypatch,
    raw_output,
):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(raw_output, None))
    proc.returncode = 0
    monkeypatch.setattr(asyncio, "create_subprocess_shell", AsyncMock(return_value=proc))
    bash = _bash_handler(tmp_path, mode="full")

    result = await bash({"command": "printf synthetic"})

    decoded = raw_output.decode("utf-8", errors="replace")
    assert result.observation == f"[exit=0]\n{decoded[:100_000]}"
    assert result.event_output["output_truncated"] is True
    assert decoded.startswith(result.event_output["output"])
    _assert_serialized_within_budget(result.event_output)


async def test_bash_observation_keeps_existing_hundred_thousand_character_cap(
    tmp_path,
    monkeypatch,
):
    raw_output = b"x" * 100_001
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(raw_output, None))
    proc.returncode = 0
    monkeypatch.setattr(asyncio, "create_subprocess_shell", AsyncMock(return_value=proc))
    bash = _bash_handler(tmp_path, mode="full")

    result = await bash({"command": "printf synthetic"})

    assert result.observation == f"[exit=0]\n{'x' * 100_000}"
    assert result.event_output["output_truncated"] is True
    _assert_serialized_within_budget(result.event_output)
