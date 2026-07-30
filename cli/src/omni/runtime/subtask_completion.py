"""Durable completion, notification, and transcript write-back for subtasks."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from omni.memory.service import MemoryLayer, MemoryService, principal_of
from omni.runtime.notifications import Notifier, TaskNotification
from omni.runtime.session_focus import SessionFocusService
from omni.runtime.task_results import (
    _artifact_uris,
    _collect_artifacts,
    _notify_title,
    _result_summary,
    _task_result_message,
)
from omni.storage.db import Database
from omni.storage.models import (
    ArtifactORM,
    ConversationMessageORM,
    SessionORM,
    SubtaskORM,
    _utcnow,
)

logger = logging.getLogger(__name__)
ExternalKey = Callable[[str], Awaitable[str]]
PrincipalForSession = Callable[[str], Awaitable[str]]


async def session_external_key(db: Database, session_id: str) -> str:
    """Resolve one session's outbound channel key for completion notices."""
    if not session_id:
        return ""
    async with db.session() as session:
        row = await session.get(SessionORM, session_id)
    return row.external_key if row is not None else ""


async def session_principal(
    db: Database,
    settings: Any,
    session_id: str,
) -> str:
    """Resolve the memory principal that owns a background completion."""
    identity = settings.memory.channel_identity
    if not session_id:
        return principal_of("cli", "", channel_identity=identity)
    async with db.session() as session:
        row = await session.get(SessionORM, session_id)
    if row is None:
        return principal_of("cli", "", channel_identity=identity)
    return principal_of(
        row.channel,
        row.external_key,
        channel_identity=identity,
    )


async def complete_subtask(
    *,
    db: Database,
    settings: Any,
    memory: MemoryService | None,
    notifier: Notifier,
    external_key: ExternalKey,
    principal_for_session: PrincipalForSession,
    subtask_id: str,
    result: Any,
    trace: list[dict[str, Any]],
    notify_channel: str,
    entry: Any,
    session_id: str,
    status: str,
    persist_message: bool,
) -> None:
    """Settle a successful/degraded execution and publish its durable result."""
    async with db.session() as session:
        task = (
            await session.execute(
                select(SubtaskORM).where(SubtaskORM.id == subtask_id)
            )
        ).scalar_one()
        task.status = status
        task.result_json = (
            result if isinstance(result, dict) else {"result": result}
        )
        task.trace_log = trace
        task.finished_at = _utcnow()
        task.owner_pid = 0
        await session.commit()
    summary = _result_summary(result)
    if memory is not None:
        try:
            principal = await principal_for_session(session_id)
            await memory.record(
                layer=MemoryLayer.TASK,
                scope="task",
                scope_id=subtask_id,
                summary=f"[{entry.name}] {summary}",
                memory_type="task_result",
                importance=0.6,
                principal=principal,
            )
        except Exception:  # noqa: BLE001
            pass
    await write_back_result(
        db=db,
        settings=settings,
        memory=memory,
        principal_for_session=principal_for_session,
        session_id=session_id,
        subtask_id=subtask_id,
        skill_name=entry.name,
        summary=summary,
        result=result,
        task_id=task.task_id,
        workflow_run_id=task.workflow_run_id or "",
        workflow_step_id=task.workflow_step_id or "",
        persist_message=persist_message,
    )
    if notify_channel:
        await notifier.notify(
            TaskNotification(
                subtask_id=subtask_id,
                skill_name=entry.name,
                status=status,
                channel=notify_channel,
                task_id=str(task.task_id or ""),
                session_id=session_id,
                external_key=await external_key(session_id),
                title=_notify_title(entry, result),
                summary=summary,
                artifacts=_artifact_uris(result),
                payload=result if isinstance(result, dict) else {},
            )
        )


