"""Acceptance: worded schedule times are semantically admitted, not guessed.

Reproduces the reported incident end-to-end at the ``schedule_task`` boundary:

    reference = 2026-07-30 09:49 Asia/Shanghai
    input     = 今天7点10分   (no AM/PM)
    expected  = needs_input, and NOTHING is created

Before this change the model silently completed the bare hour to 19:10 to dodge
the past-time guard and a schedule appeared with no confirmation. Now the
ambiguous critical field fails closed into a clarification, and only an explicit
AM/PM (or an exact machine time) creates a schedule.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from omni.agent import OmniAgent
from omni.agent.schedule_tools import _repair_payload, build_schedule_tools
from omni.config import load_settings
from omni.core.action_contracts import ResolverContext
from omni.core.react_agent import _is_terminal_tool_result
from omni.runtime.action_checkpoints import ActionCheckpointStore
from tests.conftest import PlanningLLM

SH = ZoneInfo("Asia/Shanghai")
REFERENCE = datetime(2026, 7, 30, 9, 49, tzinfo=SH)


def _handler(tools, name):
    return next(t.handler for t in tools if t.spec.name == name)


def _frozen_ctx(agent: OmniAgent, user_message: str):
    ctx = agent._make_ctx("", "cli")
    ctx.resolver_context = ResolverContext(
        user_message=user_message,
        reference_time=REFERENCE,
        timezone="Asia/Shanghai",
        timezone_source="host",
    )
    return ctx


_AMBIGUOUS_WHEN = {
    "raw_expression": "今天7点10分",
    "trigger_kind": "once",
    "constraints": {
        "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
        "clock": {"surface_hour": 7, "minute": 10, "day_period": None, "evidence": "7点10分"},
    },
}


@pytest.mark.asyncio
async def test_wechat_long_goal_direct_plan_ignores_ungrounded_pm_and_clarifies():
    """e50e2ae3: bypass a second fragile schedule_task JSON generation."""
    goal = (
        "持续跟踪 RAG 系统最新进展，整理关键论文、工程实践、评测结论，"
        "并形成一份可追溯的中文研究简报"
    )
    user_message = f"{goal}，今天7点10分执行"
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(
        {
            "intent_type": "schedule",
            "confidence": 0.95,
            "capability_inputs": {
                "schedule": {
                    "task": {
                        "goal": goal,
                        "when": {
                            "raw_expression": "今天7点10分",
                            "trigger_kind": "once",
                            "constraints": {
                                "date": {
                                    "kind": "relative_day",
                                    "offset": 0,
                                    "evidence": "今天",
                                },
                                "clock": {
                                    "surface_hour": 7,
                                    "minute": 10,
                                    "day_period": "pm",
                                    "evidence": "7点10分",
                                },
                            },
                        },
                    },
                }
            },
        }
    )
    try:
        turn = await agent.handle_turn(
            user_message,
            channel="wechat",
            drain_tasks=False,
        )

        assert turn.kind == "needs_input"
        assert "AM or PM" in turn.text
        assert agent.llm.plan_calls == 1
        assert agent.llm.calls == 0
        assert await agent.scheduler.list() == []
        events = await agent.tasks.list_events(turn.task_id)
        resolved = [event for event in events if event.event_type == "schedule.resolved"]
        assert len(resolved) == 1
        assert resolved[0].status == "needs_input"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_ambiguous_worded_time_asks_and_creates_nothing():
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        result = await schedule_task({"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN})

        assert result["status"] == "needs_input"
        assert result["resolution_status"] == "ambiguous"
        # Both readings are offered as recovery choices; the user must confirm.
        choices = result["recovery_choices"]
        assert choices and any(c["id"] == "run_now" for c in choices)
        assert any(c["id"] == "cancel" for c in choices)
        # The property that matters: the ambiguous critical field created nothing.
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_explicit_evening_resolves_and_creates_once_schedule():
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天晚上7点10分执行")
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        when = {
            "raw_expression": "今天晚上7点10分",
            "trigger_kind": "once",
            "constraints": {
                "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
                "clock": {
                    "surface_hour": 7,
                    "minute": 10,
                    "day_period": "pm",
                    "evidence": "晚上7点10分",
                },
            },
        }
        result = await schedule_task({"goal": "为 RAG 系统综述准备材料", "when": when})

        assert result["status"] == "ok"
        assert result["kind"] == "once"
        rows = await agent.scheduler.list()
        assert len(rows) == 1 and rows[0].kind == "once"
        assert rows[0].next_due_at is not None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_durable_clarification_resume_evening_creates_schedule():
    # Ambiguity persists as a resumable draft; answering "pm" later (even a fresh
    # tool surface) creates exactly the evening schedule — no re-guessing.
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        tools = build_schedule_tools(agent.runtime, ctx)
        schedule_task = _handler(tools, "schedule_task")

        ask = await schedule_task({"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN})
        assert ask["status"] == "needs_input"
        draft_id = ask["draft_id"]
        assert draft_id and await agent.scheduler.list() == []

        # A brand-new surface (as after a process/turn boundary) resumes by id.
        resume = _handler(build_schedule_tools(agent.runtime, ctx), "resolve_action_checkpoint")
        created = await resume({"checkpoint_id": draft_id, "choice": "pm"})

        assert created["status"] == "ok" and created["kind"] == "once"
        rows = await agent.scheduler.list()
        assert len(rows) == 1 and rows[0].kind == "once"

        # Replaying the same answer is idempotent — still a single schedule.
        again = await resume({"checkpoint_id": draft_id, "choice": "pm"})
        assert again["status"] == "ok"
        assert len(await agent.scheduler.list()) == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_ambiguous_clarification_offers_other_time_option():
    # The AM/PM clarification now offers "enter a different time" alongside the
    # two readings, run_now, and cancel — so the user is not boxed into the two
    # computed readings. The option is declarative data the model lays out.
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        result = await schedule_task({"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN})

        assert result["status"] == "needs_input"
        ids = [c["id"] for c in result["recovery_choices"]]
        assert "other_time" in ids
        assert "run_now" in ids and "cancel" in ids
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_resolve_other_time_asks_for_a_time_and_keeps_draft_open():
    # Picking "other_time" must not dead-end: it asks for a concrete time and
    # leaves the draft resumable (the user can still pick a listed reading).
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        tools = build_schedule_tools(agent.runtime, ctx)
        ask = await _handler(tools, "schedule_task")(
            {"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN}
        )
        draft_id = ask["draft_id"]
        resume = _handler(tools, "resolve_action_checkpoint")

        other = await resume({"checkpoint_id": draft_id, "choice": "other_time"})
        assert other["status"] == "needs_input"
        assert other.get("outcome") == "other_time"
        assert await agent.scheduler.list() == []

        # The draft is still open, so a listed reading still resolves it.
        created = await resume({"checkpoint_id": draft_id, "choice": "pm"})
        assert created["status"] == "ok" and created["kind"] == "once"
        assert len(await agent.scheduler.list()) == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_resolve_unrecognized_choice_invites_a_time_not_a_dead_end():
    # A reply that is neither a listed reading nor a keyword is treated as "none
    # of these": it invites a concrete time rather than the old dead-end, and
    # creates nothing.
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        tools = build_schedule_tools(agent.runtime, ctx)
        ask = await _handler(tools, "schedule_task")(
            {"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN}
        )
        draft_id = ask["draft_id"]
        resume = _handler(tools, "resolve_action_checkpoint")

        vague = await resume({"checkpoint_id": draft_id, "choice": "some weekday afternoon"})
        assert vague["status"] == "needs_input"
        assert "reschedule" in vague["message"].lower()
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_new_time_reschedule_supersedes_prior_open_draft():
    # After an ambiguous ask, a fresh turn giving a grounded explicit time
    # resolves and creates — and supersedes the earlier unanswered draft so it is
    # not re-surfaced later. (The new turn carries its own grounded message, as a
    # real reschedule would.)
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        store = ActionCheckpointStore(ctx.db)
        ask = await _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")(
            {"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN}
        )
        assert ask["status"] == "needs_input"
        assert len(await store.list_open()) == 1

        ctx2 = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天晚上7点10分执行")
        explicit_pm = {
            "raw_expression": "今天晚上7点10分",
            "trigger_kind": "once",
            "constraints": {
                "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
                "clock": {
                    "surface_hour": 7,
                    "minute": 10,
                    "day_period": "pm",
                    "evidence": "晚上7点10分",
                },
            },
        }
        created = await _handler(build_schedule_tools(agent.runtime, ctx2), "schedule_task")(
            {"goal": "为 RAG 系统综述准备材料", "when": explicit_pm}
        )

        assert created["status"] == "ok" and created["kind"] == "once"
        assert len(await agent.scheduler.list()) == 1
        # The earlier draft was superseded, so nothing lingers to be re-surfaced.
        assert await store.list_open() == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_new_ambiguous_ask_supersedes_prior_draft_but_keeps_itself():
    # A *different* ambiguous ask supersedes the earlier draft but keeps the new
    # one open — the exclude_id guard protects the draft just created. (An
    # identical ask would instead dedupe onto the same draft.)
    agent = await OmniAgent.create(load_settings())
    try:
        ctx1 = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        store = ActionCheckpointStore(ctx1.db)
        first = await _handler(build_schedule_tools(agent.runtime, ctx1), "schedule_task")(
            {"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN}
        )

        ctx2 = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天8点20分执行")
        when2 = {
            "raw_expression": "今天8点20分",
            "trigger_kind": "once",
            "constraints": {
                "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
                "clock": {"surface_hour": 8, "minute": 20, "day_period": None, "evidence": "8点20分"},
            },
        }
        second = await _handler(build_schedule_tools(agent.runtime, ctx2), "schedule_task")(
            {"goal": "为 RAG 系统综述准备材料", "when": when2}
        )
        assert first["status"] == "needs_input" and second["status"] == "needs_input"

        open_ids = [rec.id for rec in await store.list_open()]
        assert second["draft_id"] in open_ids
        assert first["draft_id"] not in open_ids
        assert len(open_ids) == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_durable_clarification_elapsed_reading_offers_repair_not_creation():
    # Picking the already-past morning reading must not create anything; it keeps
    # the draft open and offers a future repair (plan §8), which then creates.
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        tools = build_schedule_tools(agent.runtime, ctx)
        ask = await _handler(tools, "schedule_task")(
            {"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN}
        )
        draft_id = ask["draft_id"]
        resume = _handler(tools, "resolve_action_checkpoint")

        elapsed = await resume({"checkpoint_id": draft_id, "choice": "am"})
        assert elapsed["status"] == "needs_input"
        assert any(c["id"] == "repair_next_day:am" for c in elapsed["recovery_choices"])
        assert await agent.scheduler.list() == []

        repaired = await resume({"checkpoint_id": draft_id, "choice": "repair_next_day:am"})
        assert repaired["status"] == "ok" and repaired["kind"] == "once"
        assert len(await agent.scheduler.list()) == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_durable_clarification_concurrent_resume_creates_at_most_one():
    # Property (plan §15): concurrent/retried answers converge on a single
    # schedule — only the CAS winner materialises one.
    import asyncio

    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        tools = build_schedule_tools(agent.runtime, ctx)
        ask = await _handler(tools, "schedule_task")(
            {"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN}
        )
        draft_id = ask["draft_id"]
        resume = _handler(tools, "resolve_action_checkpoint")

        results = await asyncio.gather(
            *(resume({"checkpoint_id": draft_id, "choice": "pm"}) for _ in range(5))
        )
        assert all(r["status"] == "ok" for r in results)
        assert len(await agent.scheduler.list()) == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_durable_clarification_wrong_decider_cannot_resolve():
    # Only the original requester may answer the clarification (decider identity).
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        ask = await _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")(
            {"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN}
        )
        draft_id = ask["draft_id"]

        other = agent._make_ctx("", "cli")
        other.principal = "someone-else"
        other.resolver_context = ctx.resolver_context
        resume = _handler(build_schedule_tools(agent.runtime, other), "resolve_action_checkpoint")

        # Neither a direct pick nor the repair path may act for another principal.
        blocked = await resume({"checkpoint_id": draft_id, "choice": "pm"})
        assert blocked["status"] == "error"
        blocked_repair = await resume({"checkpoint_id": draft_id, "choice": "repair_next_day:am"})
        assert blocked_repair["status"] == "error"
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


class _RecorderSpy:
    """Captures the action.* admission trail without a live task recorder."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def append_event(self, task_id, *, event_type, status, name, tool_name, output_json, summary):  # noqa: ANN001, ANN002
        self.events.append((event_type, status, dict(output_json or {})))


