"""Local-timezone display + Codex-style timezone context.

Persistence is UTC; everything shown to a human *or the model* is converted to
the process-local zone with an explicit offset. These tests pin that a stored
UTC timestamp reaches the model/system-prompt as local wall-clock with an
offset (so it no longer renders 8 hours behind under +08:00), and that the
system prompt carries a ``Timezone`` line like Codex's ``<timezone>``.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from omni.core.system_prompt import build_system_prompt
from omni.core.timefmt import (
    coerce_datetime,
    ensure_aware,
    format_local_iso,
    local_time_context,
    local_timezone_name,
)
from omni.skills_runtime.builtin_tools.recall import _task_payload
from omni.storage.models import TaskORM


@contextmanager
def _tz(name: str):
    """Pin the process-local timezone for a deterministic assertion (POSIX)."""
    if not hasattr(time, "tzset"):
        pytest.skip("process-local TZ switching requires POSIX time.tzset")
    old = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_coerce_and_ensure_aware():
    assert coerce_datetime(None) is None
    assert coerce_datetime("") is None
    assert coerce_datetime("not-a-date") is None
    # Naive is interpreted as UTC (SQLite drops tzinfo on read).
    assert ensure_aware(datetime(2026, 7, 24, 7, 13, 24)).tzinfo == UTC
    parsed = coerce_datetime("2026-07-24T07:13:24Z")
    assert parsed is not None and parsed.tzinfo == UTC and parsed.hour == 7


def test_format_local_iso_carries_offset_under_plus8():
    with _tz("Asia/Shanghai"):
        aware = format_local_iso(datetime(2026, 7, 24, 7, 13, 24, tzinfo=UTC))
        naive = format_local_iso(datetime(2026, 7, 24, 7, 13, 24))  # read-back naive UTC
    assert aware == "2026-07-24T15:13:24+08:00"
    assert naive == "2026-07-24T15:13:24+08:00"
    assert format_local_iso(None) == ""


def test_local_time_context_shanghai():
    now = datetime(2026, 7, 24, 7, 13, 0, tzinfo=UTC)
    with _tz("Asia/Shanghai"):
        ctx = local_time_context(now)
        assert local_timezone_name() == "Asia/Shanghai"
    assert ctx.offset == "+08:00"
    assert ctx.name == "Asia/Shanghai"
    assert ctx.current_date == "2026-07-24"
    assert ctx.timezone == "Asia/Shanghai (+08:00)"
    assert ctx.now.hour == 15


def test_local_time_context_negative_offset():
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    with _tz("America/New_York"):
        ctx = local_time_context(now)
    assert ctx.name == "America/New_York"
    assert ctx.offset.startswith("-")  # -04:00 (DST) or -05:00
    assert ctx.timezone.startswith("America/New_York (-")


def test_system_prompt_injects_local_time_and_timezone():
    now = datetime(2026, 7, 24, 7, 13, 0, tzinfo=UTC)
    with _tz("Asia/Shanghai"):
        prompt = build_system_prompt(role="R", tools=[], project_name="proj", now=now)
    assert "Timezone: Asia/Shanghai (+08:00)" in prompt
    assert "Current time: 2026-07-24 15:13 +08:00" in prompt


def test_recall_task_payload_localizes_created_at():
    run = TaskORM(
        id="abcd1234ef",
        status="succeeded",
        title="RAG survey",
        created_at=datetime(2026, 7, 24, 7, 13, 24, tzinfo=UTC),
    )
    with _tz("Asia/Shanghai"):
        payload = _task_payload(run)
    assert payload["created_at"] == "2026-07-24T15:13:24+08:00"
