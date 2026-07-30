"""Home-level single-owner locks for IM channels.

Channel credentials live under the Omni *home* (``~/.omni/channels``), not under a
workspace, so a given bot account must be polled by exactly one ``omni serve``
daemon machine-wide. Two daemons (one per project, or a stray ghost workspace)
polling the same WeChat/Feishu bot fight over the session — WeChat returns
``errcode -14`` and replies can duplicate.

A per-channel lock file (``~/.omni/channels/<name>.lock``) records the owning pid
so only the first daemon binds the channel; the rest degrade that channel to
task-only. Locks are advisory and self-healing: a lock whose pid is dead is
considered stale and may be taken over.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from omni.runtime.daemon import pid_alive

__all__ = ["ChannelLock", "acquire", "lock_owner", "read_lock", "release"]


@dataclass
class ChannelLock:
    """A held channel lock; pass to :func:`release` to free it."""

    path: Path
    pid: int


def _lock_path(channels_dir: Path, name: str) -> Path:
    return channels_dir / f"{name}.lock"


def read_lock(channels_dir: Path, name: str) -> dict | None:
    """Return the parsed lock payload, or ``None`` if missing/unreadable."""
    path = _lock_path(channels_dir, name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lock_owner(channels_dir: Path, name: str) -> int:
    """Return the pid of a *live, foreign* owner, or ``0`` if free/stale/ours.

    A lock owned by the current process, by a dead pid, or with no readable pid
    counts as free (``0``) so the caller may (re)acquire it.
    """
    data = read_lock(channels_dir, name)
    if not data:
        return 0
    try:
        pid = int(data.get("pid", 0) or 0)
    except (TypeError, ValueError):
        return 0
    if pid and pid != os.getpid() and pid_alive(pid):
        return pid
    return 0


def acquire(channels_dir: Path, name: str, *, project_dir: str = "") -> ChannelLock | None:
    """Atomically acquire ``<name>.lock``; ``None`` if a live daemon owns it.

    Uses ``O_CREAT | O_EXCL`` so concurrent daemons can't both win, and reclaims
    a single stale lock (dead/own pid) before retrying.
    """
    channels_dir.mkdir(parents=True, exist_ok=True)
    path = _lock_path(channels_dir, name)
    payload = json.dumps(
        {"pid": os.getpid(), "ts": time.time(), "project_dir": project_dir}
    )
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if lock_owner(channels_dir, name):
                return None  # a live, foreign daemon holds it
            try:  # stale (dead pid) or ours → reclaim and retry once
                path.unlink()
            except OSError:
                return None
            continue
        except OSError:
            return None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return ChannelLock(path=path, pid=os.getpid())
    return None


def release(lock: ChannelLock | None) -> None:
    """Remove the lock file iff this process still owns it."""
    if lock is None:
        return
    try:
        data = json.loads(lock.path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    try:
        if int(data.get("pid", 0) or 0) == os.getpid():
            lock.path.unlink()
    except (OSError, TypeError, ValueError):
        pass
