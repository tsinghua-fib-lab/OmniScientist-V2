"""Consumers for the durable-memory passes that session ends park.

Ending a session records that it owes consolidation and returns immediately (see
``TurnMemory.enqueue_session_maintenance``), because performing the pass costs
several model round trips and nobody leaving a session should pay for them. That
leaves someone else to pick the work up, and the two long-lived shapes need
different consumers:

* an interactive window drains once at startup, when the user is reading the
  banner rather than waiting on a prompt;
* a service drains on the runtime's own tick, rate-limited, since it may stay up
  across many sessions and never restart.

Both are best-effort and detached: a failed drain settles its own run and leaves
the rest of the queue for the next attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Long enough that an idle service is not polling the queue for no reason, short
# enough that a window left open for a day still consolidates what it parked.
_TICK_INTERVAL_S = 300.0

# One drain handles a handful of parked sessions; the rest wait for the next pass
# so a large backlog cannot monopolise a tick.
_DRAIN_LIMIT = 5


def maintenance_tick(
    agent,  # noqa: ANN001 - OmniAgent (imported here would close an import cycle)
    *,
    interval_s: float = _TICK_INTERVAL_S,
    limit: int = _DRAIN_LIMIT,
) -> Callable[[], Awaitable[int]]:
    """Build a runtime tick hook that drains parked maintenance periodically.

    The runtime ticks far more often than this work needs to run, so the hook
    keeps its own clock and returns immediately in between.
    """
    # ``None`` is "never drained", so the first tick always runs. A numeric zero
    # would be compared against ``time.monotonic()``, whose epoch is the boot
    # time: on a host up for less than ``interval_s`` — every fresh CI runner and
    # every freshly rebooted machine — the first tick reads as if it had just run
    # and the queue waits out a whole interval before anything drains it.
    state: dict[str, float | None] = {"last": None}

    async def tick() -> int:
        now = time.monotonic()
        last = state["last"]
        if last is not None and now - last < max(1.0, interval_s):
            return 0
        state["last"] = now
        try:
            return await agent.drain_pending_maintenance(limit=limit)
        except Exception:  # noqa: BLE001
            logger.debug("queued memory maintenance drain failed", exc_info=True)
            return 0

    return tick


def spawn_maintenance_drain(
    agent,  # noqa: ANN001 - OmniAgent (see above)
    *,
    limit: int = _DRAIN_LIMIT,
    delay_s: float = 1.0,
    interval_s: float = _TICK_INTERVAL_S,
) -> asyncio.Task | None:
    """Drain parked maintenance beside an interactive window. Never blocks it.

    Loops rather than running once, because ``/new`` and ``/clear`` park work too:
    a window left open all day would otherwise hand its whole backlog to the next
    launch. Returns the task so teardown can cancel it (and so tests can await
    it). Cancelling is safe — the pass in flight settles its own run.
    """

    async def drain() -> int:
        drained = 0
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(max(0.0, delay_s))
            while True:
                try:
                    drained += await agent.drain_pending_maintenance(limit=limit)
                except Exception:  # noqa: BLE001
                    logger.debug("memory maintenance drain failed", exc_info=True)
                await asyncio.sleep(max(1.0, interval_s))
        return drained

    try:
        return asyncio.get_running_loop().create_task(drain(), name="omni-memory-drain")
    except RuntimeError:
        return None
