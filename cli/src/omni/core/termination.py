"""Canonical execution-outcome classification shared by every run entry point.

This module owns the whole terminal vocabulary — the reason codes, whether a
code is a bounded stop or a failure, its user-safe label, and the action that
lifts it. Keeping those together is what makes a terminal outcome renderable
the same way on every surface; when the label lived apart from the
classification, each renderer picked its own subset of reasons to speak about
and the rest ended a turn in silence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

OutcomeStatus = Literal["succeeded", "degraded", "failed", "cancelled", "interrupted"]

# The response ran into the output-token cap partway through the answer. Named
# here rather than at the provider because it decides how a *turn* settles, and
# every surface that reports a turn already reads this vocabulary.
OUTPUT_CAP_TRUNCATED = "output_cap_truncated"

# Appended to the text itself, not only to the reason code: the answer travels
# further than its metadata — into a notebook, an artifact, a pasted excerpt —
# and a sentence that stops mid-word reads as a mistake unless it says why.
_TRUNCATED_OUTPUT_NOTICE = (
    "[Incomplete: this answer stopped at the model's output-token limit and is "
    "cut off above. Ask for the remainder, or request the answer in sections.]"
)

_BOUNDED_REASONS = frozenset(
    {
        "max_tool_calls",
        "max_iterations",
        "no_progress",
        "max_total_tokens",
        "max_cost",
        # Wall-clock stops are *bounded* outcomes, not failures: the loop hits a
        # time layer (overall ceiling or the progress watchdog), forces a final
        # synthesis over the results it already has, and settles ``degraded`` —
        # never ``failed``. A genuine "the model never responded" hard error
        # stays ``llm_timeout`` (classified separately) and is not listed here.
        "timeout",
        "stalled",
        # The model kept emitting tool calls the transport could not use. It
        # tried to act and we still force a best-effort answer, so this is a
        # degraded delivery — not a hard failure, and not a clean finish.
        "malformed_tool_calls",
        # The answer was real work that our ceiling cut short. Delivering it is
        # right — the prose above the cut still answers the question — but
        # settling it ``succeeded`` would tell the user a fragment is the whole
        # reply, which is the one outcome nothing downstream can recover from.
        OUTPUT_CAP_TRUNCATED,
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


TERMINATION_LABELS = {
    "cancelled": "execution cancelled by the user",
    "interrupted": "execution interrupted; the owning process exited",
    "max_iterations": "iteration limit reached",
    "max_tool_calls": "tool budget reached",
    "max_total_tokens": "token budget reached",
    "max_cost": "cost budget reached",
    "timeout": "execution reached its overall time budget",
    "stalled": "execution stalled (no model activity within the idle window)",
    "no_progress": "tool calls made no further progress",
    "malformed_tool_calls": "the model emitted tool calls with no function name",
    OUTPUT_CAP_TRUNCATED: "the answer reached the model's output-token limit and is incomplete",
    "llm_error": "model call failed",
    "llm_transcript_invalid": "model service rejected the tool transcript",
    "llm_auth_error": "model authentication failed",
    "llm_configuration_error": "model configuration is unavailable",
    "llm_rate_limited": "model service rate limited the request",
    "llm_unavailable": "model service is temporarily unavailable",
    "llm_invalid_request": "model service rejected the request",
    "llm_timeout": "model call timed out",
    "artifact_contract_failed": "artifact rendering or validation failed",
    "artifact_revision_failed": "artifact revision failed",
}

# Budgets an operator can widen, and the single action that lifts each one.
# A bounded stop reported without this reads as "try again", which under the
# same ceiling would only reproduce the same stop.
BUDGET_EXHAUSTED_REASONS = frozenset(
    {"max_iterations", "max_tool_calls", "max_total_tokens", "max_cost"}
)
_NEXT_ACTIONS = {
    "max_iterations": "re-run with a larger max_iterations budget",
    "max_tool_calls": "re-run with a larger max_tool_calls budget",
    "max_total_tokens": "re-run with a larger token budget",
    "max_cost": "re-run with a larger cost budget",
    "timeout": "re-run with a longer max_seconds budget",
    "stalled": "re-run with a longer stall/idle timeout, or narrow the request",
    "no_progress": "narrow the request or supply the input the tools could not find",
    "malformed_tool_calls": (
        "switch to a model with reliable tool-calling support, or re-run without tools"
    ),
    OUTPUT_CAP_TRUNCATED: (
        "ask for the remainder, or request the answer in sections so no single "
        "response has to carry all of it"
    ),
}


def termination_reason_label(reason: str) -> str:
    """Return one channel-neutral, user-safe label for a terminal reason."""
    canonical = base_termination_reason(reason)
    return TERMINATION_LABELS.get(canonical, canonical or "unknown")


def termination_next_action(reason: str) -> str:
    """Return the action that lifts this stop, or ``""`` when there is none."""
    return _NEXT_ACTIONS.get(base_termination_reason(reason), "")


def mark_truncated_output(text: str) -> str:
    """Append the incompleteness notice to an answer the output cap cut short.

    Idempotent, because an answer can pass through more than one layer that
    knows it was truncated and a doubled notice reads as a bug of its own.
    """
    body = (text or "").rstrip()
    if _TRUNCATED_OUTPUT_NOTICE in body:
        return body
    return f"{body}\n\n{_TRUNCATED_OUTPUT_NOTICE}" if body else _TRUNCATED_OUTPUT_NOTICE


@dataclass(frozen=True, slots=True)
class TerminalOutcome:
    """How one execution ended, in the form every surface can render.

    Built once at the boundary that observes the stop, then carried outward, so
    a status, its cause, and the way to act on it cannot drift apart between
    the loop, the task record, and the terminal line the user reads.
    """

    status: OutcomeStatus
    reason: str
    label: str
    next_action: str = ""
    provider: str = ""
    iterations: int = 0
    tool_calls: int = 0
    detail: str = ""
    artifacts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_bounded(self) -> bool:
        return is_bounded_termination(self.reason)

    def summary(self) -> str:
        """One line naming what happened and, when it exists, what to do."""
        head = f"{self.provider} " if self.provider else ""
        body = f"{head}{self.status}: {self.label}".strip()
        return f"{body} — {self.next_action}" if self.next_action else body


def terminal_outcome(
    *,
    kind: str,
    reason: str,
    provider: str = "",
    iterations: int = 0,
    tool_calls: int = 0,
    detail: str = "",
    artifacts: tuple[str, ...] = (),
) -> TerminalOutcome:
    """Classify one execution's stop into the shared terminal record."""
    canonical = base_termination_reason(reason)
    return TerminalOutcome(
        status=execution_outcome_status(kind, reason),
        reason=canonical,
        label=termination_reason_label(reason),
        next_action=termination_next_action(reason),
        provider=provider,
        iterations=max(0, int(iterations or 0)),
        tool_calls=max(0, int(tool_calls or 0)),
        detail=detail,
        artifacts=tuple(artifacts),
    )


__all__ = [
    "BUDGET_EXHAUSTED_REASONS",
    "OUTPUT_CAP_TRUNCATED",
    "TERMINATION_LABELS",
    "OutcomeStatus",
    "TerminalOutcome",
    "aggregate_outcome_status",
    "base_termination_reason",
    "execution_outcome_status",
    "is_bounded_termination",
    "mark_truncated_output",
    "terminal_outcome",
    "termination_next_action",
    "termination_reason_label",
]
