"""Persistence operations for durable skill-execution attempts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from omni.runtime.task_results import _aware_dt, _result_has_artifacts
from omni.storage.models import ArtifactORM, SubtaskORM, _utcnow


class SkillExecutionStore:
    def __init__(self, db) -> None:  # noqa: ANN001
        self._db = db

    async def get(self, execution_id: str) -> SubtaskORM | None:
        async with self._db.session() as session:
            return await session.get(SubtaskORM, execution_id)

    async def finish_cancelled(
        self,
        execution_id: str,
        *,
        result: dict,
        trace: list[dict],
    ) -> None:
        async with self._db.session() as session:
            row = await session.get(SubtaskORM, execution_id)
            if row is None:
                return
            row.status = "cancelled"
            row.result_json = result
            row.error = ""
            row.trace_log = trace
            row.finished_at = _utcnow()
            await session.commit()

    async def list(
        self,
        *,
        limit: int = 30,
        status: str | None = None,
        include_archived: bool = False,
    ) -> list[SubtaskORM]:
        async with self._db.session() as session:
            query = select(SubtaskORM).order_by(SubtaskORM.created_at.desc()).limit(limit)
            if status:
                query = query.where(SubtaskORM.status == status)
            if not include_archived:
                query = query.where(SubtaskORM.archived_at.is_(None))
            return list((await session.execute(query)).scalars().all())

    async def archive(self, execution_id: str, *, reason: str = "") -> bool:
        async with self._db.session() as session:
            row = await session.get(SubtaskORM, execution_id)
            if row is None:
                return False
            row.archived_at = _utcnow()
            row.archived_reason = reason.strip()[:500]
            await session.commit()
            return True

    async def unarchive(self, execution_id: str) -> bool:
        async with self._db.session() as session:
            row = await session.get(SubtaskORM, execution_id)
            if row is None:
                return False
            row.archived_at = None
            row.archived_reason = ""
            await session.commit()
            return True

    async def delete(self, execution_id: str) -> bool:
        async with self._db.session() as session:
            row = await session.get(SubtaskORM, execution_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def clear(
        self,
        *,
        status: str | None = None,
        before: datetime | None = None,
        protect: tuple[str, ...] = ("running", "succeeded"),
        dry_run: bool = False,
    ) -> int:
        async with self._db.session() as session:
            rows = list((await session.execute(select(SubtaskORM))).scalars().all())
            selected = [
                row
                for row in rows
                if row.archived_at is None
                and row.status not in protect
                and (not status or row.status == status)
                and (before is None or _aware_dt(row.created_at) < before)
            ]
            if not dry_run:
                for row in selected:
                    await session.delete(row)
                await session.commit()
            return len(selected)

    async def has_artifacts(self, execution: SubtaskORM) -> bool:
        if _result_has_artifacts(execution.result_json):
            return True
        async with self._db.session() as session:
            hit = (
                await session.execute(
                    select(ArtifactORM.id)
                    .where(ArtifactORM.subtask_id == execution.id)
                    .limit(1)
                )
            ).first()
            return hit is not None


__all__ = ["SkillExecutionStore"]
