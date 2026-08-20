"""Read projections from the selected store (sessions, tasks, artifacts, ROM)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from omni.agent import OmniAgent
from omni.agent.conversation_store import PERSONA_CONTROL_EXTERNAL_KEY
from omni.config.paths import OmniPaths
from omni.research.store import ResearchStore
from omni.storage.models import (
    MESSAGE_ORDER_ASC,
    MESSAGE_ORDER_DESC,
    ArtifactORM,
    ConversationMessageORM,
    SubtaskORM,
    TaskEventORM,
    TaskORM,
)
from omni.web.activity import (
    FOLLOWABLE_TASK_STATUSES,
    display_title,
    is_soulagent_turn_input,
    persona_turn_display_title,
    project_events_after,
    public_skill_payload,
    public_skill_text,
)
from omni.web.protocol import RpcError, jsonable, utc_iso
from omni.web.workspace import OpenedWorkspace, WorkspaceHub

_EXECUTING_TASK_STATUSES = {"running", "recovering"}
_ERROR_PREVIEW = 160
_ROOT_TURN = (
    TaskORM.kind == "turn",
    TaskORM.parent_task_id.is_(None),
    TaskORM.archived_at.is_(None),
)


def _ts(value: Any) -> str | None:
    return utc_iso(value)


def session_dict(row: Any, *, messages: int | None = None) -> dict[str, Any]:
    title = row.title or ""
    data = {
        "id": row.id,
        "title": title,
        "display_title": display_title(title=title),
        "channel": row.channel or "cli",
        "status": row.status or "active",
        "external_key": row.external_key or "",
        "created_at": _ts(row.created_at),
        "updated_at": _ts(row.updated_at),
        "last_activity_at": _ts(row.updated_at),
    }
    if messages is not None:
        data["messages"] = messages
    return data


def message_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "role": row.role,
        "content": row.content or "",
        "content_type": row.content_type or "text",
        "name": row.name or "",
        "created_at": _ts(row.created_at),
        "meta": jsonable(row.meta or {}),
    }


def task_summary(row: Any) -> dict[str, Any]:
    projection_skill = (
        "soulagent" if is_soulagent_turn_input(getattr(row, "user_input", "")) else ""
    )
    title = (
        persona_turn_display_title(row.user_input)
        if projection_skill
        else row.title or ""
    )
    return {
        "id": row.id,
        "session_id": row.session_id or "",
        "parent_task_id": row.parent_task_id or "",
        "channel": row.channel or "",
        "status": row.status or "",
        "kind": row.kind or "turn",
        "title": title,
        "user_input": getattr(row, "user_input", "") or "",
        "summary": public_skill_text(projection_skill, row.summary),
        "error": public_skill_text(projection_skill, row.error, error=True),
        "current_stage": row.current_stage or "",
        "current_tool": getattr(row, "current_tool", "") or "",
        "current_workflow_id": getattr(row, "current_workflow_id", "") or "",
        "current_subtask_id": getattr(row, "current_subtask_id", "") or "",
        "attempt": int(getattr(row, "attempt", 1) or 1),
        "retry_of_task_id": getattr(row, "retry_of_task_id", "") or "",
        "root_task_id": getattr(row, "root_task_id", "") or "",
        "created_at": _ts(row.created_at),
        "started_at": _ts(getattr(row, "started_at", None)),
        "updated_at": _ts(getattr(row, "updated_at", None)),
        "finished_at": _ts(getattr(row, "finished_at", None)),
    }


def _artifact_presentation_role(row: Any) -> str:
    """Return the durable artifact's primary/support presentation role."""
    meta = row.meta if isinstance(getattr(row, "meta", None), dict) else {}
    declared = str(meta.get("presentation_role") or "").strip().lower()
    if declared in {"primary", "support"}:
        return declared
    source = str(row.rel_path or row.uri or "")
    name = Path(source).name.casefold()
    kind = str(row.kind or "").strip().lower()
    if kind in {"provenance", "manifest", "input"} or name.endswith(
        (".provenance.json", ".figure-bundle.json")
    ):
        return "support"
    return "primary"


