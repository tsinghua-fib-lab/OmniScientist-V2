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

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from datetime import date as date_cls
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omni.core.action_contracts import (
    ResolutionCandidate,
    ResolutionResult,
    ResolutionStatus,
    ResolverContext,
)
from omni.core.timefmt import local_timezone_name

POLICY_VERSION = "temporal-policy-v1"

_T = TypeVar("_T")

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

    kind = str(when.get("trigger_kind", _TRIGGER_ONCE)).strip().lower() or _TRIGGER_ONCE
    if kind not in _SUPPORTED_KINDS:
        return ResolutionResult(
            status=ResolutionStatus.UNSUPPORTED,
            reason=f"unsupported trigger kind '{kind}'",
            unresolved_fields=("trigger_kind",),
        )
    constraints = when.get("constraints")
    constraints = constraints if isinstance(constraints, Mapping) else {}
    user_message = ctx.user_message or ""

    if kind == _TRIGGER_INTERVAL:
        return _resolve_interval(constraints, raw, user_message)
    if kind == _TRIGGER_CRON:
        return _resolve_cron(constraints, raw, user_message)

    # Evidence must be internally consistent with the model's quote. Worded
    # fields (date/zone/…) are then attested against the user message; clock
    # numbers are attested by extractors, not by requiring the quote's
    # orthography to be a substring of what the user typed.
    for sub in ("date", "clock", "timezone", "interval", "recurrence", "cron"):
        node = constraints.get(sub)
        if isinstance(node, Mapping):
            ev = node.get("evidence")
            if ev and not _grounded(ev, raw):
                return _invalid(f"evidence '{ev}' is not part of '{raw}'", sub)
            if (
                ev
                and sub != "clock"
                and user_message
                and not _grounded(ev, user_message)
            ):
                return _invalid(
                    f"evidence '{ev}' is not grounded in the user request", sub
                )

    zone, zone_name, _src, zone_err = _resolve_zone(constraints.get("timezone"), ctx)
    if zone_err:
        return _invalid(zone_err, "timezone")

    clock = _resolve_clock(constraints.get("clock"), raw, user_message)
    if isinstance(clock, ResolutionResult):
        return clock  # missing/invalid
    if kind == _TRIGGER_RECURRING:
        return _resolve_recurring(constraints, clock, raw)
    return _resolve_once(constraints.get("date"), clock, zone, zone_name, ctx)


# ── clock ────────────────────────────────────────────────────────────────────

