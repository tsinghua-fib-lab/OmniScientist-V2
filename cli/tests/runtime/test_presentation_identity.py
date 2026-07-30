"""Canonical Task identity across completion and notification presentation."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from omni.agent import OmniAgent
from omni.channels.base import Channel
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.runtime.notifications import TaskNotification, task_notification_from_dict
from omni.runtime.presentation import (
    ArtifactRef,
    default_task_actions,
    task_presentation_from_notification,
    task_presentation_from_result,
    turn_presentation_from_result,
)
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from tests.conftest import ScriptedLLM


def _presentation_skill(name: str) -> SkillEntry:
    """A trivial async skill so a model-submitted workflow step can bind."""
    return SkillEntry(
        name=name,
        description=f"presentation fixture {name}",
        source="project_omni",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(
            command=sys.executable,
            args=["-c", "print('{\"status\":\"ok\"}')"],
            stdout_format="json",
        ),
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"status": {"type": "string"}}},
    )


def _workflow_turn() -> SimpleNamespace:
    return SimpleNamespace(
        task_id="05571218b61b4f1aab86fd83a660c75e",
        session_id="session123456789",
        submitted_workflow_ids=["f4902f1686924dd9a74efa920bbc6626"],
        submitted_subtask_ids=[],
        drained_results=[
            {
                "workflow_run_id": "f4902f1686924dd9a74efa920bbc6626",
                "kind": "workflow",
                "status": "succeeded",
                "result": {"summary": "Workflow complete."},
                "trace": [],
            }
        ],
        text="",
        kind="workflow",
        terminated_reason="workflow",
        plan_summary="",
        degraded_warnings=[],
        settlement_status="passed",
    )


def test_turn_presentation_projects_internal_artifact_uris_for_each_channel() -> None:
    artifact = ArtifactRef(
        title="RAG survey",
        format="md",
        uri="artifact://paper1",
        path="/workspace/papers/rag.md",
    )
    turn = SimpleNamespace(
        text="Paper: artifact://paper1",
        task_id="task-1",
        session_id="session-1",
        kind="text",
        terminated_reason="done",
        plan_summary="",
        degraded_warnings=[],
        settlement_status="",
        submitted_workflow_ids=[],
        submitted_subtask_ids=[],
        drained_results=[],
        artifacts=[artifact],
    )

    cli = turn_presentation_from_result(turn, channel="cli")
    im = turn_presentation_from_result(turn, channel="feishu")

    assert cli.assistant_text == "Paper: /workspace/papers/rag.md"
    assert im.assistant_text == "Paper: rag.md"
    assert "artifact://" not in cli.to_markdown()
    assert "artifact://" not in im.to_markdown(include_local_paths=False)
    assert cli.to_markdown().count("**Outputs**") == 1


def test_workflow_completion_keeps_object_identity_but_actions_use_owning_task() -> None:
    presentation = turn_presentation_from_result(_workflow_turn())
    completion = presentation.tasks[0]

    assert completion.task_id == "05571218b61b4f1aab86fd83a660c75e"
    assert completion.object_kind == "workflow_run"
    assert completion.object_id == "f4902f1686924dd9a74efa920bbc6626"
    assert completion.subtask_id == completion.object_id  # compatibility alias

    markdown = completion.to_markdown()
    assert "`workflow=f4902f16`" in markdown
    assert "`task=05571218`" in markdown
    assert "/task show 05571218" in markdown
    assert "/task attach 05571218" in markdown
    assert "/task show f4902f16: inspect this workflow run" in markdown


def test_completion_uses_payload_owner_when_outer_turn_lacks_task_id() -> None:
    turn = _workflow_turn()
    turn.task_id = ""
    turn.drained_results[0]["task_id"] = "05571218b61b4f1aab86fd83a660c75e"

    completion = turn_presentation_from_result(turn).tasks[0]

    assert completion.task_id == "05571218b61b4f1aab86fd83a660c75e"
    assert "/task show 05571218" in completion.to_markdown()


def test_missing_identity_never_generates_incomplete_task_commands() -> None:
    presentation = task_presentation_from_result(
        subtask_id="",
        task_id="",
        object_kind="workflow_run",
        object_id="",
        skill="Research workflow",
        status="failed",
        error="workflow disappeared",
    )

    markdown = presentation.to_markdown()
    assert "/task show :" not in markdown
    assert "/task attach :" not in markdown
    assert "/task show " not in markdown
    assert "/task attach " not in markdown
    assert default_task_actions("") == [
        "/verify --session: audit this session's research claims and evidence"
    ]


def test_object_identity_without_owner_never_generates_task_navigation() -> None:
    presentation = task_presentation_from_result(
        subtask_id="f4902f1686924dd9a74efa920bbc6626",
        task_id="",
        object_kind="workflow_run",
        object_id="f4902f1686924dd9a74efa920bbc6626",
        skill="Research workflow",
        status="failed",
        error="owning task unavailable",
    )

    markdown = presentation.to_markdown()
    assert "`workflow=f4902f16`" in markdown
    assert "`task=" not in markdown
    assert "/task show " not in markdown
    assert "/task attach " not in markdown


def test_failed_result_surfaces_setup_action_and_explicit_artifact_absence() -> None:
    presentation = task_presentation_from_result(
        subtask_id="paper-review-execution",
        task_id="paper-review-task",
        skill="paper-review",
        status="failed",
        result={
            "summary": "A Semantic Scholar API key is required.",
            "setup_command": "omni config semantic-scholar -k <API_KEY> --test",
            "next_actions": ["omni config semantic-scholar -k <API_KEY> --test"],
        },
        error="configuration missing",
    )

    markdown = presentation.to_markdown()

    assert "No saved artifact was produced" in markdown
    assert "omni config semantic-scholar -k <API_KEY> --test" in markdown
    assert markdown.count("omni config semantic-scholar -k <API_KEY> --test") == 1


def test_notification_round_trip_preserves_owner_and_old_json_still_loads() -> None:
    note = task_notification_from_dict(
        {
            "task_id": "05571218b61b4f1aab86fd83a660c75e",
            "subtask_id": "",
            "skill_name": "",
            "status": "succeeded",
            "object_kind": "workflow_run",
            "object_id": "f4902f1686924dd9a74efa920bbc6626",
            "title": "Research workflow",
        }
    )

    assert note.task_id == "05571218b61b4f1aab86fd83a660c75e"
    rendered = task_presentation_from_notification(note)
    assert rendered.task_id == note.task_id
    assert rendered.object_kind == "workflow_run"
    assert "/task attach 05571218" in rendered.to_markdown()

    legacy = task_notification_from_dict(
        {
            "subtask_id": "skill-execution-123",
            "skill_name": "literature-search",
            "status": "succeeded",
        }
    )
    assert legacy.task_id == ""
    assert legacy.reference_id == "skill-execution-123"


def test_scheduled_goal_notification_infers_task_owner_for_legacy_producer() -> None:
    note = TaskNotification(
        subtask_id="task-owner-123",
        skill_name="scheduled-goal",
        status="succeeded",
        object_kind="scheduled_goal",
        object_id="task-owner-123",
    )

    assert note.task_id == "task-owner-123"


def test_scheduled_goal_without_run_task_does_not_treat_schedule_as_task() -> None:
    note = TaskNotification(
        subtask_id="",
        skill_name="scheduled-goal",
        status="failed",
        object_kind="scheduled_goal",
        object_id="schedule-definition-123",
    )

    assert note.task_id == ""
    markdown = task_presentation_from_notification(note).to_markdown()
    assert "/task show " not in markdown
    assert "/task attach " not in markdown


@pytest.mark.asyncio
async def test_channel_delivery_uses_notification_task_id_without_object_lookup(settings) -> None:
    class _Runs:
        def __init__(self) -> None:
            self.claims: list[dict] = []
            self.events: list[dict] = []

        async def claim_delivery(self, _key: str, **kwargs):  # noqa: ANN001
            self.claims.append(kwargs)
            return True

        async def finish_delivery(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return None

        async def append_event(self, task_id: str, **kwargs):  # noqa: ANN001
            self.events.append({"task_id": task_id, **kwargs})

    class _Runtime:
        async def get_workflow_run(self, _object_id: str):  # noqa: ANN001
            raise AssertionError("canonical notification owner should avoid lookup")

    class _Agent:
        def __init__(self) -> None:
            self.tasks = _Runs()
            self.runtime = _Runtime()

    class _Channel(Channel):
        name = "dummy"

        async def start(self) -> None:
            return None

    agent = _Agent()
    channel = _Channel(settings, agent)  # type: ignore[arg-type]
    note = TaskNotification(
        task_id="task-owner-123",
        subtask_id="",
        skill_name="",
        status="succeeded",
        object_kind="workflow_run",
        object_id="workflow-456",
        channel="dummy",
        external_key="chat-1",
    )

    await channel.send_task_notification(note)

    assert agent.tasks.claims[0]["task_id"] == "task-owner-123"
    assert agent.tasks.events[-1]["task_id"] == "task-owner-123"


@pytest.mark.asyncio
async def test_model_submitted_workflow_keeps_task_identity_and_drill_down() -> None:
    """A model-submitted run stays addressable by ``/task show``.

    The model, not the planner, now decides the steps, so the identity contract
    is carried by the ``run_workflow`` tool result. It must still name the
    owning task and the workflow run, and the turn presentation must still
    offer the owning-task actions plus the workflow drill-down that
    ``/task show`` depends on.
    """
    agent = await OmniAgent.create(load_settings())
    agent.registry.register(_presentation_skill("literature-search"))
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="call_workflow",
                        name="run_workflow",
                        arguments={
                            "goal": "Run a research workflow.",
                            "mode": "background",
                            "steps": [
                                {
                                    "id": "search",
                                    "skill": "literature-search",
                                    "input": {"query": "RAG"},
                                }
                            ],
                        },
                    )
                ]
            )
        ]
    )

    try:
        turn = await agent.handle_turn("Run a research workflow.", drain_tasks=False)
    finally:
        await agent.aclose()

    workflow_run_id = turn.submitted_workflow_ids[0]
    submission = turn.tool_trace[0].result
    assert submission["task_id"] == turn.task_id
    assert submission["object_kind"] == "workflow_run"
    assert submission["object_id"] == workflow_run_id

    presentation = turn_presentation_from_result(turn)
    assert presentation.submitted_workflow_ids == [workflow_run_id]
    assert any(
        action.startswith(f"/task show {turn.task_id[:8]}:")
        for action in presentation.next_actions
    )
    assert (
        f"/task show {workflow_run_id[:8]}: inspect this workflow run"
        in presentation.next_actions
    )
