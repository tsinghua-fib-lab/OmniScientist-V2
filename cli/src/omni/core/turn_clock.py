"""Pausable wall-clock budget for one agent turn (and workflow envelope).

A ReAct turn runs under an absolute wall-clock deadline (``react.max_seconds``).
Human approval latency must not count against that budget: Codex treats approval
as a gate *before* the timed section, and explicitly extends interactive
deadlines by the paused duration. This module gives Omni the same property.

The deadline lives on a :class:`TurnClock`. The clock can be *paused* for the
exact duration of any approval wait from deep inside the tool-dispatch stack —
the approval gate has no direct reference to the loop's clock, so the clocks
currently in scope are published on a context-local registry. Because
``asyncio.create_task`` copies the current context, parallel tool branches share
the same clock objects (extensions propagate), and nested loops push their own
clock so a pause credits the whole ancestor stack exactly once.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from contextvars import ContextVar

__all__ = ["TurnClock", "register_clock", "pause_clocks", "active_clocks"]


class TurnClock:
    """A monotonic wall-clock deadline that can be paused (ref-counted).

    ``pause_enter``/``pause_exit`` are ref-counted so overlapping approvals (for
    example two parallel tool branches both awaiting the owner) credit the real
    wall time exactly once: the deadline is extended by ``now - earliest_start``
    only when the outermost pause for this clock ends.
    """

    __slots__ = ("_deadline", "_pause_depth", "_paused_at")

    def __init__(self, max_seconds: float, *, now: float | None = None) -> None:
        base = time.monotonic() if now is None else float(now)
        self._deadline = base + max(0.0, float(max_seconds))
        self._pause_depth = 0
        self._paused_at = 0.0

    @property
    def deadline(self) -> float:
        """Current absolute monotonic deadline (moves out as the clock is paused)."""
        return self._deadline

    def remaining(self) -> float:
        """Seconds left before the deadline, crediting any in-progress pause."""
        rem = self._deadline - time.monotonic()
        if self._pause_depth:
            rem += time.monotonic() - self._paused_at
        return rem

    def expired(self) -> bool:
        """True once the (pause-adjusted) budget is exhausted."""
        return self.remaining() <= 0.0

    def extend(self, seconds: float) -> None:
        """Push the deadline out by ``seconds`` (ignored when non-positive)."""
        if seconds > 0:
            self._deadline += seconds

    def pause_enter(self) -> None:
        if self._pause_depth == 0:
            self._paused_at = time.monotonic()
        self._pause_depth += 1

    def pause_exit(self) -> None:
        if self._pause_depth == 0:
            return
        self._pause_depth -= 1
        if self._pause_depth == 0:
            self.extend(time.monotonic() - self._paused_at)
            self._paused_at = 0.0


_active_clocks: ContextVar[tuple[TurnClock, ...]] = ContextVar(
    "omni_active_turn_clocks", default=()
)


def active_clocks() -> tuple[TurnClock, ...]:
    """The turn clocks currently in scope for this execution context."""
    return _active_clocks.get()


@contextlib.contextmanager
def register_clock(clock: TurnClock) -> Iterator[TurnClock]:
    """Publish ``clock`` on the context-local registry for its lifetime.

    Nested loops stack: an inner ``register_clock`` adds to (does not replace)
    the outer clocks, so a pause credits every ancestor deadline.
    """
    token = _active_clocks.set(_active_clocks.get() + (clock,))
    try:
        yield clock
    finally:
        _active_clocks.reset(token)


@contextlib.contextmanager
def pause_clocks() -> Iterator[None]:
    """Pause every in-scope turn clock across the wrapped (awaited) block.

    Usable as ``with pause_clocks(): decision = await approver(req)`` — the
    clocks resume and absorb the elapsed wall time when the block exits, even on
    error/cancellation (the wait so far is still credited).
    """
    clocks = _active_clocks.get()
    for clock in clocks:
        clock.pause_enter()
    try:
        yield
    finally:
        for clock in clocks:
            clock.pause_exit()
