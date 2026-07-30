"""Terminal lifecycle helpers for durable skill executions."""

from __future__ import annotations

import inspect
from typing import Any

from omni.runtime.task_results import _skill_execution_event


async def settle_subtask_cancellation(
    *,
    store: Any,
    task_recorder: Any,
    on_event: Any,
    subtask_id: str,
    skill_name: str,
    task_id: str,
    event_link: dict[str, str],
    trace: list[dict[str, Any]],
    refresh_parent: bool,
) -> dict[str, Any]:
    """Persist a cancelled execution and close its observable event stream."""
    result = {
        "status": "cancelled",
        "summary": f"{skill_name} was cancelled by the user.",
        "recoverable": True,
    }
    await store.finish_cancelled(subtask_id, result=result, trace=trace)
    if on_event is not None:
        emitted = on_event(
            "task_done",
            _skill_execution_event(
                task_id,
                subtask_id,
                skill_name,
                status="cancelled",
                result=result,
            ),
        )
        if inspect.isawaitable(emitted):
            await emitted
    if task_recorder is not None and task_id:
        await task_recorder.append_event(
            task_id,
            event_type="subtask.cancelled",
            status="cancelled",
            name=skill_name,
            skill_name=skill_name,
            **event_link,
            output_json=result,
            summary=result["summary"],
        )
        if refresh_parent:
            await task_recorder.refresh_from_executions(task_id)
    return result


__all__ = ["settle_subtask_cancellation"]
