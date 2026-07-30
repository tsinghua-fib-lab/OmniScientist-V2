"""Durable task recorder — one task per user request.

The subtask runtime persists long-running skill work. A task captures the
whole agent turn: the user message, ReAct tool calls, submitted subtasks,
artifacts, and final response. `/task` renders tasks by default and drills
down into subtasks when needed.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, literal, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from omni.core.termination import aggregate_outcome_status
from omni.runtime.task_results import (
    _aware_dt,
    _result_has_artifacts,
    action_required_presentation,
    installation_required_presentation,
)
from omni.runtime.verification import effective_subtasks
from omni.storage.db import Database
from omni.storage.models import (
    ArtifactORM,
    OutboundDeliveryORM,
    SessionORM,
    SubtaskORM,
    TaskControlORM,
    TaskEventORM,
    TaskORM,
    WorkflowRunORM,
    WorkflowStepORM,
    _utcnow,
)

logger = logging.getLogger(__name__)

_ACTIVE_TASK_STATUSES = {"running", "recovering"}
_ACTIVE_EXECUTION_STATUSES = {"scheduled", "pending", "running", "recovering"}
_FAILED_EXECUTION_STATUSES = {"failed", "cancelled", "interrupted"}

# Deletion protection tiers for tasks (user requests):
#   * blocked  — a worker owns the row; refuse in bulk until it settles/reconciles.
#   * protected — carries provenance (results, artifacts, or a pending decision);
#                 deletable only with --force.
#   * everything else (failed / cancelled / interrupted) is deletable by default.
_TASK_BLOCKED_STATUSES = ("running", "recovering")
_TASK_PROTECTED_STATUSES = ("succeeded", "degraded", "needs_input", "awaiting_approval")
_TASK_PRUNABLE_STATUSES = ("failed", "cancelled", "interrupted")

# Intents whose turns are pure conversation/inspection: the planner routes them
# to a direct answer (optionally reading built-in tools) rather than durable
# skill work. Only these are eligible to be filed as ``kind="chat"`` — richer
# intents (memory_update / schedule / single_skill_task / workflow) always
# remain ``turn`` even when they leave no artifact.
_CONVERSATIONAL_INTENTS = {"direct_answer", "react_fallback"}
_STEERABLE_INTENTS = {"react_fallback", "schedule"}


def _process_is_alive(pid: int) -> bool:
    """Best-effort local PID liveness check without signaling the process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    except OSError:
        # Unknown platform-specific probe failures fail closed: retain the
        # short lease instead of stealing a possibly live execution's claim.
        return True
    return True


def _windows_process_is_alive(pid: int) -> bool:
    """Query a Windows process handle without calling destructive ``os.kill``."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        process_query_limited_information = 0x1000
        still_active = 259
        handle = open_process(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            # Access denied means the process exists but cannot be queried.
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == still_active
        finally:
            close_handle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        # Probe failures retain the short lease; they never justify stealing
        # ownership, and—critically—never send a Windows termination signal.
        return True


def _is_conversational_turn(task: TaskORM) -> bool:
    """A no-work direct answer that should stay out of the /task work ledger.

    True only when a top-level ``turn`` produced no durable side effect — no
    submitted subtask/workflow, no artifact, and no schedule — and the planner
    classified it as a conversational intent. Reclassifying such a turn to
    ``kind="chat"`` hides it from the default ``/task`` view without deleting
    it (it stays in the transcript and under ``/task list --kind chat``).
    """
    return (
        (task.kind or "turn") == "turn"
        and not task.schedule_id
        and not task.parent_task_id
        and not task.origin_workflow_run_id
        and not (task.submitted_subtask_ids or task.submitted_workflow_ids or task.artifact_ids)
        and (task.intent_type or "") in _CONVERSATIONAL_INTENTS
    )


@dataclass
class TaskClearOutcome:
    """Result of a task-level clear/prune, split for a transparent preview.

    ``deleted`` counts rows removed (or, in a dry run, rows that *would* be
    removed) grouped by status. ``protected`` and ``blocked`` explain what was
    left behind and why, so the CLI can say exactly who is protected instead of
    a bare ``0 deleted``.
    """

    deleted: dict[str, int] = field(default_factory=dict)
    protected: dict[str, int] = field(default_factory=dict)
    blocked: dict[str, int] = field(default_factory=dict)

    @property
    def deleted_total(self) -> int:
        return sum(self.deleted.values())

    @property
    def protected_total(self) -> int:
        return sum(self.protected.values())

    @property
    def blocked_total(self) -> int:
        return sum(self.blocked.values())


def _clip(value: Any, n: int = 600) -> str:
    text = str(value or "")
    return text if len(text) <= n else text[: n - 1] + "…"


_DEFAULT_EVENT_JSON_LIMIT = 8_000
_PLAN_REVISION_JSON_LIMIT = 256 * 1024
_PLAN_REVISION_METADATA = (
    "revision",
    "revision_id",
    "content_hash",
    "parent_hash",
    "source",
    "stage",
    "finding_ids",
    "diff",
    "catalog_hash",
    "contract_hash",
    "validation_status",
)


def _jsonable(value: Any, *, limit: int = _DEFAULT_EVENT_JSON_LIMIT) -> Any:
    """Return a JSON-friendly payload bounded by its stored JSON bytes."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        normalized = json.loads(encoded)
    except Exception:  # noqa: BLE001
        return _bounded_text_payload(
            {},
            key="repr",
            text=repr(value),
            limit=limit,
        )
    if _stored_json_size(normalized) <= limit:
        return normalized
    return _bounded_text_payload(
        {"truncated": True},
        key="preview",
        text=encoded,
        limit=limit,
    )


def _stored_json_size(value: Any) -> int:
    """Measure bytes using the conservative default JSON storage encoding."""
    return len(json.dumps(value, default=str).encode("utf-8"))


def _bounded_text_payload(
    base: Mapping[str, Any],
    *,
    key: str,
    text: str,
    limit: int,
) -> dict[str, Any]:
    """Fit one text field into a final serialized JSON envelope."""
    empty = {**base, key: ""}
    if _stored_json_size(empty) > limit:
        # Callers keep ``base`` independently bounded; this is a fail-safe for
        # an unexpectedly tiny budget rather than permission to exceed it.
        return {"truncated": True}
    low = 0
    high = len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = {**base, key: text[:midpoint]}
        if _stored_json_size(candidate) <= limit:
            low = midpoint
        else:
            high = midpoint - 1
    return {**base, key: text[:low]}


def _event_jsonable(value: Any, *, event_type: str) -> Any:
    """Keep normal events compact while retaining immutable plan revisions.

    Revision history cannot be reconstructed from ``Task.plan_json`` because
    that projection stores only the latest accepted plan.  A larger, still
    bounded event budget preserves ordinary revision snapshots.  Oversized
    snapshots fail safe while keeping their queryable provenance outside the
    truncated plan preview.
    """
    if not event_type.startswith("plan.revision."):
        return _jsonable(value)
    bounded = _jsonable(value, limit=_PLAN_REVISION_JSON_LIMIT)
    if not (
        isinstance(bounded, dict)
        and bounded.get("truncated") is True
        and isinstance(value, Mapping)
    ):
        return bounded
    metadata = {
        key: _jsonable(value.get(key), limit=16_000)
        for key in _PLAN_REVISION_METADATA
        if key in value
    }
    return _bounded_text_payload(
        {**metadata, "truncated": True},
        key="plan_preview",
        text=str(bounded.get("preview") or ""),
        limit=_PLAN_REVISION_JSON_LIMIT,
    )


def _result_summary(value: Any, error: str = "") -> str:
    if error:
        return _clip(error, 220)
    if isinstance(value, Mapping):
        for key in ("summary", "message", "title", "text", "abstract", "result"):
            if value.get(key):
                return _clip(value.get(key), 220)
        status = value.get("status")
        if status:
            return _clip(status, 220)
    if value not in (None, ""):
        return _clip(value, 220)
    return ""