# Day-part words carry a period only when the model copies them into the clock
# evidence (see ``test_explicit_evening_resolves_unique``); a word left elsewhere
# in the sentence never grounds this clock. Both lists therefore also carry the
# "today/tomorrow"-prefixed spellings: in Chinese, "this-evening" and "this-morning"
# name a part of the day exactly as the bare "evening"/"morning" words do, and
# omitting them asked the user to restate a period they had already given.
_AM_WORDS = (
    "\u4e0a\u5348",
    "\u65e9\u4e0a",
    "\u65e9\u6668",
    "\u6e05\u6668",
    "\u51cc\u6668",
    "\u4eca\u65e9",
    "\u4eca\u6668",
    "\u660e\u65e9",
)
_PM_WORDS = (
    "\u4e0b\u5348",
    "\u665a\u4e0a",
    "\u508d\u665a",
    "\u591c\u91cc",
    "\u591c\u95f4",
    "\u4e2d\u5348",
    "\u4eca\u665a",
    "\u4eca\u591c",
    "\u660e\u665a",
)
_AM_RE = re.compile(
    r"(?<![a-z])(?:a\.?m\.?|morning|midnight)(?![a-z])",
    re.IGNORECASE,
)
_PM_RE = re.compile(
    r"(?<![a-z])(?:p\.?m\.?|afternoon|evening|night|noon)(?![a-z])",
    re.IGNORECASE,
)
_H24_RE = re.compile(
    r"(?:24\s*(?:h|hours?|\u5c0f\u65f6\u5236|\u65f6\u5236)|24-hour)",
    re.IGNORECASE,
)
_DECIMAL_HOUR_RE = re.compile(
    r"(?<!\d)(\d{1,2})(?=\s*(?::|\u70b9|\u65f6|h\b|a\.?m|p\.?m))",
    re.IGNORECASE,
)
_CJK_HOUR_RE = re.compile(
    r"([\u96f6\u3007\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94"
    r"\u516d\u4e03\u516b\u4e5d\u5341]{1,3})(?=[\u70b9\u65f6])"
)
_CJK_DIGITS = {
    "\u96f6": 0,
    "\u3007": 0,
    "\u4e00": 1,
    "\u4e8c": 2,
    "\u4e24": 2,
    "\u4e09": 3,
    "\u56db": 4,
    "\u4e94": 5,
    "\u516d": 6,
    "\u4e03": 7,
    "\u516b": 8,
    "\u4e5d": 9,
}
_ENGLISH_HOURS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "twenty-one": 21,
    "twenty-two": 22,
    "twenty-three": 23,
}
_ENGLISH_CLOCK_RE = re.compile(
    r"\b([a-z]+(?:-[a-z]+)?)(?:\s+([a-z]+(?:-[a-z]+)?))?"
    r"\s*(?:a\.?m\.?|p\.?m\.?|o'clock)\b",
    re.IGNORECASE,
)
_ENGLISH_MINUTES = {
    **{word: value for word, value in _ENGLISH_HOURS.items() if value <= 19},
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
}
for _tens_word, _tens_value in (
    ("twenty", 20),
    ("thirty", 30),
    ("forty", 40),
    ("fifty", 50),
):
    for _unit_word, _unit_value in tuple(_ENGLISH_MINUTES.items()):
        if 1 <= _unit_value <= 9:
            _ENGLISH_MINUTES[f"{_tens_word}-{_unit_word}"] = (
                _tens_value + _unit_value
            )
_COLON_TIME_RE = re.compile(
    r"(?<!\d)\d{1,2}\s*:\s*(\d{1,2})(?:\s*:\s*(\d{1,2}))?"
)
_CJK_MINUTE_RE = re.compile(
    r"(?:\d{1,2}|[\u96f6\u3007\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94"
    r"\u516d\u4e03\u516b\u4e5d\u5341]{1,3})[\u70b9\u65f6]\s*(\d{1,2})"
)
_CJK_MINUTE_WORD_RE = re.compile(
    r"(?:\d{1,2}|[\u96f6\u3007\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94"
    r"\u516d\u4e03\u516b\u4e5d\u5341]{1,3})[\u70b9\u65f6]\s*"
    r"([\u96f6\u3007\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94"
    r"\u516d\u4e03\u516b\u4e5d\u5341]{1,3})\u5206"
)


def _explicit_period(raw: str) -> str | None:
    """Return an explicitly evidenced AM/PM class, never a model completion."""
    am = any(word in raw for word in _AM_WORDS) or _AM_RE.search(raw) is not None
    pm = any(word in raw for word in _PM_WORDS) or _PM_RE.search(raw) is not None
    if am == pm:
        return None
    return "am" if am else "pm"


def _grounded_period_evidence(clock: Mapping[str, Any], clock_evidence: str) -> str:
    """Return an explicit period span, including languages unknown to the host.

    The semantic planner may normalize an arbitrary-language phrase to ``am`` or
    ``pm``, but it must also copy the exact phrase that carries that meaning.
    This preserves multilingual extraction without allowing a bare numeric clock
    to acquire a model-invented period.
    """
    evidence = str(clock.get("day_period_evidence") or "").strip()
    if not evidence:
        return ""
    if not _grounded(evidence, clock_evidence) or not any(
        char.isalpha() for char in evidence
    ):
        return ""
    return evidence


def _cjk_hour(value: str) -> int | None:
    ten = "\u5341"
    if ten not in value:
        return _CJK_DIGITS.get(value)
    left, right = value.split(ten, 1)
    tens = _CJK_DIGITS.get(left, 1) if left else 1
    ones = _CJK_DIGITS.get(right, 0) if right else 0
    return tens * 10 + ones


