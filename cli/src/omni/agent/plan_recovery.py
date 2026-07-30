"""Recovery ladder: turn validation findings into the next executable state.

Validation classifies; recovery *routes*. A rejected plan is never a dead end.
Given a plan and its :class:`PlanValidationResult`, this module returns the best
next state on a short ladder:

    Rung 0  safety violation          → hard stop (never swallowed)
    Rung 3  needs_input               → ask for a single user-suppliable field
    Rung 4  ReAct handoff (the floor) → hand to the capable, safety-bounded
                                        assistant every other agent uses by
                                        default, with the findings as context

The ladder used to carry three middle rungs that rewrote a multi-step plan in
place — reroute an unbindable identifier, swap a step to a producer capability,
prune un-satisfiable steps and detach their dependents. All three existed to
patch a DAG that had been sealed before any tool ran. The model now sequences
multi-step work itself against live results, so there is no pre-sealed DAG left
to patch, and a plan that will not execute goes straight to the floor that can
act, look things up, and re-sequence.

The floor stays under the normal tool policy (no self-granted tools).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.plan_factory import (
    build_assistant_plan,
    build_react_recovery_plan,
    needs_input_plan,
)
from omni.agent.plan_validator import (
    SEVERITY_BLOCKING,
    PlanFinding,
    PlanValidationResult,
)
from omni.agent.reference_markers import references_prior_work

if TYPE_CHECKING:
    from omni.skills_runtime.registry import SkillRegistry

logger = logging.getLogger(__name__)

# Recovery actions the orchestrator dispatches on.
ACTION_EXECUTE = "execute"
ACTION_NEEDS_INPUT = "needs_input"
ACTION_REACT = "react"
ACTION_HARD_STOP = "hard_stop"

@dataclass(slots=True)
class RecoveryOutcome:
    action: str
    plan: IntentPlan
    notes: list[str] = field(default_factory=list)
    missing_inputs: list[dict] = field(default_factory=list)
    rung: str = ""

    @property
    def executable(self) -> bool:
        return self.action == ACTION_EXECUTE


def recover(
    plan: IntentPlan,
    validation: PlanValidationResult,
    registry: SkillRegistry,
) -> RecoveryOutcome:
    """Return the next executable/actionable state for a (possibly rejected) plan."""
    # Rung 0 — safety violations are terminal and never degraded.
    if validation.has_safety_finding:
        notes = [f.message for f in validation.findings if f.severity == "safety"]
        return RecoveryOutcome(action=ACTION_HARD_STOP, plan=plan, notes=notes, rung="0_safety")

    # Codex "look before asking": a clarifying question that points at the agent's
    # own prior work is a history-blind refusal. Downgrade it to a capable,
    # tool-enabled turn that can pull the referent back (recent activity +
    # get_task/get_subtask/open_artifact/memory_search) before deciding it cannot
    # proceed. Genuinely under-specified requests (no referential marker) still ask.
    if plan.intent_type == IntentType.NEEDS_INPUT and references_prior_work(plan.user_message):
        return _reference_lookup_react(plan)

    # A degraded-but-ok plan with no structural blocker just executes.
    if validation.ok:
        # ``display_degraded_warnings`` drops contract self-heal messages so a
        # skill ``missing_message`` never leaks to the user as a degraded note.
        return RecoveryOutcome(
            action=ACTION_EXECUTE,
            plan=plan,
            notes=list(validation.display_degraded_warnings),
            rung="ok",
        )

    # Rung 3 — the sole blocker is one user-suppliable field: ask, don't fail.
    single = _single_missing_field(validation)
    if single is not None:
        field_name, message = single
        needs = _needs_input_plan(
            plan.user_message,
            task_id=plan.task_id,
            # ``ask`` marks this text as written for the user: Rung 3 exists
            # precisely because one field the user can supply is the only
            # blocker, so the finding message *is* the question.
            missing_inputs=[{"field": field_name, "reason": message, "ask": message}],
            rationale="only a single user-suppliable input is missing; ask instead of failing",
        )
        return RecoveryOutcome(
            action=ACTION_NEEDS_INPUT,
            plan=needs,
            notes=[message],
            missing_inputs=list(needs.missing_inputs),
            rung="3_needs_input",
        )

    # Rung 4 — the floor: hand to the capable assistant with the findings as context.
    assistant = build_react_recovery_plan(
        plan,
        rationale="deterministic plan not executable for a non-safety reason; capable assistant takes over",
    )
    return RecoveryOutcome(
        action=ACTION_REACT,
        plan=assistant,
        notes=[
            *_react_notes(validation.findings),
            *_recovery_execution_notes(plan),
            _REDERIVE_NOTE,
        ],
        rung="4_react",
    )


def _single_missing_field(validation: PlanValidationResult) -> tuple[str, str] | None:
    blocking = [f for f in validation.findings if f.severity == SEVERITY_BLOCKING]
    if not blocking:
        return None
    # One objective gap can legitimately produce more than one finding: schema
    # validation reports the invalid value while resolver evidence reports that
    # the same exact slot is unproved. Treat those as one user-suppliable field,
    # not as multiple blockers that force a generic ReAct fallback. Provider
    # contract-definition failures are never user-repairable.
    if any(
        not finding.missing_field
        or finding.owner == "provider"
        or finding.repair_strategy == "provider_contract_fix"
        for finding in blocking
    ):
        return None
    identities = {
        (
            finding.scope,
            finding.step_id,
            finding.field_path or finding.missing_field,
        )
        for finding in blocking
    }
    if len(identities) == 1:
        finding = next(
            (
                item
                for item in blocking
                if item.code in {"step_input_contract", "provider_input_contract"}
            ),
            blocking[0],
        )
        return finding.missing_field, finding.message
    return None


def _reference_lookup_react(plan: IntentPlan) -> RecoveryOutcome:
    """Turn a history-blind clarifying question into a capable look-it-up turn."""
    assistant = build_assistant_plan(
        plan.user_message,
        task_id=plan.task_id,
        rationale=(
            "request references prior work; look it up with tools before asking the user "
            "to re-clarify"
        ),
    )
    # Surface the cross-session recent-activity digest so the referent resolves
    # deterministically rather than via fuzzy recall alone.
    assistant.context_policy.include_recent_activity = True
    notes = [
        "The request refers to earlier output. Use recent activity and get_task / "
        "get_subtask / open_artifact / memory_search to find it; only ask the user if no "
        "matching prior work exists."
    ]
    return RecoveryOutcome(action=ACTION_REACT, plan=assistant, notes=notes, rung="4_react_lookup")


# Rung 4 replaces an executor that existed to *produce* something this turn, so
# the floor inherits that obligation. Without saying so, a request repeated after
# an earlier answer is cheapest to satisfy by restating that answer: asked twice
# to review the day's commits, the floor read the previous review out of context
# and returned it, and nothing in the turn had looked at a commit. The lookup
# rung (``4_react_lookup``) deliberately says the opposite — reuse prior work —
# which is why this belongs on this branch and not in the shared context block.
_REDERIVE_NOTE = (
    "The route that would have produced this output did not run, so nothing has "
    "produced it yet. Gather the evidence and derive the answer in this turn; "
    "restating a conclusion from an earlier turn does not satisfy the request."
)


def _react_notes(findings: list[PlanFinding]) -> list[str]:
    notes: list[str] = []
    for finding in findings:
        if finding.severity == "degraded":
            continue
        where = f"step '{finding.step_id}'" if finding.step_id else "plan"
        if finding.code in {"step_input_contract", "provider_input_contract"}:
            # Self-heal: never echo the skill's raw contract message to the floor
            # as a user-visible note; instruct it to look the value up.
            notes.append(
                f"{where} needs an input resolved with tools before it can run; "
                "look it up (do not ask the user, do not invent it)."
            )
            continue
        notes.append(f"{where} ({finding.skill_name or finding.code}) cannot run deterministically: {finding.message}")
    if not notes:
        notes.append("The deterministic plan failed validation; continue with bounded tools or ask the user to clarify.")
    return notes


def _recovery_execution_notes(plan: IntentPlan) -> list[str]:
    """Tell the model how to satisfy the retained exact-provider obligations."""

    if plan.workflow_steps:
        step_ids = [
            str(step.get("id") or "")
            for step in plan.workflow_steps
            if str(step.get("id") or "")
        ]
        return [
            "After resolving objective inputs, call run_workflow with the same "
            f"authorised step ids/providers ({', '.join(step_ids)}); change only "
            "their inputs. Replacing the provider DAG requires a new plan."
        ]
    if plan.selected_skills:
        names = [
            selection.skill
            for selection in plan.selected_skills
            if selection.skill
        ]
        return [
            "After resolving objective inputs, call run_skill in foreground or "
            f"background mode with the authorised provider ({', '.join(names)}). "
            "A replacement provider requires a new plan."
        ]
    return []


def _needs_input_plan(
    text: str,
    *,
    task_id: str,
    missing_inputs: list[dict],
    rationale: str,
) -> IntentPlan:
    return needs_input_plan(
        text,
        task_id=task_id,
        missing_inputs=missing_inputs,
        rationale=rationale,
        confidence=0.7,
    )


# ── orchestrator plumbing (kept here so the orchestrator only dispatches) ──


def recovery_event(recovery: RecoveryOutcome, validation: PlanValidationResult) -> dict[str, Any]:
    """Return ``runs.append_event`` kwargs describing the recovery decision."""
    return {
        "event_type": "plan.recovery",
        "status": "succeeded",
        "name": recovery.action,
        "output_json": {
            "action": recovery.action,
            "rung": recovery.rung,
            "findings": [f.code for f in validation.findings],
            "notes": recovery.notes[:6],
        },
        "summary": f"recovery {recovery.action} ({recovery.rung})"[:220],
    }


def hard_stop_reasons(recovery: RecoveryOutcome, validation: PlanValidationResult) -> list[str]:
    return recovery.notes or list(validation.errors)


def react_context_block(notes: list[str]) -> str:
    """Render the recovery findings as a bounded context block for the ReAct floor."""
    if not notes:
        return ""
    return (
        "[Plan recovery] The deterministic plan could not run directly. Continue with bounded tools or ask "
        "the user to clarify. Do not invent missing identifiers or sources:\n"
        + "\n".join(f"- {note}" for note in notes[:6])
    )
