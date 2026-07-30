"""Recovery transitions for standalone skill executions and workflow steps."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, update

from omni.agent.plan_revision import (
    child_agent_provider_authority_snapshot,
    create_provider_authority_renewal,
    native_provider_authority_snapshot,
    provider_authority_renewal_chain_is_valid,
    provider_authority_renewal_is_valid,
    provider_snapshot_is_valid,
    queued_workflow_authority,
    runtime_provider_authority_snapshot,
    workflow_native_authority_kind,
)
from omni.runtime.daemon import pid_alive
from omni.runtime.workflow_plan import _is_child_task_step, _is_native_workflow_step
from omni.runtime.workflow_state import step_and_descendants
from omni.skills_runtime.context import SKILL_SOURCE_PARAM
from omni.skills_runtime.registry import resolve_step_entry, step_skill_source
from omni.storage.db import Database
from omni.storage.models import SubtaskORM, WorkflowRunORM, WorkflowStepORM

Enqueue = Callable[..., Awaitable[str]]
EnqueueLocal = Callable[..., Awaitable[None]]
logger = logging.getLogger(__name__)

_RETRY_CLAIM_PREFIX = "retry_claim:"
_RETRY_ENQUEUED_PREFIX = "retry_enqueued:"
_RETRY_CLAIM_LEASE_SECONDS = 30.0


def _renew_step_provider_authority(
    registry: Any,
    step: dict[str, Any],
    row: WorkflowStepORM,
) -> dict[str, Any]:
    """Snapshot the provider explicitly re-authorized by retry/resume."""
    native_kind = workflow_native_authority_kind(step)
    if native_kind:
        snapshot = (
            child_agent_provider_authority_snapshot(registry, step)
            if native_kind == "agent_delegate"
            else native_provider_authority_snapshot(native_kind)
        )
        provider_name = native_kind
        provider_source = "omni_runtime"
    else:
        source = step_skill_source(step)
        provider_name = str(
            step.get("skill_name") or step.get("skill") or row.skill_name or ""
        )
        entry = resolve_step_entry(
            registry,
            {**step, "skill_name": provider_name},
        )
        snapshot = runtime_provider_authority_snapshot(registry, entry)
        provider_source = str(getattr(entry, "source", "") or source)
    snapshot.update(
        consumer_kind="workflow_step",
        consumer_id=row.step_key,
        provider_name=provider_name,
        provider_source=provider_source,
    )
    return snapshot


def _record_provider_authority_renewal(
    run: WorkflowRunORM,
    *,
    action: str,
    renewed: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep the original approval visible while auditing explicit re-authorization."""
    envelope = dict(run.execution_authority_json or {})
    if (
        envelope.get("schema") == "omni.queued-workflow-authority.v1"
        and not envelope.get("fingerprint")
    ):
        legacy = queued_workflow_authority(
            list(envelope.get("provider_authorities") or [])
        )
        envelope.update(legacy)
    if not provider_authority_renewal_chain_is_valid(envelope):
        raise ValueError(
            "provider authority renewal chain is invalid; "
            "re-plan or re-submit before recovery"
        )
    audit = list(envelope.get("provider_authority_renewals") or [])
    previous_fingerprint = str(
        (audit[-1].get("fingerprint") if audit else None)
        or envelope.get("fingerprint")
        or ""
    )
    renewal = create_provider_authority_renewal(
        previous_fingerprint=previous_fingerprint,
        action=action,
        renewed_at=datetime.now(UTC).isoformat(),
        provider_authorities=renewed,
    )
    audit.append(renewal)
    envelope["provider_authority_renewals"] = audit
    if not provider_authority_renewal_chain_is_valid(envelope):
        raise ValueError(
            "renewed provider authority chain is invalid; "
            "re-plan or re-submit before recovery"
        )
    run.execution_authority_json = envelope
    return renewal


