"""Runtime adapter for the ``artifact.figure`` deliverable."""

from __future__ import annotations

from typing import Any

from omni.agent.intent_plan import IntentPlan
from omni.agent.plan_result import PlanExecutionResult
from omni.agent.plan_revision import provider_authority_for_consumer
from omni.agent.plan_runner_utils import plan_summary, verification_status
from omni.core.react_agent import ToolInvocationRecord
from omni.core.tool_result import tool_event_output
from omni.runtime.tool_gateway import ToolGateway
from omni.skills_runtime.context import SKILL_SOURCE_PARAM, ExecContext, Tool


class ArtifactFigureRunner:
    """Runner for providers that declare an ``artifact.figure`` contract."""

    capabilities = ("artifact.figure", "figure.architecture", "figure.workflow")

    def matches_provider(self, entry: Any | None) -> bool:
        caps = {str(item).lower() for item in getattr(entry, "capabilities", []) or []}
        deliverables = {str(item).lower() for item in getattr(entry, "deliverables", []) or []}
        revision = getattr(entry, "artifact_revision", {}) if entry is not None else {}
        return (
            bool(caps.intersection(self.capabilities))
            or "artifact.figure" in deliverables
            or (isinstance(revision, dict) and bool(revision))
        )

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
        trace: list[ToolInvocationRecord] = []
        search_result: Any = None
        if plan.intent_type.value == "qa_plus_artifact":
            search_result, search_trace = await self._search_if_available(
                plan,
                ctx=ctx,
                tools=tools,
                on_tool_event=on_tool_event,
            )
            trace.extend(search_trace)
            if _is_contract_violation(search_result):
                return PlanExecutionResult(
                    handled=True,
                    text=(
                        "The local evidence search returned data that did not match its declared "
                        "contract, so Omni stopped before creating a figure from untrusted input."
                    ),
                    kind="error",
                    tool_trace=trace,
                    terminated_reason="search_output_contract_violation",
                    error="search_corpus output failed contract validation",
                    plan_summary=plan_summary(plan),
                    verification_status="failed",
                )

        selection = plan.selected_skills[0] if plan.selected_skills else None
        skill = selection.skill if selection else ""
        entry = (
            registry.resolve_ref(skill, getattr(selection, "skill_source", ""))
            if skill
            else None
        )
        if entry is None:
            return PlanExecutionResult(
                handled=True,
                text=(
                    "A figure deliverable was requested, but the validated plan has no executable provider. "
                    "Install or enable a compatible figure skill and retry."
                ),
                kind="needs_input",
                terminated_reason="missing_artifact_figure_provider",
                plan_summary=plan_summary(plan),
                verification_status="needs_input",
            )
        params = dict(plan.provider_inputs.get(skill) or {})
        skill_source = getattr(selection, "skill_source", "") if selection else ""
        if skill_source:
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
            output_json={
                "intent_type": plan.intent_type.value,
                "capability": "artifact.figure",
                "provider": skill,
                "submitted_subtask_ids": [subtask_id],
                "tool_trace": [r.name for r in trace],
            },
            summary=f"submitted {skill} for artifact.figure",
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
        trace.append(trace_record)

        title = str(params.get("title") or "Scientific Figure")
        if plan.intent_type.value == "qa_plus_artifact":
            lead = _grounded_answer(search_result)
            text = (
                f"{lead}\n\n"
                f"Created `{skill}` execution `id={subtask_id[:8]}` to generate \"{title}\"."
            )
            if ctx.task_id:
                text += (
                    f" Parent task: `id={ctx.task_id[:8]}`. "
                    f"Use `/task show {ctx.task_id[:8]}` to inspect artifacts and the audit trace."
                )
        else:
            text = f"Created `{skill}` execution `id={subtask_id[:8]}` to generate \"{title}\"."
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
            tool_trace=trace,
            terminated_reason=plan.intent_type.value,
            plan_summary=plan_summary(plan),
            degraded_warnings=list(plan.degraded_warnings),
            verification_status="pending_child_task" if not drain_tasks else verification_status(drained),
        )

    async def _search_if_available(
        self,
        plan: IntentPlan,
        *,
        ctx: ExecContext,
        tools: list[Tool],
        on_tool_event: Any = None,
    ) -> tuple[Any, list[ToolInvocationRecord]]:
        trace: list[ToolInvocationRecord] = []
        search_tool = next((tool for tool in tools if tool.spec.name == "search_corpus"), None)
        if search_tool is None:
            return None, trace
        args = {"query": _search_query(plan.user_message), "k": 2}
        record = ToolInvocationRecord(name="search_corpus", arguments=args)
        gateway = ToolGateway.from_context(
            ctx,
            event_family="figure_runner",
            upstream=on_tool_event,
        )
        try:
            record.result = await gateway.invoke_operation(
                "search_corpus",
                args,
                invoke=lambda: search_tool.handler(args),
                sensitive=search_tool.sensitive,
                input_schema=search_tool.input_schema,
                output_schema=search_tool.output_schema,
            )
        except Exception as exc:  # noqa: BLE001
            record.error = f"{type(exc).__name__}: {exc}"
        trace.append(record)
        return record.result, trace


def _search_query(message: str) -> str:
    return " ".join((message or "").split())


def _is_contract_violation(result: Any) -> bool:
    output = tool_event_output(result)
    return isinstance(output, dict) and output.get("contract_violation") is True


def _grounded_answer(search_result: Any) -> str:
    if isinstance(search_result, dict) and search_result.get("matches"):
        return "Local corpus evidence was found and will be available to the figure task."
    if isinstance(search_result, dict) and search_result.get("status") == "empty":
        return "No local corpus evidence matched; the figure will mark unsupported details when applicable."
    return (
        "This request has answer and figure deliverables: qa.grounded or synthesis.final owns the answer, "
        "and the artifact.figure provider owns the figure."
    )
