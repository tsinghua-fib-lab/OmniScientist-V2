"""Contract tests for typed task retry / resume / requeue recovery."""

from __future__ import annotations

import pytest

from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult
from omni.runtime.action_checkpoints import ActionCheckpointStore
from omni.runtime.task_object_resolver import TaskObjectResolution, resolve_task_object
from omni.runtime.task_recovery import TaskRecoveryCoordinator
from tests.conftest import ScriptedLLM


async def _failed_planning_task(agent: OmniAgent, *, text: str = "schedule a RAG review") -> str:
    task = await agent.tasks.create_task(
        session_id=await agent.ensure_session(channel="cli"),
        channel="cli",
        user_input=text,
        title=text[:80],
    )
    await agent.tasks.finish_task(
        task.id,
        status="failed",
        summary="model service rejected the request",
        error="llm_invalid_request",
    )
    return task.id


async def _needs_input_task_with_checkpoint(
    agent: OmniAgent,
    *,
    text: str = "今天7点10分提醒我",
) -> tuple[str, str]:
    session_id = await agent.ensure_session(channel="cli")
    task = await agent.tasks.create_task(
        session_id=session_id,
        channel="cli",
        user_input=text,
        title=text[:80],
    )
    store = ActionCheckpointStore(agent.db)
    record = await store.open_clarification(
        action_kind="schedule.create",
        contract_version="v1",
        policy_version="temporal-policy-v1",
        channel="cli",
        session_id=session_id,
        actor_principal="local",
        required_decider="local",
        task_id=task.id,
        payload={"goal": "remind me", "title": "", "when": {"raw_expression": "今天7点10分"}},
        resolution={
            "status": "ambiguous",
            "reason": "day period unclear",
            "unresolved_fields": ["when"],
            "raw_expression": "今天7点10分",
            "candidates": [
                {
                    "id": "am",
                    "label": "7:10 AM",
                    "validity": "future",
                    "value": {"kind": "once", "at": "2099-01-01T07:10:00", "timezone": ""},
                },
                {
                    "id": "pm",
                    "label": "7:10 PM",
                    "validity": "future",
                    "value": {"kind": "once", "at": "2099-01-01T19:10:00", "timezone": ""},
                },
            ],
        },
    )
    await agent.tasks.append_event(
        task.id,
        event_type="action.checkpoint.created",
        status="info",
        name="schedule.create",
        output_json={"checkpoint_id": record.id, "phase": "semantic_clarification"},
        summary="checkpoint created",
    )
    await agent.tasks.mark_needs_input(
        task.id,
        summary="Is 7:10 AM or 7:10 PM?",
        missing_inputs=[{"field": "when", "reason": "ambiguous"}],
    )
    return task.id, record.id


