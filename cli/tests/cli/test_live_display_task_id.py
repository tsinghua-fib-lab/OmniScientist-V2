"""Persistent task-id surfacing + the soft-timeout notice in the live display.

Point 3 of the timeout redesign: a turn must never be "which task was that?".
The owning task id is bound the moment it is acknowledged and then stays in the
transient status line for the whole turn (including a later timeout/degrade).
The soft foreground threshold renders a one-time, non-alarming "still working"
line rather than a warning.
"""

from __future__ import annotations

from omni.cli.live_display import TurnDisplay


def test_status_line_shows_the_owning_task_id() -> None:
    display = TurnDisplay(status_line=False)
    display.begin("planning")
    assert "task" not in display._status_text().plain  # none until acknowledged

    display.set_task("abcdef1234567890")
    status = display._status_text().plain
    assert "task abcdef12" in status  # short id, persistently in the status line


def test_set_task_is_idempotent_and_ignores_empty() -> None:
    display = TurnDisplay(status_line=False)
    display.begin("planning")
    display.set_task("")  # empty never binds
    assert "task" not in display._status_text().plain
    display.set_task("deadbeefcafef00d")
    display.set_task("deadbeefcafef00d")  # same id, no error / no duplicate
    assert display._status_text().plain.count("task ") == 1


def test_soft_timeout_notice_renders_once() -> None:
    display = TurnDisplay(status_line=False)
    display.begin("planning")
    display.set_task("abcdef1234567890")

    display.tool_event("notice", {"kind": "soft_timeout", "elapsed_s": 250.0, "soft_timeout_s": 240.0})
    assert display._soft_notified is True

    # A second soft-timeout notice is ignored (no repeated nagging).
    display.tool_event("notice", {"kind": "soft_timeout", "elapsed_s": 300.0})
    assert display._soft_notified is True


def test_usage_notice_appears_on_the_status_line() -> None:
    display = TurnDisplay(status_line=False)
    display.begin("planning")
    display.tool_event("notice", {"kind": "usage", "total_tokens": 12400, "cost_usd": 0.0123})
    status = display._status_text().plain
    assert "12.4k tok" in status
    assert "$0.0123" in status


def test_usage_warn_renders_once() -> None:
    display = TurnDisplay(status_line=False)
    display.begin("planning")
    display.tool_event(
        "notice",
        {"kind": "usage_warn", "total_tokens": 200000, "cost_usd": 0.51, "reason": "warn_total_tokens"},
    )
    display.tool_event(
        "notice",
        {"kind": "usage_warn", "total_tokens": 250000, "cost_usd": 0.60, "reason": "warn_total_tokens"},
    )
    assert display._usage_warned is True


def test_unknown_notice_kind_is_ignored() -> None:
    display = TurnDisplay(status_line=False)
    display.begin("planning")
    display.tool_event("notice", {"kind": "something_else"})
    assert display._soft_notified is False
