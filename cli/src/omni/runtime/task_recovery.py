"""Typed recovery coordinator for ``task retry|resume|requeue``.

Semantics:

* **retry** — create a new attempt from an immutable input snapshot.
* **resume** — reopen a research task into the same ReAct loop, or continue
  from a durable schedule/workflow checkpoint.
* **requeue** — put the same standalone skill execution back on the recovery
  queue (former in-place ``resume_subtask``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from omni.runtime.action_checkpoints import ActionCheckpointStore
from omni.runtime.schedule_checkpoint_resume import (
    find_open_checkpoint_for_task,
    resolve_schedule_checkpoint,
)
from omni.runtime.task_object_resolver import TaskObjectKind, TaskObjectResolution
from omni.scheduling.service import ScheduleService
from omni.storage.models import TaskORM, WorkflowStepORM

logger = logging.getLogger(__name__)


async def _resolve_local_workflow_step(
    runtime: Any, step_id: str
) -> tuple[Any, WorkflowStepORM | None, str]:
    """Resolve a planner-facing step key / row id inside one workspace."""
    if not step_id:
        return None, None, "not_found"
    runs = await runtime.list_workflow_runs(limit=1000)
    exact: list[tuple[Any, WorkflowStepORM]] = []
    prefix: list[tuple[Any, WorkflowStepORM]] = []
    for run in runs:
        for step in await runtime.list_workflow_steps(run.id):
            values = {step.id, step.step_key, step.current_execution_id}
            if step_id in values:
                exact.append((run, step))
            elif any(value and str(value).startswith(step_id) for value in values):
                prefix.append((run, step))
    matches = exact or prefix
    if len(matches) == 1:
        run, step = matches[0]
        return run, step, "ok"
    if matches:
        return None, None, "ambiguous"
    return None, None, "not_found"

RecoveryStatus = Literal[
    "ok",
    "not_found",
    "ambiguous",
    "wrong_state",
    "checkpoint_required",
    "input_required",
    "needs_step",
    "error",
]

_RETRYABLE_TASK_STATUSES = frozenset(
    {"failed", "cancelled", "interrupted", "degraded"}
)
_ACTIVE_STATUSES = frozenset({"running", "recovering", "recovery_claimed"})
_RETRYABLE_EXECUTION_STATUSES = frozenset(
    {"failed", "cancelled", "degraded", "succeeded", "interrupted"}
)


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """Uniform result for CLI and programmatic recovery callers."""

    status: RecoveryStatus
    object_kind: TaskObjectKind | None = None
    object_id: str = ""
    task_id: str = ""
    new_id: str = ""
    message: str = ""
    suggested_command: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _outcome(
    status: RecoveryStatus,
    *,
    kind: TaskObjectKind | None = None,
    object_id: str = "",
    task_id: str = "",
    new_id: str = "",
    message: str = "",
    suggested: str = "",
    **detail: Any,
) -> RecoveryOutcome:
    return RecoveryOutcome(
        status=status,
        object_kind=kind,
        object_id=object_id,
        task_id=task_id,
        new_id=new_id,
        message=message,
        suggested_command=suggested,
        detail=detail,
    )


def _snapshot_from_task(task: TaskORM) -> dict[str, Any]:
    snap = dict(getattr(task, "input_snapshot_json", None) or {})
    if snap.get("user_input"):
        return snap
    return {
        "user_input": task.user_input or "",
        "file_uris": list(snap.get("file_uris") or []),
        "interaction_mode": str(snap.get("interaction_mode") or ""),
        "channel": task.channel or "cli",
        "origin": str(snap.get("origin") or "interactive"),
        "external_key": task.external_key or "",
    }


async def enrich_resolution(
    agent: Any,
    resolution: TaskObjectResolution,
    object_id: str,
) -> TaskObjectResolution:
    """Fill in local workflow step_key matches when the global scan misses.

    ``task show`` already falls back to :func:`resolve_workflow_step` for
    planner-facing step keys (e.g. ``diagram``). Recovery commands share that
    fallback so ``task retry diagram`` keeps working.
    """
    if resolution.status == "ambiguous":
        return resolution
    if resolution.status == "ok" and resolution.object_kind:
        return resolution
    workflow, step, step_status = await _resolve_local_workflow_step(
        agent.runtime, object_id
    )
    if step_status == "ambiguous":
        return TaskObjectResolution(status="ambiguous")
    if step is None or step_status != "ok":
        return resolution
    return TaskObjectResolution(
        status="ok",
        object_kind="workflow_step",
        object_id=step.id,
        task_id=step.task_id or (workflow.task_id if workflow is not None else ""),
        settings=getattr(agent, "settings", None),
    )


class TaskRecoveryCoordinator:
    """Dispatch recovery by typed object kind inside one owning workspace."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    async def _reconcile_lost(
        self, *, task_id: str = "", execution_id: str = ""
    ) -> None:
        runtime = getattr(self._agent, "runtime", None)
        reconcile = getattr(runtime, "reconcile_lost_executors", None)
        if reconcile is None:
            return
        await reconcile(
            task_id=task_id or None,
            execution_id=execution_id or None,
            explicit=True,
        )

    async def retry(
        self,
        resolution: TaskObjectResolution,
        *,
        notify_channel: str = "",
        step: str = "",
        run_turn: bool = True,
        object_id: str = "",
        session_id: str | None = None,
    ) -> RecoveryOutcome:
        resolution = await enrich_resolution(self._agent, resolution, object_id)
        if resolution.status == "ambiguous":
            return _outcome(
                "ambiguous",
                message="Prefix matches multiple objects; provide a longer id.",
            )
        if resolution.status != "ok" or not resolution.object_kind:
            return _outcome("not_found", message="No task object matched that id.")
        kind = resolution.object_kind
        object_id = resolution.object_id
        if kind == "task":
            return await self._retry_task(
                object_id, run_turn=run_turn, session_id=session_id
            )
        if kind == "skill_execution":
            return await self._retry_skill_execution(
                object_id, notify_channel=notify_channel
            )
        if kind == "workflow_step":
            return await self._retry_workflow_step_object(
                object_id, notify_channel=notify_channel
            )
        if kind == "workflow_run":
            if not step:
                return _outcome(
                    "needs_step",
                    kind=kind,
                    object_id=object_id,
                    task_id=resolution.task_id,
                    message=(
                        f"Workflow {object_id[:8]} requires --step <step-id> for retry."
                    ),
                    suggested=f"omni task retry {object_id[:8]} --step <step-id>",
                )
            return await self._retry_workflow_step(
                object_id, step, notify_channel=notify_channel
            )
        return _outcome("error", message=f"Unsupported object kind: {kind}")

    async def resume(
        self,
        resolution: TaskObjectResolution,
        *,
        step: str = "",
        input_choice: str = "",
        decider: str = "local",
        object_id: str = "",
    ) -> RecoveryOutcome:
        resolution = await enrich_resolution(self._agent, resolution, object_id)
        if resolution.status == "ambiguous":
            return _outcome(
                "ambiguous",
                message="Prefix matches multiple objects; provide a longer id.",
            )
        if resolution.status != "ok" or not resolution.object_kind:
            return _outcome("not_found", message="No task object matched that id.")
        kind = resolution.object_kind
        object_id = resolution.object_id
        if kind == "task":
            return await self._resume_task(
                object_id, input_choice=input_choice, decider=decider
            )
        if kind == "workflow_step":
            return await self._resume_workflow_step_object(object_id)
        if kind == "workflow_run":
            if not step:
                return _outcome(
                    "needs_step",
                    kind=kind,
                    object_id=object_id,
                    task_id=resolution.task_id,
                    message=(
                        f"Workflow {object_id[:8]} requires --step <step-id> for resume."
                    ),
                    suggested=f"omni task resume {object_id[:8]} --step <step-id>",
                )
            return await self._resume_workflow_step(object_id, step)
        if kind == "skill_execution":
            return await self._resume_skill_execution(object_id)
        return _outcome("error", message=f"Unsupported object kind: {kind}")

    async def requeue(
        self,
        resolution: TaskObjectResolution,
        *,
        object_id: str = "",
    ) -> RecoveryOutcome:
        resolution = await enrich_resolution(self._agent, resolution, object_id)
        if resolution.status == "ambiguous":
            return _outcome(
                "ambiguous",
                message="Prefix matches multiple objects; provide a longer id.",
            )
        if resolution.status != "ok" or not resolution.object_kind:
            return _outcome("not_found", message="No task object matched that id.")
        if resolution.object_kind != "skill_execution":
            return _outcome(
                "wrong_state",
                kind=resolution.object_kind,
                object_id=resolution.object_id,
                task_id=resolution.task_id,
                message=(
                    f"{resolution.object_kind} {resolution.object_id[:8]} cannot be "
                    "requeued; requeue applies only to standalone skill executions."
                ),
                suggested=(
                    f"omni task retry {resolution.object_id[:8]}"
                    if resolution.object_kind == "task"
                    else f"omni task resume {resolution.object_id[:8]}"
                ),
            )
        return await self._requeue_skill_execution(resolution.object_id)

    # ── task ──────────────────────────────────────────────────────────────

    async def _retry_task(
        self, task_id: str, *, run_turn: bool, session_id: str | None = None
    ) -> RecoveryOutcome:
        agent = self._agent
        original = await agent.tasks.get_task(task_id)
        if original is None:
            return _outcome("not_found", message=f"Task {task_id} was not found.")
        if original.status in _ACTIVE_STATUSES:
            await self._reconcile_lost(task_id=original.id)
            original = await agent.tasks.get_task(task_id)
            if original is None:
                return _outcome("not_found", message=f"Task {task_id} was not found.")
        if original.status in _ACTIVE_STATUSES:
            return _outcome(
                "wrong_state",
                kind="task",
                object_id=original.id,
                task_id=original.id,
                message=(
                    f"Task {original.id[:8]} is {original.status}; "
                    "wait for it to settle before retrying."
                ),
            )
        if original.status == "needs_input":
            return _outcome(
                "wrong_state",
                kind="task",
                object_id=original.id,
                task_id=original.id,
                message=(
                    f"Task {original.id[:8]} is waiting for input; "
                    "use resume with --input instead of retry."
                ),
                suggested=f"omni task resume {original.id[:8]} --input <choice>",
            )
        if original.status == "awaiting_approval":
            return _outcome(
                "wrong_state",
                kind="task",
                object_id=original.id,
                task_id=original.id,
                message=(
                    f"Task {original.id[:8]} is awaiting approval; "
                    "approve or reject it instead of retrying."
                ),
                suggested=f"omni task approve {original.id[:8]}",
            )
        if original.status not in _RETRYABLE_TASK_STATUSES:
            return _outcome(
                "wrong_state",
                kind="task",
                object_id=original.id,
                task_id=original.id,
                message=(
                    f"Task {original.id[:8]} is {original.status}; "
                    "only failed/cancelled/interrupted/degraded tasks can be retried."
                ),
            )

        snapshot = _snapshot_from_task(original)
        user_input = str(snapshot.get("user_input") or original.user_input or "").strip()
        if not user_input:
            return _outcome(
                "error",
                kind="task",
                object_id=original.id,
                task_id=original.id,
                message=(
                    f"Task {original.id[:8]} has no recoverable input snapshot."
                ),
            )
        file_uris = [
            str(u) for u in (snapshot.get("file_uris") or []) if str(u).strip()
        ]
        root_id = str(getattr(original, "root_task_id", "") or "") or original.id
        # Fail closed when a prior retry of this chain is still active, and number
        # attempts from the highest known attempt in the lineage (not just parent+1).
        related = await agent.tasks.list_tasks(limit=200, include_archived=True)
        lineage = [
            row
            for row in related
            if row.id == root_id
            or str(getattr(row, "root_task_id", "") or "") == root_id
            or str(getattr(row, "retry_of_task_id", "") or "") == original.id
        ]
        active = [
            row
            for row in lineage
            if row.id != original.id and row.status in _ACTIVE_STATUSES
        ]
        if active:
            live = active[0]
            return _outcome(
                "wrong_state",
                kind="task",
                object_id=original.id,
                task_id=live.id,
                message=(
                    f"Task {original.id[:8]} already has an active retry "
                    f"{live.id[:8]} ({live.status}); wait for it to settle."
                ),
                suggested=f"omni task show {live.id[:8]}",
            )
        attempt = max(
            (int(getattr(row, "attempt", 1) or 1) for row in lineage),
            default=int(getattr(original, "attempt", 1) or 1),
        ) + 1
        # A foreground REPL retry grafts the new attempt into the *current*
        # conversation (so the user watches it stream and any needs_input
        # clarification resurfaces in-session). Headless callers keep the
        # original task's session.
        turn_session = (session_id or "").strip() or (original.session_id or "")
        new_task = await agent.tasks.create_task(
            session_id=turn_session,
            channel=str(snapshot.get("channel") or original.channel or "cli"),
            user_input=user_input,
            external_key=str(
                snapshot.get("external_key") or original.external_key or ""
            ),
            title=original.title or "",
            kind=original.kind or "turn",
            depth=int(original.depth or 0),
            retry_of_task_id=original.id,
            root_task_id=root_id,
            attempt=attempt,
            input_snapshot=snapshot,
            file_uris=file_uris,
            interaction_mode=str(snapshot.get("interaction_mode") or ""),
            origin=str(snapshot.get("origin") or "interactive"),
        )
        await agent.tasks.inherit_research_ledger(new_task.id, original)
        prior_grants = [
            str(name).strip()
            for name in (getattr(original, "approved_tools", None) or [])
            if str(name).strip()
        ]
        if prior_grants:
            await agent.tasks.grant_tools(
                new_task.id, prior_grants, reason="retry-inherit"
            )
        if run_turn:
            try:
                mode = str(snapshot.get("interaction_mode") or "").strip() or None
                await agent.handle_turn(
                    user_input,
                    session_id=new_task.session_id or None,
                    channel=new_task.channel or "cli",
                    file_uris=file_uris or None,
                    interaction_mode=mode,
                    existing_task_id=new_task.id,
                    origin=str(snapshot.get("origin") or "interactive"),
                )
            except Exception as exc:  # noqa: BLE001 - keep the attempt observable
                logger.exception("task retry turn failed for %s", new_task.id)
                await agent.tasks.finish_task(
                    new_task.id,
                    status="failed",
                    summary=f"retry attempt failed: {exc}",
                    error=str(exc),
                )
                return _outcome(
                    "error",
                    kind="task",
                    object_id=original.id,
                    task_id=new_task.id,
                    new_id=new_task.id,
                    message=(
                        f"Created retry attempt {new_task.id[:8]} but the turn "
                        f"failed: {exc}"
                    ),
                    attempt=attempt,
                    root_task_id=root_id,
                )
        return _outcome(
            "ok",
            kind="task",
            object_id=original.id,
            task_id=new_task.id,
            new_id=new_task.id,
            message=(
                f"Created task retry attempt {new_task.id[:8]} "
                f"(attempt {attempt} of {root_id[:8]})."
            ),
            attempt=attempt,
            root_task_id=root_id,
            retry_of_task_id=original.id,
        )

    async def _resume_task(
        self,
        task_id: str,
        *,
        input_choice: str,
        decider: str,
    ) -> RecoveryOutcome:
        agent = self._agent
        task = await agent.tasks.get_task(task_id)
        if task is None:
            return _outcome("not_found", message=f"Task {task_id} was not found.")
        if task.status in _ACTIVE_STATUSES:
            return _outcome(
                "wrong_state",
                kind="task",
                object_id=task.id,
                task_id=task.id,
                message=f"Task {task.id[:8]} is {task.status}; it does not need resuming.",
            )
        if task.status == "awaiting_approval":
            return _outcome(
                "wrong_state",
                kind="task",
                object_id=task.id,
                task_id=task.id,
                message=(
                    f"Task {task.id[:8]} is awaiting approval; "
                    "approve or reject it instead of resume."
                ),
                suggested=f"omni task approve {task.id[:8]}",
            )
        if task.status in _RETRYABLE_TASK_STATUSES:
            from omni.runtime.task_continue import task_has_research_work

            if task_has_research_work(task):
                return await self._resume_into_live_loop(task)
            return _outcome(
                "checkpoint_required",
                kind="task",
                object_id=task.id,
                task_id=task.id,
                message=(
                    f"Task {task.id[:8]} has no durable checkpoint to resume; "
                    "use retry to create a new attempt."
                ),
                suggested=f"omni task retry {task.id[:8]}",
            )
        if task.status != "needs_input":
            return _outcome(
                "wrong_state",
                kind="task",
                object_id=task.id,
                task_id=task.id,
                message=(
                    f"Task {task.id[:8]} is {task.status}; "
                    "resume only applies to suspended needs_input tasks."
                ),
            )

        store = ActionCheckpointStore(agent.db)
        record = await find_open_checkpoint_for_task(
            store, task_id=task.id, task_recorder=agent.tasks
        )
        if record is None:
            return _outcome(
                "checkpoint_required",
                kind="task",
                object_id=task.id,
                task_id=task.id,
                message=(
                    f"Task {task.id[:8]} is waiting for input but has no open "
                    "clarification checkpoint; retry or answer in the original session."
                ),
                suggested=f"omni task retry {task.id[:8]}",
            )
        if not (input_choice or "").strip():
            candidates = ", ".join(record.candidate_ids) or "am|pm"
            return _outcome(
                "input_required",
                kind="task",
                object_id=task.id,
                task_id=task.id,
                message=(
                    f"Task {task.id[:8]} needs a clarification choice "
                    f"({candidates}). Pass --input <choice>."
                ),
                suggested=(
                    f"omni task resume {task.id[:8]} --input <choice>"
                ),
                checkpoint_id=record.id,
                candidates=list(record.candidate_ids),
            )

        service = ScheduleService(
            agent.db, agent.runtime, agent.settings, registry=agent.registry
        )

        async def _emit(event_type: str, data: dict[str, Any], *, status: str = "info") -> None:
            await agent.tasks.append_event(
                task.id,
                event_type=event_type,
                status=status,
                name="schedule.create",
                output_json=data,
                summary=event_type,
            )

        async def _record(outcome: str, payload: dict[str, Any]) -> None:
            await agent.tasks.append_event(
                task.id,
                event_type="schedule.resolved",
                status=outcome,
                name="schedule_task",
                tool_name="schedule_task",
                output_json=payload,
                summary=str(payload.get("summary") or "")[:400],
            )

        actor = decider or "local"
        # Only the original requester may answer. Local CLI may resume drafts that
        # were themselves created on the local CLI channel — never impersonate an
        # IM principal.
        if record.required_decider and actor != record.required_decider:
            same_local_cli = (
                actor == "local"
                and record.required_decider == "local"
                and (record.channel or "cli") == "cli"
            )
            if not same_local_cli:
                return _outcome(
                    "error",
                    kind="task",
                    object_id=task.id,
                    task_id=task.id,
                    message=(
                        "Only the original requester can answer this clarification "
                        f"(required decider: {record.required_decider})."
                    ),
                    checkpoint_id=record.id,
                )

        result = await resolve_schedule_checkpoint(
            store=store,
            service=service,
            checkpoint_id=record.id,
            choice=input_choice,
            decider=actor,
            emit_action=_emit,
            record_outcome=_record,
        )
        status = str(result.get("status") or "")
        outcome_kind = str(result.get("outcome") or "")

        # run_now / cancel close the draft; settle the suspended Task so it does
        # not linger in needs_input without an open checkpoint.
        if outcome_kind in {"run_now", "cancelled"} or (
            status == "ok" and outcome_kind == "cancelled"
        ):
            summary = str(
                result.get("summary")
                or result.get("message")
                or "Clarification cancelled."
            )
            await agent.tasks.finish_task(
                task.id, status="cancelled", summary=summary
            )
            return _outcome(
                "ok",
                kind="task",
                object_id=task.id,
                task_id=task.id,
                new_id=record.id,
                message=summary,
                checkpoint_id=record.id,
                outcome=outcome_kind or "cancelled",
            )

        if status == "needs_input":
            # Draft still open (e.g. past reading → ask again).
            return _outcome(
                "input_required",
                kind="task",
                object_id=task.id,
                task_id=task.id,
                message=str(result.get("message") or result.get("error") or ""),
                checkpoint_id=record.id,
                **{
                    k: v
                    for k, v in result.items()
                    if k not in {"status", "message", "error"}
                },
            )
        if status == "error":
            # If the checkpoint was already CAS-closed, do not leave the Task
            # suspended with no open draft — settle as failed with the error.
            fresh = await store.get(record.id)
            if fresh is not None and fresh.state != "open":
                summary = str(
                    result.get("error") or "Clarification resolved but scheduling failed."
                )
                await agent.tasks.finish_task(
                    task.id, status="failed", summary=summary, error=summary
                )
            return _outcome(
                "error",
                kind="task",
                object_id=task.id,
                task_id=task.id,
                message=str(result.get("error") or "Could not resume clarification."),
                checkpoint_id=record.id,
            )

        summary = str(result.get("summary") or "Clarification resolved.")
        settle_status = "succeeded"
        if result.get("status") == "rejected" or outcome_kind == "rejected":
            settle_status = "failed"
        await agent.tasks.finish_task(task.id, status=settle_status, summary=summary)
        return _outcome(
            "ok",
            kind="task",
            object_id=task.id,
            task_id=task.id,
            new_id=str(result.get("schedule_id") or record.id),
            message=summary,
            checkpoint_id=record.id,
            schedule_id=str(result.get("schedule_id") or ""),
        )

    # ── skill execution ───────────────────────────────────────────────────

    async def _retry_skill_execution(
        self, execution_id: str, *, notify_channel: str
    ) -> RecoveryOutcome:
        runtime = self._agent.runtime
        execution = await runtime.get_subtask(execution_id)
        if execution is None:
            return _outcome(
                "not_found", message=f"Skill execution {execution_id} was not found."
            )
        if execution.status in _ACTIVE_STATUSES:
            await self._reconcile_lost(
                task_id=execution.task_id or "", execution_id=execution.id
            )
            execution = await runtime.get_subtask(execution_id)
            if execution is None:
                return _outcome(
                    "not_found",
                    message=f"Skill execution {execution_id} was not found.",
                )
        if execution.status in _ACTIVE_STATUSES:
            return _outcome(
                "wrong_state",
                kind="skill_execution",
                object_id=execution.id,
                task_id=execution.task_id or "",
                message=(
                    f"Skill execution {execution.id[:8]} is {execution.status}; "
                    "wait for it to settle before retrying."
                ),
            )
        if execution.status not in _RETRYABLE_EXECUTION_STATUSES:
            return _outcome(
                "wrong_state",
                kind="skill_execution",
                object_id=execution.id,
                task_id=execution.task_id or "",
                message=(
                    f"Skill execution {execution.id[:8]} is {execution.status}; "
                    "it cannot be retried."
                ),
            )
        try:
            new_id = await runtime.retry_subtask(
                execution.id, notify_channel=notify_channel or None
            )
        except ValueError as exc:
            return _outcome(
                "error",
                kind="skill_execution",
                object_id=execution.id,
                task_id=execution.task_id or "",
                message=str(exc),
            )
        if not new_id:
            return _outcome(
                "not_found",
                kind="skill_execution",
                object_id=execution.id,
                message=f"Skill execution {execution_id} could not be retried.",
            )
        return _outcome(
            "ok",
            kind="skill_execution",
            object_id=execution.id,
            task_id=execution.task_id or "",
            new_id=new_id,
            message=f"Created skill execution attempt {new_id[:8]}.",
        )

    async def _resume_skill_execution(self, execution_id: str) -> RecoveryOutcome:
        runtime = self._agent.runtime
        execution = await runtime.get_subtask(execution_id)
        if execution is None:
            return _outcome(
                "not_found", message=f"Skill execution {execution_id} was not found."
            )
        # Workflow-linked executions resume through their stable step (checkpoint).
        if execution.workflow_run_id and execution.workflow_step_id:
            step = await runtime._workflow_step(execution.workflow_step_id)  # noqa: SLF001
            if step is None:
                return _outcome(
                    "not_found",
                    message=f"Workflow step for execution {execution_id[:8]} was not found.",
                )
            return await self._resume_workflow_step(
                execution.workflow_run_id, step.step_key
            )
        # Standalone executions have no durable checkpoint in V1 — redirect.
        return _outcome(
            "checkpoint_required",
            kind="skill_execution",
            object_id=execution.id,
            task_id=execution.task_id or "",
            message=(
                f"Skill execution {execution.id[:8]} has no durable checkpoint; "
                "use requeue to put it back on the recovery queue, or retry for a "
                "fresh attempt."
            ),
            suggested=f"omni task requeue {execution.id[:8]}",
        )

    async def _requeue_skill_execution(self, execution_id: str) -> RecoveryOutcome:
        runtime = self._agent.runtime
        execution = await runtime.get_subtask(execution_id)
        if execution is None:
            return _outcome(
                "not_found", message=f"Skill execution {execution_id} was not found."
            )
        if execution.workflow_run_id:
            return _outcome(
                "wrong_state",
                kind="skill_execution",
                object_id=execution.id,
                task_id=execution.task_id or "",
                message=(
                    f"Skill execution {execution.id[:8]} belongs to a workflow; "
                    "use resume --step or retry --step instead of requeue."
                ),
                suggested=(
                    f"omni task resume {execution.workflow_run_id[:8]} --step <step-id>"
                ),
            )
        if execution.status in _ACTIVE_STATUSES:
            await self._reconcile_lost(
                task_id=execution.task_id or "", execution_id=execution.id
            )
            execution = await runtime.get_subtask(execution_id)
            if execution is None:
                return _outcome(
                    "not_found",
                    message=f"Skill execution {execution_id} was not found.",
                )
        if execution.status in _ACTIVE_STATUSES:
            return _outcome(
                "wrong_state",
                kind="skill_execution",
                object_id=execution.id,
                task_id=execution.task_id or "",
                message=(
                    f"Skill execution {execution.id[:8]} is {execution.status}; "
                    "it does not need requeueing."
                ),
            )
        ok = await runtime.requeue_subtask(execution.id)
        if not ok:
            return _outcome(
                "wrong_state",
                kind="skill_execution",
                object_id=execution.id,
                task_id=execution.task_id or "",
                message=(
                    f"Skill execution {execution.id[:8]} is {execution.status}; "
                    "it is not requeueable."
                ),
                suggested=f"omni task retry {execution.id[:8]}",
            )
        return _outcome(
            "ok",
            kind="skill_execution",
            object_id=execution.id,
            task_id=execution.task_id or "",
            new_id=execution.id,
            message=(
                f"Returned skill execution {execution.id[:8]} to the recovery queue."
            ),
        )

    # ── workflow ──────────────────────────────────────────────────────────

    async def _retry_workflow_step_object(
        self, step_object_id: str, *, notify_channel: str
    ) -> RecoveryOutcome:
        async with self._agent.db.session() as session:
            step = await session.get(WorkflowStepORM, step_object_id)
        if step is None:
            return _outcome(
                "not_found", message=f"Workflow step {step_object_id} was not found."
            )
        return await self._retry_workflow_step(
            step.workflow_run_id, step.step_key, notify_channel=notify_channel
        )

    async def _retry_workflow_step(
        self,
        workflow_run_id: str,
        step_key: str,
        *,
        notify_channel: str,
    ) -> RecoveryOutcome:
        runtime = self._agent.runtime
        workflow = await runtime.get_workflow_run(workflow_run_id)
        if workflow is None:
            return _outcome(
                "not_found", message=f"Workflow {workflow_run_id} was not found."
            )
        new_id = await runtime.retry_workflow_step(
            workflow.id, step_key, notify_channel=notify_channel or None
        )
        if not new_id:
            return _outcome(
                "not_found",
                kind="workflow_run",
                object_id=workflow.id,
                task_id=workflow.task_id,
                message=(
                    f"Workflow step '{step_key}' was not found or produced no "
                    "new execution."
                ),
            )
        return _outcome(
            "ok",
            kind="workflow_run",
            object_id=workflow.id,
            task_id=workflow.task_id,
            new_id=new_id,
            message=(
                f"Created skill execution attempt {new_id[:8]}, "
                f"starting at step {step_key}."
            ),
            step=step_key,
        )

    async def _resume_workflow_step_object(self, step_object_id: str) -> RecoveryOutcome:
        async with self._agent.db.session() as session:
            step = await session.get(WorkflowStepORM, step_object_id)
        if step is None:
            return _outcome(
                "not_found", message=f"Workflow step {step_object_id} was not found."
            )
        return await self._resume_workflow_step(step.workflow_run_id, step.step_key)

    async def _resume_workflow_step(
        self, workflow_run_id: str, step_key: str
    ) -> RecoveryOutcome:
        runtime = self._agent.runtime
        workflow = await runtime.get_workflow_run(workflow_run_id)
        if workflow is None:
            return _outcome(
                "not_found", message=f"Workflow {workflow_run_id} was not found."
            )
        ok = await runtime.resume_workflow_step(workflow.id, step_key)
        if not ok:
            return _outcome(
                "wrong_state",
                kind="workflow_run",
                object_id=workflow.id,
                task_id=workflow.task_id,
                message=(
                    f"Workflow {workflow.id[:8]} step '{step_key}' is "
                    f"{workflow.status}; it does not need resuming or is not recoverable."
                ),
                suggested=f"omni task retry {workflow.id[:8]} --step {step_key}",
            )
        return _outcome(
            "ok",
            kind="workflow_run",
            object_id=workflow.id,
            task_id=workflow.task_id,
            new_id=workflow.id,
            message=(
                f"Returned workflow {workflow.id[:8]} to the recovery queue "
                f"from step {step_key}."
            ),
            step=step_key,
        )

    async def _resume_into_live_loop(self, task: TaskORM) -> RecoveryOutcome:
        """Reopen the same task into ReAct so the model sees its ledger."""
        agent = self._agent
        snapshot = task.input_snapshot_json if isinstance(task.input_snapshot_json, dict) else {}
        try:
            await agent.handle_turn(
                "Continue this task.",
                session_id=str(task.session_id or "") or None,
                channel=str(task.channel or "cli"),
                existing_task_id=task.id,
                origin=str(snapshot.get("origin") or "interactive"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("live-loop resume failed for %s", task.id)
            return _outcome(
                "error",
                kind="task",
                object_id=task.id,
                task_id=task.id,
                message=f"Could not resume task {task.id[:8]} into the live loop: {exc}",
            )
        return _outcome(
            "ok",
            kind="task",
            object_id=task.id,
            task_id=task.id,
            message=f"Resumed task {task.id[:8]} into the live research loop.",
        )


__all__ = [
    "RecoveryOutcome",
    "RecoveryStatus",
    "TaskRecoveryCoordinator",
    "enrich_resolution",
]
