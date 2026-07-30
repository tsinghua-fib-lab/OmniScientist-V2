"""Cron / scheduled jobs (P2): the cron engine and the Scheduler firing loop."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.runtime.scheduler import cron_matches, next_cron_fire, parse_cron


@pytest.fixture
def local_tz():
    """Pin the process' local timezone so ``next_cron_fire`` is deterministic.

    ``next_cron_fire`` reads cron fields in the *operator's* local wall-clock
    (``0 18 * * *`` = 18:00 local), so its output depends on the host zone. Tests
    set a fixed zone via ``TZ`` + ``time.tzset()`` and restore it on teardown,
    keeping the suite reproducible regardless of the machine it runs on.
    """
    original = os.environ.get("TZ")

    def _set(tz: str) -> None:
        os.environ["TZ"] = tz
        time.tzset()

    yield _set
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


# ── cron engine ──

def test_cron_matches_daily_at_nine():
    assert cron_matches("0 9 * * *", datetime(2026, 7, 10, 9, 0, tzinfo=UTC))
    assert not cron_matches("0 9 * * *", datetime(2026, 7, 10, 9, 1, tzinfo=UTC))
    assert not cron_matches("0 9 * * *", datetime(2026, 7, 10, 10, 0, tzinfo=UTC))


def test_cron_step_and_list_fields():
    # every 15 minutes
    assert cron_matches("*/15 * * * *", datetime(2026, 7, 10, 8, 30, tzinfo=UTC))
    assert not cron_matches("*/15 * * * *", datetime(2026, 7, 10, 8, 31, tzinfo=UTC))
    # explicit list
    assert cron_matches("0 8,12,18 * * *", datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
    assert not cron_matches("0 8,12,18 * * *", datetime(2026, 7, 10, 13, 0, tzinfo=UTC))


def test_cron_day_of_week_monday():
    # 2026-07-13 is a Monday; cron dow Monday == 1.
    assert cron_matches("0 9 * * 1", datetime(2026, 7, 13, 9, 0, tzinfo=UTC))
    assert not cron_matches("0 9 * * 1", datetime(2026, 7, 14, 9, 0, tzinfo=UTC))  # Tuesday


def test_cron_dom_or_dow_rule():
    # "1st of the month OR a Monday" — both restricted → OR semantics.
    expr = "0 0 1 * 1"
    assert cron_matches(expr, datetime(2026, 7, 1, 0, 0, tzinfo=UTC))   # 1st (Wed)
    assert cron_matches(expr, datetime(2026, 7, 13, 0, 0, tzinfo=UTC))  # Monday
    assert not cron_matches(expr, datetime(2026, 7, 14, 0, 0, tzinfo=UTC))  # neither


def test_next_cron_fire_is_strictly_after(local_tz):
    local_tz("UTC")  # in UTC the local wall-clock equals the stored UTC instant
    base = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
    nxt = next_cron_fire("0 9 * * *", base)
    assert nxt == datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def test_next_cron_fire_reads_cron_in_local_time(local_tz):
    """``0 18 * * *`` means 18:00 *local* — in UTC+8 that is 10:00 UTC.

    This is what "every day at 6pm" means to the operator and what ``omni
    schedule`` prints; the firing instant is still returned as aware UTC.
    """
    local_tz("Asia/Shanghai")  # fixed UTC+8, no DST
    base = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)  # 08:00 local
    nxt = next_cron_fire("0 18 * * *", base)
    assert nxt == datetime(2026, 7, 10, 10, 0, tzinfo=UTC)


def test_parse_cron_rejects_bad_expressions():
    with pytest.raises(ValueError):
        parse_cron("0 9 * *")  # only 4 fields
    with pytest.raises(ValueError):
        parse_cron("99 9 * * *")  # minute out of range → matches nothing


# ── Scheduler firing ──

async def _tasks_for(agent: OmniAgent, skill: str) -> int:
    tasks = await agent.runtime.list_subtasks(limit=100)
    return sum(1 for t in tasks if t.skill_name == skill)


@pytest.mark.asyncio
async def test_interval_schedule_fires_when_due_and_rearms():
    agent = await OmniAgent.create(load_settings())
    try:
        now = datetime.now(UTC)
        sid = await agent.scheduler.add(
            "cron-fixture", {"input": "go"}, kind="interval", interval_s=3600,
            first_due=now - timedelta(seconds=1),
        )
        fired = await agent.scheduler.run_due(now=now)
        assert len(fired) == 1
        assert await _tasks_for(agent, "cron-fixture") == 1

        sched = await agent.scheduler.get(sid)
        assert sched is not None
        assert sched.enabled is True
        assert sched.run_count == 1
        assert sched.next_due_at is not None
        # DB timestamps come back tz-naive; normalise before comparing (UTC).
        next_due = sched.next_due_at
        if next_due.tzinfo is None:
            next_due = next_due.replace(tzinfo=UTC)
        assert next_due > now
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_once_schedule_fires_then_disables():
    agent = await OmniAgent.create(load_settings())
    try:
        now = datetime.now(UTC)
        sid = await agent.scheduler.add(
            "once-fixture", kind="once", first_due=now - timedelta(seconds=1),
        )
        fired = await agent.scheduler.run_due(now=now)
        assert len(fired) == 1
        sched = await agent.scheduler.get(sid)
        assert sched is not None
        assert sched.enabled is False
        assert sched.next_due_at is None
        # A second tick fires nothing (already disabled).
        assert await agent.scheduler.run_due(now=now + timedelta(hours=2)) == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_not_due_schedule_does_not_fire():
    agent = await OmniAgent.create(load_settings())
    try:
        now = datetime.now(UTC)
        await agent.scheduler.add(
            "future-fixture", kind="interval", interval_s=3600,
            first_due=now + timedelta(hours=1),
        )
        assert await agent.scheduler.run_due(now=now) == []
        assert await _tasks_for(agent, "future-fixture") == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_disable_enable_and_remove():
    agent = await OmniAgent.create(load_settings())
    try:
        now = datetime.now(UTC)
        sid = await agent.scheduler.add(
            "toggle-fixture", kind="interval", interval_s=3600,
            first_due=now - timedelta(seconds=1),
        )
        assert await agent.scheduler.set_enabled(sid[:8], False) is True
        assert await agent.scheduler.run_due(now=now) == []  # disabled → no fire

        assert await agent.scheduler.set_enabled(sid[:8], True) is True
        assert len(await agent.scheduler.run_due(now=now)) == 1

        assert await agent.scheduler.remove(sid[:8]) is True
        assert await agent.scheduler.get(sid) is None
        assert await agent.scheduler.remove("nope") is False
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_feature_disabled_never_fires(monkeypatch):
    settings = load_settings()
    settings.schedules.enabled = False
    agent = await OmniAgent.create(settings)
    try:
        now = datetime.now(UTC)
        await agent.scheduler.add(
            "off-fixture", kind="interval", interval_s=3600,
            first_due=now - timedelta(seconds=1),
        )
        assert await agent.scheduler.run_due(now=now) == []
    finally:
        await agent.aclose()
