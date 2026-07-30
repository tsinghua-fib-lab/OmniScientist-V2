"""Deterministic, truthful rendering of a scheduling outcome.

The message is derived from the structured :class:`ScheduleCreateResult`, not
from model prose, so a scheduling turn can never claim success it did not
achieve. Readiness is stated honestly across its independent axes: a schedule
only fires when it is registered **and** scheduling is enabled **and** a runner
is up, so the message never promises "it will fire" when ``omni serve`` / the
home service is down.
"""

from __future__ import annotations

from omni.scheduling.contracts import (
    STATUS_AWAITING_APPROVAL,
    STATUS_CREATED,
    STATUS_ERROR,
    STATUS_NEEDS_INPUT,
    STATUS_REJECTED,
    ScheduleCreateResult,
)


def _runner_clause(result: ScheduleCreateResult) -> str:
    if not result.scheduling_enabled:
        return (
            " Scheduling is currently disabled (schedules.enabled=false), so it will not "
            "fire until re-enabled."
        )
    if result.runner_ready is False:
        return (
            " No runner is active: start the background service (`omni serve`) so it fires "
            "unattended, or run `omni schedule run` to fire due jobs now."
        )
    if result.runner_ready is None:
        return " It fires from the background service; start `omni serve` if it is not already running."
    return ""


def _autonomy_clause(result: ScheduleCreateResult) -> str:
    if result.approved_tools:
        return f" When it runs it may use these tools unattended: {', '.join(result.approved_tools)}."
    return (
        " Unattended autonomy is off (schedules.autonomy=off), so it cannot use sensitive "
        "tools (write_file/run_compute/…) and will produce no files until enabled."
    )


def build_summary(result: ScheduleCreateResult) -> str:
    """One deterministic sentence describing exactly what happened."""
    if result.status == STATUS_CREATED:
        where = f", delivered to the {result.channel} inbox" if result.channel and result.channel != "cli" else ""
        head = (
            f"Scheduled '{result.title}' ({result.spec}); next run {result.next_run_local}{where}."
        )
        view = (
            f" View or manage it with `omni schedule show {result.schedule_id[:8]}`."
            if result.schedule_id
            else ""
        )
        return head + _runner_clause(result) + _autonomy_clause(result) + view
    if result.status == STATUS_AWAITING_APPROVAL:
        return (
            f"This request needs the machine owner's approval before it is scheduled. "
            f"Nothing runs yet. Approve it locally with `{result.approve_command}` "
            f"(or deny with `omni schedule deny {result.proposal_id[:8]}`)."
        )
    if result.status == STATUS_NEEDS_INPUT:
        base = result.error or result.reason or "More detail is needed before this can be scheduled."
        if result.recovery_choices:
            options = "; ".join(choice.get("label", "") for choice in result.recovery_choices if choice.get("label"))
            if options:
                return f"{base} Options: {options}."
        return base
    if result.status == STATUS_REJECTED:
        return result.summary or result.error or result.reason or "The schedule request was rejected."
    if result.status == STATUS_ERROR:
        return result.error or "The schedule request could not be processed."
    return result.error or result.reason or ""


__all__ = ["build_summary"]
