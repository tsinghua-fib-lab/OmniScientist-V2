"""Continue/resume binds the task that already holds ROM and debts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult
from omni.runtime.task_continue import (
    continue_from_persisted_plan,
    is_continue_request,
    resolve_continue_task,
    task_has_research_work,
)
from omni.runtime.task_object_resolver import TaskObjectResolution
from omni.runtime.task_recovery import TaskRecoveryCoordinator
from omni.storage.models import TaskORM
from tests.conftest import ScriptedLLM


class _Capture(ScriptedLLM):
    def __init__(self, script: list[ChatWithToolsResult]) -> None:
        super().__init__(script)
        self.prompts: list[list] = []

    async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
        self.prompts.append(list(messages))
        return await super().chat_with_tools(messages, tools, **kwargs)


def test_continue_phrases_are_control_plane_not_rom_tax() -> None:
    assert is_continue_request("继续上次")
    assert is_continue_request("补上图")
    assert is_continue_request("接着写")
    assert is_continue_request("Continue this task.")
    assert is_continue_request("继续")
    assert not is_continue_request("写一篇有图的综述")
    assert not is_continue_request("continue exploring a new topic about CRISPR")


def test_carried_plan_keeps_contract_and_does_not_force_find_skill() -> None:
    persisted = SimpleNamespace(
        plan_json={
            "outputs": ["answer", "artifact.figure", "draft.manuscript"],
            "provenance_mode": "light",
            "verification_plan": {
                "required_outputs": ["artifact.figure", "draft.manuscript"],
                "required_events": ["react.finished"],
            },
            "selected_skills": [{"skill": "openalex-search"}],
        },
        provenance_mode="light",
    )
    plan = continue_from_persisted_plan("继续上次", "task-1", persisted)
    assert plan is not None
    assert "artifact.figure" in plan.outputs
    assert "draft.manuscript" in plan.verification_plan.required_outputs
    assert plan.selected_skills == []
    assert plan.tool_policy.require_opening_tool is False


def test_chat_without_ledger_is_not_research_work() -> None:
    assert not task_has_research_work(
        SimpleNamespace(source_ids=[], claim_ids=[], evidence_ids=[], artifact_ids=[], plan_json={})
    )
    assert task_has_research_work(
        SimpleNamespace(
            source_ids=["s1"],
            claim_ids=[],
            evidence_ids=[],
            artifact_ids=[],
            plan_json={},
        )
    )


@pytest.mark.asyncio
async def test_resolve_continue_binds_last_cancelled_survey() -> None:
    survey = SimpleNamespace(
        id="survey-1",
        kind="turn",
        status="cancelled",
        archived_at=None,
        created_at=2,
        source_ids=["src-1"],
        claim_ids=[],
        evidence_ids=[],
        artifact_ids=[],
        plan_json={"verification_plan": {"required_outputs": ["draft.manuscript"]}},
    )
    chat = SimpleNamespace(
        id="chat-1",
        kind="turn",
        status="cancelled",
        archived_at=None,
        created_at=1,
        source_ids=[],
        claim_ids=[],
        evidence_ids=[],
        artifact_ids=[],
        plan_json={"outputs": ["answer"]},
    )
    tasks = SimpleNamespace(
        get_task=AsyncMock(return_value=None),
        list_tasks_for_session=AsyncMock(return_value=[chat, survey]),
    )
    assert await resolve_continue_task(tasks, user_message="继续上次", session_id="sess") == "survey-1"
    assert await resolve_continue_task(tasks, user_message="hello", session_id="sess") == ""


def _survey_plan(task_id: str, user_message: str) -> dict:
    return {
        "intent_type": "react_fallback",
        "task_id": task_id,
        "user_message": user_message,
        "outputs": ["answer", "artifact.figure", "draft.manuscript"],
        "provenance_mode": "light",
        "verification_plan": {
            "required_outputs": ["artifact.figure", "draft.manuscript"],
            "required_events": ["react.finished"],
        },
    }


async def _cancelled_survey(agent: OmniAgent, *, text: str = "写一篇有图的综述") -> TaskORM:
    session_id = await agent.ensure_session(channel="cli")
    task = await agent.tasks.create_task(
        session_id=session_id,
        channel="cli",
        user_input=text,
        title=text[:80],
    )
    await agent.tasks.record_plan(task.id, _survey_plan(task.id, text), status="validated")
    from sqlalchemy.orm.attributes import flag_modified

    async with agent.db.session() as session:
        row = await session.get(TaskORM, task.id)
        assert row is not None
        row.source_ids = ["src-kept"]
        flag_modified(row, "source_ids")
        await session.commit()
    await agent.tasks.finish_task(
        task.id,
        status="cancelled",
        summary="cancelled mid-survey; sources were preserved",
    )
    refreshed = await agent.tasks.get_task(task.id)
    assert refreshed is not None
    return refreshed


@pytest.mark.asyncio
async def test_continue_reopens_same_task_with_snapshot() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = _Capture(
        [
            ChatWithToolsResult(content="I will write the manuscript from the kept sources."),
            ChatWithToolsResult(content="Figure still owed; continuing production."),
            ChatWithToolsResult(content="Stopped after the ledger finding."),
        ]
    )
    agent.llm = llm
    try:
        original = await _cancelled_survey(agent)
        result = await agent.handle_turn("继续上次", session_id=original.session_id, channel="cli")
        assert result.task_id == original.id
        blobs = [
            str(message.get("content") or "")
            for call in llm.prompts
            for message in call
            if isinstance(message, dict)
        ]
        assert any("Task research state" in blob and "source:src-kept" in blob for blob in blobs)
        assert any("do not call find_skill" in blob.lower() for blob in blobs)
        assert not any(
            getattr(item, "name", "") == "find_skill"
            for batch in llm.prompts
            for message in batch
            if isinstance(message, dict)
            for item in (message.get("tool_calls") or [])
        )
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_retry_copies_research_ledger() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        original = await _cancelled_survey(agent)
        outcome = await TaskRecoveryCoordinator(agent).retry(
            TaskObjectResolution(
                status="ok",
                object_kind="task",
                object_id=original.id,
                task_id=original.id,
                settings=settings,
            ),
            run_turn=False,
        )
        assert outcome.status == "ok", outcome.message
        retry = await agent.tasks.get_task(outcome.new_id or "")
        assert retry is not None
        assert retry.id != original.id
        assert retry.source_ids == ["src-kept"]
        assert "draft.manuscript" in (retry.plan_json.get("verification_plan") or {}).get(
            "required_outputs", []
        )
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_resume_cancelled_survey_enters_live_loop_not_host_fill() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = _Capture(
        [
            ChatWithToolsResult(content="Continuing from the existing sources."),
            ChatWithToolsResult(content="Still producing the owed figure."),
            ChatWithToolsResult(content="Ledger finding received."),
        ]
    )
    agent.llm = llm
    try:
        original = await _cancelled_survey(agent)
        outcome = await TaskRecoveryCoordinator(agent).resume(
            TaskObjectResolution(
                status="ok",
                object_kind="task",
                object_id=original.id,
                task_id=original.id,
                settings=settings,
            )
        )
        assert outcome.status == "ok", outcome.message
        assert outcome.task_id == original.id
        assert "live" in outcome.message.lower()
        blobs = [
            str(message.get("content") or "")
            for call in llm.prompts
            for message in call
            if isinstance(message, dict)
        ]
        assert any("source:src-kept" in blob for blob in blobs)
        assert not any("Filled remaining" in blob for blob in blobs)
    finally:
        await agent.aclose()
