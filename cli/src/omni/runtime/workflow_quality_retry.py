"""Durable, provider-authorized quality retries for workflow skill steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from omni.runtime.deliverable_assessment import (
    collect_deliverable_assessments,
    quality_retry_decision,
)
from omni.skills_runtime.context import ExecContext
from omni.storage.models import SubtaskORM, WorkflowStepORM

ProcessExecution = Callable[..., Awaitable[None]]


def _quality_side_effect_refs(result: dict[str, Any]) -> list[str]:
    """Return durable output references that make blind replay unsafe."""

    refs: list[str] = []
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, dict):
                refs.extend(
                    str(item.get(key) or "")
                    for key in ("uri", "id", "path")
                )
            elif item:
                refs.append(str(item))
    refs.extend(
        str(value)
        for key, value in result.items()
        if key.endswith("_uri") and value
    )
    return list(dict.fromkeys(item for item in refs if item))


class WorkflowQualityRetryManager:
    """Admit and persist one bounded provider-owned quality repair attempt."""

    def __init__(
        self,
        *,
        db: Any,
        registry: Any,
        process_execution: ProcessExecution,
        task_recorder: Any,
    ) -> None:
        self._db = db
        self._registry = registry
        self._process_execution = process_execution
        self._task_recorder = task_recorder

    def set_task_recorder(self, recorder: Any) -> None:
        self._task_recorder = recorder

    async def retry(
        self,
        *,
        workflow_run_id: str,
        step: dict[str, Any],
        execution: SubtaskORM,
        input_data: dict[str, Any],
        result: dict[str, Any],
        ctx: ExecContext,
        child_event: Any,
    ) -> SubtaskORM | None:
        """Replay one provider at most once after its own quality check."""

        entry = self._registry.resolve_ref(
            str(step.get("skill_name") or ""),
            str(step.get("skill_source") or ""),
        )
        quality = (
            step.get("quality_contract")
            if isinstance(step.get("quality_contract"), dict)
            else {}
        )
        retry_cfg = (
            quality.get("retry")
            if isinstance(quality.get("retry"), dict)
            else {}
        )
        if (
            entry is None
            or quality.get("assessment_required") is not True
            or int(retry_cfg.get("max_attempts") or 0) < 1
        ):
            return None
        assessments = collect_deliverable_assessments([result])
        assessment = next(
            (
                item
                for item in assessments
                if item.provider_binding_id
                == str(step.get("provider_binding_id") or "")
                and item.contract_hash
                == str(step.get("provider_contract_hash") or "")
            ),
            None,
        )
        if assessment is None:
            return None
        feedback_field = str(retry_cfg.get("feedback_field") or "").strip()
        properties = (
            entry.input_schema.get("properties")
            if isinstance(entry.input_schema, dict)
            and isinstance(entry.input_schema.get("properties"), dict)
            else {}
        )
        # A replay without a declared feedback channel is not the promised
        # bounded quality-repair loop; do not silently rerun unchanged input.
        if not feedback_field or feedback_field not in properties:
            return None
        committed = _quality_side_effect_refs(result)
        side_effect_policy = str(
            retry_cfg.get("side_effect_policy") or ""
        ).strip()
        idempotency_field = str(
            retry_cfg.get("idempotency_key_field") or ""
        ).strip()
        # Retry safety is owned by the provider contract and enforced from the
        # original request. A key merely echoed in provider output cannot make
        # an already-committed first attempt idempotent.
        idempotency_key = str(
            input_data.get(idempotency_field)
            if idempotency_field and idempotency_field in properties
            else ""
        )
        prior_quality_retries = await self._retry_count(
            workflow_run_id,
            str(step.get("id") or ""),
        )
        decision = quality_retry_decision(
            assessment,
            provider_replay_safe=bool(entry.replay_safe),
            prior_quality_retries=prior_quality_retries,
            committed_side_effects=committed,
            idempotency_required=(
                side_effect_policy == "idempotency_key_required"
            ),
            idempotency_key=idempotency_key,
        )
        if not decision.allowed:
            return None
        retry_input = dict(input_data)
        retry_input[feedback_field] = assessment.feedback
        retry = await self._create_execution(
            workflow_run_id=workflow_run_id,
            step=step,
            previous=execution,
            input_data=retry_input,
            feedback=assessment.feedback,
        )
        if retry is None:
            return None
        if self._task_recorder is not None:
            await self._task_recorder.record_subtask_submitted(
                ctx.task_id,
                subtask_id=retry.id,
                skill_name=retry.skill_name,
                input_json=retry.input_json,
                mode="workflow_quality_retry",
                workflow_run_id=workflow_run_id,
                workflow_step_id=retry.workflow_step_id or "",
            )
            await self._task_recorder.append_event(
                ctx.task_id,
                event_type="workflow.step.quality_retry",
                status="running",
                name=str(step.get("id") or ""),
                skill_name=retry.skill_name,
                workflow_run_id=workflow_run_id,
                workflow_step_id=retry.workflow_step_id or "",
                subtask_id=retry.id,
                step_id=str(step.get("id") or ""),
                output_json={
                    "retry_of": execution.id,
                    "attempt": retry.step_attempt,
                    "feedback": assessment.feedback,
                },
                summary=(
                    f"quality retry for step {step.get('id')} "
                    f"(attempt {retry.step_attempt})"
                ),
            )
        await self._process_execution(
            retry.id,
            on_event=child_event,
            ctx_override=ctx,
            refresh_parent=False,
        )
        return await self._get_execution(retry.id)

    async def _retry_count(
        self,
        workflow_run_id: str,
        step_key: str,
    ) -> int:
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(WorkflowStepORM).where(
                        WorkflowStepORM.workflow_run_id == workflow_run_id,
                        WorkflowStepORM.step_key == step_key,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return 0
            count = 0
            for execution_id in row.execution_ids or []:
                candidate = await session.get(SubtaskORM, execution_id)
                if (
                    candidate is not None
                    and str(candidate.recovery_policy or "")
                    == "provider_quality_retry"
                ):
                    count += 1
            return count

    async def _create_execution(
        self,
        *,
        workflow_run_id: str,
        step: dict[str, Any],
        previous: SubtaskORM,
        input_data: dict[str, Any],
        feedback: str,
    ) -> SubtaskORM | None:
        step_key = str(step.get("id") or "")
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(WorkflowStepORM).where(
                        WorkflowStepORM.workflow_run_id == workflow_run_id,
                        WorkflowStepORM.step_key == step_key,
                    )
                )
            ).scalar_one_or_none()
            if (
                row is None
                or str(row.current_execution_id or "") != previous.id
            ):
                return None
            execution_ids = list(row.execution_ids or [])
            if any(
                (
                    candidate is not None
                    and str(candidate.recovery_policy or "")
                    == "provider_quality_retry"
                )
                for candidate in [
                    await session.get(SubtaskORM, execution_id)
                    for execution_id in execution_ids
                ]
            ):
                return None
            retry = SubtaskORM(
                session_id=previous.session_id,
                task_id=previous.task_id,
                workflow_run_id=workflow_run_id,
                workflow_step_id=row.id,
                project=previous.project,
                skill_name=previous.skill_name,
                status="scheduled",
                input_json=dict(input_data),
                provider_authority_json=dict(
                    previous.provider_authority_json or {}
                ),
                notify_channel="",
                step_attempt=len(execution_ids) + 1,
                retry_of=previous.id,
                original_error=feedback,
                recovery_attempt=int(previous.recovery_attempt or 0) + 1,
                recovery_policy="provider_quality_retry",
            )
            session.add(retry)
            await session.flush()
            execution_ids.append(retry.id)
            row.execution_ids = execution_ids
            row.current_execution_id = retry.id
            row.status = "running"
            await session.commit()
            await session.refresh(retry)
            return retry

    async def _get_execution(self, execution_id: str) -> SubtaskORM | None:
        async with self._db.session() as session:
            return await session.get(SubtaskORM, execution_id)


__all__ = ["WorkflowQualityRetryManager"]
