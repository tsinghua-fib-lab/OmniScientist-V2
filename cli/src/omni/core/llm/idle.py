"""Idle-on-activity watchdog for one model call.

Codex bounds a stream by silence between SSE events, not by how long the call
has been open. A productive paper draft can stream for many minutes; a hung
provider goes quiet. This module is the ReAct-side counterpart of the skill
executor's ``_await_skill_call`` / ``_ProgressHeartbeat``: wait in slices, reset
the idle clock on activity, and only then raise.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

T = TypeVar("T")

ActivityHook = Callable[[], Any]


class StreamIdleTimeout(TimeoutError):
    """The model call produced no events for longer than the idle window."""


class IdleWatchdog:
    """Records when a model call last made observable progress."""

    __slots__ = ("last",)

    def __init__(self) -> None:
        self.last = time.monotonic()

    def tick(self) -> None:
        self.last = time.monotonic()


async def emit_activity(hook: ActivityHook | None) -> None:
    """Run a possibly-async activity hook."""
    if hook is None:
        return
    result = hook()
    if inspect.isawaitable(result):
        await result


def provider_http_timeout(*, connect_s: float, idle_s: float) -> httpx.Timeout:
    """Split connect/write/pool from the stream idle (read) timeout.

    A single float used to apply to every httpx phase, so a long *active* SSE
    stream died at ``model.request_timeout_s`` even while tokens were arriving.
    Read is the idle window (Codex stream idle); connect stays the request
    timeout so a dead endpoint still fails fast.
    """
    connect = max(0.001, float(connect_s))
    read = max(0.001, float(idle_s) if idle_s > 0 else connect)
    return httpx.Timeout(connect=connect, read=read, write=connect, pool=connect)


async def await_attempt(
    factory: Callable[[ActivityHook | None], Awaitable[T]],
    *,
    idle_s: float,
    on_activity: ActivityHook | None = None,
    wall_s: float = 86_400.0,
) -> T:
    """Run one LLM attempt, bounding *silence* so the wrapper can reconnect.

    ReAct's outer wait is the turn wall clock. This is the per-attempt idle
    window: a quiet stream fails here, the retrying client reconnects, and a
    productive stream that keeps ticking is never cancelled.
    """
    if idle_s <= 0:
        return await factory(on_activity)
    watchdog = IdleWatchdog()

    def composed() -> Any:
        watchdog.tick()
        return on_activity() if on_activity is not None else None

    return await await_with_idle(
        factory(composed),
        stall_s=idle_s,
        deadline=time.monotonic() + max(1.0, wall_s),
        watchdog=watchdog,
    )


async def await_with_idle(
    coro: Awaitable[T],
    *,
    stall_s: float,
    deadline: float,
    watchdog: IdleWatchdog,
) -> T:
    """Wait until ``coro`` finishes, the wall-clock deadline, or idle silence.

    ``deadline`` is an absolute ``time.monotonic()`` instant (the turn ceiling).
    ``stall_s <= 0`` disables the idle window and only the wall clock remains.
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError
            if stall_s > 0 and now >= watchdog.last + stall_s:
                raise StreamIdleTimeout
            slice_end = deadline
            if stall_s > 0:
                slice_end = min(slice_end, watchdog.last + stall_s)
            done, _ = await asyncio.wait({task}, timeout=max(0.0, slice_end - now))
            if done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


__all__ = [
    "ActivityHook",
    "IdleWatchdog",
    "StreamIdleTimeout",
    "await_attempt",
    "await_with_idle",
    "emit_activity",
    "provider_http_timeout",
]