def _standalone_renewed_authority(
    authority: dict[str, Any],
    *,
    prior_authority: dict[str, Any],
    action: str,
) -> dict[str, Any]:
    """Retain the immutable root and append one contiguous renewal link."""
    active = _provider_authority_without_audit(authority)
    prior = dict(prior_authority or {})
    root = prior.get("provider_authority_root")
    if not isinstance(root, dict) or not root:
        candidate = _provider_authority_without_audit(prior)
        root = (
            candidate
            if provider_snapshot_is_valid(candidate)
            else queued_workflow_authority([])
        )
    else:
        root = dict(root)
    renewals = [
        dict(item)
        for item in (prior.get("provider_authority_renewals") or [])
        if isinstance(item, dict)
    ]
    legacy = prior.get("authority_renewal")
    if (
        not renewals
        and isinstance(legacy, dict)
        and provider_authority_renewal_is_valid(legacy)
    ):
        renewals.append(dict(legacy))
    if not provider_authority_renewal_chain_is_valid(
        {**root, "provider_authority_renewals": renewals}
    ):
        raise ValueError(
            "standalone provider authority renewal chain is invalid; "
            "re-submit the task before recovery"
        )
    previous_fingerprint = str(
        (renewals[-1].get("fingerprint") if renewals else None)
        or root.get("fingerprint")
        or ""
    )
    renewal = create_provider_authority_renewal(
        previous_fingerprint=previous_fingerprint,
        action=action,
        renewed_at=datetime.now(UTC).isoformat(),
        provider_authorities=[active] if provider_snapshot_is_valid(active) else [],
    )
    renewals.append(renewal)
    return {
        **active,
        "provider_authority_root": root,
        "provider_authority_renewals": renewals,
        # Compatibility projection for callers that display only the latest
        # recovery decision. The full append-only chain is authoritative.
        "authority_renewal": renewal,
    }


def _provider_authority_without_audit(
    authority: dict[str, Any],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(authority or {}).items()
        if key
        not in {
            "authority_renewal",
            "provider_authority_root",
            "provider_authority_renewals",
        }
    }


def _retry_state_token(prefix: str, payload: dict[str, Any]) -> str:
    return prefix + json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _retry_state(
    policy: str,
    prefix: str,
) -> dict[str, Any] | None:
    if not str(policy or "").startswith(prefix):
        return None
    try:
        payload = json.loads(str(policy)[len(prefix) :])
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _retry_claim_is_recoverable(payload: dict[str, Any]) -> bool:
    try:
        owner_pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        owner_pid = 0
    try:
        claimed_at = datetime.fromisoformat(str(payload.get("claimed_at") or ""))
        age = (datetime.now(UTC) - claimed_at).total_seconds()
    except (TypeError, ValueError):
        age = _RETRY_CLAIM_LEASE_SECONDS
    return (
        owner_pid <= 0
        or not pid_alive(owner_pid)
        or age >= _RETRY_CLAIM_LEASE_SECONDS
    )


async def resolve_workflow_step(
    db: Database,
    workflow_run_id: str,
    value: str,
) -> WorkflowStepORM | None:
    """Resolve an exact or unique-prefix stable step within one workflow run."""
    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(WorkflowStepORM)
                    .where(WorkflowStepORM.workflow_run_id == workflow_run_id)
                    .order_by(WorkflowStepORM.position.asc())
                )
            ).scalars().all()
        )
    exact = [row for row in rows if value in {row.id, row.step_key, row.current_execution_id}]
    if len(exact) == 1:
        return exact[0]
    matches = [
        row
        for row in rows
        if row.id.startswith(value)
        or row.step_key.startswith(value)
        or bool(row.current_execution_id and row.current_execution_id.startswith(value))
    ]
    return matches[0] if len(matches) == 1 else None


