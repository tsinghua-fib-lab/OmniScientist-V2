"""Scheduled agent tasks (P1): NL→schedule routing, the schedule_task tool
group, governance, and the fire→headless-turn→inbox chain.

The user asks "run X on a schedule" in natural language. Instead of dead-ending
in capability matching ("no executable contracted provider"), the request is
routed to a focused scheduling turn whose only tools create/inspect/cancel a
durable schedule. When a goal schedule comes due it fires a full *headless
orchestrator turn* (one brain, headless door) — the same planner→workflow→
verification pipeline an interactive turn uses, so a multi-deliverable goal is
decomposed and each deliverable is separately budgeted/verified — whose verified
result lands in the inbox. P1 is "the agent can turn a request into a scheduled
task, and that task runs the full pipeline unattended".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.agent import OmniAgent
from omni.agent.intent_plan import IntentType
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.plan_factory import SCHEDULE_TOOLS, build_react_recovery_plan
from omni.agent.plan_recovery import ACTION_REACT, recover
from omni.agent.plan_validator import PlanFinding, PlanValidationResult
from omni.agent.planner import IntentPlanner
from omni.agent.schedule_tools import GOAL_SKILL, build_schedule_tools
from omni.config import load_settings
from omni.core.approval import ApprovalDecision, ApprovalRequest
from omni.runtime.hooks import HookDecision
from omni.skills_runtime.builtin_tools import build_builtin_tools
from omni.skills_runtime.registry import SkillRegistry
from tests.conftest import PlanningLLM, ScriptedLLM


def _handler(tools, name):
    return next(t.handler for t in tools if t.spec.name == name)


# ── NL → schedule routing (planner boundary) ──


def test_complete_schedule_proposal_routes_to_direct_host_admission():
    """A complete schedule proposal is admitted without a second model turn."""
    planner = IntentPlanner(SkillRegistry(load_settings()))
    proposal = ModelPlanProposal(
        intent_type="schedule",
        confidence=0.9,
        rationale="user asked to run a daily research digest",
        capability_inputs={
            "schedule": {
                "task": {"goal": "总结今天的科研"},
                "when": {
                    "raw_expression": "每天下午6点",
                    "trigger_kind": "recurring",
                    "constraints": {
                        "recurrence": {"freq": "daily", "evidence": "每天"},
                        "clock": {
                            "surface_hour": 6,
                            "minute": 0,
                            "day_period": "pm",
                            "evidence": "下午6点",
                        },
                    },
                },
                "title": "daily digest",
            }
        },
    )
    plan = planner.plan_from_proposal("每天下午6点总结今天的科研", proposal, task_id="t1")

    assert plan.intent_type is IntentType.SCHEDULE
    assert plan.execution_mode == "direct"
    assert plan.tool_policy.allowed_tools == list(SCHEDULE_TOOLS)
    assert plan.task_contract["schedule_proposal"] == {
        "goal": "总结今天的科研",
        "when": {
            "raw_expression": "每天下午6点",
            "trigger_kind": "recurring",
            "constraints": {
                "recurrence": {"freq": "daily", "evidence": "每天"},
                "clock": {
                    "surface_hour": 6,
                    "minute": 0,
                    "day_period": "pm",
                    "evidence": "下午6点",
                },
            },
        },
        "title": "daily digest",
    }
    # scheduling is not a research capability, so no skills get pre-selected.
    assert not plan.selected_skills


def test_incomplete_or_ungrounded_schedule_proposals_stay_on_focused_react():
    # Only a strictly-complete, contract-shaped proposal takes the single-shot direct
    # path; every looser proposal degrades into the self-correcting ReAct surface.
    # Worded times are covered by test_worded_schedule_times_route_to_self_correcting_react;
    # the cases here are the other un-admittable ones: exact machine triggers not
    # literal-in-message (anti-invention guard), goal conflicts, and a malformed ``when``.
    planner = IntentPlanner(SkillRegistry(load_settings()))
    cases = [
        (
            "今天7点10分提醒我",
            {"task": {"goal": "提醒我"}, "at": "2026-07-30T19:10:00+08:00"},
        ),
        (
            "run 0 18 * * *",
            {
                "goal": "root goal",
                "task": {"goal": "different nested goal"},
                "cron": "0 18 * * *",
            },
        ),
        (
            "run every 3600 seconds",
            {
                "task": {"goal": "future work"},
                "every_seconds": 60,
            },
        ),
        (
            "run at 2099-01-01T09:00",
            {
                "task": {"goal": "future work"},
                "at": "2099-01-01",
            },
        ),
        (
            "run at 2099-01-01T09:00",
            {
                "task": {"goal": "future work"},
                "at": "2099-01-01T09:00",
                "timezone": "Asia/Shanghai",
            },
        ),
        (
            "run 0 18 * * *",
            {
                "task": {"goal": "future work"},
                "when": "invalid",
                "cron": "0 18 * * *",
            },
        ),
    ]

    for user_message, schedule_input in cases:
        plan = planner.plan_from_proposal(
            user_message,
            ModelPlanProposal(
                intent_type="schedule",
                capability_inputs={"schedule": schedule_input},
            ),
            task_id="fallback",
        )

        assert plan.execution_mode == "react"
        assert "schedule_proposal" not in plan.task_contract

    aliases = planner.plan_from_proposal(
        "run 0 18 * * *",
        ModelPlanProposal(
            intent_type="schedule",
            capability_inputs={
                "schedule": {
                    "task": {"goal": "goal A"},
                    "cron": "0 18 * * *",
                },
                "schedule.task": {
                    "goal": "goal B",
                    "cron": "0 18 * * *",
                },
            },
        ),
        task_id="alias-conflict",
    )
    assert aliases.execution_mode == "react"
    assert "schedule_proposal" not in aliases.task_contract
    assert aliases.task_contract["deferred_goal"]["objective"] == "run 0 18 * * *"


def test_worded_schedule_times_route_to_self_correcting_react():
    """A bare worded time is NOT admitted single-shot — it degrades into ReAct.

    Direct admission is a single-shot ``schedule_task`` call with no self-correction
    round, so only ``_direct_schedule_proposal`` (a valid ``when.trigger_kind`` plus
    fully structured, grounded constraints) may take it. A loosely-annotated worded
    time — whether the model left the clock unstructured, wrote a stray
    ``when.trigger_kind="at"`` (outside the once/interval/cron/recurring enum), or
    omitted ``trigger_kind`` — falls through to the *self-correcting* ReAct surface.
    That is the proven path (the pre-regression 73c42ade trace): the model retries the
    tool against the real schema into a clean AM/PM clarification, and F1/F2 keep that
    turn's ``needs_input`` terminal-and-protected.

    Admitting these worded times directly (the removed F3 fork) is exactly what turned
    the model's easy schema slip into a fatal, unrecoverable "tool 'schedule_task'
    input failed contract validation" (the ef3d4fe8 / 686373a5 incident). The fast lane
    must be a strict subset of what the gateway accepts and degrade *into* ReAct.
    """
    planner = IntentPlanner(SkillRegistry(load_settings()))
    cases = [
        # Valid trigger_kind but no structured constraints — not strictly complete.
        {
            "task": {"goal": "提醒我"},
            "when": {"raw_expression": "今天7点10分", "trigger_kind": "once"},
        },
        # Valid trigger_kind with an empty (unstructured) constraints object.
        {
            "task": {"goal": "提醒我"},
            "when": {
                "raw_expression": "今天7点10分",
                "trigger_kind": "once",
                "constraints": {"clock": {}},
            },
        },
        # The actual incident: trigger_kind="at" is outside the allowed enum.
        {
            "task": {"goal": "提醒我"},
            "when": {
                "raw_expression": "今天7点10分",
                "trigger_kind": "at",
                "constraints": {"hour": 7, "minute": 10, "date": "today", "day_period": None},
            },
        },
        # A worded when with no trigger_kind at all.
        {"task": {"goal": "提醒我"}, "when": {"raw_expression": "今天7点10分"}},
    ]
    for schedule_input in cases:
        plan = planner.plan_from_proposal(
            "今天7点10分提醒我",
            ModelPlanProposal(
                intent_type="schedule",
                capability_inputs={"schedule": schedule_input},
            ),
            task_id="worded-schedule",
        )
        assert plan.intent_type is IntentType.SCHEDULE
        assert plan.execution_mode == "react"
        assert "schedule_proposal" not in plan.task_contract


def test_planner_clock_aliases_can_use_direct_admission():
    """``at`` / ``clock.hour`` are shape, not a guessed period.

    The 2367d610 planner already had 17:03 right and only missed the contract
    field names; that must not force a second ReAct rewrite of the quote.
    """
    planner = IntentPlanner(SkillRegistry(load_settings()))
    plan = planner.plan_from_proposal(
        "帮我改成今天17点03开始执行",
        ModelPlanProposal(
            intent_type="schedule",
            capability_inputs={
                "schedule": {
                    "task": {
                        "goal": "为 RAG 系统综述准备材料",
                        "when": {
                            "raw_expression": "今天17点03",
                            "trigger_kind": "at",
                            "constraints": {
                                "clock": {"hour": 17, "minute": 3, "day_period": None},
                            },
                        },
                    }
                }
            },
        ),
        task_id="alias-when",
    )
    assert plan.execution_mode == "direct"
    when = plan.task_contract["schedule_proposal"]["when"]
    assert when["trigger_kind"] == "once"
    clock = when["constraints"]["clock"]
    assert clock["surface_hour"] == 17
    assert clock["evidence"] == "今天17点03"


def test_complete_cron_when_ir_can_use_direct_admission():
    planner = IntentPlanner(SkillRegistry(load_settings()))
    plan = planner.plan_from_proposal(
        "run cron 0 18 * * *",
        ModelPlanProposal(
            intent_type="schedule",
            capability_inputs={
                "schedule": {
                    "task": {"goal": "future work"},
                    "when": {
                        "raw_expression": "cron 0 18 * * *",
                        "trigger_kind": "cron",
                        "constraints": {
                            "cron": {
                                "expr": "0 18 * * *",
                                "evidence": "0 18 * * *",
                            }
                        },
                    },
                }
            },
        ),
        task_id="cron-when",
    )

    assert plan.execution_mode == "direct"
    assert plan.task_contract["schedule_proposal"]["when"]["trigger_kind"] == "cron"


def test_schedule_plan_persists_only_a_deferred_goal_contract():
    planner = IntentPlanner(SkillRegistry(load_settings()))
    proposal = ModelPlanProposal(
        intent_type="schedule",
        confidence=0.9,
        provenance_mode="full",
        capability_inputs={
            "schedule": {
                "task": {
                    "goal": (
                        "获取 Attention Is All You Need 摘要，生成 RAG 架构图，"
                        "并输出论文"
                    ),
                    "when": {
                        "raw_expression": "明天下午六点",
                        "trigger_kind": "once",
                        "constraints": {
                            "date": {"kind": "relative_day", "offset": 1, "evidence": "明天"},
                            "clock": {
                                "surface_hour": 6,
                                "minute": 0,
                                "day_period": "pm",
                                "evidence": "下午六点",
                            },
                        },
                    },
                },
            }
        },
    )

    plan = planner.plan_from_proposal(
        "明天下午六点执行科研材料准备",
        proposal,
        task_id="schedule-contract",
    )

    deferred = plan.task_contract["deferred_goal"]
    assert plan.task_contract["schema_version"] == 2
    assert deferred == {
        "objective": "获取 Attention Is All You Need 摘要，生成 RAG 架构图，并输出论文",
        "binding_state": "deferred",
        "provenance_mode": "full",
    }
    assert plan.task_contract["schedule_proposal"] == {
        "goal": "获取 Attention Is All You Need 摘要，生成 RAG 架构图，并输出论文",
        "when": {
            "raw_expression": "明天下午六点",
            "trigger_kind": "once",
            "constraints": {
                "date": {"kind": "relative_day", "offset": 1, "evidence": "明天"},
                "clock": {
                    "surface_hour": 6,
                    "minute": 0,
                    "day_period": "pm",
                    "evidence": "下午六点",
                },
            },
        },
    }
    assert plan.provider_inputs == {}
    assert plan.capability_inputs == {}
    assert plan.workflow_steps == []
    assert plan.selected_skills == []
    assert plan.requested_constraints == []
    assert plan.binding_records == []


def test_schedule_payload_preserves_deferred_goal_without_treating_it_as_capability():
    proposal = ModelPlanProposal.from_payload(
        {
            "intent_type": "schedule",
            "capability_inputs": {
                "schedule": {"task": {"goal": "生成 RAG 架构图并输出论文"}}
            },
        }
    )

    assert proposal.capability_inputs == {
        "schedule": {"task": {"goal": "生成 RAG 架构图并输出论文"}}
    }


def test_schedule_recovery_keeps_schedule_policy_and_goal_contract():
    registry = SkillRegistry(load_settings())
    planner = IntentPlanner(registry)
    plan = planner.plan_from_proposal(
        "明天下午六点执行科研材料准备",
        ModelPlanProposal(
            intent_type="schedule",
            capability_inputs={
                "schedule": {"task": {"goal": "生成 RAG 架构图并输出论文"}}
            },
        ),
        task_id="schedule-recovery",
    )
    original_contract = plan.task_contract
    original_verification = plan.verification_plan.to_dict()
    validation = PlanValidationResult(
        status="rejected",
        errors=["synthetic non-safety planning blocker"],
        findings=[
            PlanFinding(
                code="synthetic_planning_blocker",
                message="synthetic non-safety planning blocker",
            )
        ],
    )

    outcome = recover(plan, validation, registry)

    assert outcome.action == ACTION_REACT
    assert outcome.plan.intent_type is IntentType.SCHEDULE
    assert outcome.plan.execution_mode == "react"
    assert outcome.plan.tool_policy.allowed_tools == list(SCHEDULE_TOOLS)
    assert outcome.plan.task_contract == original_contract
    assert outcome.plan.task_contract is not original_contract
    assert outcome.plan.verification_plan.to_dict() == original_verification
    assert "schedule.resolved" in outcome.plan.verification_plan.required_events


def test_direct_schedule_recovery_discards_frozen_direct_proposal():
    planner = IntentPlanner(SkillRegistry(load_settings()))
    plan = planner.plan_from_proposal(
        "run 0 18 * * *",
        ModelPlanProposal(
            intent_type="schedule",
            capability_inputs={
                "schedule": {
                    "task": {
                        "goal": "summarize today's research",
                        "cron": "0 18 * * *",
                    }
                }
            },
        ),
        task_id="direct-schedule-recovery",
    )

    recovered = build_react_recovery_plan(plan, rationale="synthetic recovery")

    assert plan.execution_mode == "direct"
    assert "schedule_proposal" in plan.task_contract
    assert recovered.execution_mode == "react"
    assert "schedule_proposal" not in recovered.task_contract


def test_schedule_plan_requires_a_real_scheduling_outcome_event():
    """L5: a SCHEDULE turn is only 'done' when it reached a scheduling outcome.

    Verification requires the ``schedule.resolved`` event the tool emits for every
    terminal result — so a turn that claims success in prose (or invents a CLI
    command) *without* calling the tool fails verification instead of reporting a
    schedule that never existed."""
    planner = IntentPlanner(SkillRegistry(load_settings()))
    plan = planner.plan_from_proposal(
        "每天下午6点总结今天的科研",
        ModelPlanProposal(intent_type="schedule", confidence=0.9),
        task_id="t1",
    )
    required = plan.verification_plan.required_events
    assert "schedule.resolved" in required
    assert "react.finished" not in required


def test_schedule_proposal_is_not_pre_empted_by_needs_input():
    """A schedule proposal proceeds to the scheduling turn even when the model
    also reported advisory gaps: the tool asks one concise question when the
    timing is ambiguous, rather than the planner short-circuiting to needs_input."""
    planner = IntentPlanner(SkillRegistry(load_settings()))
    proposal = ModelPlanProposal(
        intent_type="schedule",
        missing_inputs=[{"field": "time", "reason": "ambiguous cadence"}],
    )
    plan = planner.plan_from_proposal("帮我定期总结科研", proposal, task_id="t1")

    assert plan.intent_type is IntentType.SCHEDULE
    assert plan.tool_policy.allowed_tools == list(SCHEDULE_TOOLS)


# ── schedule_task tool handler ──


@pytest.mark.asyncio
async def test_schedule_task_creates_cron_schedule_for_agent_goal():
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = agent._make_ctx("", "cli")
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        res = await schedule_task(
            {"goal": "总结今天的科研", "cron": "0 18 * * *", "title": "daily digest"}
        )
        assert res["status"] == "ok"
        assert res["kind"] == "cron"
        assert res["channel"] == "cli"

        rows = await agent.scheduler.list()
        assert len(rows) == 1
        sched = rows[0]
        # The schedulable unit of work is the general-purpose agent-goal sub-agent.
        assert sched.skill_name == GOAL_SKILL
        assert sched.cron_expr == "0 18 * * *"
        assert (sched.input_json or {}).get("input") == "总结今天的科研"
        assert sched.title == "daily digest"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_schedule_task_clarifies_or_rejects_bad_input():
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = agent._make_ctx("", "cli")
        h = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        # Missing goal → concise clarification, not a crash.
        assert (await h({"cron": "0 9 * * *"}))["status"] == "needs_input"
        # No trigger at all → clarify (need exactly one).
        assert (await h({"goal": "x"}))["status"] == "needs_input"
        # Two triggers → clarify (need exactly one).
        two = await h({"goal": "x", "cron": "0 9 * * *", "every_seconds": 60})
        assert two["status"] == "needs_input"
        # A malformed cron is a hard error the model can relay.
        assert (await h({"goal": "x", "cron": "not a cron"}))["status"] == "error"

        # None of the above should have created a schedule.
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_schedule_task_one_time_at_is_stored_with_due_time():
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = agent._make_ctx("", "cli")
        h = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        res = await h({"goal": "one-off summary", "at": "2099-01-01T09:00"})
        assert res["status"] == "ok"
        assert res["kind"] == "once"

        rows = await agent.scheduler.list()
        assert len(rows) == 1 and rows[0].kind == "once"
        assert rows[0].next_due_at is not None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_list_and_cancel_schedule_tools_roundtrip():
    agent = await OmniAgent.create(load_settings())
    try:
        ctx = agent._make_ctx("", "cli")
        tools = build_schedule_tools(agent.runtime, ctx)
        schedule_task = _handler(tools, "schedule_task")
        list_schedules = _handler(tools, "list_schedules")
        cancel_schedule = _handler(tools, "cancel_schedule")

        created = await schedule_task({"goal": "hourly digest", "every_seconds": 3600})
        assert created["status"] == "ok"

        listed = await list_schedules({})
        assert listed["status"] == "ok" and listed["count"] == 1
        assert listed["schedules"][0]["goal"] == "hourly digest"

        # An id *prefix* (as printed by list_schedules) is accepted.
        removed = await cancel_schedule({"schedule_id": created["schedule_id"][:8]})
        assert removed["status"] == "ok" and removed["removed"] is True
        assert (await list_schedules({}))["count"] == 0

        # Cancelling an unknown id is a clean error, not an exception.
        assert (await cancel_schedule({"schedule_id": "deadbeef"}))["status"] == "error"
    finally:
        await agent.aclose()


# ── governance: scheduling lives only on the coordinator surface ──


def test_scheduling_tools_are_not_on_the_builtin_subagent_surface():
    """``agent-goal`` runs on the builtin-tool surface; scheduling tools must not
    appear there, so a scheduled run cannot recursively create more schedules —
    they are added only on the top-level coordinator surface (``tool_surface``)."""
    settings = load_settings()
    from omni.skills_runtime.context import ExecContext

    ctx = ExecContext(settings=settings, paths=settings.paths)
    builtin_names = {t.spec.name for t in build_builtin_tools(ctx)}
    assert not builtin_names & set(SCHEDULE_TOOLS)


def test_schedule_tools_are_absent_without_a_database():
    """Schedules are DB-backed, so DB-free callers/tests get no scheduling tools."""
    settings = load_settings()
    from omni.skills_runtime.context import ExecContext

    ctx = ExecContext(settings=settings, paths=settings.paths, db=None)
    assert build_schedule_tools(runtime=None, ctx=ctx) == []


# ── end-to-end: fire → agent-goal → inbox ──


@pytest.mark.asyncio
async def test_due_schedule_fires_agent_goal_and_delivers_to_inbox():
    agent = await OmniAgent.create(load_settings())
    try:
        # Deterministic offline sub-agent: agent-goal's ReAct returns a final
        # answer with no tool calls. ``_make_ctx`` reads ``self.llm`` at execution
        # time, so swapping it here reaches the fired task.
        agent.llm = ScriptedLLM()

        now = datetime.now(UTC)
        ctx = agent._make_ctx("", "cli")
        schedule_task = _handler(build_schedule_tools(agent.runtime, ctx), "schedule_task")

        # A one-time trigger set in the (near) future is created; we then fire
        # by advancing the scheduler's clock past its due time. (Scheduling in
        # the *past* now returns needs_input — the exact time-confusion bug this
        # work fixes — so we no longer rely on that to make a job due.)
        soon = (datetime.now().astimezone().replace(tzinfo=None) + timedelta(minutes=1)).isoformat()
        created = await schedule_task({"goal": "总结今天的科研并给出方向性规划", "at": soon})
        assert created["status"] == "ok"

        fired = await agent.scheduler.run_due(now=now + timedelta(minutes=5))
        assert len(fired) == 1

        # A goal schedule now fires as a full *headless orchestrator turn* (one
        # brain, headless door) run off-tick under a pre-created owning task —
        # not a single bounded ``agent-goal`` skill execution. Await the detached
        # turn and any workflow subtasks it enqueues.
        await agent.scheduler.drain_fires()
        await agent.runtime.drain()

        # The fired reference is the owning task; it ran under the full pipeline
        # and reached a terminal status, and is linked back to its schedule.
        owning = await agent.tasks.get_task(fired[0])
        assert owning is not None
        assert owning.status in {"succeeded", "degraded", "failed", "needs_input"}
        assert owning.schedule_id, "the owning task must be traceable to its schedule"

        # The verified outcome is delivered to the local inbox (durable, daemon-free).
        inbox = agent.notifier.read_all()
        assert any(
            note.get("skill_name") == GOAL_SKILL
            and note.get("object_kind") == "task"
            and note.get("object_id") == owning.id
            for note in inbox
        )
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_nl_scheduling_request_flows_through_planner_to_created_schedule():
    """The exact previously-broken chain: a natural-language scheduling request no
    longer dead-ends in capability matching ("no executable contracted provider").

    The planner classifies it as ``schedule`` and supplies one grounded proposal.
    The host admits that proposal directly, without asking the model to repeat the
    long goal and nested arguments in a second tool-call JSON document.
    """
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(
        {
            "intent_type": "schedule",
            "confidence": 0.9,
            "rationale": "daily research digest",
            "capability_inputs": {
                "schedule": {
                    "task": {"goal": "summarise today's research"},
                    "when": {
                        "raw_expression": "every day at 6pm",
                        "trigger_kind": "recurring",
                        "constraints": {
                            "recurrence": {
                                "freq": "daily",
                                "evidence": "every day",
                            },
                            "clock": {
                                "surface_hour": 6,
                                "minute": 0,
                                "day_period": "pm",
                                "evidence": "6pm",
                            },
                        },
                    },
                    "title": "daily research digest",
                }
            },
        },
    )

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(True, scope="once")

    agent.approver = approver
    try:
        turn = await agent.handle_turn(
            "set up a task every day at 6pm to summarise today's research",
            channel="cli",
            drain_tasks=False,
        )
        assert turn.task_id
        assert agent.llm.plan_calls == 1
        assert agent.llm.calls == 0
        # A durable schedule now exists, targeting the agent-goal sub-agent.
        rows = await agent.scheduler.list()
        assert len(rows) == 1
        assert rows[0].skill_name == GOAL_SKILL
        assert rows[0].cron_expr == "0 18 * * *"
        # …and the turn recorded a single truthful scheduling outcome event, so
        # verification is backed by a real result rather than model prose (L5).
        events = await agent.tasks.list_events(turn.task_id)
        resolved = [e for e in events if e.event_type == "schedule.resolved"]
        assert resolved and resolved[0].status == "created"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_direct_schedule_still_obeys_pre_tool_hook(monkeypatch):
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(
        {
            "intent_type": "schedule",
            "capability_inputs": {
                "schedule": {
                    "task": {"goal": "summarise today's research"},
                    "cron": "0 18 * * *",
                }
            },
        }
    )

    async def deny_schedule(event: str, **kwargs):
        payload = kwargs.get("payload") or {}
        if event == "pre_tool" and payload.get("tool_name") == "schedule_task":
            return HookDecision(action="deny", reason="blocked by owner hook")
        return HookDecision()

    monkeypatch.setattr(agent.hooks, "emit", deny_schedule)
    try:
        turn = await agent.handle_turn(
            "schedule 0 18 * * * to summarise today's research",
            channel="cli",
            drain_tasks=False,
        )

        assert turn.kind == "error"
        assert await agent.scheduler.list() == []
        events = await agent.tasks.list_events(turn.task_id)
        assert any(event.event_type == "plan.tool.rejected" for event in events)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_a_bad_cron_cannot_be_filed_as_a_scheduled_reminder():
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(
        {
            "intent_type": "schedule",
            "capability_inputs": {
                "schedule": {
                    "task": {"goal": "summarise today's research"},
                    "cron": "not a cron",
                }
            },
        }
    )
    try:
        turn = await agent.handle_turn(
            "schedule 'not a cron' to summarise today's research",
            channel="cli",
            drain_tasks=False,
        )

        assert turn.kind == "error"
        assert turn.settlement_status == "failed"
        settled = await agent.tasks.get_task(turn.task_id)
        assert settled is not None and settled.status == "failed"
        event_types = {event.event_type for event in await agent.tasks.list_events(turn.task_id)}
        assert "task.succeeded" not in event_types
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_direct_schedule_gateway_exception_becomes_failed_result(monkeypatch):
    from omni.scheduling.service import ScheduleService

    async def fail_create(_self, _request):
        raise RuntimeError("synthetic scheduler failure")

    monkeypatch.setattr(ScheduleService, "create", fail_create)
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(
        {
            "intent_type": "schedule",
            "capability_inputs": {
                "schedule": {
                    "task": {"goal": "future work"},
                    "cron": "0 18 * * *",
                }
            },
        }
    )
    try:
        turn = await agent.handle_turn(
            "schedule 0 18 * * * for future work",
            channel="cli",
            drain_tasks=False,
        )

        assert turn.kind == "error"
        assert turn.settlement_status == "failed"
        assert "Scheduling failed before completion" in turn.text
        events = await agent.tasks.list_events(turn.task_id)
        finished = [event for event in events if event.event_type == "execution.finished"]
        assert finished and finished[-1].status == "failed"
    finally:
        await agent.aclose()
