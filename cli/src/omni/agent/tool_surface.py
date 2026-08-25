"""Build the coordinator's skill/tool surface without owning turn orchestration."""

from __future__ import annotations

import copy
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from omni.agent.interaction_lifecycle import enqueue_notify_channel, resolve_execution_mode
from omni.agent.plan_runner_utils import workflow_terminal_message
from omni.agent.schedule_tools import build_schedule_tools
from omni.agent.skill_lookup import (
    FIND_SKILL_NEXT_ACTION,
    rank_skill_matches,
    skill_contract_card,
)
from omni.core.funnel_facts import project_skill_observation
from omni.core.react_agent import ToolSpec
from omni.core.tool_contracts import admit_provider_arguments
from omni.core.tool_exposure import apply_default_exposure
from omni.core.tool_result import (
    is_tool_rejection,
    owned_result_outcome,
    tool_event_output,
)
from omni.runtime.execution_policy import skill_requires_approval
from omni.runtime.subtask_runtime import WorkflowNeedsInput
from omni.skills_runtime.builtin_tools import build_builtin_tools
from omni.skills_runtime.context import SKILL_SOURCE_PARAM, ExecContext, Tool
from omni.skills_runtime.executor import execute_skill
from omni.skills_runtime.manifest import SkillKind
from omni.skills_runtime.registry import scope_sources

MCPLoader = Callable[[ExecContext], Awaitable[list[Tool]]]


