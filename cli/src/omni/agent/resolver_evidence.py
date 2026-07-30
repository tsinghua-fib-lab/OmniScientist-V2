"""Objective evidence for resolver-owned provider inputs.

Resolver fields are facts, not semantic preferences.  This module derives their
requirements from the exact selected provider contract and fails closed unless
the current value is locally provable or carries matching grounded evidence.
It deliberately has no dependency on planner semantic-binding rollout modes.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.provider_binding import (
    provider_binding_id,
    provider_contract_hash,
)
from omni.core.field_contract import contract_text, field_binding_owner, field_resolver
from omni.core.field_resolvers import resolve_field
from omni.skills_runtime.registry import resolve_step_entry

if TYPE_CHECKING:
    from omni.agent.plan_validator import PlanFinding


@dataclass(frozen=True, slots=True)
class ResolverEvidence:
    """Proof attached to one exact resolver-owned provider input."""

    evidence_id: str
    slot_id: str
    provider_binding_id: str
    provider_name: str
    provider_source: str
    contract_hash: str
    field_path: str
    field_name: str
    value: Any
    resolver: str
    required_mode: str
    verification_mode: str = ""
    source: str = "provider_input_compiler"
    verified: bool = False
    step_id: str = ""
    capability_instance: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ResolverEvidence:
        required = (
            "provider_binding_id",
            "provider_name",
            "provider_source",
            "contract_hash",
            "field_path",
            "field_name",
            "resolver",
        )
        if any(not str(payload.get(key) or "") for key in required):
            raise ValueError("resolver evidence is missing binding identity")
        slot_id = str(payload.get("slot_id") or "")
        if not slot_id:
            slot_id = _stable_slot_id(
                provider_binding_id=str(payload["provider_binding_id"]),
                field_path=str(payload["field_path"]),
                resolver=str(payload["resolver"]),
            )
        evidence_id = str(payload.get("evidence_id") or "")
        if not evidence_id:
            evidence_id = _stable_evidence_id(
                slot_id=slot_id,
                value=payload.get("value"),
                contract_hash=str(payload["contract_hash"]),
                verification_mode=str(payload.get("verification_mode") or ""),
                source=str(payload.get("source") or "provider_input_compiler"),
            )
        return cls(
            evidence_id=evidence_id,
            slot_id=slot_id,
            provider_binding_id=str(payload["provider_binding_id"]),
            provider_name=str(payload["provider_name"]),
            provider_source=str(payload["provider_source"]),
            contract_hash=str(payload["contract_hash"]),
            field_path=str(payload["field_path"]),
            field_name=str(payload["field_name"]),
            value=payload.get("value"),
            resolver=str(payload["resolver"]),
            required_mode=str(payload.get("required_mode") or "grounded_search"),
            verification_mode=str(payload.get("verification_mode") or ""),
            source=str(payload.get("source") or "provider_input_compiler"),
            verified=bool(payload.get("verified", False)),
            step_id=str(payload.get("step_id") or ""),
            capability_instance=str(
                payload.get("capability_instance")
                or payload.get("capability")
                or ""
            ),
        )


@dataclass(frozen=True, slots=True)
class _ResolverSlot:
    provider_binding_id: str
    provider_name: str
    provider_source: str
    contract_hash: str
    field_path: str
    field_name: str
    value: Any
    resolver: str
    field_schema: dict[str, Any]
    step_id: str = ""
    capability_instance: str = ""

    @property
    def slot_id(self) -> str:
        return _stable_slot_id(
            provider_binding_id=self.provider_binding_id,
            field_path=self.field_path,
            resolver=self.resolver,
        )


def materialize_resolver_evidence(
    plan: IntentPlan,
    registry: Any,
) -> list[ResolverEvidence]:
    """Rebuild current resolver evidence from exact provider bindings."""
    prior = _coerce_evidence(plan.resolver_evidence)
    preserve_legacy_hash = bool(
        plan.revision_hash
        and not plan._resolver_evidence_present  # noqa: SLF001
        and (
            plan.plan_schema_version < 2
            or not plan._plan_schema_version_present  # noqa: SLF001
        )
    )
    current: list[ResolverEvidence] = []
    for slot in _resolver_slots(plan, registry):
        required_mode = required_resolver_verification_mode(
            slot.field_schema,
            value=slot.value,
            user_message=plan.user_message,
        )
        local_mode = _local_verification_mode(
            slot,
            user_message=plan.user_message,
            required_mode=required_mode,
        )
        if local_mode and verification_satisfies(required_mode, local_mode):
            achieved_mode = local_mode
            verified = True
            source = {
                "local_exists": "resolver.local_exists",
                "syntactic": "resolver.syntactic",
                "user_exact": "user_input.normalize",
            }.get(local_mode, "resolver.local")
        else:
            matched = next(
                (
                    item
                    for item in prior
                    if _evidence_matches_slot(
                        item,
                        slot,
                        required_mode=required_mode,
                    )
                ),
                None,
            )
            achieved_mode = matched.verification_mode if matched else ""
            verified = matched is not None
            source = matched.source if matched else "provider_input_compiler"
        evidence = _evidence_for_slot(
            slot,
            required_mode=required_mode,
            verification_mode=achieved_mode,
            source=source,
            verified=verified,
        )
        current.append(evidence)
    if not preserve_legacy_hash:
        plan.resolver_evidence = [item.to_dict() for item in current]
        plan._resolver_evidence_present = True  # noqa: SLF001
    return current


def validate_resolver_evidence(
    plan: IntentPlan,
    registry: Any,
) -> list[PlanFinding]:
    """Return blocking findings for every unproved resolver-owned value."""
    from omni.agent.plan_validator import SEVERITY_BLOCKING, PlanFinding

    findings: list[PlanFinding] = []
    for evidence in materialize_resolver_evidence(plan, registry):
        if evidence.verified and verification_satisfies(
            evidence.required_mode,
            evidence.verification_mode,
        ):
            continue
        findings.append(
            PlanFinding(
                code="grounded_binding_unverified",
                message=(
                    f"resolver-owned field '{evidence.field_name}' on provider "
                    f"'{evidence.provider_name}' has not met verification mode "
                    f"'{evidence.required_mode}'"
                ),
                severity=SEVERITY_BLOCKING,
                scope="step" if evidence.step_id else "plan",
                step_id=evidence.step_id,
                skill_name=evidence.provider_name,
                capability=evidence.capability_instance,
                missing_field=evidence.field_name,
                repairable=False,
                constraint_id=evidence.evidence_id,
                field_path=evidence.field_path,
                actual=evidence.value,
                expected=evidence.required_mode,
                owner="resolver",
                repair_strategy="resolver",
                capability_instance=evidence.capability_instance,
                provider_binding_id=evidence.provider_binding_id,
                provider_source=evidence.provider_source,
                provider_contract_hash=evidence.contract_hash,
            )
        )
    return findings


def seal_resolver_evidence(
    plan: IntentPlan,
    registry: Any,
    *,
    field_path: str,
    value: Any,
    verification_mode: str,
    source: str,
) -> bool:
    """Seal host-obtained evidence for one exact current provider input."""
    slot = next(
        (
            candidate
            for candidate in _resolver_slots(plan, registry)
            if candidate.field_path == field_path
            and _values_equal(candidate.value, value)
        ),
        None,
    )
    if slot is None:
        return False
    required_mode = required_resolver_verification_mode(
        slot.field_schema,
        value=slot.value,
        user_message=plan.user_message,
    )
    if not verification_satisfies(required_mode, verification_mode):
        return False
    evidence = _evidence_for_slot(
        slot,
        required_mode=required_mode,
        verification_mode=verification_mode,
        source=source,
        verified=True,
    )
    retained = [
        item
        for item in _coerce_evidence(plan.resolver_evidence)
        if item.field_path != field_path
    ]
    plan.resolver_evidence = [
        item.to_dict() for item in [*retained, evidence]
    ]
    plan._resolver_evidence_present = True  # noqa: SLF001
    return True


def required_resolver_verification_mode(
    field_schema: dict[str, Any],
    *,
    value: Any,
    user_message: str,
) -> str:
    """Select the evidence strength required for one resolver-owned value."""
    declared = contract_text(
        field_schema,
        "verification_mode",
        lower=True,
    )
    if declared:
        return declared
    resolver_name = field_resolver(field_schema)
    if resolver_name in {"file_path", "path"}:
        return "local_exists"
    if resolver_value_matches_user(
        resolver_name,
        value=value,
        user_message=user_message,
    ):
        return "user_exact"
    # A value that is *already* a canonical, self-verifying identifier — an arXiv
    # id or a DOI that the resolver normalizes to itself — is a locally provable
    # fact. Planning must not gate its admission behind a synchronous network
    # title search: a bare id carries no independent title to search against, the
    # search is slow and low-precision, and the provider proves the id exists the
    # moment it fetches it. This mirrors how Codex / Claude Code / OpenClaw trust
    # a well-formed identifier argument and let the tool validate it at call time.
    # Free text that is *not* yet a canonical id (a title sitting in an id field)
    # is not locally provable and still needs grounding below.
    if value_is_canonical_identifier(resolver_name, value):
        return "syntactic"
    return "grounded_search"


def value_is_canonical_identifier(resolver_name: str, value: Any) -> bool:
    """Whether ``value`` is already a canonical id the resolver resolves to itself.

    Uses the same offline resolver the compiler uses, so "canonical" means
    exactly "the resolver can normalize this on its own" (e.g. ``1706.03762`` for
    ``arxiv_id``). Titles and other free text do not resolve and return ``False``.
    """
    if not resolver_name:
        return False
    resolution = resolve_field(
        resolver_name,
        {"identifier": value, "input": value},
    )
    return bool(resolution.resolved)


def resolver_value_matches_user(
    resolver_name: str,
    *,
    value: Any,
    user_message: str,
) -> bool:
    """Whether a canonical resolver value is explicitly present in user text."""
    if not resolver_name or not user_message:
        return False
    resolved_value = resolve_field(
        resolver_name,
        {"identifier": value, "input": value},
    )
    if not resolved_value.resolved:
        return False
    literal = str(value or "").strip()
    if len(literal) >= 4 and literal.casefold() in user_message.casefold():
        return True
    resolved_user = resolve_field(
        resolver_name,
        {"input": user_message},
    )
    return (
        resolved_user.resolved
        and _values_equal(resolved_user.value, resolved_value.value)
    )


def verification_satisfies(required: str, achieved: str) -> bool:
    """Whether one achieved proof strength satisfies a declared requirement."""
    required = str(required or "grounded_search").casefold()
    achieved = str(achieved or "").casefold()
    accepted = {
        "grounded_search": {"grounded_search"},
        "local_exists": {"local_exists"},
        "syntactic": {
            "syntactic",
            "user_exact",
            "grounded_search",
        },
        "user_exact": {"user_exact", "grounded_search"},
    }
    return achieved in accepted.get(required, {required})


def _resolver_slots(plan: IntentPlan, registry: Any) -> list[_ResolverSlot]:
    if plan.intent_type == IntentType.WORKFLOW:
        return _workflow_slots(plan, registry)
    if plan.intent_type in {
        IntentType.QA_PLUS_ARTIFACT,
        IntentType.SINGLE_SKILL_TASK,
    }:
        return _selected_skill_slots(plan, registry)
    return []


def _workflow_slots(plan: IntentPlan, registry: Any) -> list[_ResolverSlot]:
    slots: list[_ResolverSlot] = []
    for index, step in enumerate(plan.workflow_steps):
        entry = resolve_step_entry(registry, step)
        if entry is None:
            continue
        params = step.get("input") if isinstance(step.get("input"), dict) else {}
        for field_name, field_schema in _resolver_fields(entry):
            if not _has_value(params.get(field_name)):
                continue
            field_path = (
                f"/workflow_steps/{index}/input/"
                f"{_escape_pointer(field_name)}"
            )
            slots.append(
                _ResolverSlot(
                    provider_binding_id=provider_binding_id(step, entry),
                    provider_name=str(getattr(entry, "name", "") or ""),
                    provider_source=str(getattr(entry, "source", "") or ""),
                    contract_hash=provider_contract_hash(entry),
                    field_path=field_path,
                    field_name=field_name,
                    value=params[field_name],
                    resolver=field_resolver(field_schema),
                    field_schema=field_schema,
                    step_id=str(step.get("id") or ""),
                    capability_instance=str(step.get("capability") or ""),
                )
            )
    return slots


def _selected_skill_slots(
    plan: IntentPlan,
    registry: Any,
) -> list[_ResolverSlot]:
    slots: list[_ResolverSlot] = []
    for selection in plan.selected_skills:
        entry = registry.resolve_ref(
            selection.skill,
            getattr(selection, "skill_source", ""),
        )
        if entry is None:
            continue
        params = plan.provider_inputs.get(selection.skill, {})
        capability = (
            str(selection.matched_capabilities[0])
            if selection.matched_capabilities
            else ""
        )
        binding = {
            "consumer_kind": "plan_selection",
            "id": selection.skill,
            "capability": capability,
        }
        for field_name, field_schema in _resolver_fields(entry):
            if not _has_value(params.get(field_name)):
                continue
            field_path = (
                f"/provider_inputs/{_escape_pointer(selection.skill)}/"
                f"{_escape_pointer(field_name)}"
            )
            slots.append(
                _ResolverSlot(
                    provider_binding_id=provider_binding_id(binding, entry),
                    provider_name=str(getattr(entry, "name", "") or ""),
                    provider_source=str(getattr(entry, "source", "") or ""),
                    contract_hash=provider_contract_hash(entry),
                    field_path=field_path,
                    field_name=field_name,
                    value=params[field_name],
                    resolver=field_resolver(field_schema),
                    field_schema=field_schema,
                    capability_instance=capability,
                )
            )
    return slots


def _resolver_fields(entry: Any) -> list[tuple[str, dict[str, Any]]]:
    schema = getattr(entry, "input_schema", None)
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return []
    return [
        (str(name), raw_schema)
        for name, raw_schema in properties.items()
        if isinstance(raw_schema, dict)
        and field_binding_owner(raw_schema) == "resolver"
        and field_resolver(raw_schema)
    ]


def _local_verification_mode(
    slot: _ResolverSlot,
    *,
    user_message: str,
    required_mode: str,
) -> str:
    resolution = resolve_field(
        slot.resolver,
        {"identifier": slot.value, "input": slot.value},
    )
    if not resolution.resolved:
        return ""
    if required_mode == "local_exists":
        return "local_exists"
    if required_mode == "user_exact" and resolver_value_matches_user(
        slot.resolver,
        value=slot.value,
        user_message=user_message,
    ):
        return "user_exact"
    if required_mode == "syntactic":
        return "syntactic"
    return ""


def _evidence_matches_slot(
    evidence: ResolverEvidence,
    slot: _ResolverSlot,
    *,
    required_mode: str,
) -> bool:
    return (
        evidence.verified
        and evidence.slot_id == slot.slot_id
        and evidence.provider_binding_id == slot.provider_binding_id
        and evidence.provider_name == slot.provider_name
        and evidence.provider_source == slot.provider_source
        and evidence.contract_hash == slot.contract_hash
        and evidence.field_path == slot.field_path
        and evidence.field_name == slot.field_name
        and evidence.resolver == slot.resolver
        and _values_equal(evidence.value, slot.value)
        and verification_satisfies(
            required_mode,
            evidence.verification_mode,
        )
    )


def _evidence_for_slot(
    slot: _ResolverSlot,
    *,
    required_mode: str,
    verification_mode: str,
    source: str,
    verified: bool,
) -> ResolverEvidence:
    evidence_id = _stable_evidence_id(
        slot_id=slot.slot_id,
        value=slot.value,
        contract_hash=slot.contract_hash,
        verification_mode=verification_mode,
        source=source,
    )
    return ResolverEvidence(
        evidence_id=evidence_id,
        slot_id=slot.slot_id,
        provider_binding_id=slot.provider_binding_id,
        provider_name=slot.provider_name,
        provider_source=slot.provider_source,
        contract_hash=slot.contract_hash,
        field_path=slot.field_path,
        field_name=slot.field_name,
        value=slot.value,
        resolver=slot.resolver,
        required_mode=required_mode,
        verification_mode=verification_mode,
        source=source,
        verified=verified,
        step_id=slot.step_id,
        capability_instance=slot.capability_instance,
    )


def _coerce_evidence(values: Any) -> list[ResolverEvidence]:
    evidence: list[ResolverEvidence] = []
    for value in values or []:
        if isinstance(value, ResolverEvidence):
            evidence.append(value)
        elif isinstance(value, dict):
            try:
                evidence.append(ResolverEvidence.from_dict(value))
            except ValueError:
                continue
    return evidence


def _stable_slot_id(
    *,
    provider_binding_id: str,
    field_path: str,
    resolver: str,
) -> str:
    return "resolver-slot-" + _canonical_hash(
        {
            "provider_binding_id": provider_binding_id,
            "field_path": field_path,
            "resolver": resolver,
        }
    )[:20]


def _stable_evidence_id(
    *,
    slot_id: str,
    value: Any,
    contract_hash: str,
    verification_mode: str,
    source: str,
) -> str:
    return "resolver-evidence-" + _canonical_hash(
        {
            "slot_id": slot_id,
            "value": value,
            "contract_hash": contract_hash,
            "verification_mode": verification_mode,
            "source": source,
        }
    )[:20]


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="backslashreplace")
    return hashlib.sha256(encoded).hexdigest()


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return _normal(left).casefold() == _normal(right).casefold()
    return _canonical_hash(left) == _canonical_hash(right)


def _normal(value: str) -> str:
    return unicodedata.normalize("NFC", value or "")


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


__all__ = [
    "ResolverEvidence",
    "materialize_resolver_evidence",
    "provider_binding_id",
    "provider_contract_hash",
    "required_resolver_verification_mode",
    "resolver_value_matches_user",
    "seal_resolver_evidence",
    "validate_resolver_evidence",
    "value_is_canonical_identifier",
    "verification_satisfies",
]
