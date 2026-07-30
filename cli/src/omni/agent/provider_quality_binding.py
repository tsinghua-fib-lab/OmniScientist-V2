"""Exact provider-quality bindings for workflow and selected-skill executions.

Standalone selected skills use the same provider-owned quality model as
workflow steps.  This module materializes their task obligations and carries
the host-owned assessment identity through the sealed provider authority so a
provider can judge quality without being trusted to identify its own consumer.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omni.agent.provider_binding import provider_binding_id, provider_contract_hash
from omni.agent.task_contract import (
    bind_task_contract_providers,
    build_task_contract,
    provider_quality_checks,
    task_contract_deliverables,
)
from omni.runtime.deliverable_assessment import bind_deliverable_assessment_identity

if TYPE_CHECKING:
    from omni.agent.intent_plan import IntentPlan

ASSESSMENT_IDENTITY_SCHEMA = "omni.provider-assessment-identity/v1"


def materialize_selected_skill_quality(
    plan: IntentPlan,
    registry: Any,
) -> list[dict[str, str]]:
    """Bind v2 selected-skill quality contracts into the task contract.

    The provider contract is the only source of check identifiers and
    deliverable identity.  Returned issues are objective missing/inconsistent
    bindings that the plan validator must reject.
    """

    if (
        plan.plan_schema_version < 2
        or not plan._plan_schema_version_present  # noqa: SLF001
    ):
        return []

    selected_steps: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    capabilities = _selected_capabilities(plan)
    contract = (
        copy.deepcopy(plan.task_contract)
        if isinstance(plan.task_contract, dict) and plan.task_contract
        else build_task_contract(
            objective=plan.user_message,
            deliverables=list(plan.outputs),
            capabilities=capabilities,
            workflow_steps=[],
            provenance_mode=plan.provenance_mode,
            confidence=plan.confidence,
        )
    )
    contract["schema_version"] = 2
    contract["autonomy"] = "selected_skill_execution"
    contract["step_count"] = len(plan.selected_skills)

    for index, selection in enumerate(plan.selected_skills):
        source = str(getattr(selection, "skill_source", "") or "")
        entry = registry.resolve_ref(selection.skill, source)
        if entry is None:
            continue
        quality = (
            copy.deepcopy(entry.quality_contract)
            if isinstance(getattr(entry, "quality_contract", None), dict)
            else {}
        )
        if quality.get("assessment_required") is not True:
            continue
        checks = _declared_checks(quality)
        binding = (
            plan.provider_bindings[index]
            if index < len(plan.provider_bindings)
            and isinstance(plan.provider_bindings[index], dict)
            else {}
        )
        step_id = _selected_step_id(index)
        if not checks:
            issues.append(
                _issue(
                    selection.skill,
                    step_id,
                    "provider quality contract requires an assessment but declares no checks",
                )
            )
            continue
        if not _binding_matches_entry(binding, entry):
            issues.append(
                _issue(
                    selection.skill,
                    step_id,
                    "selected skill has no exact sealed provider quality binding",
                )
            )
            continue
        deliverable_id = _provider_deliverable_id(entry, selection)
        selected_steps.append(
            {
                "id": step_id,
                "skill_name": selection.skill,
                "skill_source": str(getattr(entry, "source", "") or source),
                "capability": _selection_capability(selection),
                "deliverable": deliverable_id,
                "deliverable_id": deliverable_id,
                "required": True,
                "provider_binding_id": str(binding["provider_binding_id"]),
                "provider_contract_hash": str(binding["contract_hash"]),
                "provider_name": str(getattr(entry, "name", "") or selection.skill),
                "provider_source": str(getattr(entry, "source", "") or source),
                "provider_version": str(getattr(entry, "version", "") or ""),
                "quality_contract": quality,
                "input_pointer": (
                    f"/provider_inputs/{_escape_pointer(selection.skill)}"
                ),
            }
        )

    plan.task_contract = bind_task_contract_providers(contract, selected_steps)
    plan.verification_plan.deliverable_checks = provider_quality_checks(
        selected_steps
    )
    return issues


def selected_skill_assessment_identity(
    plan: IntentPlan,
    index: int,
) -> dict[str, Any]:
    """Return one exact task-contract identity for an execution authority."""

    if index < 0 or index >= len(plan.selected_skills):
        return {}
    if index >= len(plan.provider_bindings):
        return {}
    binding = plan.provider_bindings[index]
    if not isinstance(binding, dict):
        return {}
    selection = plan.selected_skills[index]
    step_id = _selected_step_id(index)
    matches = [
        item
        for item in task_contract_deliverables(plan.task_contract)
        if str(item.get("consumer_step_id") or "") == step_id
        and str(item.get("provider_binding_id") or "")
        == str(binding.get("provider_binding_id") or "")
        and str(item.get("provider_contract_hash") or "")
        == str(binding.get("contract_hash") or "")
    ]
    if len(matches) != 1:
        return {}
    obligation = matches[0]
    checks = [
        str(item)
        for item in obligation.get("required_checks") or []
        if str(item)
    ]
    if not checks:
        return {}
    return {
        "schema": ASSESSMENT_IDENTITY_SCHEMA,
        "consumer_kind": "selected_skill",
        "consumer_id": str(index),
        "binding_consumer_kind": str(
            binding.get("consumer_kind") or "plan_selection"
        ),
        "binding_consumer_id": str(
            binding.get("consumer_id") or selection.skill
        ),
        "id": step_id,
        "deliverable_id": str(obligation.get("id") or ""),
        "provider_binding_id": str(binding.get("provider_binding_id") or ""),
        "provider_contract_hash": str(binding.get("contract_hash") or ""),
        "provider_name": str(binding.get("provider_name") or selection.skill),
        "provider_source": str(
            binding.get("provider_source")
            or getattr(selection, "skill_source", "")
            or ""
        ),
        "capability": _selection_capability(selection),
        "required_checks": checks,
    }


def workflow_step_assessment_identity(
    step: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the same sealed assessment identity for one workflow consumer."""

    quality = (
        step.get("quality_contract")
        if isinstance(step.get("quality_contract"), Mapping)
        else {}
    )
    checks = _declared_checks(quality)
    step_id = str(step.get("id") or "").strip()
    binding_id = str(step.get("provider_binding_id") or "").strip()
    contract_hash = str(step.get("provider_contract_hash") or "").strip()
    provider_name = str(
        step.get("provider_name")
        or step.get("skill_name")
        or step.get("skill")
        or ""
    ).strip()
    provider_source = str(step.get("provider_source") or "").strip()
    if (
        quality.get("assessment_required") is not True
        or not checks
        or not all(
            (
                step_id,
                binding_id,
                contract_hash,
                provider_name,
                provider_source,
            )
        )
    ):
        return {}
    return {
        "schema": ASSESSMENT_IDENTITY_SCHEMA,
        "consumer_kind": "workflow_step",
        "consumer_id": step_id,
        "binding_consumer_kind": str(
            step.get("consumer_kind") or "workflow_step"
        ),
        "binding_consumer_id": step_id,
        "id": step_id,
        "deliverable_id": str(step.get("deliverable_id") or step_id),
        "provider_binding_id": binding_id,
        "provider_contract_hash": contract_hash,
        "provider_name": provider_name,
        "provider_source": provider_source,
        "capability": str(step.get("capability") or ""),
        "required_checks": checks,
    }