class ToolSurfaceBuilder:
    """Construct tools and their thin runtime adapters from shared services."""

    def __init__(self, runtime: Any, tasks: Any, registry: Any, mcp_loader: MCPLoader) -> None:
        self.runtime = runtime
        self.tasks = tasks
        self.registry = registry
        self.mcp_loader = mcp_loader

    async def build(
        self,
        ctx: ExecContext,
        *,
        wait_for_tasks: bool,
        on_tool_event: Any = None,
        external_tools: list[Tool] | None = None,
        external_authoritative: bool = False,
    ) -> list[Tool]:
        external = list(external_tools or [])
        if external_authoritative:
            return external
        tools: list[Tool] = list(build_builtin_tools(ctx))
        for entry in self.registry.list_sync_tools():
            if entry.kind in (SkillKind.PYTHON_ENGINE, SkillKind.CLI_EXEC):
                tools.append(self._sync_skill(entry, ctx))
        # ``find_skill`` doubles as the lookup for tools whose schema this turn
        # does not send. It is built before the deferred set is known, so it reads
        # the list lazily; by the time the model can call it, the list is filled.
        deferred_specs: list[ToolSpec] = []
        tools.extend(
            [
                self._find_skill(deferred_specs, ctx),
                self._run_skill(ctx, wait_for_tasks=wait_for_tasks, on_tool_event=on_tool_event),
                self._run_workflow(ctx, wait_for_tasks=wait_for_tasks, on_tool_event=on_tool_event),
            ]
        )
        # Scheduling tools live only on the top-level coordinator surface (not on
        # prompt-skill/subagent surfaces built from ``build_builtin_tools``), so a
        # scheduled ``agent-goal`` run cannot recursively create more schedules.
        # A headless scheduled turn also runs the full coordinator surface but
        # sets ``allow_scheduling=False`` for the same recursion guard.
        if getattr(ctx, "allow_scheduling", True):
            tools.extend(build_schedule_tools(self.runtime, ctx))
        # Everything assembled so far is an Omni-owned result schema.
        # External/MCP tools below retain their own explicit success channel.
        for tool in tools:
            if tool.outcome_resolver is None:
                tool.outcome_resolver = owned_result_outcome
        tools.extend(await self.mcp_loader(ctx))
        if external:
            by_name = {tool.spec.name: tool for tool in tools}
            by_name.update({tool.spec.name: tool for tool in external})
            tools = list(by_name.values())
        apply_default_exposure(tools)
        deferred_specs.extend(t.spec for t in tools if t.spec.exposure != "direct")
        return tools

    def _find_skill(self, deferred_specs: list[ToolSpec] | None = None, ctx: Any = None) -> Tool:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            query = str(args.get("query", "")).lower().strip()
            selectable = self.registry.list_selectable()
            services = None
            admit = getattr(self.registry, "admission_services", None)
            if callable(admit):
                services = admit(ctx=ctx)
            hits = [
                skill_contract_card(entry, services=services, ctx=ctx)
                for entry in rank_skill_matches(
                    selectable, query, services=services, ctx=ctx
                )
            ]
            result: dict[str, Any] = {"matches": hits, "total_skills": len(selectable)}
            if hits:
                result["next_action"] = FIND_SKILL_NEXT_ACTION
            # Tools whose schema this turn withheld are looked up here, so the
            # model can read a parameter list it was not sent instead of guessing
            # one. Matching is by name substring: the model already knows the
            # names from the catalog block, it is the arguments it is missing.
            unlisted = [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                }
                for spec in (deferred_specs or [])
                if not query or query in spec.name.lower() or spec.name.lower() in query
            ]
            if unlisted:
                result["unlisted_tools"] = unlisted[:10]
            return result

        return Tool(
            ToolSpec(
                "find_skill",
                (
                    "Load one skill's routing instructions and input_schema. If a catalog "
                    "description matches, query that exact name, follow the returned instructions, "
                    "then call run_skill. Do not switch to a neighbour skill because memory "
                    "mentions SVG/PNG or because you want to author a .dot first. Also looks up "
                    "parameters of tools listed with their schema omitted."
                ),
                {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            ),
            handler,
        )

    @staticmethod
    def _parameters(
        args: dict[str, Any],
        *,
        prefer_nonempty_input: bool = False,
    ) -> dict[str, Any] | None:
        if prefer_nonempty_input:
            raw = args.get("input") or args.get("parameters") or {}
        else:
            raw = args.get("input") if "input" in args else args.get("parameters", {})
        if isinstance(raw, str):
            return {"input": raw}
        return raw if isinstance(raw, dict) else None

    def _resolve(self, name: str, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        # Honour a ``$<scope>:<name>`` escape when the model asks for a specific
        # source; otherwise resolve the winning skill exactly as before.
        source = ""
        scope, sep, rest = name.partition(":")
        if sep and rest and scope_sources(scope) is not None:
            entry = self.registry.resolve_explicit(name)
            if entry is not None:
                source = entry.source
        else:
            entry = self.registry.get(name)
        resolution: dict[str, Any] = {
            "skill_name": entry.name if entry is not None else name,
            "planned_skill_name": "",
            "capability_resolution": {},
            "skill_source": source,
        }
        return entry, resolution

    def _skill_admission_target(self, args: dict[str, Any]) -> str:
        """Resolve a generic skill wrapper to its concrete policy target."""
        name = str(args.get("skill_name", "") or args.get("skill", "")).strip()
        params = self._parameters(args, prefer_nonempty_input=True) or {}
        entry, _resolution = self._resolve(name, params)
        return str(entry.name if entry is not None else name)

    async def _validate_choice(
        self,
        name: str,
        params: dict[str, Any],
        ctx: ExecContext,
    ) -> tuple[Any, dict[str, Any], dict[str, Any] | None]:
        entry, resolution = self._resolve(name, params)
        if entry is None:
            return None, resolution, {"error": f"unknown skill '{name}'; use find_skill to search the catalog"}
        if entry.is_deprecated:
            return entry, resolution, {"error": f"skill '{entry.name}' is deprecated", "replaced_by": entry.replaced_by}
        admitted, contract_error = admit_provider_arguments(entry, params)
        if contract_error:
            await self.tasks.append_event(
                ctx.task_id,
                event_type="plan.tool.rejected",
                status="needs_input",
                name="pre_tool_use",
                skill_name=entry.name,
                input_json=params,
                output_json=contract_error,
                summary=contract_error["message"],
            )
            return entry, resolution, {"status": "needs_input", "skill_name": entry.name, **contract_error}
        resolution["admitted_input"] = admitted
        return entry, resolution, None

    @staticmethod
    def _with_resolution(params: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
        if not resolution["planned_skill_name"]:
            return params
        return {
            **params,
            "planned_skill_name": resolution["planned_skill_name"],
            "capability_resolution": resolution["capability_resolution"],
        }

    def _run_skill(self, ctx: ExecContext, *, wait_for_tasks: bool, on_tool_event: Any) -> Tool:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args.get("skill_name", "") or args.get("skill", "")).strip()
            params = self._parameters(args)
            if params is None:
                return {"error": "input/parameters must be an object or string"}
            entry, resolution, error = await self._validate_choice(name, params, ctx)
            if error:
                return error
            recovery_authority, authority_error = (
                _recovery_selected_skill_authority(ctx, entry)
            )
            if authority_error:
                return {
                    "status": "rejected",
                    "reason": "selected_skill_provider_authority_mismatch",
                    "error": authority_error,
                }
            params = self._with_resolution(
                dict(resolution.pop("admitted_input", params) or params),
                resolution,
            )
            if resolution.get("skill_source"):
                params = {**params, SKILL_SOURCE_PARAM: resolution["skill_source"]}
            mode = resolve_execution_mode(
                args.get("mode"), wait_for_tasks=wait_for_tasks, is_async=entry.is_async
            )
            if recovery_authority is not None and mode == "inline":
                mode = "foreground" if wait_for_tasks else "background"
            notify_channel = enqueue_notify_channel(
                ctx.channel, mode=mode, wait_for_tasks=wait_for_tasks
            )
            if mode == "background":
                subtask_id = await self.runtime.enqueue(
                    entry.name,
                    params,
                    notify_channel,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    **(
                        {"provider_authority": recovery_authority}
                        if recovery_authority is not None
                        else {}
                    ),
                )
                return {
                    "status": "submitted",
                    "subtask_id": subtask_id,
                    "task_id": ctx.task_id,
                    "object_kind": "skill_execution",
                    "object_id": subtask_id,
                    "skill_name": entry.name,
                    "planned_skill_name": resolution["planned_skill_name"],
                    "capability_resolution": resolution["capability_resolution"],
                    "mode": mode,
                    # Not terminal. Dispatching one skill is an action, not an
                    # answer: "get the abstract, draw the diagram, and write the
                    # paper" is three deliverables, and ending the turn on the
                    # first submission silently dropped the other two while still
                    # settling succeeded. run_workflow stays terminal because a
                    # workflow *is* the whole plan for the request; one skill is
                    # not. The model decides when it is done, as it does in Codex
                    # and Claude Code.
                    "message": (
                        f"Submitted background skill {entry.name} as execution {subtask_id}"
                        + (f" under task {ctx.task_id}." if ctx.task_id else ".")
                        + " It runs on its own; continue with any remaining work,"
                        " and finish when nothing is left."
                    ),
                }
            if mode == "foreground":
                subtask_id = await self.runtime.enqueue(
                    entry.name,
                    params,
                    notify_channel,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    **(
                        {"provider_authority": recovery_authority}
                        if recovery_authority is not None
                        else {}
                    ),
                )
                await self.runtime.process(
                    subtask_id,
                    on_event=on_tool_event,
                    ctx_override=ctx,
                )
                task = await self.runtime.get_subtask(subtask_id)
                body = task.result_json if task is not None else {}
                extra = {
                    "status": task.status if task is not None else "unknown",
                    "subtask_id": subtask_id,
                    "task_id": ctx.task_id,
                    "object_kind": "skill_execution",
                    "object_id": subtask_id,
                    "skill_name": entry.name,
                    "planned_skill_name": resolution["planned_skill_name"],
                    "capability_resolution": resolution["capability_resolution"],
                    "mode": mode,
                }
                if task is not None and task.error:
                    extra["error"] = task.error
                observed = project_skill_observation(body, extra=extra)
                return observed
            result = await execute_skill(
                entry,
                params,
                ctx,
                progress_callback=_inline_usage_progress(on_tool_event),
            )
            if _concrete_skill_failed(result):
                return result
            return project_skill_observation(
                result,
                extra={
                    "skill_name": entry.name,
                    "planned_skill_name": resolution["planned_skill_name"],
                    "capability_resolution": resolution["capability_resolution"],
                    "mode": "inline",
                },
            )

        return Tool(
            ToolSpec(
                "run_skill",
                (
                    "Run one skill. inline waits without persistence. foreground persists and waits "
                    "only on a turn that drains (CLI). On IM or the daemon it detaches like "
                    "background — the work outlives the turn and files arrive on hop 2. "
                    "background returns an execution id. auto chooses from skill duration and detach mode."
                ),
                {
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string"},
                        "skill": {"type": "string"},
                        "mode": {"type": "string", "enum": ["auto", "inline", "foreground", "background"]},
                        "input": {"description": "Skill input as an object or string."},
                        "parameters": {"type": "object"},
                    },
                    "required": ["skill_name"],
                },
            ),
            handler,
            admission_target=self._skill_admission_target,
        )

    def _run_workflow(self, ctx: ExecContext, *, wait_for_tasks: bool, on_tool_event: Any) -> Tool:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            goal = str(args.get("goal") or args.get("input") or "").strip()
            steps = args.get("steps")
            if steps is None and isinstance(args.get("plan"), dict):
                steps = args["plan"].get("steps")
                goal = goal or str(args["plan"].get("goal") or "")
            if not isinstance(steps, list) or not steps:
                return {"error": "workflow requires a non-empty steps list"}
            steps = copy.deepcopy(steps)
            execution_authority, authority_error = (
                _recovery_workflow_authority(ctx, steps)
            )
            if authority_error:
                return {
                    "status": "rejected",
                    "reason": "workflow_provider_authority_mismatch",
                    "error": authority_error,
                }
            mode = resolve_execution_mode(args.get("mode"), wait_for_tasks=wait_for_tasks, is_async=True)
            notify_channel = enqueue_notify_channel(
                ctx.channel, mode=mode, wait_for_tasks=wait_for_tasks
            )
            try:
                workflow_run_id = await self.runtime.enqueue_workflow(
                    goal,
                    steps,
                    notify_channel,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    task_contract=args.get("task_contract") if isinstance(args.get("task_contract"), dict) else None,
                    workflow_dag=args.get("workflow_dag") if isinstance(args.get("workflow_dag"), dict) else None,
                    execution_authority=execution_authority,
                )
            except WorkflowNeedsInput as exc:
                return {
                    "status": "needs_input",
                    "skill_name": "workflow",
                    "mode": mode,
                    "message": "The workflow lacks required input. Ask the user before creating a task.",
                    "missing": exc.missing,
                }
            if mode == "background":
                return {
                    "status": "submitted",
                    "workflow_run_id": workflow_run_id,
                    "task_id": ctx.task_id,
                    "object_kind": "workflow_run",
                    "object_id": workflow_run_id,
                    "kind": "workflow",
                    "mode": mode,
                    "step_count": len(steps),
                    "notify_channel": notify_channel,
                    "message": (
                        f"Submitted workflow run {workflow_run_id}"
                        + (f" under task {ctx.task_id}" if ctx.task_id else "")
                        + f" with {len(steps)} steps."
                    ),
                    "_omni_control": {"terminal": True},
                }
            await self.runtime.process(
                workflow_run_id,
                on_event=on_tool_event,
                ctx_override=ctx,
            )
            workflow = await self.runtime.get_workflow_run(workflow_run_id)
            result = workflow.result_json if workflow is not None else {}
            result_status = (
                str(result.get("status"))
                if isinstance(result, dict) and result.get("status")
                else workflow.status if workflow is not None else "unknown"
            )
            response = {
                **result,
                "status": result_status,
                "workflow_status": workflow.status if workflow is not None else "unknown",
                "workflow_run_id": workflow_run_id,
                "task_id": ctx.task_id,
                "object_kind": "workflow_run",
                "object_id": workflow_run_id,
                "kind": "workflow",
                "mode": "foreground",
                **(
                    {"error": workflow.error}
                    if workflow is not None and workflow.error
                    else {}
                ),
            }
            if workflow is not None and workflow.status == "failed":
                response["_omni_control"] = {"terminal": True}
                response["message"] = workflow_terminal_message(response, task_id=ctx.task_id)
            return response

        return Tool(
            ToolSpec(
                "run_workflow",
                (
                    "Execute a model-proposed multi-skill workflow. Use it when work needs multiple providers or "
                    "one step consumes another step's output. Each step needs id, skill/skill_name, input/parameters, "
                    "and optional depends_on. foreground waits; background returns the workflow run id."
                ),
                {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "mode": {"type": "string", "enum": ["auto", "foreground", "background", "inline"]},
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "skill": {"type": "string"},
                                    "skill_name": {"type": "string"},
                                    "skill_source": {"type": "string"},
                                    "capability": {"type": "string"},
                                    "provider_type": {"type": "string"},
                                    "deliverable": {"type": "string"},
                                    "input": {"type": "object"},
                                    "parameters": {"type": "object"},
                                    "depends_on": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                        "plan": {"type": "object"},
                    },
                    "required": ["steps"],
                },
            ),
            handler,
        )

    @staticmethod
    def _sync_skill(entry: Any, ctx: ExecContext) -> Tool:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return await execute_skill(entry, args, ctx)

        return Tool(
            ToolSpec(
                entry.name,
                entry.short_desc(220),
                entry.input_schema,
                replay_safe=entry.replay_safe,
            ),
            handler,
            sensitive=skill_requires_approval(entry),
            input_schema=entry.input_schema,
            output_schema=entry.output_schema,
            replay_safe=entry.replay_safe,
        )


