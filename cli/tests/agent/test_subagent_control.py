"""Async multi-agent control plane (Codex V2 parity): spawn / wait / list /
interrupt, the session-tree concurrency cap, and turn-end teardown (no orphan
tasks). Plus the async delegation tool surface gating.

All offline: a content-addressed ``RoutingLLM`` drives the specialist loops, so
results are deterministic regardless of concurrent scheduling.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from omni.agent.subagent_control import SubagentControl
from omni.agent.subagents import SubagentSpec, _child_context
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, LLMClient
from omni.skills_runtime.builtin_tools.delegate import build_delegation_tools
from omni.skills_runtime.context import ExecContext


class RoutingLLM(LLMClient):
    """Deterministic content-addressed LLM (mirrors test_subagents.RoutingLLM)."""

    def __init__(self, responder: Callable[[str], ChatWithToolsResult], *, delay: float = 0.0) -> None:
        self.model = "routing"
        self._responder = responder
        self._delay = delay

    async def chat_with_tools(self, messages, tools, **kw: Any) -> ChatWithToolsResult:  # noqa: ANN001
        if self._delay:
            await asyncio.sleep(self._delay)
        last_user = next(
            (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        return self._responder(last_user)

    async def chat(self, system: str, user: str, **kw: Any) -> str:
        return f"summary:{user[:20]}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0, 0.0, 0.5] for _ in texts]


def _echo_llm(*, delay: float = 0.0) -> RoutingLLM:
    return RoutingLLM(lambda user: ChatWithToolsResult(content=f"ANSWER::{user}"), delay=delay)


def _ctx(llm: Any, **overrides: Any) -> ExecContext:
    s = load_settings()
    s.paths.ensure_dirs()
    s.subagents.async_enabled = True
    s.subagents.reviewer_enabled = False
    for key, val in overrides.items():
        setattr(s.subagents, key, val)
    return ExecContext(settings=s, paths=s.paths, task_id="run-root", llm=llm)


def _control(ctx: ExecContext) -> SubagentControl:
    control = SubagentControl(ctx, cfg=ctx.settings.subagents, depth=0)
    ctx.subagent_control = control
    return control


# ── spawn + wait ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_returns_handle_and_wait_collects_summary():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    try:
        nickname = await control.spawn(SubagentSpec(goal="read paper A", role="reader"))
        assert nickname == "reader-1"

        result = await control.wait(nickname, None)
        assert result["timed_out"] is False
        assert result["status"] == "ok"
        assert result["summary"] == "ANSWER::read paper A"
        assert result["nickname"] == "reader-1"
    finally:
        await control.aclose(grace_s=0.1)


@pytest.mark.asyncio
async def test_wait_is_idempotent_after_completion():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    try:
        nick = await control.spawn(SubagentSpec(goal="G"))
        first = await control.wait(nick, None)
        second = await control.wait(nick, None)
        assert first == second
    finally:
        await control.aclose(grace_s=0.1)


@pytest.mark.asyncio
async def test_wait_any_returns_first_finished():
    # Two children: one fast, one slow. ``wait(None)`` returns the fast one first.
    def responder(user: str) -> ChatWithToolsResult:
        return ChatWithToolsResult(content=f"done::{user}")

    ctx = _ctx(RoutingLLM(responder), max_active=4)
    control = _control(ctx)
    try:
        fast = await control.spawn(SubagentSpec(goal="fast"))
        # Slow child uses its own delayed LLM so ordering is deterministic.
        slow_ctx_llm = RoutingLLM(responder, delay=0.3)
        ctx.llm = slow_ctx_llm  # subsequent spawn clones ctx (with this llm)
        await control.spawn(SubagentSpec(goal="slow"))

        first = await control.wait(None, 2.0)
        assert first["timed_out"] is False
        assert first["nickname"] == fast
    finally:
        await control.aclose(grace_s=1.0)


# ── concurrency: fire-and-continue + session-tree cap ─────────────────────────


@pytest.mark.asyncio
async def test_async_subagents_overlap_instead_of_serializing():
    ctx = _ctx(_echo_llm(delay=0.2), max_active=4)
    control = _control(ctx)
    try:
        start = time.perf_counter()
        nicks = [await control.spawn(SubagentSpec(goal=f"t{i}")) for i in range(3)]
        # Collect all three; they should have run concurrently.
        for nick in nicks:
            await control.wait(nick, 2.0)
        elapsed = time.perf_counter() - start
        # Serial would be ~0.6s; concurrent is well under.
        assert elapsed < 0.45
    finally:
        await control.aclose(grace_s=0.1)


@pytest.mark.asyncio
async def test_max_active_caps_concurrent_execution():
    ctx = _ctx(_echo_llm(delay=0.2), max_active=1)
    control = _control(ctx)
    try:
        start = time.perf_counter()
        a = await control.spawn(SubagentSpec(goal="a"))
        b = await control.spawn(SubagentSpec(goal="b"))
        await control.wait(a, 2.0)
        await control.wait(b, 2.0)
        elapsed = time.perf_counter() - start
        # With a cap of 1 the two 0.2s children run back-to-back (~0.4s).
        assert elapsed >= 0.35
    finally:
        await control.aclose(grace_s=0.1)


# ── wait timeout ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_timeout_reports_timed_out_without_blocking_forever():
    ctx = _ctx(_echo_llm(delay=0.5))
    control = _control(ctx)
    try:
        nick = await control.spawn(SubagentSpec(goal="slow"))
        res = await control.wait(nick, 0.1)
        assert res["timed_out"] is True
        assert res["status"] == "running"
        assert res["nickname"] == nick
    finally:
        await control.aclose(grace_s=1.0)


# ── list + interrupt ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_reports_spawned_agents():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    try:
        await control.spawn(SubagentSpec(goal="alpha", role="reader"))
        await control.spawn(SubagentSpec(goal="beta", role="analyst"))
        listed = control.list()
        assert [a["nickname"] for a in listed] == ["reader-1", "analyst-2"]
        assert {a["role"] for a in listed} == {"reader", "analyst"}
    finally:
        await control.aclose(grace_s=0.1)


@pytest.mark.asyncio
async def test_interrupt_cancels_a_running_subagent():
    ctx = _ctx(_echo_llm(delay=0.5))
    control = _control(ctx)
    try:
        nick = await control.spawn(SubagentSpec(goal="long running"))
        await asyncio.sleep(0.05)  # let it enter the loop
        assert control.interrupt(nick) is True

        res = await control.wait(nick, 2.0)
        assert res["timed_out"] is False
        # Cooperative cancel yields a preserved-partial salvage, not a hang.
        assert "cancel" in res["summary"].lower()
    finally:
        await control.aclose(grace_s=0.5)


def test_interrupt_unknown_returns_false():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    assert control.interrupt("nope-1") is False


# ── teardown: no orphan tasks survive the turn ────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_leaves_no_orphan_tasks():
    ctx = _ctx(_echo_llm(delay=1.0))
    control = _control(ctx)
    # Spawn but deliberately never wait — aclose must reclaim it.
    await control.spawn(SubagentSpec(goal="dangling"))
    await control.spawn(SubagentSpec(goal="dangling2"))

    await control.aclose(grace_s=0.05)

    assert all(live.task is not None and live.task.done() for live in control._agents.values())
    leftover = [t for t in asyncio.all_tasks() if t.get_name().startswith("subagent:")]
    assert leftover == []


@pytest.mark.asyncio
async def test_spawn_after_close_is_refused():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    await control.aclose(grace_s=0.05)
    with pytest.raises(RuntimeError):
        await control.spawn(SubagentSpec(goal="too late"))


# ── tool surface gating (Codex-parity) ────────────────────────────────────────


def test_async_tools_offered_only_with_flag_and_control():
    # Flag on + control attached → coordinator gets the async verbs.
    ctx = _ctx(_echo_llm())
    _control(ctx)
    names = {t.spec.name for t in build_delegation_tools(ctx)}
    assert {
        "spawn_subagents",
        "spawn_subagent",
        "wait_subagent",
        "list_subagents",
        "interrupt_subagent",
        "message_subagent",
        "followup_subagent",
    } <= names


def test_async_tools_absent_without_control():
    # Flag on but no control (e.g. a specialist) → only the blocking batch tool.
    ctx = _ctx(_echo_llm())
    assert [t.spec.name for t in build_delegation_tools(ctx)] == ["spawn_subagents"]


def test_async_tools_absent_when_flag_off():
    ctx = _ctx(_echo_llm(), async_enabled=False)
    _control(ctx)  # even with a control, the flag gates the verbs
    assert [t.spec.name for t in build_delegation_tools(ctx)] == ["spawn_subagents"]


def test_child_context_clears_inherited_control():
    ctx = _ctx(_echo_llm())
    _control(ctx)
    child = _child_context(ctx, depth=0)
    assert child.subagent_control is None


# ── tool handlers end-to-end ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_and_wait_tool_handlers_roundtrip():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    tools = {t.spec.name: t for t in build_delegation_tools(ctx)}
    try:
        spawned = await tools["spawn_subagent"].handler({"goal": "read A", "role": "reader"})
        assert spawned["status"] == "ok"
        nickname = spawned["nickname"]

        waited = await tools["wait_subagent"].handler({"nickname": nickname})
        assert waited["status"] == "ok"
        assert waited["timed_out"] is False
        assert waited["subagent"]["summary"] == "ANSWER::read A"

        listed = await tools["list_subagents"].handler({})
        assert listed["status"] == "ok"
        assert listed["subagents"][0]["nickname"] == nickname
    finally:
        await control.aclose(grace_s=0.1)


@pytest.mark.asyncio
async def test_spawn_tool_rejects_missing_goal():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    tools = {t.spec.name: t for t in build_delegation_tools(ctx)}
    out = await tools["spawn_subagent"].handler({"role": "reader"})
    assert out["status"] == "error"
    await control.aclose(grace_s=0.1)


# ── P1b: message (steer a running subagent) ──────────────────────────────────


@pytest.mark.asyncio
async def test_message_steers_a_running_subagent():
    # The specialist echoes its last user message; steering is drained at the top
    # of the first iteration, so the delivered instruction shows up in its answer.
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    try:
        nick = await control.spawn(SubagentSpec(goal="work"))
        # Pushed synchronously before the child task runs → consumed on iteration 1.
        assert control.message(nick, "focus on X") is True
        res = await control.wait(nick, None)
        assert res["status"] == "ok"
        assert "focus on X" in res["summary"]
    finally:
        await control.aclose(grace_s=0.1)


@pytest.mark.asyncio
async def test_message_to_finished_or_unknown_returns_false():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    try:
        nick = await control.spawn(SubagentSpec(goal="quick"))
        await control.wait(nick, None)  # let it finish
        assert control.message(nick, "too late") is False
        assert control.message("ghost-9", "nobody home") is False
    finally:
        await control.aclose(grace_s=0.1)


# ── P1b: followup (continuation seeded with prior summary) ────────────────────


@pytest.mark.asyncio
async def test_followup_continues_finished_subagent_with_prior_summary():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    try:
        first = await control.spawn(SubagentSpec(goal="read A", role="reader"))
        r1 = await control.wait(first, None)
        assert r1["summary"] == "ANSWER::read A"

        second = await control.followup(first, "summarize deeper")
        assert second == "reader-2"
        r2 = await control.wait(second, None)
        # The continuation saw both its new instruction and the prior result.
        assert "summarize deeper" in r2["summary"]
        assert "ANSWER::read A" in r2["summary"]
    finally:
        await control.aclose(grace_s=0.1)


@pytest.mark.asyncio
async def test_followup_refuses_running_or_unknown():
    ctx = _ctx(_echo_llm(delay=0.5))
    control = _control(ctx)
    try:
        running = await control.spawn(SubagentSpec(goal="slow"))
        with pytest.raises(ValueError, match="still running"):
            await control.followup(running, "continue")
        with pytest.raises(ValueError, match="unknown"):
            await control.followup("ghost-9", "continue")
    finally:
        await control.aclose(grace_s=1.0)


# ── P1b: tool handlers ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_tool_handler_delivers_and_is_consumed():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    tools = {t.spec.name: t for t in build_delegation_tools(ctx)}
    try:
        spawned = await tools["spawn_subagent"].handler({"goal": "work"})
        msg = await tools["message_subagent"].handler(
            {"nickname": spawned["nickname"], "message": "focus here"}
        )
        assert msg["status"] == "ok" and msg["delivered"] is True
        waited = await tools["wait_subagent"].handler({"nickname": spawned["nickname"]})
        assert "focus here" in waited["subagent"]["summary"]
    finally:
        await control.aclose(grace_s=0.1)


@pytest.mark.asyncio
async def test_message_tool_handler_errors_on_finished():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    tools = {t.spec.name: t for t in build_delegation_tools(ctx)}
    try:
        spawned = await tools["spawn_subagent"].handler({"goal": "quick"})
        await tools["wait_subagent"].handler({"nickname": spawned["nickname"]})
        out = await tools["message_subagent"].handler(
            {"nickname": spawned["nickname"], "message": "late"}
        )
        assert out["status"] == "error"
    finally:
        await control.aclose(grace_s=0.1)


@pytest.mark.asyncio
async def test_followup_tool_handler_roundtrip():
    ctx = _ctx(_echo_llm())
    control = _control(ctx)
    tools = {t.spec.name: t for t in build_delegation_tools(ctx)}
    try:
        spawned = await tools["spawn_subagent"].handler({"goal": "read A", "role": "reader"})
        await tools["wait_subagent"].handler({"nickname": spawned["nickname"]})
        fu = await tools["followup_subagent"].handler(
            {"nickname": spawned["nickname"], "message": "go deeper"}
        )
        assert fu["status"] == "ok"
        r2 = await tools["wait_subagent"].handler({"nickname": fu["nickname"]})
        assert "go deeper" in r2["subagent"]["summary"]
        assert "ANSWER::read A" in r2["subagent"]["summary"]
    finally:
        await control.aclose(grace_s=0.1)


@pytest.mark.asyncio
async def test_followup_tool_handler_errors_on_running():
    ctx = _ctx(_echo_llm(delay=0.5))
    control = _control(ctx)
    tools = {t.spec.name: t for t in build_delegation_tools(ctx)}
    try:
        spawned = await tools["spawn_subagent"].handler({"goal": "slow"})
        out = await tools["followup_subagent"].handler(
            {"nickname": spawned["nickname"], "message": "continue"}
        )
        assert out["status"] == "error"
    finally:
        await control.aclose(grace_s=1.0)
