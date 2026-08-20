"""Workspace-scoped Web control surface for the bundled SoulAgent Skill."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import select

from omni.agent.conversation_store import PERSONA_CONTROL_EXTERNAL_KEY
from omni.personas.catalog import persona_snapshot
from omni.personas.roots import persona_state_root
from omni.storage.models import SubtaskORM, TaskORM
from omni.web.protocol import RpcError

_MUTATING_ACTIONS = frozenset({"activate", "switch", "refresh", "unload"})
_HOST_OWNED_FIELDS = frozenset({"project_root", "kg_root", "host", "persona_text"})
_ACTIVE_TASK_STATUSES = frozenset(
    {"pending", "queued", "running", "recovering", "awaiting_approval"}
)
_TERMINAL_SKILL_STATUSES = frozenset(
    {
        "succeeded",
        "ok",
        "degraded",
        "failed",
        "cancelled",
        "interrupted",
        "skipped",
    }
)
_PERSONA_PROTOCOL_PREFIX = "$soulagent "
PERSONA_CONTROL_SESSION_TITLE = "Scientist persona"


def folder_persona_input(*, action: str, scientist_name: str = "") -> str:
    """Host-owned SoulAgent input: the same phrases CLI users type."""
    if action == "unload":
        return "restore yourself"
    name = scientist_name.strip() or "the selected scientist"
    return f"think like {name}"

try:  # POSIX advisory locks.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - platform dependent
    _fcntl = None  # type: ignore[assignment]

try:  # Windows byte-range advisory locks.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - platform dependent
    _msvcrt = None  # type: ignore[assignment]


def _try_lock(handle: Any) -> bool:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return True
    if _msvcrt is not None:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
        return True
    return True


def _release_lock(handle: Any) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
    elif _msvcrt is not None:
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


def _operation_from_task(
    row: TaskORM,
    *,
    persona_root: Path | None = None,
) -> dict[str, Any] | None:
    """Return only the browser-safe identity of a canonical persona turn."""
    raw = str(row.user_input or "").lstrip()
    if not raw.startswith(_PERSONA_PROTOCOL_PREFIX):
        return None
    try:
        request = json.loads(raw.removeprefix(_PERSONA_PROTOCOL_PREFIX))
    except (TypeError, ValueError):
        return None
    if not isinstance(request, dict):
        return None
    action = str(request.get("action") or "").strip().casefold()
    if action not in _MUTATING_ACTIONS:
        return None
    raw_root = str(request.get("project_root") or "").strip()
    if persona_root is not None and raw_root:
        try:
            if Path(raw_root).expanduser().resolve() != persona_root.resolve():
                return None
        except OSError:
            return None
    scientist_id = str(request.get("scientist_id") or "").strip()
    return {
        "task_id": row.id,
        "status": str(row.status or ""),
        "action": action,
        "scientist_id": scientist_id,
    }


async def _soulagent_skill_status(session: Any, task_id: str) -> str:
    row = (
        await session.execute(
            select(SubtaskORM)
            .where(
                SubtaskORM.task_id == task_id,
                SubtaskORM.skill_name == "soulagent",
            )
            .order_by(SubtaskORM.created_at.desc())
        )
    ).scalars().first()
    return str(row.status or "") if row is not None else ""


async def active_persona_operation(agent: Any, rec: Any = None) -> dict[str, Any] | None:
    """Find a Web persona write that has not yet reached a Skill terminal state."""
    persona_root = persona_state_root(rec.paths) if rec is not None else None
    async with agent.db.session() as session:
        rows = (
            await session.execute(
                select(TaskORM)
                .where(
                    TaskORM.kind == "turn",
                    TaskORM.parent_task_id.is_(None),
                    TaskORM.archived_at.is_(None),
                    TaskORM.status.in_(_ACTIVE_TASK_STATUSES),
                    TaskORM.user_input.startswith(_PERSONA_PROTOCOL_PREFIX),
                )
                .order_by(TaskORM.created_at.desc())
            )
        ).scalars().all()
        for row in rows:
            operation = _operation_from_task(row, persona_root=persona_root)
            if operation is None:
                continue
            skill_status = await _soulagent_skill_status(session, row.id)
            if skill_status in _TERMINAL_SKILL_STATUSES:
                continue
            return operation
    return None


@contextlib.asynccontextmanager
async def persona_admission(agent: Any, rec: Any) -> AsyncIterator[None]:
    """Serialize Web persona submission and reject an already-active one.

    The short kernel lock closes the check-then-submit race across browser tabs
    and separate ``omni web`` processes.  It is released as soon as the normal
    turn has persisted its Task; the durable Task row then remains the source
    of truth until SoulAgent reaches a terminal state.
    """
    lock_path = persona_state_root(rec.paths) / ".soulagent" / "web-persona-admission.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    deadline = time.monotonic() + 5.0
    try:
        while not acquired:
            try:
                acquired = _try_lock(handle)
            except OSError:
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.05)
        if not acquired:
            raise RpcError("busy", "another scientist-persona operation is being submitted")

        operation = await active_persona_operation(agent, rec)
        if operation is not None:
            raise RpcError(
                "busy",
                "a scientist-persona operation is already running in this folder",
                task_id=operation["task_id"],
            )
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                _release_lock(handle)
        handle.close()


def _public_snapshot(
    payload: dict[str, Any],
    *,
    writable: bool,
    operation: dict[str, Any] | None,
) -> dict[str, Any]:
    project_root = Path(str(payload.get("project_root") or ""))
    kg_root = Path(str(payload.get("kg_root") or ""))
    scanner = "project" if kg_root == project_root / "scientist-kg" else "home"
    available = [
        {
            "scientist_id": str(item.get("scientist_id") or ""),
            "scientist_name": str(item.get("scientist_name") or ""),
            "aliases": [str(alias) for alias in item.get("aliases") or []],
        }
        for item in payload.get("available") or []
        if isinstance(item, dict)
    ]
    invalid = [
        {
            "directory": str(item.get("directory") or ""),
            "error": str(item.get("error") or "invalid scientist persona"),
        }
        for item in payload.get("invalid") or []
        if isinstance(item, dict)
    ]
    return {
        "active": bool(payload.get("active")),
        "scientist_id": str(payload.get("scientist_id") or ""),
        "scientist_name": str(payload.get("scientist_name") or ""),
        "scanner": scanner,
        "writable": writable,
        "available": available,
        "invalid": invalid,
        "operation": operation,
    }


async def describe_persona(agent: Any, rec: Any) -> dict[str, Any]:
    """Project a stable, shared persona snapshot off the ASGI event loop."""
    operation_before = await active_persona_operation(agent, rec)
    payload = await asyncio.to_thread(
        persona_snapshot,
        rec.paths,
        repair_incomplete_unload=False,
        metadata_only=True,
    )
    operation_after = await active_persona_operation(agent, rec)
    return _public_snapshot(
        payload,
        writable=bool(rec.writable),
        # A SoulAgent Skill still writing keeps mutation locked. A terminal
        # Skill is not an operation, even if the parent turn is still wrapping up.
        operation=operation_after or operation_before,
    )


async def _persona_control_session(agent: Any) -> str:
    return await agent.ensure_session(
        channel="web",
        external_key=PERSONA_CONTROL_EXTERNAL_KEY,
        title=PERSONA_CONTROL_SESSION_TITLE,
    )


async def persona_turn_request(
    agent: Any,
    rec: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Validate a UI selection and compile the explicit SoulAgent protocol turn."""
    forbidden = sorted(field for field in _HOST_OWNED_FIELDS if field in params)
    if forbidden:
        raise RpcError(
            "invalid_params",
            f"persona.start does not accept host-owned fields: {', '.join(forbidden)}",
        )
    action = str(params.get("action") or "").strip().casefold()
    if action not in _MUTATING_ACTIONS:
        raise RpcError("invalid_params", f"unsupported persona action: {action or '(empty)'}")

    snapshot = await describe_persona(agent, rec)
    scientist_id = str(params.get("scientist_id") or "").strip()

    if action == "unload":
        payload: dict[str, Any] = {
            "input": folder_persona_input(action="unload"),
            "action": "unload",
            "project_root": str(persona_state_root(rec.paths)),
        }
    else:
        if not scientist_id and action == "refresh":
            scientist_id = str(snapshot.get("scientist_id") or "")
        known_personas = {
            str(item.get("scientist_id") or ""): str(item.get("scientist_name") or "")
            for item in snapshot.get("available") or []
            if isinstance(item, dict)
        }
        if not scientist_id or scientist_id not in known_personas:
            raise RpcError("not_found", f"scientist persona is not installed: {scientist_id}")
        payload = {
            "input": folder_persona_input(
                action=action,
                scientist_name=known_personas[scientist_id],
            ),
            "action": action,
            "scientist_id": scientist_id,
            "force": bool(params.get("force", action == "refresh")),
            "project_root": str(persona_state_root(rec.paths)),
        }

    return {
        "text": "$soulagent " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "session_id": await _persona_control_session(agent),
        "interaction_mode": "auto",
        "file_uris": [],
        "client_run_id": str(params.get("client_run_id") or ""),
    }


async def persona_operation_status(
    agent: Any,
    task_id: str,
) -> dict[str, Any]:
    """Return the minimal, non-sensitive settlement contract for one UI action."""
    if not task_id:
        raise RpcError("invalid_params", "persona.status requires task_id")
    from omni.cli.commands.tasks_cmd import task_detail_payload

    payload = await task_detail_payload(agent, task_id)
    if payload is None:
        raise RpcError("not_found", f"task not found: {task_id}")
    task, _events, _workflows, _steps, subtasks, _children = payload
    if task.id != task_id:
        raise RpcError("not_found", f"task not found: {task_id}")
    outcome_code = ""
    skill_status = ""
    for subtask in reversed(subtasks):
        if str(subtask.skill_name or "").casefold() != "soulagent":
            continue
        skill_status = str(subtask.status or "")
        result = subtask.result_json if isinstance(subtask.result_json, dict) else {}
        outcome = result.get("outcome")
        if isinstance(outcome, dict):
            code = outcome.get("code")
            outcome_code = code if isinstance(code, str) else ""
        break
    return {
        "task_id": task.id,
        "task_status": str(task.status or ""),
        "skill_status": skill_status,
        "outcome_code": outcome_code,
    }