def _recovery_workflow_authority(
    ctx: ExecContext,
    steps: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Rebind a recovery workflow only to its pre-authorised provider DAG.

    Ordinary ReAct turns have no workflow-step authorities and retain dynamic
    workflow behavior. A recovery turn carries the abandoned typed DAG in its
    execution authority; once present, changing step ids/providers requires a
    separately planned and authorised replacement instead of silently
    satisfying old quality obligations with a different provider.
    """

    # A child skill may inherit the coordinator context object. Its delegated
    # tools are governed by ``provider_authority``, not by the parent recovery
    # plan's consumer bindings.
    if str(getattr(ctx, "subtask_id", "") or ""):
        return None, ""
    authority = getattr(ctx, "execution_authority", None)
    if authority is None:
        return None, ""
    payload = (
        authority.to_dict()
        if callable(getattr(authority, "to_dict", None))
        else copy.deepcopy(authority)
        if isinstance(authority, dict)
        else {}
    )
    providers = [
        item
        for item in payload.get("provider_authorities") or []
        if isinstance(item, dict)
        and str(item.get("consumer_kind") or "") == "workflow_step"
    ]
    if not providers:
        return None, ""

    by_id = {
        str(item.get("consumer_id") or ""): item
        for item in providers
        if str(item.get("consumer_id") or "")
    }
    submitted_ids = [str(step.get("id") or "") for step in steps]
    if (
        not all(submitted_ids)
        or len(set(submitted_ids)) != len(submitted_ids)
        or set(submitted_ids) != set(by_id)
    ):
        return None, (
            "recovery workflow steps do not match the authorised provider DAG; "
            "re-plan before replacing, adding, or removing a provider"
        )

    for step in steps:
        step_id = str(step["id"])
        expected = by_id[step_id]
        expected_name = str(expected.get("provider_name") or "")
        submitted_name = _submitted_workflow_provider_name(step)
        if submitted_name != expected_name:
            return None, (
                f"recovery workflow step '{step_id}' selected provider "
                f"'{submitted_name or '<missing>'}', expected '{expected_name}'"
            )
        expected_source = str(expected.get("provider_source") or "")
        submitted_source = str(step.get("skill_source") or "")
        if submitted_source and submitted_source != expected_source:
            return None, (
                f"recovery workflow step '{step_id}' selected source "
                f"'{submitted_source}', expected '{expected_source}'"
            )
        if expected_source and expected_source not in {"native", "omni_runtime"}:
            step.setdefault("skill_source", expected_source)
        identity = (
            expected.get("assessment_identity")
            if isinstance(expected.get("assessment_identity"), dict)
            else {}
        )
        if identity.get("capability"):
            step.setdefault("capability", str(identity["capability"]))
        if identity.get("deliverable_id"):
            step.setdefault("deliverable", str(identity["deliverable_id"]))
    return payload, ""


def _recovery_selected_skill_authority(
    ctx: ExecContext,
    entry: Any,
) -> tuple[dict[str, Any] | None, str]:
    """Return the one exact selected-skill authority on a recovery turn."""

    if str(getattr(ctx, "subtask_id", "") or ""):
        return None, ""
    authority = getattr(ctx, "execution_authority", None)
    payload = (
        authority.to_dict()
        if callable(getattr(authority, "to_dict", None))
        else authority
        if isinstance(authority, dict)
        else {}
    )
    providers = [
        item
        for item in payload.get("provider_authorities") or []
        if isinstance(item, dict)
        and str(item.get("consumer_kind") or "") == "selected_skill"
    ]
    if not providers:
        return None, ""
    provider_name = str(getattr(entry, "name", "") or "")
    provider_source = str(getattr(entry, "source", "") or "")
    matches = [
        item
        for item in providers
        if str(item.get("provider_name") or "") == provider_name
        and str(item.get("provider_source") or "") == provider_source
    ]
    if len(matches) == 1:
        return copy.deepcopy(matches[0]), ""
    if not matches:
        return None, (
            f"recovery selected provider '{provider_name}' from "
            f"'{provider_source}' outside the authorised plan; re-plan first"
        )
    return None, (
        f"recovery provider '{provider_name}' is bound to multiple consumers; "
        "re-plan with an explicit consumer before execution"
    )


def _submitted_workflow_provider_name(step: dict[str, Any]) -> str:
    """Return the authority provider name represented by one tool step."""

    provider_type = str(
        step.get("provider_type") or step.get("provider") or ""
    ).strip().lower()
    capability = str(step.get("capability") or "").strip().lower()
    if provider_type == "native_executor" or capability in {
        "synthesis.final",
        "draft.section",
        "draft.manuscript",
    }:
        return "native_synthesis"
    if provider_type in {"child_task", "subagent", "agent"}:
        return "agent_delegate"
    return str(step.get("skill_name") or step.get("skill") or "").strip()


def _inline_usage_progress(on_tool_event: Any) -> Any:
    """Forward engine usage snapshots to the turn notice channel (status line)."""

    async def _progress(stage: str, pct: float = 0.0, **data: Any) -> None:
        if str(stage) != "usage" or on_tool_event is None:
            return
        result = on_tool_event("notice", {"kind": "usage", **data})
        if inspect.isawaitable(result):
            await result

    return _progress


def _concrete_skill_failed(result: Any) -> bool:
    """Keep concrete denial/failure semantics at the generic wrapper surface."""
    output = tool_event_output(result)
    if not isinstance(output, dict):
        return False
    return (
        is_tool_rejection(output)
        or output.get("contract_violation") is True
        or str(output.get("status") or "").lower()
        in {"error", "failed", "blocked", "cancelled", "timed_out"}
    )


__all__ = ["ToolSurfaceBuilder"]
