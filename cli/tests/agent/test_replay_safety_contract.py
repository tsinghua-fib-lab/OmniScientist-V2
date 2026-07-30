"""Automatic retries require an explicit, host-visible replay-safety contract."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omni.config import load_settings
from omni.core import react_agent
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.runtime.notifications import InboxNotifier
from omni.runtime.subtask_runtime import SubtaskRuntime
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database
from tests.conftest import ScriptedLLM


@pytest.mark.asyncio
async def test_non_replay_safe_tool_is_not_retried_after_timeout(monkeypatch) -> None:
    calls = 0

    async def invoker(_name, _args):  # noqa: ANN001
        nonlocal calls
        calls += 1
        raise TimeoutError("response lost after dispatch")

    monkeypatch.setattr(react_agent, "_TOOL_RETRY_BASE_DELAY", 0)
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(tool_calls=[ToolCall("c1", "publish", {"value": "x"})]),
            ChatWithToolsResult(content="the publish outcome is unknown"),
        ]
    )
    spec = ToolSpec(
        "publish",
        "publish once",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        replay_safe=False,
    )
    agent = ReActLoopAgent(llm, invoker, max_iterations=2)

    result = await agent.run(system_prompt="sys", user_message="publish", tools=[spec])

    assert calls == 1
    assert result.tool_trace[0].attempts == 1
    assert result.tool_trace[0].status == "failed"


@pytest.mark.asyncio
async def test_replay_safe_tool_retries_pre_execution_failure_at_most_once(monkeypatch) -> None:
    calls = 0

    async def invoker(_name, _args):  # noqa: ANN001
        nonlocal calls
        calls += 1
        raise TimeoutError("connection timed out before request was sent")

    monkeypatch.setattr(react_agent, "_TOOL_RETRY_BASE_DELAY", 0)
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(tool_calls=[ToolCall("c1", "lookup", {"query": "RAG"})]),
            ChatWithToolsResult(content="lookup unavailable"),
        ]
    )
    spec = ToolSpec(
        "lookup",
        "idempotent lookup",
        {"type": "object", "properties": {"query": {"type": "string"}}},
        replay_safe=True,
    )
    agent = ReActLoopAgent(llm, invoker, max_iterations=2)

    result = await agent.run(system_prompt="sys", user_message="lookup", tools=[spec])

    assert calls == 2
    assert result.tool_trace[0].attempts == 2
    assert result.tool_trace[0].status == "failed"


def test_replay_safety_is_host_metadata_not_exposed_in_model_schema() -> None:
    spec = ToolSpec(
        "lookup",
        "idempotent lookup",
        {"type": "object", "properties": {}},
        replay_safe=True,
    )

    assert spec.replay_safe is True
    assert "replay_safe" not in repr(spec.to_openai_spec())


_COUNTING_TRANSIENT = (
    "import json,os,sys;"
    "d=json.load(sys.stdin);p=d['counter'];"
    "n=(int(open(p).read()) if os.path.exists(p) else 0)+1;"
    "open(p,'w').write(str(n));"
    "print(json.dumps({'status':'error','error':'connection timeout (transient)'}))"
)

_COUNTING_HEAL = (
    "import json,os,sys;"
    "d=json.load(sys.stdin);p=d['counter'];"
    "n=(int(open(p).read()) if os.path.exists(p) else 0)+1;"
    "open(p,'w').write(str(n));"
    "print(json.dumps({'status':'ok','summary':'healed'}) if n>1 "
    "else json.dumps({'status':'error','error':'connection timeout (transient)'}))"
)


def _cli_skill(name: str, script: str, *, replay_safe: bool) -> SkillEntry:
    return SkillEntry(
        name=name,
        description="replay contract fixture",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(
            command=sys.executable,
            args=["-c", script],
            stdout_format="json",
        ),
        execution={"replay_safe": replay_safe},
    )


async def _runtime(entry: SkillEntry) -> SubtaskRuntime:
    settings = load_settings()
    settings.paths.ensure_dirs()
    settings.tasks.retry_backoff_s = 0
    db = get_database(settings.paths.project_db)
    await db.init()
    registry = SkillRegistry(settings)
    registry.build_index()
    registry.register(entry)
    inbox = InboxNotifier(settings.paths.project_dir / "inbox.jsonl")

    def ctx_factory(session_id, channel):  # noqa: ANN001, ANN202
        return ExecContext(
            settings=settings,
            paths=settings.paths,
            session_id=session_id,
            channel=channel,
        )

    return SubtaskRuntime(db, settings, registry, ctx_factory, notifier=inbox)


@pytest.mark.asyncio
async def test_non_replay_safe_skill_transient_failure_executes_once(tmp_path: Path) -> None:
    runtime = await _runtime(
        _cli_skill("publish-once", _COUNTING_TRANSIENT, replay_safe=False)
    )
    counter = tmp_path / "io" / "unsafe-count.txt"
    counter.parent.mkdir()

    subtask_id = await runtime.enqueue(
        "publish-once",
        {"counter": str(counter)},
        "cli",
    )
    await runtime.drain()

    task = await runtime.get_subtask(subtask_id)
    assert counter.read_text(encoding="utf-8") == "1"
    assert task.status == "failed"
    assert task.recovery_attempt == 0


@pytest.mark.asyncio
async def test_replay_safe_skill_transient_failure_may_retry_once(tmp_path: Path) -> None:
    runtime = await _runtime(
        _cli_skill("safe-lookup", _COUNTING_HEAL, replay_safe=True)
    )
    counter = tmp_path / "io" / "safe-count.txt"
    counter.parent.mkdir()

    subtask_id = await runtime.enqueue(
        "safe-lookup",
        {"counter": str(counter)},
        "cli",
    )
    await runtime.drain()

    task = await runtime.get_subtask(subtask_id)
    assert counter.read_text(encoding="utf-8") == "2"
    assert task.status == "succeeded"
    assert task.recovery_attempt == 1
