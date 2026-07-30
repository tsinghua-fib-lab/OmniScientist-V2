"""Pure helpers for the authoritative typed-plan revision lifecycle."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from omni.agent.intent_plan import IntentPlan
from omni.agent.plan_revision import PlanRevision, canonical_plan_hash
from omni.agent.plan_validator import (
    SEVERITY_BLOCKING,
    SEVERITY_DEGRADED,
    SEVERITY_SAFETY,
    PlanFinding,
    PlanValidationResult,
)
from omni.skills_runtime.registry import resolve_step_entry, step_skill_source


@dataclass(frozen=True, slots=True)
class RepairDecision:
    """Host verdict for one model-proposed candidate revision."""

    accepted: bool
    reason: str
    diff: tuple[dict[str, Any], ...] = ()


def model_repair_findings(
    settings: Any,
    plan: IntentPlan,
    findings: list[PlanFinding],
    *,
    registry: Any = None,
) -> list[PlanFinding]:
    """Return only findings inside the owner's repair and contract boundary."""
    cfg = getattr(settings, "planner", None)
    mode = str(getattr(cfg, "model_repair", "off") or "off").lower()
    if mode == "off":
        return []
    patchable = [
        finding
        for finding in findings
        if finding.repairable
        and finding.owner in {"", "model", "planner"}
        and finding.field_path
    ]
    if not patchable:
        return []
    if mode == "auto":
        return [
            finding
            for finding in patchable
            if _finding_has_full_trusted_contract(plan, finding, registry)
        ]
    allowed = {
        str(item)
        for item in getattr(cfg, "model_repair_capabilities", []) or []
    }
    return [
        finding
        for finding in patchable
        if _finding_capability(plan, finding) in allowed
    ]


def _finding_capability(plan: IntentPlan, finding: PlanFinding) -> str:
    capability = str(
        finding.capability or finding.capability_instance or ""
    )
    if capability:
        return capability
    path = finding.field_path
    if path.startswith("/workflow_steps/"):
        try:
            index = int(path.split("/", 3)[2])
            return str(plan.workflow_steps[index].get("capability") or "")
        except (IndexError, TypeError, ValueError):
            return ""
    if path.startswith("/capability_inputs/"):
        raw = path.split("/", 3)[2]
        return raw.replace("~1", "/").replace("~0", "~")
    return ""


def _finding_has_full_trusted_contract(
    plan: IntentPlan,
    finding: PlanFinding,
    registry: Any,
) -> bool:
    if registry is None:
        return False
    skill_name = str(finding.skill_name or "")
    step = (
        next(
            (
                item
                for item in plan.workflow_steps
                if str(item.get("id") or "") == finding.step_id
            ),
            {},
        )
        if finding.step_id
        else {}
    )
    if not skill_name and step:
        skill_name = str(step.get("skill_name") or step.get("skill") or "")
    entry = resolve_step_entry(registry, step) if skill_name and step else None
    if step and step_skill_source(step) and entry is None:
        return False
    selection = next(
        (
            item
            for item in plan.selected_skills
            if (
                (skill_name and item.skill == skill_name)
                or (
                    not skill_name
                    and _finding_capability(plan, finding)
                    in item.matched_capabilities
                )
            )
        ),
        None,
    )
    if entry is None and selection is not None:
        selected_source = str(
            getattr(selection, "skill_source", "") or ""
        )
        entry = resolve_step_entry(
            registry,
            {
                "skill_name": selection.skill,
                "skill_source": selected_source,
            },
        )
        if selected_source and entry is None:
            return False
    if entry is None and skill_name and selection is None:
        entry = registry.get(skill_name)
    if entry is None:
        capability = _finding_capability(plan, finding)
        entry, _ = registry.resolve_capability(capability) if capability else (None, "")
    return bool(
        entry is not None
        and getattr(entry, "trusted", True)
        and str(getattr(entry, "contract_level", "")) == "full"
    )


