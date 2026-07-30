"""Runtime enforcement helpers for sealed provider execution authority."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import update

from omni.agent.plan_revision import (
    child_agent_provider_authority_snapshot,
    native_provider_authority_snapshot,
    provider_authority_error,
    provider_authority_for_consumer,
    provider_snapshot_authority_error,
    runtime_provider_authority_snapshot,
    workflow_native_authority_kind,
)
from omni.agent.provider_quality_binding import (
    workflow_step_assessment_identity,
)
from omni.runtime.notifications import Notifier, TaskNotification
from omni.skills_runtime.context import SKILL_SOURCE_PARAM
from omni.skills_runtime.registry import resolve_step_entry, step_skill_source
from omni.storage.db import Database
from omni.storage.models import (
    SubtaskORM,
    WorkflowRunORM,
    WorkflowStepORM,
    _utcnow,
)

ExternalKey = Callable[[str], Awaitable[str]]


def submission_provider_authority(
    registry: Any,
    skill_name: str,
    input_data: dict[str, Any],
) -> dict[str, Any]:
    """Seal the exact skill source selected for one standalone submission."""
    source = str(input_data.get(SKILL_SOURCE_PARAM, "") or "")
    entry = registry.resolve_ref(skill_name, source)
    snapshot = runtime_provider_authority_snapshot(registry, entry)
    if snapshot:
        snapshot.update(
            consumer_kind="subtask",
            consumer_id="",
            provider_name=skill_name,
            provider_source=str(getattr(entry, "source", "") or source),
        )
    return snapshot


def workflow_provider_authorities(
    registry: Any,
    steps: list[dict[str, Any]],
    execution_authority: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Resolve the persisted provider authority for every workflow node."""
    return {
        str(step.get("id") or ""): workflow_step_provider_authority(
            registry,
            step,
            execution_authority,
        )
        for step in steps
    }


def workflow_step_provider_authority(
    registry: Any,
    step: dict[str, Any],
    execution_authority: dict[str, Any] | None,
) -> dict[str, Any]:
    """Seal one skill, native executor, or child-agent workflow provider."""
    step_id = str(step.get("id") or "")
    if execution_authority is not None:
        return provider_authority_for_consumer(
            execution_authority,
            consumer_kind="workflow_step",
            consumer_id=step_id,
        )
    assessment_metadata = _workflow_assessment_metadata(step)
    native_kind = workflow_native_authority_kind(step)
    if native_kind:
        snapshot = (
            child_agent_provider_authority_snapshot(
                registry,
                step,
                native_snapshot_factory=native_provider_authority_snapshot,
            )
            if native_kind == "agent_delegate"
            else native_provider_authority_snapshot(native_kind)
        )
        if snapshot:
            snapshot.update(
                consumer_kind="workflow_step",
                consumer_id=step_id,
                provider_name=native_kind,
                provider_source="omni_runtime",
                **assessment_metadata,
            )
        return snapshot
    skill_name = str(step.get("skill_name") or "")
    source = step_skill_source(step)
    entry = resolve_step_entry(registry, step) if skill_name else None
    snapshot = runtime_provider_authority_snapshot(registry, entry)
    if snapshot:
        snapshot.update(
            consumer_kind="workflow_step",
            consumer_id=step_id,
            provider_name=skill_name,
            provider_source=str(getattr(entry, "source", "") or source),
            **assessment_metadata,
        )
    return snapshot


def _workflow_assessment_metadata(
    step: dict[str, Any],
) -> dict[str, Any]:
    quality = (
        step.get("quality_contract")
        if isinstance(step.get("quality_contract"), dict)
        else {}
    )
    if quality.get("assessment_required") is not True:
        return {}
    identity = workflow_step_assessment_identity(step)
    return {
        "assessment_identity_required": True,
        **({"assessment_identity": identity} if identity else {}),
    }


