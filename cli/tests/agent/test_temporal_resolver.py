"""TemporalResolver (temporal-policy-v1) — the first Action resolver tenant.

The property under test throughout: a *grounded* time proposal resolves to a
single canonical trigger only when it is genuinely unambiguous; a bare hour with
no AM/PM stays AMBIGUOUS with both readings as candidates (never collapsed by
discarding the past one), and impossible/twice-occurring wall times are handled
structurally rather than guessed.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from omni.core.action_contracts import ResolutionStatus, ResolverContext
from omni.scheduling.temporal import resolve_temporal

SH = ZoneInfo("Asia/Shanghai")
NY = ZoneInfo("America/New_York")

# The reported incident's clock: 09:49 Asia/Shanghai, so 07:10 is already past.
NOW_SH = datetime(2026, 7, 30, 9, 49, tzinfo=SH)


def _ctx(user_message: str, *, now: datetime = NOW_SH, tz: str = "Asia/Shanghai") -> ResolverContext:
    return ResolverContext(user_message=user_message, reference_time=now, timezone=tz)


def test_bare_time_is_ambiguous_and_past_candidate_is_not_dropped():
    when = {
        "raw_expression": "今天7点10分",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {"surface_hour": 7, "minute": 10, "day_period": None, "evidence": "7点10分"},
        },
    }
    result = resolve_temporal(when, _ctx("为 RAG 综述，今天7点10分执行"))
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.unresolved_fields == ("day_period",)
    by_id = {c.id: c for c in result.candidates}
    assert set(by_id) == {"am", "pm"}
    # The past reading survives as a candidate — disambiguation is by meaning,
    # not by filtering out whatever already elapsed.
    assert by_id["am"].validity == "past"
    assert by_id["am"].value["at"].endswith("07:10:00+08:00")
    assert by_id["pm"].validity == "future"
    assert by_id["pm"].value["at"].endswith("19:10:00+08:00")


def test_explicit_evening_resolves_unique():
    when = {
        "raw_expression": "今天晚上7点10分",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {"surface_hour": 7, "minute": 10, "day_period": "pm", "evidence": "晚上7点10分"},
        },
    }
    result = resolve_temporal(when, _ctx("今天晚上7点10分"))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["at"].endswith("19:10:00+08:00")
    assert result.value["timezone"] == "Asia/Shanghai"


def test_explicit_morning_past_still_resolves_but_marked_past():
    # Past-ness of a *unique* instant is decided downstream (ScheduleService
    # past-guard), so the resolver reports RESOLVED with a past-tagged candidate.
    when = {
        "raw_expression": "今天上午7点10分",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {"surface_hour": 7, "minute": 10, "day_period": "am", "evidence": "上午7点10分"},
        },
    }
    result = resolve_temporal(when, _ctx("今天上午7点10分"))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.candidates[0].validity == "past"
    assert result.value["at"].endswith("07:10:00+08:00")


def test_explicit_24_hour_resolves_unique():
    when = {
        "raw_expression": "今天19:10",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {"surface_hour": 19, "minute": 10, "evidence": "19:10"},
        },
    }
    result = resolve_temporal(when, _ctx("今天19:10"))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["at"].endswith("19:10:00+08:00")


def test_date_omitted_rolls_to_today_when_future():
    when = {
        "raw_expression": "19点",
        "trigger_kind": "once",
        "constraints": {"clock": {"surface_hour": 19, "minute": 0, "evidence": "19点"}},
    }
    result = resolve_temporal(when, _ctx("19点提醒我"))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["at"].startswith("2026-07-30T19:00")


def test_date_omitted_rolls_to_tomorrow_when_past():
    when = {
        "raw_expression": "6点",
        "trigger_kind": "once",
        "constraints": {"clock": {"surface_hour": 6, "minute": 0, "hour_system": "24", "evidence": "6点"}},
    }
    result = resolve_temporal(when, _ctx("6点提醒我"))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["at"].startswith("2026-07-31T06:00")


def test_absolute_date_is_not_shifted_to_the_future():
    when = {
        "raw_expression": "2026年8月1日上午9点",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "absolute", "year": 2026, "month": 8, "day": 1, "evidence": "2026年8月1日"},
            "clock": {"surface_hour": 9, "minute": 0, "day_period": "am", "evidence": "上午9点"},
        },
    }
    result = resolve_temporal(when, _ctx("2026年8月1日上午9点"))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["at"].startswith("2026-08-01T09:00")


def test_interval_duration_resolves():
    when = {
        "raw_expression": "每小时",
        "trigger_kind": "interval",
        "constraints": {"interval": {"seconds": 3600, "evidence": "每小时"}},
    }
    result = resolve_temporal(when, _ctx("每小时跑一次"))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.value == {"kind": "interval", "interval_s": 3600}


def test_recurring_ampm_is_ambiguous():
    when = {
        "raw_expression": "每天7点",
        "trigger_kind": "recurring",
        "constraints": {
            "recurrence": {"freq": "daily", "evidence": "每天"},
            "clock": {"surface_hour": 7, "minute": 0, "evidence": "7点"},
        },
    }
    result = resolve_temporal(when, _ctx("每天7点"))
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.unresolved_fields == ("day_period",)


def test_recurring_daily_resolves_to_cron():
    when = {
        "raw_expression": "每天晚上7点",
        "trigger_kind": "recurring",
        "constraints": {
            "recurrence": {"freq": "daily", "evidence": "每天"},
            "clock": {"surface_hour": 7, "minute": 0, "day_period": "pm", "evidence": "晚上7点"},
        },
    }
    result = resolve_temporal(when, _ctx("每天晚上7点"))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.value == {"kind": "cron", "cron_expr": "0 19 * * *"}


def test_explicit_timezone_overrides_actor_zone():
    when = {
        "raw_expression": "纽约时间今天19点",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {"surface_hour": 19, "minute": 0, "evidence": "19点"},
            "timezone": {"name": "America/New_York", "evidence": "纽约时间"},
        },
    }
    result = resolve_temporal(when, _ctx("纽约时间今天19点"))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["timezone"] == "America/New_York"


def test_dst_gap_is_invalid():
    ctx = ResolverContext(
        user_message="2026年3月8日2点30",
        reference_time=datetime(2026, 3, 1, 0, 0, tzinfo=NY),
        timezone="America/New_York",
    )
    when = {
        "raw_expression": "2026年3月8日2点30",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "absolute", "year": 2026, "month": 3, "day": 8, "evidence": "2026年3月8日"},
            "clock": {"surface_hour": 2, "minute": 30, "day_period": "am", "evidence": "2点30"},
        },
    }
    result = resolve_temporal(when, ctx)
    assert result.status is ResolutionStatus.INVALID


def test_dst_fold_is_ambiguous():
    ctx = ResolverContext(
        user_message="2026年11月1日1点30",
        reference_time=datetime(2026, 10, 1, 0, 0, tzinfo=NY),
        timezone="America/New_York",
    )
    when = {
        "raw_expression": "2026年11月1日1点30",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "absolute", "year": 2026, "month": 11, "day": 1, "evidence": "2026年11月1日"},
            "clock": {"surface_hour": 1, "minute": 30, "day_period": "am", "evidence": "1点30"},
        },
    }
    result = resolve_temporal(when, ctx)
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.unresolved_fields == ("dst_fold",)
    assert len(result.candidates) == 2


def test_ungrounded_raw_expression_is_invalid():
    when = {
        "raw_expression": "明天上午9点",
        "trigger_kind": "once",
        "constraints": {"clock": {"surface_hour": 9, "minute": 0, "day_period": "am"}},
    }
    result = resolve_temporal(when, _ctx("今天7点10分"))
    assert result.status is ResolutionStatus.INVALID


def test_evidence_span_must_belong_to_raw_expression():
    when = {
        "raw_expression": "7点10分",
        "trigger_kind": "once",
        "constraints": {"clock": {"surface_hour": 7, "minute": 10, "day_period": "pm", "evidence": "晚上"}},
    }
    result = resolve_temporal(when, _ctx("7点10分"))
    assert result.status is ResolutionStatus.INVALID


def test_unsupported_trigger_kind():
    result = resolve_temporal({"raw_expression": "农历初一", "trigger_kind": "lunar"}, _ctx("农历初一"))
    assert result.status is ResolutionStatus.UNSUPPORTED


def test_missing_clock_is_missing():
    when = {
        "raw_expression": "今天",
        "trigger_kind": "once",
        "constraints": {"date": {"kind": "relative_day", "offset": 0, "evidence": "今天"}},
    }
    result = resolve_temporal(when, _ctx("今天"))
    assert result.status is ResolutionStatus.MISSING
    assert result.unresolved_fields == ("clock",)
