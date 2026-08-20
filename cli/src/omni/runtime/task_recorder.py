"""Durable task recorder — one task per user request.

The subtask runtime persists long-running skill work. A task captures the
whole agent turn: the user message, ReAct tool calls, submitted subtasks,
artifacts, and final response. `/task` renders tasks by default and drills
down into subtasks when needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, literal, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from omni.core.termination import aggregate_outcome_status
from omni.runtime.settlement import (
    TURN_END_EVENTS,
    Settlement,
    effective_subtasks,
    settlement_for,
)
from omni.runtime.task_results import (
    _aware_dt,
    _result_has_artifacts,
    action_required_presentation,
    installation_required_presentation,
)
from omni.storage.db import Database, retry_while_busy, sqlite_busy
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

# Contention on an event's sequence number is brief — each retry only re-reads a
# max — so a handful of closely spaced attempts absorbs a worker pool reporting
# progress without making a genuinely wedged database wait.
_EVENT_SEQ_ATTEMPTS = 5
_EVENT_SEQ_BACKOFF_S = 0.01

_ACTIVE_TASK_STATUSES = {"running", "recovering"}
_ACTIVE_EXECUTION_STATUSES = {"scheduled", "pending", "running", "recovering"}
_FAILED_EXECUTION_STATUSES = {"failed", "cancelled", "interrupted"}

# The statuses that mean "this run is over". A run reaches one of these once,
# and moving off one goes through ``reopen_task_for_recovery`` so the transition
# is recorded rather than silently overwriting what the user was already shown.
# ``needs_input`` and ``awaiting_approval`` are deliberately absent: they are
# protected pauses, and answering one is exactly how it settles.
_TERMINAL_TASK_STATUSES = {"succeeded", "degraded", "failed", "cancelled", "interrupted"}

# Deletion protection tiers for tasks (user requests):
#   * blocked  — a worker owns the row; refuse in bulk until it settles/reconciles.
#   * protected — carries provenance (results, artifacts, or a pending decision);
#                 deletable only with --force.
#   * everything else (failed / cancelled / interrupted) is deletable by default.
_TASK_BLOCKED_STATUSES = ("running", "recovering")
_TASK_PROTECTED_STATUSES = ("succeeded", "degraded", "needs_input", "awaiting_approval")
_TASK_PRUNABLE_STATUSES = ("failed", "cancelled", "interrupted")

# Intents whose turns are pure conversation: the planner routes them to an
# answer rather than to work. Only these are eligible to be filed as
# ``kind="chat"`` — richer intents (memory_update / schedule / single_skill_task
# / workflow) always remain ``turn`` even when they leave no artifact.
#
# ``react_fallback`` used to be here and is the reason this list is now one
# entry. It names the general agent loop, the path that runs shell, reads files,
# writes memory and spawns sub-agents; it is where the heaviest turns land, not
# the lightest. Run 2db31f83 asked for shell permission and then spent 473
# seconds on 48 bash calls and four sub-agents before settling as ``chat`` and
# vanishing from the ledger.
_CONVERSATIONAL_INTENTS = {"direct_answer"}
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

    The cheap screen, read off the task row alone: a top-level ``turn`` the
    planner routed to an answer, holding no submitted subtask/workflow, no
    artifact and no schedule. Passing it is necessary but not sufficient —
    :func:`_left_no_trace` still has to agree — because these columns record
    only three shapes of work and a turn can do plenty that fits none of them.
    """
    return (
        (task.kind or "turn") == "turn"
        and not task.schedule_id
        and not task.parent_task_id
        and not task.origin_workflow_run_id
        and not (task.submitted_subtask_ids or task.submitted_workflow_ids or task.artifact_ids)
        and (task.intent_type or "") in _CONVERSATIONAL_INTENTS
    )


# Guards the one-shot repair below per (process, sessions DB): the misfiling it
# undoes cannot recur once the predicate is fixed, so one sweep per workspace is
# all a process ever needs.
_repaired_chat: set[str] = set()


async def repair_misfiled_chat(db: Any, *, force: bool = False) -> int:
    """Return to the ledger the turns the old predicate hid, and count them.

    Between 9ce69f1 and the fix above, any succeeded ``react_fallback`` turn
    that recorded no subtask, workflow or artifact was filed as ``chat`` and
    dropped out of ``/task``. That caught the agent loop's heaviest runs: in the
    workspace where this was found, the three longest turns on record — one of
    them 473 seconds with four sub-agents — were all hidden, against three
    ordinary turns left visible.

    Only rows with evidence of work move, and they only ever move back to
    ``turn``, so a genuine conversation stays where it is and running this twice
    changes nothing the first run left.

    Asked before it writes, and on both list paths, so the steady state costs a
    read: once a workspace has been swept there is nothing left to match, and no
    later process takes a write lock to discover that. This runs while other
    processes may be mid-task, and a lock held for a repair with no work to do
    is a lock taken away from one that has some.
    """
    key = str(getattr(db, "path", "") or id(db))
    if not force and key in _repaired_chat:
        return 0
    _repaired_chat.add(key)
    delegated = select(TaskORM.parent_task_id).where(
        TaskORM.parent_task_id.is_not(None), TaskORM.parent_task_id != ""
    )
    used_a_tool = select(TaskEventORM.task_id).where(
        TaskEventORM.event_type.contains(".tool.")
    )
    misfiled = (
        TaskORM.kind == "chat",
        or_(
            TaskORM.intent_type == "react_fallback",
            TaskORM.id.in_(delegated),
            TaskORM.id.in_(used_a_tool),
        ),
    )
    try:
        async with db.session() as s:
            pending = await s.scalar(
                select(func.count()).select_from(TaskORM).where(*misfiled)
            )
        if not pending:
            return 0
        async with db.session() as s:
            result = await s.execute(update(TaskORM).where(*misfiled).values(kind="turn"))
            await s.commit()
            return int(result.rowcount or 0)
    except Exception:  # noqa: BLE001 - a repair must never break a list view.
        logger.debug("task recorder: chat repair skipped", exc_info=True)
        return 0


