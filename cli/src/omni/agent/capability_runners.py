"""Capability runners for deterministic plan execution.

PlanExecutor owns orchestration, not deliverable-specific details.  A runner is
the runtime adapter for one capability/deliverable slot, similar to a tiny
Claude Science style workbench adapter: resolve provider, build provider input,
submit/execute work, and emit auditable run events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from omni.agent.figure_runner import ArtifactFigureRunner
from omni.agent.intent_plan import IntentPlan
from omni.agent.plan_result import PlanExecutionResult
from omni.agent.plan_revision import provider_authority_for_consumer
from omni.agent.plan_runner_utils import (
    completed_skill_answer,
    plan_capabilities,
    plan_summary,
    settlement_status,
)
from omni.core.react_agent import ToolInvocationRecord
from omni.core.tool_contracts import skill_input_contract_error
from omni.runtime.task_results import (
    action_required_presentation,
    installation_required_presentation,
)
from omni.skills_runtime.context import SKILL_SOURCE_PARAM, ExecContext, Tool


class CapabilityRunner(Protocol):
    capabilities: tuple[str, ...]

    def matches_provider(self, entry: Any | None) -> bool: ...

    async def run(
        self,
        plan: IntentPlan,
        *,
        ctx: ExecContext,
        tools: list[Tool],
        runtime: Any,
        tasks: Any,
        registry: Any,
        drain_tasks: bool,
        on_tool_event: Any = None,
        active_target: Any | None = None,
    ) -> PlanExecutionResult: ...


@dataclass(slots=True)
class CapabilityRunnerRegistry:
    runners: list[CapabilityRunner] = field(default_factory=list)

    def register(self, runner: CapabilityRunner) -> None:
        self.runners.append(runner)

    def for_capability(self, capability: str) -> CapabilityRunner | None:
        wanted = capability.strip().lower()
        for runner in self.runners:
            if wanted in {cap.lower() for cap in runner.capabilities}:
                return runner
        return None

    def for_provider(self, entry: Any | None) -> CapabilityRunner | None:
        for runner in self.runners:
            if runner.matches_provider(entry):
                return runner
        return None

    def for_plan(self, plan: IntentPlan, *, registry: Any) -> CapabilityRunner | None:
        """Resolve the runner for an already-validated plan.

        The executor should not know which concrete deliverable a plan contains.
        It asks this registry to map selected providers or capability-shaped
        outputs onto a runner.
        """
        for selection in plan.selected_skills:
            runner = self.for_provider(
                registry.resolve_ref(selection.skill, getattr(selection, "skill_source", ""))
            )
            if runner is not None:
                return runner
        for capability in plan_capabilities(plan):
            runner = self.for_capability(capability)
            if runner is not None:
                return runner
        return None


class SkillTaskRunner:
    """Generic runner for contracted skill tasks without deliverable-specific logic."""

    capabilities = ("skill.task",)

    def matches_provider(self, entry: Any | None) -> bool:
        return entry is not None

    async def run(
        self,
        plan: IntentPlan,
        *,
        ctx: ExecContext,
        tools: list[Tool],
        runtime: Any,
        tasks: Any,
        registry: Any,
        drain_tasks: bool,
        on_tool_event: Any = None,
        active_target: Any | None = None,
    ) -> PlanExecutionResult:
        del tools, active_target
        if not plan.selected_skills:
            return PlanExecutionResult(handled=False)
        selection = plan.selected_skills[0]
        skill = selection.skill
        skill_source = getattr(selection, "skill_source", "")
        entry = registry.resolve_ref(skill, skill_source)
        params = dict(plan.provider_inputs.get(skill) or {})
        contract_error = skill_input_contract_error(entry, params)
        if contract_error:
            await tasks.append_event(
                ctx.task_id,
                event_type="plan.tool.rejected",
                status="needs_input",
                name="pre_tool_use",
                skill_name=skill,
                input_json=params,
                output_json=contract_error,
                summary=contract_error["message"],
            )
            return PlanExecutionResult(
                handled=True,
                text=contract_error["message"],
                kind="needs_input",
                terminated_reason="pre_tool_use_rejected",
                plan_summary=plan_summary(plan),
                settlement_status="needs_input",
            )
        if skill_source:
            # Preserve the forced source so the worker resolves the same
            # (possibly shadowed) skill rather than the winning one.
            params = {**params, SKILL_SOURCE_PARAM: skill_source}
        subtask_id = await runtime.enqueue(
            skill,
            params,
            "" if drain_tasks else ctx.channel,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            provider_authority=provider_authority_for_consumer(
                ctx.execution_authority,
                consumer_kind="selected_skill",
                consumer_id="0",
            ),
        )
        trace_record = ToolInvocationRecord(
            name="run_skill",
            arguments={"skill_name": skill, "input": params, "mode": "foreground" if drain_tasks else "background"},
        )
        await tasks.append_event(
            ctx.task_id,
            event_type="plan.executed",
            status="succeeded",
            name=plan.intent_type.value,
            skill_name=skill,
            subtask_id=subtask_id,
            output_json={"intent_type": plan.intent_type.value, "submitted_subtask_ids": [subtask_id]},
            summary=f"submitted {skill}",
        )
        drained: list[dict[str, Any]] = []
        if drain_tasks:
            await runtime.process(
                subtask_id,
                on_event=on_tool_event,
                ctx_override=ctx,
            )
            task = await runtime.get_subtask(subtask_id)
            if task is not None:
                trace_record.result = {
                    "status": task.status,
                    "subtask_id": subtask_id,
                    "task_id": ctx.task_id,
                    "object_kind": "skill_execution",
                    "object_id": subtask_id,
                    "skill_name": task.skill_name,
                    "result": task.result_json,
                }
                trace_record.error = task.error
                drained.append(
                    {
                        "subtask_id": subtask_id,
                        "task_id": ctx.task_id,
                        "object_kind": "skill_execution",
                        "object_id": subtask_id,
                        "skill": task.skill_name,
                        "status": task.status,
                        "result": task.result_json,
                        "error": task.error,
                        "trace": task.trace_log,
                    }
                )
        else:
            trace_record.result = {
                "status": "submitted",
                "phase": "submitted",
                "subtask_id": subtask_id,
                "task_id": ctx.task_id,
                "object_kind": "skill_execution",
                "object_id": subtask_id,
                "skill_name": skill,
                "mode": "background",
            }
        action_required = action_required_presentation(drained)
        if action_required is not None:
            text, reason, _ = action_required
            return PlanExecutionResult(
                handled=True,
                text=text,
                kind="needs_input",
                submitted_subtask_ids=[subtask_id],
                drained_results=drained,
                tool_trace=[trace_record],
                terminated_reason=reason,
                plan_summary=plan_summary(plan),
                degraded_warnings=list(plan.degraded_warnings),
                settlement_status="needs_input",
            )
        installation_required = installation_required_presentation(drained)
        if installation_required is not None:
            text, reason, _ = installation_required
            return PlanExecutionResult(
                handled=True,
                text=text,
                kind="error",
                submitted_subtask_ids=[subtask_id],
                drained_results=drained,
                tool_trace=[trace_record],
                terminated_reason=reason,
                error=text,
                plan_summary=plan_summary(plan),
                degraded_warnings=list(plan.degraded_warnings),
                settlement_status="failed",
            )
        if drain_tasks and drained and settlement_status(drained) == "failed":
            # The plan bet the whole turn on one skill and the skill did not
            # deliver. Reporting that as the answer makes a routing choice look
            # like a verdict on the request: the user asked for a literature
            # review, not for news that one provider was unreachable. The
            # failure is real and worth reporting, but it is an observation
            # about a route, so hand it back unhandled — the turn continues into
            # ReAct, where the model sees what went wrong and still has the
            # search and web tools to reach the answer another way.
            return PlanExecutionResult(
                handled=False,
                submitted_subtask_ids=[subtask_id],
                drained_results=drained,
                tool_trace=[trace_record],
                terminated_reason="single_skill_failed",
                plan_summary=plan_summary(plan),
                degraded_warnings=list(plan.degraded_warnings),
            )
        if drain_tasks and drained:
            # The child finished in this turn. Codex would now speak from the
            # tool result; a ``Created execution`` receipt is only an ack for
            # work that still outlives the turn (daemon / IM).
            text = completed_skill_answer(drained, skill=skill)
        else:
            text = f"Created `{skill}` execution `id={subtask_id[:8]}` from the validated plan."
            if ctx.task_id:
                text += (
                    f" Parent task: `id={ctx.task_id[:8]}`. "
                    f"Use `/task show {ctx.task_id[:8]}` to inspect status and results."
                )
        return PlanExecutionResult(
            handled=True,
            text=text,
            kind="text",
            submitted_subtask_ids=[subtask_id],
            drained_results=drained,
            tool_trace=[trace_record],
            terminated_reason=plan.intent_type.value,
            plan_summary=plan_summary(plan),
            degraded_warnings=list(plan.degraded_warnings),
            settlement_status="pending_child_task" if not drain_tasks else settlement_status(drained),
        )
DEFAULT_CAPABILITY_RUNNERS = CapabilityRunnerRegistry([ArtifactFigureRunner(), SkillTaskRunner()])
