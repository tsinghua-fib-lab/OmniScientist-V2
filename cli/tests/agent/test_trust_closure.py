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
from omni.storage.models import SubtaskORM
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
async def test_a_claim_with_no_durable_trace_settles_failed_not_succeeded() -> None:
    """Prose cannot promise a side effect the record never saw.

    The turn says it finished, but the plan declared that finishing means leaving
    an ``evidence.required`` event and none exists. On CLI the user would see the
    missing tool call; over IM and in headless runs they see only this sentence,
    so the durable status has to disagree with it.
    """
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
        assert settled.error and "evidence.required" in settled.error
        event_types = [event.event_type for event in events]
        assert "task.succeeded" not in event_types
        assert "task.failed" in event_types
        message = next(event for event in events if event.event_type == "assistant.message")
        assert message.duration_ms is not None and message.duration_ms >= 0
        assert message.output_json["elapsed_ms"] == message.duration_ms
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_an_unfounded_claim_reaches_the_user_not_just_the_task_row() -> None:
    """The disagreement has to be visible in the answer, not buried in a status.

    A durable row that says ``failed`` while the channel says "done" is worse
    than either alone, so the settlement is projected back onto the turn.
    """
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
        result = await agent._apply_settlement(  # noqa: SLF001
            run.id,
            TurnResult(text="answer", session_id="session", task_id=run.id),
        )
    finally:
        await agent.aclose()

    assert result.kind == "error"
    assert result.terminated_reason == "settlement_failed"
    assert result.settlement_status == "failed"
    assert any("evidence.required" in warning for warning in result.degraded_warnings)


@pytest.mark.asyncio
async def test_the_public_finish_api_cannot_publish_an_unearned_success() -> None:
    """``finish_task`` is the compatibility door, not a way around settlement."""
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
async def test_needs_input_is_a_protected_suspend_not_a_verification_failure() -> None:
    # A clarifying turn is a suspend, not a failure: even when the verification
    # contract requires an event the paused turn legitimately never produced (the
    # deployed "missing_events=1" schedule regression), settle_task must honor
    # needs_input as the terminal outcome — never flip it to failed. (Codex/Claude
    # Code: awaiting-input is terminal-and-protected.)
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(session_id="", channel="cli", user_input="今天7点10分提醒我")
    plan = IntentPlan(
        task_id=run.id,
        user_message="今天7点10分提醒我",
        intent_type=IntentType.SCHEDULE,
        verification_plan=VerificationPlan(required_events=["schedule.resolved"]),
    )
    await agent.tasks.record_plan(run.id, plan, status="validated")
    try:
        status = await agent.tasks.settle_task(
            run.id, proposed_status="needs_input", summary="AM or PM?"
        )
        settled = await agent.tasks.get_task(run.id)
        events = await agent.tasks.list_events(run.id)
    finally:
        await agent.aclose()

    assert status == "needs_input"
    assert settled is not None and settled.status == "needs_input"
    event_types = [event.event_type for event in events]
    # A suspend must not be ranked against the (moot) required-event contract.
    assert "task.needs_input" in event_types
    assert "task.failed" not in event_types


@pytest.mark.asyncio
async def test_finish_turn_needs_input_settles_a_protected_suspend() -> None:
    # C3 end-to-end: the ReAct loop resolves an ambiguous schedule as
    # kind="needs_input" (C1 forces the tool, R3 composes the question); the
    # turn-completion path must settle that through mark_needs_input — never run
    # the (moot) schedule.resolved verification contract — so the clarification is
    # not mislabeled failed (the missing_events regression) regardless of which
    # settle door (finish_turn vs settle_task) it enters.
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(session_id="", channel="cli", user_input="今天7点10分提醒我")
    plan = IntentPlan(
        task_id=run.id,
        user_message="今天7点10分提醒我",
        intent_type=IntentType.SCHEDULE,
        verification_plan=VerificationPlan(required_events=["schedule.resolved"]),
    )
    await agent.tasks.record_plan(run.id, plan, status="validated")
    try:
        await agent.task_controller.finish_turn(
            run.id, kind="needs_input", text="您指的是明早07:10还是今晚19:10？",
        )
        settled = await agent.tasks.get_task(run.id)
        events = await agent.tasks.list_events(run.id)
    finally:
        await agent.aclose()

    assert settled is not None and settled.status == "needs_input"
    event_types = [event.event_type for event in events]
    assert "task.needs_input" in event_types
    assert "task.failed" not in event_types