def artifact_dict(row: Any, *, path: str = "") -> dict[str, Any]:
    return {
        "id": row.id,
        "session_id": row.session_id or "",
        "task_id": row.task_id or "",
        "subtask_id": row.subtask_id or "",
        "workflow_run_id": row.workflow_run_id or "",
        "kind": row.kind or "file",
        "title": row.title or "",
        "uri": row.uri or "",
        "rel_path": row.rel_path or "",
        "path": path,
        "mime": row.mime or "",
        "size_bytes": int(row.size_bytes or 0),
        "presentation_role": _artifact_presentation_role(row),
        "created_at": _ts(row.created_at),
    }


def path_allowed(path: Path, paths: OmniPaths) -> bool:
    """True when ``path`` sits in this workspace's store or opened directory."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    roots = [paths.project_dir]
    if paths.workspace_root is not None:
        roots.append(paths.workspace_root)
    if paths.invocation_cwd is not None:
        roots.append(paths.invocation_cwd)
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


async def list_sessions(
    agent: OmniAgent,
    *,
    limit: int = 50,
    channel: str = "",
    hub: WorkspaceHub | None = None,
    rec: OpenedWorkspace | None = None,
) -> list[dict[str, Any]]:
    rows = await agent.list_sessions(limit=limit)
    out = [
        session_dict(row, messages=n)
        for row, n in rows
        if (row.external_key or "") != PERSONA_CONTROL_EXTERNAL_KEY
    ]
    if channel:
        out = [item for item in out if item["channel"] == channel]
    await _enrich_sessions(agent, out, hub=hub, rec=rec)
    out.sort(
        key=lambda item: (
            item.get("last_activity_at") or item.get("created_at") or "",
            item.get("id") or "",
        ),
        reverse=True,
    )
    return out


async def get_session(
    agent: OmniAgent,
    session_id: str,
    *,
    hub: WorkspaceHub | None = None,
    rec: OpenedWorkspace | None = None,
) -> dict[str, Any]:
    row = await agent.get_session(session_id)
    if row is None:
        raise RpcError("not_found", f"session not found: {session_id}")
    data = session_dict(row)
    await _enrich_sessions(agent, [data], hub=hub, rec=rec)
    return data


async def rename_session(agent: OmniAgent, session_id: str, title: str) -> dict[str, Any]:
    row = await agent.rename_session(session_id, title)
    if row is None:
        raise RpcError("not_found", f"session not found: {session_id}")
    data = session_dict(row)
    await _enrich_sessions(agent, [data])
    return data


async def delete_session(
    agent: OmniAgent,
    session_id: str,
    *,
    hub: WorkspaceHub | None = None,
    rec: OpenedWorkspace | None = None,
) -> dict[str, Any]:
    """Delete a session after refusing a live web turn on that thread."""
    row = await agent.get_session(session_id)
    if row is None:
        raise RpcError("not_found", f"session not found: {session_id}")
    if hub is not None and rec is not None:
        live = hub.runs.by_session(rec.key, row.id)
        if live is not None and not live.done:
            raise RpcError(
                "busy",
                "session has a running turn",
                session_id=row.id,
                task_id=live.task_id,
                client_run_id=live.client_run_id,
            )
    outcome = await agent.delete_session(row.id)
    if not outcome.deleted:
        raise RpcError(outcome.code or "error", outcome.message, session_id=outcome.session_id)
    return {
        "session_id": outcome.session_id,
        "deleted_task_ids": list(outcome.deleted_task_ids),
    }


async def _enrich_sessions(
    agent: OmniAgent,
    sessions: list[dict[str, Any]],
    *,
    hub: WorkspaceHub | None = None,
    rec: OpenedWorkspace | None = None,
) -> None:
    ids = [str(item.get("id") or "") for item in sessions if item.get("id")]
    if not ids:
        return
    live = hub.runs.live_task_ids(rec.key) if hub is not None and rec is not None else set()
    async with agent.db.session() as session:
        task_rows = list(
            (
                await session.execute(
                    select(TaskORM).where(
                        TaskORM.session_id.in_(ids),
                        TaskORM.kind == "turn",
                        TaskORM.parent_task_id.is_(None),
                        TaskORM.archived_at.is_(None),
                    )
                )
            ).scalars().all()
        )
        message_rows = list(
            (
                await session.execute(
                    select(ConversationMessageORM)
                    .where(
                        ConversationMessageORM.session_id.in_(ids),
                        ConversationMessageORM.role == "user",
                    )
                    .order_by(*MESSAGE_ORDER_ASC)
                )
            ).scalars().all()
        )
    first_task: dict[str, TaskORM] = {}
    latest_task: dict[str, TaskORM] = {}
    for task in task_rows:
        sid = task.session_id
        current_first = first_task.get(sid)
        if current_first is None or (task.created_at and current_first.created_at and task.created_at < current_first.created_at):
            first_task[sid] = task
        current_latest = latest_task.get(sid)
        stamp = task.finished_at or task.started_at or task.created_at
        latest_stamp = (
            (current_latest.finished_at or current_latest.started_at or current_latest.created_at)
            if current_latest is not None
            else None
        )
        if current_latest is None or (stamp and (latest_stamp is None or stamp > latest_stamp)):
            latest_task[sid] = task
    first_user: dict[str, str] = {}
    for msg in message_rows:
        if msg.session_id in first_user:
            continue
        if (msg.meta or {}).get("compacted"):
            continue
        if (msg.content_type or "text") not in {"", "text"}:
            continue
        text = " ".join((msg.content or "").split())
        if text:
            first_user[msg.session_id] = text
    for item in sessions:
        sid = str(item["id"])
        first = first_task.get(sid)
        latest = latest_task.get(sid)
        item["display_title"] = display_title(
            title=str(item.get("title") or ""),
            task_input=first.user_input if first is not None else "",
            user_message=first_user.get(sid, ""),
        )
        item["first_task_id"] = first.id if first is not None else ""
        item["first_task_at"] = _ts(first.created_at) if first is not None else None
        item["latest_task_id"] = latest.id if latest is not None else ""
        item["latest_task_status"] = latest.status if latest is not None else ""
        item["latest_task_at"] = (
            _ts(latest.finished_at or latest.started_at or latest.created_at) if latest is not None else None
        )
        activity = item.get("latest_task_at") or item.get("updated_at") or item.get("created_at")
        item["last_activity_at"] = activity
        status = str(item.get("latest_task_status") or "")
        if latest is not None and latest.id in live:
            item["worker"] = "live"
        elif status in _EXECUTING_TASK_STATUSES:
            item["worker"] = "external"
        elif status == "interrupted":
            item["worker"] = "interrupted"
        else:
            item["worker"] = ""


def _apply_worker(item: dict[str, Any], latest: TaskORM | None, live: set[str]) -> None:
    status = str(item.get("latest_task_status") or "")
    if latest is not None and latest.id in live:
        item["worker"] = "live"
    elif status in _EXECUTING_TASK_STATUSES:
        item["worker"] = "external"
    elif status == "interrupted":
        item["worker"] = "interrupted"
    else:
        item["worker"] = ""


async def _root_turns_for_sessions(
    agent: OmniAgent, ids: list[str]
) -> tuple[dict[str, TaskORM], dict[str, TaskORM]]:
    if not ids:
        return {}, {}
    async with agent.db.session() as session:
        first_stamp = (
            select(TaskORM.session_id, func.min(TaskORM.created_at).label("stamp"))
            .where(TaskORM.session_id.in_(ids), *_ROOT_TURN)
            .group_by(TaskORM.session_id)
            .subquery()
        )
        latest_stamp = (
            select(TaskORM.session_id, func.max(TaskORM.created_at).label("stamp"))
            .where(TaskORM.session_id.in_(ids), *_ROOT_TURN)
            .group_by(TaskORM.session_id)
            .subquery()
        )
        first_rows = list(
            (
                await session.execute(
                    select(TaskORM).join(
                        first_stamp,
                        (TaskORM.session_id == first_stamp.c.session_id)
                        & (TaskORM.created_at == first_stamp.c.stamp)
                        & (TaskORM.kind == "turn")
                        & TaskORM.parent_task_id.is_(None)
                        & TaskORM.archived_at.is_(None),
                    )
                )
            ).scalars().all()
        )
        latest_rows = list(
            (
                await session.execute(
                    select(TaskORM).join(
                        latest_stamp,
                        (TaskORM.session_id == latest_stamp.c.session_id)
                        & (TaskORM.created_at == latest_stamp.c.stamp)
                        & (TaskORM.kind == "turn")
                        & TaskORM.parent_task_id.is_(None)
                        & TaskORM.archived_at.is_(None),
                    )
                )
            ).scalars().all()
        )
    first = {str(row.session_id): row for row in first_rows}
    latest = {str(row.session_id): row for row in latest_rows}
    return first, latest


async def _enrich_inbox(
    agent: OmniAgent,
    sessions: list[dict[str, Any]],
    *,
    hub: WorkspaceHub | None = None,
    rec: OpenedWorkspace | None = None,
) -> None:
    ids = [str(item.get("id") or "") for item in sessions if item.get("id")]
    if not ids:
        return
    live = hub.runs.live_task_ids(rec.key) if hub is not None and rec is not None else set()
    first_task, latest_task = await _root_turns_for_sessions(agent, ids)
    for item in sessions:
        sid = str(item["id"])
        first = first_task.get(sid)
        latest = latest_task.get(sid)
        item["display_title"] = display_title(
            title=str(item.get("title") or ""),
            task_input=first.user_input if first is not None else "",
        )
        item["first_task_id"] = first.id if first is not None else ""
        item["first_task_at"] = _ts(first.created_at) if first is not None else None
        item["latest_task_id"] = latest.id if latest is not None else ""
        item["latest_task_status"] = latest.status if latest is not None else ""
        item["latest_task_at"] = (
            _ts(latest.finished_at or latest.started_at or latest.created_at)
            if latest is not None
            else None
        )
        item["last_activity_at"] = (
            item.get("latest_task_at") or item.get("updated_at") or item.get("created_at")
        )
        _apply_worker(item, latest, live)


async def _session_fingerprint(
    agent: OmniAgent,
    session: dict[str, Any],
    *,
    messages: list[dict[str, Any]] | None = None,
    turns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    session_id = str(session.get("id") or "")
    last_message_id = ""
    message_count = session.get("messages")
    if messages is not None:
        message_count = len(messages)
        last_message_id = str(messages[-1]["id"]) if messages else ""
    elif session_id:
        async with agent.db.session() as db:
            last = (
                await db.execute(
                    select(ConversationMessageORM.id)
                    .where(ConversationMessageORM.session_id == session_id)
                    .order_by(*MESSAGE_ORDER_DESC)
                    .limit(1)
                )
            ).scalar_one_or_none()
        last_message_id = str(last or "")
        if message_count is None:
            async with agent.db.session() as db:
                message_count = int(
                    (
                        await db.execute(
                            select(func.count())
                            .select_from(ConversationMessageORM)
                            .where(ConversationMessageORM.session_id == session_id)
                        )
                    ).scalar_one()
                    or 0
                )
    latest_id = str(session.get("latest_task_id") or "")
    latest_status = str(session.get("latest_task_status") or "")
    if turns:
        latest_id = str(turns[0].get("id") or latest_id)
        latest_status = str(turns[0].get("status") or latest_status)
    latest_event_seq = 0
    if latest_id:
        async with agent.db.session() as db:
            latest_event_seq = int(
                (
                    await db.execute(
                        select(func.max(TaskEventORM.seq)).where(
                            TaskEventORM.task_id == latest_id
                        )
                    )
                ).scalar_one()
                or 0
            )
    return {
        "session_id": session_id,
        "message_count": int(message_count or 0),
        "last_message_id": last_message_id,
        "latest_task_id": latest_id,
        "latest_task_status": latest_status,
        "latest_event_seq": latest_event_seq,
        "updated_at": session.get("updated_at"),
        "last_activity_at": session.get("last_activity_at"),
    }


def _execution_summary(sub: SubtaskORM, *, artifact_count: int = 0) -> dict[str, Any]:
    return {
        "object_kind": "skill_execution",
        "object_id": sub.id,
        "id": sub.id,
        "subtask_id": sub.id,
        "task_id": sub.task_id or "",
        "skill": sub.skill_name,
        "skill_name": sub.skill_name,
        "status": sub.status,
        "attempt": int(sub.attempt or 0),
        "step_attempt": int(sub.step_attempt or 1),
        "workflow_run_id": sub.workflow_run_id or "",
        "workflow_step_id": sub.workflow_step_id or "",
        "error": (sub.error or "")[:_ERROR_PREVIEW],
        "artifact_count": int(artifact_count or 0),
        "created_at": _ts(sub.created_at),
        "started_at": _ts(sub.started_at),
        "finished_at": _ts(sub.finished_at),
    }


async def _executions_for_tasks(
    agent: OmniAgent, task_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    ids = [task_id for task_id in task_ids if task_id]
    if not ids:
        return {}
    async with agent.db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(SubtaskORM)
                    .where(
                        SubtaskORM.task_id.in_(ids),
                        SubtaskORM.archived_at.is_(None),
                    )
                    .order_by(SubtaskORM.created_at.asc())
                )
            ).scalars().all()
        )
        counts = {
            str(task_id): int(count or 0)
            for task_id, count in (
                await session.execute(
                    select(ArtifactORM.subtask_id, func.count())
                    .where(
                        ArtifactORM.task_id.in_(ids),
                        ArtifactORM.subtask_id.is_not(None),
                    )
                    .group_by(ArtifactORM.subtask_id)
                )
            ).all()
        }
    grouped: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in ids}
    for row in rows:
        grouped.setdefault(str(row.task_id or ""), []).append(
            _execution_summary(row, artifact_count=counts.get(row.id, 0))
        )
    return grouped


async def workspace_inbox(
    agent: OmniAgent,
    *,
    limit: int = 50,
    channel: str = "",
    session_id: str = "",
    hub: WorkspaceHub | None = None,
    rec: OpenedWorkspace | None = None,
) -> dict[str, Any]:
    """Cheap session fingerprints for the visible-page poll.

    Skips the full user-message scan used by ``session.list`` titles. Sidebar
    copy uses the stored title plus the first root-turn input already loaded
    for latest-task status.
    """
    rows = await agent.list_sessions(limit=limit)
    sessions = [
        session_dict(row, messages=n)
        for row, n in rows
        if (row.external_key or "") != PERSONA_CONTROL_EXTERNAL_KEY
    ]
    if channel:
        sessions = [item for item in sessions if item["channel"] == channel]
    await _enrich_inbox(agent, sessions, hub=hub, rec=rec)
    sessions.sort(
        key=lambda item: (
            item.get("last_activity_at") or item.get("created_at") or "",
            item.get("id") or "",
        ),
        reverse=True,
    )
    focus_id = str(session_id or "").strip()
    focus = None
    if focus_id:
        focus = next((item for item in sessions if item["id"] == focus_id), None)
        if focus is None:
            try:
                focus = await get_session(agent, focus_id, hub=hub, rec=rec)
            except RpcError:
                focus = None
        if focus is not None:
            focus = dict(focus)
            focus.update(await _session_fingerprint(agent, focus))
    return {"sessions": sessions, "focus": focus}


async def session_timeline(
    agent: OmniAgent,
    session_id: str,
    *,
    hub: WorkspaceHub | None = None,
    rec: OpenedWorkspace | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """One conversation: messages, root turns, and compact executions."""
    session = await get_session(agent, session_id, hub=hub, rec=rec)
    messages = await session_messages(agent, session["id"])
    turns = await list_tasks(agent, limit=limit, session_id=session["id"])
    executions = await _executions_for_tasks(agent, [str(item["id"]) for item in turns])
    for item in turns:
        item["executions"] = executions.get(str(item["id"]), [])
    fingerprint = await _session_fingerprint(agent, session, messages=messages, turns=turns)
    return {
        "session": session,
        "messages": messages,
        "turns": turns,
        "fingerprint": fingerprint,
        "followable": bool(
            fingerprint.get("latest_task_status") in FOLLOWABLE_TASK_STATUSES
        ),
    }


async def session_messages(agent: OmniAgent, session_id: str) -> list[dict[str, Any]]:
    row = await agent.get_session(session_id)
    if row is None:
        raise RpcError("not_found", f"session not found: {session_id}")
    msgs = await agent.session_messages(row.id)
    return [message_dict(m) for m in msgs]


async def create_session(agent: OmniAgent, *, title: str = "") -> dict[str, Any]:
    session_id = await agent.ensure_session(channel="web", title=title)
    row = await agent.get_session(session_id)
    if row is None:
        raise RpcError("create_failed", "session was created but could not be reloaded")
    return session_dict(row, messages=0)


async def list_tasks(
    agent: OmniAgent,
    *,
    limit: int = 40,
    session_id: str = "",
) -> list[dict[str, Any]]:
    if session_id:
        rows = [
            row
            for row in await agent.tasks.list_tasks_for_session(session_id)
            if row.kind == "turn"
            and row.parent_task_id is None
            and row.archived_at is None
        ]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        rows = rows[:limit]
    else:
        rows = await agent.tasks.list_tasks(limit=limit, kind="turn")
    return [task_summary(row) for row in rows]


async def list_task_events(
    agent: OmniAgent,
    task_id: str,
    *,
    after_seq: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    task = await agent.tasks.get_task(task_id)
    if task is None:
        raise RpcError("not_found", f"task not found: {task_id}")
    projection_skill = (
        "soulagent" if is_soulagent_turn_input(task.user_input) else ""
    )
    events = await project_events_after(
        agent,
        task_id,
        after_seq=after_seq,
        limit=limit,
        fallback_skill_name=projection_skill,
    )
    return {
        "task": task_summary(task),
        "events": events,
        "after_seq": max(0, int(after_seq or 0)),
        "last_seq": int(events[-1]["seq"]) if events else max(0, int(after_seq or 0)),
    }


async def get_task(agent: OmniAgent, task_id: str) -> dict[str, Any]:
    from omni.cli.commands.tasks_cmd import task_detail_payload

    payload = await task_detail_payload(agent, task_id)
    if payload is None:
        raise RpcError("not_found", f"task not found: {task_id}")
    task, events, workflows, steps, subtasks, children = payload
    root_projection_skill = (
        "soulagent" if is_soulagent_turn_input(task.user_input) else ""
    )
    workflow_items = [
        {
            "object_kind": "workflow_run",
            "object_id": wf.id,
            "id": wf.id,
            "workflow_run_id": wf.id,
            "task_id": wf.task_id,
            "session_id": wf.session_id or "",
            "status": wf.status,
            "title": getattr(wf, "goal", "") or "",
            "goal": getattr(wf, "goal", "") or "",
            "current_step_id": wf.current_step_id or "",
            "attempt": int(wf.attempt or 0),
            "retry_of": wf.retry_of or "",
            "resume_of": wf.resume_of or "",
            "error": wf.error or "",
            "step_ids": [step.id for step in steps if step.workflow_run_id == wf.id],
            "execution_ids": [sub.id for sub in subtasks if sub.workflow_run_id == wf.id],
            "created_at": _ts(wf.created_at),
            "started_at": _ts(wf.started_at),
            "finished_at": _ts(wf.finished_at),
        }
        for wf in workflows
    ]
    step_items = [
        {
            "object_kind": "workflow_step",
            "object_id": step.id,
            "id": step.id,
            "workflow_step_id": step.id,
            "workflow_run_id": step.workflow_run_id,
            "task_id": step.task_id,
            "step_id": step.step_key,
            "name": step.skill_name or step.step_key,
            "skill_name": step.skill_name or "",
            "capability": step.capability or "",
            "provider_type": step.provider_type or "",
            "deliverable": step.deliverable or "",
            "status": step.status,
            "position": step.position,
            "required": bool(step.required),
            "depends_on": jsonable(step.depends_on or []),
            "optional_depends_on": jsonable(step.optional_depends_on or []),
            "allow_failed_dependencies": bool(step.allow_failed_dependencies),
            "failure_policy": step.failure_policy or "",
            "current_execution_id": step.current_execution_id or "",
            "execution_ids": jsonable(step.execution_ids or []),
            "child_task_id": step.child_task_id or "",
            "child_task_ids": jsonable(step.child_task_ids or []),
            "error": public_skill_text(
                str(step.skill_name or ""), step.error, error=True
            ),
            "warning": public_skill_text(str(step.skill_name or ""), step.warning),
            "recoverable": bool(step.recoverable),
            "created_at": _ts(step.created_at),
            "started_at": _ts(step.started_at),
            "finished_at": _ts(step.finished_at),
        }
        for step in steps
    ]
    execution_items = [
        {
            "object_kind": "skill_execution",
            "object_id": sub.id,
            "id": sub.id,
            "subtask_id": sub.id,
            "task_id": sub.task_id or "",
            "workflow_run_id": sub.workflow_run_id or "",
            "workflow_step_id": sub.workflow_step_id or "",
            "parent_event_id": sub.parent_event_id or "",
            "skill": sub.skill_name,
            "skill_name": sub.skill_name,
            "status": sub.status,
            "session_id": sub.session_id or "",
            "attempt": int(sub.attempt or 0),
            "step_attempt": int(sub.step_attempt or 1),
            "retry_of": sub.retry_of or "",
            "resume_of": sub.resume_of or "",
            "original_error": public_skill_text(
                str(sub.skill_name or ""), sub.original_error, error=True
            ),
            "recovery_attempt": int(sub.recovery_attempt or 0),
            "recovery_policy": sub.recovery_policy or "",
            "error": public_skill_text(
                str(sub.skill_name or ""), sub.error, error=True
            ),
            "input_json": public_skill_payload(
                str(sub.skill_name or ""),
                sub.input_json,
                result=False,
            ),
            "result_json": public_skill_payload(
                str(sub.skill_name or ""),
                sub.result_json,
                result=True,
            ),
            "created_at": _ts(sub.created_at),
            "started_at": _ts(sub.started_at),
            "finished_at": _ts(sub.finished_at),
        }
        for sub in subtasks
    ]
    return {
        "task": task_summary(task),
        "events": [
            {
                "id": ev.id,
                "event_id": ev.id,
                "task_id": ev.task_id,
                "seq": int(ev.seq or 0),
                "event_type": ev.event_type,
                "status": ev.status,
                "lifecycle_status": ev.lifecycle_status or "",
                "result_success": ev.result_success,
                "name": public_skill_text(
                    str(ev.skill_name or root_projection_skill), ev.name
                ),
                "tool_name": ev.tool_name or "",
                "skill_name": ev.skill_name or "",
                "workflow_run_id": ev.workflow_run_id or "",
                "workflow_step_id": ev.workflow_step_id or "",
                "subtask_id": ev.subtask_id or "",
                "step_id": ev.step_id or "",
                "summary": public_skill_text(
                    str(ev.skill_name or root_projection_skill), ev.summary
                ),
                "created_at": _ts(ev.created_at),
                "input_json": public_skill_payload(
                    str(ev.skill_name or root_projection_skill),
                    ev.input_json,
                    result=False,
                ),
                "output": public_skill_payload(
                    str(ev.skill_name or root_projection_skill),
                    ev.output_json,
                    result=True,
                ),
                "output_json": public_skill_payload(
                    str(ev.skill_name or root_projection_skill),
                    ev.output_json,
                    result=True,
                ),
                "error": public_skill_text(
                    str(ev.skill_name or root_projection_skill), ev.error, error=True
                ),
                "pct": ev.pct,
                "duration_ms": ev.duration_ms,
            }
            for ev in events
        ],
        "workflows": workflow_items,
        "steps": step_items,
        "executions": execution_items,
        "subtasks": execution_items,
        "children": [task_summary(child) for child in children],
    }


async def list_artifacts(
    agent: OmniAgent,
    *,
    session_id: str = "",
    task_id: str = "",
    limit: int = 40,
) -> list[dict[str, Any]]:
    if task_id:
        task = await agent.tasks.get_task(task_id)
        if task is None:
            raise RpcError("not_found", f"task not found: {task_id}")
        if session_id and task.session_id != session_id:
            raise RpcError("not_found", f"task not found in session: {task_id}")
        rows = await agent.artifacts.list_by_task(task.id)
    elif session_id:
        rows = await agent.artifacts.list_by_session(session_id, limit=limit)
    else:
        rows = await agent.artifacts.list_recent(limit=limit)
    out: list[dict[str, Any]] = []
    for row in rows:
        resolved = await agent.artifacts.resolve_path(row.uri)
        path = str(resolved) if resolved is not None else ""
        if path and not path_allowed(Path(path), agent.paths):
            path = ""
        out.append(artifact_dict(row, path=path))
    return out


async def get_artifact(agent: OmniAgent, ident: str) -> dict[str, Any]:
    uri = ident if ident.startswith("artifact://") else f"artifact://{ident}"
    row = await agent.artifacts.get(uri)
    if row is None:
        raise RpcError("not_found", f"artifact not found: {ident}")
    resolved = await agent.artifacts.resolve_path(row.uri)
    path = str(resolved) if resolved is not None else ""
    if path and not path_allowed(Path(path), agent.paths):
        raise RpcError("forbidden", "artifact path is outside this workspace")
    preview = ""
    if resolved is not None and resolved.is_file() and (row.mime or "").startswith("text/"):
        try:
            preview = resolved.read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            preview = ""
    data = artifact_dict(row, path=path)
    data["preview"] = preview
    return data


async def rom_snapshot(agent: OmniAgent) -> dict[str, Any]:
    store = ResearchStore(agent.db)
    hyps = await store.list_hypotheses(limit=40)
    claims = await store.list_claims(limit=40)
    sources = await store.list_sources(limit=40)
    runs = await store.list_runs(limit=20)
    counts = await store.counts()
    return {
        "counts": counts,
        "hypotheses": [
            {
                "id": h.id,
                "statement": h.statement,
                "status": h.status,
                "confidence": h.confidence,
                "updated_at": _ts(h.updated_at),
            }
            for h in hyps
        ],
        "claims": [
            {
                "id": c.id,
                "text": c.text,
                "polarity": c.polarity,
                "confidence": c.confidence,
                "hypothesis_id": c.hypothesis_id,
            }
            for c in claims
        ],
        "sources": [
            {
                "id": s.id,
                "title": s.title,
                "arxiv_id": s.arxiv_id,
                "doi": s.doi,
                "year": s.year,
                "venue": s.venue,
            }
            for s in sources
        ],
        "runs": [
            {
                "id": r.id,
                "title": getattr(r, "title", "") or getattr(r, "name", "") or "",
                "status": getattr(r, "status", "") or "",
            }
            for r in runs
        ],
    }


def notebook_text(paths: OmniPaths) -> str:
    notebook = paths.notebook
    if not notebook.is_file():
        return ""
    try:
        return notebook.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


async def cost_snapshot(agent: OmniAgent, *, session_id: str = "", task_id: str = "") -> dict[str, Any]:
    target = task_id
    if not target and session_id:
        latest = await agent.tasks.latest_task_for_session(session_id)
        target = latest.id if latest is not None else ""
    if not target:
        tasks = await agent.tasks.list_tasks(limit=8, kind="turn")
        summaries = []
        for task in tasks:
            summaries.append(
                {"task_id": task.id, **(await agent.tasks.cost_summary(task.id))}
            )
        return {"tasks": summaries}
    return {"task_id": target, **(await agent.tasks.cost_summary(target))}


async def save_attachment(
    rec: OpenedWorkspace,
    *,
    filename: str,
    data: bytes,
) -> str:
    """Write an uploaded file into the store and return its absolute path."""
    safe = Path(filename).name or "upload.bin"
    dest_dir = rec.paths.project_dir / "web-uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe
    if dest.exists():
        dest = dest_dir / f"{dest.stem}-{len(data)}{dest.suffix}"
    dest.write_bytes(data)
    return str(dest.resolve())
