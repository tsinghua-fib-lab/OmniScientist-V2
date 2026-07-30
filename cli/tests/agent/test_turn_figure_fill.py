"""Host figure fill runs scientific-figure when this task still owes a figure.

Sibling-task PNGs are not in ``list_by_task(this_id)``, so they cannot skip the
fill. A figure already owned by this task_id must not be regenerated.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omni.agent.figure_runner import unrendered_authored_dot
from omni.agent.intent_plan import IntentPlan, IntentType, VerificationPlan
from omni.agent.turn_execution import TurnCompletion
from omni.core.react_agent import AgentLoopResult


def _plan() -> IntentPlan:
    return IntentPlan(
        task_id="eda313e1" + "0" * 24,
        user_message=(
            "为 智能体 系统综述准备材料：获取 Attention Is All You Need 摘要，"
            "并生成包含 query、key、value 的注意力机制示意图，以及一篇智能体系统综述论文。"
        ),
        intent_type=IntentType.REACT_FALLBACK,
        verification_plan=VerificationPlan(
            required_outputs=["artifact.figure", "draft.manuscript"]
        ),
    )


def _completion(*, artifacts: Any, runtime: Any, registry: Any = None) -> TurnCompletion:
    return TurnCompletion(
        tasks=SimpleNamespace(),
        task_controller=SimpleNamespace(),
        hooks=SimpleNamespace(),
        runtime=runtime,
        artifacts=artifacts,
        llm=object(),
        registry=registry,
    )


@pytest.mark.asyncio
async def test_owed_figure_is_filled_on_this_task() -> None:
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-figure-1"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(
                status="succeeded",
                error="",
                result_json={"ok": True},
                skill_name="scientific-figure",
            )
        ),
    )
    registry = SimpleNamespace(
        resolve_capability=lambda _cap: (SimpleNamespace(name="scientific-figure"), [])
    )
    completion = _completion(
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[])),
        runtime=runtime,
        registry=registry,
    )
    result = AgentLoopResult(kind="text", content="Old figures already exist.")
    drained: list[dict] = []

    notes = await completion._fill_remaining_figure(
        _plan(), result, drained, task_id=_plan().task_id, session_id="s1"
    )

    runtime.enqueue.assert_awaited_once()
    kwargs = runtime.enqueue.await_args
    assert kwargs.args[0] == "scientific-figure"
    assert kwargs.kwargs["task_id"] == _plan().task_id
    assert kwargs.kwargs["queue"] is False
    runtime.process.assert_awaited_once_with("sub-figure-1")
    assert any("artifact.figure" in note for note in notes)
    assert drained[0]["subtask_id"] == "sub-figure-1"


@pytest.mark.asyncio
async def test_this_task_figure_skips_host_fill() -> None:
    runtime = SimpleNamespace(enqueue=AsyncMock())
    owned = SimpleNamespace(
        kind="figure",
        title="Attention",
        rel_path="figures/attention.png",
        mime="image/png",
    )
    completion = _completion(
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[owned])),
        runtime=runtime,
    )

    notes = await completion._fill_remaining_figure(
        _plan(),
        AgentLoopResult(kind="text", content="done"),
        [],
        task_id=_plan().task_id,
        session_id="s1",
    )

    runtime.enqueue.assert_not_called()
    assert notes == []


@pytest.mark.asyncio
async def test_other_task_files_are_not_in_this_task_inventory() -> None:
    """list_by_task(this_id) is empty even when a sibling folder has a PNG."""
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-2"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(status="succeeded", error="", result_json={})
        ),
    )
    completion = _completion(
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[])),
        runtime=runtime,
    )

    await completion._fill_remaining_figure(
        _plan(),
        AgentLoopResult(kind="text", content="reused b1a5b1da"),
        [],
        task_id=_plan().task_id,
        session_id="s1",
    )

    runtime.enqueue.assert_awaited_once()


def test_unrendered_authored_dot_skips_skill_sidecar_and_keeps_custom() -> None:
    skill_stem = "Agent-Loop-Engineering-Closed-Loop-0b4d6b05-33c85420"
    rows = [
        SimpleNamespace(path=f"figures/{skill_stem}.dot", rel_path=""),
        SimpleNamespace(path=f"figures/{skill_stem}.png", rel_path=""),
        SimpleNamespace(path=f"figures/{skill_stem}.svg", rel_path=""),
        SimpleNamespace(path="figures/agent-loop-engineering-architecture.dot", rel_path=""),
    ]
    assert unrendered_authored_dot(rows) == "figures/agent-loop-engineering-architecture.dot"
    assert unrendered_authored_dot(rows[:3]) == ""


@pytest.mark.asyncio
async def test_host_fill_passes_unrendered_dot_to_the_figure_skill() -> None:
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-authored"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(status="succeeded", error="", result_json={})
        ),
    )
    custom = SimpleNamespace(
        kind="file",
        title="custom",
        path="figures/agent-loop-engineering-architecture.dot",
        rel_path="",
        mime="text/vnd.graphviz",
    )
    completion = _completion(
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[custom])),
        runtime=runtime,
    )

    await completion._fill_remaining_figure(
        _plan(),
        AgentLoopResult(kind="text", content="wrote a custom dot"),
        [],
        task_id=_plan().task_id,
        session_id="s1",
    )

    runtime.enqueue.assert_awaited_once()
    assert runtime.enqueue.await_args.kwargs["task_id"] == _plan().task_id
    params = runtime.enqueue.await_args.args[1]
    assert params["source_artifact_path"] == "figures/agent-loop-engineering-architecture.dot"