async def _left_no_trace(session: AsyncSession, task_id: str) -> bool:
    """Whether a turn reached for anything at all before answering.

    The row columns see three kinds of work: skill executions, workflows and
    artifacts. A turn that shells out, edits a file, writes memory or delegates
    to a sub-agent registers in none of them, so the row alone reported run
    2db31f83 — 48 bash calls, four child tasks — as having done nothing.

    Two questions cover the rest without a list of tool names to maintain: did
    it delegate, and did it call a tool. A turn that answered from what it
    already had did neither, and that is the one this feature was built to hide.
    """
    delegated = await session.scalar(
        select(func.count())
        .select_from(TaskORM)
        .where(TaskORM.parent_task_id == task_id)
    )
    if delegated:
        return False
    used_a_tool = await session.scalar(
        select(func.count())
        .select_from(TaskEventORM)
        .where(
            TaskEventORM.task_id == task_id,
            TaskEventORM.event_type.contains(".tool."),
        )
    )
    return not used_a_tool


@dataclass(frozen=True)
class TaskDeleteItem:
    """Immutable row summary captured with the deletion policy decision."""

    id: str
    status: str
    title: str


@dataclass(frozen=True)
class TaskExecutionBarrier:
    """One live execution that prevents deletion of its owning Task tree."""

    task_id: str
    object_kind: str
    object_id: str
    status: str


@dataclass
class TaskClearOutcome:
    """Result of a task-level clear/prune, split for a transparent preview.

    ``deleted`` counts rows removed (or, in a dry run, rows that *would* be
    removed) grouped by status. The id-bearing fields expose the exact closure
    for CLI previews and index cleanup. ``protected`` and ``blocked`` explain
    status/lease barriers; ``retained`` records archive or age boundaries, so
    the CLI can explain every preserved tree instead of reporting bare zeroes.
    """

    deleted: dict[str, int] = field(default_factory=dict)
    protected: dict[str, int] = field(default_factory=dict)
    blocked: dict[str, int] = field(default_factory=dict)
    retained: dict[str, int] = field(default_factory=dict)
    deleted_ids: list[str] = field(default_factory=list)
    deleted_tasks: list[TaskDeleteItem] = field(default_factory=list)
    known_task_ids: list[str] = field(default_factory=list)
    protected_tasks: dict[str, str] = field(default_factory=dict)
    blocked_tasks: dict[str, str] = field(default_factory=dict)
    blocked_executions: list[TaskExecutionBarrier] = field(default_factory=list)
    retained_tasks: dict[str, str] = field(default_factory=dict)
    missing_ids: list[str] = field(default_factory=list)
    concurrent_write: bool = False

    @property
    def deleted_total(self) -> int:
        return sum(self.deleted.values())

    @property
    def protected_total(self) -> int:
        return sum(self.protected.values())

    @property
    def blocked_total(self) -> int:
        return sum(self.blocked.values())

    @property
    def retained_total(self) -> int:
        return sum(self.retained.values())


def _unique_task_ids(task_ids: Sequence[str]) -> list[str]:
    """Return non-empty task ids once, preserving the caller's order."""
    return list(
        dict.fromkeys(
            str(task_id).strip() for task_id in task_ids if str(task_id).strip()
        )
    )


def _task_descendant_closure(
    rows_by_id: Mapping[str, TaskORM],
    root_ids: Sequence[str],
) -> list[TaskORM]:
    """Expand roots to their complete child-Task closure without recursion."""
    children: dict[str, list[str]] = {}
    for row in rows_by_id.values():
        if row.parent_task_id:
            children.setdefault(row.parent_task_id, []).append(row.id)
    pending = list(reversed(_unique_task_ids(root_ids)))
    seen: set[str] = set()
    closure: list[TaskORM] = []
    while pending:
        task_id = pending.pop()
        if task_id in seen:
            continue
        seen.add(task_id)
        row = rows_by_id.get(task_id)
        if row is None:
            continue
        closure.append(row)
        pending.extend(reversed(children.get(task_id, [])))
    return closure


def _task_ancestor_closure(
    rows_by_id: Mapping[str, TaskORM],
    task_ids: Sequence[str],
) -> list[TaskORM]:
    """Return every stored ancestor of ``task_ids`` once, nearest first."""
    seen = set(_unique_task_ids(task_ids))
    ancestors: list[TaskORM] = []
    for task_id in _unique_task_ids(task_ids):
        row = rows_by_id.get(task_id)
        parent_id = (row.parent_task_id or "") if row is not None else ""
        branch_seen = {task_id}
        while parent_id and parent_id not in branch_seen:
            branch_seen.add(parent_id)
            parent = rows_by_id.get(parent_id)
            if parent is None:
                break
            if parent.id not in seen:
                seen.add(parent.id)
                ancestors.append(parent)
            parent_id = parent.parent_task_id or ""
    return ancestors


def _topmost_selected_task_ids(
    rows_by_id: Mapping[str, TaskORM],
    task_ids: Sequence[str],
) -> list[str]:
    """Keep selected rows that have no selected ancestor through a skipped row."""
    selected = set(task_ids)
    roots: list[str] = []
    for task_id in _unique_task_ids(task_ids):
        parent_id = rows_by_id[task_id].parent_task_id or ""
        visited = {task_id}
        has_selected_ancestor = False
        while parent_id and parent_id not in visited:
            if parent_id in selected:
                has_selected_ancestor = True
                break
            visited.add(parent_id)
            parent = rows_by_id.get(parent_id)
            parent_id = (parent.parent_task_id or "") if parent is not None else ""
        ancestry_cycle = bool(parent_id and parent_id in visited)
        if not has_selected_ancestor and not ancestry_cycle:
            roots.append(task_id)
    return roots


