"""The one canonical scheduling request, its result, and their serialisers.

Every schedule-creation surface builds a :class:`ScheduleCreateRequest` and hands
it to :class:`~omni.scheduling.service.ScheduleService`; nothing parses or
re-composes a schedule command on its own. This is the structural fix for the
``omni schedule add --at`` incident: the CLI and the ``schedule_task`` tool share
one trigger vocabulary and (critically) **one time-normalisation site**, so a
naive wall-clock time means the operator's local zone on *both* paths instead of
UTC on one and local on the other.

Time policy (mirrors ``omni.core.timefmt``): a naive ``at`` is the operator's
local wall-clock (or an explicit IANA ``timezone``); it is converted to a UTC
instant for storage. A one-time trigger already in the past is **not** silently
created — the service returns a structured ``needs_input`` with recovery choices.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The general-purpose, schedulable target: a ReAct sub-agent over a free-form
# goal. Goal-based requests target it; ``omni schedule add <skill>`` can target
# any registered skill instead.
GOAL_SKILL = "agent-goal"

TRIGGER_INTERVAL = "interval"
TRIGGER_CRON = "cron"
TRIGGER_ONCE = "once"

# Terminal outcomes the service can reach. These are the *only* things a
# scheduling turn may report as done (Codex maps a resolved decision to a
# definitive tool result the same way): a created schedule, a durable pending
# approval, an explicit clarification, or an explicit rejection/error.
STATUS_CREATED = "created"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_NEEDS_INPUT = "needs_input"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class ScheduleTrigger:
    """When a schedule fires. Exactly one kind is meaningful per request."""

    kind: str  # interval | cron | once
    interval_s: int = 0
    cron_expr: str = ""
    at: str = ""  # raw ISO-8601 as provided (naive ⇒ operator-local)
    timezone: str = ""  # optional explicit IANA zone for a naive ``at``


def interval_trigger(interval_s: int) -> ScheduleTrigger:
    return ScheduleTrigger(kind=TRIGGER_INTERVAL, interval_s=int(interval_s or 0))


def cron_trigger(cron_expr: str) -> ScheduleTrigger:
    return ScheduleTrigger(kind=TRIGGER_CRON, cron_expr=str(cron_expr or "").strip())


def once_trigger(at: str, timezone: str = "") -> ScheduleTrigger:
    return ScheduleTrigger(kind=TRIGGER_ONCE, at=str(at or "").strip(), timezone=str(timezone or "").strip())


@dataclass(frozen=True)
class ScheduleActor:
    """Who is requesting the schedule (origin channel/session/identity).

    ``principal`` is the memory-isolation identity: ``"local"`` for the machine
    owner or ``"<channel>:<external_key>"`` for an IM peer. The service uses the
    channel to decide whether creation needs a durable approval proposal.
    """

    channel: str = "cli"
    session_id: str = ""
    principal: str = "local"


@dataclass(frozen=True)
class ScheduleCreateRequest:
    """The canonical, surface-independent request to create a schedule."""

    trigger: ScheduleTrigger
    goal: str = ""
    skill_name: str = GOAL_SKILL
    input: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    actor: ScheduleActor = field(default_factory=ScheduleActor)
    # Sensitive tools the schedule may run unattended (``None`` ⇒ autonomy
    # default; ``[]`` ⇒ fail-closed). Passed through to ``Scheduler.add``.
    requested_grants: list[str] | None = None
    idempotency_key: str = ""

    def resolved_input(self) -> dict[str, Any]:
        if self.goal.strip():
            return {"input": self.goal.strip()}
        return dict(self.input or {})

    def resolved_title(self) -> str:
        if self.title.strip():
            return self.title.strip()
        base = self.goal.strip() or str(self.resolved_input().get("input") or "").strip() or self.skill_name
        return (base[:57] + "…") if len(base) > 60 else base

    def canonical_payload(self) -> dict[str, Any]:
        """The immutable, comparable action snapshot stored in a proposal.

        Digested for tamper detection and replayed verbatim on approval, so the
        executed action is exactly the one the requester specified — never
        reconstructed from natural language.
        """
        grants = None if self.requested_grants is None else sorted({str(t).strip() for t in self.requested_grants if str(t).strip()})
        return {
            "skill_name": self.skill_name,
            "input": self.resolved_input(),
            "title": self.resolved_title(),
            "trigger": asdict(self.trigger),
            "actor": asdict(self.actor),
            "requested_grants": grants,
        }

    def digest(self) -> str:
        blob = json.dumps(self.canonical_payload(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ScheduleCreateRequest:
        """Rebuild a request from a stored :meth:`canonical_payload` snapshot."""
        trig = dict(payload.get("trigger") or {})
        actor = dict(payload.get("actor") or {})
        return cls(
            trigger=ScheduleTrigger(
                kind=str(trig.get("kind") or TRIGGER_ONCE),
                interval_s=int(trig.get("interval_s") or 0),
                cron_expr=str(trig.get("cron_expr") or ""),
                at=str(trig.get("at") or ""),
                timezone=str(trig.get("timezone") or ""),
            ),
            skill_name=str(payload.get("skill_name") or GOAL_SKILL),
            input=dict(payload.get("input") or {}),
            title=str(payload.get("title") or ""),
            actor=ScheduleActor(
                channel=str(actor.get("channel") or "cli"),
                session_id=str(actor.get("session_id") or ""),
                principal=str(actor.get("principal") or "local"),
            ),
            requested_grants=(
                None if payload.get("requested_grants") is None else list(payload.get("requested_grants") or [])
            ),
        )


@dataclass
class ScheduleCreateResult:
    """The definitive outcome of a create/approve call — no prose inference.

    Readiness is split into independent axes (registration / enablement / runner)
    so a caller never claims "it will fire" when only a row exists but no runner
    is up. ``summary`` is a deterministic, truthful sentence the CLI prints and
    the tool relays.
    """

    status: str  # created | awaiting_approval | needs_input | rejected | error
    schedule_id: str = ""
    proposal_id: str = ""
    kind: str = ""
    spec: str = ""
    title: str = ""
    next_run_local: str = ""
    timezone: str = ""
    channel: str = "cli"
    approved_tools: list[str] = field(default_factory=list)
    # Readiness axes (L6): a schedule fires only when all are true.
    registered: bool = False
    scheduling_enabled: bool = False
    runner_ready: bool | None = None  # None ⇒ could not determine
    # Clarification / recovery (L4).
    reason: str = ""
    recovery_choices: list[dict[str, str]] = field(default_factory=list)
    approve_command: str = ""
    error: str = ""
    summary: str = ""

    # Every terminal outcome is recorded under a single event type so
    # verification can require a real scheduling result instead of accepting
    # ``react.finished`` + model prose (Codex decision→result mapping).
    EVENT_TYPE = "schedule.resolved"

    def is_terminal_success(self) -> bool:
        return self.status in (STATUS_CREATED, STATUS_AWAITING_APPROVAL)

    def tool_result(self) -> dict[str, Any]:
        """Backward-compatible payload for the ``schedule_task`` tool.

        ``created`` maps to the historic ``"ok"`` status so existing callers and
        tests keep working; every other status is surfaced verbatim so the model
        relays a durable-pending / clarification / error truthfully.
        """
        payload: dict[str, Any] = {
            "status": "ok" if self.status == STATUS_CREATED else self.status,
            "outcome": self.status,
            "summary": self.summary,
        }
        if self.error:
            payload["error"] = self.error
        if self.status == STATUS_NEEDS_INPUT:
            payload["message"] = self.summary or self.reason
            if self.recovery_choices:
                payload["recovery_choices"] = self.recovery_choices
        if self.schedule_id:
            payload.update(
                schedule_id=self.schedule_id,
                kind=self.kind,
                spec=self.spec,
                title=self.title,
                next_run=self.next_run_local,
                channel=self.channel,
                approved_tools=self.approved_tools,
                runner_ready=self.runner_ready,
            )
        if self.proposal_id:
            payload.update(proposal_id=self.proposal_id, approve_command=self.approve_command)
        return payload


def to_cli_argv(request: ScheduleCreateRequest) -> list[str]:
    """Serialise a request into the exact ``omni schedule add`` argv.

    Deterministic so a surfaced fallback command is guaranteed to parse — the
    ``omni schedule add --at`` incident was a model hand-composing a flag that
    did not exist. The round-trip (argv → real Typer parser) is asserted in
    tests, making that class of drift impossible to ship.
    """
    argv: list[str] = ["schedule", "add"]
    trig = request.trigger
    if trig.kind == TRIGGER_CRON:
        argv += ["--cron", trig.cron_expr]
    elif trig.kind == TRIGGER_INTERVAL:
        argv += ["--every", str(int(trig.interval_s or 0))]
    else:
        argv += ["--at", trig.at]
        if trig.timezone:
            argv += ["--timezone", trig.timezone]
    if request.goal.strip():
        argv += ["--goal", request.goal.strip()]
    elif request.skill_name and request.skill_name != GOAL_SKILL:
        argv.append(request.skill_name)
        if request.input:
            argv += ["--input", json.dumps(request.input, ensure_ascii=False)]
    if request.title.strip():
        argv += ["--title", request.title.strip()]
    if request.actor.session_id:
        argv += ["--session", request.actor.session_id]
    if request.requested_grants is not None:
        for tool in request.requested_grants:
            if str(tool).strip():
                argv += ["--allow-tool", str(tool).strip()]
    return argv


@dataclass(frozen=True)
class NormalizedTrigger:
    """Result of resolving a trigger to storable fields + a display instant."""

    kind: str
    interval_s: int
    cron_expr: str
    first_due_utc: datetime | None
    timezone_label: str


def resolve_once_instant(
    at: str,
    timezone: str,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, str, str]:
    """Resolve a one-time ``at`` string to a UTC instant.

    Returns ``(due_utc, timezone_label, error)``. A naive ``at`` is interpreted
    in ``timezone`` when given, otherwise in the process-local zone (Python's
    ``astimezone`` convention) — never as UTC. ``error`` is non-empty when the
    timestamp or zone is unparseable.
    """
    raw = (at or "").strip()
    if not raw:
        return None, "", "A one-time schedule needs an 'at' timestamp."
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        return None, "", f"Invalid one-time timestamp '{at}': {exc}"
    label = ""
    if parsed.tzinfo is None:
        if timezone.strip():
            try:
                zone = ZoneInfo(timezone.strip())
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                return None, "", f"Unknown timezone '{timezone}'."
            parsed = parsed.replace(tzinfo=zone)
            label = timezone.strip()
        else:
            # Naive ⇒ operator-local wall-clock (same instant Python's
            # ``astimezone`` yields for a bare datetime).
            parsed = parsed.astimezone()
    else:
        label = str(parsed.tzinfo)
    return parsed.astimezone(UTC), label, ""


__all__ = [
    "GOAL_SKILL",
    "TRIGGER_INTERVAL",
    "TRIGGER_CRON",
    "TRIGGER_ONCE",
    "STATUS_CREATED",
    "STATUS_AWAITING_APPROVAL",
    "STATUS_NEEDS_INPUT",
    "STATUS_REJECTED",
    "STATUS_ERROR",
    "ScheduleTrigger",
    "ScheduleActor",
    "ScheduleCreateRequest",
    "ScheduleCreateResult",
    "NormalizedTrigger",
    "interval_trigger",
    "cron_trigger",
    "once_trigger",
    "to_cli_argv",
    "resolve_once_instant",
]
