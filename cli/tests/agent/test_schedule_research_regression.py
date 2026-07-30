"""Research-usage regressions for schedule goal swap + near-term admission.

Problem 2 — WeChat RAG 综述 scheduled at a new time fired as 联邦学习 because
the host silently overwrote the open-draft / tool goal with Active target.

Problem 1 — 「今天上午10点5分」arrived at 10:02, but blocking compaction +
planning froze "now" at 10:09, so the past-guard blamed the user. Compaction
must not run on a 30-message heuristic, and past-ness is vs message receipt.

Each case is a realistic research WeChat/CLI follow-up, not a toy parser.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.agent import OmniAgent
from omni.agent.schedule_tools import build_schedule_tools
from omni.config import load_settings
from omni.core.action_contracts import ResolverContext
from omni.scheduling.contracts import (
    STATUS_AWAITING_APPROVAL,
    STATUS_CREATED,
    STATUS_NEEDS_INPUT,
    ScheduleActor,
    ScheduleCreateRequest,
    once_trigger,
)
from omni.scheduling.service import ScheduleService
from tests.agent.test_schedule_action_admission import (
    _AMBIGUOUS_WHEN,
    REFERENCE,
    SH,
    _handler,
)

RAG = "为 RAG 系统综述准备材料，并生成 Attention Is All You Need 摘要与架构图"
FL = "撰写联邦学习在非IID数据下的优化方法综述"
RAG_TITLE = "RAG系统综述材料准备"

_AM_1013 = {
    "raw_expression": "今天上午10点13分",
    "trigger_kind": "once",
    "constraints": {
        "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
        "clock": {
            "surface_hour": 10,
            "minute": 13,
            "day_period": "am",
            "evidence": "上午10点13分",
        },
    },
}

_AM_1005 = {
    "raw_expression": "今天上午10点5分",
    "trigger_kind": "once",
    "constraints": {
        "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
        "clock": {
            "surface_hour": 10,
            "minute": 5,
            "day_period": "am",
            "evidence": "上午10点5分",
        },
    },
}


def _ctx(agent: OmniAgent, user_message: str, *, channel: str = "cli"):
    ctx = agent._make_ctx("research-sess", channel)
    ctx.resolver_context = ResolverContext(
        user_message=user_message,
        reference_time=REFERENCE,
        timezone="Asia/Shanghai",
        timezone_source="host",
        channel=channel,
        session_id="research-sess",
    )
    return ctx


async def _open_rag_draft(agent: OmniAgent, ctx) -> str:
    tools = build_schedule_tools(agent.runtime, ctx)
    ask = await _handler(tools, "schedule_task")(
        {"goal": RAG, "title": RAG_TITLE, "when": _AMBIGUOUS_WHEN}
    )
    assert ask["status"] == "needs_input" and ask.get("draft_id")
    return str(ask["draft_id"])


# ── Problem 2: sealed goal is what is stored, shown, and would fire ──────────


@pytest.mark.asyncio
async def test_p2_time_only_followup_stores_rag_not_federated_learning_active_target():
    """The 8e292dfa incident: open RAG draft + Active-target FL host goal."""
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        await _open_rag_draft(agent, ctx)
        ctx.deferred_goal = FL
        ctx.resolver_context = ResolverContext(
            user_message="今天上午10点13分开始执行吧",
            reference_time=REFERENCE,
            timezone="Asia/Shanghai",
            timezone_source="host",
            session_id="research-sess",
        )
        result = await _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")(
            {"goal": RAG, "title": RAG_TITLE, "when": _AM_1013}
        )
        assert result["status"] == "ok"
        assert result.get("goal") == RAG
        assert FL not in str(result.get("summary") or "")
        assert RAG[:12] in str(result.get("summary") or result.get("goal") or "")
        rows = await agent.scheduler.list()
        assert len(rows) == 1
        assert rows[0].input_json.get("input") == RAG
        assert "RAG" in (rows[0].title or "")
        assert FL not in (rows[0].title or "")
        assert FL not in str(rows[0].input_json)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_p2_draft_wins_when_model_also_drifted_to_federated_learning():
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        await _open_rag_draft(agent, ctx)
        ctx.deferred_goal = FL
        ctx.resolver_context = ResolverContext(
            user_message="今天上午10点13分开始执行吧",
            reference_time=REFERENCE,
            timezone="Asia/Shanghai",
            timezone_source="host",
            session_id="research-sess",
        )
        result = await _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")(
            {"goal": FL, "title": "联邦学习综述", "when": _AM_1013}
        )
        assert result["status"] == "ok"
        rows = await agent.scheduler.list()
        assert rows[0].input_json.get("input") == RAG
        assert result.get("goal") == RAG
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_p2_new_time_via_resolve_checkpoint_keeps_the_draft_goal():
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        draft_id = await _open_rag_draft(agent, ctx)
        ctx.deferred_goal = FL
        ctx.resolver_context = ResolverContext(
            user_message="今天上午10点13分开始执行吧",
            reference_time=REFERENCE,
            timezone="Asia/Shanghai",
            timezone_source="host",
            session_id="research-sess",
        )
        result = await _handler(
            build_schedule_tools(agent.runtime, ctx), "resolve_action_checkpoint"
        )({"checkpoint_id": draft_id, "when": _AM_1013})
        assert result["status"] == "ok"
        assert result.get("goal") == RAG
        rows = await agent.scheduler.list()
        assert rows[0].input_json.get("input") == RAG
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_p2_display_and_payload_are_the_same_object_for_an_im_proposal():
    """WeChat card / approve echo / stored payload must not disagree."""
    agent = await OmniAgent.create(load_settings())
    try:
        service = ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)
        due = (datetime.now().astimezone().replace(tzinfo=None) + timedelta(hours=3)).isoformat(
            timespec="minutes"
        )
        result = await service.create(
            ScheduleCreateRequest(
                trigger=once_trigger(due),
                goal=RAG,
                title=RAG_TITLE,
                actor=ScheduleActor(channel="wechat", session_id="s1", principal="wechat:peer"),
            )
        )
        assert result.status == STATUS_AWAITING_APPROVAL
        assert result.goal == RAG
        payload = result.tool_result()
        assert payload.get("goal") == RAG
        assert RAG[:12] in (payload.get("summary") or "")
        assert payload.get("approve_command")
        rows = await service.list_proposals()
        stored = (rows[0].payload_json or {}).get("input") or {}
        assert stored.get("input") == RAG
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_p2_time_only_without_a_draft_asks_instead_of_using_active_target():
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _ctx(agent, "今天上午10点13分开始执行吧")
        ctx.deferred_goal = FL
        result = await _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")(
            {"goal": "", "when": _AM_1013}
        )
        assert result["status"] == "needs_input"
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_p2_user_can_replace_the_draft_goal_when_they_name_new_work():
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        await _open_rag_draft(agent, ctx)
        ctx.resolver_context = ResolverContext(
            user_message=f"改成{FL}，今天上午10点13分开始",
            reference_time=REFERENCE,
            timezone="Asia/Shanghai",
            timezone_source="host",
            session_id="research-sess",
        )
        ctx.deferred_goal = FL
        result = await _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")(
            {"goal": FL, "when": _AM_1013}
        )
        assert result["status"] == "ok"
        rows = await agent.scheduler.list()
        assert rows[0].input_json.get("input") == FL
    finally:
        await agent.aclose()


# ── Problem 1: receipt clock, compaction, near-term IM ───────────────────────


@pytest.mark.asyncio
async def test_p1_future_at_receipt_is_created_even_if_wall_clock_has_slipped():
    """10:02 message for 10:05, host finished thinking at 10:09 → still create."""
    agent = await OmniAgent.create(load_settings())
    try:
        service = ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)
        wall = datetime.now().astimezone()
        receipt = wall - timedelta(minutes=10)
        due_local = (wall.replace(tzinfo=None) - timedelta(minutes=5)).isoformat(timespec="seconds")
        result = await service.create(
            ScheduleCreateRequest(
                trigger=once_trigger(due_local),
                goal=RAG,
                actor=ScheduleActor(channel="cli", principal="local"),
                reference_time=receipt,
            )
        )
        assert result.status == STATUS_CREATED
        rows = await agent.scheduler.list()
        assert rows[0].input_json.get("input") == RAG
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_p1_genuinely_past_at_receipt_still_asks():
    agent = await OmniAgent.create(load_settings())
    try:
        service = ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)
        receipt = datetime.now(UTC)
        due_local = (
            datetime.now().astimezone().replace(tzinfo=None) - timedelta(hours=2)
        ).isoformat(timespec="seconds")
        result = await service.create(
            ScheduleCreateRequest(
                trigger=once_trigger(due_local),
                goal=RAG,
                reference_time=receipt,
            )
        )
        assert result.status == STATUS_NEEDS_INPUT
        assert "past" in (result.error or "").lower()
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_p1_already_clarified_near_term_im_skips_laptop_approve():
    """Open-draft RAG + 'in two minutes' on WeChat must not wait for `schedule approve`."""
    agent = await OmniAgent.create(load_settings())
    try:
        service = ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)
        due_local = (
            datetime.now().astimezone().replace(tzinfo=None) + timedelta(minutes=2)
        ).isoformat(timespec="seconds")
        result = await service.create(
            ScheduleCreateRequest(
                trigger=once_trigger(due_local),
                goal=RAG,
                title=RAG_TITLE,
                actor=ScheduleActor(channel="wechat", session_id="s1", principal="wechat:peer"),
                already_clarified=True,
            )
        )
        assert result.status == STATUS_CREATED
        assert result.goal == RAG
        assert RAG[:12] in (result.summary or "")
        assert await agent.scheduler.list()
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_p1_first_shot_near_term_im_still_proposes_but_shows_the_goal_and_warns():
    agent = await OmniAgent.create(load_settings())
    try:
        service = ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)
        due_local = (
            datetime.now().astimezone().replace(tzinfo=None) + timedelta(minutes=2)
        ).isoformat(timespec="seconds")
        result = await service.create(
            ScheduleCreateRequest(
                trigger=once_trigger(due_local),
                goal=RAG,
                title=RAG_TITLE,
                actor=ScheduleActor(channel="wechat", session_id="s1", principal="wechat:peer"),
            )
        )
        assert result.status == STATUS_AWAITING_APPROVAL
        assert result.near_term is True
        assert result.goal == RAG
        summary = result.summary or ""
        assert RAG[:12] in summary
        assert "soon" in summary.lower()
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_p1_approve_after_the_slot_slipped_runs_immediately_instead_of_blaming_the_user():
    agent = await OmniAgent.create(load_settings())
    try:
        service = ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)
        wall = datetime.now().astimezone()
        receipt = wall - timedelta(minutes=10)
        due_local = (wall.replace(tzinfo=None) - timedelta(minutes=5)).isoformat(timespec="seconds")
        proposed = await service.create(
            ScheduleCreateRequest(
                trigger=once_trigger(due_local),
                goal=RAG,
                actor=ScheduleActor(channel="wechat", session_id="s1", principal="wechat:peer"),
                reference_time=receipt,
            )
        )
        assert proposed.status == STATUS_AWAITING_APPROVAL
        approved = await service.approve(proposed.proposal_id, decided_by="local")
        assert approved.status == STATUS_CREATED
        assert approved.slot_elapsed is True
        assert approved.goal == RAG
        assert "immediately" in (approved.summary or "").lower()
        rows = await agent.scheduler.list()
        assert rows[0].input_json.get("input") == RAG
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_p1_make_ctx_freezes_receipt_time_not_a_later_wall_clock():
    agent = await OmniAgent.create(load_settings())
    try:
        receipt = datetime(2026, 8, 13, 10, 2, 45, tzinfo=SH)
        ctx = agent._make_ctx("s", "wechat", user_message="今天上午10点5分", receipt_time=receipt)
        assert ctx.resolver_context.reference_time == receipt
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_p1_ambiguous_then_1005_am_is_admitted_against_the_frozen_morning_clock():
    """REFERENCE is 09:49; 10:05 AM is still future at receipt even if planning is slow."""
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = _ctx(agent, "为 RAG 系统综述准备材料，今天7点10分执行")
        await _open_rag_draft(agent, ctx)
        ctx.resolver_context = ResolverContext(
            user_message="今天上午10点5分开始执行吧",
            reference_time=REFERENCE,  # 09:49, before 10:05
            timezone="Asia/Shanghai",
            timezone_source="host",
            session_id="research-sess",
        )
        result = await _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")(
            {"goal": RAG, "when": _AM_1005}
        )
        assert result["status"] == "ok"
        assert result.get("goal") == RAG
        rows = await agent.scheduler.list()
        assert rows[0].input_json.get("input") == RAG
    finally:
        await agent.aclose()