def _add_status_count(bucket: dict[str, int], status: str) -> None:
    bucket[status] = bucket.get(status, 0) + 1


def _protect_task_rows(
    outcome: TaskClearOutcome,
    rows: Sequence[TaskORM],
    *,
    force: bool,
) -> bool:
    """Record full-tree deletion barriers; active work is never force-deletable."""
    blocked = [row for row in rows if row.status in _TASK_BLOCKED_STATUSES]
    protected = [
        row
        for row in rows
        if not force and row.status in _TASK_PROTECTED_STATUSES
    ]
    for row in blocked:
        if row.id not in outcome.blocked_tasks:
            outcome.blocked_tasks[row.id] = row.status
            _add_status_count(outcome.blocked, row.status)
    for row in protected:
        if row.id not in outcome.protected_tasks:
            outcome.protected_tasks[row.id] = row.status
            _add_status_count(outcome.protected, row.status)
    return bool(blocked or protected)


def _retain_task_rows(
    outcome: TaskClearOutcome,
    rows: Sequence[TaskORM],
    *,
    reason: str,
) -> bool:
    """Record rows outside a clear operation's visibility/retention scope."""
    retained = bool(rows)
    for row in rows:
        if row.id in outcome.retained_tasks:
            continue
        outcome.retained_tasks[row.id] = reason
        _add_status_count(outcome.retained, reason)
    return retained


async def _active_executions_by_task(
    session: AsyncSession,
) -> dict[str, list[TaskExecutionBarrier]]:
    """Load live workflow/step/subtask leases, grouped by owning Task.

    Task status is a presentation/aggregation projection and can briefly lag
    the execution rows beneath it. Deletion authority therefore comes from
    both layers. Querying active rows by their small status set avoids a large
    ``IN`` expression when a delegated Task tree contains many nodes.
    """
    active: dict[str, list[TaskExecutionBarrier]] = {}
    models = (
        ("workflow_run", WorkflowRunORM),
        ("workflow_step", WorkflowStepORM),
        ("skill_execution", SubtaskORM),
    )
    for object_kind, model in models:
        rows = (
            await session.execute(
                select(model.id, model.task_id, model.status).where(
                    model.status.in_(tuple(_ACTIVE_EXECUTION_STATUSES))
                )
            )
        ).all()
        for object_id, task_id, status in rows:
            if not task_id:
                continue
            owner = str(task_id)
            active.setdefault(owner, []).append(
                TaskExecutionBarrier(
                    task_id=owner,
                    object_kind=object_kind,
                    object_id=str(object_id),
                    status=str(status),
                )
            )
    return active


def _protect_active_executions(
    outcome: TaskClearOutcome,
    task_ids: Sequence[str],
    active_by_task: Mapping[str, Sequence[TaskExecutionBarrier]],
) -> bool:
    """Record live execution leases owned by any Task in the guarded tree."""
    matches = [
        execution
        for task_id in _unique_task_ids(task_ids)
        for execution in active_by_task.get(task_id, ())
    ]
    existing = {
        (barrier.object_kind, barrier.object_id)
        for barrier in outcome.blocked_executions
    }
    for barrier in matches:
        key = (barrier.object_kind, barrier.object_id)
        if key in existing:
            continue
        existing.add(key)
        outcome.blocked_executions.append(barrier)
        _add_status_count(outcome.blocked, barrier.status)
    return bool(matches)


def _delete_item(row: TaskORM) -> TaskDeleteItem:
    """Freeze the Task fields a confirmation preview is allowed to display."""
    return TaskDeleteItem(
        id=row.id,
        status=row.status,
        title=str(row.title or row.user_input or "-"),
    )


def _task_id_batches(task_ids: Sequence[str], *, size: int = 500) -> list[list[str]]:
    """Bound SQLite bind parameters for closure-wide maintenance statements."""
    ids = _unique_task_ids(task_ids)
    return [ids[offset : offset + size] for offset in range(0, len(ids), size)]


def _unrooted_task_ids(
    rows_by_id: Mapping[str, TaskORM],
    task_ids: Sequence[str],
    root_ids: Sequence[str],
) -> list[str]:
    """Find selected rows that no acyclic topmost root can reach."""
    reachable = {
        row.id for row in _task_descendant_closure(rows_by_id, root_ids)
    }
    return [task_id for task_id in _unique_task_ids(task_ids) if task_id not in reachable]


async def _begin_task_delete_snapshot(
    session: AsyncSession,
    outcome: TaskClearOutcome,
    *,
    dry_run: bool,
) -> bool:
    """Open a stable read snapshot or reserve the SQLite writer for deletion.

    SQLite's legacy deferred mode does not start a transaction for SELECTs.
    Without an explicit write reservation, another process can turn a checked
    Task tree active between policy validation and the cascading DELETE. Real
    mutations therefore start with ``BEGIN IMMEDIATE``; previews use ``BEGIN``
    for a coherent, non-blocking snapshot and are revalidated on confirmation.
    """
    statement = "BEGIN" if dry_run else "BEGIN IMMEDIATE"
    try:
        await session.execute(text(statement))
    except OperationalError as exc:
        code = getattr(getattr(exc, "orig", None), "sqlite_errorcode", None)
        message = str(exc).lower()
        sqlite_contention = (
            isinstance(code, int) and (code & 0xFF) in {5, 6}
        ) or "database is locked" in message or "database is busy" in message
        if not sqlite_contention:
            raise
        outcome.concurrent_write = True
        return False
    return True


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
        lifecycle_status=str(payload.get("lifecycle_status") or ""),
        result_success=payload.get("result_success"),
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
    if event.event_type in TURN_END_EVENTS:
        task.steering_status = "sealed"
    _merge_task_ids(task, raw_output)