@pytest.mark.asyncio
async def test_admission_emits_action_audit_trail_without_goal_text():
    # Plan §13: a structured action.* trail is recorded (proposed → resolution →
    # checkpoint.created → checkpoint.resolved → admitted), and it never carries
    # the goal text/credentials — only admission facts.
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        spy = _RecorderSpy()
        ctx.task_recorder = spy
        ctx.task_id = "t-audit"
        tools = build_schedule_tools(agent.runtime, ctx)

        ask = await _handler(tools, "schedule_task")(
            {"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN}
        )
        kinds = [e[0] for e in spy.events]
        assert "action.proposed" in kinds
        assert "action.resolution" in kinds
        assert "action.checkpoint.created" in kinds

        await _handler(tools, "resolve_action_checkpoint")(
            {"checkpoint_id": ask["draft_id"], "choice": "pm"}
        )
        kinds = [e[0] for e in spy.events]
        assert "action.checkpoint.resolved" in kinds
        assert "action.admitted" in kinds

        # The action.* audit payloads never carry the goal text (only admission
        # facts); the goal lives solely in the tool's own schedule.resolved outcome.
        for _type, _status, data in spy.events:
            if not _type.startswith("action."):
                continue
            assert "goal" not in data
            assert "为 RAG 系统综述准备材料" not in str(data)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_host_deferred_goal_is_sealed_over_a_drifted_model_goal():
    # Decision #3: a host goal grounded in THIS user message still beats a
    # drifted model rewrite. (An ungrounded host goal from Active target must
    # not beat an open draft — that is a separate regression.)
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，2099-01-01T09:00 执行")
        ctx.deferred_goal = "为 RAG 系统综述准备材料"  # planner-owned clean goal
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        result = await schedule_task(
            {"goal": "a totally different drifted goal", "at": "2099-01-01T09:00"}
        )

        assert result["status"] == "ok"
        rows = await agent.scheduler.list()
        assert len(rows) == 1
        # The sealed host goal is what actually runs, not the model's rewrite.
        assert rows[0].input_json.get("input") == "为 RAG 系统综述准备材料"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_model_goal_is_used_when_no_host_deferred_goal():
    # With nothing authoritative to seal (planner extracted no distinct goal),
    # the model's goal is trusted as before — no regression on the common path.
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "one-off summary at 2099-01-01T09:00")
        assert ctx.deferred_goal == ""
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        result = await schedule_task({"goal": "model-authored goal", "at": "2099-01-01T09:00"})

        assert result["status"] == "ok"
        rows = await agent.scheduler.list()
        assert len(rows) == 1 and rows[0].input_json.get("input") == "model-authored goal"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_open_clarification_is_surfaced_at_turn_start():
    # Turn-start recovery: a durable open clarification for this requester is
    # surfaced as a one-line reminder so the model can resume it even after the
    # asking turn has been compacted out of conversation history.
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        tools = build_schedule_tools(agent.runtime, ctx)
        # Fresh surface: nothing pending, so no notice is injected.
        assert await agent._open_clarifications_block(ctx) == ""

        ask = await _handler(tools, "schedule_task")(
            {"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN}
        )
        draft_id = ask["draft_id"]
        assert draft_id

        block = await agent._open_clarifications_block(ctx)
        assert draft_id[:8] in block
        assert "resolve_action_checkpoint" in block
        # The original wording is echoed so the model can match the user's reply.
        assert "今天7点10分" in block
        assert "为 RAG 系统综述准备材料" in block
        assert "when" in block or "at" in block

        # A different requester sees nothing (scoped to the original decider).
        other = _frozen_ctx(agent, "unrelated")
        other.principal = "someone-else"
        assert await agent._open_clarifications_block(other) == ""
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_exact_machine_at_still_creates_without_a_resolver():
    # Section 12: exact machine formats keep the direct path (CLI/model parity),
    # so passing an explicit ISO ``at`` is unaffected by semantic admission.
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "one-off summary at 2099-01-01T09:00")
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        result = await schedule_task({"goal": "one-off summary", "at": "2099-01-01T09:00"})

        assert result["status"] == "ok"
        assert result["kind"] == "once"
        assert len(await agent.scheduler.list()) == 1
    finally:
        await agent.aclose()


