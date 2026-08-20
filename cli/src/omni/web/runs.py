"""In-process ownership of web turns.

A browser disconnect unsubscribes. Only ``omni web`` shutdown interrupts the
underlying ``handle_turn``. CLI windows, IM, and the home service never consult
this manager.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omni.config import load_settings
from omni.web.protocol import RpcError

PARTIAL_LIMIT = 32_768
QUEUE_SIZE = 256


def max_inflight_turns(rec: Any) -> int:
    """Read ``web.max_inflight_turns`` from current settings (not process cache)."""
    settings = load_settings(
        project=rec.project,
        cwd=Path(rec.open_path) if rec.project is None else None,
        trusted=rec.trusted,
    )
    raw = getattr(getattr(settings, "web", None), "max_inflight_turns", 10)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 10


@dataclass
class RunHandle:
    """One background ``handle_turn`` plus zero or more SSE subscribers."""

    client_run_id: str
    workspace_key: str
    session_id: str
    task_id: str = ""
    partial: str = ""
    done: bool = False
    error: dict[str, str] | None = None
    result: dict[str, Any] | None = None
    task: asyncio.Task[Any] | None = None
    subscribers: list[asyncio.Queue[tuple[str, dict[str, Any]] | None]] = field(
        default_factory=list
    )

    def append_partial(self, piece: str) -> None:
        if not piece:
            return
        combined = f"{self.partial}{piece}"
        self.partial = combined[-PARTIAL_LIMIT:]

    def publish(self, event: str, data: dict[str, Any]) -> None:
        item: tuple[str, dict[str, Any]] | None = (event, data)
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(item)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def close_subscribers(self) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue[tuple[str, dict[str, Any]] | None]:
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue(
            maxsize=QUEUE_SIZE
        )
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[tuple[str, dict[str, Any]] | None]) -> None:
        try:
            self.subscribers.remove(queue)
        except ValueError:
            pass
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


class RunManager:
    """Owns web turns for the life of the ``omni web`` process."""

    def __init__(self) -> None:
        self._by_run: dict[str, RunHandle] = {}
        self._by_task: dict[tuple[str, str], str] = {}
        self._by_session: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    def get(self, client_run_id: str) -> RunHandle | None:
        return self._by_run.get(client_run_id)

    def by_task(self, workspace_key: str, task_id: str) -> RunHandle | None:
        run_id = self._by_task.get((workspace_key, task_id))
        return self._by_run.get(run_id) if run_id else None

    def by_session(self, workspace_key: str, session_id: str) -> RunHandle | None:
        if not session_id:
            return None
        run_id = self._by_session.get((workspace_key, session_id))
        return self._by_run.get(run_id) if run_id else None

    def live_task_ids(self, workspace_key: str) -> set[str]:
        return {
            handle.task_id
            for handle in self._by_run.values()
            if handle.workspace_key == workspace_key
            and handle.task_id
            and not handle.done
        }

    def inflight_in_workspace(self, workspace_key: str) -> int:
        return sum(
            1
            for handle in self._by_run.values()
            if handle.workspace_key == workspace_key and not handle.done
        )

    async def admit(
        self,
        rec: Any,
        *,
        session_id: str = "",
        client_run_id: str = "",
    ) -> RunHandle:
        run_id = (client_run_id or "").strip() or uuid.uuid4().hex
        async with self._lock:
            if session_id:
                existing = self.by_session(rec.key, session_id)
                if existing is not None and not existing.done:
                    raise RpcError(
                        "busy",
                        "session already has a running turn",
                        session_id=session_id,
                        task_id=existing.task_id,
                        client_run_id=existing.client_run_id,
                    )
            cap = max_inflight_turns(rec)
            if cap > 0 and self.inflight_in_workspace(rec.key) >= cap:
                raise RpcError(
                    "capacity",
                    f"workspace already has {cap} running web turns",
                    limit=cap,
                )
            handle = RunHandle(
                client_run_id=run_id,
                workspace_key=rec.key,
                session_id=session_id,
            )
            self._by_run[run_id] = handle
            if session_id:
                self._by_session[(rec.key, session_id)] = run_id
            return handle

    def bind(self, handle: RunHandle, *, session_id: str, task_id: str) -> None:
        if session_id:
            handle.session_id = session_id
            self._by_session[(handle.workspace_key, session_id)] = handle.client_run_id
        if task_id:
            handle.task_id = task_id
            self._by_task[(handle.workspace_key, task_id)] = handle.client_run_id

    def finish(self, handle: RunHandle) -> None:
        handle.done = True
        if handle.session_id:
            key = (handle.workspace_key, handle.session_id)
            if self._by_session.get(key) == handle.client_run_id:
                self._by_session.pop(key, None)
        handle.close_subscribers()

    def discard(self, handle: RunHandle) -> None:
        self.finish(handle)
        self._by_run.pop(handle.client_run_id, None)
        if handle.task_id:
            self._by_task.pop((handle.workspace_key, handle.task_id), None)

    async def shutdown(self, interrupt: Callable[[RunHandle], Any] | None = None) -> None:
        """Interrupt every live turn, await it, then drop handles."""
        handles = [handle for handle in self._by_run.values() if not handle.done]
        for handle in handles:
            if interrupt is not None:
                try:
                    result = interrupt(handle)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001 — shutdown is best-effort
                    pass
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()
        pending = [handle.task for handle in handles if handle.task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for handle in list(self._by_run.values()):
            self.discard(handle)
