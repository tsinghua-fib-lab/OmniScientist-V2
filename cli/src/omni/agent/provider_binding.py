"""Exact provider identities shared by planning, facts, and verification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from omni.skills_runtime.registry import resolve_step_entry

if TYPE_CHECKING:
    from omni.agent.intent_plan import IntentPlan


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    """One consumer bound to one exact provider contract."""

    provider_binding_id: str
    consumer_kind: str
    consumer_id: str
    step_id: str
    capability: str
    provider_name: str
    provider_source: str
    provider_version: str
    contract_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def provider_contract_hash(entry: Any) -> str:
    """Hash every provider contract surface that affects accepted execution."""
    return _canonical_hash(
        {
            "name": str(getattr(entry, "name", "") or ""),
            "source": str(getattr(entry, "source", "") or ""),
            "version": str(getattr(entry, "version", "") or ""),
            "input_schema": _declared_contract_value(getattr(entry, "input_schema", None)),
            "input_schema_declared": getattr(entry, "input_schema_declared", None),
            "output_schema": _declared_contract_value(getattr(entry, "output_schema", None)),
            "output_schema_declared": getattr(entry, "output_schema_declared", None),
            "template_signatures": getattr(entry, "template_signatures", None) or {},
        }
    )


def _declared_contract_value(value: Any) -> Any:
    """Preserve explicit falsy JSON-Schema values in provider identity."""
    return {} if value is None else value


def provider_binding_id(step: dict[str, Any], entry: Any) -> str:
    """Return a stable id for an exact consumer/provider/contract tuple."""
    payload = {
        "consumer_kind": str(step.get("consumer_kind") or "workflow_step"),
        "consumer_id": str(step.get("id") or step.get("consumer_id") or ""),
        "capability": str(step.get("capability") or ""),
        "provider_name": str(getattr(entry, "name", "") or ""),
        "provider_source": str(getattr(entry, "source", "") or ""),
        "provider_version": str(getattr(entry, "version", "") or ""),
        "contract_hash": provider_contract_hash(entry),
    }
    return "provider-binding-" + _canonical_hash(payload)[:20]


def native_provider_binding(
    step: dict[str, Any],
    *,
    provider_name: str = "synthesis.final",
    provider_version: str = "1",
) -> ProviderBinding:
    """Bind a built-in native provider without pretending it is a skill."""
    consumer_id = str(step.get("id") or provider_name)
    capability = str(step.get("capability") or provider_name)
    input_schema, output_schema = _native_synthesis_contract()
    contract_hash = _canonical_hash(
        {
            "name": provider_name,
            "source": "native",
            "version": provider_version,
            "input_schema": input_schema,
            "output_schema": output_schema,
        }
    )
    binding_id = (
        "provider-binding-"
        + _canonical_hash(
            {
                "consumer_kind": "workflow_step",
                "consumer_id": consumer_id,
                "provider_name": provider_name,
                "provider_source": "native",
                "contract_hash": contract_hash,
            }
        )[:20]
    )
    return ProviderBinding(
        provider_binding_id=binding_id,
        consumer_kind="workflow_step",
        consumer_id=consumer_id,
        step_id=consumer_id,
        capability=capability,
        provider_name=provider_name,
        provider_source="native",
        provider_version=provider_version,
        contract_hash=contract_hash,
    )


def materialize_provider_bindings(
    plan: IntentPlan,
    registry: Any,
) -> list[ProviderBinding]:
    """Seal exact provider bindings onto the plan and workflow consumers."""
    bindings: list[ProviderBinding] = []
    preserve_legacy_revision = preserves_legacy_accepted_revision(plan)
    if str(getattr(plan.intent_type, "value", plan.intent_type)) == "workflow":
        for step in plan.workflow_steps:
            binding = _step_provider_binding(
                step,
                resolve_step_entry(registry, step),
                seal=not preserve_legacy_revision,
            )
            if binding is None:
                continue
            bindings.append(binding)
    else:
        for selection in plan.selected_skills:
            source = str(getattr(selection, "skill_source", "") or "")
            entry = (
                registry.resolve_ref(selection.skill, source)
                if callable(getattr(registry, "resolve_ref", None))
                else None
            )
            if entry is None:
                continue
            # Convert an implicit winner into an exact durable reference. An
            # already source-qualified selection is left unchanged. Accepted
            # v1 revisions are validated read-only so their canonical content
            # remains identical to the persisted approval snapshot.
            if not source and not preserve_legacy_revision:
                selection.skill_source = str(getattr(entry, "source", "") or "")
            capability = (
                str(selection.matched_capabilities[0]) if selection.matched_capabilities else ""
            )
            pseudo_step = {
                "consumer_kind": "plan_selection",
                "id": selection.skill,
                "capability": capability,
            }
            raw = _binding_for_step(pseudo_step, entry)
            bindings.append(
                ProviderBinding(
                    **{
                        **raw.to_dict(),
                        "consumer_kind": "plan_selection",
                        "step_id": "",
                    }
                )
            )
    if not preserve_legacy_revision:
        plan.provider_bindings = [binding.to_dict() for binding in bindings]
    return bindings


def preserves_legacy_accepted_revision(plan: IntentPlan) -> bool:
    """Return whether provider migration must leave an accepted v1 plan intact.

    Provider bindings are an execution-hardening addition in plan schema v2.
    Rewriting an already accepted v1 snapshot would invalidate its persisted
    content hash. Validation may still resolve its providers, but only runtime
    derivatives may carry the new sealed fields.
    """
    return bool(
        plan.revision_hash
        and not plan._plan_schema_version_present  # noqa: SLF001
        and not plan._provider_bindings_present  # noqa: SLF001
    )


def materialize_step_provider_binding(
    step: dict[str, Any],
    entry: Any | None,
) -> ProviderBinding | None:
    """Seal one workflow step at every planning/runtime ingress."""
    return _step_provider_binding(step, entry, seal=True)


def _step_provider_binding(
    step: dict[str, Any],
    entry: Any | None,
    *,
    seal: bool,
) -> ProviderBinding | None:
    if _is_native_step(step):
        binding = native_provider_binding(step)
    else:
        if entry is None:
            return None
        binding = _binding_for_step(step, entry)
    if seal:
        _seal_step_binding(step, binding)
    return binding


def _binding_for_step(step: dict[str, Any], entry: Any) -> ProviderBinding:
    consumer_kind = str(step.get("consumer_kind") or "workflow_step")
    consumer_id = str(step.get("id") or "")
    return ProviderBinding(
        provider_binding_id=provider_binding_id(step, entry),
        consumer_kind=consumer_kind,
        consumer_id=consumer_id,
        step_id=consumer_id if consumer_kind == "workflow_step" else "",
        capability=str(step.get("capability") or ""),
        provider_name=str(getattr(entry, "name", "") or ""),
        provider_source=str(getattr(entry, "source", "") or ""),
        provider_version=str(getattr(entry, "version", "") or ""),
        contract_hash=provider_contract_hash(entry),
    )


def _seal_step_binding(step: dict[str, Any], binding: ProviderBinding) -> None:
    step["provider_binding_id"] = binding.provider_binding_id
    step["provider_contract_hash"] = binding.contract_hash
    step["provider_name"] = binding.provider_name
    step["provider_source"] = binding.provider_source
    step["provider_version"] = binding.provider_version


def _is_native_step(step: dict[str, Any]) -> bool:
    provider = str(step.get("provider_type") or step.get("provider") or "").lower()
    capability = str(step.get("capability") or "").lower()
    return provider == "native_executor" or capability in {
        "synthesis.final",
        "draft.section",
        "draft.manuscript",
    }


def _native_synthesis_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    # Local import avoids loading the runtime executor during ordinary skill
    # discovery while keeping planning and execution on one native contract.
    from omni.runtime.final_synthesis import (
        NATIVE_SYNTHESIS_INPUT_SCHEMA,
        NATIVE_SYNTHESIS_OUTPUT_SCHEMA,
    )

    return NATIVE_SYNTHESIS_INPUT_SCHEMA, NATIVE_SYNTHESIS_OUTPUT_SCHEMA


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="backslashreplace")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ProviderBinding",
    "materialize_provider_bindings",
    "materialize_step_provider_binding",
    "preserves_legacy_accepted_revision",
    "native_provider_binding",
    "provider_binding_id",
    "provider_contract_hash",
]
