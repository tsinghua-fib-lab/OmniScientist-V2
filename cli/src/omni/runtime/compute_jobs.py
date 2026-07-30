"""Durable lifecycle ledger for scientific compute jobs."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from omni.storage.db import Database
from omni.storage.models import ComputeJobORM, _utcnow

_ACTIVE = {"queued", "running", "submitted", "cancel_requested"}


class ComputeJobStore:
    """Persist compute state independently from any one backend process."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        command: str,
        requested_backend: str,
        task_id: str = "",
        session_id: str = "",
        cwd: str = "",
        profile: str = "",
    ) -> ComputeJobORM:
        row = ComputeJobORM(
            task_id=task_id,
            session_id=session_id,
            requested_backend=requested_backend or "local",
            status="queued",
            command=command,
            cwd=cwd,
            profile=profile,
        )
        async with self._db.session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def mark_running(self, job_id: str) -> None:
        async with self._db.session() as session:
            row = await session.get(ComputeJobORM, job_id)
            if row is None:
                return
            row.status = "running"
            row.started_at = row.started_at or _utcnow()
            row.updated_at = _utcnow()
            await session.commit()

    async def finish(self, job_id: str, result: Any) -> ComputeJobORM | None:
        raw_status = str(getattr(result, "status", "error") or "error")
        status = {
            "ok": "succeeded",
            "error": "failed",
            "timeout": "timeout",
            "submitted": "submitted",
            "cancelled": "cancelled",
        }.get(raw_status, "failed")
        payload = result.to_dict() if hasattr(result, "to_dict") else dict(result or {})
        detail = str(payload.get("detail") or "")
        external_id = _external_job_id(detail) if status == "submitted" else ""
        async with self._db.session() as session:
            row = await session.get(ComputeJobORM, job_id)
            if row is None:
                return None
            row.backend = str(payload.get("backend") or row.requested_backend)
            row.status = status
            row.external_job_id = external_id
            row.result_json = payload
            row.error = detail if status in {"failed", "timeout"} else ""
            row.updated_at = _utcnow()
            row.finished_at = None if status == "submitted" else _utcnow()
            await session.commit()
            await session.refresh(row)
            return row

    async def get(self, job_id: str) -> ComputeJobORM | None:
        async with self._db.session() as session:
            exact = await session.get(ComputeJobORM, job_id)
            if exact is not None:
                return exact
            rows = list(
                (
                    await session.execute(
                        select(ComputeJobORM).order_by(ComputeJobORM.created_at.desc()).limit(500)
                    )
                ).scalars().all()
            )
        matches = [row for row in rows if row.id.startswith(job_id)]
        return matches[0] if len(matches) == 1 else None

    async def list(self, *, session_id: str = "", limit: int = 20) -> list[ComputeJobORM]:
        async with self._db.session() as session:
            query = select(ComputeJobORM).order_by(ComputeJobORM.created_at.desc()).limit(limit)
            if session_id:
                query = query.where(ComputeJobORM.session_id == session_id)
            return list((await session.execute(query)).scalars().all())

    async def request_cancel(self, job_id: str) -> ComputeJobORM | None:
        async with self._db.session() as session:
            row = await session.get(ComputeJobORM, job_id)
            if row is None or row.status not in _ACTIVE:
                return row
            row.status = "cancel_requested"
            row.updated_at = _utcnow()
            await session.commit()
            await session.refresh(row)
            return row


def compute_job_payload(row: ComputeJobORM) -> dict[str, Any]:
    """Return a bounded status payload without exposing a local absolute cwd."""
    return {
        "id": row.id,
        "task_id": row.task_id,
        "status": row.status,
        "requested_backend": row.requested_backend,
        "backend": row.backend,
        "command": row.command,
        "profile": row.profile,
        "external_job_id": row.external_job_id,
        "result": row.result_json or {},
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "started_at": row.started_at.isoformat() if row.started_at else "",
        "finished_at": row.finished_at.isoformat() if row.finished_at else "",
    }


def _external_job_id(detail: str) -> str:
    match = re.search(r"\bjob\s+([^\s]+)", detail, flags=re.IGNORECASE)
    return match.group(1) if match else ""


__all__ = ["ComputeJobStore", "compute_job_payload"]
