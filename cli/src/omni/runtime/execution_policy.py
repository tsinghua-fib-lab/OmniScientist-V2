"""Shared execution-policy primitives for every tool and skill route."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, TypeVar

T = TypeVar("T")

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - platform dependent
    _fcntl = None  # type: ignore[assignment]

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - platform dependent
    _msvcrt = None  # type: ignore[assignment]

_FILESYSTEM_MUTATIONS = frozenset({"write_file", "edit_file"})
_EXECUTION_TOOLS = frozenset({"bash", "run_compute"})
_STORE_MUTATIONS = frozenset({
    "add_evidence",
    "build_research_artifact",
    "cite_source",
    "log_run",
    "record_claim",
    "record_hypothesis",
    "record_run",
    "remember",
    "run_skill",
    "run_workflow",
    "search_literature",
    "submit_task",
})


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    references: int = 0


class ToolResourceLockPool:
    """Serialize calls touching the same resource without serializing safe reads.

    Locks are acquired in sorted resource order to avoid deadlocks. Entries are
    reference-counted and removed once no caller owns or waits for them, keeping
    a long-running daemon bounded even when it edits many distinct files.
    """

    def __init__(self, *, stripes: int = 64, lock_dir: Path | None = None) -> None:
        # ``stripes`` remains an explicit sizing hint for configuration/tests;
        # exact resource keys are retained so unrelated paths never collide.
        self.stripes = max(1, int(stripes))
        self.lock_dir = lock_dir
        self._entries: dict[str, _LockEntry] = {}
        self._guard = asyncio.Lock()

    async def run(
        self,
        resources: Iterable[str],
        invoke: Callable[[], Awaitable[T]],
    ) -> T:
        keys = sorted({str(item).strip() for item in resources if str(item).strip()})
        if not keys:
            return await invoke()
        entries = await self._reserve(keys)
        acquired: list[_LockEntry] = []
        process_locks: list[IO[bytes]] = []
        try:
            for entry in entries:
                await entry.lock.acquire()
                acquired.append(entry)
            if self.lock_dir is not None:
                for key in keys:
                    process_locks.append(await self._acquire_process_lock(key))
            return await invoke()
        finally:
            for lock_file in reversed(process_locks):
                _release_process_lock(lock_file)
            for entry in reversed(acquired):
                entry.lock.release()
            await self._release(keys)

    async def _acquire_process_lock(self, key: str) -> IO[bytes]:
        """Acquire an advisory file lock without blocking the event loop."""
        assert self.lock_dir is not None
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        lock_file = (self.lock_dir / f"{digest}.lock").open("a+b")
        try:
            while not _try_process_lock(lock_file):
                await asyncio.sleep(0.05)
        except BaseException:
            lock_file.close()
            raise
        return lock_file

    async def _reserve(self, keys: list[str]) -> list[_LockEntry]:
        async with self._guard:
            entries: list[_LockEntry] = []
            for key in keys:
                entry = self._entries.get(key)
                if entry is None:
                    entry = _LockEntry(asyncio.Lock())
                    self._entries[key] = entry
                entry.references += 1
                entries.append(entry)
            return entries

    async def _release(self, keys: list[str]) -> None:
        async with self._guard:
            for key in keys:
                entry = self._entries.get(key)
                if entry is None:
                    continue
                entry.references -= 1
                if entry.references <= 0 and not entry.lock.locked():
                    self._entries.pop(key, None)


def resources_for_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    scope: str = "",
    sensitive: bool = False,
) -> list[str]:
    """Return deterministic resource keys for a side-effecting call."""
    name = str(tool_name or "").strip()
    base = scope or "global"
    if name in _FILESYSTEM_MUTATIONS:
        raw = str((arguments or {}).get("path") or "").strip()
        candidate = Path(raw).expanduser() if raw else Path(base)
        scope_path = Path(base).expanduser()
        if raw and not candidate.is_absolute() and scope_path.is_absolute():
            candidate = scope_path / candidate
        path = str(candidate.resolve(strict=False))
        return [f"fs:{path}"]
    if name in _EXECUTION_TOOLS:
        return [f"exec:{base}"]
    if name == "cancel_compute":
        job_id = str((arguments or {}).get("job_id") or "unknown")
        return [f"compute-control:{job_id}"]
    if name in _STORE_MUTATIONS:
        return [f"store:{base}"]
    if sensitive:
        return [f"tool:{base}:{name}"]
    return []


def skill_requires_approval(entry: Any) -> bool:
    """Classify executable skills without inferring risk from their names."""
    execution = getattr(entry, "execution", None)
    execution = execution if isinstance(execution, dict) else {}
    explicit = execution.get("requires_approval")
    if explicit is not None:
        return bool(explicit)
    risk = str(execution.get("risk") or "").strip().lower()
    if risk in {"write", "exec", "destructive", "sensitive"}:
        return True
    kind = str(getattr(getattr(entry, "kind", None), "value", getattr(entry, "kind", "")))
    source = str(getattr(entry, "source", "") or "")
    return kind == "cli_exec" and source not in {"builtin", "project_omni", "user_omni"}


def _try_process_lock(lock_file: IO[bytes]) -> bool:
    if _fcntl is not None:
        try:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    if _msvcrt is not None:
        try:
            lock_file.seek(0, 2)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    return True


def _release_process_lock(lock_file: IO[bytes]) -> None:
    try:
        if _fcntl is not None:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
        elif _msvcrt is not None:
            lock_file.seek(0)
            _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)
    finally:
        lock_file.close()


def tool_is_mutating(tool_name: str) -> bool:
    """True when a start event must land before this tool may run."""
    name = str(tool_name or "").strip()
    return (
        name in _FILESYSTEM_MUTATIONS
        or name in _EXECUTION_TOOLS
        or name in _STORE_MUTATIONS
    )


__all__ = [
    "ToolResourceLockPool",
    "resources_for_tool",
    "skill_requires_approval",
    "tool_is_mutating",
]
