"""Durable persistence and recovery lifecycle for workflow runs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from omni.agent.plan_revision import queued_workflow_authority
from omni.runtime import subtask_recovery
from omni.runtime.cancel_persist import complete_despite_cancel, persist_best_effort
from omni.runtime.notifications import Notifier, TaskNotification
from omni.runtime.provider_authority import workflow_provider_authorities
from omni.runtime.task_results import _artifact_uris, _result_summary
from omni.runtime.workflow_completion import write_back_workflow_result
from omni.runtime.workflow_lifecycle import cancelled_workflow_result, write_workflow_finish
from omni.runtime.workflow_plan import (
    _is_child_task_step,
    _is_native_workflow_step,
    prepare_workflow_plan,
)
from omni.runtime.workflow_progress import emit, relayed_fields, traceable
from omni.runtime.workflow_runtime import WorkflowExecutionError, WorkflowRuntime
from omni.skills_runtime.context import SKILL_SOURCE_PARAM, ExecContext
from omni.storage.db import retry_while_busy, sqlite_busy
from omni.storage.models import (
    SubtaskORM,
    TaskORM,
    WorkflowCheckpointORM,
    WorkflowRunORM,
    WorkflowStepORM,
    _utcnow,
)

logger = logging.getLogger(__name__)

ProcessExecution = Callable[..., Awaitable[None]]
EnqueueLocal = Callable[..., Awaitable[None]]
ExternalKey = Callable[[str], Awaitable[str]]
CtxFactory = Callable[[str, str], ExecContext]


class WorkflowRunManager:
    """Persist and execute WorkflowRun -> WorkflowStep -> SkillExecution."""

    def __init__(
        self,
        *,
        db: Any,
        settings: Any,
        registry: Any,
        ctx_factory: CtxFactory,
        notifier: Notifier,
        task_recorder: Any,
        process_execution: ProcessExecution,
        enqueue_local: EnqueueLocal,
        external_key: ExternalKey,
        worker_running: Callable[[], bool],
    ) -> None:
        self._db = db
        self._settings = settings
        self._registry = registry
        self._ctx_factory = ctx_factory
        self._notifier = notifier
        self._task_recorder = task_recorder
        self._process_execution = process_execution
        self._enqueue_local = enqueue_local
        self._external_key = external_key
        self._worker_running = worker_running
        self._executor = WorkflowRuntime(
            db,
            registry,
            task_recorder=task_recorder,
            step_executor=self._execute_skill_step,
        )

    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def set_task_recorder(self, recorder: Any) -> None:
        self._task_recorder = recorder
        self._executor.set_task_recorder(recorder)

    async def enqueue(
        self,
        goal: str,
        steps: list[dict[str, Any]],
        notify_channel: str = "cli",
        *,
        session_id: str = "",
        task_id: str = "",
        parent_event_id: str = "",
        task_contract: dict[str, Any] | None = None,
        workflow_dag: dict[str, Any] | None = None,
        execution_authority: dict[str, Any] | None = None,
    ) -> str:
        normalised = prepare_workflow_plan(str(goal or ""), steps, self._registry)
        provider_authorities = workflow_provider_authorities(
            self._registry,
            normalised,
            execution_authority,
        )
        task_id = await self._ensure_owner(
            task_id=task_id,
            session_id=session_id,
            channel=notify_channel,
            goal=goal,
        )
        run = WorkflowRunORM(
            task_id=task_id,
            session_id=session_id,
            project=self._settings.paths.project_name,
            status="pending",
            goal=str(goal or ""),
            plan_json={"steps": normalised},
            execution_authority_json=(
                dict(execution_authority)
                if isinstance(execution_authority, dict)
                and execution_authority
                else queued_workflow_authority(
                    list(provider_authorities.values())
                )
            ),
            task_contract_json=task_contract or {},
            workflow_dag_json=workflow_dag or {},
            notify_channel=notify_channel,
        )
        async with self._db.session() as session:
            session.add(run)
            await session.flush()
            step_rows: list[WorkflowStepORM] = []
            for position, step in enumerate(normalised, start=1):
                row_input = dict(step.get("input") or {})
                if step.get("skill_source"):
                    row_input[SKILL_SOURCE_PARAM] = str(step["skill_source"])
                row = WorkflowStepORM(
                    workflow_run_id=run.id,
                    task_id=task_id,
                    step_key=str(step["id"]),
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
                    input_json=row_input,
                    provider_authority_json=dict(
                        provider_authorities.get(str(step.get("id") or "")) or {}
                    ),
                )
                session.add(row)
                step_rows.append(row)
            await session.flush()
            executions = await self._create_initial_executions(
                session,
                run=run,
                steps=normalised,
                step_rows=step_rows,
                parent_event_id=parent_event_id,
            )
            await session.commit()
            await session.refresh(run)
        await self._record_submission(run, normalised, executions)
        if self._worker_running():
            await self._enqueue_local(run.id, kind="workflow")
        return run.id

    async def _ensure_owner(
        self,
        *,
        task_id: str,
        session_id: str,
        channel: str,
        goal: str,
    ) -> str:
        if task_id:
            return task_id
        if self._task_recorder is not None:
            owner = await self._task_recorder.create_task(
                session_id=session_id,
                channel=channel or "cli",
                user_input=str(goal or "Run workflow"),
                title=str(goal or "Workflow")[:80],
                kind="maintenance",
            )
            return owner.id
        owner = TaskORM(
            session_id=session_id,
            project=self._settings.paths.project_name,
            channel=channel or "cli",
            kind="maintenance",
            status="running",
            title=str(goal or "Workflow")[:80],
            user_input=str(goal or "Run workflow"),
            current_stage="workflow.submitted",
        )
        async with self._db.session() as session:
            session.add(owner)
            await session.commit()
            await session.refresh(owner)
        return owner.id

    async def _create_initial_executions(
        self,
        session: Any,
        *,
        run: WorkflowRunORM,
        steps: list[dict[str, Any]],
        step_rows: list[WorkflowStepORM],
        parent_event_id: str,
    ) -> list[SubtaskORM]:
        executions: list[SubtaskORM] = []
        for step, row in zip(steps, step_rows, strict=True):
            if _is_native_workflow_step(step) or _is_child_task_step(step):
                continue
            execution = SubtaskORM(
                session_id=run.session_id,
                task_id=run.task_id,
                workflow_run_id=run.id,
                workflow_step_id=row.id,
                parent_event_id=parent_event_id,
                project=self._settings.paths.project_name,
                skill_name=str(step.get("skill_name") or ""),
                status="scheduled",
                input_json=dict(row.input_json or {}),
                provider_authority_json=dict(
                    row.provider_authority_json or {}
                ),
                notify_channel="",
                step_attempt=1,
            )
            session.add(execution)
            await session.flush()
            row.current_execution_id = execution.id
            row.execution_ids = [execution.id]
            executions.append(execution)
        return executions

    async def _record_submission(
        self,
        run: WorkflowRunORM,
        steps: list[dict[str, Any]],
        executions: list[SubtaskORM],
    ) -> None:
        if self._task_recorder is None:
            return
        await self._task_recorder.record_workflow_submitted(
            run.task_id,
            workflow_run_id=run.id,
            goal=run.goal,
            steps=steps,
        )
        for execution in executions:
            await self._task_recorder.record_subtask_submitted(
                run.task_id,
                subtask_id=execution.id,
                skill_name=execution.skill_name,
                input_json=execution.input_json,
                mode="workflow_step",
                workflow_run_id=run.id,
                workflow_step_id=execution.workflow_step_id or "",
            )

    async def pending_ids(self, *, limit: int = 100) -> list[str]:
        async with self._db.session() as session:
            rows = await session.execute(
                select(WorkflowRunORM.id)
                .where(WorkflowRunORM.status.in_(("pending", "recovering")))
                .order_by(WorkflowRunORM.created_at)
                .limit(limit)
            )
            return [str(value) for value in rows.scalars().all()]

    async def recover_running(self) -> int:
        async with self._db.session() as session:
            result = await session.execute(
                update(WorkflowRunORM)
                .where(WorkflowRunORM.status == "running")
                .values(status="recovering")
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def process(
        self,
        workflow_run_id: str,
        *,
        on_event: Any = None,
        ctx_override: ExecContext | None = None,
    ) -> None:
        claimed = await self._claim(workflow_run_id)
        if claimed is None:
            return
        input_data, session_id, notify_channel, task_id = claimed
        trace: list[dict[str, Any]] = []

        async def progress(stage: str, pct: float = 0.0, **data: Any) -> None:
            execution_progress = data.pop("execution_progress", False) is True
            skill_name = str(data.get("skill") or "")
            step_key = str(data.get("step_id") or "")
            execution_id = str(data.get("execution_id") or data.get("subtask_id") or "")
            step_row_id = await self._step_row_id(workflow_run_id, step_key)
            event = {"stage": stage, "pct": pct, "ts": datetime.now(UTC).isoformat(), **data}
            trace.append(traceable(event))
            try:
                async with self._db.session() as session:
                    run = await session.get(WorkflowRunORM, workflow_run_id)
                    if run is not None:
                        run.trace_log = list(trace)
                        run.current_step_id = step_key
                        await session.commit()
            except OperationalError as exc:
                # Progress is advisory. A cancel write must be allowed to win
                # the one-writer lock instead of queueing behind this update.
                if not sqlite_busy(exc):
                    raise
                logger.debug(
                    "workflow %s progress write deferred; store busy",
                    workflow_run_id[:8],
                )
            if (
                self._task_recorder is not None
                and task_id
                and not execution_progress
                and str(data.get("status") or "") != "cancelled"
            ):
                await self._task_recorder.append_event(
                    task_id,
                    event_type="workflow.progress",
                    status="running",
                    name=stage,
                    skill_name=skill_name,
                    workflow_run_id=workflow_run_id,
                    workflow_step_id=step_row_id,
                    subtask_id=execution_id,
                    step_id=step_key,
                    output_json=event,
                    summary=stage,
                    pct=pct,
                )
            await emit(
                on_event,
                "task_progress",
                {
                    "workflow_run_id": workflow_run_id,
                    "subtask_id": execution_id,
                    "skill": skill_name,
                    **event,
                },
            )

        ctx = ctx_override or self._ctx_factory(session_id, notify_channel)
        ctx.task_id = task_id
        ctx.workflow_run_id = workflow_run_id
        ctx.subtask_id = ""
        if getattr(ctx, "registry", None) is None:
            ctx.registry = self._registry
        await emit(
            on_event,
            "task_start",
            {
                "workflow_run_id": workflow_run_id,
                "task_id": task_id,
                "kind": "workflow",
            },
        )
        if self._task_recorder is not None:
            await self._task_recorder.append_event(
                task_id,
                event_type="workflow.start",
                status="running",
                name="workflow",
                workflow_run_id=workflow_run_id,
                input_json=input_data,
                summary=f"running workflow {workflow_run_id[:8]}",
            )
        status, result, error = await complete_despite_cancel(
            lambda: self._execute(workflow_run_id, input_data, ctx, progress),
            lambda out: self._finish(
                workflow_run_id, status=out[0], result=out[1], error=out[2], trace=trace,
            ),
            ("cancelled", cancelled_workflow_result(), ""),
        )
        await write_back_workflow_result(
            db=self._db,
            session_id=session_id,
            workflow_run_id=workflow_run_id,
            status=status,
            result=result,
            task_id=task_id,
        )
        if status == "cancelled":
            await persist_best_effort(
                lambda: self._record_completion(
                    task_id, workflow_run_id, status, result, error
                )
            )
        else:
            await self._record_completion(task_id, workflow_run_id, status, result, error)
        await emit(
            on_event,
            "task_done",
            {
                "workflow_run_id": workflow_run_id,
                "task_id": task_id,
                "kind": "workflow",
                "status": status,
                "result": result,
                "error": error,
            },
        )
        if notify_channel:
            await self._notifier.notify(
                TaskNotification(
                    subtask_id="",
                    skill_name="",
                    status=status,
                    task_id=task_id,
                    object_kind="workflow_run",
                    object_id=workflow_run_id,
                    channel=notify_channel,
                    session_id=session_id,
                    external_key=await self._external_key(session_id),
                    title="Research workflow",
                    summary=_result_summary(result) or error,
                    artifacts=_artifact_uris(result),
                    payload=result,
                )
            )

    async def _execute(
        self,
        workflow_run_id: str,
        input_data: dict[str, Any],
        ctx: ExecContext,
        progress: Any,
    ) -> tuple[str, dict[str, Any], str]:
        try:
            result = await self._executor.execute(workflow_run_id, input_data, ctx, progress)
            result_status = str(result.get("status") or "succeeded").lower()
            status = (
                "degraded"
                if result_status in {"degraded", "partial", "completed_with_warnings"}
                else "cancelled" if result_status == "cancelled" else "succeeded"
            )
            return status, result, ""
        except WorkflowExecutionError as exc:
            logger.warning("workflow run %s failed with partial result: %s", workflow_run_id, exc)
            return "failed", exc.result, str(exc)
        except Exception as exc:  # noqa: BLE001
            if sqlite_busy(exc):
                logger.warning("workflow run %s persist deferred; store busy", workflow_run_id)
                return "cancelled", cancelled_workflow_result(), ""
            logger.exception("workflow run %s failed", workflow_run_id)
            result = {
                "status": "failed",
                "summary": "Workflow execution failed before completion.",
                "error": f"{type(exc).__name__}: {exc}",
                "steps": [],
            }
            return "failed", result, str(result["error"])

    async def _claim(
        self,
        workflow_run_id: str,
    ) -> tuple[dict[str, Any], str, str, str] | None:
        async with self._db.session() as session:
            result = await session.execute(
                update(WorkflowRunORM)
                .where(
                    WorkflowRunORM.id == workflow_run_id,
                    WorkflowRunORM.status.in_(("pending", "recovering")),
                )
                .values(
                    status="running",
                    started_at=_utcnow(),
                    finished_at=None,
                    attempt=WorkflowRunORM.attempt + 1,
                )
            )
            if int(result.rowcount or 0) == 0:
                await session.commit()
                return None
            run = await session.get(WorkflowRunORM, workflow_run_id)
            assert run is not None
            payload = {
                "goal": run.goal,
                "steps": list((run.plan_json or {}).get("steps") or []),
                "task_contract": dict(run.task_contract_json or {}),
                "workflow_dag": dict(run.workflow_dag_json or {}),
            }
            claimed = (payload, run.session_id, run.notify_channel, run.task_id)
            await session.commit()
            return claimed

    async def _finish(
        self,
        workflow_run_id: str,
        *,
        status: str,
        result: dict[str, Any],
        error: str,
        trace: list[dict[str, Any]],
    ) -> None:
        await retry_while_busy(
            lambda: write_workflow_finish(
                self._db,
                workflow_run_id,
                status=status,
                result=result,
                error=error,
                trace=trace,
            )
        )

    async def _record_completion(
        self,
        task_id: str,
        workflow_run_id: str,
        status: str,
        result: dict[str, Any],
        error: str,
    ) -> None:
        if self._task_recorder is None:
            return
        event_type = {
            "succeeded": "workflow.done",
            "degraded": "workflow.degraded",
            "cancelled": "workflow.cancelled",
        }.get(status, "workflow.failed")
        await self._task_recorder.append_event(
            task_id,
            event_type=event_type,
            status=status,
            name="workflow",
            workflow_run_id=workflow_run_id,
            output_json=result,
            error=error,
            summary=_result_summary(result),
            pct=1.0,
        )
        await self._task_recorder.refresh_from_executions(task_id)

    async def _execute_skill_step(
        self,
        workflow_run_id: str,
        step: dict[str, Any],
        input_data: dict[str, Any],
        ctx: ExecContext,
        progress: Any,
    ) -> dict[str, Any]:
        step_key = str(step["id"])
        async with self._db.session() as session:
            step_row = (
                await session.execute(
                    select(WorkflowStepORM).where(
                        WorkflowStepORM.workflow_run_id == workflow_run_id,
                        WorkflowStepORM.step_key == step_key,
                    )
                )
            ).scalar_one()
            execution_id = step_row.current_execution_id
            execution = await session.get(SubtaskORM, execution_id) if execution_id else None
            execution_input = dict(input_data)
            selected_source = str(
                (step_row.input_json or {}).get(SKILL_SOURCE_PARAM) or ""
            )
            if selected_source:
                execution_input[SKILL_SOURCE_PARAM] = selected_source
            if execution is None:
                execution_ids = list(step_row.execution_ids or [])
                execution = SubtaskORM(
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    workflow_run_id=workflow_run_id,
                    workflow_step_id=step_row.id,
                    project=self._settings.paths.project_name,
                    skill_name=str(step.get("skill_name") or ""),
                    status="scheduled",
                    input_json=execution_input,
                    provider_authority_json=dict(
                        step_row.provider_authority_json or {}
                    ),
                    notify_channel="",
                    step_attempt=len(execution_ids) + 1,
                )
                session.add(execution)
                await session.flush()
                execution_ids.append(execution.id)
                step_row.execution_ids = execution_ids
                step_row.current_execution_id = execution.id
                execution_id = execution.id
            else:
                execution.input_json = execution_input
                if execution.status in {"failed", "cancelled", "skipped", "interrupted"}:
                    execution.status = "recovering"
                    execution.error = ""
                    execution.finished_at = None
            step_row.status = "running"
            await session.commit()
            step_attempt = int(execution.step_attempt or 1)
            skill_name = execution.skill_name

        async def child_event(phase: str, data: dict[str, Any]) -> None:
            if phase != "task_progress":
                return
            await progress(
                str(data.get("stage") or "skill.progress"),
                float(data.get("pct") or 0.0),
                step_id=step_key,
                skill=skill_name,
                execution_id=execution_id,
                execution_progress=True,
                **relayed_fields(data),
            )

        await self._process_execution(
            execution_id,
            on_event=child_event,
            ctx_override=ctx,
            refresh_parent=False,
        )
        execution = await self.get_execution(execution_id)
        if execution is None:
            return {
                "status": "failed",
                "result": {"status": "error", "error": "skill execution disappeared"},
                "error": "skill execution disappeared",
                "execution_id": execution_id,
                "attempt": step_attempt,
            }
        result = dict(execution.result_json or {})
        return {
            "status": execution.status,
            "result": result,
            "error": execution.error or "",
            "execution_id": execution.id,
            "attempt": int(execution.step_attempt or step_attempt),
        }

    async def _step_row_id(self, workflow_run_id: str, step_key: str) -> str:
        if not workflow_run_id or not step_key:
            return ""
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(WorkflowStepORM.id).where(
                        WorkflowStepORM.workflow_run_id == workflow_run_id,
                        WorkflowStepORM.step_key == step_key,
                    )
                )
            ).scalar_one_or_none()
        return str(row or "")

    async def get_execution(self, execution_id: str) -> SubtaskORM | None:
        async with self._db.session() as session:
            return await session.get(SubtaskORM, execution_id)

    async def get(self, workflow_run_id: str) -> WorkflowRunORM | None:
        async with self._db.session() as session:
            exact = await session.get(WorkflowRunORM, workflow_run_id)
            if exact is not None:
                return exact
            rows = list(
                (
                    await session.execute(
                        select(WorkflowRunORM)
                        .order_by(WorkflowRunORM.created_at.desc())
                        .limit(500)
                    )
                ).scalars().all()
            )
        matches = [row for row in rows if row.id.startswith(workflow_run_id)]
        return matches[0] if len(matches) == 1 else None

    async def list_runs(self, *, task_id: str = "", limit: int = 100) -> list[WorkflowRunORM]:
        async with self._db.session() as session:
            query = select(WorkflowRunORM).order_by(WorkflowRunORM.created_at.asc())
            if task_id:
                query = query.where(WorkflowRunORM.task_id == task_id)
            rows = await session.execute(query.limit(limit))
            return list(rows.scalars().all())

    async def list_steps(self, workflow_run_id: str) -> list[WorkflowStepORM]:
        async with self._db.session() as session:
            rows = await session.execute(
                select(WorkflowStepORM)
                .where(WorkflowStepORM.workflow_run_id == workflow_run_id)
                .order_by(WorkflowStepORM.position.asc())
            )
            return list(rows.scalars().all())

    async def list_checkpoints(self, workflow_run_id: str) -> list[WorkflowCheckpointORM]:
        async with self._db.session() as session:
            rows = await session.execute(
                select(WorkflowCheckpointORM)
                .where(WorkflowCheckpointORM.workflow_run_id == workflow_run_id)
                .order_by(WorkflowCheckpointORM.seq, WorkflowCheckpointORM.created_at)
            )
            return list(rows.scalars().all())

    async def retry_step(
        self,
        workflow_run_id: str,
        step_id: str,
        *,
        notify_channel: str | None = None,
    ) -> str | None:
        run = await self.get(workflow_run_id)
        if run is None:
            return None
        return await subtask_recovery.retry_workflow_step(
            db=self._db,
            registry=self._registry,
            workflow_run_id=run.id,
            step_id=step_id,
            task_recorder=self._task_recorder,
            enqueue_local=self._enqueue_local,
            worker_running=self._worker_running(),
            notify_channel=notify_channel,
        )

    async def resume_step(self, workflow_run_id: str, step_id: str) -> bool:
        run = await self.get(workflow_run_id)
        if run is None:
            return False
        return await subtask_recovery.resume_workflow_step(
            db=self._db,
            registry=self._registry,
            workflow_run_id=run.id,
            step_id=step_id,
            task_recorder=self._task_recorder,
            enqueue_local=self._enqueue_local,
            worker_running=self._worker_running(),
        )

    async def get_step(self, workflow_step_id: str) -> WorkflowStepORM | None:
        async with self._db.session() as session:
            return await session.get(WorkflowStepORM, workflow_step_id)


__all__ = ["WorkflowRunManager"]
