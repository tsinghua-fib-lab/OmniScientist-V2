"""P1.1: nested ReAct loops must share the parent's ExecutionControl.

Regression guard that the subagent and self-review nested loops forward the
parent ``execution_control`` into ``react.run`` — otherwise a mid-turn user
cancel/steer never reaches the nested loop (and each loop spins up its own
empty control owner, breaking the "one shared, nestable instance" contract).
"""

from __future__ import annotations

from typing import Any

import pytest

from omni.core.execution_control import ExecutionControl
from omni.core.react_agent import AgentLoopResult


class _RecordingReact:
    """A fake ReAct agent that records the ``execution_control`` it was handed."""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def run(self, **kwargs: Any) -> AgentLoopResult:
        self.seen.append(kwargs.get("execution_control"))
        return AgentLoopResult(kind="text", content="draft answer")


@pytest.mark.asyncio
async def test_subagent_run_once_forwards_execution_control() -> None:
    from omni.agent.subagents import _run_once

    control = ExecutionControl(None)
    react = _RecordingReact()
    await _run_once(react, system="s", user="u", specs=[], execution_control=control)

    assert react.seen == [control]


@pytest.mark.asyncio
async def test_self_review_revision_forwards_execution_control(monkeypatch) -> None:  # noqa: ANN001
    import omni.agent.reviewer as reviewer

    control = ExecutionControl(None)
    react = _RecordingReact()

    async def _fake_review_output(*_a: Any, **_k: Any) -> reviewer.ReviewVerdict:
        return reviewer.ReviewVerdict("revise", 0.1, notes="add citations")

    # Force exactly one bounded revision, and stub best-effort cost recording.
    monkeypatch.setattr(reviewer, "review_output", _fake_review_output)
    monkeypatch.setattr(reviewer, "gate", lambda *_a, **_k: "revise")

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(reviewer, "_record_review_cost", _noop)
    monkeypatch.setattr(reviewer, "_record_loop_cost", _noop)

    class _Cfg:
        self_review = True
        self_review_min_score = 0.9
        self_review_max_revises = 1

    result = AgentLoopResult(kind="text", content="first draft")
    out = await reviewer.review_and_correct(
        llm=object(),
        cfg=_Cfg(),
        tasks=None,
        react=react,
        result=result,
        system="s",
        user_message="goal",
        tool_specs=[],
        history=[],
        task_id="t1",
        execution_control=control,
    )

    assert out.content == "draft answer"  # revision was applied
    assert react.seen == [control]  # and the control was shared with it
