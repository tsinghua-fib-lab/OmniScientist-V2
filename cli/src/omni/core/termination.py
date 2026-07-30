"""Canonical execution-outcome classification shared by every run entry point."""

from __future__ import annotations

from typing import Literal, cast

OutcomeStatus = Literal["succeeded", "degraded", "failed", "cancelled", "interrupted"]

_BOUNDED_REASONS = frozenset(
    {
        "max_tool_calls",
        "max_iterations",
        "no_progress",
        "max_total_tokens",
        "max_cost",
    }
)
_STATUS_RANK: dict[OutcomeStatus, int] = {
    "succeeded": 0,
    "degraded": 1,
    "failed": 2,
    "cancelled": 3,
    "interrupted": 4,
}


def base_termination_reason(reason: str) -> str:
    """Strip presentation/finalization prefixes without changing the cause."""
    value = str(reason or "").strip()
    while value.startswith("synthesized_"):
        value = value.removeprefix("synthesized_")
    return value


def is_bounded_termination(reason: str) -> bool:
    return base_termination_reason(reason) in _BOUNDED_REASONS


def execution_outcome_status(kind: str, reason: str) -> OutcomeStatus:
    """Classify delivery shape and stop cause independently from answer text."""
    stop_reason = base_termination_reason(reason)
    if stop_reason == "cancelled":
        return "cancelled"
    if stop_reason == "interrupted":
        return "interrupted"
    if str(kind or "").lower() == "error":
        return "failed"
    if str(kind or "").lower() == "partial" or is_bounded_termination(reason):
        return "degraded"
    return "succeeded"


def aggregate_outcome_status(*statuses: str) -> OutcomeStatus:
    """Return the strongest terminal outcome: failed > degraded > succeeded."""
    strongest: OutcomeStatus = "succeeded"
    aliases = {"passed": "succeeded", "partial": "degraded", "error": "failed"}
    for raw in statuses:
        value = aliases.get(str(raw or "").lower(), str(raw or "").lower())
        if value not in _STATUS_RANK:
            continue
        status = cast(OutcomeStatus, value)
        if _STATUS_RANK[status] > _STATUS_RANK[strongest]:
            strongest = status
    return strongest


__all__ = [
    "OutcomeStatus",
    "aggregate_outcome_status",
    "base_termination_reason",
    "execution_outcome_status",
    "is_bounded_termination",
]