async def retry_subtask(
    *,
    db: Database,
    original: SubtaskORM,
    enqueue: Enqueue,
    task_recorder: Any,
    notify_channel: str | None,
) -> str:
    """Create one idempotent fresh execution from an immutable input snapshot."""
    if original.workflow_run_id:
        raise ValueError("workflow skill executions must be retried through their stable step")
    terminal_statuses = {"failed", "degraded", "cancelled", "succeeded"}
    claim_token = ""
    retry_id = ""
    previous_policy = ""
    original_status = ""
    input_snapshot: dict[str, Any] = {}
    prior_authority: dict[str, Any] = {}
    original_error = ""
    recovery_attempt = 0
    skill_name = ""
    notify = ""
    session_id = ""
    parent_event_id = ""
    owner_task_id = str(original.task_id or "")
    async with db.session() as session:
        current = await session.get(SubtaskORM, original.id)
        if current is None:
            raise ValueError(f"subtask '{original.id}' no longer exists")
        owner_task_id = owner_task_id or str(current.task_id or "")
        completed = _retry_state(
            str(current.recovery_policy or ""),
            _RETRY_ENQUEUED_PREFIX,
        )
        if completed and completed.get("retry_id"):
            retry_id = str(completed["retry_id"])
            child = await session.get(SubtaskORM, retry_id)
            if child is not None:
                return retry_id
        active_claim = _retry_state(
            str(current.recovery_policy or ""),
            _RETRY_CLAIM_PREFIX,
        )
        if active_claim and active_claim.get("retry_id"):
            retry_id = str(active_claim["retry_id"])
            child = await session.get(SubtaskORM, retry_id)
            if child is not None:
                original_status = str(
                    active_claim.get("original_status") or "failed"
                )
                claim_token = str(current.recovery_policy or "")
            elif not _retry_claim_is_recoverable(active_claim):
                raise ValueError(
                    f"subtask '{original.id}' recovery is already being claimed"
                )
            else:
                previous_policy = str(
                    active_claim.get("previous_policy") or ""
                )
                original_status = str(
                    active_claim.get("original_status") or "failed"
                )
                takeover = {
                    **active_claim,
                    "pid": os.getpid(),
                    "claimed_at": datetime.now(UTC).isoformat(),
                }
                next_claim = _retry_state_token(
                    _RETRY_CLAIM_PREFIX,
                    takeover,
                )
                claimed = await session.execute(
                    update(SubtaskORM)
                    .where(
                        SubtaskORM.id == current.id,
                        SubtaskORM.status == "recovery_claimed",
                        SubtaskORM.recovery_policy
                        == str(current.recovery_policy or ""),
                    )
                    .values(recovery_policy=next_claim)
                )
                if int(claimed.rowcount or 0) != 1:
                    await session.rollback()
                    raise ValueError(
                        f"subtask '{original.id}' recovery state changed; "
                        "refresh and retry"
                    )
                claim_token = next_claim
                await session.commit()
        else:
            if current.status not in terminal_statuses:
                raise ValueError(
                    f"subtask '{original.id}' is {current.status}; "
                    "only terminal executions can be retried"
                )
            retry_id = uuid4().hex
            original_status = str(current.status)
            previous_policy = str(current.recovery_policy or "")
            claim_token = _retry_state_token(
                _RETRY_CLAIM_PREFIX,
                {
                    "retry_id": retry_id,
                    "original_status": original_status,
                    "previous_policy": previous_policy,
                    "pid": os.getpid(),
                    "claimed_at": datetime.now(UTC).isoformat(),
                },
            )
            claim = await session.execute(
                update(SubtaskORM)
                .where(
                    SubtaskORM.id == current.id,
                    SubtaskORM.status == original_status,
                    SubtaskORM.recovery_policy == previous_policy,
                )
                .values(
                    status="recovery_claimed",
                    recovery_policy=claim_token,
                )
            )
            if int(claim.rowcount or 0) != 1:
                await session.rollback()
                raise ValueError(
                    f"subtask '{original.id}' recovery state changed; refresh and retry"
                )
            await session.commit()
        input_snapshot = dict(current.input_json or {})
        prior_authority = dict(current.provider_authority_json or {})
        original_error = str(current.error or current.original_error or "")
        recovery_attempt = int(current.recovery_attempt or 0) + 1
        skill_name = str(current.skill_name or "")
        notify = str(
            current.notify_channel
            if notify_channel is None
            else notify_channel
        )
        session_id = str(current.session_id or "")
        parent_event_id = str(current.parent_event_id or "")
    child_exists = False
    async with db.session() as session:
        child_exists = await session.get(SubtaskORM, retry_id) is not None
    if not child_exists:
        try:
            created_id = await enqueue(
                skill_name,
                input_snapshot,
                notify,
                session_id=session_id,
                task_id=owner_task_id or None,
                parent_event_id=parent_event_id,
                retry_of=original.id,
                subtask_id=retry_id,
                prior_provider_authority=prior_authority,
                provider_authority_renewal_action=(
                    f"retry_subtask:{original.id}"
                ),
                original_error=original_error,
                recovery_attempt=recovery_attempt,
                recovery_policy="retry_fresh_execution",
            )
            if str(created_id) != retry_id:
                raise RuntimeError(
                    "retry enqueue did not preserve its idempotency key"
                )
        except Exception:
            async with db.session() as session:
                child_exists = (
                    await session.get(SubtaskORM, retry_id)
                ) is not None
                if not child_exists:
                    await session.execute(
                        update(SubtaskORM)
                        .where(
                            SubtaskORM.id == original.id,
                            SubtaskORM.status == "recovery_claimed",
                            SubtaskORM.recovery_policy == claim_token,
                        )
                        .values(
                            status=original_status,
                            recovery_policy=previous_policy,
                        )
                    )
                    await session.commit()
            if not child_exists:
                raise
            logger.exception(
                "retry enqueue reported an error after persisting %s; "
                "continuing with the durable execution",
                retry_id,
            )
    final_policy = _retry_state_token(
        _RETRY_ENQUEUED_PREFIX,
        {
            "retry_id": retry_id,
            "original_status": original_status,
        },
    )
    async with db.session() as session:
        new_task = await session.get(SubtaskORM, retry_id)
        if new_task is None:
            raise ValueError(
                f"retry execution '{retry_id}' was not persisted"
            )
        if not new_task.provider_authority_json.get(
            "provider_authority_root"
        ):
            new_task.provider_authority_json = _standalone_renewed_authority(
                dict(new_task.provider_authority_json or {}),
                prior_authority=prior_authority,
                action=f"retry_subtask:{original.id}",
            )
            new_task.original_error = original_error
            new_task.recovery_attempt = recovery_attempt
            new_task.recovery_policy = "retry_fresh_execution"
        finalized = await session.execute(
            update(SubtaskORM)
            .where(
                SubtaskORM.id == original.id,
                SubtaskORM.status == "recovery_claimed",
                SubtaskORM.recovery_policy == claim_token,
            )
            .values(
                status=original_status,
                recovery_policy=final_policy,
            )
        )
        if int(finalized.rowcount or 0) != 1:
            parent = await session.get(SubtaskORM, original.id)
            if not (
                parent is not None
                and str(parent.recovery_policy or "") == final_policy
            ):
                await session.rollback()
                raise ValueError(
                    f"subtask '{original.id}' recovery state changed; "
                    "refresh and retry"
                )
        await session.commit()
    if task_recorder is not None and owner_task_id:
        try:
            await task_recorder.reopen_task_for_recovery(
                owner_task_id,
                subtask_id=retry_id,
                reason=f"retry {original.id[:8]} -> {retry_id[:8]}",
            )
            await task_recorder.append_event(
                owner_task_id,
                event_type="subtask.retry",
                status="pending",
                name=skill_name,
                skill_name=skill_name,
                subtask_id=retry_id,
                input_json={
                    "retry_of": original.id,
                    "input_snapshot": input_snapshot,
                },
                summary=f"retry {original.id[:8]} -> {retry_id[:8]}",
            )
        except Exception:  # noqa: BLE001 - execution row is already durable
            logger.exception(
                "retry %s persisted but recovery audit refresh failed",
                retry_id,
            )
    return retry_id