def _apply_plan_projection(
    task: TaskORM,
    payload: dict[str, Any],
    *,
    status: str,
    current_authority_fingerprint: str,
) -> None:
    """Update the latest plan and invalidate reviewed approval on authority change.

    Workspace-auto and schedule grants live on ``approved_tools`` without a
    reviewed plan fingerprint. A first plan persist must not wipe those; only
    a changed *reviewed* authority (the owner approved a specific plan) drops
    the grant set.
    """
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
        had_reviewed_approval = bool(task.approval_authority_fingerprint)
        task.approval_authority_fingerprint = ""
        if had_reviewed_approval:
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
        # Sequence numbers come from reading the current max, so the workers of
        # one process would otherwise race each other for every progress event.
        # Serializing them here makes the common case contention-free; the unique
        # index and the retry below still cover a second process writing at once.
        self._event_seq_lock = asyncio.Lock()
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

    @property
    def db(self) -> Database:
        """The workspace database this recorder writes events into."""
        return self._db

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
        retry_of_task_id: str = "",
        root_task_id: str = "",
        attempt: int = 1,
        input_snapshot: dict | None = None,
        file_uris: list[str] | None = None,
        interaction_mode: str = "",
        origin: str = "interactive",
    ) -> TaskORM:
        from omni.runtime.task_title import short_task_title

        title = title or short_task_title(user_input)
        if not external_key and session_id:
            external_key = await self._session_external_key(session_id)
        # Immutable turn input so any later ``task retry`` can reproduce the
        # request. Explicit args backfill fields the caller did not fold in.
        snapshot = dict(input_snapshot or {})
        snapshot.setdefault("user_input", user_input)
        if file_uris and not snapshot.get("file_uris"):
            snapshot["file_uris"] = [str(u) for u in file_uris]
        if interaction_mode and not snapshot.get("interaction_mode"):
            snapshot["interaction_mode"] = interaction_mode
        if origin and not snapshot.get("origin"):
            snapshot["origin"] = origin
        if channel and not snapshot.get("channel"):
            snapshot["channel"] = channel
        if external_key and not snapshot.get("external_key"):
            snapshot["external_key"] = external_key
        row = TaskORM(
            session_id=session_id,
            parent_task_id=parent_task_id or None,
            origin_workflow_run_id=origin_workflow_run_id,
            origin_workflow_step_id=origin_workflow_step_id,
            schedule_id=schedule_id or "",
            retry_of_task_id=retry_of_task_id or "",
            root_task_id=root_task_id or "",
            attempt=max(1, int(attempt or 1)),
            input_snapshot_json=snapshot,
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
        if retry_of_task_id:
            await self.append_event(
                row.id,
                event_type="task.retry.created",
                status="info",
                name="retry",
                output_json={
                    "retry_of_task_id": retry_of_task_id,
                    "root_task_id": row.root_task_id,
                    "attempt": row.attempt,
                },
                summary=(
                    f"retry attempt {row.attempt} of {row.root_task_id[:8] or row.id[:8]}"
                ),
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
        lifecycle_status: str = "",
        result_success: bool | None = None,
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
            "lifecycle_status": lifecycle_status,
            "result_success": result_success,
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
        # ``(task_id, seq)`` is unique and assigned by reading the current max,
        # so two events appended concurrently under one task pick the same number
        # and one insert loses. A skill that reports progress from a worker pool
        # does exactly that, and the lost event used to surface as a raw
        # IntegrityError traceback in the middle of a running turn. Re-reading the
        # max is the whole retry: the loser simply takes the next number.
        # A cancel that lands while the run is still writing hits the other
        # queue: SQLite's one-writer lock. Five 10ms retries lose on Windows —
        # the dying cli_exec keeps the aiosqlite worker lock after the asyncio
        # task is gone. ``retry_while_busy`` is the same queue workflow persist
        # already uses so the cancel event lands inside a 2s turn.
        for attempt in range(_EVENT_SEQ_ATTEMPTS):
            async def _write_event() -> TaskEventORM:
                async with self._event_seq_lock, self._db.session() as s:
                    max_seq = (
                        await s.execute(
                            select(func.max(TaskEventORM.seq)).where(
                                TaskEventORM.task_id == task_id
                            )
                        )
                    ).scalar_one_or_none()
                    seq = int(max_seq or 0) + 1
                    event = _event_row(task_id, seq, payload)
                    s.add(event)
                    # Reading the task would otherwise autoflush the insert and
                    # take the write lock before the projection is applied.
                    with s.no_autoflush:
                        task = await s.get(TaskORM, task_id)
                    if task is not None:
                        _apply_event_projection(
                            task,
                            event,
                            raw_output=output_json,
                        )
                    await s.commit()
                    await s.refresh(event)
                    return event

            try:
                event = await retry_while_busy(_write_event, attempts=3)
            except IntegrityError:
                if attempt + 1 >= _EVENT_SEQ_ATTEMPTS:
                    logger.warning(
                        "task.event.seq_contention task=%s type=%s dropped after %d attempts",
                        task_id[:8], event_type, _EVENT_SEQ_ATTEMPTS,
                    )
                    return None
                await asyncio.sleep(_EVENT_SEQ_BACKOFF_S * (attempt + 1))
                continue
            except OperationalError as exc:
                if sqlite_busy(exc):
                    logger.warning(
                        "task.event.busy task=%s type=%s dropped",
                        task_id[:8], event_type,
                    )
                    return None
                raise
            _log_event_row(event)
            return event
        return None

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
        """Settle a terminal task against the durable record.

        ``finish_task`` remains the compatibility API used by commands and
        extensions. Successful, degraded, and failed completions all flow
        through :meth:`settle_task`, so no caller can publish a status the record
        does not support; only an external cancellation or a lost-executor
        interrupt bypasses it, because those are themselves the terminal
        decision.
        """
        if status in {"cancelled", "interrupted"}:
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
        """Persist a terminal decision already made by the verifier or operator.

        The one writer of a terminal status, and it writes each run's only once.
        Sealing steering and stamping ``finished_at`` are what make a run *over*
        for everyone downstream — `/task`, the daemon's active set, the steering
        channel — so they belong to a decision that is actually final. A second
        terminal write is refused rather than applied: whatever the user was
        already shown stands until a recovery explicitly reopens the run.
        """
        if not task_id:
            return
        from omni.runtime.cancel_persist import exclusive_persist

        # Cancel-path skill/workflow writes hold the aiosqlite worker lock
        # (especially on Windows) after the asyncio task is gone. Those
        # persists already queue on ``exclusive_persist`` and give up early;
        # this is the parent turn settler, so it waits for that queue and
        # then retries with the full busy budget instead of failing the turn.
        async with exclusive_persist():
            written = await retry_while_busy(
                lambda: self._write_terminal_task(
                    task_id, status=status, summary=summary, error=error
                )
            )
        if written is None:
            return
        parent_task_id, kind = written
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

    async def _write_terminal_task(
        self,
        task_id: str,
        *,
        status: str,
        summary: str,
        error: str,
    ) -> tuple[str, str] | None:
        """Write the sealed terminal row. Return ``(parent_task_id, kind)`` or skip."""
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id)
            if task is None:
                return None
            if task.status in _TERMINAL_TASK_STATUSES and task.status != status:
                logger.warning(
                    "refusing to re-settle task %s: %s is already terminal, "
                    "proposed %s (reopen for recovery to move off a terminal status)",
                    task_id[:8],
                    task.status,
                    status,
                )
                return None
            parent_task_id = task.parent_task_id or ""
            kind = task.kind or "turn"
            task.status = status
            task.steering_status = "sealed"
            if (
                self._classify_conversational
                and status == "succeeded"
                and _is_conversational_turn(task)
                and await _left_no_trace(s, task_id)
            ):
                task.kind = "chat"
            task.summary = summary or task.summary
            task.error = (error or task.error) if status in {"failed", "degraded"} else error
            task.current_stage = f"task.{status}"
            task.finished_at = _utcnow()
            await s.commit()
        return parent_task_id, kind

    async def settle_task(
        self,
        task_id: str,
        *,
        proposed_status: str,
        summary: str = "",
        error: str = "",
        turn_in_flight: bool = False,
    ) -> str:
        """Commit a terminal state once the record shows the run has earned it.

        ``turn_in_flight`` marks a caller that is not the turn itself, so the
        record is read as unfinished rather than deficient. See
        :func:`omni.runtime.settlement.settlement_for`.
        """
        # A clarifying turn is a *suspend*, not a failure: honor needs_input as a
        # protected terminal outcome. A turn that stopped to ask a question
        # legitimately produced none of the work the run was going to do, so
        # ranking it on the success/degraded/failed axis would only mislabel the
        # pause. This mirrors Codex/Claude Code, where awaiting-input is
        # terminal-and-protected.
        if proposed_status == "needs_input":
            await self.mark_needs_input(task_id, summary=summary)
            return "needs_input"
        current = await self.get_task(task_id)
        if current is not None and current.status in _TERMINAL_TASK_STATUSES:
            return current.status
        settled = await settlement_for(self, task_id, turn_in_flight=turn_in_flight)
        if settled.is_pending:
            return "running"
        # A settled ``needs_input`` falls through deliberately. It is off the
        # success/degraded/failed axis, so aggregation skips it and the caller's
        # proposal decides — which is what closing a pause requires: `task resume`
        # answers the question and then names the outcome the answer produced.
        status = aggregate_outcome_status(proposed_status, settled.status)
        if settled.status == "succeeded" and proposed_status == "degraded":
            # The record already combined the loop stop with children and named
            # outputs. A caller proposing ``degraded`` for leftover ``no_progress``
            # must not outrank a complete contract.
            status = "succeeded"
        final_error = error
        if status == "failed" and proposed_status not in {"cancelled", "needs_input"}:
            final_error = error or _settlement_error(settled)
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

    async def park_maintenance(self, task_id: str, *, summary: str = "") -> None:
        """Park a maintenance run as owed work rather than work in flight.

        ``pending`` is deliberately outside the stale sweep: the run has no
        worker yet, so it must not be mistaken for one whose process died. It
        waits in the queue until a drain claims it, however many sessions later
        that is.
        """
        if not task_id:
            return
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id)
            if task is None:
                return
            task.status = "pending"
            task.current_stage = "maintenance.queued"
            task.finished_at = None
            if summary:
                task.summary = summary
            await s.commit()
        await self.append_event(
            task_id,
            event_type="maintenance.queued",
            status="pending",
            name="memory",
            summary=summary or "memory maintenance queued for a later drain",
        )
        await self._reindex(task_id)

    async def settle_orphaned_maintenance(self, *, stale_after_s: float = 1800.0) -> list[str]:
        """Settle maintenance runs whose process died mid-pass.

        The general stale sweep only runs inside a service, and an interactive
        window never starts one — so a pass cut off by an exit stayed ``running``
        forever, and real workspaces accumulated dozens of them. The drain owns
        this queue, so it also clears the wreckage of earlier drains.
        """
        settled: list[str] = []
        minutes = max(1, int(stale_after_s // 60))
        for row in await self.list_stale_active_tasks(stale_after_s=stale_after_s):
            if row.kind != "maintenance":
                continue
            await self._finish_task_unchecked(
                row.id,
                status="interrupted",
                summary=row.summary,
                error=(
                    f"interrupted: no activity for over {minutes} minute(s); "
                    "the process running this memory pass exited before finishing"
                ),
            )
            settled.append(row.id)
        return settled

    async def claim_pending_maintenance(self, *, limit: int = 5) -> list[TaskORM]:
        """Claim parked maintenance runs, oldest first, with a status CAS.

        Every window and the service share one queue, so two drains starting at
        once must not run the same pass twice. Only rows this call moves out of
        ``pending`` are returned.
        """
        if limit <= 0:
            return []
        claimed: list[TaskORM] = []
        async with self._db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(TaskORM)
                        .where(TaskORM.kind == "maintenance", TaskORM.status == "pending")
                        .order_by(TaskORM.created_at.asc())
                        .limit(limit)
                    )
                ).scalars().all()
            )
            for row in rows:
                won = await s.execute(
                    update(TaskORM)
                    .where(TaskORM.id == row.id, TaskORM.status == "pending")
                    .values(status="running", current_stage="maintenance.claimed")
                )
                if int(won.rowcount or 0) == 1:
                    claimed.append(row)
            await s.commit()
        for row in claimed:
            await self._reindex(row.id)
        return claimed

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
        """Return stale, unacknowledged steers to ``pending`` on task resume.

        ``consumed`` means a prior process claimed the row but never durably
        acknowledged a semantic delivery. A dead consumer PID is recoverable
        immediately; a live or legacy owner must exceed the time lease first,
        so a second worker cannot steal a live process's in-memory steer.
        Recovery is intentionally at-least-once after a hard process crash;
        graceful foreground settlement remains exactly-once. Applied and
        foreground-requeued rows are terminal and remain untouched.

        Cancel is never recovered. Codex treats Interrupt as the end of that
        turn; replaying a consumed cancel would kill the next attempt
        (approval resume, scheduled re-fire, or the turn that starts after
        ``omni update --local`` restarts serve).
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
                            TaskControlORM.action == "steer",
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
        """Settle a task from workflow, direct execution, and child-task outcomes.

        Never while the parent's own turn is still running. A child finishing is
        an observation the turn may still act on — call another tool, retry it,
        write the answer — not a verdict about the turn. Codex draws the same
        line (`trigger_turn: false`: a child's completion posts a result for the
        coordinator to read, and never settles the coordinator's turn), and omni
        already honours it for subagents. Settling here mid-turn published a
        status the run had not earned, sealed steering while the user could still
        steer, and — because the task then left the active set — dropped every
        later child outcome from the aggregate.

        A background drain arriving after the turn, or a task whose work was
        enqueued with no turn at all, still settles here: that is this method's
        job, and nobody else is going to do it.
        """
        if not task_id:
            return
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id)
            if task is None or task.status not in _ACTIVE_TASK_STATUSES:
                return
            if await self._turn_in_flight(s, task):
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
            # This method already returned if the parent turn was still writing
            # the record. A child completing after ``react.finished`` is an
            # outsider looking at a finished turn, not a mid-loop observer.
            turn_in_flight=False,
        )

    async def _turn_in_flight(self, session: Any, task: TaskORM) -> bool:
        """Whether a turn has begun on this task and not yet reached its end.

        The execution epoch is already durable: ``record_plan`` opens it (a plan
        exists only because a turn planned this task) and a turn-end event seals
        it. Between those two points the loop is still running and still writing
        the record. Outside them there is no turn to defer to — a task whose work
        was enqueued directly has children but no loop, and settles from them as
        it always did.
        """
        if not task.plan_json:
            return False
        row = (
            await session.execute(
                select(TaskEventORM.id)
                .where(
                    TaskEventORM.task_id == task.id,
                    TaskEventORM.event_type.in_(tuple(TURN_END_EVENTS)),
                )
                .limit(1)
            )
        ).first()
        return row is None

    async def refresh_from_subtasks(self, task_id: str) -> None:
        """Compatibility name for callers; aggregation now spans all executions."""
        await self.refresh_from_executions(task_id)

    async def settlement(self, task_id: str) -> Settlement:
        """The terminal status the record says ``task_id`` has earned."""
        return await settlement_for(self, task_id)

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
        # Before the filter, not after: a row still misfiled as ``chat`` is
        # excluded by ``kind == "turn"`` in SQL, so repairing afterwards would
        # leave it missing from the very list that went looking for it.
        await repair_misfiled_chat(self._db)
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

    async def list_tasks_for_session(self, session_id: str) -> list[TaskORM]:
        """Every task that names this conversation, including archived rows."""
        if not session_id:
            return []
        async with self._db.session() as s:
            return list(
                (
                    await s.execute(
                        select(TaskORM).where(TaskORM.session_id == session_id)
                    )
                ).scalars().all()
            )

    async def latest_task_for_session(self, session_id: str) -> TaskORM | None:
        """Newest turn for one conversation, regardless of status.

        ``active_task_for_session`` is the controllable row. Resume orientation
        also needs the last finished turn so the card can say where work stopped.
        """
        if not session_id:
            return None
        async with self._db.session() as s:
            return (
                await s.execute(
                    select(TaskORM)
                    .where(
                        TaskORM.session_id == session_id,
                        TaskORM.kind == "turn",
                        TaskORM.archived_at.is_(None),
                    )
                    .order_by(TaskORM.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()

    async def delivered_attachment_keys(
        self,
        task_id: str,
        *,
        channel: str,
        external_key: str = "",
    ) -> set[str]:
        """uri/path keys this channel has already uploaded for ``task_id``.

        A later skill notice skips only these files. Legacy events that stored
        a cover boolean and no keys contribute nothing — unknown files must
        still be sent.
        """
        if not task_id or not channel:
            return set()
        async with self._db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(TaskEventORM)
                        .where(
                            TaskEventORM.task_id == task_id,
                            TaskEventORM.event_type.in_(
                                ("presentation.sent", "presentation.degraded")
                            ),
                            TaskEventORM.name == channel,
                        )
                        .order_by(TaskEventORM.created_at.asc(), TaskEventORM.seq.asc())
                    )
                ).scalars().all()
            )
        keys: set[str] = set()
        for row in rows:
            payload = row.output_json if isinstance(row.output_json, dict) else {}
            recorded_key = str(payload.get("external_key") or "")
            if external_key and recorded_key and recorded_key != external_key:
                continue
            for field_name in ("delivered_uris", "delivered_paths"):
                values = payload.get(field_name) or []
                if isinstance(values, list):
                    keys.update(str(item) for item in values if item)
        return keys

    async def turn_covers_deliverables(
        self,
        task_id: str,
        *,
        channel: str,
        external_key: str = "",
    ) -> bool:
        """Whether a sent/degraded parent turn already attached chat files.

        Pending-child turns withhold attachments and must still notify. The
        skill-notice path compares ``delivered_attachment_keys`` to the files
        the notice would send; this boolean only answers "did any file go".
        """
        return bool(
            await self.delivered_attachment_keys(
                task_id, channel=channel, external_key=external_key
            )
        )

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

    async def list_artifacts_by_task(self, task_id: str) -> list[ArtifactORM]:
        if not task_id:
            return []
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(ArtifactORM)
                    .where(ArtifactORM.task_id == task_id)
                    .order_by(ArtifactORM.created_at.asc())
                )
            ).scalars().all()
        return list(rows)

    async def settle_open_children_for_cancel(self, task_id: str) -> None:
        """Mark leftover open children cancelled when the parent turn stops.

        The cancelled execute task may fail to persist its own rows on
        Python 3.11+. This write runs on the uncancelled turn wrapper.
        """
        if not task_id:
            return
        from omni.runtime.cancel_persist import exclusive_persist

        async with exclusive_persist():
            await retry_while_busy(lambda: self._write_open_children_cancelled(task_id))

    async def _write_open_children_cancelled(self, task_id: str) -> None:
        from omni.runtime.workflow_lifecycle import (
            cancelled_workflow_result,
            close_open_steps_for_cancel,
        )

        now = _utcnow()
        cancelled = cancelled_workflow_result()
        async with self._db.session() as session:
            runs = list(
                (
                    await session.execute(
                        select(WorkflowRunORM).where(
                            WorkflowRunORM.task_id == task_id,
                            WorkflowRunORM.status.in_(_ACTIVE_EXECUTION_STATUSES),
                        )
                    )
                ).scalars().all()
            )
            for run in runs:
                run.status = "cancelled"
                run.result_json = cancelled
                run.error = ""
                run.current_step_id = ""
                run.finished_at = now
                await close_open_steps_for_cancel(session, run.id)
            rows = list(
                (
                    await session.execute(
                        select(SubtaskORM).where(
                            SubtaskORM.task_id == task_id,
                            SubtaskORM.status.in_(_ACTIVE_EXECUTION_STATUSES),
                        )
                    )
                ).scalars().all()
            )
            for row in rows:
                row.status = "cancelled"
                row.error = ""
                row.result_json = {
                    "status": "cancelled",
                    "summary": f"{row.skill_name} was cancelled by the user.",
                    "recoverable": True,
                }
                row.finished_at = now
                row.owner_pid = 0
            await session.commit()

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
        """Compatibility wrapper for one fail-closed full-tree deletion."""
        outcome = await self.delete_tasks([task_id])
        return outcome.deleted_total > 0

    async def delete_tasks(
        self,
        task_ids: Sequence[str],
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> TaskClearOutcome:
        """Atomically delete exact task ids and every descendant Child Task.

        The complete closure is checked before any row is touched. Active rows
        are never deletable; protected rows require ``force``. Missing ids and
        either protection tier fail the whole requested batch closed. Artifact
        rows and files survive, while every deleted Task id is removed from the
        machine-global index after the workspace transaction commits.
        """
        outcome = TaskClearOutcome()
        roots = _unique_task_ids(task_ids)
        if not roots:
            return outcome
        async with self._db.session() as s:
            if not await _begin_task_delete_snapshot(s, outcome, dry_run=dry_run):
                return outcome
            rows = list((await s.execute(select(TaskORM))).scalars().all())
            rows_by_id = {row.id: row for row in rows}
            outcome.known_task_ids = list(rows_by_id)
            active_by_task = await _active_executions_by_task(s)
            outcome.missing_ids = [task_id for task_id in roots if task_id not in rows_by_id]
            if outcome.missing_ids:
                return outcome
            closure = _task_descendant_closure(rows_by_id, roots)
            ancestors = _task_ancestor_closure(
                rows_by_id,
                roots,
            )
            guarded_rows = [*closure, *ancestors]
            has_barrier = _protect_task_rows(outcome, guarded_rows, force=force)
            has_barrier = (
                _protect_active_executions(
                    outcome,
                    [row.id for row in guarded_rows],
                    active_by_task,
                )
                or has_barrier
            )
            if has_barrier:
                return outcome
            outcome.deleted_ids = [row.id for row in closure]
            delete_roots = _topmost_selected_task_ids(rows_by_id, outcome.deleted_ids)
            unrooted = _unrooted_task_ids(
                rows_by_id,
                outcome.deleted_ids,
                delete_roots,
            )
            if unrooted:
                _retain_task_rows(
                    outcome,
                    [rows_by_id[task_id] for task_id in unrooted],
                    reason="invalid_task_tree",
                )
                outcome.deleted_ids = []
                return outcome
            outcome.deleted_tasks = [_delete_item(row) for row in closure]
            for row in closure:
                _add_status_count(outcome.deleted, row.status)
            if dry_run:
                return outcome
            for batch in _task_id_batches(outcome.deleted_ids):
                await s.execute(
                    update(ArtifactORM)
                    .where(ArtifactORM.task_id.in_(batch))
                    .values(task_id=None)
                )
            for root_id in delete_roots:
                await s.delete(rows_by_id[root_id])
            await s.commit()
        await self._deindex(outcome.deleted_ids)
        return outcome

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
        considered (used by ``prune``). Every selected root is expanded to its
        complete Child-Task closure before policy checks; eligible trees then
        cascade to workflows, skill executions, events, and controls via FKs.
        """
        outcome = TaskClearOutcome()
        async with self._db.session() as s:
            if not await _begin_task_delete_snapshot(s, outcome, dry_run=dry_run):
                return outcome
            rows = list((await s.execute(select(TaskORM))).scalars().all())
            rows_by_id = {row.id: row for row in rows}
            outcome.known_task_ids = list(rows_by_id)
            active_by_task = await _active_executions_by_task(s)
            candidates: list[TaskORM] = []
            for r in rows:
                if status and r.status != status:
                    continue
                if kind and r.kind != kind:
                    continue
                if prunable_only and r.status not in _TASK_PRUNABLE_STATUSES:
                    continue
                # Age by completion time so a task settled recently (e.g. just
                # reconciled to interrupted) is not reaped in the same sweep.
                if before is not None and _aware_dt(r.finished_at or r.created_at) >= before:
                    continue
                if r.archived_at is not None and not include_archived:
                    _retain_task_rows(outcome, [r], reason="archived")
                    continue
                if r.status in _TASK_BLOCKED_STATUSES:
                    _protect_task_rows(outcome, [r], force=force)
                    continue
                if r.status in _TASK_PROTECTED_STATUSES and not force:
                    _protect_task_rows(outcome, [r], force=force)
                    continue
                candidates.append(r)

            candidate_ids = [row.id for row in candidates]
            candidate_roots = _topmost_selected_task_ids(rows_by_id, candidate_ids)
            for task_id in _unrooted_task_ids(
                rows_by_id,
                candidate_ids,
                candidate_roots,
            ):
                _retain_task_rows(
                    outcome,
                    [rows_by_id[task_id]],
                    reason="invalid_task_tree",
                )
            deletable: list[TaskORM] = []
            seen: set[str] = set()
            for root_id in candidate_roots:
                closure = _task_descendant_closure(rows_by_id, [root_id])
                ancestors = _task_ancestor_closure(rows_by_id, [root_id])
                guarded_rows = [*closure, *ancestors]
                has_barrier = _protect_task_rows(
                    outcome,
                    guarded_rows,
                    force=force,
                )
                if not include_archived:
                    has_barrier = (
                        _retain_task_rows(
                            outcome,
                            [row for row in guarded_rows if row.archived_at is not None],
                            reason="archived",
                        )
                        or has_barrier
                    )
                if before is not None:
                    has_barrier = (
                        _retain_task_rows(
                            outcome,
                            [
                                row
                                for row in guarded_rows
                                if _aware_dt(row.finished_at or row.created_at) >= before
                            ],
                            reason="newer_than_cutoff",
                        )
                        or has_barrier
                    )
                has_barrier = (
                    _protect_active_executions(
                        outcome,
                        [row.id for row in guarded_rows],
                        active_by_task,
                    )
                    or has_barrier
                )
                if has_barrier:
                    continue
                for row in closure:
                    if row.id not in seen:
                        seen.add(row.id)
                        deletable.append(row)
            outcome.deleted_ids = [row.id for row in deletable]
            outcome.deleted_tasks = [_delete_item(row) for row in deletable]
            for row in deletable:
                _add_status_count(outcome.deleted, row.status)
            if not dry_run:
                if outcome.deleted_ids:
                    for batch in _task_id_batches(outcome.deleted_ids):
                        await s.execute(
                            update(ArtifactORM)
                            .where(ArtifactORM.task_id.in_(batch))
                            .values(task_id=None)
                        )
                for root_id in _topmost_selected_task_ids(rows_by_id, outcome.deleted_ids):
                    await s.delete(rows_by_id[root_id])
                await s.commit()
        if not dry_run and outcome.deleted_ids:
            await self._deindex(outcome.deleted_ids)
        return outcome

    async def _session_external_key(self, session_id: str) -> str:
        async with self._db.session() as s:
            row = await s.get(SessionORM, session_id)
        return row.external_key if row is not None else ""


def _settlement_error(settled: Settlement) -> str:
    """Name what the record was missing, so a failure is actionable."""
    if unfounded := settled.detail.get("unfounded_claims"):
        return "the turn claimed work that left no record: " + ", ".join(unfounded)
    if lost := settled.detail.get("lost"):
        return f"{len(lost)} submitted task(s) did not complete"
    if missing := settled.detail.get("missing"):
        return f"{len(missing)} submitted task(s) have no record"
    return "task did not complete"


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
