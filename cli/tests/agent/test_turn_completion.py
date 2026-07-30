"""Focused contracts for the extracted turn-completion boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.interaction_lifecycle import InteractionLifecycle
from omni.agent.plan_result import PlanExecutionResult
from omni.agent.plan_revision import (
    create_revision,
    execution_authority_from_snapshot,
)
from omni.agent.turn_execution import TurnCompletion
from omni.core.react_agent import AgentLoopResult, ToolInvocationRecord


class _Recorder:
    def __init__(self) -> None:
        self.timeline: list[str] = []
        self.events: list[dict[str, Any]] = []

    async def append_event(self, _task_id: str, **event: Any) -> None:
        self.timeline.append(f"event:{event['event_type']}")
        self.events.append(event)

    async def mark_awaiting_approval(self, *_args: Any, **_kwargs: Any) -> bool:
        self.timeline.append("mark_awaiting")
        return True


class _Hooks:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    async def emit(self, event: str, **_kwargs: Any) -> None:
        self._timeline.append(event)


class _Conversations:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    async def persist_message(self, *_args: Any, **_kwargs: Any) -> None:
        self._timeline.append("persist")


class _TurnMemory:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    async def record(self, *_args: Any, **_kwargs: Any) -> None:
        self._timeline.append("memory")


class _TaskController:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    async def finish_turn(self, *_args: Any, **_kwargs: Any) -> None:
        self._timeline.append("finish")

    async def apply_verifier_outcome(self, _task_id: str, result: Any) -> Any:
        self._timeline.append("verify")
        result.verification_status = "passed"
        return result


class _Runtime:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    async def drain(self, **_kwargs: Any) -> None:
        self._timeline.append("drain")

    async def get_workflow_run(self, workflow_id: str) -> Any:
        return SimpleNamespace(
            status="succeeded",
            result_json={"id": workflow_id},
            error="",
            trace_log=[],
        )

    async def get_subtask(self, subtask_id: str) -> Any:
        return SimpleNamespace(
            skill_name="demo",
            status="succeeded",
            result_json={"id": subtask_id},
            error="",
            trace_log=[],
        )


def _completion(recorder: _Recorder) -> TurnCompletion:
    return TurnCompletion(
        tasks=recorder,
        task_controller=_TaskController(recorder.timeline),
        hooks=_Hooks(recorder.timeline),
        runtime=_Runtime(recorder.timeline),
    )


def _completion_callbacks(recorder: _Recorder) -> dict[str, Any]:
    return {
        "persist_message": _Conversations(recorder.timeline).persist_message,
        "record_turn_memory": _TurnMemory(recorder.timeline).record,
        "apply_verifier_outcome": _TaskController(
            recorder.timeline
        ).apply_verifier_outcome,
    }


@pytest.mark.asyncio
async def test_complete_plan_preserves_settlement_order_and_warning_union() -> None:
    recorder = _Recorder()
    plan = IntentPlan(
        task_id="task-1",
        user_message="run",
        intent_type=IntentType.REACT_FALLBACK,
        degraded_warnings=["shared", "plan-only"],
    )
    execution = PlanExecutionResult(
        handled=True,
        text="done",
        degraded_warnings=["shared", "executor-only"],
    )

    result = await _completion(recorder).complete_plan(
        plan=plan,
        result=execution,
        session_id="session-1",
        user_message="run",
        drain_tasks=False,
        **_completion_callbacks(recorder),
    )

    assert recorder.timeline == [
        "pre_present",
        "persist",
        "memory",
        "finish",
        "verify",
        "post_present",
    ]
    assert result.verification_status == "passed"
    assert result.degraded_warnings == ["shared", "plan-only", "executor-only"]


@pytest.mark.asyncio
async def test_complete_react_records_terminal_event_and_drains_before_settlement() -> None:
    recorder = _Recorder()
    plan = IntentPlan(
        task_id="task-2",
        user_message="run",
        intent_type=IntentType.REACT_FALLBACK,
    )
    loop_result = AgentLoopResult(
        kind="text",
        content="done",
        tool_trace=[
            ToolInvocationRecord(
                name="run_workflow",
                arguments={},
                result={
                    "status": "submitted",
                    "workflow_run_id": "workflow-1",
                    "subtask_id": "subtask-1",
                },
            )
        ],
    )

    async def no_escalation(*_args: Any, **_kwargs: Any) -> None:
        return None

    result = await _completion(recorder).complete_react(
        plan=plan,
        result=loop_result,
        session_id="session-2",
        user_message="run",
        channel="cli",
        drain_tasks=True,
        emit_tool_event=None,
        maybe_escalate=no_escalation,
        **_completion_callbacks(recorder),
    )

    assert recorder.timeline == [
        "event:react.finished",
        "pre_present",
        "persist",
        "memory",
        "drain",
        "finish",
        "verify",
        "post_present",
    ]
    assert result.submitted_workflow_ids == ["workflow-1"]
    assert result.submitted_subtask_ids == ["subtask-1"]
    assert {item.get("kind", "subtask") for item in result.drained_results} == {
        "workflow",
        "subtask",
    }
    workflow = next(
        item for item in result.drained_results if item.get("kind") == "workflow"
    )
    assert workflow["task_id"] == "task-2"
    assert workflow["object_kind"] == "workflow_run"
    assert workflow["object_id"] == "workflow-1"
    execution = next(
        item for item in result.drained_results if item.get("subtask_id")
    )
    assert execution["task_id"] == "task-2"
    assert execution["object_kind"] == "skill_execution"
    assert execution["object_id"] == "subtask-1"


@pytest.mark.asyncio
async def test_interaction_gate_invalidates_stale_authority_before_reapproval() -> None:
    recorder = _Recorder()
    lifecycle = InteractionLifecycle(
        SimpleNamespace(),
        recorder,
        _Hooks(recorder.timeline),
        SimpleNamespace(),
    )
    revision = create_revision(
        IntentPlan(
            task_id="task-3",
            user_message="run",
            intent_type=IntentType.REACT_FALLBACK,
        ),
        revision=1,
        source="test",
    )
    plan = revision.plan
    current = execution_authority_from_snapshot(
        plan,
        catalog_hash="current",
        contract_hash="contract",
    )
    stale = execution_authority_from_snapshot(
        plan,
        catalog_hash="stale",
        contract_hash="contract",
    )

    async def persist(*_args: Any, **_kwargs: Any) -> None:
        recorder.timeline.append("persist")

    async def forward(_callback: Any, event: dict[str, Any]) -> None:
        recorder.timeline.append(f"forward:{event['event_type']}")

    result = await lifecycle.gate_plan_execution(
        plan=plan,
        authority=current,
        revision=revision,
        approval_bound_hash=stale.plan_hash,
        approved_plan=plan,
        approved_authority=stale,
        mode="auto",
        session_id="session-3",
        persist_message=persist,
        on_tool_event=None,
        forward=forward,
    )

    assert result is not None
    assert result.terminated_reason == "approval_invalidated"
    assert [event["event_type"] for event in recorder.events] == [
        "plan.approval.invalidated",
        "plan.approval.requested",
    ]
    assert recorder.timeline == [
        "event:plan.approval.invalidated",
        "forward:plan.approval.invalidated",
        "mark_awaiting",
        "event:plan.approval.requested",
        "persist",
        "pre_present",
        "post_present",
    ]
