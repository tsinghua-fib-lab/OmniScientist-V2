"""Durable subtask runtime (distilled from HelixForge durable_runtime).

Subtasks persist in SQLite; drained by ``omni serve`` or inline CLI, with crash
recovery for ``running`` rows and per-subtask trace/memory/notice recording.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from omni.config.settings import OmniSettings
from omni.core.tool_result import (
    attach_tool_outcome,
    owned_result_outcome,
    tool_event_output,
    tool_result_failure,
)
from omni.memory.service import MemoryService
from omni.runtime import subtask_recovery
from omni.runtime.cancel_persist import persist_best_effort
from omni.runtime.execution_ownership import ReconcileReport, reconcile_lost_executors
from omni.runtime.execution_policy import skill_requires_approval
from omni.runtime.housekeeping import run_housekeeping
from omni.runtime.notifications import InboxNotifier, Notifier
from omni.runtime.provider_authority import (
    reject_subtask_provider_authority,
    submission_provider_authority,
    workflow_subtask_authority_error,
)
from omni.runtime.skill_execution_store import SkillExecutionStore
from omni.runtime.skill_timeout import skill_exception_status, timeout_failure_result
from omni.runtime.subtask_completion import (
    complete_subtask,
    fail_subtask,
    session_external_key,
    session_principal,
    write_back_result,
)
from omni.runtime.subtask_lifecycle import settle_subtask_cancellation
from omni.runtime.subtask_retry import (
    auto_retry_budget,
    is_transient_error,
    record_auto_retry,
)
from omni.runtime.task_results import (
    _collect_artifacts,
    _result_error_message,
    _result_has_visible_output,
    _result_summary,
    _skill_execution_event,
    _task_result_message,  # noqa: F401 - compatibility re-export
    persist_detached_skill_notice,
)
from omni.runtime.tool_gateway import ToolGateway
from omni.runtime.workflow_plan import WorkflowNeedsInput, _prepare_workflow_plan
from omni.runtime.workflow_run_manager import WorkflowRunManager
from omni.runtime.workflow_runtime import WorkflowExecutionError
from omni.skills_runtime.context import SKILL_SOURCE_PARAM, ExecContext
from omni.skills_runtime.executor import SkillExecutionTimeout, execute_skill
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import Database, retry_while_busy, sqlite_busy
from omni.storage.models import (
    SubtaskORM,
    WorkflowCheckpointORM,
    WorkflowRunORM,
    WorkflowStepORM,
    _utcnow,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SubtaskRuntime", "WorkflowNeedsInput", "WorkflowExecutionError",
    "is_transient_error", "skill_exception_status",
    "_collect_artifacts", "_prepare_workflow_plan",
]

CtxFactory = Callable[[str, str], ExecContext]  # (session_id, channel) -> ExecContext

# Poller housekeeping cadence (stale-task reconcile + retention); hours-scale.
_HOUSEKEEP_INTERVAL_S = 600.0


async def _emit_event(callback: Any, phase: str, data: dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(phase, data)
    if inspect.isawaitable(result):
        await result


class SubtaskRuntime:
    def __init__(
        self,
        db: Database,
        settings: OmniSettings,
        registry: SkillRegistry,
        ctx_factory: CtxFactory,
        *,
        notifier: Notifier | None = None,
        memory: MemoryService | None = None,
        task_recorder: Any | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._registry = registry
        self._ctx_factory = ctx_factory
        self._notifier = notifier or InboxNotifier(settings.paths.project_dir / "inbox.jsonl")
        self._memory = memory
        self._task_recorder = task_recorder
        self._execution_store = SkillExecutionStore(db)
        self._workflow_manager = WorkflowRunManager(
            db=db,
            settings=settings,
            registry=registry,
            ctx_factory=ctx_factory,
            notifier=self._notifier,
            task_recorder=task_recorder,
            process_execution=self._process_subtask,
            enqueue_local=self._enqueue_local,
            external_key=self._external_key,
            worker_running=lambda: self._running,
        )
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._poller: asyncio.Task | None = None
        self._poll_interval = 0.0
        self._enqueued: set[tuple[str, str]] = set()
        self._running = False
        # Per-tick hooks (e.g. scheduler) so a long process picks up timed work.
        self._tick_hooks: list[Callable[[], Any]] = []
        self._last_housekeep = 0.0

    def add_tick_hook(self, hook: Callable[[], Any]) -> None:
        """Register an async callback invoked once per poller tick."""
        self._tick_hooks.append(hook)

    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier
        self._workflow_manager.set_notifier(notifier)

    def set_task_recorder(self, recorder: Any) -> None:
        self._task_recorder = recorder
        self._workflow_manager.set_task_recorder(recorder)

    # ── submission ──
    async def enqueue(
        self,
        skill_name: str,
        input_data: dict[str, Any],
        notify_channel: str = "cli",
        *,
        session_id: str = "",
        task_id: str = "",
        parent_event_id: str = "",
        workflow_run_id: str = "",
        workflow_step_id: str = "",
        initial_status: str = "pending",
        step_attempt: int = 1,
        retry_of: str = "",
        schedule_id: str = "",
        queue: bool = True,
        provider_authority: dict[str, Any] | None = None,
        subtask_id: str = "",
        prior_provider_authority: dict[str, Any] | None = None,
        provider_authority_renewal_action: str = "",
        original_error: str = "",
        recovery_attempt: int = 0,
        recovery_policy: str = "",
    ) -> str:
        expected_authority = (
            dict(provider_authority)
            if provider_authority is not None
            else submission_provider_authority(
                self._registry,
                skill_name,
                input_data,
            )
        )
        if provider_authority_renewal_action:
            expected_authority = (
                subtask_recovery._standalone_renewed_authority(
                    expected_authority,
                    prior_authority=dict(prior_provider_authority or {}),
                    action=provider_authority_renewal_action,
                )
            )
        task_values: dict[str, Any] = {
            "session_id": session_id,
            "task_id": task_id or None,
            "workflow_run_id": workflow_run_id or None,
            "workflow_step_id": workflow_step_id or None,
            "parent_event_id": parent_event_id,
            "schedule_id": schedule_id or "",
            "project": self._settings.paths.project_name,
            "skill_name": skill_name,
            "status": initial_status,
            "input_json": input_data,
            "provider_authority_json": expected_authority,
            "notify_channel": notify_channel,
            "step_attempt": max(1, int(step_attempt)),
            "retry_of": retry_of,
            "original_error": original_error,
            "recovery_attempt": max(0, int(recovery_attempt)),
            "recovery_policy": recovery_policy,
        }
        if subtask_id:
            task_values["id"] = subtask_id
        task = SubtaskORM(
            **task_values,
        )
        async with self._db.session() as s:
            s.add(task)
            await s.commit()
            await s.refresh(task)
        subtask_id = task.id
        if self._task_recorder is not None and task_id:
            try:
                await self._task_recorder.record_subtask_submitted(
                    task_id,
                    subtask_id=subtask_id,
                    skill_name=skill_name,
                    input_json=input_data,
                    mode="background" if notify_channel else "foreground",
                    workflow_run_id=workflow_run_id,
                    workflow_step_id=workflow_step_id,
                )
            except Exception:  # noqa: BLE001 - the execution row is durable
                logger.exception(
                    "subtask %s persisted but submission audit failed",
                    subtask_id,
                )
        if self._running and queue and initial_status in {"pending", "recovering"}:
            await self._enqueue_local(subtask_id, kind="subtask")
        return subtask_id

    async def enqueue_workflow(
        self,
        goal: str,
        steps: list[dict[str, Any]],
        notify_channel: str = "cli",
        **kwargs: Any,
    ) -> str:
        """Create a durable WorkflowRun with stable steps and skill attempts."""
        return await self._workflow_manager.enqueue(
            goal,
            steps,
            notify_channel,
            **kwargs,
        )

    async def _enqueue_local(self, item_id: str, *, kind: str = "subtask") -> None:
        """Hand a task id to this process's worker queue, de-duplicating so the
        poller can re-scan freely without flooding the queue."""
        item = (kind, item_id)
        if item in self._enqueued:
            return
        self._enqueued.add(item)
        await self._queue.put(item)

    # ── lifecycle ──
    async def start(self, workers: int = 1, *, poll_interval: float = 2.0) -> None:
        """Start workers. ``poll_interval`` > 0 also starts a DB poller so this
        process (e.g. ``omni serve``) picks up tasks enqueued by *other*
        processes/windows — set 0 to disable (single-process drain only)."""
        if self._running:
            return
        await self.recover()
        await self.housekeep()
        self._running = True
        self._poll_interval = poll_interval
        for _ in range(max(1, workers)):
            self._workers.append(asyncio.create_task(self._worker_loop()))
        if poll_interval and poll_interval > 0:
            self._poller = asyncio.create_task(self._poller_loop())
        for kind, item_id in await self._pending_items():
            await self._enqueue_local(item_id, kind=kind)
        logger.info("task runtime started: %d worker(s), poll=%.1fs", len(self._workers), poll_interval)

    async def stop(self) -> None:
        self._running = False
        tasks = [*self._workers]
        if self._poller is not None:
            tasks.append(self._poller)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._workers.clear()
        self._poller = None
        self._enqueued.clear()

    async def _poller_loop(self) -> None:
        """Re-scan the DB for pending tasks created by other processes, and run
        per-tick hooks (e.g. the scheduler firing due jobs)."""
        while self._running:
            try:
                for kind, item_id in await self._pending_items():
                    await self._enqueue_local(item_id, kind=kind)
            except Exception:  # noqa: BLE001
                logger.exception("task poller scan failed")
            for hook in self._tick_hooks:
                try:
                    res = hook()
                    if inspect.isawaitable(res):
                        await res
                except Exception:  # noqa: BLE001
                    logger.exception("task poller tick hook failed")
            if time.monotonic() - self._last_housekeep >= _HOUSEKEEP_INTERVAL_S:
                await self.housekeep()
            await asyncio.sleep(self._poll_interval)

    async def reconcile_lost_executors(
        self,
        *,
        task_id: str | None = None,
        execution_id: str | None = None,
        explicit: bool = False,
        requeue_workflow: bool | None = None,
    ) -> ReconcileReport:
        """Settle standalone executions whose foreground/background owner is gone."""
        stale_after_s = float(
            getattr(getattr(self._settings, "tasks", None), "interrupt_stale_after_s", 0.0)
            or 0.0
        )
        return await reconcile_lost_executors(
            db=self._db,
            task_recorder=self._task_recorder,
            stale_after_s=stale_after_s,
            task_id=task_id,
            execution_id=execution_id,
            explicit=explicit,
            requeue_workflow=explicit if requeue_workflow is None else requeue_workflow,
        )

    async def recover(self) -> int:
        """Take over this workspace after serve start or process replacement.

        Dead ``owner_pid`` values settle as interrupted and workflows requeue
        so WeChat/schedules continue on the new process. ``explicit=False``
        keeps the stale lease for unstamped (``owner_pid=0``) rows — serve
        restart must not treat a just-claimed skill as lost. User retry/requeue
        still pass ``explicit=True``.
        """
        report = await self.reconcile_lost_executors(
            explicit=False, requeue_workflow=True
        )
        n = (
            len(report.settled_ids)
            + len(report.requeued_workflow_ids)
            + await self._workflow_manager.recover_running()
        )
        if n:
            logger.info(
                "recovered %d orphaned execution(s) (standalone=%d workflow=%d)",
                n,
                len(report.settled_ids),
                len(report.requeued_workflow_ids),
            )
        return n

    async def housekeep(self) -> dict[str, int]:
        """Settle dead tasks + lost executors + retention."""
        self._last_housekeep = time.monotonic()
        report = await self.reconcile_lost_executors(
            explicit=False, requeue_workflow=True
        )
        hygiene = await run_housekeeping(self._task_recorder, self._settings)
        hygiene["orphans"] = len(report.settled_ids) + len(report.requeued_workflow_ids)
        return hygiene

    async def _worker_loop(self) -> None:
        while self._running:
            try:
                kind, item_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                await self._process_item(kind, item_id, on_event=None)
            except Exception:  # noqa: BLE001
                logger.exception("worker failed %s %s", kind, item_id)
            finally:
                self._enqueued.discard((kind, item_id))
                self._queue.task_done()

    # ── draining (one-shot CLI) ──
    async def drain(self, *, max_tasks: int = 50, on_event: Any = None) -> list[str]:
        await self.housekeep()
        processed: list[str] = []
        for _ in range(max_tasks):
            pending = await self._pending_items(limit=1)
            if not pending:
                break
            kind, item_id = pending[0]
            await self._process_item(kind, item_id, on_event=on_event)
            processed.append(item_id)
        return processed

    async def _pending_items(self, limit: int = 100) -> list[tuple[str, str]]:
        async with self._db.session() as s:
            subtasks = (await s.execute(
                select(SubtaskORM.id).where(
                    SubtaskORM.status.in_(("pending", "recovering")),
                    SubtaskORM.workflow_run_id.is_(None),
                )
                .order_by(SubtaskORM.created_at).limit(limit)
            )).scalars().all()
        workflows = await self._workflow_manager.pending_ids(limit=limit)
        # Workflows precede standalone executions (they own scheduled children).
        return ([('workflow', item) for item in workflows] + [
            ('subtask', item) for item in subtasks
        ])[:limit]

    async def process(
        self, item_id: str, *, on_event: Any = None, ctx_override: ExecContext | None = None
    ) -> None:
        """Process a workflow run or a standalone skill execution by id."""
        async with self._db.session() as session:
            workflow = await session.get(WorkflowRunORM, item_id)
        kind = "workflow" if workflow is not None else "subtask"
        await self._process_item(kind, item_id, on_event=on_event, ctx_override=ctx_override)

    async def _process_item(
        self, kind: str, item_id: str, *, on_event: Any, ctx_override: ExecContext | None = None
    ) -> None:
        if kind == "workflow":
            await self._workflow_manager.process(
                item_id, on_event=on_event, ctx_override=ctx_override
            )
            return
        await self._process_subtask(item_id, on_event=on_event, ctx_override=ctx_override)

    # ── execution ──
    async def _claim(
        self, subtask_id: str
    ) -> tuple[str, dict, str, str, str, str, str, dict[str, Any]] | None:
        """Atomically move a task pending/recovering → running.

        Uses a conditional ``UPDATE ... WHERE status IN (...)`` so two processes
        (e.g. an ``omni serve`` daemon and a REPL inline drain) can never both
        run the same task: SQLite serializes the writes and only the first
        UPDATE matches. Returns the task fields, or ``None`` if already claimed.
        """
        async with self._db.session() as s:
            res = await s.execute(
                update(SubtaskORM)
                .where(
                    SubtaskORM.id == subtask_id,
                    SubtaskORM.status.in_(("scheduled", "pending", "recovering")),
                )
                .values(
                    status="running",
                    started_at=_utcnow(),
                    finished_at=None,
                    owner_pid=os.getpid(),
                    attempt=SubtaskORM.attempt + 1,
                )
            )
            if (res.rowcount or 0) == 0:
                await s.commit()
                return None
            task = (await s.execute(
                select(SubtaskORM).where(SubtaskORM.id == subtask_id)
            )).scalar_one()
            payload = (
                task.skill_name,
                dict(task.input_json or {}),
                task.session_id,
                task.notify_channel,
                task.task_id or "",
                task.workflow_run_id or "",
                task.workflow_step_id or "",
                dict(task.provider_authority_json or {}),
            )
            await s.commit()
            return payload

    async def _process_subtask(
        self,
        subtask_id: str,
        *,
        on_event: Any = None,
        ctx_override: ExecContext | None = None,
        refresh_parent: bool = True,
    ) -> None:
        claimed = await self._claim(subtask_id)
        if claimed is None:
            return  # another worker/process already owns this task
        (
            skill_name,
            input_data,
            session_id,
            notify_channel,
            task_id,
            workflow_run_id,
            workflow_step_id,
            expected_provider_authority,
        ) = claimed
        step_key = await self._step_key(workflow_step_id)
        event_link = {
            "workflow_run_id": workflow_run_id,
            "workflow_step_id": workflow_step_id,
            "subtask_id": subtask_id,
            "step_id": step_key,
        }
        trace: list[dict[str, Any]] = []

        async def progress(stage: str, pct: float = 0.0, **data: Any) -> None:
            nested_step_id = str(data.get("step_id") or "")
            event = {"stage": stage, "pct": pct, "ts": datetime.now(UTC).isoformat(), **data}
            if step_key and nested_step_id and nested_step_id != step_key:
                event["skill_step_id"] = nested_step_id
            if stage == "usage":
                await _emit_event(
                    on_event,
                    "task_progress",
                    {"subtask_id": subtask_id, "skill": skill_name, **event},
                )
                return
            trace.append(event)
            await self._persist_trace(subtask_id, trace)
            if self._task_recorder is not None and task_id:
                await self._task_recorder.append_event(
                    task_id,
                    event_type="subtask.progress",
                    status="running",
                    name=stage,
                    skill_name=skill_name,
                    **{
                        **event_link,
                        "step_id": step_key or nested_step_id,
                    },
                    output_json=event,
                    summary=stage,
                    pct=pct,
                )
            await _emit_event(
                on_event,
                "task_progress",
                {"subtask_id": subtask_id, "skill": skill_name, **event},
            )

        ctx = (
            ctx_override or self._ctx_factory(session_id, notify_channel)
        ).for_execution(
            subtask_id=subtask_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_id,
            workflow_step_key=step_key,
            provider_authority=dict(expected_provider_authority or {}),
        )
        if getattr(ctx, "registry", None) is None:
            ctx.registry = self._registry
        await _emit_event(
            on_event, "task_start",
            _skill_execution_event(task_id, subtask_id, skill_name),
        )
        if self._task_recorder is not None and task_id:
            await self._task_recorder.append_event(
                task_id,
                event_type="subtask.start",
                status="running",
                name=skill_name,
                skill_name=skill_name,
                **event_link,
                input_json=input_data,
                summary=f"running {skill_name}",
            )

        entry = self._registry.resolve_ref(
            skill_name, str(input_data.pop(SKILL_SOURCE_PARAM, "") or "")
        )
        authority_error = await workflow_subtask_authority_error(
            db=self._db, registry=self._registry, entry=entry,
            expected=expected_provider_authority,
            workflow_run_id=workflow_run_id, workflow_step_id=workflow_step_id,
        )
        if authority_error:
            rejected = await self._fail_provider_authority(
                subtask_id,
                authority_error,
                expected_provider_authority,
                trace,
                notify_channel,
            )
            if rejected:
                await _emit_event(
                    on_event,
                    "task_done",
                    _skill_execution_event(
                        task_id,
                        subtask_id,
                        skill_name,
                        status="failed",
                        error=authority_error,
                    ),
                )
                if self._task_recorder is not None and task_id:
                    await self._task_recorder.append_event(
                        task_id,
                        event_type="subtask.failed",
                        status="failed",
                        name=skill_name,
                        skill_name=skill_name,
                        **event_link,
                        error=authority_error,
                        output_json={
                            "reason": "provider_authority_mismatch",
                            "expected_provider_authority": (
                                expected_provider_authority
                            ),
                        },
                    )
                    if refresh_parent:
                        await self._task_recorder.refresh_from_executions(task_id)
            return
        if entry is None:
            await self._fail(subtask_id, f"unknown skill '{skill_name}'", trace, notify_channel)
            if self._task_recorder is not None and task_id:
                await self._task_recorder.append_event(
                    task_id,
                    event_type="subtask.failed",
                    status="failed",
                    name=skill_name,
                    skill_name=skill_name,
                    **event_link,
                    error=f"unknown skill '{skill_name}'",
                )
                if refresh_parent:
                    await self._task_recorder.refresh_from_executions(task_id)
            return

        # Auto-retry only replay-safe skills on transient failure; else single-shot.
        max_retries = auto_retry_budget(self._settings) if entry.replay_safe else 0
        retries = 0
        while True:
            try:
                gateway = ToolGateway.from_context(ctx, event_family="child_task")
                transport_result = await gateway.invoke_operation(
                    skill_name,
                    input_data,
                    invoke=lambda: execute_skill(
                        entry, input_data, ctx, progress_callback=progress
                    ),
                    sensitive=skill_requires_approval(entry),
                    contract=entry,
                    outcome_resolver=owned_result_outcome,
                )
                result = tool_event_output(transport_result)
            except asyncio.CancelledError:
                await persist_best_effort(
                    lambda: settle_subtask_cancellation(
                        store=self._execution_store, task_recorder=self._task_recorder,
                        on_event=on_event, subtask_id=subtask_id, skill_name=skill_name,
                        task_id=task_id, event_link=event_link, trace=trace,
                        refresh_parent=refresh_parent,
                    )
                )
                raise
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                # A declared budget/stall expiry is not a network blip. Do not
                # auto-retry it just because the message contains "timed out".
                if (
                    not isinstance(exc, SkillExecutionTimeout)
                    and retries < max_retries
                    and is_transient_error(err)
                ):
                    retries += 1
                    await record_auto_retry(
                        self._db, self._task_recorder, self._settings,
                        subtask_id=subtask_id, error=err, attempt=retries, max_retries=max_retries,
                        task_id=task_id, skill_name=skill_name, on_event=on_event,
                    )
                    continue
                row = await self.get_subtask(subtask_id)
                durable_status = skill_exception_status(
                    exc,
                    has_durable_output=bool(row and await self.subtask_has_artifacts(row)),
                )
                logger.exception("task %s skill %s failed", subtask_id, skill_name)
                timeout_result = timeout_failure_result(
                    status=durable_status, err=err, subtask_id=subtask_id, task_id=task_id,
                )
                await self._fail(
                    subtask_id, err, trace, notify_channel,
                    result=timeout_result, status=durable_status,
                )
                await _emit_event(
                    on_event,
                    "task_done",
                    _skill_execution_event(
                        task_id, subtask_id, skill_name,
                        status=durable_status, error=err, result=timeout_result,
                    ),
                )
                if self._task_recorder is not None and task_id:
                    await self._task_recorder.append_event(
                        task_id,
                        event_type=(
                            "subtask.degraded"
                            if durable_status == "degraded"
                            else "subtask.failed"
                        ),
                        status=durable_status,
                        name=skill_name,
                        skill_name=skill_name,
                        **event_link,
                        error=err,
                        output_json=timeout_result,
                    )
                    if refresh_parent:
                        await self._task_recorder.refresh_from_executions(task_id)
                return

            result_failure = tool_result_failure(transport_result)
            if result_failure is None:
                # Contract violations are host-generated plain mappings rather
                # than provider envelopes. Resolve the trusted skill schema at
                # this boundary so they cannot fall through as success.
                result_failure = tool_result_failure(
                    attach_tool_outcome(result, owned_result_outcome(result))
                )
            if result_failure is not None:
                failure_status, result_error = result_failure
                result_error = result_error or _result_error_message(result)
                provider_status = (
                    str(result.get("status") or "").strip().lower()
                    if isinstance(result, dict)
                    else ""
                )
                if (
                    provider_status not in {"cancelled", "blocked", "rejected", "invalid"}
                    and failure_status not in {"cancelled", "rejected"}
                    and retries < max_retries
                    and is_transient_error(result_error)
                ):
                    retries += 1
                    await record_auto_retry(
                        self._db, self._task_recorder, self._settings,
                        subtask_id=subtask_id, error=result_error, attempt=retries, max_retries=max_retries,
                        task_id=task_id, skill_name=skill_name, on_event=on_event,
                    )
                    continue
                durable_status = (
                    "cancelled"
                    if provider_status == "cancelled" or failure_status == "cancelled"
                    else "failed"
                )
                await self._fail(
                    subtask_id,
                    result_error,
                    trace,
                    notify_channel,
                    result=result,
                    status=durable_status,
                )
                await _emit_event(
                    on_event,
                    "task_done",
                    _skill_execution_event(
                        task_id, subtask_id, skill_name,
                        status=durable_status, error=result_error, result=result,
                    ),
                )
                if self._task_recorder is not None and task_id:
                    await self._task_recorder.append_event(
                        task_id,
                        event_type=(
                            "subtask.cancelled"
                            if durable_status == "cancelled"
                            else "subtask.failed"
                        ),
                        status=durable_status,
                        name=skill_name,
                        skill_name=skill_name,
                        **event_link,
                        output_json=result,
                        error=result_error,
                    )
                    if refresh_parent:
                        await self._task_recorder.refresh_from_executions(task_id)
                return
            break  # success (or non-error result) → fall through to output check

        if not _result_has_visible_output(result):
            result_error = f"{skill_name} returned empty result"
            await self._fail(subtask_id, result_error, trace, notify_channel, result=result)
            await _emit_event(
                on_event,
                "task_done",
                _skill_execution_event(
                    task_id, subtask_id, skill_name,
                    status="failed", error=result_error, result=result,
                ),
            )
            if self._task_recorder is not None and task_id:
                await self._task_recorder.append_event(
                    task_id,
                    event_type="subtask.failed",
                    status="failed",
                    name=skill_name,
                    skill_name=skill_name,
                    **event_link,
                    output_json=result,
                    error=result_error,
                )
                if refresh_parent:
                    await self._task_recorder.refresh_from_executions(task_id)
            return

        result_status = str(result.get("status") or "").lower() if isinstance(result, dict) else ""
        terminal_status = (
            "degraded" if result_status in {"partial", "degraded", "warning"} else "succeeded"
        )
        await self._complete_subtask(
            subtask_id,
            result,
            trace,
            notify_channel,
            entry,
            session_id,
            status=terminal_status,
            persist_message=persist_detached_skill_notice(
                notify_channel=notify_channel,
                workflow_run_id=workflow_run_id,
            ),
        )
        await _emit_event(
            on_event,
            "task_done",
            _skill_execution_event(
                task_id, subtask_id, skill_name,
                status=terminal_status, result=result,
            ),
        )
        if self._task_recorder is not None and task_id:
            await self._task_recorder.append_event(
                task_id,
                event_type="subtask.degraded" if terminal_status == "degraded" else "subtask.done",
                status=terminal_status,
                name=skill_name,
                skill_name=skill_name,
                **event_link,
                output_json=result,
                summary=_result_summary(result),
                pct=1.0,
            )
            if refresh_parent:
                await self._task_recorder.refresh_from_executions(task_id)

    async def _persist_trace(self, subtask_id: str, trace: list[dict[str, Any]]) -> None:
        """Progress is advisory. A parallel sibling must not fail this skill."""

        async def write() -> None:
            async with self._db.session() as s:
                task = (
                    await s.execute(select(SubtaskORM).where(SubtaskORM.id == subtask_id))
                ).scalar_one_or_none()
                if task is not None:
                    task.trace_log = list(trace)
                    await s.commit()

        try:
            await retry_while_busy(write, attempts=3)
        except OperationalError as exc:
            if not sqlite_busy(exc):
                raise

    async def _persist_result(self, subtask_id: str, result: dict[str, Any]) -> None:
        async with self._db.session() as s:
            task = (await s.execute(select(SubtaskORM).where(SubtaskORM.id == subtask_id))).scalar_one_or_none()
            if task is not None:
                task.result_json = result
                await s.commit()

    async def _complete_subtask(
        self,
        subtask_id: str,
        result: Any,
        trace: list[dict[str, Any]],
        notify_channel: str,
        entry: Any,
        session_id: str,
        *,
        status: str,
        persist_message: bool,
    ) -> None:
        await complete_subtask(
            db=self._db,
            settings=self._settings,
            memory=self._memory,
            notifier=self._notifier,
            external_key=self._external_key,
            principal_for_session=self._principal_for_session,
            subtask_id=subtask_id,
            result=result,
            trace=trace,
            notify_channel=notify_channel,
            entry=entry,
            session_id=session_id,
            status=status,
            persist_message=persist_message,
        )

    async def _fail(
        self,
        subtask_id,
        error,
        trace,
        notify_channel,
        *,
        result: dict[str, Any] | None = None,
        status: str = "failed",
    ) -> None:
        await fail_subtask(
            db=self._db,
            notifier=self._notifier,
            external_key=self._external_key,
            subtask_id=subtask_id,
            error=error,
            trace=trace,
            notify_channel=notify_channel,
            result=result,
            status=status,
        )

    async def _fail_provider_authority(
        self,
        subtask_id: str,
        error: str,
        expected: dict[str, Any],
        trace: list[dict[str, Any]],
        notify_channel: str,
    ) -> bool:
        return await reject_subtask_provider_authority(
            db=self._db,
            notifier=self._notifier,
            external_key=self._external_key,
            subtask_id=subtask_id,
            error=error,
            expected=expected,
            trace=trace,
            notify_channel=notify_channel,
        )

    async def _external_key(self, session_id: str) -> str:
        return await session_external_key(self._db, session_id)

    async def _principal_for_session(self, session_id: str) -> str | None:
        return await session_principal(self._db, self._settings, session_id)

    async def _write_back_result(
        self,
        session_id: str, subtask_id: str, skill_name: str,
        summary: str, result: Any,
    ) -> None:
        await write_back_result(
            db=self._db,
            settings=self._settings,
            memory=self._memory,
            principal_for_session=self._principal_for_session,
            session_id=session_id,
            subtask_id=subtask_id,
            skill_name=skill_name,
            summary=summary,
            result=result,
        )

    @property
    def task_recorder(self) -> Any:
        """Owning-task recorder for scheduler fires (create/grant/settle), or None."""
        return self._task_recorder

    async def get_subtask(self, subtask_id: str) -> SubtaskORM | None:
        return await self._execution_store.get(subtask_id)

    async def get_workflow_run(self, workflow_run_id: str) -> WorkflowRunORM | None:
        return await self._workflow_manager.get(workflow_run_id)

    async def list_workflow_runs(self, *, task_id: str = "", limit: int = 100) -> list[WorkflowRunORM]:
        return await self._workflow_manager.list_runs(task_id=task_id, limit=limit)

    async def list_workflow_steps(self, workflow_run_id: str) -> list[WorkflowStepORM]:
        return await self._workflow_manager.list_steps(workflow_run_id)

    async def list_checkpoints(self, workflow_run_id: str) -> list[WorkflowCheckpointORM]:
        return await self._workflow_manager.list_checkpoints(workflow_run_id)

    async def list_subtasks(
        self, *, limit: int = 30, status: str | None = None, include_archived: bool = False,
    ) -> list[SubtaskORM]:
        return await self._execution_store.list(
            limit=limit, status=status, include_archived=include_archived,
        )

    async def archive_subtask(self, subtask_id: str, *, reason: str = "") -> bool:
        return await self._execution_store.archive(subtask_id, reason=reason)

    async def unarchive_subtask(self, subtask_id: str) -> bool:
        return await self._execution_store.unarchive(subtask_id)

    async def retry_subtask(self, subtask_id: str, *, notify_channel: str | None = None) -> str | None:
        """Create a fresh child task from an existing task input snapshot."""
        original = await self.get_subtask(subtask_id)
        if original is None:
            return None
        if original.status == "running":
            await self.reconcile_lost_executors(
                execution_id=original.id,
                task_id=original.task_id or None,
                explicit=True,
            )
            original = await self.get_subtask(subtask_id)
            if original is None:
                return None
        if original.workflow_run_id and original.workflow_step_id:
            step = await self._workflow_step(original.workflow_step_id)
            if step is None:
                return None
            return await self.retry_workflow_step(
                original.workflow_run_id, step.step_key, notify_channel=notify_channel,
            )
        return await subtask_recovery.retry_subtask(
            db=self._db, original=original, enqueue=self.enqueue,
            task_recorder=self._task_recorder, notify_channel=notify_channel,
        )

    async def retry_workflow_step(
        self, workflow_run_id: str, step_id: str, *, notify_channel: str | None = None,
    ) -> str | None:
        return await self._workflow_manager.retry_step(
            workflow_run_id, step_id, notify_channel=notify_channel,
        )

    async def resume_workflow_step(self, workflow_run_id: str, step_id: str) -> bool:
        return await self._workflow_manager.resume_step(workflow_run_id, step_id)

    async def requeue_subtask(self, subtask_id: str) -> bool:
        """Put a failed standalone skill execution back on the recovery queue."""
        original = await self.get_subtask(subtask_id)
        if original is not None and original.status == "running":
            await self.reconcile_lost_executors(
                execution_id=original.id,
                task_id=original.task_id or None,
                explicit=True,
            )
            original = await self.get_subtask(subtask_id)
        if original is not None and original.workflow_run_id and original.workflow_step_id:
            return False
        return await subtask_recovery.requeue_subtask(
            db=self._db, registry=self._registry, subtask_id=subtask_id,
            task_recorder=self._task_recorder, enqueue_local=self._enqueue_local,
            worker_running=self._running,
        )

    async def resume_subtask(self, subtask_id: str) -> bool:
        """Deprecated: workflow steps resume in place; standalone executions requeue."""
        original = await self.get_subtask(subtask_id)
        if original is not None and original.workflow_run_id and original.workflow_step_id:
            step = await self._workflow_step(original.workflow_step_id)
            return bool(
                step and await self.resume_workflow_step(original.workflow_run_id, step.step_key)
            )
        return await self.requeue_subtask(subtask_id)

    async def _workflow_step(self, workflow_step_id: str) -> WorkflowStepORM | None:
        return await self._workflow_manager.get_step(workflow_step_id)

    async def _step_key(self, workflow_step_id: str) -> str:
        step = await self._workflow_step(workflow_step_id) if workflow_step_id else None
        return step.step_key if step is not None else ""

    async def delete_subtask(self, subtask_id: str) -> bool:
        return await self._execution_store.delete(subtask_id)

    async def clear_subtasks(
        self, *, status: str | None = None, before: datetime | None = None,
        protect: tuple[str, ...] = ("running", "succeeded"), dry_run: bool = False,
    ) -> int:
        return await self._execution_store.clear(
            status=status, before=before, protect=protect, dry_run=dry_run,
        )

    async def subtask_has_artifacts(self, task: SubtaskORM) -> bool:
        return await self._execution_store.has_artifacts(task)
