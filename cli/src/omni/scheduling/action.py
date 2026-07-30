"""``schedule.create`` — the first :class:`ActionContract` tenant.

Binds the model's *proposal* (a natural-language ``when`` plus a host-owned
goal) to canonical scheduling arguments through semantic admission:

* ``when`` (a grounded temporal IR) is resolved by
  :func:`omni.scheduling.temporal.resolve_temporal`. A unique reading yields a
  canonical trigger; a genuinely ambiguous one (classically a bare hour like
  "7:10") yields ``needs_input`` with both readings as candidates — never a
  silent 12-hour completion.
* Exact machine triggers (``cron`` / ``every_seconds`` / ``at``) keep today's
  direct path so ``omni schedule add --cron/--at`` and explicit model timestamps
  do not regress; ``ScheduleService`` still applies the past-guard and timezone
  normalisation.

Only the sealed canonical arguments reach :class:`ScheduleService` — never the
raw proposal — so approval, audit, and execution all see the same trigger.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from omni.core.action_contracts import (
    ActionContract,
    ActionDecision,
    EffectKind,
    ResolutionResult,
    ResolutionStatus,
    ResolverContext,
)
from omni.scheduling.contracts import (
    ScheduleTrigger,
    cron_trigger,
    interval_trigger,
    once_trigger,
)
from omni.scheduling.temporal import POLICY_VERSION, resolve_temporal

SCHEDULE_CREATE = "schedule.create"
CONTRACT_VERSION = "v1"

# The model-facing IR for a natural-language time. Kept small and bounded (never
# free-text parsing): the model extracts grounded constraints, the resolver does
# the deterministic math + ambiguity/validity.
_WHEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "A grounded natural-language time. Use this (NOT 'at') whenever the user "
        "expresses the time in words, e.g. 'today 7:10', 'every day at 6pm'. Do "
        "NOT pre-resolve an ambiguous hour into a 24-hour value; leave 'day_period' "
        "null and let the system confirm AM/PM with the user."
    ),
    "properties": {
        "raw_expression": {
            "type": "string",
            "description": "The exact time wording copied verbatim from the user's message.",
        },
        "trigger_kind": {
            "type": "string",
            "enum": ["once", "interval", "cron", "recurring"],
            "description": "once = one-time; recurring = daily/weekly; interval = every N seconds.",
        },
        "constraints": {
            "type": "object",
            "description": (
                "Grounded parts. clock={surface_hour,minute,second?,day_period(am/pm/null),"
                "hour_system(12/24/null),evidence}; date={kind(relative_day/absolute/weekday),"
                "offset|year/month/day|weekday,evidence}; timezone={name,evidence}; "
                "interval={seconds,evidence}; recurrence={freq(daily/weekly),weekday?,evidence}. "
                "Every 'evidence' must be a span of raw_expression."
            ),
        },
    },
    "required": ["raw_expression", "trigger_kind"],
}

PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "title": {"type": "string"},
        "when": _WHEN_SCHEMA,
        "cron": {"type": "string"},
        "every_seconds": {"type": "integer"},
        "at": {"type": "string"},
        "timezone": {"type": "string"},
    },
    "required": ["goal"],
}

EXECUTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "title": {"type": "string"},
        "trigger": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["once", "interval", "cron"]},
                "at": {"type": "string"},
                "timezone": {"type": "string"},
                "cron_expr": {"type": "string"},
                "interval_s": {"type": "integer"},
            },
            "required": ["kind"],
        },
    },
    "required": ["goal", "trigger"],
}

CRITICAL_FIELDS = frozenset({"goal", "trigger", "actor", "requested_grants"})
EFFECTS = frozenset({EffectKind.STATE_CHANGE, EffectKind.DEFERRED, EffectKind.PERSISTENT})


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _exact_trigger(proposal: Mapping[str, Any]) -> dict[str, Any] | ResolutionResult:
    """Canonicalise an exact machine trigger (cron/every_seconds/at) or explain."""
    cron = str(proposal.get("cron", "")).strip()
    every = _positive_int(proposal.get("every_seconds"))
    at = str(proposal.get("at", "")).strip()
    timezone = str(proposal.get("timezone", "")).strip()
    provided = [bool(cron), bool(every), bool(at)]
    if sum(provided) != 1:
        return ResolutionResult(
            status=ResolutionStatus.MISSING,
            reason=(
                "Specify exactly one trigger: a natural-language 'when', or an exact "
                "'cron' (5-field local time), 'every_seconds', or 'at' (ISO-8601 local)."
            ),
            unresolved_fields=("trigger",),
        )
    if cron:
        return {"kind": "cron", "cron_expr": cron}
    if every:
        return {"kind": "interval", "interval_s": every}
    return {"kind": "once", "at": at, "timezone": timezone}


async def prepare_schedule_create(
    proposal: Mapping[str, Any], ctx: ResolverContext
) -> ActionDecision:
    """Admit a ``schedule_task`` proposal into canonical arguments (or clarify)."""
    goal = str(proposal.get("goal", "")).strip()
    title = str(proposal.get("title", "")).strip()
    timezone = str(proposal.get("timezone", "")).strip()
    when = proposal.get("when")

    if isinstance(when, Mapping) and str(when.get("raw_expression", "")).strip():
        resolution = resolve_temporal(when, ctx)
        if not resolution.resolved:
            # Ambiguous / missing / invalid / unsupported — do not create. The
            # critical ``trigger`` field is unresolved, so we fail closed into a
            # user clarification rather than guessing.
            return ActionDecision.needs_input_with(resolution)
        trigger = dict(resolution.value or {})
    else:
        exact = _exact_trigger(proposal)
        if isinstance(exact, ResolutionResult):
            return ActionDecision.needs_input_with(exact)
        trigger = exact

    if timezone and trigger.get("kind") == "once" and not trigger.get("timezone"):
        trigger = {**trigger, "timezone": timezone}

    return ActionDecision.ready_with({"goal": goal, "title": title, "trigger": trigger})


SCHEDULE_CREATE_CONTRACT = ActionContract(
    name=SCHEDULE_CREATE,
    version=CONTRACT_VERSION,
    proposal_schema=PROPOSAL_SCHEMA,
    execution_schema=EXECUTION_SCHEMA,
    critical_fields=CRITICAL_FIELDS,
    effects=EFFECTS,
    prepare=prepare_schedule_create,
)


def canonical_schedule_trigger(value: Mapping[str, Any]) -> ScheduleTrigger:
    """Map a canonical trigger dict (resolver/exact output) to a ScheduleTrigger."""
    kind = str(value.get("kind", "once"))
    if kind == "cron":
        return cron_trigger(str(value.get("cron_expr", "")))
    if kind == "interval":
        return interval_trigger(int(value.get("interval_s") or 0))
    return once_trigger(str(value.get("at", "")), str(value.get("timezone", "")))


# ── clarification presentation (interim; Phase 6 promotes to a durable draft) ──


def _hhmm(label: str) -> str:
    return label.split(" ")[-1] if label else label


def _next_day_iso(at_iso: str) -> str:
    try:
        return (datetime.fromisoformat(at_iso) + timedelta(days=1)).isoformat(timespec="seconds")
    except ValueError:
        return at_iso


def temporal_clarification_payload(
    resolution: ResolutionResult, raw_expression: str
) -> dict[str, Any]:
    """Render a resolver non-resolution into a ``needs_input`` tool result.

    Distinguishes a *candidate* (a grounded reading the user can pick) from a
    *repair option* (what to do about a reading that has already elapsed), the
    way the incident's target UX does.
    """
    raw = raw_expression or "that time"
    choices: list[dict[str, str]] = []
    lines: list[str] = []

    if resolution.ambiguous and resolution.unresolved_fields == ("day_period",):
        header = f"'{raw}' does not say whether it is AM or PM. Which did you mean?"
        for cand in resolution.candidates:
            hhmm = _hhmm(cand.label)
            if cand.validity == "future":
                choices.append({"id": f"pick:{cand.id}", "label": cand.label})
                lines.append(f"- {cand.label}")
            else:  # already elapsed → offer the same clock tomorrow instead
                nxt = _next_day_iso(str((cand.value or {}).get("at", "")))
                choices.append({"id": f"repair_next_day:{cand.id}", "label": f"tomorrow {hhmm}"})
                lines.append(f"- tomorrow {hhmm} ({nxt[:10]})")
        choices.append({"id": "run_now", "label": "run it now"})
        choices.append({"id": "cancel", "label": "cancel"})
        message = header + "\n" + "\n".join(lines)
    elif resolution.ambiguous and resolution.unresolved_fields == ("dst_fold",):
        message = f"'{raw}' occurs twice that day (a daylight-saving fall-back). Pick the exact instant:"
        for cand in resolution.candidates:
            choices.append({"id": f"pick:{cand.id}", "label": cand.label})
            lines.append(f"- {cand.label}")
        choices.append({"id": "cancel", "label": "cancel"})
        message = message + "\n" + "\n".join(lines)
    else:
        base = resolution.reason or "that time needs clarification."
        message = (
            f"{base} Please give a clearer time (for example 'today 7:10 pm'), "
            "or create it with an exact time (omni schedule add --at ...)."
        )
        choices.append({"id": "cancel", "label": "cancel"})

    return {
        "status": "needs_input",
        "outcome": "needs_input",
        "message": message,
        "error": message,
        "recovery_choices": choices,
        "resolution_status": resolution.status.value,
        "policy": POLICY_VERSION,
    }


__all__ = [
    "SCHEDULE_CREATE",
    "SCHEDULE_CREATE_CONTRACT",
    "PROPOSAL_SCHEMA",
    "EXECUTION_SCHEMA",
    "prepare_schedule_create",
    "canonical_schedule_trigger",
    "temporal_clarification_payload",
]
