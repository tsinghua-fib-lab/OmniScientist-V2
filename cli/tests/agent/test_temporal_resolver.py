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


def test_model_cannot_invent_pm_for_a_bare_hour():
    when = {
        "raw_expression": "今天7点10分",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {
                "surface_hour": 7,
                "minute": 10,
                "day_period": "pm",
                "evidence": "7点10分",
            },
        },
    }

    result = resolve_temporal(when, _ctx("今天7点10分"))

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.unresolved_fields == ("day_period",)


def test_unrelated_pm_text_cannot_ground_the_selected_clock():
    when = {
        "raw_expression": "今天在3pm会议后7点提醒我",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {
                "surface_hour": 7,
                "minute": 0,
                "day_period": "pm",
                "evidence": "7点",
            },
        },
    }

    result = resolve_temporal(when, _ctx("今天在3pm会议后7点提醒我"))

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.unresolved_fields == ("day_period",)


def test_model_cannot_replace_surface_seven_with_hour_nineteen():
    when = {
        "raw_expression": "今天7点10分",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {
                "surface_hour": 19,
                "minute": 10,
                "hour_system": "24",
                "evidence": "7点10分",
            },
        },
    }

    result = resolve_temporal(when, _ctx("今天7点10分"))

    assert result.status is ResolutionStatus.INVALID
    assert result.unresolved_fields == ("clock",)


def test_spelled_hour_must_match_the_proposed_surface_hour():
    when = {
        "raw_expression": "today at seven pm",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "today"},
            "clock": {
                "surface_hour": 6,
                "minute": 0,
                "day_period": "pm",
                "evidence": "seven pm",
            },
        },
    }

    result = resolve_temporal(when, _ctx("today at seven pm"))

    assert result.status is ResolutionStatus.INVALID
    assert result.unresolved_fields == ("clock",)


def test_minute_must_match_the_clock_evidence():
    when = {
        "raw_expression": "今天7点10分",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {
                "surface_hour": 7,
                "minute": 50,
                "day_period": None,
                "evidence": "7点10分",
            },
        },
    }

    result = resolve_temporal(when, _ctx("今天7点10分"))

    assert result.status is ResolutionStatus.INVALID
    assert result.unresolved_fields == ("clock",)


def test_clock_components_cannot_be_mixed_across_two_times():
    when = {
        "raw_expression": "3:50之后在7:10 pm提醒我",
        "trigger_kind": "once",
        "constraints": {
            "clock": {
                "surface_hour": 7,
                "minute": 50,
                "day_period": "pm",
                "evidence": "3:50之后在7:10 pm",
            },
        },
    }

    result = resolve_temporal(when, _ctx("3:50之后在7:10 pm提醒我"))

    assert result.status is ResolutionStatus.INVALID
    assert result.unresolved_fields == ("clock",)


def test_word_number_minutes_remain_supported():
    chinese = {
        "raw_expression": "今天下午七点十分",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {
                "surface_hour": 7,
                "minute": 10,
                "day_period": "pm",
                "evidence": "下午七点十分",
            },
        },
    }
    english = {
        "raw_expression": "today at seven thirty pm",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "today"},
            "clock": {
                "surface_hour": 7,
                "minute": 30,
                "day_period": "pm",
                "evidence": "seven thirty pm",
            },
        },
    }

    assert resolve_temporal(
        chinese, _ctx("今天下午七点十分")
    ).status is ResolutionStatus.RESOLVED
    assert resolve_temporal(
        english, _ctx("today at seven thirty pm")
    ).status is ResolutionStatus.RESOLVED


def test_model_cannot_invent_24_hour_system_for_a_bare_hour():
    when = {
        "raw_expression": "今天7点10分",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {
                "surface_hour": 7,
                "minute": 10,
                "hour_system": "24",
                "evidence": "7点10分",
            },
        },
    }

    result = resolve_temporal(when, _ctx("今天7点10分"))

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.unresolved_fields == ("day_period",)


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


def test_noon_and_midnight_are_explicit_periods():
    noon = {
        "raw_expression": "today at noon",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "today"},
            "clock": {
                "surface_hour": 12,
                "minute": 0,
                "day_period": "pm",
                "evidence": "noon",
            },
        },
    }
    midnight = {
        "raw_expression": "tomorrow at midnight",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 1, "evidence": "tomorrow"},
            "clock": {
                "surface_hour": 12,
                "minute": 0,
                "day_period": "am",
                "evidence": "midnight",
            },
        },
    }

    noon_result = resolve_temporal(noon, _ctx("today at noon"))
    midnight_result = resolve_temporal(midnight, _ctx("tomorrow at midnight"))

    assert noon_result.status is ResolutionStatus.RESOLVED
    assert noon_result.value["at"].endswith("12:00:00+08:00")
    assert midnight_result.status is ResolutionStatus.RESOLVED
    assert midnight_result.value["at"].endswith("00:00:00+08:00")


