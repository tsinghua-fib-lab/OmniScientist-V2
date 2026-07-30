"""End-to-end invariants for the trusted execution boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType, VerificationPlan
from omni.agent.orchestrator import OmniAgent, TurnResult
from omni.agent.subagents import SubagentSpec, run_subagent
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.runtime.execution_policy import ToolResourceLockPool, resources_for_tool
from omni.skills_runtime.executor import execute_skill
from omni.skills_runtime.manifest import SkillEntry, SkillKind
from tests.agent.test_subagents import RoutingLLM
from tests.conftest import ScriptedLLM


@pytest.mark.asyncio
async def test_direct_skill_entry_cannot_bypass_shared_policy() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(session_id="", channel="cli", user_input="run executable skill")
    ctx = agent._make_ctx("", "cli", task_id=run.id)  # noqa: SLF001
    skill = SkillEntry(
        name="external-cli-skill",
        description="untrusted executable boundary",
        kind=SkillKind.CLI_EXEC,
        source="claude_user",
    )
    try:
        result = await execute_skill(skill, {"input": "run"}, ctx)
        events = await agent.tasks.list_events(run.id)
    finally:
        await agent.aclose()

    assert result["approval_required"] is True
    assert any(event.event_type == "approval.denied" for event in events)


@pytest.mark.asyncio
async def test_prompt_skill_sensitive_tool_uses_shared_approval_gate(tmp_path: Path) -> None:
    agent = await OmniAgent.create(load_settings())
    target = tmp_path / "must-not-be-written.txt"
    agent.llm = ScriptedLLM([
        ChatWithToolsResult(
            content="",
            tool_calls=[ToolCall(
                id="write-1",
                name="write_file",
                arguments={"path": str(target), "contents": "unsafe"},
            )],
        ),
        ChatWithToolsResult(content="blocked safely"),
    ])
    run = await agent.tasks.create_task(session_id="", channel="cli", user_input="run prompt skill")
    ctx = agent._make_ctx("", "cli", task_id=run.id)  # noqa: SLF001
    ctx.llm = agent.llm
    skill = SkillEntry(
        name="prompt-writer",
        description="tries to write a file",
        kind=SkillKind.PROMPT_ONLY,
        allowed_tools=["write_file"],
        body="Write the requested file.",
    )
    try:
        await execute_skill(skill, {"input": "write it"}, ctx)
        events = await agent.tasks.list_events(run.id)
        assert not target.exists()
        assert "approval.denied" in {event.event_type for event in events}
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_verifier_failure_is_authoritative_for_task_status() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(session_id="", channel="cli", user_input="verify me")
    plan = IntentPlan(
        task_id=run.id,
        user_message="verify me",
        intent_type=IntentType.DIRECT_ANSWER,
        verification_plan=VerificationPlan(required_events=["evidence.required"]),
    )
    await agent.tasks.record_plan(run.id, plan, status="validated")
    try:
        await agent.task_controller.finish_turn(run.id, kind="text", text="looks complete")
        settled = await agent.tasks.get_task(run.id)
        events = await agent.tasks.list_events(run.id)
        assert settled is not None and settled.status == "failed"
        event_types = [event.event_type for event in events]
        assert "verification.failed" in event_types
        assert "task.succeeded" not in event_types
        assert event_types.index("verification.failed") < event_types.index("task.failed")
        message = next(event for event in events if event.event_type == "assistant.message")
        assert message.duration_ms is not None and message.duration_ms >= 0
        assert message.output_json["elapsed_ms"] == message.duration_ms
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_verifier_failure_is_reflected_in_channel_result() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(session_id="", channel="wechat", user_input="verify me")
    plan = IntentPlan(
        task_id=run.id,
        user_message="verify me",
        intent_type=IntentType.DIRECT_ANSWER,
        verification_plan=VerificationPlan(required_events=["evidence.required"]),
    )
    await agent.tasks.record_plan(run.id, plan, status="validated")
    try:
        await agent.tasks.settle_task(run.id, proposed_status="succeeded", summary="answer")
        result = await agent._apply_verifier_outcome(  # noqa: SLF001
            run.id,
            TurnResult(text="answer", session_id="session", task_id=run.id),
        )
    finally:
        await agent.aclose()

    assert result.kind == "error"
    assert result.terminated_reason == "verification_failed"
    assert result.verification_status == "failed"


@pytest.mark.asyncio
async def test_public_finish_run_cannot_bypass_verifier() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(session_id="", channel="cli", user_input="verify me")
    plan = IntentPlan(
        task_id=run.id,
        user_message="verify me",
        intent_type=IntentType.DIRECT_ANSWER,
        verification_plan=VerificationPlan(required_events=["missing.required.event"]),
    )
    await agent.tasks.record_plan(run.id, plan, status="validated")
    try:
        await agent.tasks.finish_task(run.id, status="succeeded", summary="answer")
        settled = await agent.tasks.get_task(run.id)
    finally:
        await agent.aclose()

    assert settled is not None
    assert settled.status == "failed"


@pytest.mark.asyncio
async def test_verifier_derives_degraded_bounded_execution_outcome() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(session_id="", channel="cli", user_input="bounded run")
    plan = IntentPlan(
        task_id=run.id,
        user_message="bounded run",
        intent_type=IntentType.REACT_FALLBACK,
        verification_plan=VerificationPlan(required_events=["react.finished"]),
    )
    await agent.tasks.record_plan(run.id, plan, status="validated")
    await agent.tasks.append_event(
        run.id,
        event_type="react.finished",
        # Simulate a legacy/extension producer that mislabeled a bounded answer.
        status="succeeded",
        name="react",
        output_json={
            "kind": "text",
            "terminated_reason": "synthesized_max_tool_calls",
        },
    )
    try:
        await agent.tasks.settle_task(run.id, proposed_status="succeeded", summary="best effort")
        settled = await agent.tasks.get_task(run.id)
        events = await agent.tasks.list_events(run.id)
    finally:
        await agent.aclose()

    assert settled is not None and settled.status == "degraded"
    event_types = [event.event_type for event in events]
    assert "verification.degraded" in event_types
    assert "task.succeeded" not in event_types
    assert event_types.index("verification.degraded") < event_types.index("task.degraded")


@pytest.mark.asyncio
async def test_verifier_does_not_erase_pre_review_degradation() -> None:
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(session_id="", channel="cli", user_input="bounded then reviewed")
    plan = IntentPlan(
        task_id=run.id,
        user_message="bounded then reviewed",
        intent_type=IntentType.REACT_FALLBACK,
        verification_plan=VerificationPlan(
            required_events=["execution.finished", "react.finished"]
        ),
    )
    await agent.tasks.record_plan(run.id, plan, status="validated")
    await agent.tasks.append_event(
        run.id,
        event_type="execution.finished",
        status="degraded",
        name="react",
        output_json={
            "kind": "text",
            "terminated_reason": "synthesized_max_iterations",
        },
    )
    await agent.tasks.append_event(
        run.id,
        event_type="react.finished",
        status="succeeded",
        name="react",
        output_json={"kind": "text", "terminated_reason": "done"},
    )
    try:
        await agent.tasks.settle_task(run.id, proposed_status="succeeded", summary="reviewed answer")
        settled = await agent.tasks.get_task(run.id)
    finally:
        await agent.aclose()

    assert settled is not None and settled.status == "degraded"


@pytest.mark.asyncio
async def test_subagent_is_persisted_as_child_run() -> None:
    agent = await OmniAgent.create(load_settings())
    agent.settings.subagents.reviewer_enabled = False
    agent.llm = RoutingLLM(lambda _user: ChatWithToolsResult(content="child result"))
    parent = await agent.tasks.create_task(session_id="", channel="cli", user_input="delegate")
    ctx = agent._make_ctx("", "cli", task_id=parent.id)  # noqa: SLF001
    ctx.llm = agent.llm
    try:
        result = await run_subagent(SubagentSpec(goal="inspect evidence", role="reviewer"), ctx)
        children = await agent.tasks.list_child_tasks(parent.id)
        assert result.status == "ok"
        assert len(children) == 1
        assert children[0].kind == "subagent"
        assert children[0].parent_task_id == parent.id
        assert children[0].status == "succeeded"
        child_events = await agent.tasks.list_events(children[0].id)
        assert "subagent.finished" in {event.event_type for event in child_events}
        parent_events = await agent.tasks.list_events(parent.id)
        assert any(
            event.event_type == "subagent.submitted"
            and (event.output_json or {}).get("child_task_id") == children[0].id
            for event in parent_events
        )
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_side_effect_locks_serialize_only_matching_resources() -> None:
    pool = ToolResourceLockPool(stripes=16)
    active = 0
    peak = 0

    async def work() -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "ok"

    await asyncio.gather(
        pool.run(["fs:/same"], work),
        pool.run(["fs:/same"], work),
    )
    assert peak == 1

    active = 0
    peak = 0
    await asyncio.gather(
        pool.run(["fs:/one"], work),
        pool.run(["fs:/two"], work),
    )
    assert peak == 2


@pytest.mark.asyncio
async def test_resource_lock_is_shared_across_agent_instances(tmp_path: Path) -> None:
    first = ToolResourceLockPool(lock_dir=tmp_path / "locks")
    second = ToolResourceLockPool(lock_dir=tmp_path / "locks")
    active = 0
    peak = 0

    async def work() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1

    await asyncio.gather(
        first.run(["fs:/shared"], work),
        second.run(["fs:/shared"], work),
    )

    assert peak == 1


def test_filesystem_resource_aliases_resolve_to_one_lock(tmp_path: Path) -> None:
    scope = str(tmp_path)

    direct = resources_for_tool("write_file", {"path": "results.txt"}, scope=scope)
    aliased = resources_for_tool(
        "write_file",
        {"path": "subdir/../results.txt"},
        scope=scope,
    )

    assert direct == aliased
