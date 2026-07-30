"""Cross-process advisory lock for machine-global memory consolidation.

The global memory store (``~/.omni/memory.sqlite3``) is shared by every CLI,
workspace and the ``omni serve`` daemon on the machine. WAL + ``busy_timeout``
already make concurrent single-row writes safe; this lock additionally
serializes the heavier *read-modify-write* consolidation passes (decay + dedup,
profile rebuild, summary refresh) so two processes running session-end
maintenance at the same time don't do redundant/racing work.

The lock is **non-blocking with async backoff**: it never blocks the event loop
and never deadlocks two in-process consolidations. If it can't be acquired within
the timeout it yields ``False`` and the caller simply skips maintenance — another
process is already doing it, and the pass is best-effort anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

from omni.config.paths import OmniPaths

logger = logging.getLogger(__name__)

try:  # POSIX advisory locks; absent on Windows → degrade to a no-op lock.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - platform dependent
    _fcntl = None  # type: ignore[assignment]


@contextlib.asynccontextmanager
async def global_memory_lock(
    paths: OmniPaths, *, timeout_s: float = 5.0, poll_s: float = 0.05
) -> AsyncIterator[bool]:
    """Yield ``True`` if the global-memory consolidation lock was acquired.

    Yields ``False`` (without blocking) when another process holds it past the
    timeout so the caller can skip. Always releases on exit. A no-op that yields
    ``True`` when file locking is unavailable (Windows / locked-down FS).
    """
    if _fcntl is None:
        yield True
        return
    lock_path = paths.memories_dir / ".consolidate.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a", encoding="utf-8")
    except OSError:
        # Can't even open the lock file — don't block memory maintenance on it.
        yield True
        return
    acquired = False
    deadline = time.monotonic() + max(0.0, timeout_s)
    try:
        while True:
            try:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(poll_s)
        yield acquired
    finally:
        if acquired:
            try:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            except OSError:
                logger.debug("global memory lock release failed", exc_info=True)
        handle.close()
