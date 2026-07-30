"""Persist canonical workflow completion output into its owning session."""

from __future__ import annotations

import logging
from typing import Any

from omni.runtime.task_results import _artifact_uris, _result_summary
from omni.storage.models import ConversationMessageORM, SessionORM, _utcnow

logger = logging.getLogger(__name__)


async def write_back_workflow_result(
    *,
    db: Any,
    session_id: str,
    workflow_run_id: str,
    status: str,
    result: dict[str, Any],
    task_id: str = "",
) -> None:
    """Write one workflow result while keeping Task and run identities distinct."""
    if not session_id:
        return
    lines = [
        f"[Workflow {status}] `{workflow_run_id[:8]}`",
        _result_summary(result),
    ]
    if task_id:
        lines.append(f"Full task: `/task show {task_id[:8]}`")
    lines.append(f"Workflow details: `/task show {workflow_run_id[:8]}`")
    try:
        async with db.session() as session:
            session.add(
                ConversationMessageORM(
                    session_id=session_id,
                    role="assistant",
                    content="\n".join(lines),
                    content_type="workflow_result",
                    name="workflow",
                    meta={
                        "kind": "workflow_result",
                        "task_id": task_id,
                        "workflow_run_id": workflow_run_id,
                        "status": status,
                        "artifacts": _artifact_uris(result),
                    },
                )
            )
            row = await session.get(SessionORM, session_id)
            if row is not None:
                row.updated_at = _utcnow()
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.debug("workflow result write-back failed", exc_info=True)


__all__ = ["write_back_workflow_result"]
