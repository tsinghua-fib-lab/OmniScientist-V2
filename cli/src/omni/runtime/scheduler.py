"""Cron / scheduled jobs (P2).

A *schedule* is a recurring (or one-shot) source of background tasks: when it
comes due the :class:`Scheduler` materialises it into a normal
:class:`~omni.storage.models.SubtaskORM` via the task runtime — so schedules
reuse all of the runtime's durability, auto-retry and notification machinery
instead of re-implementing execution. A long-running process (``omni serve``)
ticks :meth:`Scheduler.run_due` from its poller so due jobs fire without a
separate daemon; one-shot CLI paths tick on demand.

The cron engine is a compact, dependency-free 5-field matcher (``minute hour
day-of-month month day-of-week`` with ``*``, ``*/step``, ``a-b``, ``a-b/step``
and comma lists), honouring the standard day-of-month / day-of-week OR rule.
Cron fields are read in the operator's **local** timezone (so ``0 18 * * *`` is
18:00 local); firing instants are stored/compared in UTC.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from omni.runtime.task_status import resolve_task_status
from omni.storage.models import ScheduleORM, SubtaskORM, TaskORM, _utcnow

logger = logging.getLogger(__name__)

# The general-purpose schedulable target: a free-form goal. Kept in sync with
# ``omni.scheduling.contracts.GOAL_SKILL`` (imported lazily to avoid a cycle).
_GOAL_SKILL = "agent-goal"

# Callback that runs a due *goal* schedule as a full headless orchestrator turn
# (planner→workflow→verification). Wired by the orchestrator; ``None`` in minimal
# runtimes falls back to the legacy direct-enqueue path.
GoalRunner = Callable[..., Awaitable[Any]]


@dataclass
class _Fire:
    """A claimed schedule occurrence captured inside the claim transaction.

    Holds only plain values (not a live/detached ORM row) so the enqueue phase
    can run after the claim commit without touching an expired SQLAlchemy
    instance.
    """

    id: str
    skill_name: str
    input_json: dict[str, Any]
    session_id: str
    channel: str
    title: str
    approved_tools: list[str]


@dataclass
class _RunView:
    """A surface-independent "last run" for observability (subtask or owning task)."""

    id: str
    task_id: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    result_json: dict[str, Any]
    error: str

# Bounded unattended-autonomy grants keyed by ``schedules.autonomy`` (see
# ``SchedulesCfg``). A scheduled run fires under ``omni serve`` with no
# interactive approver, so these tool names are pre-authorised for the run's
# owning task; the approval gate clears them via its preauthorizer while the OS
# sandbox / filesystem roots still constrain *where* they may act.
_AUTONOMY_TOOLS: dict[str, tuple[str, ...]] = {
    "off": (),
    "standard": ("write_file", "edit_file", "run_compute"),
    "full": ("write_file", "edit_file", "run_compute", "bash"),
}


def autonomy_tools(settings: Any) -> list[str]:
    """Sensitive tools a scheduled run may use unattended, per ``schedules.autonomy``."""
    mode = (
        str(getattr(getattr(settings, "schedules", None), "autonomy", "standard") or "standard")
        .strip()
        .lower()
    )
    return list(_AUTONOMY_TOOLS.get(mode, _AUTONOMY_TOOLS["standard"]))

# (lo, hi) inclusive bounds per cron field, in order.
_CRON_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
# How far ahead to search for the next cron match before giving up (a schedule
# that never matches within a year is treated as expired).
_CRON_SEARCH_MINUTES = 366 * 24 * 60


def _parse_cron_field(field: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field (``*``, ``*/n``, ``a-b``, ``a-b/n``, ``a,b``) to a set."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            step = max(1, int(step_s))
        else:
            base = part
        if base in ("*", ""):
            start, end = lo, hi
        elif "-" in base:
            start_s, _, end_s = base.partition("-")
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(base)
        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                values.add(v)
    return values


def parse_cron(expr: str) -> list[set[int]]:
    """Parse a 5-field cron expression into per-field value sets (raises on bad input)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(fields)}: {expr!r}")
    parsed: list[set[int]] = []
    for field, (lo, hi) in zip(fields, _CRON_BOUNDS, strict=True):
        # day-of-week: accept 7 as an alias for Sunday (0).
        norm = field.replace("7", "0") if (lo, hi) == (0, 6) and field.strip() == "7" else field
        vals = _parse_cron_field(norm, lo, hi)
        if not vals:
            raise ValueError(f"cron field {field!r} matches nothing in {expr!r}")
        parsed.append(vals)
    return parsed


