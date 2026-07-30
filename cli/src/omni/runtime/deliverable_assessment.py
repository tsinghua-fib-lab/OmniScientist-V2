"""Typed provider-owned assessments for completed deliverables.

The host does not decide whether a scientific figure, draft, or another
domain-specific output is semantically correct.  The provider that produced
the output records that judgement, its effective inputs, and evidence under a
small common envelope.  The verifier only validates and aggregates that
envelope against the task contract.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

ASSESSMENT_SCHEMA = "omni.deliverable-assessment/v1"
_VALID_STATUSES = frozenset({"passed", "degraded", "failed", "unknown"})
_STATUS_RANK = {"passed": 0, "degraded": 1, "unknown": 2, "failed": 3}
_MAX_PROMPT_ASSESSMENT_JSON_CHARS = 200_000
_JSON_FENCE_RE = re.compile(
    r"```(?:json)?[ \t]*\r?\n(?P<body>.*?)```",
    flags=re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class AssessmentCriterion:
    """One provider-owned quality judgement."""

    criterion_id: str
    status: str
    summary: str = ""
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeliverableAssessment:
    """Validated assessment envelope emitted by one concrete provider."""

    deliverable_id: str
    provider_binding_id: str
    provider: str
    contract_hash: str
    step_id: str
    feedback: str
    status: str
    effective_inputs: dict[str, Any]
    criteria: tuple[AssessmentCriterion, ...]
    evidence_refs: tuple[str, ...] = ()
    summary: str = ""
    retryable: bool = False
    schema: str = ASSESSMENT_SCHEMA


@dataclass(frozen=True, slots=True)
class DeliverableAssessmentOutcome:
    """Task-level aggregation consumed by the runtime verifier."""

    failures: tuple[str, ...]
    degraded: tuple[str, ...]
    details: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class QualityRetryDecision:
    """Admission decision for one bounded post-execution quality retry."""

    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class _AssessmentRequirement:
    deliverable_id: str
    provider_binding_id: str
    contract_hash: str
    step_id: str
    checks: tuple[str, ...]


def make_provider_binding_id(
    *,
    provider_type: str,
    provider_name: str,
    deliverable_id: str,
) -> str:
    """Return a stable, human-auditable binding identity.

    This identifies the logical provider binding, not an individual execution
    attempt. Provider authority fingerprints remain the stronger integrity
    proof and may be carried separately in evidence/provenance.
    """

    kind = str(provider_type or "provider").strip() or "provider"
    name = str(provider_name or "unknown").strip() or "unknown"
    deliverable = str(deliverable_id or "deliverable").strip() or "deliverable"
    return f"{kind}:{name}:{deliverable}"


def parse_deliverable_assessment(payload: Any) -> DeliverableAssessment | None:
    """Parse an assessment fail-closed; malformed envelopes return ``None``."""

    if not isinstance(payload, Mapping):
        return None
    if str(payload.get("schema") or "") != ASSESSMENT_SCHEMA:
        return None
    deliverable_id = str(payload.get("deliverable_id") or "").strip()
    provider_binding_id = str(payload.get("provider_binding_id") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    contract_hash = str(payload.get("contract_hash") or "").strip()
    step_id = str(payload.get("step_id") or "").strip()
    feedback = str(payload.get("feedback") or "").strip()
    declared_status = _normalise_status(payload.get("status"))
    if not all(
        (
            deliverable_id,
            provider_binding_id,
            provider,
            contract_hash,
            step_id,
            feedback,
            declared_status,
        )
    ):
        return None
    if "retryable" not in payload or not isinstance(payload.get("retryable"), bool):
        return None

    criteria_raw = payload.get("criteria")
    if not isinstance(criteria_raw, list) or not criteria_raw:
        return None
    criteria: list[AssessmentCriterion] = []
    for raw in criteria_raw:
        criterion = _parse_criterion(raw)
        if criterion is None:
            return None
        criteria.append(criterion)

    effective_inputs_raw = payload.get("effective_inputs")
    if not isinstance(effective_inputs_raw, Mapping):
        return None
    criteria_status = _worst_status(item.status for item in criteria)
    status = _worst_status((declared_status, criteria_status))
    return DeliverableAssessment(
        deliverable_id=deliverable_id,
        provider_binding_id=provider_binding_id,
        provider=provider,
        contract_hash=contract_hash,
        step_id=step_id,
        feedback=feedback,
        status=status,
        effective_inputs=dict(effective_inputs_raw),
        criteria=tuple(criteria),
        evidence_refs=_string_tuple(payload.get("evidence_refs")),
        summary=str(payload.get("summary") or ""),
        retryable=payload.get("retryable") is True,
    )


def collect_deliverable_assessments(
    results: Iterable[Any],
) -> list[DeliverableAssessment]:
    """Collect only explicit top-level assessment envelopes from results."""

    found: list[DeliverableAssessment] = []
    seen: set[tuple[Any, ...]] = set()
    for result in results:
        if not isinstance(result, Mapping):
            continue
        candidates: list[Any] = []
        if "deliverable_assessment" in result:
            candidates.append(result.get("deliverable_assessment"))
        plural = result.get("deliverable_assessments")
        if isinstance(plural, list):
            candidates.extend(plural)
        for candidate in candidates:
            assessment = parse_deliverable_assessment(candidate)
            if assessment is None:
                continue
            fingerprint = (
                assessment.deliverable_id,
                assessment.provider_binding_id,
                assessment.contract_hash,
                assessment.step_id,
                assessment.provider,
                assessment.status,
                tuple(
                    (criterion.criterion_id, criterion.status)
                    for criterion in assessment.criteria
                ),
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            found.append(assessment)
    return found


def apply_prompt_assessment_transport(
    result: dict[str, Any],
    *,
    final_text: str,
    quality_contract: Mapping[str, Any] | None,
    provider: str,
    fallback_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Lift an explicitly emitted prompt-provider assessment into ``result``.

    Prompt-only providers return prose, so their structured assessment travels
    either as the whole JSON response or in a JSON code fence.  This function
    only transports a complete v1 envelope; it never derives a judgement from
    the prose or lifecycle status.

    A missing envelope stays missing (and therefore fails closed downstream)
    unless the provider contract explicitly declares
    ``missing_assessment_status: unknown``.  That opt-in permits the host to
    record transport unavailability as ``unknown`` for the provider-declared
    check ids.  It still never manufactures ``passed`` or evidence.
    """

    existing = result.get("deliverable_assessment")
    if parse_deliverable_assessment(existing) is not None:
        return result

    emitted = _extract_prompt_assessment(final_text)
    if emitted is not None:
        result["deliverable_assessment"] = emitted
        return result

    contract = quality_contract if isinstance(quality_contract, Mapping) else {}
    if (
        contract.get("assessment_required") is not True
        or str(contract.get("assessment_schema") or "") != ASSESSMENT_SCHEMA
        or str(contract.get("missing_assessment_status") or "").strip().lower()
        != "unknown"
    ):
        return result

    checks = _unique_strings(
        contract.get("checks") if isinstance(contract.get("checks"), list) else []
    )
    identity = {
        key: str(fallback_identity.get(key) or "").strip()
        for key in (
            "deliverable_id",
            "provider_binding_id",
            "contract_hash",
            "step_id",
        )
    }
    provider_name = str(provider or "").strip()
    if not checks or not provider_name or not all(identity.values()):
        return result

    feedback = (
        "The prompt provider did not emit a parseable "
        f"{ASSESSMENT_SCHEMA} envelope. The host recorded unknown, as explicitly "
        "permitted by the provider quality contract; no quality judgement or "
        "evidence was inferred."
    )
    result["deliverable_assessment"] = {
        "schema": ASSESSMENT_SCHEMA,
        **identity,
        "provider": provider_name,
        "feedback": feedback,
        "status": "unknown",
        "retryable": False,
        "effective_inputs": {},
        "criteria": [
            {
                "criterion_id": check,
                "status": "unknown",
                "summary": "Provider assessment was not available to the host.",
                "evidence_refs": [],
            }
            for check in checks
        ],
        "evidence_refs": [],
        "summary": "Provider assessment unavailable.",
        "assessment_origin": "host_missing_provider_assessment",
    }
    return result


