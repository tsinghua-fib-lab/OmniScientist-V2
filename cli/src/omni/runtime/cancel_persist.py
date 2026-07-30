"""Finish durable writes after Task.cancel() on Python 3.11+.

``Task.cancel()`` of a parent that is awaiting this task also cancels this
task (the parent's ``_fut_waiter``). ``uncancel()`` is not enough: the still-
cancelled parent cancels its waiter again at the next scheduling point, so a
write started with :func:`ignore_cancellation` can die mid-session.

:func:`run_uncancelled` runs the write on a sibling task that is not that
waiter, then waits until it finishes even if this task is re-cancelled.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, TypeVar

from sqlalchemy.exc import OperationalError

from omni.storage.db import busy_retry_budget, sqlite_busy

T = TypeVar("T")

_LOCKS: dict[int, asyncio.Lock] = {}


def persist_lock() -> asyncio.Lock:
    """One writer at a time per event loop.

    Windows SQLite keeps the aiosqlite worker lock after a cancelled task
    dies. Overlapping cancel persists then raise ``database is locked`` and
    replace ``CancelledError`` with a tool failure.
    """
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(id(loop))
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[id(loop)] = lock
    return lock


@asynccontextmanager
async def exclusive_persist() -> AsyncIterator[None]:
    """Serialize a cancel/terminal write with other persist helpers."""
    async with persist_lock():
        yield


def pause_cancellation() -> int:
    """Consume pending ``Task.cancel()`` requests. Return how many were held."""
    task = asyncio.current_task()
    if task is None:
        return 0
    held = 0
    while task.cancelling() > 0:
        task.uncancel()
        held += 1
    return held


def resume_cancellation(held: int) -> None:
    """Restore cancellation requests consumed by :func:`pause_cancellation`."""
    task = asyncio.current_task()
    if task is None:
        return
    for _ in range(held):
        task.cancel()


@contextmanager
def ignore_cancellation() -> Iterator[None]:
    """Run the ``with`` body even when the current task is already cancelled."""
    held = pause_cancellation()
    try:
        yield
    finally:
        resume_cancellation(held)


async def run_uncancelled(work: Callable[[], Awaitable[T]]) -> T:
    """Run ``work`` on a sibling task this task's re-cancels cannot abort."""

    async def locked() -> T:
        async with persist_lock():
            return await work()

    worker = asyncio.ensure_future(locked())
    try:
        while True:
            pause_cancellation()
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                if worker.done():
                    return worker.result()
    finally:
        if not worker.done():
            worker.cancel()
            pause_cancellation()
            await asyncio.gather(worker, return_exceptions=True)


async def persist_best_effort(work: Callable[[], Awaitable[T]]) -> T | None:
    """Cancel-path persist. A busy store must not replace ``CancelledError``.

    Nested ``retry_while_busy`` calls are capped: the parent turn settler
    retries with a full budget after this write gives up.
    """
    with busy_retry_budget(2):
        try:
            return await run_uncancelled(work)
        except OperationalError as exc:
            if sqlite_busy(exc):
                return None
            raise


def _cancelled_outcome(outcome: T, cancelled: T) -> bool:
    if outcome == cancelled:
        return True
    if isinstance(outcome, str):
        return outcome == "cancelled"
    return bool(isinstance(outcome, tuple) and outcome and outcome[0] == "cancelled")


async def complete_despite_cancel(
    execute: Callable[[], Awaitable[T]],
    finish: Callable[[T], Awaitable[Any]],
    cancelled: T,
) -> T:
    """Run ``execute`` then ``finish``; persist ``cancelled`` if the task dies."""
    finished = False
    try:
        outcome = await execute()
        persist = (
            persist_best_effort
            if _cancelled_outcome(outcome, cancelled)
            else run_uncancelled
        )
        await persist(lambda: finish(outcome))
        finished = True
        return outcome
    except asyncio.CancelledError:
        if not finished:
            await persist_best_effort(lambda: finish(cancelled))
        raise


__all__ = [
    "complete_despite_cancel",
    "exclusive_persist",
    "ignore_cancellation",
    "pause_cancellation",
    "persist_best_effort",
    "persist_lock",
    "resume_cancellation",
    "run_uncancelled",
]
