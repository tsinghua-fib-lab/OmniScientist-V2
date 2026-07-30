"""Pareto control for visual composition and physical poster delivery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

Relation = Literal["dominates", "dominated", "equal", "incomparable"]

_SCORE_KEYS = (
    "hierarchy",
    "information_structure",
    "figure_readability",
    "space_use",
    "poster_character",
)


@dataclass(frozen=True)
class CandidateEvidence:
    """Evidence bound to one exact rendered HTML candidate."""

    html_sha256: str
    inspection: Mapping[str, Any]
    review_receipt: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ControllerState:
    """Independent incumbents for composition quality and safe delivery."""

    composition_anchor: CandidateEvidence | None
    delivery_candidate: CandidateEvidence | None


def observe(
    state: ControllerState,
    candidate: CandidateEvidence,
) -> ControllerState:
    """Update only the role that a candidate demonstrably improves."""

    anchor = state.composition_anchor
    if candidate.review_receipt is not None and (
        anchor is None or composition_relation(anchor, candidate) == "dominates"
    ):
        anchor = candidate

    delivery = state.delivery_candidate
    if _inspection_rank(candidate.inspection) > 0 and (
        delivery is None or delivery_relation(delivery, candidate) == "dominates"
    ):
        delivery = candidate

    return replace(
        state,
        composition_anchor=anchor,
        delivery_candidate=delivery,
    )


def composition_relation(
    incumbent: CandidateEvidence,
    candidate: CandidateEvidence,
) -> Relation:
    """Compare VLM judgments without collapsing rubric dimensions to one score."""

    incumbent_vector = _composition_vector(incumbent.review_receipt)
    candidate_vector = _composition_vector(candidate.review_receipt)
    if incumbent_vector is None or candidate_vector is None:
        return "incomparable"
    return _pareto_relation(incumbent_vector, candidate_vector)


def delivery_relation(
    incumbent: CandidateEvidence,
    candidate: CandidateEvidence,
) -> Relation:
    """Compare deterministic delivery facts without making aesthetic judgments."""

    return _pareto_relation(
        _delivery_vector(incumbent.inspection),
        _delivery_vector(candidate.inspection),
    )


def ready_for_delivery(candidate: CandidateEvidence) -> bool:
    """Return whether the exact candidate passes both independent gates."""

    receipt = candidate.review_receipt
    return (
        _inspection_rank(candidate.inspection) == 2
        and isinstance(receipt, Mapping)
        and str(receipt.get("verdict") or "") == "pass"
    )


def _composition_vector(receipt: Mapping[str, Any] | None) -> tuple[float, ...] | None:
    if not isinstance(receipt, Mapping):
        return None
    scores = receipt.get("scores")
    if not isinstance(scores, Mapping):
        return None
    normalized_scores: list[float] = []
    for key in _SCORE_KEYS:
        value = scores.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        normalized_scores.append(float(value))
    issues = receipt.get("critical_issues")
    issue_count = len(issues) if isinstance(issues, list) else 0
    directives = receipt.get("global_directives")
    directive_count = len(directives) if isinstance(directives, list) else 0
    verdict = 1.0 if str(receipt.get("verdict") or "") == "pass" else 0.0
    return (
        verdict,
        *normalized_scores,
        -float(issue_count),
        -float(directive_count),
    )


def _delivery_vector(inspection: Mapping[str, Any]) -> tuple[float, ...]:
    warnings = inspection.get("warnings")
    warning_items = warnings if isinstance(warnings, list) else []
    error_count = sum(
        1
        for item in warning_items
        if isinstance(item, Mapping) and item.get("severity") == "error"
    )
    return (
        float(_inspection_rank(inspection)),
        -float(error_count),
        -_normalized_overflow(inspection),
    )


def _inspection_rank(inspection: Mapping[str, Any]) -> int:
    outcome = inspection.get("outcome")
    code = str(outcome.get("code") or "") if isinstance(outcome, Mapping) else ""
    return {"inspection_complete": 2, "inspection_blocked": 1}.get(code, 0)


def _normalized_overflow(inspection: Mapping[str, Any]) -> float:
    report = inspection.get("report")
    poster = report.get("poster") if isinstance(report, Mapping) else None
    modules = report.get("modules") if isinstance(report, Mapping) else None
    if not isinstance(poster, Mapping) or not isinstance(modules, list):
        return float("inf")
    width = _number(poster.get("width"))
    height = _number(poster.get("height"))
    if width is None or height is None:
        return float("inf")
    overflow = 0.0
    for module in modules:
        rect = module.get("rect") if isinstance(module, Mapping) else None
        if not isinstance(rect, Mapping):
            continue
        left = _number(rect.get("left"), allow_zero=True)
        top = _number(rect.get("top"), allow_zero=True)
        item_width = _number(rect.get("width"))
        item_height = _number(rect.get("height"))
        if left is not None and item_width is not None:
            overflow = max(overflow, (left + item_width - width) / width)
        if top is not None and item_height is not None:
            overflow = max(overflow, (top + item_height - height) / height)
    return max(0.0, overflow)


def _number(value: Any, *, allow_zero: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= (0.0 if allow_zero else 1e-12) else None


def _pareto_relation(
    incumbent: tuple[float, ...],
    candidate: tuple[float, ...],
) -> Relation:
    if len(incumbent) != len(candidate):
        return "incomparable"
    no_worse = all(new >= old for old, new in zip(incumbent, candidate, strict=True))
    improves = any(new > old for old, new in zip(incumbent, candidate, strict=True))
    if no_worse and improves:
        return "dominates"
    not_better = all(old >= new for old, new in zip(incumbent, candidate, strict=True))
    regresses = any(old > new for old, new in zip(incumbent, candidate, strict=True))
    if not_better and regresses:
        return "dominated"
    if incumbent == candidate:
        return "equal"
    return "incomparable"


__all__ = [
    "CandidateEvidence",
    "ControllerState",
    "Relation",
    "composition_relation",
    "delivery_relation",
    "observe",
    "ready_for_delivery",
]
