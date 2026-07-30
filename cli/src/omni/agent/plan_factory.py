"""Dependency-free construction of the capable ReAct floor plan.

Both the planner (which produces plans) and the recovery ladder (which repairs
them) need to build the same "capable, safety-bounded assistant" turn. Keeping
that constructor here — depending only on the plan dataclasses — lets the
recovery module build it without importing the planner, so the "repair" layer
never depends on the "produce" layer (no import cycle, no lazy imports).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace

from omni.agent.intent_plan import (
    ContextPolicy,
    IntentPlan,
    IntentType,
    ToolPolicy,
    VerificationPlan,
)
from omni.agent.task_contract import build_schedule_task_contract

# An identifier-lookup downgrade (Rung 0.75) hands a whole workflow to the ReAct
# floor to "search → id → fetch → re-run". That needs more headroom than the
# abandoned workflow's tight ceiling (typically 4), so the floor can look the id
# up *and* re-submit the authorised step DAG within one turn.
_IDENTIFIER_LOOKUP_MIN_TOOL_CALLS = 8
_IDENTIFIER_LOOKUP_MIN_ITERATIONS = 8

# Irreversible filesystem/shell mutations are the only tools blocked in the
# default capable assistant turn. Everything else (read, search, recall,
# research-capture, skill/workflow invocation, escalation) stays available so
# the model can behave like a real agent instead of a 1-tool fallback.
ASSISTANT_BLOCKED_TOOLS: tuple[str, ...] = ("write_file", "edit_file", "bash", "run_compute")

# The scheduling tools a SCHEDULE turn is allowed to use (see ``schedule_tools``).
SCHEDULE_TOOLS: tuple[str, ...] = (
    "schedule_task",
    "resolve_action_checkpoint",
    "list_schedules",
    "cancel_schedule",
)


def _folded_contains(needle: str, haystack: str) -> bool:
    """Whitespace-folded substring — the same fold temporal grounding uses."""
    n = "".join(needle.split()).lower()
    h = "".join(haystack.split()).lower()
    return bool(n) and n in h


def _normalize_schedule_when(when: Mapping[str, object]) -> dict[str, object]:
    """Mechanical field aliases so a planner-shaped ``when`` can take the fast lane.

    ``at`` → ``once``, ``clock.hour`` → ``clock.surface_hour``, and a missing
    ``clock.evidence`` is filled from ``raw_expression``. No AM/PM is inferred.
    """
    out = dict(when)
    kind = str(out.get("trigger_kind") or "").strip().lower()
    if kind == "at":
        out["trigger_kind"] = "once"
    constraints = out.get("constraints")
    if not isinstance(constraints, Mapping):
        return out
    constraints = dict(constraints)
    clock = constraints.get("clock")
    if isinstance(clock, Mapping):
        clock = dict(clock)
        if clock.get("surface_hour") in (None, "") and clock.get("hour") not in (None, ""):
            clock["surface_hour"] = clock["hour"]
        raw = str(out.get("raw_expression") or "").strip()
        if not str(clock.get("evidence") or "").strip() and raw:
            clock["evidence"] = raw
        constraints["clock"] = clock
        out["constraints"] = constraints
    return out


def _complete_schedule_constraint(name: str, value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    required = {
        "clock": ("surface_hour", "evidence"),
        "interval": ("seconds", "evidence"),
        "recurrence": ("freq", "evidence"),
        "cron": ("expr", "evidence"),
    }[name]
    return all(value.get(field) not in (None, "") for field in required)


def _machine_value_is_literal(kind: str, value: str, user_message: str) -> bool:
    if kind == "every_seconds":
        return (
            re.search(rf"(?<![\d,]){re.escape(value)}(?![\d,])", user_message)
            is not None
        )
    if kind == "at":
        return (
            re.search(
                rf"(?<![0-9A-Za-z]){re.escape(value)}(?![0-9A-Za-z:+-])",
                user_message,
            )
            is not None
        )
    return re.search(rf"(?<!\d){re.escape(value)}(?!\d)", user_message) is not None


def _direct_schedule_proposal(
    value: dict[str, object] | None,
    user_message: str,
) -> dict[str, object]:
    """Return one grounded, structurally complete create proposal or nothing."""
    proposal = deepcopy(value or {})
    if not str(proposal.get("goal") or "").strip():
        return {}
    when = proposal.get("when")
    exact = [key for key in ("cron", "every_seconds", "at") if proposal.get(key)]
    if when is not None and not isinstance(when, Mapping):
        return {}
    if isinstance(when, Mapping):
        if exact:
            return {}
        when = _normalize_schedule_when(when)
        proposal["when"] = when
        raw = str(when.get("raw_expression") or "").strip()
        kind = str(when.get("trigger_kind") or "").strip()
        constraints = when.get("constraints")
        required = {
            "once": ("clock",),
            "interval": ("interval",),
            "recurring": ("recurrence", "clock"),
            "cron": ("cron",),
        }.get(kind, ())
        if (
            not raw
            or not _folded_contains(raw, user_message)
            or not isinstance(constraints, Mapping)
            or not required
            or any(
                not _complete_schedule_constraint(key, constraints.get(key))
                for key in required
            )
        ):
            return {}
        return proposal
    if len(exact) != 1:
        return {}
    machine_value = str(proposal[exact[0]]).strip()
    timezone = str(proposal.get("timezone") or "").strip()
    if timezone and timezone not in user_message:
        return {}
    return (
        proposal
        if machine_value and _machine_value_is_literal(exact[0], machine_value, user_message)
        else {}
    )


def needs_input_plan(
    text: str,
    *,
    task_id: str,
    missing_inputs: list[dict[str, object]],
    rationale: str,
    confidence: float,
) -> IntentPlan:
    """A turn that asks instead of executing: no tools, one question."""
    return IntentPlan(
        task_id=task_id,
        user_message=text,
        intent_type=IntentType.NEEDS_INPUT,
        confidence=confidence,
        outputs=["question"],
        execution_mode="ask",
        provenance_mode="light",
        context_policy=ContextPolicy(include_memory=False),
        tool_policy=ToolPolicy(
            allowed_tools=[], final_reserve_enabled=True, max_tool_calls=0, max_iterations=0
        ),
        missing_inputs=[dict(item) for item in missing_inputs],
        verification_plan=VerificationPlan(required_outputs=["clarifying_question"]),
        acceptance=["missing_inputs_reported"],
        rationale=rationale,
    )


def build_assistant_plan(
    text: str,
    *,
    task_id: str,
    rationale: str,
    confidence: float = 0.5,
    intent_type: IntentType = IntentType.REACT_FALLBACK,
) -> IntentPlan:
    """Capable, safety-bounded ReAct turn.

    This is the single default execution path for every request that is not an
    explicit protocol boundary and did not resolve to a concrete
    capability/workflow, and it is also the recovery ladder's floor (Rung 4).
    The model gets a real read + research-capture + recall + skill-invocation
    catalog; only irreversible filesystem/shell mutations are blocked by default.

    ``direct_answer`` is the same capability-preserving turn with an eager-answer
    bias (``execution_mode="direct"``), not a zero-tool turn. Codex, Claude Code,
    and OpenClaw all keep tools available and let the model decide; stripping the
    catalog while the prompt still says "use docs_search first" is exactly what
    made a self-knowledge question dead-end. Trivial turns still answer in one
    shot because the model simply does not call a tool.
    """
    direct = intent_type == IntentType.DIRECT_ANSWER
    return IntentPlan(
        task_id=task_id,
        user_message=text,
        intent_type=intent_type,
        confidence=confidence,
        outputs=["answer"],
        execution_mode="direct" if direct else "react",
        provenance_mode="light",
        context_policy=ContextPolicy(
            include_memory=True,
            include_referenced_tasks=True,
            include_skill_catalog=True,
        ),
        tool_policy=ToolPolicy(
            allowed_tools=None,
            blocked_tools=list(ASSISTANT_BLOCKED_TOOLS),
            final_reserve_enabled=True,
        ),
        acceptance=["bounded_react_or_salvage"],
        verification_plan=VerificationPlan(
            required_outputs=["answer"],
            required_events=["react.finished"],
        ),
        rationale=rationale,
    )


def build_task_inspect_plan(
    text: str,
    *,
    task_id: str,
    rationale: str,
    confidence: float,
) -> IntentPlan:
    """Ground a prior-task status answer in the durable task record."""
    plan = build_assistant_plan(
        text,
        task_id=task_id,
        rationale=rationale or "inspect the referenced task before answering",
        confidence=confidence,
    )
    plan.context_policy.include_recent_activity = True
    plan.context_policy.include_skill_catalog = False
    plan.tool_policy.allowed_tools = ["get_task"]
    # No hard per-turn call ceiling: a count cap (get_task:2 / max_tool_calls=2)
    # is what turned a legitimate "look at a few tasks to resolve which one"
    # into a flood of budget refusals (incident 78071dd2). Codex bounds a turn
    # by progress + context, not a guessed call/iteration count.
    plan.tool_policy.require_opening_tool = True
    plan.acceptance = ["task_status_grounded_in_durable_record"]
    plan.verification_plan = VerificationPlan(
        required_outputs=["answer"],
        required_events=["react.tool.done", "react.finished"],
    )
    return plan


def build_task_review_plan(
    text: str,
    *,
    task_id: str,
    rationale: str,
    confidence: float,
) -> IntentPlan:
    """Review several prior tasks (a time window or cross-project retrospective).

    Unlike ``task.inspect`` — one task, status projected deterministically by the
    host — a review is an enumerative, capable read-only turn: the model lists
    and reads across tasks (``list_recent_tasks`` / ``search_tasks`` /
    ``get_task``, which now span every workspace) and composes the narrative
    itself. There is deliberately **no** hard per-turn call ceiling (a count cap
    is what made the original incident refuse legitimate enumeration). The host
    does not overwrite the prose here — it only appends an authoritative
    per-task status footer at settle time so no failed/degraded task can be
    narrated as a success.
    """
    plan = build_assistant_plan(
        text,
        task_id=task_id,
        rationale=rationale or "review the referenced prior tasks before answering",
        confidence=confidence,
    )
    plan.context_policy.include_recent_activity = True
    plan.context_policy.include_skill_catalog = False
    plan.tool_policy.allowed_tools = [
        "list_recent_tasks",
        "search_tasks",
        "get_task",
        "get_subtask",
        "open_artifact",
    ]
    plan.tool_policy.require_opening_tool = True
    plan.acceptance = ["bounded_react_or_salvage"]
    plan.verification_plan = VerificationPlan(
        required_outputs=["answer"],
        required_events=["react.tool.done", "react.finished"],
    )
    return plan


def build_schedule_plan(
    text: str,
    *,
    task_id: str,
    rationale: str,
    confidence: float = 0.8,
    deferred_goal: str = "",
    schedule_proposal: dict[str, object] | None = None,
    provenance_mode: str = "light",
) -> IntentPlan:
    """Build a host-admitted schedule plan, with focused ReAct as compatibility.

    A complete proposal from the semantic planner is frozen into the task
    contract and admitted directly by the host. Older/incomplete proposals keep
    the narrow scheduling-only ReAct surface so persisted clients degrade safely
    instead of entering capability matching.
    """
    # Direct admission is a **single-shot** invocation: the proposal is handed
    # straight to ``schedule_task`` with no ReAct self-correction round. So only a
    # strictly-complete, grounded, contract-shaped proposal (``_direct_schedule_proposal``
    # requires a ``when.trigger_kind`` in the once/interval/recurring/cron map plus
    # fully structured constraints) may take it. Everything else — including a bare
    # worded time (a lone hour like "7:10") the model annotated loosely (e.g. a stray
    # ``when.trigger_kind="at"``) — falls through to the *self-correcting* ReAct
    # surface, which is the proven path: the model retries the tool against the real
    # schema into a clean AM/PM clarification, and F1/F2 keep that ``needs_input``
    # terminal-and-protected. We do NOT admit loosely-worded times directly, because a
    # single-shot bypass turns the model's easy schema slips into an unrecoverable
    # "tool 'schedule_task' input failed contract validation" hard-fail (the
    # ef3d4fe8 / 686373a5 incident); the fast lane must be a strict subset of what the
    # gateway accepts and must degrade *into* ReAct, never replace it.
    proposal = _direct_schedule_proposal(schedule_proposal, text)
    direct = bool(proposal)
    return IntentPlan(
        task_id=task_id,
        user_message=text,
        intent_type=IntentType.SCHEDULE,
        confidence=confidence,
        outputs=["answer"],
        execution_mode="direct" if direct else "react",
        provenance_mode=(
            provenance_mode if provenance_mode in {"light", "full"} else "light"
        ),
        context_policy=ContextPolicy(
            include_memory=True,
            include_referenced_tasks=True,
            include_skill_catalog=False,
        ),
        tool_policy=ToolPolicy(
            allowed_tools=list(SCHEDULE_TOOLS),
            blocked_tools=list(ASSISTANT_BLOCKED_TOOLS),
            per_tool_limits={
                "schedule_task": 2,
                "resolve_action_checkpoint": 2,
                "list_schedules": 2,
                "cancel_schedule": 2,
            },
            max_tool_calls=5,
            max_iterations=5,
            final_reserve_enabled=True,
            # Force the opening turn through a scheduling tool: an ambiguous time
            # then resolves as a structured ``needs_input`` clarification the loop
            # suspends on, never a prose question that emits no schedule event
            # (the 70a1dd57 missing-events failure).
            require_opening_tool=True,
        ),
        acceptance=["schedule_created_or_clarified"],
        task_contract=build_schedule_task_contract(
            objective=text,
            deferred_goal=deferred_goal or text,
            schedule_proposal=proposal,
            provenance_mode=provenance_mode,
            confidence=confidence,
        ),
        # A scheduling turn is only "done" when it reached a real scheduling
        # outcome through the tool — created, a durable pending approval, an
        # explicit clarification, or a rejection — each recorded as a single
        # ``schedule.resolved`` event. Requiring that event (Codex maps a
        # resolved decision to a definitive result) means a turn that invented a
        # CLI command or claimed success in prose *without* calling the tool
        # fails verification instead of reporting a schedule that never existed.
        verification_plan=VerificationPlan(
            required_outputs=["answer"],
            required_events=["schedule.resolved"],
        ),
        rationale=rationale,
    )


def build_react_recovery_plan(
    plan: IntentPlan,
    *,
    rationale: str,
    identifier_lookup: bool = False,
) -> IntentPlan:
    """Replace execution strategy while retaining goal and safety contracts.

    Required events name the durable trace the *replaced* executor would have
    left, so recovery takes the incoming plan's own rather than inheriting a
    promise its executor can no longer keep. Scheduling is the exception: it
    already runs on the ReAct executor, so its ``schedule.resolved`` proof
    survives the swap intact.

    ``identifier_lookup`` marks the downgrade where a workflow reaches the floor
    only because one identifier could not be bound. The tool budget is widened
    there so the floor has room to look the id up and re-submit in one turn.
    """
    if plan.intent_type == IntentType.SCHEDULE:
        deferred = plan.task_contract.get("deferred_goal")
        deferred_goal = (
            str(deferred.get("objective") or "")
            if isinstance(deferred, dict)
            else ""
        )
        candidate = build_schedule_plan(
            plan.user_message,
            task_id=plan.task_id,
            rationale=rationale,
            confidence=plan.confidence,
            deferred_goal=deferred_goal,
            provenance_mode=plan.provenance_mode,
        )
    else:
        candidate = build_assistant_plan(
            plan.user_message,
            task_id=plan.task_id,
            rationale=rationale,
            confidence=plan.confidence,
        )

    candidate.outputs = list(plan.outputs)
    candidate.provenance_mode = plan.provenance_mode
    candidate.acceptance = list(plan.acceptance)
    candidate.task_contract = deepcopy(plan.task_contract)
    if plan.intent_type == IntentType.SCHEDULE:
        # Recovery explicitly hands control back to focused ReAct. Do not retain
        # the frozen direct payload: persisted/legacy callers must not be able to
        # re-enter host-direct execution merely because the contract still has it.
        candidate.task_contract.pop("schedule_proposal", None)
    # Retain the exact abandoned DAG as an authority template. ReAct may gather
    # missing objective facts, but a provider-owned quality obligation can only
    # be satisfied by re-submitting these same provider consumers (or by a
    # separately authorised replacement plan), never by an untracked tool
    # result that merely looks equivalent.
    candidate.workflow_steps = deepcopy(plan.workflow_steps)
    candidate.workflow_dag = deepcopy(plan.workflow_dag)
    candidate.selected_skills = deepcopy(plan.selected_skills)
    candidate.capability_inputs = deepcopy(plan.capability_inputs)
    candidate.provider_inputs = deepcopy(plan.provider_inputs)
    candidate.provider_bindings = deepcopy(plan.provider_bindings)
    candidate.context_policy = deepcopy(plan.context_policy)
    candidate.tool_policy = _react_recovery_tool_policy(
        original=plan.tool_policy,
        fallback=candidate.tool_policy,
        exact_selected_skill=bool(plan.selected_skills)
        and not bool(plan.workflow_steps),
        unbound_provider_route=(
            plan.intent_type
            in {IntentType.SINGLE_SKILL_TASK, IntentType.QA_PLUS_ARTIFACT}
            and not bool(plan.selected_skills)
        ),
    )
    if identifier_lookup:
        candidate.tool_policy = replace(
            candidate.tool_policy,
            max_tool_calls=max(
                candidate.tool_policy.max_tool_calls or 0,
                _IDENTIFIER_LOOKUP_MIN_TOOL_CALLS,
            ),
            max_iterations=max(
                candidate.tool_policy.max_iterations or 0,
                _IDENTIFIER_LOOKUP_MIN_ITERATIONS,
            ),
        )
    original_verification = plan.verification_plan
    if plan.intent_type == IntentType.SCHEDULE:
        # A schedule plan already runs through the focused ReAct executor. Its
        # ``schedule.resolved`` event is an executor-specific proof that the
        # durable scheduling action actually completed, so recovery must retain
        # the contract exactly rather than treating it as an abandoned workflow.
        candidate.verification_plan = deepcopy(original_verification)
    else:
        candidate.verification_plan = VerificationPlan(
            required_outputs=list(original_verification.required_outputs),
            required_events=list(candidate.verification_plan.required_events),
        )
    return candidate


def _react_recovery_tool_policy(
    *,
    original: ToolPolicy,
    fallback: ToolPolicy,
    exact_selected_skill: bool = False,
    unbound_provider_route: bool = False,
) -> ToolPolicy:
    """Keep real authority ceilings, not an executor shape that never resolved."""

    if unbound_provider_route:
        allowed_tools = fallback.allowed_tools
        max_tool_calls = fallback.max_tool_calls
        max_iterations = fallback.max_iterations
    else:
        allowed_tools = (
            None
            if original.allowed_tools is None
            else _unique(
                [
                    *original.allowed_tools,
                    *(["run_skill"] if exact_selected_skill else []),
                ]
            )
        )
        max_tool_calls = original.max_tool_calls
        max_iterations = original.max_iterations

    return ToolPolicy(
        allowed_tools=allowed_tools,
        blocked_tools=_unique(
            [*original.blocked_tools, *fallback.blocked_tools]
        ),
        per_tool_limits=dict(original.per_tool_limits),
        max_tool_calls=max_tool_calls,
        max_iterations=max_iterations,
        # Reserving a final answer is a safety/termination guarantee. Recovery
        # must not weaken it merely because the abandoned executor disabled it.
        final_reserve_enabled=(
            original.final_reserve_enabled
            or fallback.final_reserve_enabled
        ),
    )


def _unique(values: list[str]) -> list[str]:
    """Return non-empty strings in first-seen order."""

    return list(dict.fromkeys(str(value) for value in values if str(value)))
