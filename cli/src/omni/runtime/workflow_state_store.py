"""Persistence boundary for stable workflow steps and checkpoints."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from omni.runtime.task_results import _collect_artifacts
from omni.runtime.workflow_state import (
    workflow_checkpoint_summary,
    workflow_step_record,
)
from omni.runtime.workflow_state import (
    workflow_state as build_workflow_state,
)
from omni.storage.db import Database
from omni.storage.models import (
    SubtaskORM,
    WorkflowCheckpointORM,
    WorkflowRunORM,
    WorkflowStepORM,
    _utcnow,
)


class WorkflowStateStore:
    """Persist logical workflow nodes separately from skill execution attempts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def ensure_steps(
        self,
        workflow_run_id: str,
        task_id: str,
        steps: list[dict[str, Any]],
    ) -> None:
        async with self._db.session() as session:
            existing = set(
                (
                    await session.execute(
                        select(WorkflowStepORM.step_key).where(
                            WorkflowStepORM.workflow_run_id == workflow_run_id
                        )
                    )
                ).scalars().all()
            )
            for position, step in enumerate(steps, start=1):
                step_key = str(step["id"])
                if step_key in existing:
                    continue
                session.add(
                    WorkflowStepORM(
                        workflow_run_id=workflow_run_id,
                        task_id=task_id,
                        step_key=step_key,
                        position=position,
                        skill_name=str(step.get("skill_name") or ""),
                        capability=str(step.get("capability") or ""),
                        provider_type=str(step.get("provider_type") or "skill"),
                        deliverable=str(step.get("deliverable") or ""),
                        required=step.get("required", True) is not False,
                        depends_on=list(step.get("depends_on") or []),
                        optional_depends_on=list(step.get("optional_depends_on") or []),
                        allow_failed_dependencies=step.get("allow_failed_dependencies") is True,
                        failure_policy=str(step.get("failure_policy") or ""),
                        input_json=dict(step.get("input") or {}),
                    )
                )
            await session.commit()

    async def load_step_records(
        self,
        workflow_run_id: str,
        steps: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        async with self._db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(WorkflowStepORM)
                        .where(WorkflowStepORM.workflow_run_id == workflow_run_id)
                        .order_by(WorkflowStepORM.position.asc())
                    )
                ).scalars().all()
            )
        by_key = {str(step["id"]): step for step in steps}
        records: list[dict[str, Any]] = []
        for row in rows:
            if row.status not in {"succeeded", "degraded", "failed", "skipped", "cancelled"}:
                continue
            source = dict(by_key.get(row.step_key) or {})
            if row.current_execution_id:
                source.update(
                    execution_id=row.current_execution_id,
                    subtask_id=row.current_execution_id,
                )
            if row.child_task_id:
                source["child_task_id"] = row.child_task_id
            records.append(
                workflow_step_record(
                    source,
                    status="skipped" if row.status == "cancelled" else row.status,
                    result=dict(row.result_json or {}),
                    error=row.error or "",
                    warning=row.warning or "",
                    skip_reason=(row.error or "") if row.status in {"skipped", "cancelled"} else "",
                    recoverable=bool(row.recoverable),
                )
            )
        return records

    async def mark_step_running(self, workflow_run_id: str, step_key: str) -> None:
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(WorkflowStepORM).where(
                        WorkflowStepORM.workflow_run_id == workflow_run_id,
                        WorkflowStepORM.step_key == step_key,
                    )
                )
            ).scalar_one()
            row.status = "running"
            row.error = ""
            row.warning = ""
            row.started_at = _utcnow()
            row.finished_at = None
            await session.commit()

    async def step_row_id(self, workflow_run_id: str, step_key: str) -> str:
        """Resolve a planner-facing step key to its stable persisted id."""
        async with self._db.session() as session:
            row_id = (
                await session.execute(
                    select(WorkflowStepORM.id).where(
                        WorkflowStepORM.workflow_run_id == workflow_run_id,
                        WorkflowStepORM.step_key == step_key,
                    )
                )
            ).scalar_one()
        return str(row_id)

    async def provider_authority(
        self,
        workflow_run_id: str,
        step_key: str,
    ) -> dict[str, Any]:
        """Load the immutable provider authority sealed for one logical step."""
        async with self._db.session() as session:
            authority = (
                await session.execute(
                    select(WorkflowStepORM.provider_authority_json).where(
                        WorkflowStepORM.workflow_run_id == workflow_run_id,
                        WorkflowStepORM.step_key == step_key,
                    )
                )
            ).scalar_one()
        return dict(authority or {})

    async def execution_authority(
        self,
        workflow_run_id: str,
    ) -> dict[str, Any]:
        """Load the immutable workflow root and its explicit renewal chain."""
        async with self._db.session() as session:
            authority = (
                await session.execute(
                    select(WorkflowRunORM.execution_authority_json).where(
                        WorkflowRunORM.id == workflow_run_id
                    )
                )
            ).scalar_one()
        return dict(authority or {})

    async def persist_step_outcome(
        self,
        workflow_run_id: str,
        record: dict[str, Any],
    ) -> None:
        step_key = str(record.get("id") or "")
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(WorkflowStepORM).where(
                        WorkflowStepORM.workflow_run_id == workflow_run_id,
                        WorkflowStepORM.step_key == step_key,
                    )
                )
            ).scalar_one()
            row.status = str(record.get("status") or "failed")
            row.result_json = dict(record.get("result") or {})
            row.error = str(record.get("error") or record.get("skip_reason") or "")
            row.warning = str(record.get("warning") or "")
            row.recoverable = record.get("recoverable") is True
            execution_id = str(record.get("execution_id") or record.get("subtask_id") or "")
            if execution_id:
                row.current_execution_id = execution_id
                execution_ids = list(row.execution_ids or [])
                if execution_id not in execution_ids:
                    execution_ids.append(execution_id)
                row.execution_ids = execution_ids
            if record.get("child_task_id"):
                child_task_id = str(record["child_task_id"])
                row.child_task_id = child_task_id
                child_task_ids = list(row.child_task_ids or [])
                if child_task_id not in child_task_ids:
                    child_task_ids.append(child_task_id)
                row.child_task_ids = child_task_ids
            row.finished_at = _utcnow()
            if row.status in {"skipped", "cancelled"} and row.current_execution_id:
                execution = await session.get(SubtaskORM, row.current_execution_id)
                if execution is not None and execution.status == "scheduled":
                    execution.status = "skipped"
                    execution.error = row.error or row.warning
                    execution.finished_at = _utcnow()
            await session.commit()

    async def skip_remaining(
        self,
        workflow_run_id: str,
        steps: list[dict[str, Any]],
        step_records: list[dict[str, Any]],
        results_by_id: dict[str, Any],
        terminal_ids: set[str],
        failed_step_ids: set[str],
        *,
        reason: str,
        warning: str,
    ) -> None:
        for step in steps:
            step_id = str(step["id"])
            if step_id in terminal_ids:
                continue
            result = {"status": "partial", "warning": warning, "recoverable": True}
            record = workflow_step_record(
                step,
                status="skipped",
                result=result,
                warning=warning,
                skip_reason=reason,
                recoverable=True,
            )
            step_records.append(record)
            results_by_id[step_id] = result
            terminal_ids.add(step_id)
            failed_step_ids.add(step_id)
            await self.persist_step_outcome(workflow_run_id, record)

    async def persist_result(
        self,
        workflow_run_id: str,
        result: dict[str, Any],
        *,
        current_step_id: str = "",
    ) -> None:
        async with self._db.session() as session:
            run = await session.get(WorkflowRunORM, workflow_run_id)
            if run is not None:
                run.result_json = result
                run.current_step_id = current_step_id
                await session.commit()

    async def persist_checkpoint(
        self,
        workflow_run_id: str,
        task_id: str,
        steps: list[dict[str, Any]],
        step_records: list[dict[str, Any]],
        *,
        status: str,
        current_step_id: str = "",
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        checkpoint = workflow_checkpoint_summary(
            steps,
            step_records,
            status=status,
            current_step_id=current_step_id,
        )
        payload = snapshot or build_workflow_state(
            workflow_run_id, "", step_records, len(steps)
        )
        payload.setdefault("checkpoint", checkpoint)
        async with self._db.session() as session:
            latest = (
                await session.execute(
                    select(WorkflowCheckpointORM)
                    .where(WorkflowCheckpointORM.workflow_run_id == workflow_run_id)
                    .order_by(
                        WorkflowCheckpointORM.seq.desc(),
                        WorkflowCheckpointORM.created_at.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            seq = int(latest.seq) + 1 if latest is not None else 1
            session.add(
                WorkflowCheckpointORM(
                    workflow_run_id=workflow_run_id,
                    task_id=task_id,
                    seq=seq,
                    status=status,
                    current_step_id=current_step_id,
                    last_completed_step_id=str(checkpoint["last_completed_step_id"] or ""),
                    completed_step_ids=list(checkpoint["completed_step_ids"]),
                    failed_step_ids=list(checkpoint["failed_step_ids"]),
                    pending_steps=list(checkpoint["pending_steps"]),
                    emitted_artifacts=_collect_artifacts(payload),
                    snapshot_json=payload,
                )
            )
            await session.commit()


__all__ = ["WorkflowStateStore"]