def _collect_ids(value: Any, key: str) -> list[str]:
    found: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, Mapping):
            raw = obj.get(key)
            if isinstance(raw, list):
                found.extend(str(v) for v in raw if v)
            elif raw:
                found.append(str(raw))
            research = obj.get("research")
            if isinstance(research, Mapping):
                walk(research)
            for nested_key in ("result", "results", "payload"):
                nested = obj.get(nested_key)
                if isinstance(nested, (Mapping, list)):
                    walk(nested)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(value)
    out: list[str] = []
    seen: set[str] = set()
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _event_row(
    task_id: str,
    seq: int,
    payload: Mapping[str, Any],
) -> TaskEventORM:
    """Materialize one event row without performing I/O."""
    event_type = str(payload.get("event_type") or "")
    status = str(payload.get("status") or "")
    tool_name = str(payload.get("tool_name") or "")
    skill_name = str(payload.get("skill_name") or "")
    error = str(payload.get("error") or "")
    output_json = payload.get("output_json")
    now = _utcnow()
    return TaskEventORM(
        task_id=task_id,
        seq=seq,
        event_type=event_type,
        status=status,
        name=str(
            payload.get("name")
            or tool_name
            or skill_name
            or event_type
        ),
        tool_name=tool_name,
        skill_name=skill_name,
        workflow_run_id=str(payload.get("workflow_run_id") or ""),
        workflow_step_id=str(payload.get("workflow_step_id") or ""),
        subtask_id=str(payload.get("subtask_id") or ""),
        step_id=str(payload.get("step_id") or ""),
        input_json=_event_jsonable(
            payload.get("input_json") or {},
            event_type=event_type,
        ),
        output_json=_event_jsonable(
            output_json or {},
            event_type=event_type,
        ),
        error=error,
        summary=str(
            payload.get("summary")
            or _result_summary(output_json, error)
        ),
        pct=payload.get("pct"),
        started_at=now if event_type.endswith(".start") else None,
        finished_at=(
            now
            if event_type.endswith((".done", ".failed"))
            or status in {"succeeded", "failed"}
            else None
        ),
        duration_ms=payload.get("duration_ms"),
    )


def _apply_event_projection(
    task: TaskORM,
    event: TaskEventORM,
    *,
    raw_output: Any,
) -> None:
    """Apply the same task projection update used by a standalone append."""
    if task.status not in _ACTIVE_TASK_STATUSES:
        return
    task.current_stage = event.event_type
    if event.tool_name:
        task.current_tool = event.tool_name
    if event.workflow_run_id:
        task.current_workflow_id = event.workflow_run_id
    if event.subtask_id:
        task.current_subtask_id = event.subtask_id
    if event.summary:
        task.summary = event.summary
    if event.event_type in {"execution.finished", "react.finished"}:
        task.steering_status = "sealed"
    _merge_task_ids(task, raw_output)


def _apply_plan_projection(
    task: TaskORM,
    payload: dict[str, Any],
    *,
    status: str,
    current_authority_fingerprint: str,
) -> None:
    """Update the latest plan and invalidate approval on authority change."""
    authority_unchanged = bool(
        current_authority_fingerprint
        and task.approval_authority_fingerprint
        == current_authority_fingerprint
    )
    task.plan_json = payload
    task.plan_status = status
    task.intent_type = str(payload.get("intent_type") or "")
    if str(task.steering_status or "") != "sealed":
        task.steering_status = (
            "open" if task.intent_type in _STEERABLE_INTENTS else "closed"
        )
    task.provenance_mode = str(payload.get("provenance_mode") or "")
    policy = payload.get("tool_policy")
    task.tool_policy_json = policy if isinstance(policy, dict) else {}
    task.current_stage = f"plan.{status}"
    task.current_authority_fingerprint = current_authority_fingerprint
    if not authority_unchanged:
        task.approval_authority_fingerprint = ""
        task.approved_tools = []


def _log_event_row(event: TaskEventORM) -> None:
    logger.info(
        "task.event task=%s seq=%s type=%s status=%s name=%s workflow=%s "
        "step=%s execution=%s pct=%s summary=%s",
        event.task_id[:8],
        event.seq,
        event.event_type,
        event.status or "-",
        event.name,
        event.workflow_run_id[:8] if event.workflow_run_id else "-",
        event.step_id
        or (
            event.workflow_step_id[:8]
            if event.workflow_step_id
            else "-"
        ),
        event.subtask_id[:8] if event.subtask_id else "-",
        "" if event.pct is None else event.pct,
        _clip(event.summary or event.error, 160),
    )


async def _stage_task_events(
    session: Any,
    task_id: str,
    payloads: list[dict[str, Any]],
) -> list[TaskEventORM]:
    """Stage ordered task events inside the caller's open transaction.

    Control ownership transitions use this helper so the durable state change
    and its audit evidence either commit together or both roll back.
    """
    if not payloads:
        return []
    task = await session.get(TaskORM, task_id)
    if task is None:
        raise LookupError(f"task not found while staging events: {task_id}")
    max_seq = (
        await session.execute(
            select(func.max(TaskEventORM.seq)).where(
                TaskEventORM.task_id == task_id
            )
        )
    ).scalar_one_or_none()
    next_seq = int(max_seq or 0) + 1
    rows: list[TaskEventORM] = []
    for offset, payload in enumerate(payloads):
        event = _event_row(task_id, next_seq + offset, payload)
        session.add(event)
        _apply_event_projection(
            task,
            event,
            raw_output=payload.get("output_json"),
        )
        rows.append(event)
    return rows