def _extract_prompt_assessment(text: str) -> dict[str, Any] | None:
    """Return the first complete, explicitly delimited v1 assessment."""

    raw_text = str(text or "")
    candidates = [raw_text.strip()]
    candidates.extend(match.group("body").strip() for match in _JSON_FENCE_RE.finditer(raw_text))
    for candidate in candidates:
        if not candidate or len(candidate) > _MAX_PROMPT_ASSESSMENT_JSON_CHARS:
            continue
        try:
            document = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(document, Mapping):
            continue
        raw_assessment: Any
        if str(document.get("schema") or "") == ASSESSMENT_SCHEMA:
            raw_assessment = document
        else:
            raw_assessment = document.get("deliverable_assessment")
        if parse_deliverable_assessment(raw_assessment) is not None:
            return dict(raw_assessment)
    return None


def bind_deliverable_assessment_identity(
    result: dict[str, Any],
    step: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind provider judgements to the host-sealed execution identity.

    Providers own the criterion status, evidence, and feedback. The host owns
    which exact provider contract and workflow consumer produced the result, so
    those identity fields are overwritten at the execution boundary instead of
    trusting provider-supplied fallbacks.
    """
    binding_id = str(step.get("provider_binding_id") or "").strip()
    contract_hash = str(step.get("provider_contract_hash") or "").strip()
    step_id = str(step.get("id") or "").strip()
    if not binding_id or not contract_hash or not step_id:
        return result
    deliverable_id = str(step.get("deliverable_id") or step_id).strip()
    provider = str(
        step.get("provider_name")
        or step.get("skill_name")
        or step.get("skill")
        or "synthesis.final"
    ).strip()

    def bind(raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        if str(raw.get("schema") or "") != ASSESSMENT_SCHEMA:
            return
        raw["deliverable_id"] = deliverable_id
        raw["provider_binding_id"] = binding_id
        raw["contract_hash"] = contract_hash
        raw["step_id"] = step_id
        raw["provider"] = provider

    bind(result.get("deliverable_assessment"))
    plural = result.get("deliverable_assessments")
    if isinstance(plural, list):
        for item in plural:
            bind(item)
    return result


def evaluate_deliverable_assessments(
    checks: Iterable[str],
    assessments: Iterable[DeliverableAssessment | None],
    *,
    task_contract: Mapping[str, Any] | None,
) -> DeliverableAssessmentOutcome:
    """Aggregate provider assessments without embedding domain-specific rules.

    Structured task contracts can identify an exact ``deliverable_id`` and
    ``provider_binding_id`` for a check. Older contracts still fail closed when
    no provider emitted an assessment carrying the requested criterion.
    """

    requested = _unique_strings(checks)
    valid = [assessment for assessment in assessments if assessment is not None]
    contract = task_contract or {}
    exact_contract = _uses_exact_assessment_obligations(contract)
    requirements = _assessment_requirements(
        contract,
        require_exact=exact_contract,
    )
    failures: list[str] = []
    degraded: list[str] = []
    details: list[dict[str, Any]] = []

    for check in requested:
        expected = [requirement for requirement in requirements if check in requirement.checks]
        if expected:
            for requirement in expected:
                matches = [
                    assessment
                    for assessment in valid
                    if assessment.deliverable_id == requirement.deliverable_id
                    and (
                        not requirement.provider_binding_id
                        or assessment.provider_binding_id
                        == requirement.provider_binding_id
                    )
                    and (
                        not requirement.contract_hash
                        or assessment.contract_hash == requirement.contract_hash
                    )
                    and (
                        not requirement.step_id
                        or assessment.step_id == requirement.step_id
                    )
                ]
                _record_check_outcome(
                    check,
                    matches,
                    failures=failures,
                    degraded=degraded,
                    details=details,
                    deliverable_id=requirement.deliverable_id,
                    provider_binding_id=requirement.provider_binding_id,
                    contract_hash=requirement.contract_hash,
                    step_id=requirement.step_id,
                )
            continue

        if exact_contract:
            failures.append(check)
            details.append(
                {
                    "check": check,
                    "status": "failed",
                    "reason": "missing_exact_obligation",
                    "deliverable_id": "",
                    "provider_binding_id": "",
                    "contract_hash": "",
                    "step_id": "",
                }
            )
            continue

        matches = [
            assessment
            for assessment in valid
            if _criterion_status(assessment, check)
        ]
        _record_check_outcome(
            check,
            matches,
            failures=failures,
            degraded=degraded,
            details=details,
        )

    return DeliverableAssessmentOutcome(
        failures=tuple(_unique_strings(failures)),
        degraded=tuple(_unique_strings(degraded)),
        details=tuple(details),
    )


def quality_retry_decision(
    assessment: DeliverableAssessment,
    *,
    provider_replay_safe: bool,
    prior_quality_retries: int,
    committed_side_effects: Iterable[str] = (),
    idempotency_required: bool = False,
    idempotency_key: str = "",
) -> QualityRetryDecision:
    """Admit at most one provider-authorised, side-effect-safe quality retry.

    The helper is intentionally pure. The workflow lifecycle remains the sole
    owner of durable retry attempts, descendant invalidation, and event writes.
    """

    if assessment.status == "passed":
        return QualityRetryDecision(False, "quality_already_satisfied")
    if not assessment.retryable:
        return QualityRetryDecision(False, "assessment_not_retryable")
    if not provider_replay_safe:
        return QualityRetryDecision(False, "provider_not_replay_safe")
    if max(0, int(prior_quality_retries)) >= 1:
        return QualityRetryDecision(False, "quality_retry_budget_exhausted")
    if idempotency_required and not str(idempotency_key or "").strip():
        return QualityRetryDecision(False, "idempotency_key_required")
    effects = [str(item) for item in committed_side_effects if str(item)]
    if effects and not str(idempotency_key or "").strip():
        return QualityRetryDecision(False, "unprotected_side_effects")
    return QualityRetryDecision(True, "quality_retry_admitted")


def _record_check_outcome(
    check: str,
    assessments: list[DeliverableAssessment],
    *,
    failures: list[str],
    degraded: list[str],
    details: list[dict[str, Any]],
    deliverable_id: str = "",
    provider_binding_id: str = "",
    contract_hash: str = "",
    step_id: str = "",
) -> None:
    matching = [
        (assessment, _criterion_status(assessment, check))
        for assessment in assessments
    ]
    matching = [
        (assessment, status)
        for assessment, status in matching
        if status
    ]
    if not matching:
        failures.append(check)
        details.append(
            {
                "check": check,
                "status": "failed",
                "reason": "missing_assessment",
                "deliverable_id": deliverable_id,
                "provider_binding_id": provider_binding_id,
                "contract_hash": contract_hash,
                "step_id": step_id,
            }
        )
        return

    for assessment, status in matching:
        if status == "failed":
            failures.append(check)
        elif status in {"degraded", "unknown"}:
            degraded.append(check)
        details.append(
            {
                "check": check,
                "status": status,
                "reason": "provider_assessment",
                "deliverable_id": assessment.deliverable_id,
                "provider_binding_id": assessment.provider_binding_id,
                "provider": assessment.provider,
                "contract_hash": assessment.contract_hash,
                "step_id": assessment.step_id,
                "feedback": assessment.feedback,
                "summary": _criterion_summary(assessment, check),
                "evidence_refs": list(assessment.evidence_refs),
            }
        )


def _criterion_status(assessment: DeliverableAssessment, check: str) -> str:
    statuses = [
        criterion.status
        for criterion in assessment.criteria
        if criterion.criterion_id == check
    ]
    return _worst_status(statuses)


def _criterion_summary(assessment: DeliverableAssessment, check: str) -> str:
    summaries = [
        criterion.summary
        for criterion in assessment.criteria
        if criterion.criterion_id == check and criterion.summary
    ]
    return "; ".join(summaries)


def _assessment_requirements(
    task_contract: Mapping[str, Any],
    *,
    require_exact: bool = False,
) -> list[_AssessmentRequirement]:
    raw_deliverables = task_contract.get("deliverables")
    if not isinstance(raw_deliverables, list):
        return []
    requirements: list[_AssessmentRequirement] = []
    for raw in raw_deliverables:
        if not isinstance(raw, Mapping) or raw.get("required") is False:
            continue
        deliverable_id = str(
            raw.get("deliverable_id") or raw.get("id") or ""
        ).strip()
        provider_binding_id = str(raw.get("provider_binding_id") or "").strip()
        contract_hash = str(
            raw.get("provider_contract_hash")
            or raw.get("contract_hash")
            or ""
        ).strip()
        step_id = str(
            raw.get("consumer_step_id") or raw.get("step_id") or ""
        ).strip()
        checks_raw = (
            raw.get("required_checks")
            if isinstance(raw.get("required_checks"), list)
            else raw.get("checks")
            if isinstance(raw.get("checks"), list)
            else raw.get("acceptance")
            if isinstance(raw.get("acceptance"), list)
            else []
        )
        checks = _unique_strings(checks_raw)
        has_exact_identity = all(
            (
                deliverable_id,
                provider_binding_id,
                contract_hash,
                step_id,
            )
        )
        if deliverable_id and checks and (has_exact_identity or not require_exact):
            requirements.append(
                _AssessmentRequirement(
                    deliverable_id=deliverable_id,
                    provider_binding_id=provider_binding_id,
                    contract_hash=contract_hash,
                    step_id=step_id,
                    checks=tuple(checks),
                )
            )
    return requirements


def _uses_exact_assessment_obligations(
    task_contract: Mapping[str, Any],
) -> bool:
    """Return whether the task contract requires v2 exact identity matching."""

    raw_version = task_contract.get("schema_version")
    try:
        return int(raw_version) >= 2
    except (TypeError, ValueError):
        return False


def _parse_criterion(payload: Any) -> AssessmentCriterion | None:
    if not isinstance(payload, Mapping):
        return None
    criterion_id = str(
        payload.get("criterion_id") or payload.get("check_id") or ""
    ).strip()
    status = _normalise_status(payload.get("status"))
    if not criterion_id or not status:
        return None
    return AssessmentCriterion(
        criterion_id=criterion_id,
        status=status,
        summary=str(payload.get("summary") or ""),
        evidence_refs=_string_tuple(payload.get("evidence_refs")),
    )


def _normalise_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in _VALID_STATUSES else ""


def _worst_status(values: Iterable[str]) -> str:
    statuses = [status for status in values if status in _VALID_STATUSES]
    return max(statuses, key=_STATUS_RANK.__getitem__) if statuses else ""


def _string_tuple(value: Any) -> tuple[str, ...]:
    return tuple(_unique_strings(value if isinstance(value, list) else []))


def _unique_strings(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


__all__ = [
    "ASSESSMENT_SCHEMA",
    "AssessmentCriterion",
    "DeliverableAssessment",
    "DeliverableAssessmentOutcome",
    "QualityRetryDecision",
    "apply_prompt_assessment_transport",
    "collect_deliverable_assessments",
    "bind_deliverable_assessment_identity",
    "evaluate_deliverable_assessments",
    "make_provider_binding_id",
    "parse_deliverable_assessment",
    "quality_retry_decision",
]