async def retry_workflow_step(
    *,
    db: Database,
    registry: Any,
    workflow_run_id: str,
    step_id: str,
    task_recorder: Any,
    enqueue_local: EnqueueLocal,
    worker_running: bool,
    notify_channel: str | None = None,
) -> str | None:
    """Create new execution attempts while preserving stable logical step ids."""
    target = await resolve_workflow_step(db, workflow_run_id, step_id)
    if target is None:
        return None
    async with db.session() as session:
        terminal_statuses = {
            "failed",
            "degraded",
            "cancelled",
            "succeeded",
        }
        claim = await session.execute(
            update(WorkflowRunORM)
            .where(
                WorkflowRunORM.id == workflow_run_id,
                WorkflowRunORM.status.in_(terminal_statuses),
            )
            .values(status="recovery_claimed")
        )
        if int(claim.rowcount or 0) != 1:
            await session.rollback()
            return None
        run = await session.get(WorkflowRunORM, workflow_run_id)
        if run is None:
            await session.rollback()
            return None
        rows = list(
            (
                await session.execute(
                    select(WorkflowStepORM)
                    .where(WorkflowStepORM.workflow_run_id == workflow_run_id)
                    .order_by(WorkflowStepORM.position.asc())
                )
            ).scalars().all()
        )
        plan_steps = list((run.plan_json or {}).get("steps") or [])
        affected = step_and_descendants(plan_steps, target.step_key)
        new_target_id = ""
        created: list[tuple[WorkflowStepORM, SubtaskORM]] = []
        renewed_authorities: list[dict[str, Any]] = []
        for row in rows:
            if row.step_key not in affected:
                continue
            plan_step = next(
                (step for step in plan_steps if str(step.get("id") or "") == row.step_key),
                {},
            )
            renewed_authority = _renew_step_provider_authority(
                registry,
                plan_step,
                row,
            )
            row.provider_authority_json = dict(renewed_authority)
            if renewed_authority:
                renewed_authorities.append(renewed_authority)
            row.result_json = {}
            row.error = ""
            row.warning = ""
            row.recoverable = False
            row.started_at = None
            row.finished_at = None
            row.child_task_id = ""
            if _is_native_workflow_step(plan_step) or _is_child_task_step(plan_step):
                row.status = "pending"
                row.current_execution_id = ""
                continue
            previous = (
                await session.get(SubtaskORM, row.current_execution_id)
                if row.current_execution_id
                else None
            )
            execution_ids = list(row.execution_ids or [])
            execution = SubtaskORM(
                session_id=run.session_id,
                task_id=run.task_id,
                workflow_run_id=run.id,
                workflow_step_id=row.id,
                project=run.project,
                skill_name=row.skill_name,
                status="scheduled",
                input_json=dict(row.input_json or {}),
                provider_authority_json=dict(
                    renewed_authority
                ),
                notify_channel="",
                step_attempt=len(execution_ids) + 1,
                retry_of=previous.id if previous is not None else "",
                original_error=(previous.error or previous.original_error or "") if previous else "",
                recovery_attempt=int(previous.recovery_attempt or 0) + 1 if previous else 1,
                recovery_policy=f"retry_workflow_step:{target.step_key}",
            )
            session.add(execution)
            await session.flush()
            execution_ids.append(execution.id)
            row.execution_ids = execution_ids
            row.current_execution_id = execution.id
            row.status = "pending"
            created.append((row, execution))
            if row.id == target.id:
                new_target_id = execution.id
        run.status = "recovering"
        run.error = ""
        run.result_json = {}
        run.current_step_id = target.step_key
        run.finished_at = None
        if notify_channel is not None:
            run.notify_channel = notify_channel
        _record_provider_authority_renewal(
            run,
            action=f"retry_workflow_step:{target.step_key}",
            renewed=renewed_authorities,
        )
        task_id = run.task_id
        await session.commit()
    if task_recorder is not None:
        await task_recorder.reopen_task_for_recovery(
            task_id,
            subtask_id=new_target_id,
            reason=f"retry workflow step {target.step_key}",
        )
        for row, execution in created:
            await task_recorder.record_subtask_submitted(
                task_id,
                subtask_id=execution.id,
                skill_name=execution.skill_name,
                input_json=execution.input_json,
                mode="workflow_step_retry",
                workflow_run_id=workflow_run_id,
                workflow_step_id=row.id,
            )
        await task_recorder.append_event(
            task_id,
            event_type="workflow.step.retry",
            status="pending",
            name=target.step_key,
            skill_name=target.skill_name,
            workflow_run_id=workflow_run_id,
            workflow_step_id=target.id,
            subtask_id=new_target_id,
            step_id=target.step_key,
            output_json={"affected_steps": sorted(affected), "execution_id": new_target_id},
            summary=f"retry workflow step {target.step_key}",
        )
    if worker_running:
        await enqueue_local(workflow_run_id, kind="workflow")
    return new_target_id or None