def test_grounded_period_span_preserves_multilingual_semantic_extraction():
    when = {
        "raw_expression": "hoy a las 7 de la tarde",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "hoy"},
            "clock": {
                "surface_hour": 7,
                "minute": 0,
                "day_period": "pm",
                "day_period_evidence": "de la tarde",
                "evidence": "7 de la tarde",
            },
        },
    }

    result = resolve_temporal(when, _ctx("hoy a las 7 de la tarde"))

    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["at"].endswith("19:00:00+08:00")


def test_ungrounded_multilingual_period_span_is_rejected():
    when = {
        "raw_expression": "hoy a las 7",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "hoy"},
            "clock": {
                "surface_hour": 7,
                "minute": 0,
                "day_period": "pm",
                "day_period_evidence": "de la tarde",
                "evidence": "7",
            },
        },
    }

    result = resolve_temporal(when, _ctx("hoy a las 7"))

    assert result.status is ResolutionStatus.INVALID
    assert result.unresolved_fields == ("clock",)


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


def test_bare_small_hour_does_not_use_an_ungrounded_24_hour_hint():
    when = {
        "raw_expression": "6点",
        "trigger_kind": "once",
        "constraints": {"clock": {"surface_hour": 6, "minute": 0, "hour_system": "24", "evidence": "6点"}},
    }
    result = resolve_temporal(when, _ctx("6点提醒我"))
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.unresolved_fields == ("day_period",)


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
        user_message="2026年3月8日凌晨2点30",
        reference_time=datetime(2026, 3, 1, 0, 0, tzinfo=NY),
        timezone="America/New_York",
    )
    when = {
        "raw_expression": "2026年3月8日凌晨2点30",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "absolute", "year": 2026, "month": 3, "day": 8, "evidence": "2026年3月8日"},
            "clock": {
                "surface_hour": 2,
                "minute": 30,
                "day_period": "am",
                "evidence": "凌晨2点30",
            },
        },
    }
    result = resolve_temporal(when, ctx)
    assert result.status is ResolutionStatus.INVALID


def test_dst_fold_is_ambiguous():
    ctx = ResolverContext(
        user_message="2026年11月1日凌晨1点30",
        reference_time=datetime(2026, 10, 1, 0, 0, tzinfo=NY),
        timezone="America/New_York",
    )
    when = {
        "raw_expression": "2026年11月1日凌晨1点30",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "absolute", "year": 2026, "month": 11, "day": 1, "evidence": "2026年11月1日"},
            "clock": {
                "surface_hour": 1,
                "minute": 30,
                "day_period": "am",
                "evidence": "凌晨1点30",
            },
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
    assert result.unresolved_fields == ("clock",)


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


# ── the grounding ladder: the model's quote first, the user's wording next ────
#
# Run 82322c46 asked for "今晚21点12分" and was told to restate the time. The model
# had read it correctly (surface_hour 21, minute 12) but quoted only "21点", so the
# minute counted as ungrounded. The same user said "今晚21:13" minutes later and it
# worked first try — not because the resolver understands colons better, but
# because a colon time is hard to quote by halves. These pin the repair without
# loosening what the narrow quote is actually for.

_TONIGHT = {"kind": "relative_day", "offset": 0, "evidence": "今晚"}


def test_a_minute_the_user_stated_is_not_refused_for_a_short_quote():
    """82322c46 verbatim: evidence "21点" while the user wrote "今晚21点12分"."""
    when = {
        "raw_expression": "今晚21点12分",
        "trigger_kind": "once",
        "constraints": {
            "date": _TONIGHT,
            "clock": {
                "surface_hour": 21,
                "minute": 12,
                "day_period": "pm",
                "hour_system": 24,
                "evidence": "21点",
            },
        },
    }

    result = resolve_temporal(when, _ctx("帮我今晚21点12分开始执行", now=datetime(2026, 8, 12, 13, 0, tzinfo=SH)))

    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["at"].endswith("21:12:00+08:00")


