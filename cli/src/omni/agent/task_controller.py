"""Task lifecycle coordination for agent turns.

The orchestrator decides *what* happens in a turn. This controller owns the
durable task bookkeeping around that turn: create, ack, assistant event, final
status, and verification.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

from omni.runtime.task_recorder import TaskRecorder
from omni.runtime.verification import VerificationRunner


class TaskController:
    """Small boundary for task creation and terminal status handling."""

    def __init__(self, tasks: TaskRecorder, verifier: VerificationRunner) -> None:
        self._tasks = tasks
        self._verifier = verifier

    async def create_turn_task(
        self,
        *,
        session_id: str,
        channel: str,
        user_input: str,
        on_task_ack: Any = None,
    ) -> str:
        task = await self._tasks.create_task(
            session_id=session_id,
            channel=channel,
            user_input=user_input,
        )
        if on_task_ack is not None:
            ack_result = on_task_ack(
                {"task_id": task.id, "session_id": session_id, "status": "planning"}
            )
            if inspect.isawaitable(ack_result):
                await ack_result
        return task.id

    async def finish_turn(
        self,
        task_id: str,
        *,
        kind: str,
        text: str,
        submitted_workflow_ids: list[str] | None = None,
        submitted_subtask_ids: list[str] | None = None,
        drain_tasks: bool = True,
        error: str = "",
        task_status: str = "",
        missing_inputs: list[dict[str, Any]] | None = None,
    ) -> None:
        workflows = [wid for wid in (submitted_workflow_ids or []) if wid]
        submitted = [tid for tid in (submitted_subtask_ids or []) if tid]
        terminal_status = task_status or (
            "failed"
            if kind == "error"
            else "needs_input"
            if kind == "needs_input"
            else "succeeded"
        )
        message_status = (
            terminal_status
            if terminal_status in {"cancelled", "interrupted"}
            else "failed" if kind == "error"
            else "needs_input" if kind == "needs_input"
            else "succeeded"
        )
        task = await self._tasks.get_task(task_id)
        started_at = getattr(task, "started_at", None) or getattr(task, "created_at", None)
        elapsed_ms: float | None = None
        if started_at is not None:
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            elapsed_ms = max(0.0, (datetime.now(UTC) - started_at).total_seconds() * 1000)
        await self._tasks.append_event(
            task_id,
            event_type="assistant.message",
            status=message_status,
            name="assistant",
            output_json={
                "text": text,
                "kind": kind,
                "submitted_workflow_ids": workflows,
                "submitted_subtask_ids": submitted,
                "elapsed_ms": elapsed_ms,
            },
            error=error,
            summary=(text or error)[:220],
            duration_ms=elapsed_ms,
        )
        if terminal_status == "needs_input":
            if task is None or task.status != "needs_input":
                await self._tasks.mark_needs_input(
                    task_id,
                    summary=text[:500],
                    missing_inputs=missing_inputs or [],
                )
            return
        if terminal_status in {"cancelled", "interrupted"}:
            await self._tasks.finish_task(
                task_id,
                status=terminal_status,
                summary=text[:500],
                error=error,
            )
            return
        if workflows or submitted:
            if not drain_tasks:
                await self._verifier.verify(task_id)
                return
            await self._tasks.refresh_from_executions(task_id)
            task = await self._tasks.get_task(task_id)
            if task is not None and task.status not in {"running", "recovering"}:
                return
        await self._tasks.settle_task(
            task_id,
            proposed_status=terminal_status,
            summary=text[:500],
            error=error,
        )

    async def apply_verifier_outcome(self, task_id: str, result: Any) -> Any:
        """Reflect the durable verifier decision in the user-facing result."""
        if getattr(result, "terminated_reason", "") in {"cancelled", "interrupted"}:
            result.verification_status = "skipped"
            return result
        events = await self._tasks.list_events(task_id)
        verification = next(
            (
                event.event_type.removeprefix("verification.")
                for event in reversed(events)
                if event.event_type.startswith("verification.")
            ),
            "",
        )
        task = await self._tasks.get_task(task_id)
        if not verification and task is not None and task.status in {"running", "recovering"}:
            verification = "pending"
        if verification:
            result.verification_status = verification
        if verification == "failed" and getattr(result, "kind", "") != "error":
            result.kind = "error"
            result.terminated_reason = "verification_failed"
            warnings = list(getattr(result, "degraded_warnings", []) or [])
            result.degraded_warnings = list(
                dict.fromkeys(
                    [
                        *warnings,
                        "The result did not satisfy its verification contract; "
                        "inspect verification.failed.",
                    ]
                )
            )
        return result
