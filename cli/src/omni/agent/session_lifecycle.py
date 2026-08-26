"""Atomic lifecycle operations spanning Sessions, transcripts, and Task trees."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from omni.agent.conversation_store import ConversationStore
from omni.runtime.task_recorder import (
    TaskClearOutcome,
    TaskRecorder,
    _begin_task_delete_snapshot,
    _task_descendant_closure,
    stage_task_deletion,
)
from omni.storage.db import Database
from omni.storage.models import (
    ActionCheckpointORM,
    ArtifactORM,
    ComputeJobORM,
    ConversationMessageORM,
    ScheduleActionProposalORM,
    ScheduleORM,
    SessionFocusORM,
    SessionORM,
    TaskORM,
)

_ACTIVE_COMPUTE_STATUSES = frozenset({"queued", "running", "submitted", "cancel_requested"})


@dataclass(frozen=True)
class SessionDeleteBlockedDependency:
    """One live durable object that makes deleting its owning Session unsafe."""

    kind: str
    object_id: str
    status: str
    session_id: str


@dataclass(frozen=True)
class SessionBatchDeleteOutcome:
    """Structured result of one workspace-local atomic Session deletion."""

    requested_session_ids: tuple[str, ...]
    deleted_session_ids: tuple[str, ...] = ()
    deleted_task_ids: tuple[str, ...] = ()
    retained_artifact_count: int = 0
    missing_session_ids: tuple[str, ...] = ()
    ambiguous_session_ids: tuple[str, ...] = ()
    blocked_tasks: tuple[tuple[str, str], ...] = ()
    blocked_executions: tuple[tuple[str, str, str], ...] = ()
    blocked_dependencies: tuple[SessionDeleteBlockedDependency, ...] = ()
    concurrent_write: bool = False
    code: str = ""
    message: str = ""

    @property
    def deleted(self) -> bool:
        return bool(self.deleted_session_ids) and not self.code


def _unique_session_refs(session_ids: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            ref
            for session_id in session_ids
            if (ref := str(session_id).strip())
        )
    )


def _resolve_session_refs(
    rows: Sequence[SessionORM],
    refs: Sequence[str],
) -> tuple[list[SessionORM], list[str], list[str]]:
    by_id = {row.id: row for row in rows}
    resolved: list[SessionORM] = []
    seen: set[str] = set()
    missing: list[str] = []
    ambiguous: list[str] = []
    for ref in refs:
        exact = by_id.get(ref)
        matches = [exact] if exact is not None else [row for row in rows if row.id.startswith(ref)]
        if not matches:
            missing.append(ref)
            continue
        if len(matches) != 1:
            ambiguous.append(ref)
            continue
        row = matches[0]
        if row.id not in seen:
            seen.add(row.id)
            resolved.append(row)
    return resolved, missing, ambiguous


async def _workspace_dependencies(
    session: AsyncSession,
    session_ids: tuple[str, ...],
    task_ids: Sequence[str],
) -> list[SessionDeleteBlockedDependency]:
    """Return durable workspace objects that still reference a selected Session."""
    blocked: list[SessionDeleteBlockedDependency] = []
    schedules = (
        await session.execute(
            select(ScheduleORM).where(
                ScheduleORM.session_id.in_(session_ids),
            )
        )
    ).scalars().all()
    blocked.extend(
        SessionDeleteBlockedDependency(
            "schedule",
            row.id,
            "enabled" if row.enabled else "disabled",
            row.session_id,
        )
        for row in schedules
    )

    checkpoint_scope = ActionCheckpointORM.session_id.in_(session_ids)
    compute_scope = ComputeJobORM.session_id.in_(session_ids)
    if task_ids:
        checkpoint_scope = or_(checkpoint_scope, ActionCheckpointORM.task_id.in_(task_ids))
        compute_scope = or_(compute_scope, ComputeJobORM.task_id.in_(task_ids))
    checkpoints = (
        await session.execute(
            select(ActionCheckpointORM).where(
                checkpoint_scope,
                ActionCheckpointORM.state == "open",
            )
        )
    ).scalars().all()
    blocked.extend(
        SessionDeleteBlockedDependency(
            "action_checkpoint", row.id, row.state, row.session_id
        )
        for row in checkpoints
    )
    compute_jobs = (
        await session.execute(
            select(ComputeJobORM).where(
                compute_scope,
                ComputeJobORM.status.in_(_ACTIVE_COMPUTE_STATUSES),
            )
        )
    ).scalars().all()
    blocked.extend(
        SessionDeleteBlockedDependency("compute_job", row.id, row.status, row.session_id)
        for row in compute_jobs
    )
    blocked.extend(await _pending_proposal_dependencies(session, session_ids))
    return blocked


async def _pending_proposal_dependencies(
    session: AsyncSession,
    session_ids: tuple[str, ...],
) -> list[SessionDeleteBlockedDependency]:
    proposals = (
        await session.execute(
            select(ScheduleActionProposalORM).where(
                ScheduleActionProposalORM.session_id.in_(session_ids),
                ScheduleActionProposalORM.state == "pending",
            )
        )
    ).scalars().all()
    return [
        SessionDeleteBlockedDependency(
            "schedule_action_proposal", row.id, row.state, row.session_id
        )
        for row in proposals
    ]


class SessionLifecycleService:
    """Coordinate fail-closed Session deletion inside one workspace database."""

    def __init__(
        self,
        db: Database,
        *,
        tasks: TaskRecorder,
        conversations: ConversationStore,
        control_db: Database | None = None,
    ) -> None:
        self._db = db
        self._tasks = tasks
        self._conversations = conversations
        self._control_db = control_db

    async def delete_many(self, session_ids: Sequence[str]) -> SessionBatchDeleteOutcome:
        """Delete selected Sessions and all their Task trees in one transaction."""
        refs = _unique_session_refs(session_ids)
        if not refs:
            return SessionBatchDeleteOutcome(
                requested_session_ids=(),
                code="invalid_request",
                message="at least one session id is required",
            )

        # Pending schedule proposals live in the machine-global control store.
        # Hold a read-only write reservation there while the workspace mutation
        # commits so proposal creation cannot race the safety preflight.
        if self._control_db is not None and self._control_db.path != self._db.path:
            await self._control_db.init()
            async with self._control_db.session() as control_session:
                reservation = TaskClearOutcome()
                if not await _begin_task_delete_snapshot(
                    control_session,
                    reservation,
                    dry_run=False,
                ):
                    return self._concurrent_write(refs)
                outcome = await self._delete_workspace(refs, control_session=control_session)
                await control_session.rollback()
        else:
            outcome = await self._delete_workspace(refs, control_session=None)

        if outcome.deleted:
            await self._tasks.deindex_deleted_tasks(outcome.deleted_task_ids)
            self._conversations.forget_deleted_sessions(outcome.deleted_session_ids)
        return outcome

    @staticmethod
    def _concurrent_write(refs: tuple[str, ...]) -> SessionBatchDeleteOutcome:
        return SessionBatchDeleteOutcome(
            requested_session_ids=refs,
            concurrent_write=True,
            code="concurrent_write",
            message="session deletion could not reserve its lifecycle stores; retry after they settle",
        )

    async def _delete_workspace(
        self,
        refs: tuple[str, ...],
        *,
        control_session: AsyncSession | None,
    ) -> SessionBatchDeleteOutcome:
        """Run preflight and all workspace mutations in one immediate transaction."""

        async with self._db.session() as session:
            reservation = TaskClearOutcome()
            if not await _begin_task_delete_snapshot(
                session,
                reservation,
                dry_run=False,
            ):
                return self._concurrent_write(refs)

            rows = list((await session.execute(select(SessionORM))).scalars().all())
            selected, missing, ambiguous = _resolve_session_refs(rows, refs)
            if missing or ambiguous:
                await session.rollback()
                return SessionBatchDeleteOutcome(
                    requested_session_ids=refs,
                    missing_session_ids=tuple(missing),
                    ambiguous_session_ids=tuple(ambiguous),
                    code="ambiguous" if ambiguous else "not_found",
                    message=(
                        "one or more session ids are ambiguous"
                        if ambiguous
                        else "one or more sessions were not found"
                    ),
                )

            resolved_ids = tuple(row.id for row in selected)
            resolved_id_set = set(resolved_ids)
            task_rows = list(
                (await session.execute(select(TaskORM))).scalars().all()
            )
            task_ids = [
                row.id for row in task_rows if row.session_id in resolved_id_set
            ]
            descendant_closure = _task_descendant_closure(
                {row.id: row for row in task_rows},
                task_ids,
            )
            foreign_descendants = sorted(
                (
                    row
                    for row in descendant_closure
                    if row.session_id not in resolved_id_set
                ),
                key=lambda row: row.id,
            )
            if foreign_descendants:
                blocked_tasks = tuple(
                    (row.id, row.status) for row in foreign_descendants
                )
                await session.rollback()
                return SessionBatchDeleteOutcome(
                    requested_session_ids=refs,
                    blocked_tasks=blocked_tasks,
                    code="conflict",
                    message="session task tree crosses an unselected session",
                )
            artifact_scope = ArtifactORM.session_id.in_(resolved_ids)
            if task_ids:
                artifact_scope = or_(artifact_scope, ArtifactORM.task_id.in_(task_ids))
            retained_artifact_count = int(
                (
                    await session.execute(
                        select(func.count(ArtifactORM.id)).where(artifact_scope)
                    )
                ).scalar_one()
                or 0
            )
            blocked_dependencies = await _workspace_dependencies(
                session,
                resolved_ids,
                task_ids,
            )
            if control_session is not None:
                blocked_dependencies.extend(
                    await _pending_proposal_dependencies(control_session, resolved_ids)
                )
            if blocked_dependencies:
                await session.rollback()
                unique = {
                    (item.kind, item.object_id): item for item in blocked_dependencies
                }
                return SessionBatchDeleteOutcome(
                    requested_session_ids=refs,
                    blocked_dependencies=tuple(
                        sorted(unique.values(), key=lambda item: (item.kind, item.object_id))
                    ),
                    code="busy",
                    message="one or more sessions have durable dependencies",
                )
            task_outcome = await stage_task_deletion(
                session,
                task_ids,
                force=True,
            )
            blocked_executions = tuple(
                (row.object_kind, row.object_id, row.status)
                for row in task_outcome.blocked_executions
            )
            if task_outcome.blocked_tasks or blocked_executions:
                await session.rollback()
                return SessionBatchDeleteOutcome(
                    requested_session_ids=refs,
                    blocked_tasks=tuple(task_outcome.blocked_tasks.items()),
                    blocked_executions=blocked_executions,
                    code="busy",
                    message="one or more sessions have active work",
                )
            if (
                task_outcome.missing_ids
                or task_outcome.protected_tasks
                or task_outcome.retained_tasks
            ):
                await session.rollback()
                return SessionBatchDeleteOutcome(
                    requested_session_ids=refs,
                    code="conflict",
                    message="session tasks changed while deletion was being prepared",
                )

            # Flush the staged ORM Task deletes before the transcript rows. Any
            # later failure still rolls the complete transaction back.
            await session.flush()
            await session.execute(
                delete(ConversationMessageORM).where(
                    ConversationMessageORM.session_id.in_(resolved_ids)
                )
            )
            await session.execute(
                delete(SessionFocusORM).where(SessionFocusORM.session_id.in_(resolved_ids))
            )
            await session.execute(delete(SessionORM).where(SessionORM.id.in_(resolved_ids)))
            await session.commit()

        deleted_task_ids = tuple(task_outcome.deleted_ids)
        return SessionBatchDeleteOutcome(
            requested_session_ids=refs,
            deleted_session_ids=resolved_ids,
            deleted_task_ids=deleted_task_ids,
            retained_artifact_count=retained_artifact_count,
        )


__all__ = [
    "SessionBatchDeleteOutcome",
    "SessionDeleteBlockedDependency",
    "SessionLifecycleService",
]