def test_a_clock_with_no_quote_at_all_falls_back_to_the_users_wording():
    """c975ac22: the model omitted ``evidence`` entirely and lost a correct 19:18."""
    when = {
        "raw_expression": "今晚19点18",
        "trigger_kind": "once",
        "constraints": {
            "date": _TONIGHT,
            "clock": {"surface_hour": 19, "minute": 18, "day_period": None, "hour_system": 24},
        },
    }

    result = resolve_temporal(when, _ctx("今晚19点18执行", now=datetime(2026, 8, 12, 13, 0, tzinfo=SH)))

    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["at"].endswith("19:18:00+08:00")


def test_a_minute_in_neither_the_quote_nor_the_wording_is_still_refused():
    """The ladder's rungs are the model's quote and the *user's* words — never the
    model's imagination. Widening must not turn the grounding check off."""
    when = {
        "raw_expression": "今晚21点12分",
        "trigger_kind": "once",
        "constraints": {
            "date": _TONIGHT,
            "clock": {"surface_hour": 21, "minute": 59, "hour_system": 24, "evidence": "21点"},
        },
    }

    result = resolve_temporal(when, _ctx("帮我今晚21点12分开始执行"))

    assert result.status is ResolutionStatus.INVALID
    assert result.unresolved_fields == ("clock",)
    assert "59" in result.reason


def test_a_narrow_quote_still_picks_one_time_out_of_a_sentence_with_several():
    """Why the wide rung is a fallback and not the primary: with two times in the
    sentence, the quote is the only thing that says which one is meant."""
    when = {
        "raw_expression": "3点50的会结束后晚7点10分提醒我",
        "trigger_kind": "once",
        "constraints": {
            "clock": {
                "surface_hour": 7,
                "minute": 10,
                "day_period": "pm",
                "day_period_evidence": "晚",
                "evidence": "晚7点10分",
            },
        },
    }

    result = resolve_temporal(when, _ctx("3点50的会结束后晚7点10分提醒我"))

    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["at"].endswith("19:10:00+08:00")


def test_two_times_and_no_quote_is_refused_rather_than_guessed():
    """The mirror image: nothing narrows the sentence, so the host does not choose."""
    when = {
        "raw_expression": "3点50的会结束后7点10分提醒我",
        "trigger_kind": "once",
        "constraints": {"clock": {"surface_hour": 7, "minute": 10, "day_period": "pm"}},
    }

    result = resolve_temporal(when, _ctx("3点50的会结束后7点10分提醒我"))

    assert result.status is ResolutionStatus.INVALID
    assert result.unresolved_fields == ("clock",)


def test_a_conventional_particle_in_the_quote_does_not_invent_a_new_time():
    """2367d610: the model added 分; the user wrote 今天17点03 with no 分.

    Orthography of the quote is not the grounded object — the hour and minute
    the extractors already see in the user text are.
    """
    when = {
        "raw_expression": "今天17点03分",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 0, "evidence": "今天"},
            "clock": {
                "surface_hour": 17,
                "minute": 3,
                "day_period": None,
                "hour_system": 24,
                "evidence": "17点03分",
            },
        },
    }

    result = resolve_temporal(
        when,
        _ctx("帮我改成今天17点03开始执行", now=datetime(2026, 8, 14, 16, 49, tzinfo=SH)),
    )

    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["at"].endswith("17:03:00+08:00")


def test_an_invented_date_word_in_the_quote_is_still_refused():
    """The third rung attests values, it does not let 明天 replace 今天."""
    when = {
        "raw_expression": "明天上午9点",
        "trigger_kind": "once",
        "constraints": {
            "date": {"kind": "relative_day", "offset": 1, "evidence": "明天"},
            "clock": {"surface_hour": 9, "minute": 0, "day_period": "am", "evidence": "9点"},
        },
    }

    result = resolve_temporal(when, _ctx("今天7点10分"))

    assert result.status is ResolutionStatus.INVALID
    assert "date" in result.unresolved_fields or "clock" in result.unresolved_fields


def test_an_evening_word_the_model_quoted_resolves_the_period():
    """今晚 says a part of the day exactly as 晚上 does; only the spelling differed."""
    when = {
        "raw_expression": "今晚7点",
        "trigger_kind": "once",
        "constraints": {
            "clock": {
                "surface_hour": 7,
                "minute": 0,
                "day_period": "pm",
                "evidence": "今晚7点",
            },
        },
    }

    result = resolve_temporal(when, _ctx("今晚7点提醒我", now=datetime(2026, 8, 12, 13, 0, tzinfo=SH)))

    assert result.status is ResolutionStatus.RESOLVED
    assert result.value["at"].endswith("19:00:00+08:00")