def assess_repair_candidate(
    before: IntentPlan,
    candidate: IntentPlan,
    *,
    current_revision: PlanRevision,
    before_validation: PlanValidationResult,
    after_validation: PlanValidationResult,
    targeted_finding_ids: set[str],
) -> RepairDecision:
    """Accept only a scoped, invariant-preserving, strictly better candidate."""
    if canonical_plan_hash(before) != current_revision.content_hash:
        return RepairDecision(False, "authoritative plan changed during repair")
    changed = _hard_invariant_changes(before, candidate)
    if changed:
        return RepairDecision(
            False,
            "candidate changed immutable plan fields: " + ", ".join(changed),
        )
    remaining = {finding.finding_id for finding in after_validation.findings}
    if targeted_finding_ids.intersection(remaining):
        return RepairDecision(False, "targeted findings remain after revalidation")
    if after_validation.has_safety_finding:
        return RepairDecision(False, "candidate introduced a safety finding")
    if any(
        finding.severity == SEVERITY_BLOCKING
        for finding in after_validation.findings
    ):
        return RepairDecision(False, "candidate remains structurally blocked")
    if _finding_score(after_validation) >= _finding_score(before_validation):
        return RepairDecision(False, "candidate did not strictly reduce findings")
    diff = tuple(plan_diff(before, candidate))
    if not diff:
        return RepairDecision(False, "candidate made no executable plan change")
    return RepairDecision(True, "candidate accepted", diff=diff)


def plan_diff(before: IntentPlan, after: IntentPlan) -> list[dict[str, Any]]:
    """Return a deterministic, audit-safe structural diff."""
    output: list[dict[str, Any]] = []
    _diff_value(before.to_dict(), after.to_dict(), "", output)
    return output


def _finding_score(result: PlanValidationResult) -> tuple[int, int, int, int]:
    return (
        sum(f.severity == SEVERITY_SAFETY for f in result.findings),
        sum(f.severity == SEVERITY_BLOCKING for f in result.findings),
        sum(f.severity == SEVERITY_DEGRADED for f in result.findings),
        len(result.findings),
    )


def _hard_invariant_changes(before: IntentPlan, after: IntentPlan) -> list[str]:
    checks = {
        "task_id": (before.task_id, after.task_id),
        "user_message": (before.user_message, after.user_message),
        "outputs": (before.outputs, after.outputs),
        "tool_policy": (before.tool_policy.to_dict(), after.tool_policy.to_dict()),
        "context_policy": (
            before.context_policy.to_dict(),
            after.context_policy.to_dict(),
        ),
        "verification_plan": (
            before.verification_plan.to_dict(),
            after.verification_plan.to_dict(),
        ),
        "task_contract": (before.task_contract, after.task_contract),
        "workflow_dag": (before.workflow_dag, after.workflow_dag),
        "steps": (
            _step_identities(before.workflow_steps),
            _step_identities(after.workflow_steps),
        ),
    }
    return [name for name, (left, right) in checks.items() if left != right]


def _step_identities(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": step.get("id"),
            "capability": step.get("capability"),
            "skill": step.get("skill_name") or step.get("skill"),
            "depends_on": copy.deepcopy(step.get("depends_on") or []),
            "required": step.get("required", True),
            "deliverable": step.get("deliverable"),
        }
        for step in steps
    ]


def _diff_value(
    before: Any,
    after: Any,
    path: str,
    output: list[dict[str, Any]],
) -> None:
    if type(before) is not type(after):
        output.append({"op": "replace", "path": path or "/", "before": before, "after": after})
        return
    if isinstance(before, dict):
        for key in sorted(set(before) | set(after)):
            child = f"{path}/{_escape_pointer(str(key))}"
            if key not in before:
                output.append({"op": "add", "path": child, "after": copy.deepcopy(after[key])})
            elif key not in after:
                output.append({"op": "remove", "path": child, "before": copy.deepcopy(before[key])})
            else:
                _diff_value(before[key], after[key], child, output)
        return
    if isinstance(before, list):
        if before != after:
            output.append(
                {
                    "op": "replace",
                    "path": path or "/",
                    "before": copy.deepcopy(before),
                    "after": copy.deepcopy(after),
                }
            )
        return
    if before != after:
        output.append(
            {
                "op": "replace",
                "path": path or "/",
                "before": copy.deepcopy(before),
                "after": copy.deepcopy(after),
            }
        )


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


__all__ = [
    "RepairDecision",
    "assess_repair_candidate",
    "plan_diff",
    "model_repair_findings",
]