def _evidenced_hours(value: str) -> set[int]:
    hours = {int(match) for match in _DECIMAL_HOUR_RE.findall(value)}
    hours.update(
        hour
        for match in _ENGLISH_CLOCK_RE.finditer(value)
        if (hour := _ENGLISH_HOURS.get(match.group(1).lower())) is not None
    )
    hours.update(
        hour
        for token in _CJK_HOUR_RE.findall(value)
        if (hour := _cjk_hour(token)) is not None
    )
    return {hour for hour in hours if 0 <= hour <= 23}


def _evidenced_subhours(value: str) -> tuple[set[int], set[int]]:
    minutes: set[int] = set()
    seconds: set[int] = set()
    for minute, second in _COLON_TIME_RE.findall(value):
        minutes.add(int(minute))
        if second:
            seconds.add(int(second))
    minutes.update(int(value) for value in _CJK_MINUTE_RE.findall(value))
    minutes.update(
        minute
        for token in _CJK_MINUTE_WORD_RE.findall(value)
        if (minute := _cjk_hour(token)) is not None
    )
    minutes.update(
        minute
        for match in _ENGLISH_CLOCK_RE.finditer(value)
        if (minute := _ENGLISH_MINUTES.get((match.group(2) or "zero").lower()))
        is not None
    )
    return minutes, seconds


def _attested(selected: set[int], user_vals: set[int]) -> bool:
    """True when every selected number appears in the user's wording.

    An empty selection has nothing to attest. A non-empty selection against a
    silent user span means the number lives only in the model's quote.
    """
    if not selected:
        return True
    if not user_vals:
        return False
    return selected <= user_vals


def _widen(
    narrow: str,
    wide: str,
    extract: Callable[[str], _T],
    *,
    silent: Callable[[_T], bool] = lambda value: not value,
    widest: str = "",
) -> tuple[_T, str]:
    """Ground a copied value: model evidence, then model quote, then user text.

    Shaped after Codex's ``seek_sequence`` (exact → looser) rather than rejecting
    a near-miss outright. The rungs are ``clock.evidence``, ``raw_expression``,
    and the turn's user message. Widening only when a rung is *silent* keeps a
    narrow quote able to pick one time out of a sentence that mentions several.

    Run 82322c46 is why the second rung exists: the model read 21:12 correctly
    but quoted only the hour, so the minute looked ungrounded. Run 2367d610 is
    why the third rung exists: the model added a conventional minute marker the
    user never wrote, and a substring check on the whole quote refused a clock
    the extractors already saw in the user text.

    The day period deliberately does *not* use this ladder. An hour is a value
    the model **copies**; a period is a meaning the host **derives**, and a
    period word elsewhere in the sentence belongs to a different clock.
    """
    value = extract(narrow)
    if not silent(value):
        return value, narrow
    value = extract(wide)
    if not silent(value):
        return value, wide
    if widest:
        return extract(widest), widest
    return value, wide


