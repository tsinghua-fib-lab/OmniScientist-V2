"""Who may settle a turn, and when the record is ready to be judged.

Incident 949be04f: a run that finished perfectly well showed the user

    task.failed — the turn claimed work that left no record: react.finished

part-way through, then flipped back to ``succeeded`` when it ended. The run had
dispatched a background Skill; the daemon finished that child while the ReAct
loop was still working, the child's completion settled the *parent*, and the
verification check read the not-yet-written ``react.finished`` as a claim with
no evidence. Three separate rules were missing, and the tests below pin each:

* a required event the turn has not reached yet is "not yet", not "never";
* a child finishing is an observation, never a verdict on the parent turn;
* a terminal status is reached once, not overwritten in place.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from omni.agent import OmniAgent
from omni.agent.intent_plan import IntentPlan, IntentType, VerificationPlan
from omni.config import load_settings
from omni.runtime.settlement import (
    _failed_presentation,
    _undelivered_presentation,
    settlement_for,
)
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind


def _echo_async_skill() -> SkillEntry:
    script = (
        "import json,sys;"
        "d=json.load(sys.stdin);"
        "print(json.dumps({'status':'ok','summary':'echo '+d.get('input','')}))"
    )
    return SkillEntry(
        name="echo-async",
        description="echo async skill",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


async def _turn_that_dispatched_background_work(agent: OmniAgent):
    """A task mid-turn: a plan is recorded, a child is running, no answer yet."""
    agent.registry.register(_echo_async_skill())
    run = await agent.tasks.create_task(
        session_id=await agent.ensure_session(channel="cli"),
        channel="cli",
        user_input="write the report",
    )
    await agent.tasks.record_plan(
        run.id,
        IntentPlan(
            task_id=run.id,
            user_message="write the report",
            intent_type=IntentType.REACT_FALLBACK,
            verification_plan=VerificationPlan(required_events=["react.finished"]),
        ),
        status="validated",
    )
    await agent.runtime.enqueue(
        "echo-async",
        {"input": "ok"},
        "cli",
        session_id=run.session_id,
        task_id=run.id,
    )
    return run


@pytest.mark.asyncio
async def test_a_child_finishing_mid_turn_does_not_settle_the_turn() -> None:
    """The reported incident, end to end: the daemon drains while the loop runs.

    Nothing about the parent may move. Publishing ``failed`` here is wrong on its
    own terms, but the collateral damage is worse: the status the user is shown
    contradicts the answer they are about to get, steering is sealed while they
    can still steer, and the task leaves the active set so every later child
    outcome is dropped from the aggregate.
    """
    agent = await OmniAgent.create(load_settings())
    try:
        run = await _turn_that_dispatched_background_work(agent)

        await agent.runtime.drain()

        parent = await agent.tasks.get_task(run.id)
        assert parent is not None
        assert parent.status == "running"
        assert parent.finished_at is None
        assert parent.steering_status != "sealed"
        events = {event.event_type for event in await agent.tasks.list_events(run.id)}
        assert "task.failed" not in events
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_the_same_run_settles_succeeded_once_the_turn_ends() -> None:
    """Deferring the verdict must not lose it: the turn still settles, correctly."""
    agent = await OmniAgent.create(load_settings())
    try:
        run = await _turn_that_dispatched_background_work(agent)
        await agent.runtime.drain()

        await agent.tasks.append_event(
            run.id,
            event_type="react.finished",
            status="succeeded",
            name="react",
            output_json={"kind": "text", "terminated_reason": "done"},
        )
        await agent.task_controller.finish_turn(
            run.id,
            kind="text",
            text="here is the report",
            submitted_subtask_ids=[
                str(item) for item in ((await agent.tasks.get_task(run.id)).submitted_subtask_ids or [])
            ],
        )

        settled = await agent.tasks.get_task(run.id)
        assert settled is not None
        assert settled.status == "succeeded"
        assert settled.finished_at is not None
        events = {event.event_type for event in await agent.tasks.list_events(run.id)}
        assert "task.failed" not in events
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_an_outsider_reads_a_missing_turn_event_as_not_yet() -> None:
    """The same record, two readers, two correct answers.

    Absence is ambiguous — nothing in the row says whether the turn skipped the
    event or has not got there yet — so the reader declares which it is. A child
    completion asking about a live turn gets ``pending``; the turn asking about
    itself has finished producing evidence and gets the verdict.
    """
    agent = await OmniAgent.create(load_settings())
    try:
        run = await _turn_that_dispatched_background_work(agent)
        await agent.runtime.drain()

        from_outside = await settlement_for(agent.tasks, run.id, turn_in_flight=True)
        from_the_turn = await settlement_for(agent.tasks, run.id)

        assert from_outside.is_pending
        assert from_outside.detail.get("in_flight") == ["turn"]
        assert from_the_turn.status == "failed"
        assert from_the_turn.detail.get("unfounded_claims") == ["react.finished"]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_an_unfounded_side_effect_still_fails_a_finished_turn() -> None:
    """The protection this whole check exists for is untouched.

    The turn ended and said it scheduled something; no ``schedule.resolved`` row
    exists. That is a claim with no evidence, not a claim that has not come due,
    and it still settles ``failed``.
    """
    agent = await OmniAgent.create(load_settings())
    try:
        run = await agent.tasks.create_task(
            session_id="", channel="cli", user_input="remind me every monday"
        )
        await agent.tasks.record_plan(
            run.id,
            IntentPlan(
                task_id=run.id,
                user_message="remind me every monday",
                intent_type=IntentType.REACT_FALLBACK,
                verification_plan=VerificationPlan(
                    required_events=["react.finished", "schedule.resolved"]
                ),
            ),
            status="validated",
        )
        await agent.tasks.append_event(
            run.id,
            event_type="react.finished",
            status="succeeded",
            name="react",
            output_json={"kind": "text", "terminated_reason": "done"},
        )

        await agent.tasks.settle_task(run.id, proposed_status="succeeded", summary="done")

        settled = await agent.tasks.get_task(run.id)
        assert settled is not None and settled.status == "failed"
        assert "schedule.resolved" in (settled.error or "")
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_a_terminal_status_is_reached_once_not_overwritten() -> None:
    """A settled run keeps the status the user was shown until a recovery reopens it.

    Silently rewriting a terminal status is how the incident stayed invisible:
    the bogus ``failed`` was papered over by a later ``succeeded``, so the only
    evidence left was a stale line in the user's terminal.
    """
    agent = await OmniAgent.create(load_settings())
    try:
        run = await agent.tasks.create_task(session_id="", channel="cli", user_input="do it")
        await agent.tasks.settle_task(run.id, proposed_status="succeeded", summary="done")

        await agent.tasks.finish_task(run.id, status="failed", error="second opinion")

        settled = await agent.tasks.get_task(run.id)
        assert settled is not None and settled.status == "succeeded"
        types = [event.event_type for event in await agent.tasks.list_events(run.id)]
        assert types.count("task.succeeded") == 1
        assert "task.failed" not in types

        await agent.tasks.reopen_task_for_recovery(run.id, reason="operator retry")
        await agent.tasks.finish_task(run.id, status="failed", error="second opinion")

        recovered = await agent.tasks.get_task(run.id)
        assert recovered is not None and recovered.status == "failed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_missing_manuscript_with_figure_settles_degraded() -> None:
    """Named scientific outputs are a contract: a figure is not a paper.

    After the turn ends, settlement is ``degraded`` with ``undelivered_outputs``,
    never a fake ``succeeded``. Mid-turn the same gap is ``pending``, not a
    verdict — the 949be04f rule still holds.
    """
    agent = await OmniAgent.create(load_settings())
    try:
        run = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="Attention abstract, RAG figure, and a survey paper",
        )
        await agent.tasks.record_plan(
            run.id,
            IntentPlan(
                task_id=run.id,
                user_message="Attention abstract, RAG figure, and a survey paper",
                intent_type=IntentType.REACT_FALLBACK,
                outputs=["artifact.figure", "draft.manuscript"],
                verification_plan=VerificationPlan(
                    required_events=["react.finished"],
                    required_outputs=["artifact.figure", "draft.manuscript"],
                ),
            ),
            status="validated",
        )
        await agent.artifacts.put_bytes(
            b"png-bytes",
            kind="figure",
            title="RAG architecture",
            ext="png",
            mime="image/png",
            task_id=run.id,
        )

        mid = await settlement_for(agent.tasks, run.id, turn_in_flight=True)
        assert mid.is_pending
        assert "undelivered_outputs" not in mid.detail

        await agent.tasks.append_event(
            run.id,
            event_type="react.finished",
            status="succeeded",
            name="react",
            output_json={"kind": "text", "terminated_reason": "done"},
        )
        await agent.task_controller.finish_turn(
            run.id,
            kind="text",
            text="figure is ready; the paper was not written",
        )

        settled = await agent.tasks.get_task(run.id)
        assert settled is not None
        assert settled.status == "degraded"
        verdict = await settlement_for(agent.tasks, run.id)
        assert verdict.status == "degraded"
        assert verdict.detail.get("undelivered_outputs") == ["draft.manuscript"]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_no_progress_with_figure_and_paper_settles_succeeded() -> None:
    """Tool-churn after both deliverables exist is not a missing-output degrade."""
    agent = await OmniAgent.create(load_settings())
    try:
        run = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="Attention abstract, RAG figure, and a survey paper",
        )
        await agent.tasks.record_plan(
            run.id,
            IntentPlan(
                task_id=run.id,
                user_message="Attention abstract, RAG figure, and a survey paper",
                intent_type=IntentType.REACT_FALLBACK,
                outputs=["artifact.figure", "draft.manuscript"],
                verification_plan=VerificationPlan(
                    required_events=["react.finished"],
                    required_outputs=["artifact.figure", "draft.manuscript"],
                ),
            ),
            status="validated",
        )
        await agent.artifacts.put_bytes(
            b"png-bytes",
            kind="figure",
            title="RAG architecture",
            ext="png",
            mime="image/png",
            task_id=run.id,
        )
        await agent.artifacts.put_bytes(
            b"# Survey\n",
            kind="report",
            title="Survey",
            ext="md",
            mime="text/markdown",
            task_id=run.id,
        )
        await agent.tasks.append_event(
            run.id,
            event_type="react.finished",
            status="degraded",
            name="react",
            output_json={"kind": "partial", "terminated_reason": "no_progress"},
        )
        await agent.task_controller.finish_turn(
            run.id,
            kind="partial",
            text="Partial result: Repeated tool calls made no progress",
            task_status="degraded",
        )
        settled = await agent.tasks.get_task(run.id)
        assert settled is not None
        assert settled.status == "succeeded"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["wechat", "feishu", "dingtalk"])
async def test_im_finish_turn_settles_when_submitted_child_already_terminal(channel: str) -> None:
    """IM drain_tasks=False must not leave the parent running after the child ended."""
    agent = await OmniAgent.create(load_settings())
    try:
        agent.registry.register(_echo_async_skill())
        session_id = await agent.ensure_session(channel=channel)
        run = await agent.tasks.create_task(
            session_id=session_id,
            channel=channel,
            user_input="draw the figure",
        )
        await agent.tasks.record_plan(
            run.id,
            IntentPlan(
                task_id=run.id,
                user_message="draw the figure",
                intent_type=IntentType.REACT_FALLBACK,
                verification_plan=VerificationPlan(required_events=["react.finished"]),
            ),
            status="validated",
        )
        subtask_id = await agent.runtime.enqueue(
            "echo-async",
            {"input": "ok"},
            channel,
            session_id=session_id,
            task_id=run.id,
        )
        await agent.runtime.drain()
        child = await agent.runtime.get_subtask(subtask_id)
        assert child is not None and child.status == "succeeded"
        await agent.tasks.append_event(
            run.id,
            event_type="react.finished",
            status="degraded",
            name="react",
            output_json={"kind": "partial", "terminated_reason": "no_progress"},
        )
        await agent.tasks.append_event(
            run.id,
            event_type="presentation.sent",
            status="succeeded",
            name=channel,
            output_json={"channel": channel, "kind": "turn", "status": "succeeded"},
        )
        await agent.task_controller.finish_turn(
            run.id,
            kind="partial",
            text="Partial result: Repeated tool calls made no progress",
            submitted_subtask_ids=[subtask_id],
            drain_tasks=False,
            task_status="degraded",
        )
        settled = await agent.tasks.get_task(run.id)
        assert settled is not None
        assert settled.status != "running"
    finally:
        await agent.aclose()


@pytest.mark.parametrize(
    "channel",
    ["wechat", "feishu", "dingtalk", "weixin", "lark", "dingding"],
)
def test_im_channels_owe_a_chat_delivery_before_settling(channel: str) -> None:
    run = SimpleNamespace(channel=channel)
    assert _undelivered_presentation(run, []) is True
    assert _failed_presentation(run, []) is False


def test_cli_does_not_owe_a_chat_delivery_before_settling() -> None:
    run = SimpleNamespace(channel="cli")
    assert _undelivered_presentation(run, []) is False
    assert _failed_presentation(run, []) is False
