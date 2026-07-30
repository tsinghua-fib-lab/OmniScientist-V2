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
from omni.agent.schedule_tools import build_schedule_tools
from omni.config import load_settings
from omni.core.action_contracts import ResolverContext

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
    # Decision #3: the goal is host-owned. When the planner extracted a distinct
    # deferred goal, ``schedule_task`` seals *that* — a goal the ReAct model
    # re-typed (drifted) must not be what gets scheduled.
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