class TaskRecorder:
    """Append-only event recorder for user-request tasks."""

    def __init__(
        self,
        db: Database,
        *,
        project: str,
        index: Any | None = None,
        classify_conversational: bool = True,
    ) -> None:
        self._db = db
        self._project = project
        # Optional global task index (``control.sqlite3``). When set, task
        # create/status/archive/delete transitions are mirrored into it so any
        # CLI can list + route to this workspace's tasks. ``None`` ⇒ no-op (the
        # legacy single-workspace behaviour used by tests and headless helpers).
        self._index = index
        # When True, a terminal-succeeded turn that produced no durable work is
        # reclassified to ``kind="chat"`` at settle time so it drops out of the
        # default ``/task`` ledger. False restores the legacy "one task per
        # request" behaviour.
        self._classify_conversational = classify_conversational

    async def _record_index(self, task: TaskORM | None) -> None:
        """Mirror a fresh task row into the global index (best-effort)."""
        if self._index is None or task is None:
            return
        try:
            await self._index.record(task)
        except Exception:  # noqa: BLE001 - the index must never break a task write.
            logger.debug("task index record failed", exc_info=True)

    async def _reindex(self, task_id: str) -> None:
        """Re-read a task after a mutation and refresh its global index row."""
        if self._index is None or not task_id:
            return
        try:
            async with self._db.session() as s:
                task = await s.get(TaskORM, task_id)
            await self._record_index(task)
        except Exception:  # noqa: BLE001
            logger.debug("task index reindex failed for %s", task_id, exc_info=True)

    async def _deindex(self, task_ids: list[str]) -> None:
        """Drop index rows for deleted tasks (best-effort)."""
        if self._index is None or not task_ids:
            return
        try:
            await self._index.remove(task_ids)
        except Exception:  # noqa: BLE001
            logger.debug("task index remove failed", exc_info=True)

    async def create_task(
        self,
        *,
        session_id: str,
        channel: str,
        user_input: str,
        external_key: str = "",
        title: str = "",
        parent_task_id: str = "",
        kind: str = "turn",
        depth: int = 0,
        origin_workflow_run_id: str = "",
        origin_workflow_step_id: str = "",
        schedule_id: str = "",
    ) -> TaskORM:
        title = title or _clip(" ".join(user_input.split()), 80) or "Untitled task"
        if not external_key and session_id:
            external_key = await self._session_external_key(session_id)
        row = TaskORM(
            session_id=session_id,
            parent_task_id=parent_task_id or None,
            origin_workflow_run_id=origin_workflow_run_id,
            origin_workflow_step_id=origin_workflow_step_id,
            schedule_id=schedule_id or "",
            kind=kind or "turn",
            depth=max(0, int(depth)),
            project=self._project,
            channel=channel,
            external_key=external_key,
            status="running",
            title=title,
            user_input=user_input,
            current_stage="user.message",
        )
        async with self._db.session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
        await self.append_event(
            row.id,
            event_type="user.message",
            status="succeeded",
            name="user",
            input_json={"text": user_input},
            summary=_clip(user_input, 220),
        )
        await self.append_event(
            row.id,
            event_type="task.ack",
            status="succeeded",
            name="ack",
            output_json={"task_id": row.id, "status": "planning"},
            summary=f"accepted task {row.id[:8]}",
        )
        if parent_task_id:
            await self.append_event(
                parent_task_id,
                event_type=f"{row.kind}.submitted",
                status="running",
                name=row.kind,
                output_json={
                    "child_task_id": row.id,
                    "kind": row.kind,
                    "depth": row.depth,
                },
                summary=f"submitted {row.kind} task {row.id[:8]}",
            )
        await self._record_index(row)
        logger.info("task.created task=%s channel=%s session=%s title=%s", row.id[:8], channel, session_id[:8], title)
        return row

    async def record_plan(
        self,
        task_id: str,
        plan: Any,
        *,
        status: str = "created",
        emit_event: bool = True,
        current_authority_fingerprint: str = "",
    ) -> None:
        """Update the latest-plan projection and optionally emit its legacy event.

        New revision-aware callers emit explicit ``plan.revision.*`` audit
        events and one final ``plan.validated`` event.  The default remains
        ``True`` so existing extensions keep the historic ``plan.<status>``
        event contract.
        """
        if not task_id:
            return
        payload = plan.to_dict() if hasattr(plan, "to_dict") else _jsonable(plan)
        projection = (
            payload if isinstance(payload, dict) else {"plan": payload}
        )
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id)
            if task is not None:
                _apply_plan_projection(
                    task,
                    projection,
                    status=status,
                    current_authority_fingerprint=(
                        current_authority_fingerprint
                    ),
                )
            await s.commit()
        if emit_event:
            await self.append_event(
                task_id,
                event_type=f"plan.{status}",
                status=(
                    "succeeded"
                    if status in {"created", "validated", "executed"}
                    else status
                ),
                name="plan",
                output_json=payload,
                summary=(
                    f"plan {status}: "
                    f"{str((payload or {}).get('intent_type') or '')}"
                ),
            )

    async def record_plan_transition(
        self,
        task_id: str,
        plan: Any,
        *,
        status: str,
        events: list[dict[str, Any]],
        current_authority_fingerprint: str = "",
    ) -> None:
        """Atomically update the plan projection and append ordered audit events."""
        if not task_id:
            return
        payload = (
            plan.to_dict()
            if hasattr(plan, "to_dict")
            else _jsonable(plan)
        )
        projection = (
            payload if isinstance(payload, dict) else {"plan": payload}
        )
        rows: list[TaskEventORM] = []
        async with self._db.session() as session:
            task = await session.get(TaskORM, task_id)
            if task is None:
                return
            _apply_plan_projection(
                task,
                projection,
                status=status,
                current_authority_fingerprint=(
                    current_authority_fingerprint
                ),
            )
            max_seq = (
                await session.execute(
                    select(func.max(TaskEventORM.seq)).where(
                        TaskEventORM.task_id == task_id
                    )
                )
            ).scalar_one_or_none()
            next_seq = int(max_seq or 0) + 1
            for offset, event_payload in enumerate(events):
                row = _event_row(
                    task_id,
                    next_seq + offset,
                    event_payload,
                )
                rows.append(row)
                session.add(row)
                _apply_event_projection(
                    task,
                    row,
                    raw_output=event_payload.get("output_json"),
                )
            await session.commit()
        for row in rows:
            _log_event_row(row)

    async def append_event(
        self,
        task_id: str,
        *,
        event_type: str,
        status: str = "",
        name: str = "",
        tool_name: str = "",
        skill_name: str = "",
        workflow_run_id: str = "",
        workflow_step_id: str = "",
        subtask_id: str = "",
        step_id: str = "",
        input_json: Any | None = None,
        output_json: Any | None = None,
        error: str = "",
        summary: str = "",
        pct: float | None = None,
        duration_ms: float | None = None,
    ) -> TaskEventORM | None:
        if not task_id:
            return None
        payload = {
            "event_type": event_type,
            "status": status,
            "name": name,
            "tool_name": tool_name,
            "skill_name": skill_name,
            "workflow_run_id": workflow_run_id,
            "workflow_step_id": workflow_step_id,
            "subtask_id": subtask_id,
            "step_id": step_id,
            "input_json": input_json,
            "output_json": output_json,
            "error": error,
            "summary": summary,
            "pct": pct,
            "duration_ms": duration_ms,
        }
        async with self._db.session() as s:
            max_seq = (
                await s.execute(select(func.max(TaskEventORM.seq)).where(TaskEventORM.task_id == task_id))
            ).scalar_one_or_none()
            seq = int(max_seq or 0) + 1
            event = _event_row(task_id, seq, payload)
            s.add(event)
            task = await s.get(TaskORM, task_id)
            if task is not None:
                _apply_event_projection(
                    task,
                    event,
                    raw_output=output_json,
                )
            await s.commit()
            await s.refresh(event)
        _log_event_row(event)
        return event

    async def link_workflow(self, task_id: str, workflow_run_id: str) -> None:
        """Attach one durable workflow run to its user-request task."""
        if not task_id or not workflow_run_id:
            return
        async with self._db.session() as session:
            run = await session.get(WorkflowRunORM, workflow_run_id)
            task = await session.get(TaskORM, task_id)
            if run is not None:
                run.task_id = task_id
            if task is not None:
                ids = list(task.submitted_workflow_ids or [])
                if workflow_run_id not in ids:
                    ids.append(workflow_run_id)
                task.submitted_workflow_ids = ids
                task.current_workflow_id = workflow_run_id
            await session.commit()

    async def record_workflow_submitted(
        self,
        task_id: str,
        *,
        workflow_run_id: str,
        goal: str,
        steps: list[dict[str, Any]],
    ) -> None:
        event = await self.append_event(
            task_id,
            event_type="workflow.submitted",
            status="pending",
            name="workflow",
            workflow_run_id=workflow_run_id,
            input_json={"goal": goal, "steps": steps},
            output_json={
                "workflow_run_id": workflow_run_id,
                "step_count": len(steps),
            },
            summary=f"submitted workflow {workflow_run_id[:8]} with {len(steps)} step(s)",
        )
        _ = event
        await self.link_workflow(task_id, workflow_run_id)

    async def link_subtask(self, task_id: str, subtask_id: str, *, event_id: str = "") -> None:
        if not task_id or not subtask_id:
            return
        async with self._db.session() as s:
            subtask = await s.get(SubtaskORM, subtask_id)
            task = await s.get(TaskORM, task_id)
            if subtask is not None:
                subtask.task_id = task_id
                subtask.parent_event_id = event_id
            if task is not None:
                ids = list(task.submitted_subtask_ids or [])
                if subtask_id not in ids:
                    ids.append(subtask_id)
                task.submitted_subtask_ids = ids
                task.current_subtask_id = subtask_id
            await s.commit()

    async def record_subtask_submitted(
        self,
        task_id: str,
        *,
        subtask_id: str,
        skill_name: str,
        input_json: Any,
        mode: str = "background",
        workflow_run_id: str = "",
        workflow_step_id: str = "",
    ) -> None:
        event = await self.append_event(
            task_id,
            event_type="subtask.submitted",
            status="pending",
            name=skill_name,
            skill_name=skill_name,
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_id,
            subtask_id=subtask_id,
            input_json=input_json,
            output_json={"subtask_id": subtask_id, "skill_name": skill_name, "mode": mode},
            summary=f"submitted {skill_name}",
        )
        await self.link_subtask(task_id, subtask_id, event_id=event.id if event is not None else "")

    async def finish_task(self, task_id: str, *, status: str, summary: str = "", error: str = "") -> None:
        """Settle a terminal task through verification.

        ``finish_task`` remains the compatibility API used by commands and
        extensions. Successful, degraded, and failed completions all flow
        through :meth:`settle_task`; only an external cancellation bypasses
        content verification because it is itself the terminal decision.
        """
        if status == "cancelled":
            await self._finish_task_unchecked(
                task_id,
                status=status,
                summary=summary,
                error=error,
            )
            return
        if status == "needs_input":
            await self.mark_needs_input(task_id, summary=summary)
            return
        await self.settle_task(
            task_id,
            proposed_status=status,
            summary=summary,
            error=error,
        )

    async def _finish_task_unchecked(
        self,
        task_id: str,
        *,
        status: str,
        summary: str = "",
        error: str = "",
    ) -> None:
        """Persist a terminal decision already made by the verifier or operator."""
        if not task_id:
            return
        parent_task_id = ""
        kind = "turn"
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id)
            if task is None:
                return
            parent_task_id = task.parent_task_id or ""
            kind = task.kind or "turn"
            task.status = status
            task.steering_status = "sealed"
            if (
                self._classify_conversational
                and status == "succeeded"
                and _is_conversational_turn(task)
            ):
                task.kind = "chat"
            task.summary = summary or task.summary
            task.error = (error or task.error) if status in {"failed", "degraded"} else error
            task.current_stage = f"task.{status}"
            task.finished_at = _utcnow()
            await s.commit()
        await self.append_event(
            task_id,
            event_type=f"task.{status}",
            status=status,
            name="task",
            output_json={"status": status, "summary": summary, "error": error},
            summary=summary,
            error=error,
        )
        if parent_task_id:
            await self.append_event(
                parent_task_id,
                event_type=f"{kind}.finished",
                status=status,
                name=kind,
                output_json={
                    "child_task_id": task_id,
                    "kind": kind,
                    "status": status,
                    "summary": summary,
                },
                error=error,
                summary=f"{kind} task {task_id[:8]} {status}",
            )
        await self._reindex(task_id)
        logger.info("task.finished task=%s status=%s summary=%s", task_id[:8], status, _clip(summary or error, 180))

    async def settle_task(
        self,
        task_id: str,
        *,
        proposed_status: str,
        summary: str = "",
        error: str = "",
    ) -> str:
        """Verify an otherwise complete task before committing its terminal state."""
        verification = await self.verify_task(task_id)
        if verification == "pending":
            return "running"
        verification_outcome = (
            "failed" if verification == "failed" else
            "degraded" if verification == "degraded" else
            "succeeded"
        )
        status = aggregate_outcome_status(proposed_status, verification_outcome)
        final_error = error
        if status == "failed" and proposed_status not in {"cancelled", "needs_input"}:
            final_error = error or "task verification failed; inspect verification.failed"
        await self._finish_task_unchecked(
            task_id,
            status=status,
            summary=summary,
            error=final_error,
        )
        return status

    async def mark_needs_input(self, task_id: str, *, summary: str = "", missing_inputs: Any | None = None) -> None:
        """Pause a task while waiting for the user to supply required inputs."""
        if not task_id:
            return
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id)
            if task is None:
                return
            task.status = "needs_input"
            task.steering_status = "closed"
            task.summary = summary or task.summary
            task.error = ""
            task.current_stage = "task.needs_input"
            task.finished_at = None
            await s.commit()
        await self.append_event(
            task_id,
            event_type="task.needs_input",
            status="needs_input",
            name="task",
            output_json={"status": "needs_input", "missing_inputs": missing_inputs or []},
            summary=summary,
        )
        await self._reindex(task_id)

    async def mark_awaiting_approval(
        self,
        task_id: str,
        *,
        summary: str = "",
        authority_fingerprint: str,
        expected_plan_json: dict[str, Any],
    ) -> bool:
        """Atomically bind an approval request to the current execution authority."""
        if not task_id:
            return False
        async with self._db.session() as s:
            values: dict[str, Any] = {
                "status": "awaiting_approval",
                "plan_status": "awaiting_approval",
                "error": "",
                "current_stage": "task.awaiting_approval",
                "finished_at": None,
                "approval_authority_fingerprint": authority_fingerprint,
                # A new review always replaces prior consent.  No sensitive
                # grant exists until the exact fingerprint wins the claim CAS.
                "approved_tools": [],
            }
            if summary:
                values["summary"] = summary
            result = await s.execute(
                update(TaskORM)
                .where(
                    TaskORM.id == task_id,
                    TaskORM.current_authority_fingerprint
                    == authority_fingerprint,
                    TaskORM.plan_json == expected_plan_json,
                )
                .values(**values)
            )
            await s.commit()
        bound = bool(result.rowcount == 1)
        if not bound:
            return False
        await self.append_event(
            task_id,
            event_type="task.awaiting_approval",
            status="awaiting_approval",
            name="task",
            output_json={"status": "awaiting_approval"},
            summary=summary or "validated plan is waiting for approval",
        )
        await self._reindex(task_id)
        return True

    async def mark_running(self, task_id: str, *, summary: str = "") -> None:
        """Resume an awaiting/recovering task without creating a second task."""
        if not task_id:
            return
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id)
            if task is None:
                return
            previous_status = task.status
            task.status = "running"
            task.steering_status = (
                "open"
                if str(task.intent_type or "") in _STEERABLE_INTENTS
                else "closed"
            )
            task.current_stage = "task.resumed"
            task.finished_at = None
            if summary:
                task.summary = summary
            await s.commit()
        await self.append_event(
            task_id,
            event_type="task.resumed",
            status="running",
            name="task",
            output_json={"previous_status": previous_status},
            summary=summary or f"task resumed from {previous_status}",
        )
        await self._reindex(task_id)

    async def claim_plan_approval(
        self,
        task_id: str,
        *,
        authority_fingerprint: str,
        expected_plan_json: dict[str, Any],
        approved_tools: list[str],
    ) -> bool:
        """Claim one unchanged approval snapshot and install its exact grants."""
        if not task_id:
            return False
        exact_grants = sorted(
            {str(name).strip() for name in approved_tools if str(name).strip()}
        )
        async with self._db.session() as s:
            result = await s.execute(
                update(TaskORM)
                .where(
                    TaskORM.id == task_id,
                    TaskORM.status == "awaiting_approval",
                    TaskORM.current_authority_fingerprint
                    == authority_fingerprint,
                    TaskORM.approval_authority_fingerprint
                    == authority_fingerprint,
                    TaskORM.plan_json == expected_plan_json,
                )
                .values(
                    status="running",
                    current_stage="plan.approval.claimed",
                    finished_at=None,
                    approved_tools=exact_grants,
                )
            )
            await s.commit()
        claimed = bool(result.rowcount == 1)
        if claimed:
            await self._reindex(task_id)
        return claimed

    async def grant_tools(self, task_id: str, tools: list[str], *, reason: str) -> list[str]:
        """Add durable grants outside plan approval (for example schedules).

        Human plan approval does not use this additive API; its CAS installs
        the exact reviewed grant set by replacement.
        """
        requested = sorted({str(name).strip() for name in tools if str(name).strip()})
        if not task_id:
            return []
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id)
            if task is None:
                return []
            granted = sorted({*(task.approved_tools or []), *requested})
            task.approved_tools = granted
            await s.commit()
        await self.append_event(
            task_id,
            event_type="approval.task.granted",
            status="succeeded",
            name="run_approval",
            output_json={"approved_tools": granted, "reason": reason},
            summary=f"task approved for {len(granted)} declared sensitive tool(s)",
        )
        return granted

    async def try_request_control(
        self,
        task_id: str,
        *,
        action: str,
        instruction: str = "",
    ) -> TaskControlORM | None:
        """Atomically enqueue a control only while its task is active.

        The active-state predicate and insert share one SQL statement. This
        gives the foreground REPL a reliable race result: ``None`` means the
        turn reached a terminal state before the steer/cancel was accepted, so
        user text can be queued for the next turn exactly once.
        """
        action = action.strip().lower()
        if action not in {"steer", "cancel"}:
            raise ValueError("task control action must be steer or cancel")
        task = await self.get_task(task_id)
        if task is None:
            return None
        if action == "steer" and not instruction.strip():
            raise ValueError("steer instruction is required")
        control_id = uuid.uuid4().hex
        created_at = _utcnow()
        events: list[TaskEventORM] = []
        async with self._db.session() as s:
            predicates = [
                TaskORM.id == task.id,
                TaskORM.status.in_(_ACTIVE_TASK_STATUSES),
            ]
            if action == "steer":
                predicates.append(TaskORM.steering_status == "open")
            accepted = await s.execute(
                sqlite_insert(TaskControlORM).from_select(
                    ("id", "task_id", "action", "instruction", "status", "created_at"),
                    select(
                        literal(control_id),
                        TaskORM.id,
                        literal(action),
                        literal(instruction.strip()),
                        literal("pending"),
                        literal(created_at),
                    ).where(*predicates),
                )
            )
            if int(accepted.rowcount or 0) != 1:
                await s.rollback()
                return None
            row = await s.get(TaskControlORM, control_id)
            if row is None:  # pragma: no cover - insert and PK lookup share a transaction
                raise RuntimeError("accepted control row was not materialized")
            events = await _stage_task_events(
                s,
                task.id,
                [
                    {
                        "event_type": "task.control.requested",
                        "status": "pending",
                        "name": action,
                        "input_json": {
                            "action": action,
                            "instruction": instruction.strip(),
                        },
                        "summary": f"{action} requested",
                    }
                ],
            )
            await s.commit()
        for event in events:
            _log_event_row(event)
        return row

    async def request_control(
        self,
        task_id: str,
        *,
        action: str,
        instruction: str = "",
    ) -> TaskControlORM:
        """Compatibility API that raises when an active task cannot accept control."""
        row = await self.try_request_control(
            task_id,
            action=action,
            instruction=instruction,
        )
        if row is not None:
            return row
        task = await self.get_task(task_id)
        if task is None:
            raise LookupError(f"task not found: {task_id}")
        if action == "steer":
            rejection = await self.steer_rejection_reason(task.id)
            if rejection:
                raise ValueError(rejection)
        raise ValueError(f"task {task.id[:8]} is not active ({task.status})")

    async def consume_controls(
        self,
        task_id: str,
        *,
        actions: set[str] | frozenset[str] | None = None,
    ) -> list[dict[str, str]]:
        """Claim selected pending controls in creation order with a status CAS.

        Deterministic runtimes may consume only ``cancel`` while leaving
        natural-language ``steer`` rows pending for an enclosing semantic loop.
        """
        if not task_id:
            return []
        requested_actions = (
            {"steer", "cancel"} if actions is None else actions
        )
        allowed_actions = {
            str(action).strip().lower()
            for action in requested_actions
            if str(action).strip().lower() in {"steer", "cancel"}
        }
        if not allowed_actions:
            return []
        events: list[TaskEventORM] = []
        async with self._db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(TaskControlORM)
                        .where(
                            TaskControlORM.task_id == task_id,
                            TaskControlORM.status == "pending",
                            TaskControlORM.action.in_(allowed_actions),
                        )
                        .order_by(TaskControlORM.created_at.asc())
                    )
                ).scalars().all()
            )
            now = _utcnow()
            consumer_pid = os.getpid()
            controls: list[dict[str, str]] = []
            for row in rows:
                claimed = await s.execute(
                    update(TaskControlORM)
                    .where(
                        TaskControlORM.id == row.id,
                        TaskControlORM.status == "pending",
                        TaskControlORM.action.in_(allowed_actions),
                    )
                    .values(
                        status="consumed",
                        consumed_at=now,
                        consumer_pid=consumer_pid,
                    )
                )
                if int(claimed.rowcount or 0) == 1:
                    controls.append(
                        {
                            "id": row.id,
                            "action": row.action,
                            "instruction": row.instruction,
                        }
                    )
            events = await _stage_task_events(
                s,
                task_id,
                [
                    {
                        "event_type": "task.control.consumed",
                        "status": "succeeded",
                        "name": control["action"],
                        "output_json": {
                            **control,
                            "consumer_pid": consumer_pid,
                        },
                        "summary": f"{control['action']} consumed",
                    }
                    for control in controls
                ],
            )
            await s.commit()
        for event in events:
            _log_event_row(event)
        return controls

    async def mark_controls_applied(self, control_ids: list[str]) -> None:
        """Acknowledge controls only after a runtime boundary applied them."""
        ids = [str(item) for item in control_ids if str(item)]
        if not ids:
            return
        applied: list[tuple[str, str, str]] = []
        events: list[TaskEventORM] = []
        async with self._db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(TaskControlORM).where(
                            TaskControlORM.id.in_(ids),
                            TaskControlORM.status == "consumed",
                        )
                    )
                ).scalars().all()
            )
            for row in rows:
                acknowledged = await s.execute(
                    update(TaskControlORM)
                    .where(
                        TaskControlORM.id == row.id,
                        TaskControlORM.status == "consumed",
                    )
                    .values(status="applied")
                )
                if int(acknowledged.rowcount or 0) == 1:
                    applied.append((row.task_id, row.id, row.action))
            by_task: dict[str, list[dict[str, Any]]] = {}
            for task_id, control_id, action in applied:
                by_task.setdefault(task_id, []).append(
                    {
                        "event_type": "task.control.applied",
                        "status": "succeeded",
                        "name": action,
                        "output_json": {
                            "id": control_id,
                            "action": action,
                        },
                        "summary": (
                            f"{action} applied at an execution boundary"
                        ),
                    }
                )
            for task_id, payloads in by_task.items():
                events.extend(
                    await _stage_task_events(s, task_id, payloads)
                )
            await s.commit()
        for event in events:
            _log_event_row(event)

    async def requeue_unapplied_control(self, control_id: str) -> bool:
        """Atomically transfer one undelivered steer out of its finishing task.

        The status transition is the ownership decision: only the caller that
        wins ``pending|consumed -> requeued`` may place the instruction in the
        foreground next-turn queue. A resumed old task consumes only ``pending``
        rows, so it cannot deliver the same steer a second time.
        """
        if not control_id:
            return False
        transitioned: tuple[str, str] | None = None
        events: list[TaskEventORM] = []
        async with self._db.session() as s:
            row = await s.get(TaskControlORM, control_id)
            if row is None or row.action != "steer":
                return False
            changed = await s.execute(
                update(TaskControlORM)
                .where(
                    TaskControlORM.id == control_id,
                    TaskControlORM.action == "steer",
                    TaskControlORM.status.in_(("pending", "consumed")),
                )
                .values(status="requeued")
            )
            if int(changed.rowcount or 0) == 1:
                transitioned = (row.task_id, row.action)
                events = await _stage_task_events(
                    s,
                    row.task_id,
                    [
                        {
                            "event_type": "task.control.requeued",
                            "status": "succeeded",
                            "name": row.action,
                            "output_json": {
                                "id": control_id,
                                "action": row.action,
                            },
                            "summary": (
                                "unapplied steer transferred to the "
                                "next-turn queue"
                            ),
                        }
                    ],
                )
            await s.commit()
        if transitioned is None:
            return False
        for event in events:
            _log_event_row(event)
        return True

    async def recover_consumed_controls(
        self,
        task_id: str,
        *,
        stale_after_s: float = 30.0,
    ) -> int:
        """Return stale, unacknowledged claims to ``pending`` on task resume.

        ``consumed`` means a prior process claimed the row but never durably
        acknowledged a semantic delivery. A dead consumer PID is recoverable
        immediately; a live or legacy owner must exceed the time lease first,
        so a second worker cannot steal a live process's in-memory steer.
        Recovery is intentionally at-least-once after a hard process crash;
        graceful foreground settlement remains exactly-once. Applied and
        foreground-requeued rows are terminal and remain untouched.
        """
        if not task_id:
            return 0
        cutoff = _utcnow() - timedelta(
            seconds=max(0.0, float(stale_after_s))
        )
        recovered_ids: list[str] = []
        events: list[TaskEventORM] = []
        async with self._db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(TaskControlORM).where(
                            TaskControlORM.task_id == task_id,
                            TaskControlORM.status == "consumed",
                            TaskControlORM.consumed_at.is_not(None),
                        )
                    )
                ).scalars().all()
            )
            for row in rows:
                consumed_at = _aware_dt(row.consumed_at)
                owner_dead = bool(
                    row.consumer_pid
                    and not _process_is_alive(int(row.consumer_pid))
                )
                if not owner_dead and (
                    consumed_at is None or consumed_at > cutoff
                ):
                    continue
                recovered = await s.execute(
                    update(TaskControlORM)
                    .execution_options(synchronize_session=False)
                    .where(
                        TaskControlORM.id == row.id,
                        TaskControlORM.status == "consumed",
                        TaskControlORM.consumed_at.is_not(None),
                        TaskControlORM.consumed_at == row.consumed_at,
                        TaskControlORM.consumer_pid == row.consumer_pid,
                    )
                    .values(
                        status="pending",
                        consumed_at=None,
                        consumer_pid=0,
                    )
                )
                if int(recovered.rowcount or 0) == 1:
                    recovered_ids.append(row.id)
            events = await _stage_task_events(
                s,
                task_id,
                [
                    {
                        "event_type": "task.control.recovered",
                        "status": "succeeded",
                        "name": "control",
                        "output_json": {"id": control_id},
                        "summary": (
                            "unacknowledged control recovered for resumed "
                            "execution"
                        ),
                    }
                    for control_id in recovered_ids
                ],
            )
            await s.commit()
        for event in events:
            _log_event_row(event)
        return len(recovered_ids)

    async def control_status(self, control_id: str) -> str:
        """Return one durable control's lifecycle status."""
        if not control_id:
            return ""
        async with self._db.session() as s:
            row = await s.get(TaskControlORM, control_id)
            return str(row.status or "") if row is not None else ""

    async def steer_rejection_reason(self, task_id: str) -> str:
        """Explain why a detached steer cannot reach a semantic boundary.

        Foreground TUI steering has an in-memory next-turn fallback. Detached
        ``task steer`` commands do not, so they may target only an active ReAct
        or schedule turn that can actually drain natural-language steering.
        """
        task = await self.get_task(task_id)
        if task is None:
            return f"task not found: {task_id}"
        if task.status not in _ACTIVE_TASK_STATUSES:
            return (
                f"Task `{task.id[:8]}` is `{task.status}` and cannot be "
                "steered."
            )
        intent_type = str(task.intent_type or "")
        if not intent_type:
            return (
                f"Task `{task.id[:8]}` is still planning, so detached "
                "steering cannot guarantee delivery. Submit a follow-up task "
                "instead."
            )
        if intent_type not in _STEERABLE_INTENTS:
            return (
                f"Task `{task.id[:8]}` uses the deterministic "
                f"`{intent_type}` runner, which supports cancellation but has "
                "no model steering boundary. Submit a follow-up task instead."
            )
        if str(task.steering_status or "") != "open":
            return (
                f"Task `{task.id[:8]}` has passed its final steering boundary. "
                "Submit a follow-up task instead."
            )
        return ""

    async def claim_delivery(
        self,
        delivery_key: str,
        *,
        task_id: str = "",
        object_kind: str = "skill_execution",
        object_id: str = "",
        subtask_id: str = "",
        channel: str,
        external_key: str,
        kind: str,
        lease_seconds: int = 300,
    ) -> bool:
        """Atomically claim one outbound send, retrying failures or expired claims.

        This closes duplicate sends across concurrent daemon workers. A stale
        ``sending`` claim is recoverable after a lease because a process may
        have died before settling it.
        """
        if not delivery_key:
            return False
        now = _utcnow()
        async with self._db.session() as s:
            insert_result = await s.execute(
                sqlite_insert(OutboundDeliveryORM)
                .values(
                    delivery_key=delivery_key,
                    task_id=task_id,
                    object_kind=object_kind,
                    object_id=object_id or subtask_id,
                    subtask_id=subtask_id,
                    channel=channel,
                    external_key=external_key,
                    kind=kind,
                    status="sending",
                    attempts=1,
                    error="",
                    claimed_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["delivery_key"])
            )
            if int(insert_result.rowcount or 0) == 1:
                await s.commit()
                return True
            cutoff = now - timedelta(seconds=max(1, lease_seconds))
            retry_result = await s.execute(
                update(OutboundDeliveryORM)
                .where(
                    OutboundDeliveryORM.delivery_key == delivery_key,
                    or_(
                        OutboundDeliveryORM.status == "failed",
                        (
                            (OutboundDeliveryORM.status == "sending")
                            & (OutboundDeliveryORM.updated_at < cutoff)
                        ),
                    ),
                )
                .values(
                    status="sending",
                    attempts=OutboundDeliveryORM.attempts + 1,
                    error="",
                    claimed_at=now,
                    updated_at=now,
                )
            )
            await s.commit()
            return int(retry_result.rowcount or 0) == 1

    async def finish_delivery(self, delivery_key: str, *, status: str, error: str = "") -> None:
        """Settle a claimed delivery as sent, degraded, or failed."""
        if not delivery_key:
            return
        if status not in {"sent", "degraded", "failed"}:
            raise ValueError(f"unsupported delivery status: {status}")
        async with self._db.session() as s:
            await s.execute(
                update(OutboundDeliveryORM)
                .where(OutboundDeliveryORM.delivery_key == delivery_key)
                .values(status=status, error=error, updated_at=_utcnow())
            )
            await s.commit()

    async def reopen_task_for_recovery(self, task_id: str, *, subtask_id: str = "", reason: str = "") -> None:
        """Move a settled task back into a recoverable active state."""
        if not task_id:
            return
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id)
            if task is None:
                return
            previous_status = task.status
            task.status = "recovering"
            task.steering_status = (
                "open"
                if str(task.intent_type or "") in _STEERABLE_INTENTS
                else "closed"
            )
            task.current_stage = "task.recovering"
            task.current_subtask_id = subtask_id or task.current_subtask_id
            task.finished_at = None
            await s.commit()
        await self.append_event(
            task_id,
            event_type="task.recovering",
            status="recovering",
            name="task",
            subtask_id=subtask_id,
            output_json={"previous_status": previous_status, "subtask_id": subtask_id, "reason": reason},
            summary=reason or "task reopened for recovery",
        )
        await self._reindex(task_id)

    async def refresh_from_executions(self, task_id: str) -> None:
        """Settle a task from workflow, direct execution, and child-task outcomes."""
        if not task_id:
            return
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id)
            if task is None or task.status not in _ACTIVE_TASK_STATUSES:
                return
            workflow_ids = [str(v) for v in (task.submitted_workflow_ids or []) if v]
            execution_ids = [str(v) for v in (task.submitted_subtask_ids or []) if v]
            workflows = list(
                (
                    await s.execute(
                        select(WorkflowRunORM).where(WorkflowRunORM.id.in_(workflow_ids))
                    )
                ).scalars().all()
            ) if workflow_ids else []
            executions = list(
                (
                    await s.execute(select(SubtaskORM).where(SubtaskORM.id.in_(execution_ids)))
                ).scalars().all()
            ) if execution_ids else []
            child_tasks = list(
                (
                    await s.execute(
                        select(TaskORM).where(TaskORM.parent_task_id == task_id)
                    )
                ).scalars().all()
            )
            if not workflows and not executions and not child_tasks:
                return
        # Workflow executions are represented by their aggregate WorkflowRun.
        # Only standalone skill executions participate separately here.
        direct_executions = effective_subtasks(
            [execution for execution in executions if not execution.workflow_run_id]
        )
        # A child task created by a workflow step is already represented in the
        # WorkflowRun aggregate. Count only independently delegated child tasks
        # here so one logical branch cannot inflate the parent summary twice.
        direct_child_tasks = [child for child in child_tasks if not child.origin_workflow_run_id]
        components: list[Any] = [*workflows, *direct_executions, *direct_child_tasks]
        if any(component.status in _ACTIVE_EXECUTION_STATUSES for component in components):
            return
        failed_results = [
            getattr(component, "result_json", {})
            for component in components
            if component.status == "failed"
        ]
        required_action = action_required_presentation(failed_results)
        if required_action is not None and not any(
            component.status in {"succeeded", "degraded"} for component in components
        ):
            await self.mark_needs_input(task_id, summary=required_action[0])
            return
        installation_required = installation_required_presentation(failed_results)
        if any(component.status == "cancelled" for component in components):
            await self._finish_task_unchecked(
                task_id,
                status="cancelled",
                summary="Execution was cancelled; completed child results were preserved.",
            )
            return
        if any(component.status == "interrupted" for component in components):
            await self._finish_task_unchecked(
                task_id,
                status="interrupted",
                summary="Execution was interrupted; completed child results were preserved.",
            )
            return
        failed = [c for c in components if c.status in _FAILED_EXECUTION_STATUSES]
        degraded = [c for c in components if c.status == "degraded"]
        succeeded = [c for c in components if c.status == "succeeded"]
        if failed and (succeeded or degraded):
            status = "degraded"
            summary = _execution_summary(components, succeeded, degraded, failed)
            error = "; ".join(_clip(c.error, 120) for c in failed if c.error)
        elif failed:
            qa_delivered = str(task.intent_type or "") == "qa_plus_artifact"
            status = "degraded" if qa_delivered else "failed"
            if installation_required is not None:
                install_summary = installation_required[0]
                summary = (
                    f"The answer was delivered, but a Skill component is missing. {install_summary}"
                    if qa_delivered
                    else install_summary
                )
            else:
                summary = (
                    f"The answer was delivered, but {len(failed)}/{len(components)} execution(s) failed."
                    if qa_delivered else
                    f"{len(failed)}/{len(components)} execution(s) failed."
                )
            error = "; ".join(_clip(c.error, 120) for c in failed if c.error)
        elif degraded:
            status = "degraded"
            summary = _execution_summary(components, succeeded, degraded, failed)
            error = "; ".join(_clip(c.error, 120) for c in degraded if c.error)
        else:
            status = "succeeded"
            summary = f"{len(succeeded)}/{len(components)} execution(s) succeeded."
            error = ""
        await self.settle_task(
            task_id,
            proposed_status=status,
            summary=summary,
            error=error,
        )

    async def refresh_from_subtasks(self, task_id: str) -> None:
        """Compatibility name for callers; aggregation now spans all executions."""
        await self.refresh_from_executions(task_id)

    async def verify_task(self, task_id: str) -> str:
        """Compatibility entry point; VerificationRunner owns the checks."""
        from omni.runtime.verification import VerificationRunner

        return await VerificationRunner(self).verify(task_id)

    async def get_task(self, task_id: str) -> TaskORM | None:
        task_id = (task_id or "").strip()
        if not task_id:
            return None
        async with self._db.session() as s:
            exact = await s.get(TaskORM, task_id)
            if exact is not None:
                return exact
            rows = (
                await s.execute(
                    select(TaskORM)
                    .where(TaskORM.id.startswith(task_id, autoescape=True))
                    .limit(2)
                )
            ).scalars().all()
        return rows[0] if len(rows) == 1 else None

    async def list_tasks(
        self,
        *,
        limit: int = 30,
        status: str | None = None,
        include_archived: bool = False,
        kind: str | None = None,
    ) -> list[TaskORM]:
        """List recent tasks; ``kind`` filters turn / subagent / maintenance rows."""
        async with self._db.session() as s:
            q = select(TaskORM).order_by(TaskORM.created_at.desc()).limit(limit)
            if status:
                q = q.where(TaskORM.status == status)
            if kind:
                q = q.where(TaskORM.kind == kind)
            if not include_archived:
                q = q.where(TaskORM.archived_at.is_(None))
            return list((await s.execute(q)).scalars().all())

    async def active_task_for_session(self, session_id: str) -> TaskORM | None:
        """Return the newest controllable turn for one conversation."""
        if not session_id:
            return None
        async with self._db.session() as s:
            return (
                await s.execute(
                    select(TaskORM)
                    .where(
                        TaskORM.session_id == session_id,
                        TaskORM.kind == "turn",
                        TaskORM.status.in_(_ACTIVE_TASK_STATUSES),
                        TaskORM.archived_at.is_(None),
                    )
                    .order_by(TaskORM.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()

    async def list_events(self, task_id: str) -> list[TaskEventORM]:
        async with self._db.session() as s:
            return list(
                (
                    await s.execute(
                        select(TaskEventORM)
                        .where(TaskEventORM.task_id == task_id)
                        .order_by(TaskEventORM.seq.asc(), TaskEventORM.created_at.asc())
                    )
                ).scalars().all()
            )

    async def list_subtasks_by_ids(self, task_ids: list[str]) -> list[SubtaskORM]:
        if not task_ids:
            return []
        async with self._db.session() as s:
            return list((await s.execute(select(SubtaskORM).where(SubtaskORM.id.in_(task_ids)))).scalars().all())

    async def list_workflows_by_ids(self, workflow_ids: list[str]) -> list[WorkflowRunORM]:
        if not workflow_ids:
            return []
        async with self._db.session() as session:
            rows = await session.execute(
                select(WorkflowRunORM).where(WorkflowRunORM.id.in_(workflow_ids))
            )
            return list(rows.scalars().all())

    async def list_subtasks_by_workflow_ids(self, workflow_ids: list[str]) -> list[SubtaskORM]:
        """Load the skill executions owned by one or more workflow runs."""
        if not workflow_ids:
            return []
        async with self._db.session() as session:
            rows = await session.execute(
                select(SubtaskORM)
                .where(SubtaskORM.workflow_run_id.in_(workflow_ids))
                .order_by(SubtaskORM.created_at.asc())
            )
            return list(rows.scalars().all())

    async def list_workflow_steps(self, workflow_run_id: str) -> list[WorkflowStepORM]:
        if not workflow_run_id:
            return []
        async with self._db.session() as session:
            rows = await session.execute(
                select(WorkflowStepORM)
                .where(WorkflowStepORM.workflow_run_id == workflow_run_id)
                .order_by(WorkflowStepORM.position.asc())
            )
            return list(rows.scalars().all())

    async def list_child_tasks(self, parent_task_id: str) -> list[TaskORM]:
        if not parent_task_id:
            return []
        async with self._db.session() as s:
            rows = await s.execute(
                select(TaskORM)
                .where(TaskORM.parent_task_id == parent_task_id)
                .order_by(TaskORM.created_at.asc())
            )
            return list(rows.scalars().all())

    async def cost_summary(self, task_id: str, *, include_child_tasks: bool = True) -> dict[str, Any]:
        """Aggregate component cost events for a task and its durable child tasks."""
        task_ids = [task_id]
        if include_child_tasks:
            index = 0
            while index < len(task_ids):
                children = await self.list_child_tasks(task_ids[index])
                task_ids.extend(child.id for child in children if child.id not in task_ids)
                index += 1
        async with self._db.session() as session:
            events = list(
                (
                    await session.execute(
                        select(TaskEventORM).where(
                            TaskEventORM.task_id.in_(task_ids),
                            TaskEventORM.event_type == "cost.usage",
                        )
                    )
                ).scalars().all()
            )
        components: dict[str, dict[str, Any]] = {}
        totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        }
        estimated_events = 0
        for event in events:
            payload = event.output_json if isinstance(event.output_json, dict) else {}
            component = str(payload.get("component") or event.name or "unknown")
            bucket = components.setdefault(
                component,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                },
            )
            bucket["calls"] += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = int(payload.get(key) or 0)
                bucket[key] += value
                totals[key] += value
            value = float(payload.get("cost_usd") or 0.0)
            bucket["cost_usd"] = round(float(bucket["cost_usd"]) + value, 6)
            totals["cost_usd"] = round(float(totals["cost_usd"]) + value, 6)
            estimated_events += int(bool(payload.get("estimated")))
        return {
            **totals,
            "calls": len(events),
            "estimated_calls": estimated_events,
            "task_ids": task_ids,
            "components": components,
        }

    async def archive_task(self, task_id: str, *, reason: str = "") -> bool:
        """Hide one task (and, by listing, its subtasks) without deleting it."""
        async with self._db.session() as s:
            obj = await s.get(TaskORM, task_id)
            if obj is None:
                return False
            obj.archived_at = _utcnow()
            obj.archived_reason = reason.strip()[:500]
            await s.commit()
        await self._reindex(task_id)
        return True

    async def unarchive_task(self, task_id: str) -> bool:
        """Restore one archived task to default task listings."""
        async with self._db.session() as s:
            obj = await s.get(TaskORM, task_id)
            if obj is None:
                return False
            obj.archived_at = None
            obj.archived_reason = ""
            await s.commit()
        await self._reindex(task_id)
        return True

    async def task_has_artifacts(self, task_id: str) -> bool:
        """True when a task or any of its subtasks produced artifact references.

        Deleting the task only removes provenance rows (``artifacts.subtask_id``
        is set NULL, files are never touched), but this still lets ``task rm``
        warn before discarding the link between a deliverable and its request.
        """
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id)
            if task is None:
                return False
            direct = (
                await s.execute(
                    select(ArtifactORM.id)
                    .where(ArtifactORM.task_id == task_id)
                    .limit(1)
                )
            ).first()
            if direct is not None:
                return True
            subtasks = (
                await s.execute(select(SubtaskORM).where(SubtaskORM.task_id == task_id))
            ).scalars().all()
            for sub in subtasks:
                if _result_has_artifacts(sub.result_json):
                    return True
            sub_ids = [sub.id for sub in subtasks]
            if sub_ids:
                hit = (
                    await s.execute(
                        select(ArtifactORM.id).where(ArtifactORM.subtask_id.in_(sub_ids)).limit(1)
                    )
                ).first()
                if hit is not None:
                    return True
            workflow_hit = (
                await s.execute(
                    select(ArtifactORM.id)
                    .where(ArtifactORM.workflow_run_id.in_(task.submitted_workflow_ids or []))
                    .limit(1)
                )
            ).first() if task.submitted_workflow_ids else None
            if workflow_hit is not None:
                return True
        return False

    async def list_stale_active_tasks(self, *, stale_after_s: float) -> list[TaskORM]:
        """Active tasks whose worker looks dead: no event for ``stale_after_s``.

        Every live turn appends events continuously (tool calls, subtask
        progress, model steps), so a running/recovering task that has been
        silent for the whole window almost certainly lost its process.
        ``awaiting_approval``/``needs_input`` wait on humans, not workers, and
        are deliberately not considered stale.
        """
        if stale_after_s <= 0:
            return []
        cutoff = _utcnow() - timedelta(seconds=stale_after_s)
        stale: list[TaskORM] = []
        async with self._db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(TaskORM).where(TaskORM.status.in_(tuple(_ACTIVE_TASK_STATUSES)))
                    )
                ).scalars().all()
            )
            for row in rows:
                last_event = (
                    await s.execute(
                        select(func.max(TaskEventORM.created_at)).where(TaskEventORM.task_id == row.id)
                    )
                ).scalar_one_or_none()
                last_activity = _aware_dt(last_event or row.started_at or row.created_at)
                if last_activity < cutoff:
                    stale.append(row)
        return stale

    async def reconcile_stale_tasks(self, *, stale_after_s: float) -> list[str]:
        """Settle dead running/recovering tasks as ``interrupted``.

        Runs at service startup and periodically, so a task orphaned by a
        killed process stops showing as phantom "running", stops blocking
        ``task rm/clear``, and becomes prunable. Completed subtask results
        are kept; only the turn-level status is settled.
        """
        interrupted: list[str] = []
        for row in await self.list_stale_active_tasks(stale_after_s=stale_after_s):
            minutes = max(1, int(stale_after_s // 60))
            await self._finish_task_unchecked(
                row.id,
                status="interrupted",
                summary=row.summary,
                error=(
                    f"interrupted: no activity for over {minutes} minute(s); "
                    "the owning process likely exited before finishing"
                ),
            )
            interrupted.append(row.id)
        if interrupted:
            logger.info("reconciled %d stale task(s) → interrupted", len(interrupted))
        return interrupted

    async def delete_task(self, task_id: str) -> bool:
        """Delete one task by exact id; subtasks/events/controls cascade in one DELETE.

        Artifacts survive (``ON DELETE SET NULL``): produced files are user
        deliverables, not task bookkeeping. Callers own the protection policy.
        """
        async with self._db.session() as s:
            obj = await s.get(TaskORM, task_id)
            if obj is None:
                return False
            await s.execute(
                update(ArtifactORM)
                .where(ArtifactORM.task_id == task_id)
                .values(task_id=None)
            )
            await s.delete(obj)
            await s.commit()
        await self._deindex([task_id])
        return True

    async def clear_tasks(
        self,
        *,
        status: str | None = None,
        before: datetime | None = None,
        kind: str | None = "turn",
        include_archived: bool = False,
        force: bool = False,
        prunable_only: bool = False,
        dry_run: bool = False,
    ) -> TaskClearOutcome:
        """Delete (or, with ``dry_run``, count) tasks matching the filters.

        Running/recovering tasks are always blocked; succeeded/degraded/
        needs_input/awaiting_approval are protected unless ``force``. When
        ``prunable_only`` is set only failed/cancelled/interrupted tasks are
        considered (used by ``prune``). Deletion cascades to each task's
        subtasks, events, and controls via foreign keys.
        """
        outcome = TaskClearOutcome()
        async with self._db.session() as s:
            rows = list((await s.execute(select(TaskORM))).scalars().all())
            to_delete: list[TaskORM] = []
            for r in rows:
                if r.archived_at is not None and not include_archived:
                    continue
                if status and r.status != status:
                    continue
                if kind and r.kind != kind:
                    continue
                # Age by completion time so a task settled recently (e.g. just
                # reconciled to interrupted) is not reaped in the same sweep.
                if before is not None and _aware_dt(r.finished_at or r.created_at) >= before:
                    continue
                if prunable_only and r.status not in _TASK_PRUNABLE_STATUSES:
                    continue
                if r.status in _TASK_BLOCKED_STATUSES:
                    outcome.blocked[r.status] = outcome.blocked.get(r.status, 0) + 1
                    continue
                if r.status in _TASK_PROTECTED_STATUSES and not force:
                    outcome.protected[r.status] = outcome.protected.get(r.status, 0) + 1
                    continue
                to_delete.append(r)
                outcome.deleted[r.status] = outcome.deleted.get(r.status, 0) + 1
            deleted_ids = [r.id for r in to_delete]
            if not dry_run:
                if deleted_ids:
                    await s.execute(
                        update(ArtifactORM)
                        .where(ArtifactORM.task_id.in_(deleted_ids))
                        .values(task_id=None)
                    )
                for r in to_delete:
                    await s.delete(r)
                await s.commit()
        if not dry_run and deleted_ids:
            await self._deindex(deleted_ids)
        return outcome

    async def _session_external_key(self, session_id: str) -> str:
        async with self._db.session() as s:
            row = await s.get(SessionORM, session_id)
        return row.external_key if row is not None else ""


def _merge_task_ids(task: TaskORM, payload: Any) -> None:
    if not isinstance(payload, (Mapping, list)):
        return
    for attr, key in (
        ("source_ids", "source_ids"),
        ("claim_ids", "claim_ids"),
        ("evidence_ids", "evidence_ids"),
    ):
        existing = list(getattr(task, attr) or [])
        new_ids = _collect_ids(payload, key)
        for item in new_ids:
            if item not in existing:
                existing.append(item)
        setattr(task, attr, existing)


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "")
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _execution_summary(
    components: list[Any],
    succeeded: list[Any],
    degraded: list[Any],
    failed: list[Any],
) -> str:
    return (
        f"Execution completed with warnings: {len(succeeded)}/{len(components)} succeeded, "
        f"{len(degraded)} degraded, and {len(failed)} failed."
    )
