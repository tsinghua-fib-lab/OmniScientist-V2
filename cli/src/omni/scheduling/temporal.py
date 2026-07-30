"""TemporalResolver — the first Action resolver tenant (``temporal-policy-v1``).

Turns a model's *grounded* time proposal (a small, bounded IR — never free-text
NL parsing) into a canonical schedule trigger, or into candidates when the
meaning is genuinely ambiguous. The classic failure this closes: a bare hour
like "7:10" with no AM/PM was silently completed by the model to 19:10 to dodge
the past-time guard. Here that stays :data:`ResolutionStatus.AMBIGUOUS` with two
grounded candidates.

Policy (``temporal-policy-v1``):

* Timezone precedence: explicit ``constraints.timezone`` > actor/turn zone >
  process local.
* Seconds default to 0.
* A missing date rolls to the next occurrence **only** once the clock is
  uniquely resolved (never while AM/PM is unknown).
* A missing AM/PM is **never** inferred by "which reading is still in the
  future" — both readings become candidates.
* An explicit date is never shifted to force a future instant.
* A DST *gap* (non-existent wall time) is ``INVALID``; a DST *fold* (a wall
  time that occurs twice) is ``AMBIGUOUS``.
* Past-ness of a *uniquely* resolved instant is **not** decided here: the
  resolver reports it ``RESOLVED`` and the scheduling tenant reuses the existing
  ``ScheduleService`` past-guard (structured ``needs_input`` recovery).
* Model confidence never participates (there is no confidence input).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from datetime import date as date_cls
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omni.core.action_contracts import (
    ResolutionCandidate,
    ResolutionResult,
    ResolutionStatus,
    ResolverContext,
)
from omni.core.timefmt import local_timezone_name

POLICY_VERSION = "temporal-policy-v1"

_TRIGGER_ONCE = "once"
_TRIGGER_INTERVAL = "interval"
_TRIGGER_CRON = "cron"
_TRIGGER_RECURRING = "recurring"
_SUPPORTED_KINDS = frozenset({_TRIGGER_ONCE, _TRIGGER_INTERVAL, _TRIGGER_CRON, _TRIGGER_RECURRING})


def _norm(text: Any) -> str:
    """Whitespace/case-insensitive normalisation for evidence grounding.

    Some languages (e.g. Chinese) have no word spaces, so we simply drop all
    whitespace and lowercase; a with-space and without-space spelling of the same
    expression compare equal without a natural-language dictionary.
    """
    return "".join(str(text or "").split()).lower()


def _grounded(needle: Any, haystack: Any) -> bool:
    n = _norm(needle)
    return bool(n) and n in _norm(haystack)


def _invalid(reason: str, field: str = "") -> ResolutionResult:
    return ResolutionResult(
        status=ResolutionStatus.INVALID,
        reason=reason,
        unresolved_fields=(field,) if field else (),
    )


def _missing(field: str, reason: str) -> ResolutionResult:
    return ResolutionResult(status=ResolutionStatus.MISSING, reason=reason, unresolved_fields=(field,))


def resolve_temporal(when: Mapping[str, Any], ctx: ResolverContext) -> ResolutionResult:
    """Resolve a grounded ``when`` proposal against the frozen ``ctx``."""
    if not isinstance(when, Mapping):
        return _invalid("temporal proposal must be an object", "when")
    raw = str(when.get("raw_expression", "")).strip()
    if not raw:
        return _missing("raw_expression", "The time proposal is missing the user's raw wording.")
    # The raw expression must actually be part of what the user said this turn —
    # the model may not smuggle in a time the user never expressed.
    if ctx.user_message and not _grounded(raw, ctx.user_message):
        return _invalid(
            f"proposed time '{raw}' is not grounded in the user request", "raw_expression"
        )

    kind = str(when.get("trigger_kind", _TRIGGER_ONCE)).strip().lower() or _TRIGGER_ONCE
    if kind not in _SUPPORTED_KINDS:
        return ResolutionResult(
            status=ResolutionStatus.UNSUPPORTED,
            reason=f"unsupported trigger kind '{kind}'",
            unresolved_fields=("trigger_kind",),
        )
    constraints = when.get("constraints")
    constraints = constraints if isinstance(constraints, Mapping) else {}

    if kind == _TRIGGER_INTERVAL:
        return _resolve_interval(constraints, raw)
    if kind == _TRIGGER_CRON:
        return _resolve_cron(constraints, raw)

    # Evidence spans (when present) must belong to the raw expression.
    for sub in ("date", "clock", "timezone", "interval", "recurrence", "cron"):
        node = constraints.get(sub)
        if isinstance(node, Mapping):
            ev = node.get("evidence")
            if ev and not _grounded(ev, raw):
                return _invalid(f"evidence '{ev}' is not part of '{raw}'", sub)

    zone, zone_name, _src, zone_err = _resolve_zone(constraints.get("timezone"), ctx)
    if zone_err:
        return _invalid(zone_err, "timezone")

    clock = _resolve_clock(constraints.get("clock"))
    if isinstance(clock, ResolutionResult):
        return clock  # missing/invalid
    if kind == _TRIGGER_RECURRING:
        return _resolve_recurring(constraints, clock, raw)
    return _resolve_once(constraints.get("date"), clock, zone, zone_name, ctx)


# ── clock ────────────────────────────────────────────────────────────────────


def _resolve_clock(clock: Any) -> list[tuple[int, int, int, str]] | ResolutionResult:
    """Return ``[(hour24, minute, second, period_label), …]`` or a MISSING/INVALID.

    One entry when the reading is unique; two (am, pm) when the surface hour is
    1–12 and no day-period was given — the ambiguity this whole design exists to
    surface rather than silently collapse.
    """
    if not isinstance(clock, Mapping):
        return _missing("clock", "The time-of-day (hour/minute) was not provided.")
    try:
        surface_hour = int(clock.get("surface_hour"))
        minute = int(clock.get("minute", 0))
    except (TypeError, ValueError):
        return _invalid("clock hour/minute is not a number", "clock")
    second = clock.get("second", 0)
    try:
        second = int(second or 0)
    except (TypeError, ValueError):
        second = 0
    if not (0 <= minute <= 59) or not (0 <= second <= 59):
        return _invalid("clock minute/second out of range", "clock")

    period = str(clock.get("day_period") or "").strip().lower() or None
    system = str(clock.get("hour_system") or "").strip() or None

    # Unambiguous 24-hour reading: explicit 24h, midnight, or hour ≥ 13.
    if system == "24" or surface_hour == 0 or surface_hour >= 13:
        if not (0 <= surface_hour <= 23):
            return _invalid("clock hour out of range", "clock")
        return [(surface_hour, minute, second, "")]
    if not (1 <= surface_hour <= 12):
        return _invalid("clock hour out of range", "clock")

    base = surface_hour % 12  # 12 → 0
    if period in {"am", "morning"}:
        return [(base, minute, second, "am")]
    if period in {"pm", "evening", "afternoon", "night"}:
        return [(base + 12, minute, second, "pm")]
    # Genuinely ambiguous: both readings are grounded, surface both.
    return [(base, minute, second, "am"), (base + 12, minute, second, "pm")]


# ── once ─────────────────────────────────────────────────────────────────────


def _resolve_once(
    date_node: Any,
    clock: list[tuple[int, int, int, str]],
    zone: ZoneInfo | None,
    zone_name: str,
    ctx: ResolverContext,
) -> ResolutionResult:
    day = _resolve_date(date_node, ctx)
    if isinstance(day, ResolutionResult):
        return day  # invalid date
    ambiguous_period = len(clock) > 1

    # Missing date is only auto-rolled to the next occurrence once the clock is
    # unique; while AM/PM is unknown we ask the period first (policy v1).
    if day is None and ambiguous_period:
        day = _local_date(ctx)

    candidates: list[ResolutionCandidate] = []
    for hour, minute, second, period in clock:
        target_day = day if day is not None else _next_occurrence_date(hour, minute, second, zone, ctx)
        naive = datetime(target_day.year, target_day.month, target_day.day, hour, minute, second)
        validity = _dst_validity(naive, zone)
        if validity == "dst_fold":
            candidates.extend(_fold_candidates(naive, zone, zone_name, ctx))
            continue
        aware = _aware(naive, zone)
        cand = _once_candidate(period or "only", aware, zone_name, ctx, dst_gap=(validity == "dst_gap"))
        candidates.append(cand)

    if ambiguous_period:
        return ResolutionResult(
            status=ResolutionStatus.AMBIGUOUS,
            candidates=tuple(candidates),
            unresolved_fields=("day_period",),
            reason="the time of day is ambiguous (AM vs PM was not specified)",
        )
    # Unique clock. A DST fold expanded to two candidates ⇒ ambiguous fold.
    if len(candidates) > 1:
        return ResolutionResult(
            status=ResolutionStatus.AMBIGUOUS,
            candidates=tuple(candidates),
            unresolved_fields=("dst_fold",),
            reason="the wall-clock time occurs twice on this day (DST fold)",
        )
    only = candidates[0]
    if only.validity == "dst_gap":
        return _invalid(
            "the requested wall-clock time does not exist on this day (DST gap)", "clock"
        )
    return ResolutionResult(
        status=ResolutionStatus.RESOLVED,
        value=only.value,
        candidates=(only,),
        evidence=({"raw": ctx.user_message[:200], "zone": zone_name, "policy": POLICY_VERSION},),
    )


def _once_candidate(
    period: str,
    aware: datetime,
    zone_name: str,
    ctx: ResolverContext,
    *,
    dst_gap: bool = False,
) -> ResolutionCandidate:
    is_past = aware.astimezone(UTC) <= _aware_ref(ctx)
    validity = "dst_gap" if dst_gap else ("past" if is_past else "future")
    return ResolutionCandidate(
        id=period,
        value={
            "kind": "once",
            "at": aware.isoformat(timespec="seconds"),
            "timezone": zone_name,
        },
        label=aware.strftime("%Y-%m-%d %H:%M"),
        validity=validity,
        metadata={"day_period": period, "policy": POLICY_VERSION},
    )


def _fold_candidates(
    naive: datetime, zone: ZoneInfo | None, zone_name: str, ctx: ResolverContext
) -> list[ResolutionCandidate]:
    out: list[ResolutionCandidate] = []
    for fold in (0, 1):
        aware = _aware(naive, zone, fold=fold)
        out.append(
            ResolutionCandidate(
                id=f"fold{fold}",
                value={"kind": "once", "at": aware.isoformat(timespec="seconds"), "timezone": zone_name},
                label=aware.strftime("%Y-%m-%d %H:%M %z"),
                validity="dst_fold",
                metadata={"fold": fold, "policy": POLICY_VERSION},
            )
        )
    return out


# ── date ─────────────────────────────────────────────────────────────────────


def _resolve_date(date_node: Any, ctx: ResolverContext) -> date_cls | None | ResolutionResult:
    if not isinstance(date_node, Mapping):
        return None
    kind = str(date_node.get("kind") or "").strip().lower()
    if kind in {"absolute", "date"}:
        try:
            return date_cls(int(date_node["year"]), int(date_node["month"]), int(date_node["day"]))
        except (KeyError, TypeError, ValueError):
            return _invalid("absolute date is incomplete or invalid", "date")
    if kind in {"relative_day", "relative", "offset"}:
        try:
            offset = int(date_node.get("offset", 0))
        except (TypeError, ValueError):
            return _invalid("relative day offset is not a number", "date")
        return _local_date(ctx) + timedelta(days=offset)
    if kind == "weekday":
        try:
            target = int(date_node["weekday"]) % 7
        except (KeyError, TypeError, ValueError):
            return _invalid("weekday is invalid", "date")
        today = _local_date(ctx)
        ahead = (target - today.weekday()) % 7
        return today + timedelta(days=ahead)
    return _invalid(f"unsupported date kind '{kind}'", "date")


def _next_occurrence_date(
    hour: int, minute: int, second: int, zone: ZoneInfo | None, ctx: ResolverContext
) -> date_cls:
    """Today if that instant is still in the future, else tomorrow (unique clock)."""
    today = _local_date(ctx)
    naive = datetime(today.year, today.month, today.day, hour, minute, second)
    if _dst_validity(naive, zone) == "dst_gap":
        return today + timedelta(days=1)
    if _aware(naive, zone).astimezone(UTC) > _aware_ref(ctx):
        return today
    return today + timedelta(days=1)


# ── interval / cron / recurring ───────────────────────────────────────────────


def _resolve_interval(constraints: Mapping[str, Any], raw: str) -> ResolutionResult:
    node = constraints.get("interval")
    if not isinstance(node, Mapping):
        return _missing("interval", "An interval schedule needs a duration in seconds.")
    ev = node.get("evidence")
    if ev and not _grounded(ev, raw):
        return _invalid(f"evidence '{ev}' is not part of '{raw}'", "interval")
    try:
        seconds = int(node.get("seconds"))
    except (TypeError, ValueError):
        return _invalid("interval seconds is not a number", "interval")
    if seconds <= 0:
        return _invalid("interval seconds must be positive", "interval")
    return ResolutionResult(
        status=ResolutionStatus.RESOLVED,
        value={"kind": "interval", "interval_s": seconds},
        evidence=({"policy": POLICY_VERSION},),
    )


def _resolve_cron(constraints: Mapping[str, Any], raw: str) -> ResolutionResult:
    node = constraints.get("cron")
    if not isinstance(node, Mapping):
        return _missing("cron", "A cron schedule needs a 5-field expression.")
    expr = str(node.get("expr", "")).strip()
    if not expr:
        return _missing("cron", "A cron schedule needs a 5-field expression.")
    # A natural-language cron must carry the wording it was derived from.
    ev = node.get("evidence")
    if ev and not _grounded(ev, raw):
        return _invalid(f"evidence '{ev}' is not part of '{raw}'", "cron")
    return ResolutionResult(
        status=ResolutionStatus.RESOLVED,
        value={"kind": "cron", "cron_expr": expr},
        evidence=({"policy": POLICY_VERSION},),
    )


def _resolve_recurring(
    constraints: Mapping[str, Any], clock: list[tuple[int, int, int, str]], raw: str
) -> ResolutionResult:
    node = constraints.get("recurrence")
    if not isinstance(node, Mapping):
        return _missing("recurrence", "A recurring schedule needs a frequency (daily/weekly).")
    if len(clock) > 1:
        # Recurring time-of-day is ambiguous too — ask AM/PM before committing.
        hh_am, mm, _ss, _p = clock[0]
        hh_pm = clock[1][0]
        cands = tuple(
            ResolutionCandidate(
                id=pid,
                value={"kind": "cron", "cron_expr": f"{mm} {hh} * * *"},
                label=f"{hh:02d}:{mm:02d} daily",
                validity="future",
                metadata={"day_period": pid, "policy": POLICY_VERSION},
            )
            for pid, hh in (("am", hh_am), ("pm", hh_pm))
        )
        return ResolutionResult(
            status=ResolutionStatus.AMBIGUOUS,
            candidates=cands,
            unresolved_fields=("day_period",),
            reason="the recurring time of day is ambiguous (AM vs PM was not specified)",
        )
    hour, minute, _second, _period = clock[0]
    freq = str(node.get("freq") or "").strip().lower()
    if freq == "daily":
        expr = f"{minute} {hour} * * *"
    elif freq == "weekly":
        try:
            dow = int(node.get("weekday")) % 7
        except (TypeError, ValueError):
            return _invalid("weekly recurrence needs a weekday", "recurrence")
        expr = f"{minute} {hour} * * {dow}"
    else:
        return ResolutionResult(
            status=ResolutionStatus.UNSUPPORTED,
            reason=f"unsupported recurrence frequency '{freq}'",
            unresolved_fields=("recurrence",),
        )
    return ResolutionResult(
        status=ResolutionStatus.RESOLVED,
        value={"kind": "cron", "cron_expr": expr},
        evidence=({"policy": POLICY_VERSION},),
    )


# ── timezone / dst helpers ────────────────────────────────────────────────────


def _resolve_zone(
    tz_node: Any, ctx: ResolverContext
) -> tuple[ZoneInfo | None, str, str, str]:
    """Return ``(zone, iana_name, source, error)`` honouring policy precedence."""
    explicit = ""
    if isinstance(tz_node, Mapping):
        explicit = str(tz_node.get("name") or "").strip()
    elif isinstance(tz_node, str):
        explicit = tz_node.strip()
    if explicit:
        try:
            return ZoneInfo(explicit), explicit, "explicit", ""
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return None, "", "explicit", f"Unknown timezone '{explicit}'."
    if ctx.timezone:
        try:
            return ZoneInfo(ctx.timezone), ctx.timezone, "actor", ""
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    name = local_timezone_name()
    if name:
        try:
            return ZoneInfo(name), name, "process", ""
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    # Fall back to the process-local offset with no IANA label.
    return None, name, "process", ""


def _aware(naive: datetime, zone: ZoneInfo | None, *, fold: int = 0) -> datetime:
    if zone is None:
        return naive.astimezone()  # naive interpreted as local, keeps wall time
    return naive.replace(tzinfo=zone, fold=fold)


def _dst_validity(naive: datetime, zone: ZoneInfo | None) -> str:
    """"ok" | "dst_gap" | "dst_fold" for a naive wall time in ``zone``."""
    if zone is None:
        return "ok"
    a = naive.replace(tzinfo=zone, fold=0)
    b = naive.replace(tzinfo=zone, fold=1)
    if a.utcoffset() == b.utcoffset():
        return "ok"
    round_trip = a.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    return "dst_gap" if round_trip != naive else "dst_fold"


def _aware_ref(ctx: ResolverContext) -> datetime:
    ref = ctx.reference_time
    return ref if ref.tzinfo else ref.replace(tzinfo=UTC)


def _local_date(ctx: ResolverContext) -> date_cls:
    """The reference *local* date in the turn's zone."""
    ref = _aware_ref(ctx)
    if ctx.timezone:
        try:
            return ref.astimezone(ZoneInfo(ctx.timezone)).date()
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    return ref.astimezone().date()


__all__ = ["POLICY_VERSION", "resolve_temporal"]