async def native_workflow_authority_error(
    state_store: Any,
    registry: Any,
    workflow_run_id: str,
    step: dict[str, Any],
) -> str:
    """Recompute a native provider and compare it with its persisted authority."""
    kind = workflow_native_authority_kind(step)
    if not kind:
        return ""
    expected = await state_store.provider_authority(
        workflow_run_id,
        str(step.get("id") or ""),
    )
    envelope = await state_store.execution_authority(workflow_run_id)
    live = (
        child_agent_provider_authority_snapshot(
            registry,
            step,
            native_snapshot_factory=native_provider_authority_snapshot,
        )
        if kind == "agent_delegate"
        else native_provider_authority_snapshot(kind)
    )
    return provider_snapshot_authority_error(
        live,
        expected,
        authority_envelope=envelope,
        consumer_kind="workflow_step",
        consumer_id=str(step.get("id") or ""),
    )


async def workflow_subtask_authority_error(
    *,
    db: Database,
    registry: Any,
    entry: Any,
    expected: dict[str, Any],
    workflow_run_id: str,
    workflow_step_id: str,
) -> str:
    """Bind a skill attempt to both its logical step row and workflow root."""
    if not workflow_run_id:
        return provider_authority_error(
            entry,
            expected,
            registry=registry,
        )
    async with db.session() as session:
        run = await session.get(WorkflowRunORM, workflow_run_id)
        step = (
            await session.get(WorkflowStepORM, workflow_step_id)
            if workflow_step_id
            else None
        )
    if run is None or step is None or step.workflow_run_id != workflow_run_id:
        return (
            "workflow provider authority context is missing; "
            "re-plan or re-submit before running"
        )
    step_authority = dict(step.provider_authority_json or {})
    if step_authority != dict(expected or {}):
        return (
            "workflow step provider authority diverged from its execution attempt; "
            "re-plan or re-submit before running"
        )
    return provider_authority_error(
        entry,
        step_authority,
        registry=registry,
        authority_envelope=dict(run.execution_authority_json or {}),
        consumer_kind="workflow_step",
        consumer_id=str(step.step_key or ""),
    )


async def reject_subtask_provider_authority(
    *,
    db: Database,
    notifier: Notifier,
    external_key: ExternalKey,
    subtask_id: str,
    error: str,
    expected: dict[str, Any],
    trace: list[dict[str, Any]],
    notify_channel: str,
) -> bool:
    """CAS a claimed execution to failed, including a raced authority rewrite."""
    async with db.session() as session:
        result = await session.execute(
            update(SubtaskORM)
            .where(
                SubtaskORM.id == subtask_id,
                SubtaskORM.status == "running",
                SubtaskORM.provider_authority_json == expected,
            )
            .values(
                status="failed",
                error=error,
                trace_log=trace,
                finished_at=_utcnow(),
            )
        )
        task = await session.get(SubtaskORM, subtask_id)
        rejected = int(result.rowcount or 0) == 1
        if not rejected and task is not None and task.status == "running":
            current_authority = dict(task.provider_authority_json or {})
            raced = await session.execute(
                update(SubtaskORM)
                .where(
                    SubtaskORM.id == subtask_id,
                    SubtaskORM.status == "running",
                    SubtaskORM.provider_authority_json == current_authority,
                )
                .values(
                    status="failed",
                    error=error,
                    trace_log=trace,
                    finished_at=_utcnow(),
                )
            )
            rejected = int(raced.rowcount or 0) == 1
        await session.commit()
    if rejected and notify_channel and task is not None:
        await notifier.notify(
            TaskNotification(
                subtask_id=subtask_id,
                skill_name=task.skill_name,
                status="failed",
                channel=notify_channel,
                task_id=str(task.task_id or ""),
                session_id=task.session_id,
                external_key=await external_key(task.session_id),
                summary=error,
                payload={"reason": "provider_authority_mismatch"},
            )
        )
    return rejected


__all__ = [
    "native_workflow_authority_error",
    "reject_subtask_provider_authority",
    "submission_provider_authority",
    "workflow_subtask_authority_error",
    "workflow_provider_authorities",
    "workflow_step_provider_authority",
]