async def _suspend_on_a_child_asking_for_configuration(agent: OmniAgent, run_id: str) -> str:
    """A child that stopped for a missing credential, and the pause it caused.

    This is the shape a skill produces when it reports ``action_required:
    configure``: the child is filed ``failed`` because it did no work, and the
    parent is suspended because the user can supply the one thing it lacked.
    """
    child_id = "child-needs-configuration"
    async with agent.tasks._db.session() as session:  # noqa: SLF001
        session.add(
            SubtaskORM(
                id=child_id,
                task_id=run_id,
                skill_name="research-ideation",
                status="failed",
                error="Semantic Scholar API key is not configured.",
            )
        )
        await session.commit()
    await agent.tasks.record_subtask_submitted(
        run_id,
        subtask_id=child_id,
        skill_name="research-ideation",
        input_json={},
    )
    await agent.tasks.mark_needs_input(
        run_id, summary="Set a Semantic Scholar API key, then retry."
    )
    return child_id


@pytest.mark.asyncio
async def test_a_pause_is_not_re_ranked_as_the_failure_that_caused_it() -> None:
    """The record already decided; settlement reads that instead of re-deriving it.

    A run pauses for input precisely because a step could not proceed, so that
    step is always sitting there failed. Aggregating it turns every answerable
    question into the dead end it came from.
    """
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(
        session_id="", channel="cli", user_input="research latent-space steering"
    )
    try:
        await _suspend_on_a_child_asking_for_configuration(agent, run.id)
        settled = await agent.tasks.settlement(run.id)
    finally:
        await agent.aclose()

    assert settled.status == "needs_input"


@pytest.mark.asyncio
async def test_a_question_the_user_can_answer_is_never_presented_as_a_failure() -> None:
    """``architecture.md``: needs_input is "never a settlement_failed-looking error".

    The record and the answer have to tell the same story. Rewriting the suspend
    into a terminal error hides the one actionable question behind the failure it
    was reported as, and the run is unresumable for a reason nobody was told.
    """
    agent = await OmniAgent.create(load_settings())
    run = await agent.tasks.create_task(
        session_id="", channel="cli", user_input="research latent-space steering"
    )
    try:
        await _suspend_on_a_child_asking_for_configuration(agent, run.id)
        result = await agent._apply_settlement(  # noqa: SLF001
            run.id,
            TurnResult(
                text="Set a Semantic Scholar API key, then retry.",
                session_id="session",
                task_id=run.id,
                kind="needs_input",
                terminated_reason="needs_input",
            ),
        )
    finally:
        await agent.aclose()

    assert result.kind == "needs_input"
    assert result.terminated_reason == "needs_input"
    assert result.settlement_status == "needs_input"
    assert not result.degraded_warnings


@pytest.mark.asyncio
async def test_a_bounded_run_settles_degraded_even_if_it_reported_success() -> None:
    """A stop caused by a budget is not a clean finish, whoever labelled it.

    The producer here writes ``status="succeeded"`` while its own terminated
    reason says the tool budget ran out. Settlement reads both and takes the
    stronger outcome, so a partial answer cannot be filed as a complete one.
    """
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
    assert "task.succeeded" not in event_types
    assert "task.degraded" in event_types


@pytest.mark.asyncio
async def test_post_review_success_cannot_erase_an_earlier_degradation() -> None:
    """Two boundaries report; neither gets to overwrite the other.

    The loop stopped at its iteration budget and post-review then finished
    cleanly. Reading only the later event would launder the bounded run into a
    complete one.
    """
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