# ── who is actually being asked ──────────────────────────────────────────────
#
# Both of these used to end the turn with a question. They are not the same
# thing. "Is 7点 morning or evening?" is a fact only the user has. "You proposed
# minute 59 for a time nobody wrote" is a defect in the model's own arguments,
# and Codex returns that entire class to the model (``RespondToModel``) rather
# than to the human — including its safety rejections. These pin the split.

_HALLUCINATED_MINUTE = {
    "raw_expression": "今晚21点12分",
    "trigger_kind": "once",
    "constraints": {
        "date": {"kind": "relative_day", "offset": 0, "evidence": "今晚"},
        "clock": {"surface_hour": 21, "minute": 59, "hour_system": 24, "evidence": "21点"},
    },
}


@pytest.mark.asyncio
async def test_a_defect_in_the_models_own_arguments_goes_back_to_the_model():
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "帮我今晚21点12分开始执行")
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        result = await schedule_task({"goal": "开始执行", "when": _HALLUCINATED_MINUTE})

        # The property that matters is the loop's, not the payload's spelling:
        # this observation must not suspend the turn.
        assert _is_terminal_tool_result(result) is False
        assert result["status"] == "error"
        assert result["resolution_status"] == "invalid"
        # It has to say what to do differently, or the retry repeats the mistake.
        assert "clock.evidence" in result["error"]
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_a_repair_request_leaves_no_proof_that_a_schedule_exists():
    """``schedule.resolved`` is what settlement accepts as evidence a schedule was
    really created (presence alone — it does not read the status). Writing one for
    a failed attempt would let a later "I scheduled it" pass verification."""
    agent = await OmniAgent.create(load_settings())
    try:
        message = "帮我今晚21点12分开始执行"
        # The admission trail is only written for a ctx that names a task, so this
        # one has to be a real row — otherwise every event assertion here is vacuous.
        task = await agent.tasks.create_task(
            session_id="s-repair", channel="cli", user_input=message
        )
        task_id = task.id
        ctx = _frozen_ctx(agent, message)
        ctx.task_id = task_id
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        await schedule_task({"goal": "开始执行", "when": _HALLUCINATED_MINUTE})

        events = await agent.tasks.list_events(task_id)
        kinds = [event.event_type for event in events]
        assert "schedule.resolved" not in kinds
        assert "action.repair_requested" in kinds
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_the_same_defect_twice_stops_asking_the_model_and_asks_the_user():
    """A model that cannot converge must still reach the user. One repair per
    unresolved field, per turn — the second attempt suspends as before."""
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "帮我今晚21点12分开始执行")
        # One turn = one tool surface, so both calls share the repair budget.
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        first = await schedule_task({"goal": "开始执行", "when": _HALLUCINATED_MINUTE})
        second = await schedule_task({"goal": "开始执行", "when": _HALLUCINATED_MINUTE})

        assert first["status"] == "error"
        assert second["status"] == "needs_input"
        assert _is_terminal_tool_result(second) is True
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


