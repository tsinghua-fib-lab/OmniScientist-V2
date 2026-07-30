"""Execute high-confidence IntentPlans without falling back to wide ReAct."""

from __future__ import annotations

from typing import Any

from omni.agent.capability_runners import DEFAULT_CAPABILITY_RUNNERS, CapabilityRunnerRegistry
from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.plan_result import PlanExecutionResult
from omni.agent.plan_runner_utils import emit_tool_event, plan_summary, verification_status
from omni.core.react_agent import ToolInvocationRecord
from omni.memory.service import MemoryLayer
from omni.runtime.task_results import (
    action_required_presentation,
    installation_required_presentation,
)
from omni.skills_runtime.context import ExecContext, Tool


class PlanExecutor:
    """Deterministic executor for plan routes that should not enter broad ReAct."""

    def __init__(
        self,
        runtime: Any,
        tasks: Any,
        registry: Any,
        memory: Any = None,
        capability_runners: CapabilityRunnerRegistry | None = None,
    ) -> None:
        self._runtime = runtime
        self._tasks = tasks
        self._registry = registry
        self._memory = memory
        self._capability_runners = capability_runners or DEFAULT_CAPABILITY_RUNNERS

    async def execute(
        self,
        plan: IntentPlan,
        *,
        ctx: ExecContext,
        tools: list[Tool],
        drain_tasks: bool,
        on_tool_event: Any = None,
        active_target: Any | None = None,
    ) -> PlanExecutionResult:
        if plan.intent_type in {IntentType.QA_PLUS_ARTIFACT, IntentType.SINGLE_SKILL_TASK}:
            runner = self._capability_runners.for_plan(plan, registry=self._registry)
            if runner is None:
                return PlanExecutionResult(handled=False)
            return await runner.run(
                plan,
                ctx=ctx,
                tools=tools,
                runtime=self._runtime,
                tasks=self._tasks,
                registry=self._registry,
                drain_tasks=drain_tasks,
                on_tool_event=on_tool_event,
                active_target=active_target,
            )
        if plan.intent_type == IntentType.WORKFLOW:
            return await self._workflow(plan, ctx=ctx, drain_tasks=drain_tasks, on_tool_event=on_tool_event)
        if plan.intent_type == IntentType.MEMORY_UPDATE:
            return await self._memory_update(plan, ctx=ctx)
        return PlanExecutionResult(handled=False)

    async def _memory_update(self, plan: IntentPlan, *, ctx: ExecContext) -> PlanExecutionResult:
        memory_input = plan.capability_inputs.get("memory.update") or {}
        summary = str(memory_input.get("content") or "").strip()
        if not summary:
            return PlanExecutionResult(
                handled=True,
                text="I need the exact fact or preference that should be remembered.",
                kind="needs_input",
                terminated_reason="missing_memory_content",
                plan_summary=plan_summary(plan),
                verification_status="needs_input",
            )
        memory_id = ""
        if self._memory is not None and summary:
            memory_id = await self._memory.record(
                layer=MemoryLayer.SEMANTIC,
                scope="project",
                scope_id="",
                summary=summary,
                memory_type="user_note",
                tags=["user-confirmed"],
                importance=0.75,
                pinned=True,
            )
        text = f"Remembered: {summary}" + (f"\nMemory id: `{memory_id[:8]}`" if memory_id else "")
        await self._tasks.append_event(
            ctx.task_id,
            event_type="plan.executed",
            status="succeeded",
            name=plan.intent_type.value,
            output_json={"intent_type": plan.intent_type.value, "memory_id": memory_id, "summary": summary},
            summary=f"recorded memory {memory_id[:8] if memory_id else ''}".strip(),
        )
        return PlanExecutionResult(
            handled=True,
            text=text,
            kind="text",
            terminated_reason=plan.intent_type.value,
            plan_summary=plan_summary(plan),
            degraded_warnings=list(plan.degraded_warnings),
            verification_status="passed",
        )

    async def _workflow(
        self,
        plan: IntentPlan,
        *,
        ctx: ExecContext,
        drain_tasks: bool,
        on_tool_event: Any = None,
    ) -> PlanExecutionResult:
        if not plan.workflow_steps:
            return PlanExecutionResult(
                handled=True,
                text=(
                    "The request requires a multi-step research workflow, but the validated plan has no "
                    "executable steps. Specify the expected deliverable or an explicit skill."
                ),
                kind="error",
                terminated_reason="plan_validation_failed",
                error="workflow_steps empty",
                plan_summary=plan_summary(plan),
                degraded_warnings=list(plan.degraded_warnings),
                verification_status="failed",
            )
        workflow_run_id = await self._runtime.enqueue_workflow(
            plan.user_message,
            plan.workflow_steps,
            "" if drain_tasks else ctx.channel,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            task_contract=plan.task_contract,
            workflow_dag=plan.workflow_dag,
            execution_authority=(
                ctx.execution_authority.to_dict()
                if ctx.execution_authority is not None
                else None
            ),
        )
        await self._tasks.append_event(
            ctx.task_id,
            event_type="plan.executed",
            status="succeeded",
            name=plan.intent_type.value,
            workflow_run_id=workflow_run_id,
            output_json={
                "intent_type": plan.intent_type.value,
                "task_id": ctx.task_id,
                "object_kind": "workflow_run",
                "object_id": workflow_run_id,
                "submitted_workflow_ids": [workflow_run_id],
                "workflow_steps": plan.workflow_steps,
                "task_contract": plan.task_contract,
                "workflow_dag": plan.workflow_dag,
            },
            summary=f"submitted workflow {workflow_run_id[:8]}",
        )
        await emit_tool_event(
            on_tool_event,
            "plan",
            {
                "event_type": "plan.executed",
                "name": plan.intent_type.value,
                "summary": f"submitted workflow {workflow_run_id[:8]}",
                "task_id": ctx.task_id,
                "object_kind": "workflow_run",
                "object_id": workflow_run_id,
                "workflow_run_id": workflow_run_id,
            },
        )
        trace_record = ToolInvocationRecord(
            name="run_workflow",
            arguments={"goal": plan.user_message, "steps": plan.workflow_steps, "mode": "foreground" if drain_tasks else "background"},
        )
        drained: list[dict[str, Any]] = []
        if drain_tasks:
            await self._runtime.process(
                workflow_run_id,
                on_event=on_tool_event,
                ctx_override=ctx,
            )
            workflow = await self._runtime.get_workflow_run(workflow_run_id)
            if workflow is not None:
                result_payload = (
                    dict(workflow.result_json or {})
                    if isinstance(workflow.result_json, dict)
                    else {}
                )
                result_payload.setdefault("workflow_status", workflow.status)
                trace_record.result = result_payload
                trace_record.error = workflow.error
                drained.append({
                    "task_id": ctx.task_id,
                    "object_kind": "workflow_run",
                    "object_id": workflow_run_id,
                    "workflow_run_id": workflow_run_id,
                    "kind": "workflow",
                    "status": workflow.status,
                    "result": result_payload,
                    "error": workflow.error,
                    "trace": workflow.trace_log,
                })
        else:
            trace_record.result = {
                "status": "submitted",
                "task_id": ctx.task_id,
                "object_kind": "workflow_run",
                "object_id": workflow_run_id,
                "workflow_run_id": workflow_run_id,
                "kind": "workflow",
                "step_count": len(plan.workflow_steps),
            }
        step_names = " → ".join(str(step.get("skill_name") or step.get("skill") or step.get("id")) for step in plan.workflow_steps)
        text_lines = [
            f"Created research workflow `workflow={workflow_run_id[:8]}` from the validated plan.",
            f"- Steps: {step_names}",
            f"- Reason: {plan.rationale}",
        ]
        if ctx.task_id:
            text_lines.append(
                f"Use `/task show {ctx.task_id[:8]}` to inspect the complete task, trace, and result."
            )
        text_lines.append(
            f"Workflow details: `/task show {workflow_run_id[:8]}`."
        )
        text = "\n".join(text_lines)
        if drained and isinstance(drained[0].get("result"), dict):
            warning = _workflow_warning_summary(drained[0]["result"])
            if warning:
                text += f"\n- Degraded: {warning}"
        workflow_status = str(drained[0].get("status") or "") if drained else ""
        action_required = action_required_presentation(drained)
        if workflow_status == "failed" and action_required is not None:
            text, reason, _ = action_required
            return PlanExecutionResult(
                handled=True,
                text=text,
                kind="needs_input",
                submitted_workflow_ids=[workflow_run_id],
                drained_results=drained,
                tool_trace=[trace_record],
                terminated_reason=reason,
                plan_summary=plan_summary(plan),
                degraded_warnings=list(plan.degraded_warnings),
                verification_status="needs_input",
            )
        installation_required = installation_required_presentation(drained)
        if workflow_status == "failed" and installation_required is not None:
            text, reason, _ = installation_required
            return PlanExecutionResult(
                handled=True,
                text=text,
                kind="error",
                submitted_workflow_ids=[workflow_run_id],
                drained_results=drained,
                tool_trace=[trace_record],
                terminated_reason=reason,
                error=text,
                plan_summary=plan_summary(plan),
                degraded_warnings=list(plan.degraded_warnings),
                verification_status="failed",
            )
        if workflow_status in {"cancelled", "interrupted"}:
            trace_record.status = "cancelled"
            trace_record.error = f"workflow {workflow_status} by user request"
            text = (
                f"Workflow `{workflow_run_id[:8]}` was {workflow_status}. "
                "Completed steps, artifacts, and its recovery checkpoint were preserved."
            )
            return PlanExecutionResult(
                handled=True,
                text=text,
                kind="partial",
                submitted_workflow_ids=[workflow_run_id],
                drained_results=drained,
                tool_trace=[trace_record],
                terminated_reason=workflow_status,
                plan_summary=plan_summary(plan),
                degraded_warnings=list(plan.degraded_warnings),
                verification_status="skipped",
            )
        return PlanExecutionResult(
            handled=True,
            text=text,
            kind="workflow",
            submitted_workflow_ids=[workflow_run_id],
            drained_results=drained,
            tool_trace=[trace_record],
            terminated_reason=plan.intent_type.value,
            plan_summary=plan_summary(plan),
            degraded_warnings=list(plan.degraded_warnings),
            verification_status="pending_child_task" if not drain_tasks else verification_status(drained),
        )


def _workflow_warning_summary(result: dict[str, Any]) -> str:
    steps = result.get("steps")
    if not isinstance(steps, list):
        return ""
    notes: list[str] = []
    for step in steps:
        if not isinstance(step, dict) or step.get("status") not in {
            "failed", "degraded", "skipped", "cancelled",
        }:
            continue
        note = str(step.get("error") or step.get("warning") or step.get("skip_reason") or "")
        skill = str(step.get("skill_name") or step.get("id") or "step")
        notes.append(f"{skill}: {note}" if note else skill)
        if len(notes) >= 3:
            break
    return "; ".join(notes)