def provider_assessment_binding(
    authority: Mapping[str, Any] | None,
    entry: Any,
) -> tuple[dict[str, Any] | None, str]:
    """Validate and return one host-owned assessment identity at execution.

    An absent marker is a legacy/direct standalone execution and remains
    compatible. A v2 workflow or selected-skill authority explicitly marks the
    identity as required, so missing or inconsistent data fails closed.
    """

    sealed = authority if isinstance(authority, Mapping) else {}
    required = sealed.get("assessment_identity_required") is True
    raw = sealed.get("assessment_identity")
    if raw is None and not required:
        return None, ""
    if not isinstance(raw, Mapping):
        return None, (
            "provider assessment identity is missing; "
            "re-plan or re-submit before running"
        )
    identity = dict(raw)
    required_strings = (
        "consumer_kind",
        "consumer_id",
        "binding_consumer_kind",
        "binding_consumer_id",
        "id",
        "deliverable_id",
        "provider_binding_id",
        "provider_contract_hash",
        "provider_name",
        "provider_source",
    )
    if (
        str(identity.get("schema") or "") != ASSESSMENT_IDENTITY_SCHEMA
        or any(not str(identity.get(key) or "").strip() for key in required_strings)
        or str(identity.get("consumer_kind") or "")
        not in {"selected_skill", "workflow_step"}
    ):
        return None, (
            "provider assessment identity is invalid; "
            "re-plan or re-submit before running"
        )

    consumer_kind = str(identity["consumer_kind"])
    consumer_id = str(identity["consumer_id"])
    provider_name = str(getattr(entry, "name", "") or "")
    provider_source = str(getattr(entry, "source", "") or "")
    capability = str(identity.get("capability") or "")
    expected_binding = provider_binding_id(
        {
            "consumer_kind": str(identity["binding_consumer_kind"]),
            "id": str(identity["binding_consumer_id"]),
            "capability": capability,
        },
        entry,
    )
    expected_checks = _declared_checks(
        getattr(entry, "quality_contract", None)
    )
    actual_checks = [
        str(item)
        for item in identity.get("required_checks") or []
        if isinstance(item, str) and item
    ]
    expected_step_id = (
        _selected_step_id(int(consumer_id))
        if consumer_kind == "selected_skill" and consumer_id.isdigit()
        else consumer_id
        if consumer_kind == "workflow_step"
        else ""
    )
    if (
        getattr(entry, "quality_contract", {}).get("assessment_required")
        is not True
        or str(sealed.get("consumer_kind") or "") != consumer_kind
        or str(sealed.get("consumer_id") or "") != consumer_id
        or str(identity["provider_name"]) != provider_name
        or str(identity["provider_source"]) != provider_source
        or str(identity["provider_contract_hash"])
        != provider_contract_hash(entry)
        or str(identity["provider_binding_id"]) != expected_binding
        or str(identity["id"]) != expected_step_id
        or actual_checks != expected_checks
    ):
        return None, (
            "provider assessment identity does not match the live "
            "provider contract; re-plan or re-submit before running"
        )
    return identity, ""


