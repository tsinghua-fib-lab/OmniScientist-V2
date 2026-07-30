"""Cooperative runtime control for one agent execution tree.

Durable task controls remain the source of truth.  This module turns those
stored requests into a low-latency signal that can interrupt an in-flight
await while retaining steering instructions for the next safe boundary.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
ControlReader = Callable[[], Any]
ControlAcknowledger = Callable[[list[str]], Any]


class ExecutionCancelled(RuntimeError):
    """Raised when a controlled operation cannot return its own cancel result."""


class CancellationEscalator:
    """Scope repeated user cancellation requests to one foreground turn."""

    def __init__(self) -> None:
        self._requests = 0

    def request(self) -> Literal["cooperative", "force"]:
        """Return cooperative once, then force until the turn is reset."""
        self._requests += 1
        return "cooperative" if self._requests == 1 else "force"

    def reset(self) -> None:
        """Start a fresh cancellation sequence for the next foreground turn."""
        self._requests = 0


class ExecutionControl:
    """Bridge durable steer/cancel requests into a running asyncio task tree.

    ``run`` is nestable.  Only the outermost invocation polls the durable
    reader and owns cancellation; inner agent, workflow, and skill layers use
    the same instance to drain steering without competing for control rows.
    """

    def __init__(
        self,
        read_controls: ControlReader | None = None,
        *,
        poll_interval: float = 0.1,
        acknowledge_controls: ControlAcknowledger | None = None,
    ) -> None:
        self._read_controls = read_controls
        self._acknowledge_controls = acknowledge_controls
        self._poll_interval = max(0.01, float(poll_interval))
        self._cancel_requested = False
        self._steering: list[tuple[str, str]] = []
        self._pending_ack_ids: list[str] = []
        self._delivered_control_ids: list[str] = []
        self._poll_lock = asyncio.Lock()
        self._run_depth = 0

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    @property
    def delivered_control_ids(self) -> tuple[str, ...]:
        """Return controls that reached a semantic boundary in this process."""
        return tuple(dict.fromkeys(self._delivered_control_ids))

    def request_cancel(self) -> None:
        """Request cancellation locally without creating another durable row."""
        self._cancel_requested = True

    def push_steer(self, instruction: str) -> None:
        """Queue an in-memory steer without creating a durable control row.

        The controlled loop drains it at its next safe boundary via
        :meth:`take_steering` (same path as durable ``steer`` requests). Used to
        deliver an inter-agent message to a running subagent.
        """
        text = (instruction or "").strip()
        if text:
            self._steering.append(("", text))

    def take_steering(self) -> list[str]:
        """Return queued steering once, preserving arrival order."""
        values = [instruction for _, instruction in self._steering]
        delivered_ids = [
            control_id for control_id, _ in self._steering if control_id
        ]
        self._pending_ack_ids.extend(delivered_ids)
        self._delivered_control_ids.extend(delivered_ids)
        self._steering.clear()
        return values

    async def poll(self) -> None:
        """Consume new durable controls into the in-memory execution signal."""
        if self._read_controls is None or self._cancel_requested:
            return
        async with self._poll_lock:
            try:
                value = self._read_controls()
                if inspect.isawaitable(value):
                    value = await value
            except Exception as exc:  # noqa: BLE001 - a transient poll failure must not kill work.
                logger.warning("execution control poll failed: %s", exc)
                return
            if not isinstance(value, list):
                return
            for item in value:
                if not isinstance(item, dict):
                    continue
                action = str(item.get("action") or "").strip().lower()
                control_id = str(item.get("id") or "")
                if action == "cancel":
                    self._cancel_requested = True
                    if control_id:
                        self._pending_ack_ids.append(control_id)
                elif action == "steer":
                    instruction = str(item.get("instruction") or "").strip()
                    if instruction:
                        self._steering.append((control_id, instruction))

    async def _flush_acknowledgements(self, *, attempts: int = 1) -> None:
        if not self._pending_ack_ids or self._acknowledge_controls is None:
            return
        ids = list(dict.fromkeys(self._pending_ack_ids))
        self._pending_ack_ids.clear()
        max_attempts = max(1, attempts)
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                value = self._acknowledge_controls(ids)
                if inspect.isawaitable(value):
                    await value
                return
            except Exception as exc:  # noqa: BLE001 - audit failure cannot break execution.
                last_error = exc
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(0)
        self._pending_ack_ids[:0] = ids
        logger.warning(
            "execution control acknowledgement failed after %d attempt(s): %s",
            max_attempts,
            last_error,
        )

    async def run(self, awaitable: Awaitable[T]) -> T:
        """Run one execution tree and interrupt it when cancellation arrives."""
        if self._run_depth:
            return await awaitable

        self._run_depth += 1
        operation = asyncio.ensure_future(awaitable)
        try:
            while True:
                await self._flush_acknowledgements()
                if operation.done():
                    await self._flush_acknowledgements(attempts=3)
                    return operation.result()
                await self.poll()
                if self._cancel_requested:
                    await self._flush_acknowledgements(attempts=3)
                    operation.cancel()
                    try:
                        return await operation
                    except asyncio.CancelledError as exc:
                        raise ExecutionCancelled("execution cancelled by user") from exc
                done, _ = await asyncio.wait(
                    {operation},
                    timeout=self._poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    await self._flush_acknowledgements(attempts=3)
                    return operation.result()
        except asyncio.CancelledError:
            self.request_cancel()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise
        finally:
            self._run_depth -= 1


__all__ = ["CancellationEscalator", "ExecutionCancelled", "ExecutionControl"]
