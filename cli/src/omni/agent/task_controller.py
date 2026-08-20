"""Task lifecycle coordination for agent turns.

The orchestrator decides *what* happens in a turn. This controller owns the
durable task bookkeeping around that turn: create, ack, assistant event, and
final status.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

from omni.runtime.task_recorder import TaskRecorder


class TaskController:
    """Small boundary for task creation and terminal status handling."""

    def __init__(self, tasks: TaskRecorder) -> None:
        self._tasks = tasks

    async def create_turn_task(
        self,
        *,
        session_id: str,
        channel: str,
        user_input: str,
        file_uris: list[str] | None = None,
        on_task_ack: Any = None,
    ) -> str:
        # Attachments belong to the immutable turn input: without them a later
        # ``task retry`` would replay the request with its ``@`` mentions
        # stripped, which silently changes what was asked.
        task = await self._tasks.create_task(
            session_id=session_id,
            channel=channel,
            user_input=user_input,
            file_uris=file_uris,
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
        if terminal_status in {"cancelled", "interrupted"}:
            # Checkpoint open children before the advisory event storm so a
            # Windows leftover lock cannot keep the workflow ``running``.
            settler = getattr(self._tasks, "settle_open_children_for_cancel", None)
            if callable(settler):
                await settler(task_id)
            # ``append_event`` may drop on Windows busy; this span must land.
            ensurer = getattr(self._tasks, "ensure_event", None)
            if callable(ensurer):
                await ensurer(
                    task_id,
                    event_type="react.finished",
                    status=terminal_status,
                    name="react",
                    output_json={"kind": kind or "partial", "terminated_reason": terminal_status},
                    summary=f"react partial: {terminal_status}",
                )
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
            # Codex settles from the turn's own end once children are terminal.
            # Skipping settle entirely left IM parents ``running`` after
            # ``react.finished`` because nobody was draining. Refresh first: a
            # finished child is an observation that can close the parent; an
            # active child, or an IM send still owed, keeps it open.
            await self._tasks.refresh_from_executions(task_id)
            task = await self._tasks.get_task(task_id)
            if task is not None and task.status not in {"running", "recovering"}:
                return
            if not drain_tasks:
                return
        await self._tasks.settle_task(
            task_id,
            proposed_status=terminal_status,
            summary=text[:500],
            error=error,
        )

    async def apply_settlement(self, task_id: str, result: Any) -> Any:
        """Reflect what the durable record settled on in the user-facing result.

        The record can know things the finished turn did not: a child that failed
        after the answer was written, or a side effect the answer claims but never
        produced. Only those disagreements are applied here — the answer text is
        never re-judged.
        """
        if getattr(result, "terminated_reason", "") in {"cancelled", "interrupted"}:
            result.settlement_status = "skipped"
            return result
        settled = await self._tasks.settlement(task_id)
        if settled.detail.get("active") or (
            settled.status == "pending" and not settled.detail.get("awaiting_presentation")
        ):
            # Codex does not show a turn's files until the turn is done. IM
            # withholds on ``pending_child_task``; a bare ``pending`` is also
            # used for "the chat send has not happened yet" and must still send.
            result.settlement_status = "pending_child_task"
        else:
            result.settlement_status = settled.status
        warnings = list(getattr(result, "degraded_warnings", []) or [])
        if undelivered := settled.detail.get("undelivered_outputs"):
            warnings.append("Still missing deliverables: " + ", ".join(undelivered))
            result.degraded_warnings = list(dict.fromkeys(warnings))
        if settled.status != "failed" or getattr(result, "kind", "") in {
            "error",
            # A turn that stopped to ask for one supplyable thing is a suspend the
            # user can answer and resume. Rewriting it to a terminal error would
            # hide the question behind the failure it was reported as, leaving the
            # run unresumable for a reason the user was never told.
            "needs_input",
        }:
            return result
        result.kind = "error"
        result.terminated_reason = "settlement_failed"
        warnings = list(getattr(result, "degraded_warnings", []) or [])
        result.degraded_warnings = list(
            dict.fromkeys([*warnings, _settlement_warning(settled)])
        )
        return result


def _settlement_warning(settled: Any) -> str:
    """Say which part of the record contradicted the answer."""
    if unfounded := settled.detail.get("unfounded_claims"):
        return (
            "This turn reported work that left no durable record: "
            + ", ".join(unfounded)
            + ". Treat the claim as unconfirmed."
        )
    if lost := settled.detail.get("lost"):
        return f"{len(lost)} background task(s) did not complete."
    if undelivered := settled.detail.get("undelivered_outputs"):
        return "Still missing deliverables: " + ", ".join(undelivered)
    return "The task could not be accounted for in the durable record."