def _resolve_clock(
    clock: Any, raw: str = "", user_message: str = ""
) -> list[tuple[int, int, int, str]] | ResolutionResult:
    """Return ``[(hour24, minute, second, period_label), …]`` or a MISSING/INVALID.

    One entry when the reading is unique; two (am, pm) when the surface hour is
    1–12 and no day-period was given — the ambiguity this whole design exists to
    surface rather than silently collapse.

    Numeric components are grounded through :func:`_widen`: the model's
    ``evidence`` span, then ``raw_expression``, then the user message. The day
    period is deliberately *not* widened — see that helper for why.
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

    clock_evidence = str(clock.get("evidence") or "")
    evidenced_hours, hour_span = _widen(
        clock_evidence, raw, _evidenced_hours, widest=user_message
    )
    if len(evidenced_hours) > 1:
        return _invalid(
            f"'{hour_span}' states more than one hour {sorted(evidenced_hours)}; "
            "quote only the intended time in clock.evidence",
            "clock",
        )
    if evidenced_hours and surface_hour not in evidenced_hours:
        return _invalid(f"surface_hour {surface_hour} does not appear in '{hour_span}'", "clock")
    if not evidenced_hours and (surface_hour == 0 or surface_hour >= 13):
        return _invalid(
            f"surface_hour {surface_hour} is a 24-hour value that '{hour_span}' never states",
            "clock",
        )
    if user_message and not _attested(evidenced_hours, _evidenced_hours(user_message)):
        return _invalid(
            f"'{hour_span}' states hour(s) {sorted(evidenced_hours)} that the "
            "user request does not",
            "clock",
        )
    (evidenced_minutes, evidenced_seconds), sub_span = _widen(
        clock_evidence,
        raw,
        _evidenced_subhours,
        silent=lambda pair: not pair[0] and not pair[1],
        widest=user_message,
    )
    if len(evidenced_minutes) > 1 or len(evidenced_seconds) > 1:
        return _invalid(
            f"'{sub_span}' states more than one time; quote only the intended one "
            "in clock.evidence",
            "clock",
        )
    if (evidenced_minutes and minute not in evidenced_minutes) or (
        not evidenced_minutes and minute != 0
    ):
        return _invalid(f"minute {minute} does not appear in '{sub_span}'", "clock")
    if (evidenced_seconds and second not in evidenced_seconds) or (
        not evidenced_seconds and second != 0
    ):
        return _invalid(f"second {second} does not appear in '{sub_span}'", "clock")
    if user_message:
        user_minutes, user_seconds = _evidenced_subhours(user_message)
        if not _attested(evidenced_minutes, user_minutes):
            return _invalid(
                f"'{sub_span}' states minute(s) {sorted(evidenced_minutes)} that "
                "the user request does not",
                "clock",
            )
        if not _attested(evidenced_seconds, user_seconds):
            return _invalid(
                f"'{sub_span}' states second(s) {sorted(evidenced_seconds)} that "
                "the user request does not",
                "clock",
            )
    proposed_period = str(clock.get("day_period") or "").strip().lower() or None
    system = str(clock.get("hour_system") or "").strip() or None
    evidenced_period = _explicit_period(clock_evidence)
    period_evidence = str(clock.get("day_period_evidence") or "").strip()
    grounded_period_evidence = _grounded_period_evidence(clock, clock_evidence)
    if period_evidence and not grounded_period_evidence:
        return _invalid("day-period evidence is not grounded in clock evidence", "clock")
    if proposed_period in {"am", "morning"}:
        period = (
            "am"
            if evidenced_period == "am"
            or (evidenced_period is None and grounded_period_evidence)
            else None
        )
    elif proposed_period in {"pm", "evening", "afternoon", "night"}:
        period = (
            "pm"
            if evidenced_period == "pm"
            or (evidenced_period is None and grounded_period_evidence)
            else None
        )
    else:
        period = None

    # A model may not relabel a bare 1–12 hour as 24-hour notation. Accept that
    # hint only when the user's wording explicitly says 24h; 0 and 13–23 remain
    # structurally unambiguous regardless of an hour-system hint.
    explicit_24h = system == "24" and _H24_RE.search(clock_evidence) is not None
    if explicit_24h or surface_hour == 0 or surface_hour >= 13:
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


def _resolve_interval(
    constraints: Mapping[str, Any], raw: str, user_message: str = ""
) -> ResolutionResult:
    node = constraints.get("interval")
    if not isinstance(node, Mapping):
        return _missing("interval", "An interval schedule needs a duration in seconds.")
    ev = node.get("evidence")
    if ev and not _grounded(ev, raw):
        return _invalid(f"evidence '{ev}' is not part of '{raw}'", "interval")
    if ev and user_message and not _grounded(ev, user_message):
        return _invalid(f"evidence '{ev}' is not grounded in the user request", "interval")
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


def _resolve_cron(
    constraints: Mapping[str, Any], raw: str, user_message: str = ""
) -> ResolutionResult:
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
    if ev and user_message and not _grounded(ev, user_message):
        return _invalid(f"evidence '{ev}' is not grounded in the user request", "cron")
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