def test_repair_payload_cites_the_user_message_not_the_model_quote():
    from omni.core.action_contracts import ResolutionResult, ResolutionStatus

    result = ResolutionResult(
        status=ResolutionStatus.INVALID,
        reason="proposed time '今天17点03分' is not grounded in the user request",
        unresolved_fields=("raw_expression",),
    )
    payload = _repair_payload(
        result,
        "今天17点03分",
        user_message="帮我改成今天17点03开始执行",
    )
    assert "帮我改成今天17点03开始执行" in payload["error"]
    assert "The user wrote:" in payload["error"]
    assert "clock.evidence" in payload["error"]


@pytest.mark.asyncio
async def test_a_conventional_particle_in_the_quote_creates_the_schedule():
    """2367d610 at the tool boundary: 分 in the quote, 17点03 in the user text."""
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = agent._make_ctx("", "cli")
        ctx.resolver_context = ResolverContext(
            user_message="帮我改成今天17点03开始执行",
            reference_time=datetime(2026, 8, 14, 16, 49, tzinfo=SH),
            timezone="Asia/Shanghai",
            timezone_source="host",
        )
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")
        result = await schedule_task(
            {
                "goal": "为 RAG 系统综述准备材料",
                "when": {
                    "raw_expression": "今天17点03分",
                    "trigger_kind": "once",
                    "constraints": {
                        "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
                        "clock": {
                            "surface_hour": 17,
                            "minute": 3,
                            "day_period": None,
                            "hour_system": 24,
                            "evidence": "17点03分",
                        },
                    },
                },
            }
        )

        assert result["status"] == "ok"
        assert "17:03" in str(result.get("summary") or "")
        rows = await agent.scheduler.list()
        assert len(rows) == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_an_ambiguous_time_is_never_handed_to_the_model_to_guess():
    """The boundary of the change: AM/PM is not a defect the model can repair, and
    routing it back would invite exactly the silent completion this design exists
    to prevent (7点10分 quietly becoming 19:10)."""
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _frozen_ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        result = await schedule_task({"goal": "为 RAG 系统综述准备材料", "when": _AMBIGUOUS_WHEN})

        assert result["status"] == "needs_input"
        assert _is_terminal_tool_result(result) is True
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()
