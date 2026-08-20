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
from contextvars import ContextVar, Token
from typing import Any, TypeVar

from sqlalchemy.exc import OperationalError

from omni.storage.db import busy_retry_budget, sqlite_busy

T = TypeVar("T")


class _PersistLock:
    """Reentrant per-task lock so finish_task can run inside persist_best_effort."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: object | None = None
        self._depth = 0

    def locked(self) -> bool:
        return self._lock.locked()

    def held_by_other(self) -> bool:
        return self._lock.locked() and self._owner is not asyncio.current_task()

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if self._owner is not None and self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        task = asyncio.current_task()
        if self._owner is not task:
            raise RuntimeError("persist lock released by a non-owner")
        self._depth -= 1
        if self._depth > 0:
            return
        self._owner = None
        self._lock.release()

    async def __aenter__(self) -> _PersistLock:
        await self.acquire()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.release()


_LOCKS: dict[tuple[int, int], _PersistLock] = {}
_PERSIST_TOKEN: ContextVar[object | None] = ContextVar("omni_persist_token", default=None)


def persist_lock(token: object | None = None) -> _PersistLock:
    """One writer at a time per event loop and persist token.

    ``token`` should be the store being written (the ``Database``). Two
    isolated workspaces on the same loop must not share a lock: dropping
    ``react.finished`` because another black-box attempt is finishing is
    how Windows release cells lose half the self-knowledge repeats.

    ``token is None`` stays the loop-wide cancel lock used by skill-side
    ``persist_best_effort``. Windows SQLite keeps the aiosqlite worker lock
    after a cancelled task dies; overlapping cancel persists then raise
    ``database is locked`` and replace ``CancelledError`` with a tool failure.

    The lock is reentrant for the current task: ``persist_best_effort`` already
    holds it when a cancelled skill refreshes the parent and ``finish_task``
    queues again.
    """
    loop = asyncio.get_running_loop()
    resolved = token if token is not None else _PERSIST_TOKEN.get()
    key = (id(loop), id(resolved) if resolved is not None else 0)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = _PersistLock()
        _LOCKS[key] = lock
    return lock


@asynccontextmanager
async def exclusive_persist(token: object | None = None) -> AsyncIterator[None]:
    """Serialize a cancel/terminal write with other persist helpers."""
    async with persist_lock(token):
        yield


@asynccontextmanager
async def persist_scope(token: object) -> AsyncIterator[None]:
    """Bind skill-side persist helpers to this store for the current task.

    ``persist_best_effort`` takes no database argument. Isolated attempts on
    the same loop must still serialize against *their* store, not a neighbor's.
    """
    reset: Token[object | None] = _PERSIST_TOKEN.set(token)
    try:
        yield
    finally:
        _PERSIST_TOKEN.reset(reset)


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


async def run_uncancelled(
    work: Callable[[], Awaitable[T]],
    *,
    serialize: bool = True,
) -> T:
    """Run ``work`` on a sibling task this task's re-cancels cannot abort.

    ``serialize=False`` still outlives ``Task.cancel()`` but does not hold
    :func:`persist_lock` for the whole body. Parent settle uses that so each
    busy retry can release the writer lock for the cancelled skill.
    """

    async def body() -> T:
        if serialize:
            async with persist_lock():
                return await work()
        return await work()

    worker = asyncio.ensure_future(body())
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
    "persist_scope",
    "resume_cancellation",
    "run_uncancelled",
]