async def resume_workflow_step(
    *,
    db: Database,
    registry: Any,
    workflow_run_id: str,
    step_id: str,
    task_recorder: Any,
    enqueue_local: EnqueueLocal,
    worker_running: bool,
) -> bool:
    """Resume current workflow attempts in place from a stable step boundary."""
    target = await resolve_workflow_step(db, workflow_run_id, step_id)
    if target is None:
        return False
    async with db.session() as session:
        claim = await session.execute(
            update(WorkflowRunORM)
            .where(
                WorkflowRunORM.id == workflow_run_id,
                WorkflowRunORM.status.in_(
                    {"failed", "degraded", "cancelled"}
                ),
            )
            .values(status="recovery_claimed")
        )
        if int(claim.rowcount or 0) != 1:
            await session.rollback()
            return False
        run = await session.get(WorkflowRunORM, workflow_run_id)
        if run is None:
            await session.rollback()
            return False
        rows = list(
            (
                await session.execute(
                    select(WorkflowStepORM)
                    .where(WorkflowStepORM.workflow_run_id == workflow_run_id)
                    .order_by(WorkflowStepORM.position.asc())
                )
            ).scalars().all()
        )
        plan_steps = list((run.plan_json or {}).get("steps") or [])
        affected = step_and_descendants(plan_steps, target.step_key)
        renewed_authorities: list[dict[str, Any]] = []
        for row in rows:
            if row.step_key not in affected:
                continue
            plan_step = next(
                (
                    step
                    for step in plan_steps
                    if str(step.get("id") or "") == row.step_key
                ),
                {},
            )
            renewed_authority = _renew_step_provider_authority(
                registry,
                plan_step,
                row,
            )
            row.provider_authority_json = dict(renewed_authority)
            if renewed_authority:
                renewed_authorities.append(renewed_authority)
            row.status = "pending"
            row.result_json = {}
            row.error = ""
            row.warning = ""
            row.recoverable = False
            row.started_at = None
            row.finished_at = None
            row.child_task_id = ""
            if row.current_execution_id:
                execution = await session.get(SubtaskORM, row.current_execution_id)
                if execution is not None:
                    execution.provider_authority_json = dict(renewed_authority)
                    execution.resume_of = execution.id
                    if execution.error and not execution.original_error:
                        execution.original_error = execution.error
                    execution.recovery_attempt = int(execution.recovery_attempt or 0) + 1
                    execution.recovery_policy = f"resume_workflow_step:{target.step_key}"
                    execution.status = "recovering"
                    execution.error = ""
                    execution.result_json = {}
                    execution.trace_log = []
                    execution.started_at = None
                    execution.finished_at = None
        run.status = "recovering"
        run.error = ""
        run.result_json = {}
        run.current_step_id = target.step_key
        run.finished_at = None
        _record_provider_authority_renewal(
            run,
            action=f"resume_workflow_step:{target.step_key}",
            renewed=renewed_authorities,
        )
        task_id = run.task_id
        await session.commit()
    if task_recorder is not None:
        await task_recorder.reopen_task_for_recovery(
            task_id,
            subtask_id=target.current_execution_id,
            reason=f"resume workflow step {target.step_key}",
        )
        await task_recorder.append_event(
            task_id,
            event_type="workflow.step.resume",
            status="pending",
            name=target.step_key,
            skill_name=target.skill_name,
            workflow_run_id=workflow_run_id,
            workflow_step_id=target.id,
            subtask_id=target.current_execution_id,
            step_id=target.step_key,
            output_json={"affected_steps": sorted(affected)},
            summary=f"resume workflow step {target.step_key}",
        )
    if worker_running:
        await enqueue_local(workflow_run_id, kind="workflow")
    return True


