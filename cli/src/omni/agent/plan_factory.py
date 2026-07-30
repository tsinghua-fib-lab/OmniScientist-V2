"""Dependency-free construction of the capable ReAct floor plan.

Both the planner (which produces plans) and the recovery ladder (which repairs
them) need to build the same "capable, safety-bounded assistant" turn. Keeping
that constructor here — depending only on the plan dataclasses — lets the
recovery module build it without importing the planner, so the "repair" layer
never depends on the "produce" layer (no import cycle, no lazy imports).
"""

from __future__ import annotations

from copy import deepcopy

from omni.agent.intent_plan import (
    ContextPolicy,
    IntentPlan,
    IntentType,
    ToolPolicy,
    VerificationPlan,
)
from omni.agent.task_contract import build_schedule_task_contract

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
        tool_policy=(
            ToolPolicy(allowed_tools=[], max_tool_calls=0, max_iterations=1)
            if direct
            else ToolPolicy(
                allowed_tools=None,
                blocked_tools=list(ASSISTANT_BLOCKED_TOOLS),
                final_reserve_enabled=True,
            )
        ),
        acceptance=["bounded_react_or_salvage"],
        verification_plan=VerificationPlan(
            required_outputs=["answer"],
            required_events=["react.finished"],
            presentation_checks=["show_partial_when_budget_exhausted"],
        ),
        rationale=rationale,
    )


def build_schedule_plan(
    text: str,
    *,
    task_id: str,
    rationale: str,
    confidence: float = 0.8,
    deferred_goal: str = "",
    provenance_mode: str = "light",
) -> IntentPlan:
    """Tool-capable ReAct turn scoped to scheduling.

    The user asked to run work on a schedule. Instead of routing that through
    capability matching (which fails with "no executable contracted provider"
    because scheduling is not a research capability), we hand the turn a small,
    focused tool surface — only the scheduling tools — so the model turns the
    request into a durable scheduled task (``schedule_task``) or, when the timing
    is genuinely ambiguous, asks one concise question. The scheduled unit of work
    is the built-in ``agent-goal`` sub-agent, so any goal is schedulable.
    """
    return IntentPlan(
        task_id=task_id,
        user_message=text,
        intent_type=IntentType.SCHEDULE,
        confidence=confidence,
        outputs=["answer"],
        execution_mode="react",
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
        ),
        acceptance=["schedule_created_or_clarified"],
        task_contract=build_schedule_task_contract(
            objective=text,
            deferred_goal=deferred_goal or text,
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
            required_events=["react.finished", "schedule.resolved"],
            presentation_checks=["show_partial_when_budget_exhausted"],
        ),
        rationale=rationale,
    )


def build_react_recovery_plan(
    plan: IntentPlan,
    *,
    rationale: str,
) -> IntentPlan:
    """Replace execution strategy while retaining goal and safety contracts.

    Workflow lifecycle events and required task names describe the executor
    being replaced, so copying them would make a successful ReAct recovery
    unverifiable. Deliverable, artifact, provenance, and presentation checks
    still describe the user's goal and remain authoritative.
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
    )
    original_verification = plan.verification_plan
    if plan.intent_type == IntentType.SCHEDULE:
        # A schedule plan already runs through the focused ReAct executor. Its
        # ``schedule.resolved`` event is an executor-specific proof that the
        # durable scheduling action actually completed, so recovery must retain
        # the contract exactly rather than treating it as an abandoned workflow.
        candidate.verification_plan = deepcopy(original_verification)
    else:
        fallback_verification = candidate.verification_plan
        candidate.verification_plan = VerificationPlan(
            required_outputs=list(original_verification.required_outputs),
            required_events=list(fallback_verification.required_events),
            forbidden_tools=_unique(
                [
                    *original_verification.forbidden_tools,
                    *candidate.tool_policy.blocked_tools,
                ]
            ),
            # ReAct may select children dynamically. The verifier observes their
            # actual lifecycle instead of requiring names from the abandoned DAG.
            required_tasks=[],
            artifact_checks=list(original_verification.artifact_checks),
            provenance_checks=list(original_verification.provenance_checks),
            presentation_checks=_unique(
                [
                    *original_verification.presentation_checks,
                    *fallback_verification.presentation_checks,
                ]
            ),
            deliverable_checks=list(original_verification.deliverable_checks),
        )
    return candidate


def _react_recovery_tool_policy(
    *,
    original: ToolPolicy,
    fallback: ToolPolicy,
    exact_selected_skill: bool = False,
) -> ToolPolicy:
    """Keep the original ceiling while adding the ReAct floor's safety blocks."""

    return ToolPolicy(
        allowed_tools=(
            None
            if original.allowed_tools is None
            else _unique(
                [
                    *original.allowed_tools,
                    *(["run_skill"] if exact_selected_skill else []),
                ]
            )
        ),
        blocked_tools=_unique(
            [*original.blocked_tools, *fallback.blocked_tools]
        ),
        per_tool_limits=dict(original.per_tool_limits),
        max_tool_calls=original.max_tool_calls,
        max_iterations=original.max_iterations,
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
