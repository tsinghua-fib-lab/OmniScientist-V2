"""Recovery ladder: turn validation findings into the next executable state.

Validation classifies; recovery *routes*. A rejected or degraded plan is never a
dead end. Given a plan and its :class:`PlanValidationResult`, this module returns
the best next state on a fixed ladder:

    Rung 0    safety violation          → hard stop (never swallowed)
    Rung 0.75 identifier look-up handoff → a resolvable identifier the in-lane
                                          resolver could not bind is handed to
                                          the ReAct floor to act-and-look-up
                                          (search → id → fetch), never a lossy
                                          free-text rewrite
    Rung 1  grounded plan repair        → swap an identifier-bound step to a
                                          producer capability that accepts the
                                          free-text the user actually gave
                                          (grounded; ids are never invented)
    Rung 2  step degradation            → prune un-satisfiable *degradable* steps
                                          and detach their dependents, matching
                                          the workflow runtime's partial policy
    Rung 3  needs_input                 → ask for a single user-suppliable field
    Rung 4  ReAct handoff (the floor)   → hand to the capable, safety-bounded
                                          assistant every other agent uses by
                                          default, with the findings as context

Invariants: repair is attempted at most once (no loop); repair is grounded (it
only ever routes to a real search/producer, never fabricates an identifier); the
ReAct floor stays under the normal tool policy (no self-granted tools).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from omni.agent.capabilities import deliverables_from_capabilities
from omni.agent.input_resolution import extract_entity_query, is_identifier_field
from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.plan_factory import build_assistant_plan, build_react_recovery_plan
from omni.agent.plan_validator import (
    SEVERITY_BLOCKING,
    PlanFinding,
    PlanValidationResult,
    PlanValidator,
)
from omni.agent.reference_markers import references_prior_work
from omni.agent.skill_arbitrator import SkillArbitrator
from omni.agent.workflow_plan_builder import WorkflowPlanBuilder, needs_input_plan
from omni.skills_runtime.registry import resolve_step_entry

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
    *,
    allow_repair: bool = True,
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

    step_findings = [
        finding
        for finding in validation.findings
        if finding.code in {
            "step_input_contract",
            "provider_schema_invalid",
            "grounded_binding_unverified",
        }
    ]

    if plan.intent_type == IntentType.WORKFLOW and step_findings:
        # Rung 0.75 — resolvable identifier the in-lane resolver could not bind
        # (offline / no confident match). Hand it to the ReAct floor, which acts
        # and looks it up (search → id → fetch), instead of the lossy free-text
        # repair below that rewrites the workflow and drops the fetch chain.
        id_findings = _identifier_binding_findings(
            plan,
            step_findings,
            registry,
        )
        id_findings = [
            finding
            for finding in id_findings
            if (
                # A non-empty but non-canonical identifier (for example a paper
                # title in an arXiv-id field) is resolvable by acting with tools.
                # Runtime continue-with-partial policy does not license replacing
                # that fetch with search and silently weakening the goal.
                (finding.actual not in (None, "") or finding.repairable)
                and not _finding_step_explicitly_optional(plan, finding)
            )
        ]
        if id_findings:
            return _identifier_lookup_react(plan, id_findings)
        # Rung 1 — grounded repair (at most once).
        if allow_repair:
            repaired, repair_notes = _grounded_repair(plan, step_findings, registry)
            if repaired is not None:
                revalidated = PlanValidator(registry).validate(repaired)
                inner = recover(repaired, revalidated, registry, allow_repair=False)
                if inner.executable:
                    inner.notes = [*repair_notes, *inner.notes]
                    inner.rung = "1_repair"
                    return inner

        # Rung 2 — prune un-satisfiable degradable steps + detach dependents.
        pruned, prune_notes = _prune_degradable(plan, validation, registry)
        if pruned is not None:
            revalidated = PlanValidator(registry).validate(pruned)
            if pruned.intent_type == IntentType.NEEDS_INPUT:
                return RecoveryOutcome(
                    action=ACTION_NEEDS_INPUT,
                    plan=pruned,
                    notes=prune_notes,
                    missing_inputs=list(pruned.missing_inputs),
                    rung="2_degrade",
                )
            if revalidated.ok and pruned.workflow_steps:
                return RecoveryOutcome(
                    action=ACTION_EXECUTE, plan=pruned, notes=prune_notes, rung="2_degrade"
                )

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
            missing_inputs=[{"field": field_name, "reason": message}],
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
        ],
        rung="4_react",
    )


def _grounded_repair(
    plan: IntentPlan,
    step_findings: list[PlanFinding],
    registry: SkillRegistry,
) -> tuple[IntentPlan | None, list[str]]:
    """Swap identifier-bound steps whose input is free text to a producer capability.

    This never invents an identifier: it only reroutes to a capability that
    legitimately accepts the free-text query (e.g. ``literature.search``), whose
    results are real hits the downstream steps can build on.
    """
    repairs = {f.step_id: f for f in step_findings if f.repairable and f.repair_capability and f.step_id}
    if not repairs:
        return None, []

    specs = _plan_to_specs(plan)
    notes: list[str] = []
    changed = False
    for spec in specs:
        step_id = str(spec.get("id") or "")
        finding = repairs.get(step_id)
        if finding is None:
            continue
        query = _grounded_query(spec, plan.user_message)
        if not query:
            continue
        producer_entry, _ = registry.resolve_capability(finding.repair_capability)
        if producer_entry is None:
            continue
        spec["capability"] = finding.repair_capability
        # Keep recovery input provider-neutral. The provider input compiler
        # binds this semantic scalar to the selected provider's schema once.
        spec["input"] = {"input": query}
        spec.pop("skill_name", None)
        spec.pop("skill", None)
        spec.pop("identifier_field", None)
        missing = finding.missing_field or "required input"
        spec["reason"] = (
            f"grounded repair: '{finding.skill_name}' could not resolve {missing}; "
            f"routed to {finding.repair_capability} instead"
        )
        changed = True
        notes.append(
            f"The required input could not be resolved from '{query[:60]}'; the plan now uses "
            f"{finding.repair_capability} instead of inventing a value."
        )
    if not changed:
        return None, []
    rebuilt = _rebuild_workflow(plan, specs, registry, rationale_suffix="grounded repair")
    return rebuilt, notes


def _prune_degradable(
    plan: IntentPlan,
    validation: PlanValidationResult,
    registry: SkillRegistry,
) -> tuple[IntentPlan | None, list[str]]:
    """Drop degradable steps with unsatisfiable input and detach their dependents."""
    drop_ids = {
        f.step_id
        for f in validation.findings
        if f.code
        in {
            "step_input_contract",
            "provider_schema_invalid",
            "grounded_binding_unverified",
        }
        and f.step_id
        and _finding_step_is_degradable(plan, f, registry)
    }
    if not drop_ids:
        return None, []

    # ``continue_with_partial`` licenses tolerating a step's *runtime* failure so
    # independent steps proceed — it never licenses deleting a required
    # deliverable before it executes. A step that is the sole producer of a
    # required deliverable must therefore be asked about, not silently pruned.
    protected = _sole_required_producers(plan, drop_ids)
    if protected:
        missing = [
            {"field": f.missing_field or "input", "reason": f.message}
            for f in validation.findings
            if f.code
            in {
                "step_input_contract",
                "provider_schema_invalid",
                "grounded_binding_unverified",
            }
            and f.step_id in protected
        ]
        needs = _needs_input_plan(
            plan.user_message,
            task_id=plan.task_id,
            missing_inputs=missing
            or [{"field": "input", "reason": "a required deliverable is under-specified"}],
            rationale=(
                "a step that is the sole producer of a required deliverable is under-specified; "
                "ask for the missing input instead of dropping the deliverable"
            ),
        )
        notes = [
            f"Step '{step_id}' is the sole producer of a required deliverable; asking for its "
            "missing input instead of silently dropping it."
            for step_id in sorted(protected)
        ]
        return needs, notes

    specs = [spec for spec in _plan_to_specs(plan) if str(spec.get("id") or "") not in drop_ids]
    if not specs:
        # Everything was degradable and unsatisfiable → ask for what's missing.
        missing = [
            {"field": f.missing_field or "input", "reason": f.message}
            for f in validation.findings
            if f.code
            in {
                "step_input_contract",
                "provider_schema_invalid",
                "grounded_binding_unverified",
            }
            and f.step_id in drop_ids
        ]
        needs = _needs_input_plan(
            plan.user_message,
            task_id=plan.task_id,
            missing_inputs=missing or [{"field": "input", "reason": "no executable step remains"}],
            rationale="all steps were optional and under-specified",
        )
        return needs, []

    notes: list[str] = []
    for spec in specs:
        deps = [str(d) for d in spec.get("depends_on") or []]
        kept = [d for d in deps if d not in drop_ids]
        if len(kept) != len(deps):
            spec["depends_on"] = kept
    for step_id in sorted(drop_ids):
        finding = next(
            (
                f
                for f in validation.findings
                if f.code
                in {
                    "step_input_contract",
                    "provider_schema_invalid",
                    "grounded_binding_unverified",
                }
                and f.step_id == step_id
            ),
            None,
        )
        skill = finding.skill_name if finding else step_id
        notes.append(f"Skipped step '{step_id}' because {skill} lacks required input; remaining deliverables continue.")
    rebuilt = _rebuild_workflow(plan, specs, registry, rationale_suffix="degraded: pruned unsatisfiable steps")
    return rebuilt, notes


def _rebuild_workflow(
    plan: IntentPlan,
    specs: list[dict],
    registry: SkillRegistry,
    *,
    rationale_suffix: str,
) -> IntentPlan:
    builder = WorkflowPlanBuilder(SkillArbitrator(registry))
    return builder.from_specs(
        plan.user_message,
        specs,
        task_id=plan.task_id,
        rationale=f"{plan.rationale} | {rationale_suffix}".strip(" |"),
        confidence=plan.confidence or 0.7,
        provenance_mode=plan.provenance_mode,
        outputs=plan.outputs or None,
    )


def _plan_to_specs(plan: IntentPlan) -> list[dict]:
    """Copy resolved workflow steps into mutable spec dicts for re-planning."""
    return [dict(step) for step in plan.workflow_steps]


def _step_deliverables(step: dict) -> set[str]:
    capability = str(step.get("capability") or "")
    deliverables = set(deliverables_from_capabilities([capability])) if capability else set()
    # ``workflow`` is the generic fallback for capabilities with no specific
    # deliverable; it is not a concrete deliverable to protect.
    deliverables.discard("workflow")
    declared = step.get("deliverable")
    if declared:
        deliverables.add(str(declared))
    return deliverables


def _is_artifact_deliverable(deliverable: str) -> bool:
    """A user-visible rendered deliverable (figure/diagram/artifact)."""
    return "figure" in deliverable or "artifact" in deliverable


def _sole_required_producers(plan: IntentPlan, drop_ids: set[str]) -> set[str]:
    """Return drop candidates that uniquely produce a required deliverable.

    A deliverable is *required* when it is declared in ``plan.outputs`` or is a
    rendered artifact/figure the user asked for. If no surviving step produces
    that deliverable, dropping its producer would silently lose the deliverable —
    exactly the class of regression this guard prevents.
    """
    required = {str(item) for item in (plan.outputs or [])}
    surviving: set[str] = set()
    for step in plan.workflow_steps:
        if str(step.get("id") or "") in drop_ids:
            continue
        surviving |= _step_deliverables(step)
    protected: set[str] = set()
    for step in plan.workflow_steps:
        step_id = str(step.get("id") or "")
        if step_id not in drop_ids:
            continue
        produced = _step_deliverables(step)
        lost_required = {
            deliverable
            for deliverable in produced
            if (deliverable in required or _is_artifact_deliverable(deliverable))
            and deliverable not in surviving
        }
        if lost_required:
            protected.add(step_id)
    return protected


def _grounded_query(spec: dict, user_message: str) -> str:
    # Extract the entity/title (quoted span or longest Latin title run) rather
    # than dumping the whole multi-clause goal into a search — a garbled,
    # mixed-language sentence returns no results.
    return extract_entity_query(spec, user_message)


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


def _identifier_binding_findings(
    plan: IntentPlan,
    findings: list[PlanFinding],
    registry: SkillRegistry,
) -> list[PlanFinding]:
    """Return step findings whose missing field is a resolvable identifier."""
    steps_by_id = {
        str(step.get("id") or ""): step
        for step in plan.workflow_steps
    }
    out: list[PlanFinding] = []
    for finding in findings:
        if not finding.step_id or not finding.missing_field:
            continue
        step = steps_by_id.get(finding.step_id)
        entry = (
            resolve_step_entry(registry, step)
            if step is not None
            else None
        )
        if is_identifier_field(entry, finding.missing_field):
            out.append(finding)
    return out


def _finding_step_is_degradable(
    plan: IntentPlan,
    finding: PlanFinding,
    registry: SkillRegistry,
) -> bool:
    step = next(
        (
            item
            for item in plan.workflow_steps
            if str(item.get("id") or "") == finding.step_id
        ),
        {},
    )
    if not step:
        return False
    if bool(step.get("optional")) or not bool(step.get("required", True)):
        return True
    if str(step.get("failure_policy") or "") == "continue_with_partial":
        return True
    entry = resolve_step_entry(registry, step)
    workflow = getattr(entry, "workflow", {}) if entry is not None else {}
    return bool(
        isinstance(workflow, dict)
        and str(workflow.get("failure_policy") or "")
        == "continue_with_partial"
    )


def _finding_step_explicitly_optional(
    plan: IntentPlan,
    finding: PlanFinding,
) -> bool:
    """Return whether the accepted plan—not runtime policy—made a step optional."""

    step = next(
        (
            item
            for item in plan.workflow_steps
            if str(item.get("id") or "") == finding.step_id
        ),
        {},
    )
    return bool(step and (step.get("optional") is True or step.get("required") is False))


def _identifier_lookup_react(
    plan: IntentPlan, findings: list[PlanFinding]
) -> RecoveryOutcome:
    """Hand an unresolved-identifier plan to the ReAct floor to act-and-look-up."""
    assistant = build_react_recovery_plan(
        plan,
        rationale=(
            "a required identifier could not be bound in-lane; look it up with tools "
            "(search → id → fetch) before failing or asking the user"
        ),
    )
    assistant.context_policy.include_recent_activity = True
    steps_by_id = {str(step.get("id") or ""): step for step in plan.workflow_steps}
    notes: list[str] = []
    for finding in findings:
        step = steps_by_id.get(finding.step_id) or {}
        target = extract_entity_query(step, plan.user_message) or "the referenced source"
        notes.append(
            f"Resolve '{target}' to a concrete {finding.missing_field} by searching first, "
            "then fetch it. Do not ask the user for the id and do not invent one."
        )
    notes.extend(_recovery_execution_notes(plan))
    return RecoveryOutcome(action=ACTION_REACT, plan=assistant, notes=notes, rung="4_react_lookup")


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