@pytest.mark.asyncio
async def test_retry_failed_top_level_task_creates_lineage(monkeypatch) -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.llm = ScriptedLLM([ChatWithToolsResult(content="retried successfully")])
    try:
        original_id = await _failed_planning_task(agent)
        resolution = await resolve_task_object(settings, original_id)
        assert resolution.status == "ok" and resolution.object_kind == "task"

        outcome = await TaskRecoveryCoordinator(agent).retry(resolution, run_turn=True)
        assert outcome.status == "ok", outcome.message
        assert outcome.new_id and outcome.new_id != original_id

        original = await agent.tasks.get_task(original_id)
        retry = await agent.tasks.get_task(outcome.new_id)
        assert original is not None and original.status == "failed"
        assert retry is not None
        assert retry.retry_of_task_id == original_id
        assert retry.root_task_id == original_id
        assert retry.attempt == 2
        assert retry.user_input == original.user_input
        assert retry.input_snapshot_json.get("user_input") == original.user_input
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_retry_session_override_grafts_attempt_into_current_session() -> None:
    """Foreground REPL retries pass the live session so the attempt runs in it."""
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        original_id = await _failed_planning_task(agent, text="graft me")
        resolution = TaskObjectResolution(
            status="ok",
            object_kind="task",
            object_id=original_id,
            task_id=original_id,
            settings=settings,
        )
        outcome = await TaskRecoveryCoordinator(agent).retry(
            resolution, run_turn=False, session_id="cli-foreground-session"
        )
        assert outcome.status == "ok", outcome.message
        retry = await agent.tasks.get_task(outcome.new_id)
        assert retry is not None
        assert retry.session_id == "cli-foreground-session"
        assert retry.retry_of_task_id == original_id
        assert retry.input_snapshot_json.get("user_input") == "graft me"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_retry_needs_input_task_redirects_to_resume() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        task_id, _ = await _needs_input_task_with_checkpoint(agent)
        resolution = TaskObjectResolution(
            status="ok",
            object_kind="task",
            object_id=task_id,
            task_id=task_id,
            settings=settings,
        )
        outcome = await TaskRecoveryCoordinator(agent).retry(resolution, run_turn=False)
        assert outcome.status == "wrong_state"
        assert "resume" in outcome.suggested_command
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_resume_needs_input_requires_input_then_resolves() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        task_id, checkpoint_id = await _needs_input_task_with_checkpoint(agent)
        resolution = TaskObjectResolution(
            status="ok",
            object_kind="task",
            object_id=task_id,
            task_id=task_id,
            settings=settings,
        )
        missing = await TaskRecoveryCoordinator(agent).resume(resolution)
        assert missing.status == "input_required"
        assert "--input" in missing.suggested_command

        outcome = await TaskRecoveryCoordinator(agent).resume(
            resolution, input_choice="pm"
        )
        assert outcome.status == "ok", outcome.message
        assert outcome.detail.get("checkpoint_id") == checkpoint_id or outcome.new_id
        settled = await agent.tasks.get_task(task_id)
        assert settled is not None
        assert settled.status in {"succeeded", "degraded", "failed", "cancelled"}
        # Successful clarification should produce a schedule id or at least settle.
        assert settled.status != "needs_input"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_resume_failed_task_without_checkpoint_suggests_retry() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        task_id = await _failed_planning_task(agent, text="plan schedule materials")
        resolution = TaskObjectResolution(
            status="ok",
            object_kind="task",
            object_id=task_id,
            task_id=task_id,
            settings=settings,
        )
        outcome = await TaskRecoveryCoordinator(agent).resume(resolution)
        assert outcome.status == "checkpoint_required"
        assert f"omni task retry {task_id[:8]}" in outcome.suggested_command
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_retry_blocks_when_active_retry_exists() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        original_id = await _failed_planning_task(agent, text="retry once")
        first = await TaskRecoveryCoordinator(agent).retry(
            TaskObjectResolution(
                status="ok",
                object_kind="task",
                object_id=original_id,
                task_id=original_id,
                settings=settings,
            ),
            run_turn=False,
        )
        assert first.status == "ok", first.message
        # Keep the new attempt running so a second retry must fail closed.
        second = await TaskRecoveryCoordinator(agent).retry(
            TaskObjectResolution(
                status="ok",
                object_kind="task",
                object_id=original_id,
                task_id=original_id,
                settings=settings,
            ),
            run_turn=False,
        )
        assert second.status == "wrong_state"
        assert "active retry" in second.message
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_resume_run_now_settles_suspended_task() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        task_id, _ = await _needs_input_task_with_checkpoint(agent)
        outcome = await TaskRecoveryCoordinator(agent).resume(
            TaskObjectResolution(
                status="ok",
                object_kind="task",
                object_id=task_id,
                task_id=task_id,
                settings=settings,
            ),
            input_choice="run_now",
        )
        assert outcome.status == "ok", outcome.message
        settled = await agent.tasks.get_task(task_id)
        assert settled is not None
        assert settled.status == "cancelled"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_resume_other_time_keeps_task_suspended_and_asks() -> None:
    # "other_time" is not a listed reading: the CLI resume must not dead-end or
    # cancel — it keeps the task suspended and asks for a concrete time.
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        task_id, _ = await _needs_input_task_with_checkpoint(agent)
        outcome = await TaskRecoveryCoordinator(agent).resume(
            TaskObjectResolution(
                status="ok",
                object_kind="task",
                object_id=task_id,
                task_id=task_id,
                settings=settings,
            ),
            input_choice="other_time",
        )
        assert outcome.status == "input_required"
        assert outcome.message
        settled = await agent.tasks.get_task(task_id)
        assert settled is not None
        assert settled.status == "needs_input"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_requeue_rejects_top_level_task() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        task_id = await _failed_planning_task(agent)
        resolution = TaskObjectResolution(
            status="ok",
            object_kind="task",
            object_id=task_id,
            task_id=task_id,
            settings=settings,
        )
        outcome = await TaskRecoveryCoordinator(agent).requeue(resolution)
        assert outcome.status == "wrong_state"
        assert "retry" in outcome.suggested_command
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_legacy_checkpoint_without_task_id_column_still_resolves() -> None:
    """Pre-migration drafts linked only via action.checkpoint.created events."""
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="cli")
        task = await agent.tasks.create_task(
            session_id=session_id,
            channel="cli",
            user_input="今天7点10分提醒我",
        )
        store = ActionCheckpointStore(agent.db)
        record = await store.open_clarification(
            action_kind="schedule.create",
            contract_version="v1",
            policy_version="temporal-policy-v1",
            channel="cli",
            session_id=session_id,
            actor_principal="local",
            required_decider="local",
            task_id="",  # legacy
            payload={"goal": "remind me", "title": "", "when": {}},
            resolution={
                "status": "ambiguous",
                "reason": "day period unclear",
                "unresolved_fields": ["when"],
                "raw_expression": "今天7点10分",
                "candidates": [
                    {
                        "id": "am",
                        "label": "7:10 AM",
                        "validity": "future",
                        "value": {"kind": "once", "at": "2099-01-01T07:10:00", "timezone": ""},
                    },
                    {
                        "id": "pm",
                        "label": "7:10 PM",
                        "validity": "future",
                        "value": {"kind": "once", "at": "2099-01-01T19:10:00", "timezone": ""},
                    },
                ],
            },
        )
        await agent.tasks.append_event(
            task.id,
            event_type="action.checkpoint.created",
            status="info",
            name="schedule.create",
            output_json={"checkpoint_id": record.id},
        )
        await agent.tasks.mark_needs_input(task.id, summary="clarify time")

        resolution = TaskObjectResolution(
            status="ok",
            object_kind="task",
            object_id=task.id,
            task_id=task.id,
            settings=settings,
        )
        outcome = await TaskRecoveryCoordinator(agent).resume(
            resolution, input_choice="am"
        )
        assert outcome.status == "ok", outcome.message
        bound = await store.get(record.id)
        assert bound is not None
        assert bound.task_id == task.id
    finally:
        await agent.aclose()
