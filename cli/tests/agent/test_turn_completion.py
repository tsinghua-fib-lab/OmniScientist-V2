"""Focused contracts for the extracted turn-completion boundary."""

from __future__ import annotations

from pathlib import Path
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

    async def apply_settlement(self, _task_id: str, result: Any) -> Any:
        self._timeline.append("settle")
        result.settlement_status = "succeeded"
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


class _Artifacts:
    async def list_by_task(self, task_id: str) -> list[Any]:
        return [
            SimpleNamespace(
                id="artifact-1",
                uri="artifact://artifact-1",
                title="Review",
                kind="report",
            )
        ] if task_id == "task-output" else []

    async def resolve_path(self, uri: str) -> Path | None:
        return Path("/workspace/reports/review_task-out/Review.md") if uri else None


class _RoleArtifacts:
    def __init__(self) -> None:
        self.rows = [
            SimpleNamespace(
                uri="artifact://survey",
                title="Data Provenance Survey",
                kind="report",
                meta={},
                mime="application/pdf",
                size_bytes=10,
            ),
            SimpleNamespace(
                uri="artifact://manifest-learning",
                title="Manifest Learning Report",
                kind="report",
                meta={},
                mime="text/markdown",
                size_bytes=20,
            ),
            SimpleNamespace(
                uri="artifact://provenance",
                title="Figure provenance",
                kind="figure",
                meta={},
                mime="application/json",
                size_bytes=30,
            ),
            SimpleNamespace(
                uri="artifact://declared-support",
                title="Machine receipt",
                kind="data",
                meta={"presentation_role": "support"},
                mime="application/json",
                size_bytes=40,
            ),
        ]
        self.paths = {
            "artifact://survey": Path("/workspace/reports/data-provenance-survey.pdf"),
            "artifact://manifest-learning": Path("/workspace/reports/manifest-learning.md"),
            "artifact://provenance": Path("/workspace/figures/rag.provenance.json"),
            "artifact://declared-support": Path("/workspace/data/receipt.json"),
        }

    async def list_by_task(self, _task_id: str) -> list[Any]:
        return self.rows

    async def resolve_path(self, uri: str) -> Path | None:
        return self.paths.get(uri)


def _completion_callbacks(recorder: _Recorder) -> dict[str, Any]:
    return {
        "persist_message": _Conversations(recorder.timeline).persist_message,
        "record_turn_memory": _TurnMemory(recorder.timeline).record,
        "apply_settlement": _TaskController(recorder.timeline).apply_settlement,
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
        "settle",
        "post_present",
    ]
    assert result.settlement_status == "succeeded"
    assert result.degraded_warnings == ["shared", "plan-only", "executor-only"]


@pytest.mark.asyncio
async def test_complete_plan_returns_authoritative_outputs_separately_from_answer() -> None:
    recorder = _Recorder()
    completion = TurnCompletion(
        tasks=recorder,
        task_controller=_TaskController(recorder.timeline),
        hooks=_Hooks(recorder.timeline),
        runtime=_Runtime(recorder.timeline),
        artifacts=_Artifacts(),
    )
    plan = IntentPlan(
        task_id="task-output",
        user_message="write a review",
        intent_type=IntentType.REACT_FALLBACK,
    )

    result = await completion.complete_plan(
        plan=plan,
        result=PlanExecutionResult(handled=True, text="Done."),
        session_id="session-output",
        user_message="write a review",
        drain_tasks=False,
        **_completion_callbacks(recorder),
    )

    assert result.text == "Done."
    assert len(result.artifacts) == 1
    assert result.artifacts[0].title == "Review"
    # Spelled through Path: the fixture hands in a Path, and completion reports
    # it with str(), so the separator is the platform's rather than the literal's.
    assert result.artifacts[0].path == str(
        Path("/workspace/reports/review_task-out/Review.md")
    )
    assert result.artifacts[0].uri == "artifact://artifact-1"


@pytest.mark.asyncio
async def test_artifact_roles_use_declared_metadata_and_exact_legacy_suffixes() -> None:
    recorder = _Recorder()
    completion = TurnCompletion(
        tasks=recorder,
        task_controller=_TaskController(recorder.timeline),
        hooks=_Hooks(recorder.timeline),
        runtime=_Runtime(recorder.timeline),
        artifacts=_RoleArtifacts(),
    )
    plan = IntentPlan(
        task_id="task-output",
        user_message="write reports",
        intent_type=IntentType.REACT_FALLBACK,
    )

    result = await completion.complete_plan(
        plan=plan,
        result=PlanExecutionResult(handled=True, text="Done."),
        session_id="session-output",
        user_message="write reports",
        drain_tasks=False,
        **_completion_callbacks(recorder),
    )

    roles = {artifact.title: artifact.presentation_role for artifact in result.artifacts}
    assert roles["Data Provenance Survey"] == "primary"
    assert roles["Manifest Learning Report"] == "primary"
    assert roles["Figure provenance"] == "support"
    assert roles["Machine receipt"] == "support"


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
        "drain",
        "pre_present",
        "persist",
        "memory",
        "finish",
        "settle",
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
async def test_bounded_text_result_keeps_one_degraded_status_everywhere() -> None:
    recorder = _Recorder()
    plan = IntentPlan(
        task_id="task-budget",
        user_message="review",
        intent_type=IntentType.REACT_FALLBACK,
    )
    loop_result = AgentLoopResult(
        kind="text",
        content="Best-effort review from gathered evidence.",
        terminated_reason="synthesized_max_total_tokens",
    )

    async def no_escalation(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def preserve_settlement(_task_id: str, result: Any) -> Any:
        recorder.timeline.append("settle")
        return result

    callbacks = _completion_callbacks(recorder)
    callbacks["apply_settlement"] = preserve_settlement
    result = await _completion(recorder).complete_react(
        plan=plan,
        result=loop_result,
        session_id="session-budget",
        user_message="review",
        channel="cli",
        drain_tasks=False,
        emit_tool_event=None,
        maybe_escalate=no_escalation,
        **callbacks,
    )

    finished = next(event for event in recorder.events if event["event_type"] == "react.finished")
    assert finished["status"] == "degraded"
    assert result.settlement_status == "degraded"


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
