"""Host-scoped patches for one bounded objective provider-schema repair."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from omni.agent.intent_plan import IntentPlan
from omni.agent.plan_revision import (
    PlanRevision,
    canonical_plan_hash,
)
from omni.agent.plan_validator import PlanFinding

_MODEL_OWNERS = frozenset({"", "model", "planner"})
_IMMUTABLE_PREFIXES = (
    "/plan_id",
    "/task_id",
    "/user_message",
    "/tool_policy",
    "/context_policy",
    "/verification_plan",
    "/task_contract",
    "/workflow_dag",
    "/acceptance",
    "/execution_mode",
    "/provenance_mode",
)
_HOST_INPUT_CONTROL_FIELDS = frozenset({"_skill_source", "skill_source"})
_WORKFLOW_IDENTITY_FIELDS = frozenset(
    {
        "id",
        "kind",
        "name",
        "provider",
        "provider_type",
        "skill",
        "skill_name",
        "skill_source",
    }
)


@dataclass(frozen=True, slots=True)
class PlanPatchOp:
    op: str
    path: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "path": self.path, "value": copy.deepcopy(self.value)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PlanPatchOp:
        if "value" not in payload:
            raise ValueError("plan patch operation is missing value")
        return cls(
            op=str(payload.get("op") or ""),
            path=str(payload.get("path") or ""),
            value=copy.deepcopy(payload["value"]),
        )


@dataclass(frozen=True, slots=True)
class PlanPatch:
    base_revision: str
    finding_ids: list[str]
    operations: list[PlanPatchOp]

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_revision": self.base_revision,
            "finding_ids": list(self.finding_ids),
            "operations": [operation.to_dict() for operation in self.operations],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PlanPatch:
        return cls(
            base_revision=str(payload.get("base_revision") or ""),
            finding_ids=[str(item) for item in payload.get("finding_ids") or []],
            operations=[
                PlanPatchOp.from_dict(item)
                for item in payload.get("operations") or []
                if isinstance(item, dict)
            ],
        )


def allowed_patch_paths(
    plan: IntentPlan,
    findings: list[PlanFinding],
) -> frozenset[str]:
    """Compute the only paths a model may modify for these host findings."""
    allowed: set[str] = set()
    for finding in findings:
        if not finding.repairable or finding.owner not in _MODEL_OWNERS:
            continue
        path = finding.field_path or _legacy_finding_path(plan, finding)
        if not path or _is_immutable(path):
            continue
        if path.startswith("/workflow_steps/") and "/input/" in path:
            allowed.add(path)
        elif path.startswith("/capability_inputs/"):
            allowed.add(path)
    return frozenset(allowed)


def apply_plan_patch(
    plan: IntentPlan,
    patch: PlanPatch,
    *,
    current_revision: PlanRevision,
    findings: list[PlanFinding],
) -> IntentPlan:
    """Apply an allowlisted patch to a clone and invalidate compiled arguments."""
    if patch.base_revision != current_revision.content_hash:
        raise ValueError("stale plan patch base revision")
    if canonical_plan_hash(plan) != current_revision.content_hash:
        raise ValueError("stale authoritative plan for current revision")
    if not patch.finding_ids:
        raise ValueError("plan patch must reference at least one finding")
    if len(set(patch.finding_ids)) != len(patch.finding_ids):
        raise ValueError("plan patch contains duplicate finding ids")
    if not patch.operations:
        raise ValueError("plan patch must contain at least one operation")

    known = {finding.finding_id for finding in findings}
    if not set(patch.finding_ids) <= known:
        raise ValueError("plan patch references unknown findings")

    selected_findings = [
        finding for finding in findings if finding.finding_id in patch.finding_ids
    ]
    allowed = allowed_patch_paths(plan, selected_findings)
    seen_paths: set[str] = set()
    payload = copy.deepcopy(plan.to_dict())
    for operation in patch.operations:
        if operation.op not in {"add", "replace"}:
            raise ValueError(f"unsupported plan patch operation: {operation.op}")
        if operation.path in seen_paths:
            raise ValueError(f"duplicate plan patch path: {operation.path}")
        seen_paths.add(operation.path)
        if _is_immutable(operation.path) or operation.path not in allowed:
            raise ValueError(f"plan patch path is not allowed: {operation.path}")
        _set_pointer(
            payload,
            operation.path,
            copy.deepcopy(operation.value),
            require_existing=operation.op == "replace",
        )

    repaired = IntentPlan.from_dict(payload)
    _assert_immutable_identity(plan, repaired)
    # Provider arguments are a derived cache. Any accepted objective patch must
    # force the compiler and validator to rebuild them from the revised plan.
    repaired.provider_inputs = {}
    repaired.inputs_compiled = False
    repaired.input_compilation_errors = []
    return repaired


def _set_pointer(
    payload: dict[str, Any],
    path: str,
    value: Any,
    *,
    require_existing: bool,
) -> None:
    if not path.startswith("/"):
        raise ValueError(f"invalid JSON pointer: {path}")
    parts = [_unescape_pointer(part) for part in path.split("/")[1:]]
    if not parts:
        raise ValueError("cannot replace the plan root")
    current: Any = payload
    try:
        for part in parts[:-1]:
            current = current[int(part)] if isinstance(current, list) else current[part]
        leaf = parts[-1]
        if isinstance(current, list):
            index = int(leaf)
            if index < 0 or index >= len(current):
                raise IndexError(index)
            current[index] = value
            return
        if not isinstance(current, dict):
            raise TypeError(type(current).__name__)
        if require_existing and leaf not in current:
            raise KeyError(leaf)
        current[leaf] = value
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid plan patch path: {path}") from exc


def _legacy_finding_path(plan: IntentPlan, finding: PlanFinding) -> str:
    if not finding.step_id or not finding.missing_field:
        return ""
    for index, step in enumerate(plan.workflow_steps):
        if str(step.get("id") or "") == finding.step_id:
            field = _escape_pointer(finding.missing_field)
            return f"/workflow_steps/{index}/input/{field}"
    return ""


def _is_immutable(path: str) -> bool:
    return (
        any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in _IMMUTABLE_PREFIXES
        )
        or _is_host_control_path(path)
    )


def _is_host_control_path(path: str) -> bool:
    """Keep provider selection and workflow identity outside model patches."""
    if not path.startswith("/"):
        return True
    parts = [_unescape_pointer(part) for part in path.split("/")[1:]]
    if len(parts) >= 3 and parts[0] == "workflow_steps":
        if parts[2] in _WORKFLOW_IDENTITY_FIELDS:
            return True
        if (
            len(parts) >= 4
            and parts[2] == "input"
            and parts[3] in _HOST_INPUT_CONTROL_FIELDS
        ):
            return True
    return (
        len(parts) >= 3
        and parts[0] == "capability_inputs"
        and parts[2] in _HOST_INPUT_CONTROL_FIELDS
    )


def _assert_immutable_identity(before: IntentPlan, after: IntentPlan) -> None:
    immutable_pairs = (
        ("plan_id", before.plan_id, after.plan_id),
        ("task_id", before.task_id, after.task_id),
        ("user_message", before.user_message, after.user_message),
        ("tool_policy", before.tool_policy.to_dict(), after.tool_policy.to_dict()),
        ("context_policy", before.context_policy.to_dict(), after.context_policy.to_dict()),
        ("task_contract", before.task_contract, after.task_contract),
        ("workflow_dag", before.workflow_dag, after.workflow_dag),
        ("acceptance", before.acceptance, after.acceptance),
    )
    changed = [name for name, left, right in immutable_pairs if left != right]
    if changed:
        raise ValueError("plan patch changed immutable fields: " + ", ".join(changed))


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _unescape_pointer(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")
