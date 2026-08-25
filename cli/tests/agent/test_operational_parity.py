"""User-facing operational invariants across channels and daemon retries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType, SkillSelection
from omni.agent.orchestrator import OmniAgent, TurnResult
from omni.agent.plan_revision import (
    create_execution_authority,
    create_revision,
)
from omni.agent.plan_runner_utils import approval_tools_for_plan
from omni.channels.base import Channel
from omni.channels.commands import handle_channel_command
from omni.config import load_settings
from omni.memory.service import MemoryLayer
from omni.runtime.notifications import TaskNotification
from omni.runtime.presentation import ArtifactRef, TaskPresentation, TurnPresentation
from omni.runtime.taskref import is_bare_task_id
from tests.conftest import ScriptedLLM


class _RecordingChannel(Channel):
    name = "wechat"

    def __init__(self, settings, agent) -> None:  # noqa: ANN001
        super().__init__(settings, agent)
        self.sent: list[tuple[str, Any]] = []

    async def start(self) -> None:
        return None

    async def send_turn(self, external_key: str, presentation) -> None:  # noqa: ANN001
        self.sent.append((external_key, presentation))


def test_approval_scope_uses_forced_skill_source_not_same_name_winner() -> None:
    winner = SimpleNamespace(
        name="shared-provider",
        execution={"requires_approval": False},
        allowed_tools=[],
    )
    forced = SimpleNamespace(
        name="shared-provider",
        execution={"requires_approval": True},
        allowed_tools=["bash"],
    )

    class Registry:
        def get(self, name: str):  # noqa: ANN201
            return winner if name == "shared-provider" else None

        def resolve_ref(self, name: str, source: str):  # noqa: ANN201
            if name == "shared-provider" and source == "user_omni":
                return forced
            return self.get(name)

    plan = IntentPlan(
        task_id="forced-sensitive-provider",
        user_message="run $user:shared-provider",
        intent_type=IntentType.SINGLE_SKILL_TASK,
        selected_skills=[
            SkillSelection(
                skill="shared-provider",
                skill_source="user_omni",
                reason="explicit source",
            )
        ],
    )

    assert approval_tools_for_plan(plan, Registry()) == [
        "bash",
        "shared-provider",
    ]


async def _pause_with_exact_authority(
    agent: OmniAgent,
    plan: IntentPlan,
) -> IntentPlan:
    sealed = create_revision(
        plan,
        revision=0,
        source="test",
        stage="accepted",
    ).plan
    tools = approval_tools_for_plan(sealed, agent.registry)
    authority = create_execution_authority(
        sealed,
        registry=agent.registry,
        approval_tools=tools,
    )
    await agent.tasks.record_plan(
        sealed.task_id,
        sealed,
        status="validated",
        emit_event=False,
        current_authority_fingerprint=authority.fingerprint,
    )
    assert await agent.tasks.mark_awaiting_approval(
        sealed.task_id,
        summary="plan",
        authority_fingerprint=authority.fingerprint,
        expected_plan_json=sealed.to_dict(),
    )
    return sealed


@pytest.mark.asyncio
async def test_im_run_controls_are_session_scoped(monkeypatch) -> None:  # noqa: ANN001
    agent = await OmniAgent.create(load_settings())
    session_a = await agent.ensure_session(channel="wechat", external_key="user-a")
    session_b = await agent.ensure_session(channel="wechat", external_key="user-b")
    run = await agent.tasks.create_task(
        session_id=session_a,
        channel="wechat",
        user_input="long research",
    )
    await agent.tasks.record_plan(
        run.id,
        {
            "intent_type": "react_fallback",
            "user_message": run.user_input,
        },
        status="validated",
        emit_event=False,
    )
    try:
        denied = await handle_channel_command(agent, f"/task cancel {run.id[:8]}", session_b)
        assert denied is not None and "does not belong to this session" in denied.assistant_text

        steered = await handle_channel_command(
            agent,
            f"/task steer {run.id[:8]} prioritize citations",
            session_a,
        )
        assert steered is not None and "Steering instruction submitted" in steered.assistant_text

        cancelled = await handle_channel_command(agent, f"/task cancel {run.id[:8]}", session_a)
        assert cancelled is not None and "Cancellation requested" in cancelled.assistant_text
        controls = await agent.tasks.consume_controls(run.id)
        assert [item["action"] for item in controls] == ["steer", "cancel"]

        async def fake_approve(task_id: str, *, drain_tasks: bool = False) -> TurnResult:
            assert task_id == run.id
            assert drain_tasks is False
            return TurnResult(text="approved", session_id=session_a, task_id=run.id)

        monkeypatch.setattr(agent, "approve_task", fake_approve)
        await _pause_with_exact_authority(
            agent,
            IntentPlan(
                task_id=run.id,
                user_message=run.user_input,
                intent_type=IntentType.DIRECT_ANSWER,
            ),
        )
        approved = await handle_channel_command(agent, f"/task approve {run.id[:8]}", session_a)
        assert approved is not None and approved.assistant_text == "approved"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["wechat", "feishu", "dingtalk"])
async def test_bare_task_id_on_im_is_inspect_not_a_new_turn(channel: str) -> None:
    agent = await OmniAgent.create(load_settings())
    session = await agent.ensure_session(channel=channel, external_key="user-a")
    run = await agent.tasks.create_task(
        session_id=session,
        channel=channel,
        user_input="long research",
    )
    try:
        # An 8-char hex prefix with no digit is not a task id (taskref requires
        # a digit). Paste the shortest prefix the host would accept, same as
        # WeChat users pasting the ACK token.
        ref = next(
            run.id[:n]
            for n in range(8, len(run.id) + 1)
            if is_bare_task_id(run.id[:n])
        )
        shown = await handle_channel_command(agent, ref, session)
        assert shown is not None
        assert f"Task `{run.id[:8]}`" in shown.assistant_text
        unknown = await handle_channel_command(agent, "6978342b", session)
        assert unknown is None
        generative = await handle_channel_command(
            agent, f"基于 {run.id[:8]} 继续写论文", session
        )
        assert generative is None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_im_stop_and_steer_resolve_the_active_task_in_this_session() -> None:
    agent = await OmniAgent.create(load_settings())
    session_a = await agent.ensure_session(channel="wechat", external_key="user-a")
    session_b = await agent.ensure_session(channel="wechat", external_key="user-b")
    run = await agent.tasks.create_task(
        session_id=session_a,
        channel="wechat",
        user_input="long research",
    )
    await agent.tasks.record_plan(
        run.id,
        {
            "intent_type": "react_fallback",
            "user_message": run.user_input,
        },
        status="validated",
        emit_event=False,
    )
    try:
        missing = await handle_channel_command(agent, "/stop", session_b)
        assert missing is not None and "No active task" in missing.assistant_text

        steered = await handle_channel_command(agent, "/steer focus on citations", session_a)
        assert steered is not None and run.id[:8] in steered.assistant_text

        stopped = await handle_channel_command(agent, "/stop", session_a)
        assert stopped is not None and run.id[:8] in stopped.assistant_text
        controls = await agent.tasks.consume_controls(run.id)
        assert [item["action"] for item in controls] == ["steer", "cancel"]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_plan_approval_persists_only_manifest_declared_sensitive_tools(
    monkeypatch,
) -> None:  # noqa: ANN001
    agent = await OmniAgent.create(load_settings())
    session_id = await agent.ensure_session(channel="wechat", external_key="user-a")
    run = await agent.tasks.create_task(
        session_id=session_id,
        channel="wechat",
        user_input="生成科研架构图",
    )
    plan = IntentPlan(
        task_id=run.id,
        user_message=run.user_input,
        intent_type=IntentType.SINGLE_SKILL_TASK,
        selected_skills=[SkillSelection(skill="scientific-figure", reason="architecture figure")],
    )
    await _pause_with_exact_authority(agent, plan)

    async def fake_execute(
        _user_message: str,
        **kwargs: Any,
    ) -> TurnResult:
        return TurnResult(
            text="approved",
            session_id=session_id,
            task_id=kwargs["existing_task_id"],
        )

    monkeypatch.setattr(agent, "handle_turn", fake_execute)
    try:
        await agent.approve_task(run.id, drain_tasks=False)
        stored = await agent.tasks.get_task(run.id)
    finally:
        await agent.aclose()

    assert stored is not None
    assert set(stored.approved_tools) == {"bash", "write_file"}
    assert "edit_file" not in stored.approved_tools


@pytest.mark.asyncio
async def test_delivery_claim_is_atomic_and_retryable() -> None:
    agent = await OmniAgent.create(load_settings())
    try:
        claims = await asyncio.gather(*(
            agent.tasks.claim_delivery(
                "delivery-key",
                task_id="run-1",
                subtask_id="task-1",
                channel="wechat",
                external_key="user-a",
                kind="task_notification",
            )
            for _ in range(20)
        ))
        assert sum(claims) == 1
        await agent.tasks.finish_delivery("delivery-key", status="sent")
        assert not await agent.tasks.claim_delivery(
            "delivery-key",
            task_id="run-1",
            subtask_id="task-1",
            channel="wechat",
            external_key="user-a",
            kind="task_notification",
        )

        assert await agent.tasks.claim_delivery(
            "retry-key",
            task_id="run-1",
            subtask_id="task-2",
            channel="wechat",
            external_key="user-a",
            kind="task_notification",
        )
        await agent.tasks.finish_delivery("retry-key", status="failed", error="network")
        assert await agent.tasks.claim_delivery(
            "retry-key",
            task_id="run-1",
            subtask_id="task-2",
            channel="wechat",
            external_key="user-a",
            kind="task_notification",
        )
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_duplicate_task_notification_is_sent_once() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    channel = _RecordingChannel(settings, agent)
    note = TaskNotification(
        subtask_id="task-duplicate",
        skill_name="literature-search",
        status="succeeded",
        channel="wechat",
        session_id="session-a",
        external_key="user-a",
        summary="done",
    )
    try:
        await channel.send_task_notification(note)
        await channel.send_task_notification(note)
        assert len(channel.sent) == 1
        assert isinstance(channel.sent[0][1], TaskPresentation)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_skill_notice_is_not_resent_when_the_parent_turn_already_attached_files() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    channel = _RecordingChannel(settings, agent)
    session_id = await agent.ensure_session()
    task = await agent.tasks.create_task(
        session_id=session_id,
        channel="wechat",
        user_input="为 RAG 系统综述准备材料",
    )
    paper = ArtifactRef(
        title="RAG系统综述",
        format="md",
        path="/tmp/RAG系统综述.md",
        uri="artifact://paper",
    )
    try:
        await channel._send_task_presentation(
            "user-a",
            TurnPresentation(
                assistant_text="任务已完成，三份材料都已产出并保存在工作区。",
                task_id=task.id,
                artifacts=[paper],
            ),
            task_id=task.id,
            kind="turn",
        )
        assert await agent.tasks.turn_covers_deliverables(
            task.id, channel="wechat", external_key="user-a"
        )
        status = await channel.send_task_notification(
            TaskNotification(
                subtask_id="3a06844f" + "0" * 24,
                skill_name="scientific-figure",
                status="succeeded",
                object_kind="skill_execution",
                channel="wechat",
                session_id=session_id,
                external_key="user-a",
                task_id=task.id,
                summary="Generated an auditable, reproducible RAG System Architecture.",
            )
        )
        assert status == "sent"
        assert len(channel.sent) == 1
        assert "任务已完成" in channel.sent[0][1].assistant_text
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_pending_child_turn_still_sends_the_skill_notice() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    channel = _RecordingChannel(settings, agent)
    session_id = await agent.ensure_session()
    task = await agent.tasks.create_task(
        session_id=session_id,
        channel="wechat",
        user_input="生成架构图",
    )
    try:
        await channel._send_task_presentation(
            "user-a",
            TurnPresentation(
                assistant_text=(
                    "Remaining deliverables are still running. "
                    "Files will be sent when they are ready."
                ),
                task_id=task.id,
            ),
            task_id=task.id,
            kind="turn",
        )
        assert not await agent.tasks.turn_covers_deliverables(
            task.id, channel="wechat", external_key="user-a"
        )
        status = await channel.send_task_notification(
            TaskNotification(
                subtask_id="3a06844f" + "0" * 24,
                skill_name="scientific-figure",
                status="succeeded",
                object_kind="skill_execution",
                channel="wechat",
                session_id=session_id,
                external_key="user-a",
                task_id=task.id,
                summary="Figure rendered.",
            )
        )
        assert status == "sent"
        assert len(channel.sent) == 2
        assert isinstance(channel.sent[1][1], TaskPresentation)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_cost_summary_includes_durable_child_runs() -> None:
    agent = await OmniAgent.create(load_settings())
    session_id = await agent.ensure_session()
    parent = await agent.tasks.create_task(
        session_id=session_id,
        channel="cli",
        user_input="research question",
    )
    child = await agent.tasks.create_task(
        session_id=session_id,
        channel="cli",
        user_input="specialist analysis",
        parent_task_id=parent.id,
        kind="subagent",
        depth=1,
    )
    try:
        await agent.tasks.append_event(
            parent.id,
            event_type="cost.usage",
            status="succeeded",
            name="planner",
            output_json={
                "component": "planner",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost_usd": 0.01,
                "estimated": True,
            },
        )
        await agent.tasks.append_event(
            child.id,
            event_type="cost.usage",
            status="succeeded",
            name="subagent",
            output_json={
                "component": "subagent",
                "prompt_tokens": 20,
                "completion_tokens": 7,
                "total_tokens": 27,
                "cost_usd": 0.02,
                "estimated": False,
            },
        )

        summary = await agent.tasks.cost_summary(parent.id)
    finally:
        await agent.aclose()

    assert summary["calls"] == 2
    assert summary["estimated_calls"] == 1
    assert summary["total_tokens"] == 42
    assert summary["cost_usd"] == 0.03
    assert set(summary["components"]) == {"planner", "subagent"}


@pytest.mark.asyncio
async def test_session_memory_maintenance_has_settled_cost_run() -> None:
    """Ending a session leaves a finished maintenance run that owns its cost.

    Memory hygiene calls the model without a user turn to bill it to, so it gets
    its own run. That run has to reach a terminal status like any other: one
    left open would sit in `omni task list` forever, and its model spend would
    never be attributable to anything.
    """
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = ScriptedLLM()
    agent.llm = llm
    agent.memory._llm = llm
    settings.model.provider = "openai_compatible"
    session_id = await agent.ensure_session()
    await agent.memory.record(
        layer=MemoryLayer.SEMANTIC,
        scope="user",
        scope_id="local",
        summary="我偏好中文科研写作",
        memory_type="preference",
        importance=0.8,
    )
    try:
        await agent.end_session(session_id)
        runs = await agent.tasks.list_tasks(limit=10)
        maintenance = next(run for run in runs if run.kind == "maintenance")
        events = await agent.tasks.list_events(maintenance.id)
    finally:
        await agent.aclose()

    assert maintenance.status == "succeeded"
    assert any(event.event_type == "cost.usage" for event in events)
    assert any(event.name == "memory:profile_merge" for event in events)
    assert any(event.event_type == "maintenance.completed" for event in events)
    assert any(event.event_type == "task.succeeded" for event in events)
