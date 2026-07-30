"""ReAct turn: a slow human approval must not consume the turn deadline.

Regression for the deployed bug (gitee IK4HNB): bash approval waiting was
charged against the same absolute ``react.max_seconds`` budget as model/tool
time, so after the owner approved and the command *succeeded* the next loop
iteration found the deadline already passed and marked the task timeout/failed.

The fix pauses the turn clock for exactly the approval latency. These tests
drive a real ReAct loop with a scripted LLM and an approver that sleeps longer
than the turn budget, and assert the turn still completes. The control test
patches the pause to a no-op and shows the original timeout, proving the pause
is the operative fix. A parallel-batch case covers one branch waiting while
another runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

from omni.config import load_settings
from omni.core import approval as approval_mod
from omni.core.approval import ApprovalDecision, ApprovalGate
from omni.core.execution_control import ExecutionControl
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.termination import base_termination_reason
from omni.core.turn_clock import TurnClock, register_clock
from omni.skills_runtime.executor import _remaining_timeout
from tests.conftest import ScriptedLLM

BASH = ToolSpec(
    "bash", "run a shell command",
    {"type": "object", "properties": {"command": {"type": "string"}}},
)
ECHO = ToolSpec(
    "echo", "echo back",
    {"type": "object", "properties": {"x": {"type": "string"}}},
)

_SENSITIVE = {"bash", "write_file", "edit_file", "run_compute"}


def _settings():
    s = load_settings()
    s.security.require_approval = True
    s.security.approval_policy = "always"
    return s


def _slow_approver(delay: float):
    async def approver(_req):
        await asyncio.sleep(delay)  # human thinking time (wall clock)
        return ApprovalDecision(True, scope="once", reason="approved-once")

    return approver


def _invoker_through_gate(gate: ApprovalGate):
    async def real(name, args):
        if name == "bash":
            return {"stdout": "ran", "exit_code": 0}
        if name == "echo":
            return {"echoed": args.get("x")}
        raise ValueError(name)

    async def invoker(name, args):
        return await gate.invoke(
            name, args, lambda: real(name, args), sensitive=name in _SENSITIVE,
        )

    return invoker


@pytest.mark.asyncio
async def test_slow_approval_does_not_time_out_a_successful_turn():
    gate = ApprovalGate(_settings(), approver=_slow_approver(1.3))
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "bash", {"command": "sleep 0"})]),
        ChatWithToolsResult(content="done"),
    ])
    # Budget (1.0s) is deliberately shorter than the approval wait (1.3s).
    agent = ReActLoopAgent(llm, _invoker_through_gate(gate), max_iterations=4, max_seconds=1.0)
    res = await agent.run(system_prompt="sys", user_message="go", tools=[BASH])

    assert res.kind == "text"
    assert res.content == "done"
    assert res.tool_names() == ["bash"]
    assert res.tool_trace[0].status == "succeeded"


@pytest.mark.asyncio
async def test_without_the_pause_the_slow_approval_hits_the_deadline_gracefully(monkeypatch):
    """Control: neutralise the pause and the deadline *is* hit after approval.

    With the redone timeout semantics, hitting the ceiling is no longer a
    failure: the completed command is retained and the loop forces a best-effort
    synthesis, settling ``degraded`` (base reason ``timeout``) — never
    ``error``. The pause (the primary fix) is what lets the *successful* turn in
    the sibling test avoid the deadline entirely; here we prove that even the
    unpaused deadline degrades gracefully instead of discarding the result.
    """

    @contextlib.contextmanager
    def _noop():
        yield

    monkeypatch.setattr(approval_mod, "pause_clocks", _noop)

    gate = ApprovalGate(_settings(), approver=_slow_approver(1.3))
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "bash", {"command": "sleep 0"})]),
        ChatWithToolsResult(content="best-effort answer over the completed command"),
    ])
    agent = ReActLoopAgent(llm, _invoker_through_gate(gate), max_iterations=4, max_seconds=1.0)
    res = await agent.run(system_prompt="sys", user_message="go", tools=[BASH])

    # The command still ran and is retained; the deadline degrades, never fails.
    assert res.tool_trace[0].status == "succeeded"
    assert res.kind != "error"
    assert base_termination_reason(res.terminated_reason) == "timeout"


@pytest.mark.asyncio
async def test_parallel_batch_one_branch_waits_for_approval():
    gate = ApprovalGate(_settings(), approver=_slow_approver(1.3))
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[
            ToolCall("c1", "bash", {"command": "x"}),
            ToolCall("c2", "echo", {"x": "hi"}),
        ]),
        ChatWithToolsResult(content="both done"),
    ])
    agent = ReActLoopAgent(llm, _invoker_through_gate(gate), max_iterations=4, max_seconds=1.0)
    res = await agent.run(system_prompt="sys", user_message="go", tools=[BASH, ECHO])

    assert res.kind == "text"
    assert set(res.tool_names()) == {"bash", "echo"}
    assert all(rec.status == "succeeded" for rec in res.tool_trace)


@pytest.mark.asyncio
async def test_cancel_during_approval_returns_promptly_as_cancelled():
    """Path f: cancelling mid-approval stops at once and marks the turn
    *cancelled* (not failed/timeout), without waiting out the human decision."""
    polls = {"n": 0}

    def read_controls():
        polls["n"] += 1
        # Let the approval start, then cancel while it is still waiting.
        return [{"action": "cancel"}] if polls["n"] >= 3 else []

    control = ExecutionControl(read_controls, poll_interval=0.02)
    gate = ApprovalGate(_settings(), approver=_slow_approver(3.0))
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "bash", {"command": "x"})]),
        ChatWithToolsResult(content="must-not-reach"),
    ])
    agent = ReActLoopAgent(llm, _invoker_through_gate(gate), max_iterations=4, max_seconds=30.0)

    t0 = time.monotonic()
    res = await agent.run(
        system_prompt="sys", user_message="go", tools=[BASH], execution_control=control,
    )
    elapsed = time.monotonic() - t0

    assert res.terminated_reason == "cancelled"
    assert res.kind != "error"
    assert elapsed < 1.5  # returned promptly, not after the 3s approval wait


@pytest.mark.asyncio
async def test_workflow_envelope_clock_credits_step_approval_wait():
    """Path d: a step's approval wait pauses the workflow envelope clock, so a
    tiny envelope survives a longer approval and later steps keep a live budget.

    Exercises the real pieces the workflow runtime relies on: ``register_clock``
    (the wave scope), the actual ``ApprovalGate`` pause, and ``_remaining_timeout``
    reading the live ``execution_clock``.
    """
    workflow_clock = TurnClock(0.2)  # deliberately shorter than the approval
    gate = ApprovalGate(_settings(), approver=_slow_approver(0.6))

    async def real():
        return {"stdout": "ok", "exit_code": 0}

    with register_clock(workflow_clock):
        result = await gate.invoke("bash", {"command": "x"}, real, sensitive=True)

    assert result["exit_code"] == 0
    assert not workflow_clock.expired()  # the 0.6s wait was credited to the 0.2s envelope
    ctx = SimpleNamespace(execution_clock=workflow_clock)
    assert _remaining_timeout(ctx, 5.0) > 0  # a later step still gets a positive budget