def standalone_assessment_binding(
    authority: Mapping[str, Any] | None,
    entry: Any,
) -> tuple[dict[str, Any] | None, str]:
    """Compatibility alias for the now consumer-agnostic execution validator."""

    return provider_assessment_binding(authority, entry)


def bind_standalone_deliverable_assessment_identity(
    result: dict[str, Any],
    authority: Mapping[str, Any] | None,
    entry: Any,
) -> str:
    """Overwrite provider-supplied identity from a validated authority.

    Returns an objective authority error, or ``""`` after binding.  Providers
    retain ownership of criterion status, evidence, effective inputs, feedback,
    and retryability.
    """

    identity, error = provider_assessment_binding(authority, entry)
    if error or identity is None:
        return error
    bind_deliverable_assessment_identity(result, identity)
    return ""


def _provider_deliverable_id(entry: Any, selection: Any) -> str:
    quality = getattr(entry, "quality_contract", None)
    if isinstance(quality, Mapping) and quality.get("deliverable_id"):
        return str(quality["deliverable_id"])
    deliverables = getattr(entry, "deliverables", None)
    if isinstance(deliverables, list):
        declared = next((str(item) for item in deliverables if str(item)), "")
        if declared:
            return declared
    return _selection_capability(selection) or str(selection.skill)


def _selected_capabilities(plan: IntentPlan) -> list[str]:
    return _unique(
        [
            str(capability)
            for selection in plan.selected_skills
            for capability in selection.matched_capabilities
            if str(capability)
        ]
    )


def _selection_capability(selection: Any) -> str:
    return next(
        (
            str(capability)
            for capability in getattr(selection, "matched_capabilities", ())
            if str(capability)
        ),
        "",
    )


def _declared_checks(quality: Any) -> list[str]:
    if not isinstance(quality, Mapping):
        return []
    checks = quality.get("checks")
    if not isinstance(checks, list):
        return []
    return _unique(
        [
            item.strip()
            for item in checks
            if isinstance(item, str) and item.strip()
        ]
    )


def _binding_matches_entry(binding: Mapping[str, Any], entry: Any) -> bool:
    return bool(
        str(binding.get("provider_binding_id") or "")
        and str(binding.get("contract_hash") or "")
        == provider_contract_hash(entry)
        and str(binding.get("provider_name") or "")
        == str(getattr(entry, "name", "") or "")
        and str(binding.get("provider_source") or "")
        == str(getattr(entry, "source", "") or "")
    )


def _selected_step_id(index: int) -> str:
    return f"selected_skill:{index}"


def _issue(skill_name: str, step_id: str, message: str) -> dict[str, str]:
    return {
        "skill_name": skill_name,
        "step_id": step_id,
        "message": message,
    }


def _escape_pointer(value: str) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = [
    "ASSESSMENT_IDENTITY_SCHEMA",
    "bind_standalone_deliverable_assessment_identity",
    "materialize_selected_skill_quality",
    "provider_assessment_binding",
    "selected_skill_assessment_identity",
    "standalone_assessment_binding",
    "workflow_step_assessment_identity",
]
