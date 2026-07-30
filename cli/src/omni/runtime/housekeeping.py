"""Task-history hygiene: stale-task reconcile and age-based retention.

Both policies are settings-gated and run from the subtask runtime at service
startup, periodically on the poller, and once per one-shot ``task drain``.
Lost skill-execution owners are reconciled first (see
``execution_ownership``), then:

- ``tasks.interrupt_stale_after_s`` — a task stuck in running/recovering with
  no event for the window lost its process and is settled as ``interrupted``
  (terminal, prunable). ``0`` disables. The same window is the time lease for
  legacy executions that never recorded ``owner_pid``.
- ``tasks.retention_days`` — failed/cancelled/interrupted tasks whose
  completion is older than the window are deleted, cascading to subtasks and
  events. Succeeded/degraded tasks are provenance and are never auto-deleted;
  artifact files are never touched. ``0`` (default) disables.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from omni.config.settings import OmniSettings
from omni.storage.models import _utcnow

logger = logging.getLogger(__name__)


async def run_housekeeping(recorder: Any, settings: OmniSettings) -> dict[str, int]:
    """Apply both hygiene policies once; each half fails soft and is logged."""
    out = {"interrupted": 0, "retention_deleted": 0}
    if recorder is None:
        return out
    cfg = settings.tasks
    try:
        stale = await recorder.reconcile_stale_tasks(
            stale_after_s=float(getattr(cfg, "interrupt_stale_after_s", 0.0) or 0.0)
        )
        out["interrupted"] = len(stale)
    except Exception:  # noqa: BLE001
        logger.exception("stale-task reconcile failed")
    days = int(getattr(cfg, "retention_days", 0) or 0)
    if days > 0:
        try:
            cutoff = _utcnow() - timedelta(days=days)
            outcome = await recorder.clear_tasks(
                before=cutoff, kind=None, include_archived=True, prunable_only=True,
            )
            out["retention_deleted"] = outcome.deleted_total
            if outcome.deleted_total:
                logger.info(
                    "retention removed %d task(s) older than %dd", outcome.deleted_total, days
                )
        except Exception:  # noqa: BLE001
            logger.exception("retention sweep failed")
    return out
