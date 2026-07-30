"""Pausable turn clock: deadline math + ref-counted pause + context registry.

These pin the mechanism behind the approval-vs-timeout fix: a turn's wall clock
is *paused* for exactly the duration of an approval wait, so human thinking time
never counts against ``react.max_seconds``. Overlapping/parallel approvals must
credit the real elapsed time exactly once (ref-counted), and a pause must reach
every clock currently in scope (nested loops + parallel branches).
"""

from __future__ import annotations

import pytest

from omni.core import turn_clock
from omni.core.turn_clock import (
    TurnClock,
    active_clocks,
    pause_clocks,
    register_clock,
)


class _FakeMonotonic:
    """A hand-advanced monotonic source so the tests are time-deterministic."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def fake_time(monkeypatch) -> _FakeMonotonic:
    clock = _FakeMonotonic()
    monkeypatch.setattr(turn_clock.time, "monotonic", clock)
    return clock


def test_remaining_and_expired_track_elapsed(fake_time):
    clock = TurnClock(10.0)
    assert clock.remaining() == pytest.approx(10.0)
    assert not clock.expired()
    fake_time.advance(6.0)
    assert clock.remaining() == pytest.approx(4.0)
    assert not clock.expired()
    fake_time.advance(4.0)
    assert clock.expired()


def test_pause_credits_wall_time_and_is_stable_while_paused(fake_time):
    clock = TurnClock(5.0)
    fake_time.advance(2.0)  # 3s left
    clock.pause_enter()
    fake_time.advance(100.0)  # a long human decision
    # While paused, remaining is stable (the ongoing pause is credited)...
    assert clock.remaining() == pytest.approx(3.0)
    assert not clock.expired()
    clock.pause_exit()
    # ...and after resuming, the deadline absorbed the full 100s wait.
    assert clock.remaining() == pytest.approx(3.0)
    fake_time.advance(3.0)
    assert clock.expired()


def test_overlapping_pauses_credit_wall_time_once(fake_time):
    """Two concurrent approvals (parallel branches) must not double-extend."""
    clock = TurnClock(5.0)
    clock.pause_enter()  # branch A starts pausing at t0
    fake_time.advance(2.0)
    clock.pause_enter()  # branch B overlaps
    fake_time.advance(3.0)
    clock.pause_exit()  # A resumes (still paused by B)
    fake_time.advance(4.0)
    clock.pause_exit()  # B resumes at t0+9 -> extend by the 9s union, once
    assert clock.remaining() == pytest.approx(5.0)


def test_pause_exit_without_enter_is_noop(fake_time):
    clock = TurnClock(5.0)
    clock.pause_exit()  # unbalanced exit must not move the deadline
    assert clock.remaining() == pytest.approx(5.0)


def test_register_stacks_and_pause_clocks_extends_every_in_scope_clock(fake_time):
    outer = TurnClock(5.0)
    inner = TurnClock(5.0)
    assert active_clocks() == ()
    with register_clock(outer):
        assert active_clocks() == (outer,)
        with register_clock(inner):  # nested loop pushes, does not replace
            assert active_clocks() == (outer, inner)
            with pause_clocks():
                fake_time.advance(10.0)  # approval wait credited to BOTH
            assert outer.remaining() == pytest.approx(5.0)
            assert inner.remaining() == pytest.approx(5.0)
        assert active_clocks() == (outer,)
    assert active_clocks() == ()


def test_pause_clocks_is_noop_without_registered_clocks(fake_time):
    # No clock in scope (e.g. a direct skill exec) -> pausing is harmless.
    with pause_clocks():
        fake_time.advance(10.0)
    # Nothing to assert beyond "did not raise"; a fresh clock is unaffected.
    assert TurnClock(1.0).remaining() == pytest.approx(1.0)


def test_pause_clocks_credits_partial_wait_on_error(fake_time):
    clock = TurnClock(5.0)
    with pytest.raises(RuntimeError), register_clock(clock):
        with pause_clocks():
            fake_time.advance(3.0)
            raise RuntimeError("approver cancelled")
    # The 3s spent before the error is still credited (finally -> pause_exit).
    assert clock.remaining() == pytest.approx(5.0)
