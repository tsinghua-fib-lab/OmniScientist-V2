"""Durable workflow-run execution with stable steps and skill attempts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from omni.core.execution_budget import ToolExecutionBudget
from omni.core.execution_control import ExecutionCancelled, ExecutionControl
from omni.core.turn_clock import TurnClock, register_clock
from omni.runtime.deliverable_assessment import (
    bind_deliverable_assessment_identity,
)
from omni.runtime.final_synthesis import (
    NATIVE_SYNTHESIS_INPUT_SCHEMA,
    NATIVE_SYNTHESIS_OUTPUT_SCHEMA,
    run_native_synthesis,
)
from omni.runtime.provider_authority import native_workflow_authority_error
from omni.runtime.task_results import _result_error_message
from omni.runtime.tool_gateway import ToolGateway
from omni.runtime.workflow_lifecycle import (
    WorkflowExecutionError,
    mark_workflow_cancelled,
    settle_cancelled_wave,
)
from omni.runtime.workflow_plan import (
    _is_child_task_step,
    _is_native_workflow_step,
    _step_allows_failed_dependencies,
    _workflow_step_input,
    prepare_workflow_plan,
)
from omni.runtime.workflow_state import (
    all_dependencies,
    dependencies_terminal,
    select_execution_wave,
    step_concurrent_safe,
    workflow_checkpoint_summary,
    workflow_failure_message,
    workflow_step_record,
)
from omni.runtime.workflow_state import (
    workflow_state as build_workflow_state,
)
from omni.runtime.workflow_state_store import WorkflowStateStore
from omni.runtime.workflow_step_outcomes import (
    classify_workflow_outcome,
    execute_child_task,
)
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.registry import SkillRegistry, resolve_step_entry
from omni.storage.db import Database

Progress = Callable[..., Any]
StepExecutor = Callable[
    [str, dict[str, Any], dict[str, Any], ExecContext, Progress],
    Awaitable[dict[str, Any]],
]


class WorkflowRuntime:
    """Execute a :class:`WorkflowRunORM` and persist every logical boundary."""

    def __init__(
        self,
        db: Database,
        registry: SkillRegistry,
        *,
        task_recorder: Any = None,
        step_executor: StepExecutor | None = None,
    ) -> None:
        self._state_store = WorkflowStateStore(db)
        self._registry = registry
        self._task_recorder = task_recorder
        self._step_executor = step_executor

    def set_task_recorder(self, recorder: Any) -> None:
        self._task_recorder = recorder

    def set_step_executor(self, executor: StepExecutor) -> None:
        self._step_executor = executor

    async def execute(
        self,
        workflow_run_id: str,
        input_data: dict[str, Any],
        ctx: ExecContext,
        progress: Progress,
    ) -> dict[str, Any]:
        goal = str(input_data.get("goal") or "")
        steps = prepare_workflow_plan(goal, input_data.get("steps") or [], self._registry)
        if not steps:
            raise ValueError("workflow requires at least one step")
        task_cfg = ctx.settings.tasks
        max_steps = max(1, int(task_cfg.workflow_max_steps))
        if len(steps) > max_steps:
            result = {
                "status": "failed",
                "summary": f"Workflow has {len(steps)} steps, exceeding the trusted limit of {max_steps}.",
                "error": f"workflow_max_steps exceeded: {len(steps)} > {max_steps}",
                "goal": goal,
                "steps": [],
                "skills_used": [],
                "recoverable": False,
            }
            raise WorkflowExecutionError(result["error"], result)

        await self._state_store.ensure_steps(workflow_run_id, ctx.task_id, steps)
        task_contract = (
            input_data.get("task_contract")
            if isinstance(input_data.get("task_contract"), dict)
            else {}
        )
        workflow_dag = (
            input_data.get("workflow_dag")
            if isinstance(input_data.get("workflow_dag"), dict)
            else {}
        )
        total = len(steps)
        order = {str(step["id"]): index for index, step in enumerate(steps, start=1)}
        await progress("workflow.start", 0.0, total_steps=total, goal=goal)

        step_records = await self._state_store.load_step_records(workflow_run_id, steps)
        results_by_id = {
            str(record["id"]): record.get("result")
            for record in step_records
            if record.get("id") and "result" in record
        }
        completed_step_ids = {
            str(record["id"])
            for record in step_records
            if record.get("status") in {"succeeded", "degraded"}
        }
        failed_step_ids = {
            str(record["id"])
            for record in step_records
            if record.get("status") in {"failed", "skipped", "cancelled"}
        }
        terminal_ids = completed_step_ids | failed_step_ids
        execution_control = ctx.execution_control or ExecutionControl(
            lambda: self._consume_controls(
                ctx.task_id,
                actions={"cancel"},
            ),
            acknowledge_controls=self._acknowledge_controls,
        )
        workflow_started = time.monotonic()
        workflow_seconds = max(0.01, float(task_cfg.workflow_max_seconds))
        # Pausable envelope: a step's approval wait pauses this (via the approval
        # gate) instead of counting against it, so later steps aren't skipped.
        workflow_clock = TurnClock(workflow_seconds)
        tool_budget = ToolExecutionBudget(max(0, int(task_cfg.workflow_max_tool_calls)))
        cost_cfg = ctx.settings.cost
        usage_max_tokens = max(0, int(cost_cfg.max_total_tokens)) if cost_cfg.enabled else 0
        usage_max_cost = max(0.0, float(cost_cfg.max_cost_usd)) if cost_cfg.enabled else 0.0

        restored_tool_calls = sum(
            int((record.get("result") or {}).get("total_tool_calls") or 0)
            for record in step_records
            if isinstance(record.get("result"), dict)
        )
        if restored_tool_calls:
            tool_budget.admit(restored_tool_calls)
            tool_budget.mark_completed(restored_tool_calls)

        def usage_snapshot() -> dict[str, int | float | bool]:
            total_tokens = 0
            cost_usd = 0.0
            for record in step_records:
                result = record.get("result") if isinstance(record, dict) else None
                if not isinstance(result, dict):
                    continue
                usage = result.get("usage")
                if isinstance(usage, dict):
                    total_tokens += int(usage.get("total_tokens") or 0)
                usage_budget = result.get("usage_budget")
                if isinstance(usage_budget, dict):
                    cost_usd += float(usage_budget.get("cost_usd") or 0.0)
            return {
                "max_total_tokens": usage_max_tokens,
                "max_cost_usd": usage_max_cost,
                "total_tokens": total_tokens,
                "cost_usd": round(cost_usd, 6),
                "enforced": bool(usage_max_tokens or usage_max_cost),
            }

        def usage_limit_reason() -> str:
            usage = usage_snapshot()
            if usage_max_tokens and int(usage["total_tokens"]) >= usage_max_tokens:
                return "workflow_max_total_tokens"
            if usage_max_cost and float(usage["cost_usd"]) >= usage_max_cost:
                return "workflow_max_cost"
            return ""

        def workflow_state(*, final: bool = False) -> dict[str, Any]:
            ordered = sorted(
                step_records,
                key=lambda item: order.get(str(item.get("id") or ""), total + 1),
            )
            payload = build_workflow_state(workflow_run_id, goal, ordered, total, final=final)
            if task_contract:
                payload["task_contract"] = task_contract
            if workflow_dag:
                payload["workflow_dag"] = workflow_dag
            payload["execution_budget"] = {
                "max_steps": max_steps,
                "max_seconds": workflow_seconds,
                "elapsed_seconds": round(time.monotonic() - workflow_started, 3),
                "tool_calls": tool_budget.snapshot(),
                "usage": usage_snapshot(),
            }
            return payload

        async def persist_state(
            status: str,
            *,
            current_step_id: str = "",
            final: bool = False,
        ) -> None:
            step_records.sort(
                key=lambda item: order.get(str(item.get("id") or ""), total + 1)
            )
            payload = workflow_state(final=final)
            payload["checkpoint"] = workflow_checkpoint_summary(
                steps,
                step_records,
                status=status,
                current_step_id=current_step_id,
            )
            await self._state_store.persist_result(
                workflow_run_id,
                payload,
                current_step_id=current_step_id,
            )
            await self._state_store.persist_checkpoint(
                workflow_run_id,
                ctx.task_id,
                steps,
                step_records,
                status=status,
                current_step_id=current_step_id,
                snapshot=payload,
            )

        await persist_state("started")
        for record in step_records:
            if record.get("status") not in {"succeeded", "degraded"}:
                continue
            await progress(
                "workflow.step.restored",
                min(0.99, len(terminal_ids) / total),
                step_id=record.get("id", ""),
                skill=record.get("skill_name", ""),
                execution_id=record.get("execution_id", ""),
                reason="restored_from_step_state",
            )

        cancelled = False
        while len(terminal_ids) < total:
            await execution_control.poll()
            cancelled = cancelled or execution_control.cancel_requested
            if cancelled:
                for step in steps:
                    step_id = str(step["id"])
                    if step_id in terminal_ids:
                        continue
                    result = {
                        "status": "skipped",
                        "warning": "cancelled by user",
                        "recoverable": True,
                    }
                    record = workflow_step_record(
                        step,
                        status="skipped",
                        result=result,
                        warning="cancelled by user",
                        skip_reason="cancelled",
                        recoverable=True,
                    )
                    step_records.append(record)
                    results_by_id[step_id] = result
                    terminal_ids.add(step_id)
                    failed_step_ids.add(step_id)
                    await self._state_store.persist_step_outcome(workflow_run_id, record)
                await persist_state("cancelled")
                break

            limit_reason = usage_limit_reason()
            if limit_reason:
                await self._state_store.skip_remaining(
                    workflow_run_id,
                    steps,
                    step_records,
                    results_by_id,
                    terminal_ids,
                    failed_step_ids,
                    reason=limit_reason,
                    warning="workflow token/cost execution envelope exhausted",
                )
                await persist_state(limit_reason)
                break
            if workflow_clock.expired():
                await self._state_store.skip_remaining(
                    workflow_run_id,
                    steps,
                    step_records,
                    results_by_id,
                    terminal_ids,
                    failed_step_ids,
                    reason="workflow_timeout",
                    warning="workflow execution envelope timed out",
                )
                await persist_state("workflow_timeout")
                break

            remaining = [step for step in steps if str(step["id"]) not in terminal_ids]
            ready = [step for step in remaining if dependencies_terminal(step, terminal_ids)]
            if not ready:
                for step in remaining:
                    step_id = str(step["id"])
                    deps = all_dependencies(step)
                    error = "workflow dependency cycle or unresolved dependency: " + ", ".join(deps)
                    result = {"status": "error", "error": error, "dependencies": deps}
                    record = workflow_step_record(
                        step, status="failed", result=result, error=error
                    )
                    step_records.append(record)
                    results_by_id[step_id] = result
                    terminal_ids.add(step_id)
                    failed_step_ids.add(step_id)
                    await self._state_store.persist_step_outcome(workflow_run_id, record)
                await persist_state("dependency_deadlock")
                break

            executable: list[dict[str, Any]] = []
            for step in ready:
                step_id = str(step["id"])
                entry = resolve_step_entry(self._registry, step)
                failed_deps = [
                    dep for dep in step.get("depends_on") or [] if dep in failed_step_ids
                ]
                if failed_deps and not _step_allows_failed_dependencies(step, entry):
                    result = {
                        "status": "skipped",
                        "error": "dependency failed",
                        "skip_reason": "depends_on_failed",
                    }
                    record = workflow_step_record(
                        step,
                        status="skipped",
                        result=result,
                        error="dependency failed",
                        skip_reason="depends_on_failed",
                        recoverable=step.get("required", True) is False,
                    )
                    step_records.append(record)
                    results_by_id[step_id] = result
                    terminal_ids.add(step_id)
                    failed_step_ids.add(step_id)
                    await self._state_store.persist_step_outcome(workflow_run_id, record)
                    await progress(
                        "workflow.step.skipped",
                        min(0.99, len(terminal_ids) / total),
                        step_id=step_id,
                        skill=step.get("skill_name", ""),
                        reason="depends_on_failed",
                        failed_dependencies=failed_deps,
                    )
                    await persist_state("step_skipped", current_step_id=step_id)
                else:
                    executable.append(step)
            if not executable:
                continue

            wave = select_execution_wave(
                executable, self._registry,
                concurrency=max(1, int(getattr(task_cfg, "workflow_concurrency", 1))),
            )
            snapshot = dict(results_by_id)
            try:
                # Publish the envelope clock while the wave runs so a step's
                # approval pauses it (step tasks copy this context).
                with register_clock(workflow_clock):
                    outcomes = await execution_control.run(
                        asyncio.gather(
                            *(
                                self._execute_step(
                                    workflow_run_id, goal, step, snapshot, ctx, progress,
                                    index=order[str(step["id"])], total=total,
                                    completed_count=len(terminal_ids),
                                    tool_budget=tool_budget, workflow_clock=workflow_clock,
                                )
                                for step in wave
                            )
                        )
                    )
            except (ExecutionCancelled, asyncio.CancelledError):
                execution_control.request_cancel()
                cancelled = True
                await settle_cancelled_wave(
                    workflow_run_id=workflow_run_id, wave=wave, step_records=step_records,
                    results_by_id=results_by_id, terminal_ids=terminal_ids,
                    failed_step_ids=failed_step_ids, state_store=self._state_store,
                    progress=progress, total=total,
                )
                await persist_state("cancelled")
                continue
            for step, outcome in sorted(
                zip(wave, outcomes, strict=True),
                key=lambda item: order[str(item[0]["id"])],
            ):
                step_id = str(step["id"])
                record = outcome["record"]
                result = outcome["result"]
                step_records.append(record)
                results_by_id[step_id] = result
                terminal_ids.add(step_id)
                if record["status"] in {"succeeded", "degraded"}:
                    completed_step_ids.add(step_id)
                else:
                    failed_step_ids.add(step_id)
                await self._state_store.persist_step_outcome(workflow_run_id, record)
                await persist_state(f"step_{record['status']}", current_step_id=step_id)

        result = workflow_state(final=True)
        mark_workflow_cancelled(result, cancelled=cancelled)
        result["checkpoint"] = workflow_checkpoint_summary(
            steps, step_records, status=result["status"]
        )
        await progress(
            "workflow.done",
            1.0,
            total_steps=total,
            skills_used=result["skills_used"],
            status=result["status"],
        )
        await self._state_store.persist_checkpoint(
            workflow_run_id,
            ctx.task_id,
            steps,
            step_records,
            status=result["status"],
            snapshot=result,
        )
        if result["status"] == "failed":
            raise WorkflowExecutionError(workflow_failure_message(step_records), result)
        return result

    async def _execute_step(
        self,
        workflow_run_id: str,
        goal: str,
        step: dict[str, Any],
        results_by_id: dict[str, Any],
        ctx: ExecContext,
        progress: Progress,
        *,
        index: int,
        total: int,
        completed_count: int,
        tool_budget: ToolExecutionBudget,
        workflow_clock: TurnClock,
    ) -> dict[str, Any]:
        step_id = str(step["id"])
        skill_name = str(step.get("skill_name") or "")
        entry = resolve_step_entry(self._registry, step)
        base_pct = completed_count / total
        span = 1.0 / total
        await self._state_store.mark_step_running(workflow_run_id, step_id)

        async def step_progress(stage: str, pct: float = 0.0, **data: Any) -> None:
            nested = dict(data)
            nested.pop("skill", None)
            emitted_step_id = nested.pop("step_id", None)
            if emitted_step_id and emitted_step_id != step_id:
                nested["skill_step_id"] = emitted_step_id
            await progress(
                f"workflow.step.{stage}",
                min(0.99, base_pct + span * max(0.0, min(1.0, pct))),
                step_id=step_id,
                skill=skill_name,
                **nested,
            )

        await progress(
            "workflow.step.start",
            base_pct,
            step_id=step_id,
            skill=skill_name,
            index=index,
            total_steps=total,
            concurrent_safe=step_concurrent_safe(step, entry),
        )
        authority_error = await native_workflow_authority_error(
            self._state_store,
            self._registry,
            workflow_run_id,
            step,
        )
        if authority_error:
            result = {
                "status": "error",
                "error": authority_error,
                "reason": "provider_authority_mismatch",
                "execution_started": False,
            }
            await progress(
                "workflow.step.failed",
                min(0.99, (completed_count + 1) / total),
                step_id=step_id,
                skill=skill_name,
                error=authority_error,
                provider_type=str(step.get("provider_type") or ""),
            )
            return {
                "result": result,
                "record": workflow_step_record(
                    step,
                    status="failed",
                    result=result,
                    error=authority_error,
                    recoverable=True,
                ),
            }
        if _is_native_workflow_step(step):
            arguments = {
                "goal": goal,
                "step": step,
                "upstream_results": results_by_id,
            }
            gateway = ToolGateway.from_context(
                ctx,
                event_family="workflow",
            )
            result = await gateway.invoke_operation(
                "native_synthesis",
                arguments,
                invoke=lambda: run_native_synthesis(
                    goal,
                    step,
                    results_by_id,
                    llm=getattr(ctx, "llm", None),
                    artifacts=getattr(ctx, "artifacts", None),
                    session_id=getattr(ctx, "session_id", ""),
                    task_id=getattr(ctx, "task_id", ""),
                    subtask_id=getattr(ctx, "subtask_id", ""),
                    workflow_run_id=workflow_run_id,
                ),
                input_schema=NATIVE_SYNTHESIS_INPUT_SCHEMA,
                output_schema=NATIVE_SYNTHESIS_OUTPUT_SCHEMA,
            )
            bind_deliverable_assessment_identity(result, step)
            result_status = str(result.get("status", "")).lower()
            failed = result_status in {"error", "failed"} or result.get("contract_violation") is True
            status = (
                "failed"
                if failed
                else "degraded"
                if result_status in {"partial", "degraded", "warning"}
                else "succeeded"
            )
            await progress(
                (
                    "workflow.step.done"
                    if status == "succeeded"
                    else "workflow.step.degraded"
                    if status == "degraded"
                    else "workflow.step.failed"
                ),
                min(0.99, (completed_count + 1) / total),
                step_id=step_id,
                skill="",
                index=index,
                total_steps=total,
                provider_type="native_executor",
                **(
                    {"error": _result_error_message(result)}
                    if status == "failed"
                    else {}
                ),
            )
            return {
                "result": result,
                "record": workflow_step_record(
                    step,
                    status=status,
                    result=result,
                    **(
                        {"error": _result_error_message(result)}
                        if status == "failed"
                        else {}
                    ),
                ),
            }

        workflow_step_row_id = await self._state_store.step_row_id(workflow_run_id, step_id)
        child_ctx = replace(
            ctx,
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_row_id,
            workflow_step_key=step_id,
            tool_budget=tool_budget,
            execution_clock=workflow_clock,
            provider_authority=(
                await self._state_store.provider_authority(
                    workflow_run_id,
                    step_id,
                )
            ),
        )
        step_input = _workflow_step_input(step, goal, results_by_id)
        if _is_child_task_step(step):
            outcome = await execute_child_task(step, step_input, child_ctx)
            return await classify_workflow_outcome(
                self._registry,
                step,
                outcome,
                progress,
                completed_count=completed_count,
                total=total,
            )
        if entry is None:
            error = f"unknown workflow skill '{skill_name}' at step {step_id}"
            result = {"status": "error", "error": error}
            await progress(
                "workflow.step.failed",
                min(0.99, (completed_count + 1) / total),
                step_id=step_id,
                skill=skill_name,
                error=error,
            )
            return {
                "result": result,
                "record": workflow_step_record(
                    step, status="failed", result=result, error=error
                ),
            }
        if self._step_executor is None:
            raise RuntimeError("workflow skill executor is not configured")
        outcome = await self._step_executor(
            workflow_run_id,
            step,
            step_input,
            child_ctx,
            step_progress,
        )
        return await classify_workflow_outcome(
            self._registry,
            step,
            outcome,
            progress,
            completed_count=completed_count,
            total=total,
        )

    async def _consume_controls(
        self,
        task_id: str,
        *,
        actions: set[str] | None = None,
    ) -> list[dict[str, str]]:
        if self._task_recorder is None or not task_id:
            return []
        return await self._task_recorder.consume_controls(
            task_id,
            actions=actions,
        )

    async def _acknowledge_controls(self, control_ids: list[str]) -> None:
        if self._task_recorder is None:
            return
        await self._task_recorder.mark_controls_applied(control_ids)


__all__ = ["WorkflowExecutionError", "WorkflowRuntime"]