def cron_matches(expr: str, dt: datetime) -> bool:
    """Whether ``dt`` (minute resolution) satisfies the 5-field cron ``expr``."""
    minute, hour, dom, month, dow = parse_cron(expr)
    cron_dow = (dt.weekday() + 1) % 7  # Python Mon=0..Sun=6 → cron Sun=0..Sat=6
    if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
        return False
    dom_restricted = dom != set(range(1, 32))
    dow_restricted = dow != set(range(0, 7))
    dom_ok = dt.day in dom
    dow_ok = cron_dow in dow
    # Standard cron OR rule: when both day fields are restricted, either matches.
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    if dom_restricted:
        return dom_ok
    if dow_restricted:
        return dow_ok
    return True


def next_cron_fire(expr: str, after: datetime) -> datetime | None:
    """First instant strictly after ``after`` matching ``expr`` in *local* time.

    Cron fields are read in the operator's local wall-clock zone — ``0 18 * * *``
    means 18:00 local, which is what a user means by "every day at 6pm" and what
    ``omni schedule`` prints — and the matching instant is returned as an aware
    UTC datetime for storage and due-time comparison. Stepping a *naive* local
    cursor keeps the match DST-robust; returns ``None`` if nothing matches within
    a year. (Timezone alignment mirrors ``omni.core.timefmt``: persist/compare in
    UTC, reason about wall-clock in the operator's zone.)
    """
    parse_cron(expr)  # validate up front
    aware_after = after if after.tzinfo else after.replace(tzinfo=UTC)
    # Interpret the search origin in local wall-clock, then step minute-by-minute.
    cursor = aware_after.astimezone().replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
    for _ in range(_CRON_SEARCH_MINUTES):
        if cron_matches(expr, cursor):
            # ``cursor`` is naive local; astimezone(UTC) reads it as local time.
            return cursor.astimezone(UTC)
        cursor += timedelta(minutes=1)
    return None


