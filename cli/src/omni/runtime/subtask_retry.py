"""Transient-failure auto-retry for background tasks (P2).

An owner-declared ``execution.replay_safe`` skill that fails with a *transient*
error (network / transport blip, upstream 5xx / 429, timeout) may be re-run in
place by :class:`~omni.runtime.subtask_runtime.SubtaskRuntime`, up to
``settings.tasks.max_auto_retries`` times. Skills without replay authority and
deterministic failures (bad input, unknown skill, empty result) fail fast.

Kept out of ``task_runtime`` so the runtime stays a thin coordinator (see the
architecture guard in ``tests/agent/test_contract_driven_boundaries.py``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from omni.storage.models import SubtaskORM

logger = logging.getLogger(__name__)

# Substrings that mark a *transient* failure worth an automatic retry. Deterministic
# failures (bad input, validation, unknown skill, empty result) never match, so
# auto-retry only ever re-runs work that could plausibly succeed unchanged.
_TRANSIENT_SIGNS = (
    "timeout", "timed out", "temporarily", "temporary failure",
    "connection reset", "connection aborted", "connection error", "connect error",
    "connection refused", "read error", "write error", "remoteprotocol",
    "rate limit", "ratelimit", "too many requests", "429",
    "500", "502", "503", "504", "service unavailable", "bad gateway",
    "gateway timeout", "econnreset", "network", "unavailable", "overloaded",
)


def is_transient_error(error: str) -> bool:
    """Whether ``error`` looks transient (retryable) rather than deterministic."""
    text = (error or "").lower()
    return any(sign in text for sign in _TRANSIENT_SIGNS)


def auto_retry_budget(settings: Any) -> int:
    """How many inline auto-retries a transient task failure may take (0 = off)."""
    cfg = getattr(settings, "tasks", None)
    if cfg is None or not getattr(cfg, "auto_retry", True):
        return 0
    return max(0, int(getattr(cfg, "max_auto_retries", 2) or 0))


async def record_auto_retry(
    db: Any, task_recorder: Any, settings: Any, *,
    subtask_id: str, error: str, attempt: int, max_retries: int,
    task_id: str, skill_name: str, on_event: Any = None,
) -> None:
    """Book an inline transient-failure retry: bump bookkeeping, record, back off.

    The task stays ``running`` (it is re-executed in place by the runtime loop);
    this persists ``recovery_attempt`` / ``original_error`` and mirrors a
    ``task.retry`` run event (``auto=true``) so the retry is auditable, then
    applies an optional backoff (``tasks.retry_backoff_s`` × attempt).
    """
    async with db.session() as s:
        obj = await s.get(SubtaskORM, subtask_id)
        if obj is not None:
            if obj.error and not obj.original_error:
                obj.original_error = obj.error
            if not obj.original_error:
                obj.original_error = error
            obj.recovery_attempt = attempt
            obj.recovery_policy = "auto_retry_transient"
            await s.commit()
    logger.warning(
        "task %s transient failure (attempt %d/%d), retrying: %s",
        subtask_id, attempt, max_retries, error,
    )
    if task_recorder is not None and task_id:
        await task_recorder.append_event(
            task_id,
            event_type="subtask.retry",
            status="running",
            name=skill_name,
            skill_name=skill_name,
            subtask_id=subtask_id,
            output_json={
                "auto": True,
                "recovery_attempt": attempt,
                "max_auto_retries": max_retries,
                "recovery_policy": "auto_retry_transient",
                "error": (error or "")[:300],
            },
            summary=f"auto-retry {subtask_id[:8]} attempt {attempt}/{max_retries} (transient)",
        )
    if on_event is not None:
        res = on_event(
            "subtask_retry",
            {"subtask_id": subtask_id, "skill": skill_name, "attempt": attempt, "error": error},
        )
        if inspect.isawaitable(res):
            await res
    backoff = float(getattr(getattr(settings, "tasks", None), "retry_backoff_s", 0.0) or 0.0)
    if backoff > 0:
        await asyncio.sleep(backoff * attempt)


__all__ = ["auto_retry_budget", "is_transient_error", "record_auto_retry"]
