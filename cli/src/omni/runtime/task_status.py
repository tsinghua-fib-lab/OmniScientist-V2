"""Single source of truth for a task's user-facing status.

``/task show``, ``/inbox`` and ``/schedule`` must never disagree about whether a
run succeeded. They previously derived status from three different places — the
settled task row, a delivery-time notification snapshot, and the representative
subtask — which let a passing run show as ``degraded`` on one surface and
``succeeded`` on another. Every surface now resolves status from the settled task
row through this module.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Statuses a task settles into; anything else is still in flight.
TERMINAL_TASK_STATUSES = frozenset(
    {"succeeded", "degraded", "failed", "cancelled", "needs_input"}
)


def resolve_task_status(task: Any) -> str:
    """Return the authoritative user-facing status for a task row (``""`` if unknown)."""
    if task is None:
        return ""
    return str(getattr(task, "status", "") or "")


def is_terminal(status: str) -> bool:
    return status in TERMINAL_TASK_STATUSES


async def await_settled_status(
    tasks: Any, task_id: str, *, attempts: int = 8, delay: float = 0.25
) -> tuple[str, Any]:
    """Re-read a task until it settles (or attempts exhaust); return ``(status, task)``.

    A scheduled/headless run can hand back a turn result before the verifier has
    settled the durable task row; delivering that transient ``pending`` state
    mislabels a passing run as ``degraded``. Re-reading the row until it reaches a
    terminal status is the fix for that race.
    """
    if not task_id:
        return "", None
    task = None
    attempts = max(1, attempts)
    for index in range(attempts):
        task = await tasks.get_task(task_id)
        status = resolve_task_status(task)
        if is_terminal(status):
            return status, task
        if index + 1 < attempts:
            await asyncio.sleep(delay)
    return resolve_task_status(task), task


__all__ = [
    "TERMINAL_TASK_STATUSES",
    "await_settled_status",
    "is_terminal",
    "resolve_task_status",
]
