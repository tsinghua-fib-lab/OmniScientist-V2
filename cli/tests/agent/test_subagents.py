"""Subagent delegation runtime: isolation, parallelism, summary hand-back,
reviewer gate, depth bounding, and the ``spawn_subagents`` coordinator tool."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omni.agent.subagents import (
    SubagentSpec,
    _child_context,
    _specialist_tools,
    run_subagent,
    run_subagents,
)
from omni.config import load_settings
from omni.config.paths import OmniPaths
from omni.config.settings import ComputeCfg
from omni.core.llm.client import ChatWithToolsResult, LLMClient, ToolCall
from omni.runtime.isolation import IsolationError, prepare_subagent_context
from omni.runtime.task_recorder import TaskRecorder
from omni.skills_runtime.builtin_tools import build_builtin_tools
from omni.skills_runtime.builtin_tools.delegate import build_delegation_tools
from omni.skills_runtime.context import ExecContext
from omni.storage.db import get_database
from tests.conftest import ScriptedLLM, python_shell_command


def _last_user(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""


class RoutingLLM(LLMClient):
    """Content-addressed LLM: deterministic regardless of parallel scheduling.

    ``responder(user_text)`` drives ``chat_with_tools`` (the specialist loop) and
    ``judge(user_text)`` drives ``chat`` (the reviewer). Both route on message
    text, so results are stable even when specialists run concurrently.
    """

    def __init__(
        self,
        responder: Callable[[str], ChatWithToolsResult],
        *,
        judge: Callable[[str], str] | None = None,
        delay: float = 0.0,
        marks: list[tuple[float, float]] | None = None,
    ) -> None:
        self.model = "routing"
        self._responder = responder
        self._judge = judge
        self._delay = delay
        self._marks = marks
        self.chat_with_tools_calls = 0

    async def chat_with_tools(self, messages, tools, **kw: Any) -> ChatWithToolsResult:  # noqa: ANN001
        started = time.perf_counter()
        self.chat_with_tools_calls += 1
        if self._delay:
            import asyncio

            await asyncio.sleep(self._delay)
        if self._marks is not None:
            self._marks.append((started, time.perf_counter()))
        return self._responder(_last_user(messages))

    async def chat(self, system: str, user: str, **kw: Any) -> str:
        if self._judge is not None:
            return self._judge(user)
        return f"summary:{user[:20]}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0, 0.0, 0.5] for _ in texts]


def _ctx(llm: Any, *, reviewer: bool = False, **overrides: Any) -> ExecContext:
    s = load_settings()
    s.paths.ensure_dirs()
    s.subagents.reviewer_enabled = reviewer
    for key, val in overrides.items():
        setattr(s.subagents, key, val)
    return ExecContext(settings=s, paths=s.paths, task_id="run-root", llm=llm)


# ── runtime: isolation + order + parallelism + summary hand-back ────────────


@pytest.mark.asyncio
async def test_specialists_are_isolated_and_order_preserved():
    # Each specialist echoes its own goal → proves it saw only its own task and
    # not a sibling's context.
    llm = RoutingLLM(lambda user: ChatWithToolsResult(content=f"ANSWER::{user}"))
    ctx = _ctx(llm)
    specs = [SubagentSpec(goal="read paper A"), SubagentSpec(goal="read paper B")]

    results = await run_subagents(specs, ctx, depth=0)

    assert [r.goal for r in results] == ["read paper A", "read paper B"]
    assert results[0].summary == "ANSWER::read paper A"
    assert results[1].summary == "ANSWER::read paper B"
    assert results[0].summary != results[1].summary
    assert all(r.status == "ok" for r in results)
    assert all(r.depth == 1 for r in results)


def _llm_calls_overlapped(marks: list[tuple[float, float]]) -> bool:
    """Whether any two specialist LLM sleeps ran at the same time.

    Wall-clock totals lie on Windows CI: ``run_subagent`` does enough sync I/O
    that three overlapping 0.2s sleeps can still land near 0.45–0.6s.
    Interval overlap is the property (serial calls never overlap).
    """
    for i, left in enumerate(marks):
        for right in marks[i + 1 :]:
            if left[0] < right[1] and right[0] < left[1]:
                return True
    return False


@pytest.mark.asyncio
async def test_specialists_run_in_parallel():
    marks: list[tuple[float, float]] = []
    llm = RoutingLLM(
        lambda user: ChatWithToolsResult(content="ok"), delay=0.2, marks=marks
    )
    ctx = _ctx(llm, concurrency=4)
    specs = [SubagentSpec(goal=f"task {i}") for i in range(3)]

    results = await run_subagents(specs, ctx, depth=0)

    assert len(results) == 3
    assert len(marks) == 3
    assert _llm_calls_overlapped(marks)


@pytest.mark.asyncio
async def test_run_subagents_caps_at_max_subagents():
    llm = RoutingLLM(lambda user: ChatWithToolsResult(content="ok"))
    ctx = _ctx(llm, max_subagents=2)
    specs = [SubagentSpec(goal=f"t{i}") for i in range(5)]

    results = await run_subagents(specs, ctx, depth=0)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_subagent_uses_its_independent_tool_budget_and_settles_degraded():
    llm = ScriptedLLM([
        ChatWithToolsResult(
            tool_calls=[
                ToolCall("r1", "read_file", {"path": __file__}),
                ToolCall("r2", "read_file", {"path": __file__}),
            ]
        ),
        ChatWithToolsResult(content="best effort from one admitted read"),
    ])
    ctx = _ctx(llm, max_tool_calls=1)

    result = await run_subagent(
        SubagentSpec(goal="inspect the test module", tools=("read_file",)),
        ctx,
    )

    assert result.status == "partial"
    assert result.tool_calls == 1
    assert result.summary == "best effort from one admitted read"


@pytest.mark.asyncio
async def test_subagent_persists_structured_bash_command_failure(tmp_path):
    settings = load_settings(cwd=tmp_path)
    settings.paths.ensure_dirs()
    settings.security.bash_sandbox = "workspace-write"
    settings.security.os_sandbox = "off"
    db = get_database(settings.paths.project_db)
    await db.init()
    recorder = TaskRecorder(db, project=settings.paths.project_name)
    parent = await recorder.create_task(
        session_id="",
        channel="cli",
        user_input="Delegate a Bash failure check.",
    )
    failing_command = python_shell_command("raise SystemExit(1)")
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
            ChatWithToolsResult(content="The command exited with code 1."),
        ]
    )
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        task_id=parent.id,
        db=db,
        llm=llm,
        channel="cli",
        working_dir=tmp_path,
    )

    result = await run_subagent(
        SubagentSpec(goal="Run false.", tools=("bash",)),
        ctx,
    )

    assert result.task_id
    events = await recorder.list_events(result.task_id)
    bash_event = next(
        event for event in events if event.event_type == "subagent.tool.done"
    )
    assert bash_event.status == "succeeded"
    assert bash_event.output_json["result_schema"] == "omni.command-result.v1"
    assert bash_event.output_json["command_status"] == "failed"
    assert bash_event.output_json["exit_code"] == 1


@pytest.mark.asyncio
async def test_subagent_persists_budget_rejection_as_rejected(tmp_path):
    settings = load_settings(cwd=tmp_path)
    settings.paths.ensure_dirs()
    settings.subagents.max_tool_calls = 0
    db = get_database(settings.paths.project_db)
    await db.init()
    recorder = TaskRecorder(db, project=settings.paths.project_name)
    parent = await recorder.create_task(
        session_id="",
        channel="cli",
        user_input="Delegate a call beyond the child tool budget.",
    )
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("blocked-read", "read_file", {"path": __file__})]
            ),
            ChatWithToolsResult(content="The child tool budget prevented the read."),
        ]
    )
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        task_id=parent.id,
        db=db,
        llm=llm,
        channel="cli",
        working_dir=tmp_path,
    )

    result = await run_subagent(
        SubagentSpec(goal="Read a file.", tools=("read_file",)),
        ctx,
    )

    assert result.task_id
    events = await recorder.list_events(result.task_id)
    rejected = next(
        event for event in events if event.event_type == "subagent.tool.rejected"
    )
    assert rejected.status == "rejected"
    assert "hard tool budget" in rejected.error


@pytest.mark.asyncio
async def test_subagent_revision_shares_the_original_tool_envelope():
    calls = {"initial": 0, "revision": 0}

    def responder(user: str) -> ChatWithToolsResult:
        if "Tools cannot make further progress" in user:
            return ChatWithToolsResult(content="revision stopped at the shared budget")
        if "[Review feedback]" in user:
            calls["revision"] += 1
            if calls["revision"] == 1:
                return ChatWithToolsResult(
                    tool_calls=[ToolCall("rev-read", "read_file", {"path": __file__})]
                )
            return ChatWithToolsResult(content="FINAL")
        calls["initial"] += 1
        if calls["initial"] == 1:
            return ChatWithToolsResult(
                tool_calls=[ToolCall("initial-read", "read_file", {"path": __file__})]
            )
        return ChatWithToolsResult(content="DRAFT")

    def judge(user: str) -> str:
        if "DRAFT" in user:
            return '{"verdict":"revise","score":0.3,"notes":"inspect once more"}'
        return '{"verdict":"pass","score":0.9,"notes":"bounded"}'

    ctx = _ctx(
        RoutingLLM(responder, judge=judge),
        reviewer=True,
        max_tool_calls=1,
        reviewer_max_revises=1,
    )

    result = await run_subagent(
        SubagentSpec(goal="inspect the test module", tools=("read_file",)),
        ctx,
    )

    assert result.status == "partial"
    assert result.summary == "revision stopped at the shared budget"


@pytest.mark.asyncio
async def test_context_field_carries_into_goal():
    seen: list[str] = []

    def responder(user: str) -> ChatWithToolsResult:
        seen.append(user)
        return ChatWithToolsResult(content="ok")

    ctx = _ctx(RoutingLLM(responder))
    await run_subagent(SubagentSpec(goal="G", context="BG-INFO"), ctx, depth=0)
    assert "G" in seen[0]
    assert "BG-INFO" in seen[0]


# ── reviewer gate ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reviewer_rejects_bad_output():
    def responder(user: str) -> ChatWithToolsResult:
        return ChatWithToolsResult(content="BAD")

    def judge(user: str) -> str:
        return '{"verdict":"reject","score":0.1,"notes":"off-topic"}'

    ctx = _ctx(RoutingLLM(responder, judge=judge), reviewer=True)
    res = await run_subagent(SubagentSpec(goal="do X"), ctx, depth=0)
    assert res.status == "rejected"
    assert res.review is not None and res.review["verdict"] == "reject"


@pytest.mark.asyncio
async def test_reviewer_accepts_good_output():
    ctx = _ctx(
        RoutingLLM(
            lambda user: ChatWithToolsResult(content="GOOD"),
            judge=lambda user: '{"verdict":"pass","score":0.95,"notes":"great"}',
        ),
        reviewer=True,
    )
    res = await run_subagent(SubagentSpec(goal="do X"), ctx, depth=0)
    assert res.status == "ok"
    assert res.review["verdict"] == "pass"


@pytest.mark.asyncio
async def test_reviewer_triggers_one_bounded_revision():
    def responder(user: str) -> ChatWithToolsResult:
        # After the reviewer's feedback is appended, produce the improved answer.
        if "[Review feedback]" in user:
            return ChatWithToolsResult(content="FINAL")
        return ChatWithToolsResult(content="DRAFT")

    def judge(user: str) -> str:
        if "FINAL" in user:
            return '{"verdict":"pass","score":0.9,"notes":"ok"}'
        return '{"verdict":"revise","score":0.3,"notes":"add citations"}'

    ctx = _ctx(RoutingLLM(responder, judge=judge), reviewer=True, reviewer_max_revises=1)
    res = await run_subagent(SubagentSpec(goal="do X"), ctx, depth=0)
    assert res.summary == "FINAL"
    assert res.status == "ok"
    assert res.review["verdict"] == "pass"


# ── depth gating of the delegation tool ─────────────────────────────────────


def test_delegation_tool_depth_gate():
    ctx = _ctx(RoutingLLM(lambda user: ChatWithToolsResult(content="ok")), max_depth=2)
    # depth 0 (coordinator) → offered
    assert [t.spec.name for t in build_delegation_tools(ctx)] == ["spawn_subagents"]
    # at/beyond max_depth → withdrawn (bounds nesting)
    ctx.subagent_depth = 2  # type: ignore[attr-defined]
    assert build_delegation_tools(ctx) == []


def test_delegation_tool_disabled_when_off():
    ctx = _ctx(RoutingLLM(lambda user: ChatWithToolsResult(content="ok")), enabled=False)
    assert build_delegation_tools(ctx) == []


def test_builtin_tools_offer_spawn_at_depth0_not_at_max():
    ctx = _ctx(RoutingLLM(lambda user: ChatWithToolsResult(content="ok")), max_depth=2)
    assert "spawn_subagents" in {t.spec.name for t in build_builtin_tools(ctx)}
    child = _child_context(_child_context(ctx, depth=0), depth=1)  # depth 2
    assert "spawn_subagents" not in {t.spec.name for t in build_builtin_tools(child)}


# ── specialist tool surface (privilege bounding) ────────────────────────────


def test_specialist_default_excludes_mutation_tools():
    ctx = _ctx(RoutingLLM(lambda user: ChatWithToolsResult(content="ok")))
    child = _child_context(ctx, depth=0)
    names = {t.spec.name for t in _specialist_tools(child, ())}
    assert "read_file" in names
    for muted in ("write_file", "edit_file", "bash", "run_compute"):
        assert muted not in names


def test_specialist_allowlist_can_regrant():
    ctx = _ctx(RoutingLLM(lambda user: ChatWithToolsResult(content="ok")))
    child = _child_context(ctx, depth=0)
    names = {t.spec.name for t in _specialist_tools(child, ("read_file", "write_file"))}
    assert names == {"read_file", "write_file"}


def test_container_allowlist_cannot_regrant_host_execution_tools():
    ctx = _ctx(RoutingLLM(lambda user: ChatWithToolsResult(content="ok")))
    child = _child_context(ctx, depth=0)
    names = {
        t.spec.name
        for t in _specialist_tools(
            child,
            ("read_file", "write_file", "edit_file", "bash", "run_compute"),
            isolation="container",
        )
    }
    assert names == {"read_file", "run_compute"}


# ── spawn_subagents coordinator tool (end to end) ───────────────────────────


@pytest.mark.asyncio
async def test_spawn_subagents_tool_roundtrip():
    llm = RoutingLLM(lambda user: ChatWithToolsResult(content=f"done::{user}"))
    ctx = _ctx(llm)
    tool = build_delegation_tools(ctx)[0]

    out = await tool.handler({"subtasks": [
        {"goal": "read A", "role": "reader"},
        {"goal": "read B", "role": "reader"},
    ]})

    assert out["status"] == "ok"
    assert out["count"] == 2
    assert out["accepted"] == 2
    goals = [r["goal"] for r in out["results"]]
    assert goals == ["read A", "read B"]


@pytest.mark.asyncio
async def test_spawn_subagents_tool_rejects_empty():
    ctx = _ctx(RoutingLLM(lambda user: ChatWithToolsResult(content="ok")))
    tool = build_delegation_tools(ctx)[0]
    out = await tool.handler({"subtasks": [{"role": "x"}]})
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_spawn_subagents_forwards_execution_overrides(monkeypatch):
    captured: list[SubagentSpec] = []

    async def fake_run(specs, ctx, *, depth):  # noqa: ANN001, ARG001
        captured.extend(specs)
        return []

    monkeypatch.setattr("omni.agent.subagents.run_subagents", fake_run)
    ctx = _ctx(RoutingLLM(lambda user: ChatWithToolsResult(content="ok")))
    tool = build_delegation_tools(ctx)[0]

    await tool.handler({"subtasks": [{
        "goal": "run experiment",
        "model": "specialist-model",
        "compute_profile": "gpu",
        "isolation": "container",
    }]})

    assert captured[0].model == "specialist-model"
    assert captured[0].compute_profile == "gpu"
    assert captured[0].isolation == "container"


@pytest.mark.asyncio
async def test_subagent_model_and_container_profile_overrides(monkeypatch):
    made_with: list[str] = []

    def fake_factory(settings):  # noqa: ANN001
        made_with.append(settings.model.model)
        llm = RoutingLLM(lambda user: ChatWithToolsResult(content="isolated"))
        llm.model = settings.model.model
        return llm

    monkeypatch.setattr("omni.agent.subagents.create_llm_client", fake_factory)
    ctx = _ctx(RoutingLLM(lambda user: ChatWithToolsResult(content="parent")))
    ctx.settings.compute_profiles["gpu"] = ComputeCfg(
        backend="docker", docker_image="python:3.12-slim", fallback_local=False,
    )

    result = await run_subagent(SubagentSpec(
        goal="run experiment",
        model="specialist-model",
        compute_profile="gpu",
        isolation="container",
    ), ctx)

    assert made_with == ["specialist-model"]
    assert result.status == "ok"
    assert result.model == "specialist-model"
    assert result.compute_profile == "gpu"
    assert result.isolation == "container"


@pytest.mark.asyncio
async def test_container_isolation_requires_configured_image():
    ctx = _ctx(RoutingLLM(lambda user: ChatWithToolsResult(content="ok")))

    with pytest.raises(IsolationError, match="docker_image"):
        await prepare_subagent_context(ctx, mode="container")


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
async def test_worktree_isolation_creates_independent_checkout(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    project_dir = tmp_path / "omni" / "project"
    paths = OmniPaths(
        home=tmp_path / "omni",
        project_name="test",
        project_dir=project_dir,
        workspace_root=repo,
    )
    paths.ensure_dirs()
    ctx = ExecContext(settings=load_settings(), paths=paths, task_id="run-root")

    isolated = await prepare_subagent_context(ctx, mode="worktree")

    assert isolated.working_dir is not None
    assert isolated.working_dir != repo
    assert (isolated.working_dir / "README.md").read_text(encoding="utf-8") == "base\n"


@pytest.mark.asyncio
async def test_child_context_is_isolated_run_id():
    ctx = _ctx(RoutingLLM(lambda user: ChatWithToolsResult(content="ok")))
    child = _child_context(ctx, depth=0)
    assert child.task_id != ctx.task_id
    assert child.task_id.startswith("run-root::sub-")
    assert child.subagent_depth == 1  # type: ignore[attr-defined]