async def resume_subtask(
    *,
    db: Database,
    registry: Any,
    subtask_id: str,
    task_recorder: Any,
    enqueue_local: EnqueueLocal,
    worker_running: bool,
) -> bool:
    """Move a failed standalone skill execution back to the recovery queue."""
    async with db.session() as session:
        claim = await session.execute(
            update(SubtaskORM)
            .where(
                SubtaskORM.id == subtask_id,
                or_(
                    SubtaskORM.workflow_run_id.is_(None),
                    SubtaskORM.workflow_run_id == "",
                ),
                SubtaskORM.status.in_(
                    {"failed", "cancelled", "degraded"}
                ),
            )
            .values(status="recovery_claimed")
        )
        if int(claim.rowcount or 0) != 1:
            await session.rollback()
            return False
        task = await session.get(SubtaskORM, subtask_id)
        if task is None:
            await session.rollback()
            return False
        input_data = dict(task.input_json or {})
        source = str(input_data.get(SKILL_SOURCE_PARAM) or "")
        entry = registry.resolve_ref(task.skill_name, source)
        prior_authority = dict(task.provider_authority_json or {})
        task.provider_authority_json = _standalone_renewed_authority(
            runtime_provider_authority_snapshot(registry, entry),
            prior_authority=prior_authority,
            action=f"resume_subtask:{task.id}",
        )
        task.resume_of = task.id
        if task.error and not task.original_error:
            task.original_error = task.error
        task.recovery_attempt = int(task.recovery_attempt or 0) + 1
        task.recovery_policy = "resume_in_place"
        task.status = "recovering"
        task.error = ""
        task.finished_at = None
        task.started_at = None
        task_id = task.task_id
        skill_name = task.skill_name
        recovery_attempt = task.recovery_attempt
        await session.commit()
    if task_recorder is not None and task_id:
        await task_recorder.reopen_task_for_recovery(
            task_id,
            subtask_id=subtask_id,
            reason=f"resume {subtask_id[:8]}",
        )
        await task_recorder.append_event(
            task_id,
            event_type="subtask.resume",
            status="pending",
            name=skill_name,
            skill_name=skill_name,
            subtask_id=subtask_id,
            output_json={
                "resume_of": subtask_id,
                "recovery_attempt": recovery_attempt,
                "recovery_policy": "resume_in_place",
            },
            summary=f"resume {subtask_id[:8]}",
        )
    if worker_running:
        await enqueue_local(subtask_id, kind="subtask")
    return True


__all__ = [
    "resolve_workflow_step",
    "resume_subtask",
    "resume_workflow_step",
    "retry_subtask",
    "retry_workflow_step",
]
