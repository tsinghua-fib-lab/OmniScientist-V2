"""ScheduleService — the single application service behind every schedule surface.

Both the ``omni schedule`` CLI and the ``schedule_task`` agent tool call
:meth:`ScheduleService.create`; nothing else decides trigger vocabulary, time
semantics, skill validation, or whether creation needs approval. This is the
"one core, thin adapters" spine (Codex funnels every surface through one core),
plus the piece Codex's in-memory approval cannot give a cross-process IM turn: a
**durable action proposal** that a later local ``omni schedule approve <id>``
resumes by id, executing the exact stored payload.

Consent model
-------------
Creating a schedule the machine owner explicitly typed locally is not
surprising, so a CLI/local request is created directly (``omni schedule add``
never prompted either). An IM-originated request has no local interactive
approver, so instead of a dead-end denial it persists a proposal and returns
``awaiting_approval`` with the resume command. The unattended *future runs* stay
governed by ``approved_tools`` / ``schedules.autonomy`` exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from omni.channels.security import channel_requires_sensitive_confirm, is_im_channel
from omni.core.timefmt import format_local_time
from omni.runtime.scheduler import Scheduler, parse_cron
from omni.scheduling.contracts import (
    GOAL_SKILL,
    STATUS_AWAITING_APPROVAL,
    STATUS_CREATED,
    STATUS_ERROR,
    STATUS_NEEDS_INPUT,
    STATUS_REJECTED,
    TRIGGER_CRON,
    TRIGGER_INTERVAL,
    TRIGGER_ONCE,
    ScheduleActor,
    ScheduleCreateRequest,
    ScheduleCreateResult,
    resolve_once_instant,
    to_cli_argv,
)
from omni.scheduling.presentation import build_summary
from omni.storage.models import ScheduleActionProposalORM, ScheduleORM, _utcnow

# Past-time admission vs the turn's frozen receipt clock: a few seconds of
# skew at the boundary must not reject a time the user still meant as future.
_ADMISSION_GRACE = timedelta(seconds=60)
# IM once-schedules due this soon cannot survive a WeChat reply + local
# `omni schedule approve` round-trip. Already-clarified goals skip that gate.
_NEAR_TERM_APPROVAL = timedelta(minutes=15)

# How long a pending approval proposal stays actionable before it expires. A
# scheduling request is time-relevant; a week-old "approve this daily digest" is
# almost never what the owner still wants, so we let it lapse rather than fire a
# stale action. Deliberately generous so a busy owner has time to approve.
_PROPOSAL_TTL = timedelta(hours=24)


def _spec_label(kind: str, interval_s: int, cron_expr: str) -> str:
    if kind == TRIGGER_INTERVAL:
        return f"every {interval_s}s"
    if kind == TRIGGER_CRON:
        return f"cron {cron_expr}"
    return "once"


class ScheduleService:
    """Create / approve / deny schedules through one validated contract."""

    def __init__(self, db: Any, runtime: Any, settings: Any, *, registry: Any = None) -> None:
        self._db = db
        self._runtime = runtime
        self._settings = settings
        self._registry = registry
        # One-time per-instance sweep of legacy per-workspace proposals into the
        # machine-global store (see :meth:`_open_store`).
        self._migrated = False

    def _scheduler(self, db: Any = None) -> Scheduler:
        return Scheduler(db or self._db, self._runtime, self._settings)

    # ── stores ────────────────────────────────────────────────────────────────
    #
    # Approval proposals are machine-owner state, not project state: an IM turn is
    # served on one anchor workspace while the owner approves from whatever repo
    # they happen to be in. Keeping them in a per-workspace ``sessions.sqlite3``
    # made ``omni schedule approve <id>`` silently miss the proposal (the CLI
    # opened a different workspace DB than the daemon wrote to). They live in the
    # home-global control store instead; the created *schedule* still lands in the
    # originating workspace so its result returns to the right channel/inbox.

    async def _open_store(self) -> Any:
        """Return the initialised machine-global control DB (home ``control.sqlite3``).

        On first use per service instance, sweep any legacy proposals still sitting
        in the caller's own workspace DB into the control store so an in-flight
        pre-upgrade proposal is not orphaned by the move.
        """
        from omni.storage.db import get_database

        home = getattr(getattr(self._settings, "paths", None), "home", None)
        control_path = (
            self._settings.paths.control_db
            if home is not None and hasattr(self._settings.paths, "control_db")
            else Path(str(home or ".")) / "control.sqlite3"
        )
        store = get_database(control_path)
        await store.init()
        if not self._migrated:
            self._migrated = True
            await self._migrate_legacy_proposals(store)
        return store

    async def _migrate_legacy_proposals(self, store: Any) -> None:
        """Copy pending proposals from the caller's workspace DB into ``store``.

        Best-effort and idempotent: rows already present (by id) are skipped, and
        any failure is swallowed — a missed migration only means the owner re-asks,
        never a crash. ``self._db`` and ``store`` can be the same file in a
        single-workspace test/home; the id guard makes that a no-op.
        """
        if self._db is None or getattr(self._db, "path", None) == getattr(store, "path", None):
            return
        try:
            async with self._db.session() as src:
                legacy = list(
                    (
                        await src.execute(
                            select(ScheduleActionProposalORM).where(
                                ScheduleActionProposalORM.state == "pending"
                            )
                        )
                    ).scalars().all()
                )
            if not legacy:
                return
            origin_dir = str(getattr(getattr(self._settings, "paths", None), "project_dir", "") or "")
            async with store.session() as dst:
                for row in legacy:
                    if await dst.get(ScheduleActionProposalORM, row.id) is not None:
                        continue
                    dst.add(
                        ScheduleActionProposalORM(
                            id=row.id, project=row.project, channel=row.channel,
                            session_id=row.session_id, actor_principal=row.actor_principal,
                            origin_project_dir=getattr(row, "origin_project_dir", "") or origin_dir,
                            kind=row.kind, title=row.title, summary=row.summary,
                            payload_json=dict(row.payload_json or {}), payload_digest=row.payload_digest,
                            idempotency_key=row.idempotency_key, state=row.state,
                            result_schedule_id=row.result_schedule_id, decided_by=row.decided_by,
                            expires_at=row.expires_at, created_at=row.created_at,
                        )
                    )
                await dst.commit()
        except Exception:  # noqa: BLE001 - migration is a courtesy, never fatal
            return

    async def _origin_scheduler(self, origin_project_dir: str) -> Scheduler:
        """A scheduler bound to the workspace that originated a proposal.

        The approved schedule must be created where it will fire and deliver its
        result (the IM anchor for an IM request), which is usually *not* the
        workspace the approving CLI resolved to. Falls back to the local DB when
        the origin is unknown (legacy rows) or unreadable.
        """
        if not origin_project_dir:
            return self._scheduler()
        try:
            from omni.storage.db import get_database

            db = get_database(Path(origin_project_dir) / "sessions.sqlite3")
            await db.init()
            return self._scheduler(db)
        except Exception:  # noqa: BLE001 - never dead-end approval on a bad path
            return self._scheduler()

    # ── creation ────────────────────────────────────────────────────────────

    async def create(self, request: ScheduleCreateRequest) -> ScheduleCreateResult:
        """Validate + normalise a request, then create or propose a schedule."""
        # 1. Target skill must exist (goal-based always resolves to agent-goal).
        target_error = self._validate_target(request)
        if target_error is not None:
            return self._finish(target_error)

        # 2. Normalise the trigger to storable fields (one time-semantics site).
        norm = self._normalize(request)
        if isinstance(norm, ScheduleCreateResult):  # needs_input / error
            return self._finish(norm)
        kind, interval_s, cron_expr, first_due, tz_label = norm

        # 3. Consent: an IM request with no local approver becomes a durable
        #    proposal; a local/CLI request is created directly. An already-
        #    clarified near-term once-schedule skips that round-trip: a
        #    two-minute WeChat slot plus laptop approve is structurally late.
        if self._requires_proposal(request.actor):
            if request.already_clarified and _is_near_term_once(first_due):
                return self._finish(
                    await self._materialize(
                        request,
                        kind=kind,
                        interval_s=interval_s,
                        cron_expr=cron_expr,
                        first_due=first_due,
                        tz_label=tz_label,
                    )
                )
            proposed = await self._propose(
                request, kind=kind, interval_s=interval_s, cron_expr=cron_expr, tz_label=tz_label
            )
            proposed.near_term = _is_near_term_once(first_due)
            return self._finish(proposed)

        return self._finish(
            await self._materialize(
                request, kind=kind, interval_s=interval_s, cron_expr=cron_expr, first_due=first_due, tz_label=tz_label
            )
        )

    def _validate_target(self, request: ScheduleCreateRequest) -> ScheduleCreateResult | None:
        skill = request.skill_name or GOAL_SKILL
        if skill == GOAL_SKILL:
            return None
        if self._registry is not None and self._registry.get(skill) is None:
            return ScheduleCreateResult(
                status=STATUS_REJECTED,
                channel=request.actor.channel,
                reason=f"unknown skill '{skill}'",
                error=(
                    f"No skill named '{skill}' is installed. Use `omni skills list` to see "
                    "available skills, or schedule a free-form goal with --goal."
                ),
            )
        return None

    def _normalize(
        self, request: ScheduleCreateRequest
    ) -> tuple[str, int, str, datetime | None, str] | ScheduleCreateResult:
        trig = request.trigger
        provided = [bool(trig.cron_expr), bool(trig.interval_s), bool(trig.at)]
        if sum(provided) != 1:
            return ScheduleCreateResult(
                status=STATUS_NEEDS_INPUT,
                channel=request.actor.channel,
                reason="ambiguous trigger",
                error=(
                    "Specify exactly one trigger: a cron expression (--cron), a fixed "
                    "interval (--every seconds), or a one-time time (--at)."
                ),
            )
        if trig.kind == TRIGGER_CRON or trig.cron_expr:
            try:
                parse_cron(trig.cron_expr)
            except ValueError as exc:
                return ScheduleCreateResult(
                    status=STATUS_ERROR,
                    channel=request.actor.channel,
                    error=f"Invalid cron expression: {exc}",
                )
            return TRIGGER_CRON, 0, trig.cron_expr, None, ""
        if trig.kind == TRIGGER_INTERVAL or trig.interval_s:
            if int(trig.interval_s) <= 0:
                return ScheduleCreateResult(
                    status=STATUS_ERROR,
                    channel=request.actor.channel,
                    error="Interval schedules need a positive number of seconds.",
                )
            return TRIGGER_INTERVAL, int(trig.interval_s), "", None, ""
        # one-time — prefer the turn's frozen reference_time so past-time
        # admission agrees with the semantic resolver (never a fresh wall clock
        # that can disagree mid-turn or break offline tests).
        now = request.reference_time or _utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        due_utc, tz_label, error = resolve_once_instant(trig.at, trig.timezone, now=now)
        if error:
            return ScheduleCreateResult(status=STATUS_ERROR, channel=request.actor.channel, error=error)
        if due_utc is not None and due_utc + _ADMISSION_GRACE <= now:
            # Past one-time: never silently create a schedule that already
            # elapsed at *receipt*. Offer structured recovery instead (L4).
            # Host latency after receipt is not "the user gave a past time".
            return ScheduleCreateResult(
                status=STATUS_NEEDS_INPUT,
                channel=request.actor.channel,
                reason="one-time trigger is in the past",
                timezone=tz_label,
                error=(
                    f"The requested time {format_local_time(due_utc)} is already in the past. "
                    "Pick a future time, or run the goal now instead of scheduling it."
                ),
                recovery_choices=[
                    {"id": "future_time", "label": "Schedule it for a future date/time"},
                    {"id": "run_now", "label": "Run the goal now instead of scheduling"},
                    {"id": "cancel", "label": "Cancel — don't schedule anything"},
                ],
            )
        return TRIGGER_ONCE, 0, "", due_utc, tz_label

    def _requires_proposal(self, actor: ScheduleActor) -> bool:
        """True when creation must wait for a local approval (no interactive approver).

        An IM turn (WeChat/Feishu/DingTalk) is served by a daemon with no local
        approver, mirroring Codex routing a sensitive action to a reviewer that
        is not in the turn's process. Everything else (CLI/local) is created
        directly.
        """
        return is_im_channel(actor.channel) and channel_requires_sensitive_confirm(
            self._settings, actor.channel
        )

    async def _materialize(
        self,
        request: ScheduleCreateRequest,
        *,
        kind: str,
        interval_s: int,
        cron_expr: str,
        first_due: datetime | None,
        tz_label: str,
        scheduler: Scheduler | None = None,
    ) -> ScheduleCreateResult:
        scheduler = scheduler or self._scheduler()
        schedule_id = await scheduler.add(
            request.skill_name or GOAL_SKILL,
            request.resolved_input(),
            kind=kind,
            interval_s=interval_s,
            cron_expr=cron_expr,
            session_id=request.actor.session_id,
            channel=request.actor.channel or "cli",
            title=request.resolved_title(),
            first_due=first_due,
            approved_tools=request.requested_grants,
        )
        sched = await scheduler.get(schedule_id)
        return self._created_result(request, sched, kind=kind, interval_s=interval_s, cron_expr=cron_expr, tz_label=tz_label)

    def _created_result(
        self,
        request: ScheduleCreateRequest,
        sched: ScheduleORM | None,
        *,
        kind: str,
        interval_s: int,
        cron_expr: str,
        tz_label: str,
    ) -> ScheduleCreateResult:
        enabled, runner_ready = self._readiness()
        grants = list(getattr(sched, "approved_tools", None) or []) if sched else []
        next_run = (
            format_local_time(sched.next_due_at, tz=tz_label)
            if sched and sched.next_due_at
            else "not scheduled"
        )
        return ScheduleCreateResult(
            status=STATUS_CREATED,
            schedule_id=sched.id if sched else "",
            kind=kind,
            spec=_spec_label(kind, interval_s, cron_expr),
            title=request.resolved_title(),
            goal=request.resolved_goal(),
            next_run_local=next_run,
            timezone=tz_label,
            channel=request.actor.channel or "cli",
            approved_tools=grants,
            registered=sched is not None,
            scheduling_enabled=enabled,
            runner_ready=runner_ready,
        )

    async def _propose(
        self,
        request: ScheduleCreateRequest,
        *,
        kind: str,
        interval_s: int,
        cron_expr: str,
        tz_label: str,
    ) -> ScheduleCreateResult:
        payload = request.canonical_payload()
        digest = request.digest()
        idem = request.idempotency_key.strip()
        origin_dir = str(getattr(getattr(self._settings, "paths", None), "project_dir", "") or "")
        store = await self._open_store()
        async with store.session() as s:
            # Idempotency: an identical pending proposal (same actor + digest, or
            # same idempotency key) converges instead of piling up duplicates.
            existing = await self._find_open_proposal(s, request.actor.principal, digest, idem)
            if existing is not None:
                return self._proposal_result(request, existing, kind=kind, interval_s=interval_s, cron_expr=cron_expr, tz_label=tz_label)
            row = ScheduleActionProposalORM(
                project=str(getattr(getattr(self._settings, "paths", None), "project_name", "default") or "default"),
                channel=request.actor.channel or "cli",
                session_id=request.actor.session_id,
                actor_principal=request.actor.principal or "local",
                origin_project_dir=origin_dir,
                kind="schedule_create",
                title=request.resolved_title(),
                payload_json=payload,
                payload_digest=digest,
                idempotency_key=idem,
                state="pending",
                expires_at=_utcnow() + _PROPOSAL_TTL,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return self._proposal_result(request, row, kind=kind, interval_s=interval_s, cron_expr=cron_expr, tz_label=tz_label)

    async def _find_open_proposal(
        self, s: Any, principal: str, digest: str, idem: str
    ) -> ScheduleActionProposalORM | None:
        q = select(ScheduleActionProposalORM).where(
            ScheduleActionProposalORM.state == "pending"
        )
        rows = list((await s.execute(q)).scalars().all())
        now = _utcnow()
        for row in rows:
            if row.expires_at is not None and _as_utc(row.expires_at) <= now:
                continue
            if idem and row.idempotency_key == idem:
                return row
            if row.actor_principal == principal and row.payload_digest == digest:
                return row
        return None

    def _proposal_result(
        self,
        request: ScheduleCreateRequest,
        row: ScheduleActionProposalORM,
        *,
        kind: str,
        interval_s: int,
        cron_expr: str,
        tz_label: str,
    ) -> ScheduleCreateResult:
        enabled, runner_ready = self._readiness()
        return ScheduleCreateResult(
            status=STATUS_AWAITING_APPROVAL,
            proposal_id=row.id,
            kind=kind,
            spec=_spec_label(kind, interval_s, cron_expr),
            title=request.resolved_title(),
            goal=request.resolved_goal(),
            timezone=tz_label,
            channel=request.actor.channel or "cli",
            scheduling_enabled=enabled,
            runner_ready=runner_ready,
            approve_command=f"omni schedule approve {row.id[:8]}",
        )

    # ── approval resume (by id) ───────────────────────────────────────────────

    async def approve(self, proposal_id: str, *, decided_by: str = "local") -> ScheduleCreateResult:
        """Execute a stored proposal's payload — the resume-by-id half of the flow.

        Revalidates state / expiry / digest and re-checks the time (a one-time
        instant must still be in the future), then creates the schedule from the
        *stored* payload. Idempotent: a replay returns the already-created
        schedule instead of a second row.
        """
        store = await self._open_store()
        origin_dir = ""
        async with store.session() as s:
            row = await self._load_proposal(s, proposal_id)
            if row is None:
                return self._finish(ScheduleCreateResult(status=STATUS_ERROR, error=f"No pending schedule proposal matches '{proposal_id}'."))
            origin_dir = getattr(row, "origin_project_dir", "") or ""
            if row.state == "approved" and row.result_schedule_id:
                # Replay: converge on the schedule created by the first approval
                # (it lives in the origin workspace, not necessarily this CLI's).
                sched = await (await self._origin_scheduler(origin_dir)).get(row.result_schedule_id)
                request = ScheduleCreateRequest.from_payload(row.payload_json or {})
                norm = self._normalize(request)
                kind, interval_s, cron_expr = (
                    (norm[0], norm[1], norm[2]) if not isinstance(norm, ScheduleCreateResult) else (request.trigger.kind, request.trigger.interval_s, request.trigger.cron_expr)
                )
                return self._finish(self._created_result(request, sched, kind=kind, interval_s=interval_s, cron_expr=cron_expr, tz_label=request.trigger.timezone))
            if row.state != "pending":
                return self._finish(ScheduleCreateResult(status=STATUS_REJECTED, proposal_id=row.id, error=f"Proposal {row.id[:8]} is already {row.state}."))
            if row.expires_at is not None and _as_utc(row.expires_at) <= _utcnow():
                row.state = "expired"
                await s.commit()
                return self._finish(ScheduleCreateResult(status=STATUS_REJECTED, proposal_id=row.id, error=f"Proposal {row.id[:8]} expired before approval; ask again to create a fresh one."))

            request = ScheduleCreateRequest.from_payload(row.payload_json or {})
            if request.digest() != (row.payload_digest or ""):
                row.state = "denied"
                row.decided_by = decided_by
                await s.commit()
                return self._finish(ScheduleCreateResult(status=STATUS_REJECTED, proposal_id=row.id, error=f"Proposal {row.id[:8]} failed its integrity check and was rejected."))

        # Re-normalise now (a one-time instant may have lapsed while pending).
        # A slot that was future at admission and slipped during local approve
        # runs immediately — the owner is confirming the work, not re-stating
        # a time they already gave.
        norm = self._normalize(request)
        slot_elapsed = False
        if isinstance(norm, ScheduleCreateResult):
            if "past" in (norm.reason or ""):
                first_due = _utcnow()
                kind, interval_s, cron_expr, tz_label = (
                    TRIGGER_ONCE,
                    0,
                    "",
                    request.trigger.timezone,
                )
                slot_elapsed = True
            else:
                return self._finish(norm)
        else:
            kind, interval_s, cron_expr, first_due, tz_label = norm
        # Materialise back into the *originating* workspace so its runtime fires
        # the schedule and its channel manager delivers the result — not this
        # (possibly unrelated) approving CLI's workspace.
        origin_scheduler = (await self._origin_scheduler(origin_dir)) if origin_dir else None
        result = await self._materialize(
            request, kind=kind, interval_s=interval_s, cron_expr=cron_expr, first_due=first_due,
            tz_label=tz_label, scheduler=origin_scheduler,
        )
        async with store.session() as s:
            row = await self._load_proposal(s, proposal_id)
            if row is not None and row.state == "pending":
                row.state = "approved"
                row.decided_by = decided_by
                row.result_schedule_id = result.schedule_id
                await s.commit()
        result.proposal_id = proposal_id
        result.slot_elapsed = slot_elapsed
        return self._finish(result)

    async def deny(self, proposal_id: str, *, decided_by: str = "local") -> ScheduleCreateResult:
        store = await self._open_store()
        async with store.session() as s:
            row = await self._load_proposal(s, proposal_id)
            if row is None:
                return self._finish(ScheduleCreateResult(status=STATUS_ERROR, error=f"No schedule proposal matches '{proposal_id}'."))
            if row.state != "pending":
                return self._finish(ScheduleCreateResult(status=STATUS_REJECTED, proposal_id=row.id, error=f"Proposal {row.id[:8]} is already {row.state}."))
            row.state = "denied"
            row.decided_by = decided_by
            await s.commit()
        return self._finish(ScheduleCreateResult(status=STATUS_REJECTED, proposal_id=proposal_id, reason="denied by owner", summary=f"Denied schedule proposal {proposal_id[:8]}."))

    async def list_proposals(
        self, *, include_all: bool = False, limit: int = 30
    ) -> list[ScheduleActionProposalORM]:
        store = await self._open_store()
        async with store.session() as s:
            q = select(ScheduleActionProposalORM)
            if not include_all:
                q = q.where(ScheduleActionProposalORM.state == "pending")
            q = q.order_by(ScheduleActionProposalORM.created_at.desc()).limit(max(1, int(limit)))
            rows = list((await s.execute(q)).scalars().all())
        # Lazily lapse displayed proposals so a stale pending never looks live.
        now = _utcnow()
        for row in rows:
            if row.state == "pending" and row.expires_at is not None and _as_utc(row.expires_at) <= now:
                row.state = "expired"
        return rows

    async def _load_proposal(self, s: Any, proposal_id: str) -> ScheduleActionProposalORM | None:
        pid = (proposal_id or "").strip()
        if not pid:
            return None
        exact = await s.get(ScheduleActionProposalORM, pid)
        if exact is not None:
            return exact
        rows = (await s.execute(select(ScheduleActionProposalORM))).scalars().all()
        for row in rows:
            if row.id.startswith(pid):
                return row
        return None

    # ── readiness (independent axes) ─────────────────────────────────────────

    def _readiness(self) -> tuple[bool, bool | None]:
        enabled = bool(getattr(getattr(self._settings, "schedules", None), "enabled", False))
        return enabled, self._runner_ready()

    def _runner_ready(self) -> bool | None:
        paths = getattr(self._settings, "paths", None)
        if paths is None:
            return None
        probes: list[bool] = []
        try:  # home-level supervised service (newer control plane)
            from omni.runtime.service_state import service_is_running

            probes.append(bool(service_is_running(paths)))
        except Exception:  # noqa: BLE001 - a liveness probe must never break scheduling
            pass
        try:  # legacy per-workspace daemon
            from omni.runtime.daemon import daemon_info

            probes.append(bool(daemon_info(paths)))
        except Exception:  # noqa: BLE001
            pass
        if not probes:
            return None
        return any(probes)

    def _finish(self, result: ScheduleCreateResult) -> ScheduleCreateResult:
        """Attach the deterministic, truthful summary + fallback command."""
        if result.status == STATUS_AWAITING_APPROVAL and not result.approve_command and result.proposal_id:
            result.approve_command = f"omni schedule approve {result.proposal_id[:8]}"
        result.summary = build_summary(result)
        return result


def cli_fallback_command(request: ScheduleCreateRequest) -> str:
    """A ready-to-paste ``omni …`` command equivalent to ``request`` (deterministic)."""
    return "omni " + " ".join(_shell_quote(part) for part in to_cli_argv(request))


def _shell_quote(part: str) -> str:
    if part and all(c.isalnum() or c in "-_./:=" for c in part):
        return part
    return "'" + part.replace("'", "'\\''") + "'"


def _is_near_term_once(first_due: datetime | None, *, now: datetime | None = None) -> bool:
    """True when a one-time due is already here or due within the IM approve window."""
    if first_due is None:
        return False
    moment = now or _utcnow()
    if first_due.tzinfo is None:
        first_due = first_due.replace(tzinfo=UTC)
    else:
        first_due = first_due.astimezone(UTC)
    return (first_due - moment) <= _NEAR_TERM_APPROVAL


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = ["ScheduleService", "cli_fallback_command"]
