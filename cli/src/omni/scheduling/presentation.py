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


def _short_goal(result: ScheduleCreateResult) -> str:
    text = (result.goal or "").strip()
    if not text:
        return ""
    return text if len(text) <= 80 else text[:77] + "…"


def _work_clause(result: ScheduleCreateResult) -> str:
    """User-visible work item — same object that is stored and later fired."""
    shown = _short_goal(result)
    return f" Goal: '{shown}'." if shown else ""


def build_summary(result: ScheduleCreateResult) -> str:
    """One deterministic sentence describing exactly what happened."""
    if result.status == STATUS_CREATED:
        where = f", delivered to the {result.channel} inbox" if result.channel and result.channel != "cli" else ""
        head = (
            f"Scheduled '{result.title}' ({result.spec}); next run {result.next_run_local}{where}."
        )
        slipped = (
            " The original time had passed, so it will run immediately."
            if result.slot_elapsed
            else ""
        )
        view = (
            f" View or manage it with `omni schedule show {result.schedule_id[:8]}`."
            if result.schedule_id
            else ""
        )
        return head + _work_clause(result) + slipped + _runner_clause(result) + _autonomy_clause(result) + view
    if result.status == STATUS_AWAITING_APPROVAL:
        warn = (
            " The requested time is soon; approve promptly, or the work will run "
            "immediately if the slot has passed."
            if result.near_term
            else ""
        )
        return (
            f"This request needs the machine owner's approval before it is scheduled. "
            f"Nothing runs yet. Approve it locally with `{result.approve_command}` "
            f"(or deny with `omni schedule deny {result.proposal_id[:8]}`)."
            f"{_work_clause(result)}{warn}"
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


def build_card(result: ScheduleCreateResult, *, chat: bool = False) -> str:
    """Field-per-line markdown for a human reader.

    ``build_summary`` stays the one-line observation the model and the audit
    event see. Chat channels cannot run ``omni schedule show``, so that manage
    hint is CLI-only; the goal is the stored text, not the 80-character slice.
    """
    if result.status == STATUS_CREATED:
        spec = f" ({result.spec})" if result.spec else ""
        lines = [f"The scheduled task is confirmed{spec}."]
        _append_identity(lines, result)
        if result.slot_elapsed:
            lines.append("- **Note**: The original time had passed, so it will run immediately.")
        _append_readiness(lines, result)
        _append_autonomy(lines, result)
        _append_manage(lines, result, chat=chat)
        return "\n".join(lines)
    if result.status == STATUS_AWAITING_APPROVAL:
        lines = [
            "This request needs the machine owner's approval before it is scheduled. Nothing runs yet."
        ]
        _append_identity(lines, result)
        if result.approve_command:
            lines.append(f"- **Approve**: `{result.approve_command}`")
        if result.proposal_id and not chat:
            lines.append(f"- **Deny**: `omni schedule deny {result.proposal_id[:8]}`")
        if result.near_term:
            lines.append(
                "- **Note**: The requested time is soon; approve promptly, or the work "
                "will run immediately if the slot has passed."
            )
        return "\n".join(lines)
    if result.status == STATUS_NEEDS_INPUT:
        header = result.error or result.reason or "More detail is needed before this can be scheduled."
        lines = [header]
        _append_identity(lines, result)
        for choice in result.recovery_choices:
            label = str(choice.get("label") or "").strip()
            if label:
                lines.append(f"- {label}")
        return "\n".join(lines)
    if result.status == STATUS_REJECTED:
        return result.summary or result.error or result.reason or "The schedule request was rejected."
    if result.status == STATUS_ERROR:
        return result.error or "The schedule request could not be processed."
    return result.error or result.reason or ""


def result_from_tool_payload(payload: dict) -> ScheduleCreateResult | None:
    """Rehydrate a schedule outcome from a ``schedule_task`` tool result."""
    if not isinstance(payload, dict):
        return None
    outcome = str(payload.get("outcome") or "")
    if outcome not in {
        STATUS_CREATED,
        STATUS_AWAITING_APPROVAL,
        STATUS_NEEDS_INPUT,
        STATUS_REJECTED,
        STATUS_ERROR,
    }:
        if payload.get("schedule_id") and str(payload.get("status") or "") in {"ok", STATUS_CREATED}:
            outcome = STATUS_CREATED
        else:
            return None
    if not (
        payload.get("schedule_id")
        or payload.get("proposal_id")
        or payload.get("next_run")
        or payload.get("next_run_local")
        or str(payload.get("summary") or "").startswith("Scheduled '")
        or outcome in {STATUS_AWAITING_APPROVAL, STATUS_NEEDS_INPUT}
    ):
        return None
    tools = payload.get("approved_tools") or []
    if not isinstance(tools, list):
        tools = []
    return ScheduleCreateResult(
        status=outcome,
        schedule_id=str(payload.get("schedule_id") or ""),
        proposal_id=str(payload.get("proposal_id") or ""),
        kind=str(payload.get("kind") or ""),
        spec=str(payload.get("spec") or ""),
        title=str(payload.get("title") or ""),
        goal=str(payload.get("goal") or ""),
        next_run_local=str(payload.get("next_run") or payload.get("next_run_local") or ""),
        channel=str(payload.get("channel") or ""),
        approved_tools=[str(item) for item in tools if str(item).strip()],
        scheduling_enabled=payload.get("scheduling_enabled", True) is not False,
        runner_ready=payload.get("runner_ready"),
        reason=str(payload.get("reason") or ""),
        recovery_choices=[
            item for item in (payload.get("recovery_choices") or []) if isinstance(item, dict)
        ],
        approve_command=str(payload.get("approve_command") or ""),
        error=str(payload.get("error") or payload.get("message") or ""),
        summary=str(payload.get("summary") or ""),
        near_term=bool(payload.get("near_term")),
        slot_elapsed=bool(payload.get("slot_elapsed")),
    )


def is_summary_echo(text: str, result: ScheduleCreateResult) -> bool:
    """Whether *text* is the one-line receipt, not a separate human reply."""
    stripped = text.strip()
    if not stripped:
        return False
    summary = (result.summary or "").strip()
    if summary and stripped == summary:
        return True
    collapsed = " ".join(stripped.split())
    if collapsed.startswith("Scheduled '") and "next run" in collapsed:
        return True
    if collapsed.startswith("This request needs the machine owner's approval"):
        return True
    return (
        "When it runs it may use these tools unattended" in collapsed
        and "\n" not in stripped
    )


def _append_identity(lines: list[str], result: ScheduleCreateResult) -> None:
    title = (result.title or "").strip()
    goal = (result.goal or "").strip()
    if title and goal and title != goal:
        lines.append(f"- **Title**: {title}")
        lines.append(f"- **Task**: {goal}")
    elif goal:
        lines.append(f"- **Task**: {goal}")
    elif title:
        lines.append(f"- **Task**: {title}")
    if result.next_run_local:
        lines.append(f"- **When**: {result.next_run_local}")
    if result.schedule_id:
        lines.append(f"- **Id**: `{result.schedule_id[:8]}`")


def _append_readiness(lines: list[str], result: ScheduleCreateResult) -> None:
    note = _runner_clause(result).strip()
    if note:
        lines.append(f"- **Runner**: {note}")


def _append_autonomy(lines: list[str], result: ScheduleCreateResult) -> None:
    if result.approved_tools:
        lines.append(f"- **Tools**: {', '.join(result.approved_tools)}")
        return
    note = _autonomy_clause(result).strip()
    if note:
        lines.append(f"- **Tools**: {note}")


def _append_manage(lines: list[str], result: ScheduleCreateResult, *, chat: bool) -> None:
    if chat:
        if result.channel and result.channel != "cli":
            lines.append("- **Delivery**: Results will be sent to this conversation.")
        return
    if result.channel and result.channel != "cli":
        lines.append(f"- **Delivery**: Results will be delivered to the {result.channel} inbox.")
    if result.schedule_id:
        lines.append(f"- **Manage**: `omni schedule show {result.schedule_id[:8]}`")


__all__ = [
    "build_card",
    "build_summary",
    "is_summary_echo",
    "result_from_tool_payload",
]