async def write_back_result(
    *,
    db: Database,
    settings: Any,
    memory: MemoryService | None,
    principal_for_session: PrincipalForSession,
    session_id: str,
    subtask_id: str,
    skill_name: str,
    summary: str,
    result: Any,
    task_id: str = "",
    workflow_run_id: str = "",
    workflow_step_id: str = "",
    persist_message: bool = True,
) -> None:
    """Attribute artifacts and write a compact result into the owning session."""
    if not session_id:
        return
    artifacts = _collect_artifacts(result)
    uris = [item["uri"] for item in artifacts if item.get("uri")]
    artifact_ids = [
        uri[len("artifact://") :]
        for uri in uris
        if uri.startswith("artifact://")
    ]
    if artifact_ids:
        try:
            async with db.session() as session:
                rows = (
                    await session.execute(
                        select(ArtifactORM).where(
                            ArtifactORM.id.in_(artifact_ids)
                        )
                    )
                ).scalars().all()
                for row in rows:
                    if not row.session_id:
                        row.session_id = session_id
                    if not row.subtask_id:
                        row.subtask_id = subtask_id
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.debug("artifact attribution backfill failed", exc_info=True)
    if persist_message:
        content = task_result_message(
            subtask_id,
            skill_name,
            summary,
            artifacts,
            task_id=task_id,
        )
        try:
            async with db.session() as session:
                session.add(
                    ConversationMessageORM(
                        session_id=session_id,
                        role="assistant",
                        content=content,
                        content_type="task_result",
                        name=skill_name,
                        meta={
                            "kind": "task_result",
                            "task_id": task_id,
                            "object_kind": "skill_execution",
                            "object_id": subtask_id,
                            "subtask_id": subtask_id,
                            "skill": skill_name,
                            "status": "succeeded",
                            "artifacts": uris,
                        },
                    )
                )
                row = (
                    await session.execute(
                        select(SessionORM).where(
                            SessionORM.id == session_id
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.updated_at = _utcnow()
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.debug("task result write-back failed", exc_info=True)
    if memory is not None and artifacts:
        labels = ", ".join(
            item.get("label") or "artifact" for item in artifacts[:6]
        )
        try:
            principal = await principal_for_session(session_id)
            await memory.record(
                layer=MemoryLayer.ARTIFACT,
                scope="task",
                scope_id=subtask_id,
                summary=f"[{skill_name}] artifacts: {labels}",
                payload_ref=uris[0] if uris else "",
                tags=uris[:12],
                memory_type="artifact",
                importance=0.55,
                principal=principal,
            )
        except Exception:  # noqa: BLE001
            pass
    try:
        await SessionFocusService(db, settings.paths).record_skill_execution_result(
            session_id=session_id,
            skill_execution_id=subtask_id,
            skill_name=skill_name,
            result=result,
            origin="skill_execution_completed",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_id,
        )
    except Exception:  # noqa: BLE001
        logger.debug("session focus write-back failed", exc_info=True)


async def fail_subtask(
    *,
    db: Database,
    notifier: Notifier,
    external_key: ExternalKey,
    subtask_id: str,
    error: str,
    trace: list[dict[str, Any]],
    notify_channel: str,
    result: dict[str, Any] | None = None,
    status: str = "failed",
) -> None:
    """Persist one terminal failure and notify its owning channel."""
    async with db.session() as session:
        task = (
            await session.execute(
                select(SubtaskORM).where(SubtaskORM.id == subtask_id)
            )
        ).scalar_one()
        task.status = status
        task.error = error
        if result is not None:
            task.result_json = result
        task.trace_log = trace
        task.finished_at = _utcnow()
        task.owner_pid = 0
        skill_name = task.skill_name
        session_id = task.session_id
        task_id = str(task.task_id or "")
        await session.commit()
    if notify_channel:
        await notifier.notify(
            TaskNotification(
                subtask_id=subtask_id,
                skill_name=skill_name,
                status=status,
                channel=notify_channel,
                task_id=task_id,
                session_id=session_id,
                external_key=await external_key(session_id),
                summary=error,
                payload=result or {},
            )
        )


def notify_title(entry: Any, result: Any) -> str:
    """Compatibility wrapper for the shared notification-title renderer."""
    return _notify_title(entry, result)


def task_result_message(
    subtask_id: str,
    skill_name: str,
    summary: str,
    artifacts: list[dict[str, str]],
    *,
    task_id: str = "",
) -> str:
    """Compatibility wrapper for canonical Task/execution presentation."""
    return _task_result_message(
        subtask_id,
        skill_name,
        summary,
        artifacts,
        task_id=task_id,
    )


__all__ = [
    "complete_subtask",
    "fail_subtask",
    "notify_title",
    "task_result_message",
    "write_back_result",
]