class Scheduler:
    """Owns the schedule store and fires due jobs into the task runtime.

    A due *goal* schedule (``agent-goal`` marker) runs the full interactive
    pipeline through the orchestrator's ``goal_runner`` — off the poller tick, so
    planning/execution never blocks the ticker — while an explicit-skill schedule
    keeps the direct durable enqueue. ``goal_runner`` is ``None`` in minimal
    runtimes / tests without an orchestrator, which fall back to enqueue.
    """

    def __init__(
        self, db: Any, runtime: Any, settings: Any, *, goal_runner: GoalRunner | None = None
    ) -> None:
        self._db = db
        self._runtime = runtime
        self._settings = settings
        self._goal_runner = goal_runner
        # Detached, tracked headless goal turns fired off the poller tick.
        self._inflight: set[asyncio.Task[Any]] = set()

    @property
    def enabled(self) -> bool:
        return bool(getattr(getattr(self._settings, "schedules", None), "enabled", True))

    def _headless_goal_fire(self, fire: _Fire) -> bool:
        """True when this fire runs as a full headless turn (not a direct skill)."""
        if self._goal_runner is None or fire.skill_name != _GOAL_SKILL:
            return False
        mode = (
            str(getattr(getattr(self._settings, "schedules", None), "execution_mode", "headless_turn")
                or "headless_turn")
            .strip()
            .lower()
        )
        return mode == "headless_turn"

    def _spawn_goal_turn(self, fire: _Fire, task_id: str) -> None:
        """Launch a headless scheduled-goal turn detached from the poller tick.

        The schedule occurrence is already *claimed* (``next_due_at`` advanced) in
        the committed transaction, so a crash mid-run loses only this occurrence
        rather than double-firing it. Exceptions are logged, never propagated.
        """
        goal = str((fire.input_json or {}).get("input") or fire.title or "")
        coro = self._goal_runner(  # type: ignore[misc]
            goal=goal,
            task_id=task_id,
            channel=fire.channel or "cli",
            session_id=fire.session_id,
            schedule_id=fire.id,
            approved_tools=list(fire.approved_tools or []),
        )
        task = asyncio.create_task(self._run_tracked(coro, fire.id))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    @staticmethod
    async def _run_tracked(coro: Awaitable[Any], schedule_id: str) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad goal turn must not stall the scheduler
            logger.exception("scheduled goal runner failed schedule=%s", schedule_id)

    async def drain_fires(self, *, timeout: float | None = None) -> None:
        """Await in-flight headless goal turns (tests / one-shot ``schedule run``)."""
        pending = [t for t in self._inflight if not t.done()]
        if not pending:
            return
        if timeout is not None:
            await asyncio.wait(pending, timeout=timeout)
        else:
            await asyncio.gather(*pending, return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel + await detached goal turns so none outlives the agent/DB."""
        pending = [t for t in self._inflight if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()

    async def add(
        self, skill_name: str, input_json: dict[str, Any] | None = None, *,
        kind: str = "interval", interval_s: int = 0, cron_expr: str = "",
        session_id: str = "", channel: str = "cli", title: str = "",
        first_due: datetime | None = None, approved_tools: list[str] | None = None,
    ) -> str:
        """Create a schedule and compute its first ``next_due_at``.

        ``kind`` is ``interval`` (needs ``interval_s``), ``cron`` (needs
        ``cron_expr``) or ``once`` (needs ``first_due``). ``first_due`` overrides
        the computed first fire (handy to fire immediately / at a fixed time).

        ``approved_tools`` are the sensitive tools this schedule may run
        unattended; ``None`` seeds the ``schedules.autonomy`` default so an
        owner-created schedule produces artefacts instead of failing closed on
        the daemon's missing approver.
        """
        now = _utcnow()
        due = first_due if first_due is not None else self._first_due(kind, now, interval_s, cron_expr)
        grants = (
            [str(t).strip() for t in approved_tools if str(t).strip()]
            if approved_tools is not None
            else autonomy_tools(self._settings)
        )
        row = ScheduleORM(
            project=getattr(getattr(self._settings, "paths", None), "project_name", "default"),
            session_id=session_id,
            channel=channel or "cli",
            title=title,
            skill_name=skill_name,
            input_json=dict(input_json or {}),
            kind=kind,
            interval_s=int(interval_s or 0),
            cron_expr=cron_expr or "",
            enabled=True,
            next_due_at=due,
            approved_tools=sorted(set(grants)),
        )
        async with self._db.session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row.id

    @staticmethod
    def _first_due(kind: str, now: datetime, interval_s: int, cron_expr: str) -> datetime | None:
        if kind == "interval":
            return now + timedelta(seconds=max(1, int(interval_s or 0)))
        if kind == "cron":
            return next_cron_fire(cron_expr, now)
        return None  # "once" without an explicit first_due never fires

    def _compute_next(self, sched: ScheduleORM, after: datetime) -> datetime | None:
        if sched.kind == "interval":
            return after + timedelta(seconds=max(1, int(sched.interval_s or 0)))
        if sched.kind == "cron":
            return next_cron_fire(sched.cron_expr, after)
        return None  # "once" → fire then disable

    async def list(self, *, include_disabled: bool = True, limit: int = 100) -> list[ScheduleORM]:
        async with self._db.session() as s:
            q = select(ScheduleORM)
            if not include_disabled:
                q = q.where(ScheduleORM.enabled.is_(True))
            q = q.order_by(ScheduleORM.next_due_at.asc().nullslast()).limit(limit)
            return list((await s.execute(q)).scalars().all())

    async def get(self, schedule_id: str) -> ScheduleORM | None:
        async with self._db.session() as s:
            exact = await s.get(ScheduleORM, schedule_id)
            if exact is not None:
                return exact
            rows = (await s.execute(select(ScheduleORM))).scalars().all()
        for row in rows:
            if row.id.startswith(schedule_id):
                return row
        return None

    async def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        sched = await self.get(schedule_id)
        if sched is None:
            return False
        async with self._db.session() as s:
            obj = await s.get(ScheduleORM, sched.id)
            if obj is None:
                return False
            obj.enabled = enabled
            # Re-arm a schedule that was re-enabled after its due time lapsed.
            if enabled and obj.next_due_at is None and obj.kind != "once":
                obj.next_due_at = self._compute_next(obj, _utcnow())
            await s.commit()
        return True

    async def remove(self, schedule_id: str) -> bool:
        sched = await self.get(schedule_id)
        if sched is None:
            return False
        async with self._db.session() as s:
            obj = await s.get(ScheduleORM, sched.id)
            if obj is not None:
                await s.delete(obj)
                await s.commit()
        return True

    async def run_due(self, *, now: datetime | None = None, limit: int | None = None) -> list[str]:
        """Fire every enabled schedule whose ``next_due_at`` has passed.

        Exactly-once, claim-then-fire: each due schedule is *claimed* by advancing
        its ``next_due_at`` (and bumping ``run_count`` / disabling ``once`` jobs)
        inside the same transaction that selects it, before any task is enqueued.
        SQLite serialises writes, so a second ticker — a legacy per-workspace
        daemon that has not yet been retired, or a raced poll — re-reads the
        advanced ``next_due_at`` and selects nothing, instead of double-firing
        the same occurrence. Missed occurrences are *skipped*, not backfilled (a
        long outage never triggers a stampede); ``once`` schedules disable after
        firing. Returns the enqueued task ids.
        """
        if not self.enabled:
            return []
        moment = now or _utcnow()
        cap = limit if limit is not None else int(
            getattr(getattr(self._settings, "schedules", None), "max_per_tick", 50) or 50
        )
        claimed: list[_Fire] = []
        async with self._db.session() as s:
            due = list((await s.execute(
                select(ScheduleORM)
                .where(
                    ScheduleORM.enabled.is_(True),
                    ScheduleORM.next_due_at.is_not(None),
                    ScheduleORM.next_due_at <= moment,
                )
                .order_by(ScheduleORM.next_due_at.asc())
                .limit(cap)
            )).scalars().all())
            for sched in due:
                claimed.append(_Fire(
                    id=sched.id,
                    skill_name=sched.skill_name,
                    input_json=dict(sched.input_json or {}),
                    session_id=sched.session_id,
                    channel=sched.channel or "cli",
                    title=sched.title or "",
                    approved_tools=list(sched.approved_tools or []),
                ))
                sched.last_run_at = moment
                sched.run_count = int(sched.run_count or 0) + 1
                nxt = self._compute_next(sched, moment)
                if sched.kind == "once" or nxt is None:
                    sched.enabled = False
                    sched.next_due_at = None
                else:
                    sched.next_due_at = nxt
            await s.commit()
        fired: list[str] = []
        for fire in claimed:
            try:
                task_id = await self._materialise_owning_task(fire)
                if self._headless_goal_fire(fire):
                    # Full planner→workflow→verification pipeline via the
                    # orchestrator, run off-tick so the poller never blocks. The
                    # owning task id is the run's reference (its workflow's
                    # executions hang under it, traceable via ``runs``).
                    self._spawn_goal_turn(fire, task_id)
                    ref_id = task_id
                else:
                    ref_id = await self._runtime.enqueue(
                        fire.skill_name,
                        dict(fire.input_json),
                        fire.channel,
                        session_id=fire.session_id,
                        task_id=task_id,
                        schedule_id=fire.id,
                    )
            except Exception:  # noqa: BLE001 - one bad schedule must not stall the rest
                logger.exception("schedule %s failed to enqueue (occurrence skipped)", fire.id)
                continue
            # For a legacy skill fire ``ref_id`` is the run's subtask and points
            # ``last_subtask_id`` straight at it. A headless goal fire's ``ref_id``
            # is the owning *task*; its representative subtask does not exist yet
            # (the turn runs off-tick), so ``run_scheduled_goal`` binds
            # ``last_subtask_id`` once the turn has materialised its subtasks.
            if not self._headless_goal_fire(fire):
                async with self._db.session() as s:
                    obj = await s.get(ScheduleORM, fire.id)
                    if obj is not None:
                        obj.last_subtask_id = ref_id
                        await s.commit()
            fired.append(ref_id)
        if fired:
            logger.info("scheduler fired %d job(s) at %s", len(fired), moment.isoformat())
        return fired

    async def _materialise_owning_task(self, fire: _Fire) -> str:
        """Create a first-class owning task for one fire, carrying its autonomy grant.

        A scheduled run has no interactive approver, so its owning task is
        granted the schedule's ``approved_tools`` up front — the approval gate
        then clears exactly those sensitive tools through its preauthorizer
        (the same ``TaskORM.approved_tools`` path a human uses to "approve for
        this task"). The task also makes each run visible in ``/task`` and
        links it to its schedule. Returns "" when no recorder is wired (tests /
        minimal runtimes) or on failure, so the run still fires — orphaned and
        fail-closed — rather than being dropped.
        """
        recorder = getattr(self._runtime, "task_recorder", None)
        if recorder is None:
            return ""
        goal = str((fire.input_json or {}).get("input") or fire.title or fire.skill_name)
        grants = list(fire.approved_tools or []) or autonomy_tools(self._settings)
        try:
            task = await recorder.create_task(
                session_id=fire.session_id,
                channel=fire.channel or "cli",
                user_input=goal,
                title=fire.title or goal[:80] or "Scheduled task",
                kind="turn",
                schedule_id=fire.id,
            )
        except Exception:  # noqa: BLE001 - never let task bookkeeping drop a fire
            logger.exception("schedule %s: owning task creation failed; running orphaned", fire.id)
            return ""
        if grants:
            try:
                await recorder.grant_tools(
                    task.id, grants, reason=f"scheduled run of {fire.id[:8]}"
                )
            except Exception:  # noqa: BLE001
                logger.exception("schedule %s: autonomy grant failed", fire.id)
        return task.id

    async def last_run(self, sched: ScheduleORM) -> _RunView | None:
        """Uniform "last run" view for observability, resolved honestly.

        A legacy skill fire and a headless goal fire that produced subtasks point
        ``last_subtask_id`` at the representative subtask (rich: result +
        artifacts). A headless turn that produced *no* subtask (e.g. a pure-ReAct
        answer) has no subtask to show, but it still ran — fall back to the newest
        owning task linked to the schedule so ``show``/``list`` report its real
        status instead of "never ran".
        """
        async with self._db.session() as s:
            if sched.last_subtask_id:
                sub = await s.get(SubtaskORM, sched.last_subtask_id)
                if sub is not None:
                    # R2: the representative subtask carries the rich result and
                    # artifacts, but *status* is owned by the settled task row —
                    # the same source ``/task show`` and ``/inbox`` use — so all
                    # three surfaces agree.
                    owner = await s.get(TaskORM, str(sub.task_id)) if sub.task_id else None
                    status = resolve_task_status(owner) if owner is not None else sub.status
                    return _RunView(
                        id=sub.id, task_id=str(sub.task_id or ""), status=status or sub.status,
                        started_at=sub.started_at, finished_at=sub.finished_at,
                        result_json=dict(sub.result_json or {}), error=str(sub.error or ""),
                    )
            task = (await s.execute(
                select(TaskORM)
                .where(TaskORM.schedule_id == sched.id)
                .order_by(TaskORM.created_at.desc())
                .limit(1)
            )).scalars().first()
            if task is None:
                return None
            return _RunView(
                id=task.id, task_id=task.id, status=task.status,
                started_at=task.started_at, finished_at=task.finished_at,
                result_json={"summary": task.summary or ""} if task.summary else {},
                error=str(task.error or ""),
            )

    async def bind_last_run(self, schedule_id: str, task_id: str) -> None:
        """Point ``last_subtask_id`` at the headless run's representative subtask.

        A headless goal turn's work is a set of subtasks under ``task_id`` (its
        owning task). The observability surface (``schedule show``/``list``)
        renders one "last run" subtask, so bind it to the newest subtask the turn
        produced. A pure-ReAct turn with no subtasks leaves the field untouched —
        the schedule row's ``run_count``/``last_run_at`` still record the fire.
        """
        if not schedule_id or not task_id:
            return
        async with self._db.session() as s:
            newest = (await s.execute(
                select(SubtaskORM.id)
                .where(SubtaskORM.task_id == task_id)
                .order_by(SubtaskORM.created_at.desc())
                .limit(1)
            )).scalars().first()
            if not newest:
                return
            obj = await s.get(ScheduleORM, schedule_id)
            if obj is not None:
                obj.last_subtask_id = newest
                await s.commit()

    async def runs(self, schedule_id: str, *, limit: int = 20) -> list[SubtaskORM]:
        """Skill executions this schedule has materialised, newest first.

        Covers both firing models: a legacy direct enqueue tags the subtask with
        ``schedule_id``; a headless-turn run's executions hang under an owning
        task tagged with ``schedule_id``, so its workflow's subtasks are found by
        that owning-task link.
        """
        sched = await self.get(schedule_id)
        if sched is None:
            return []
        async with self._db.session() as s:
            owning = list((await s.execute(
                select(TaskORM.id).where(TaskORM.schedule_id == sched.id)
            )).scalars().all())
            conds = [SubtaskORM.schedule_id == sched.id]
            if owning:
                conds.append(SubtaskORM.task_id.in_(owning))
            rows = (await s.execute(
                select(SubtaskORM)
                .where(or_(*conds))
                .order_by(SubtaskORM.created_at.desc())
                .limit(max(1, int(limit)))
            )).scalars().all()
        return list(rows)


__all__ = ["Scheduler", "autonomy_tools", "cron_matches", "next_cron_fire", "parse_cron"]
