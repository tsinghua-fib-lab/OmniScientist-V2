"""Single permission, hook, locking, policy, and audit boundary for execution."""

from __future__ import annotations

import copy
import inspect
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from omni.core.tool_contracts import (
    prepare_json_schema,
    validate_prepared_json_schema,
)
from omni.core.tool_policy import ToolPolicyGuard, policy_violation
from omni.core.tool_result import (
    ToolCallOutcome,
    attach_tool_outcome,
    is_tool_rejection,
    tool_event_output,
    tool_event_suffix,
    tool_outcome_event_fields,
    tool_rejection_error,
    tool_result_failure,
    tool_transport_status,
)
from omni.runtime.execution_policy import tool_is_mutating
from omni.runtime.hooks import (
    HookDeniedError,
    HookManager,
    claim_execution_policy_contract,
    claim_execution_policy_delegation,
    execution_arguments_hash,
    invoke_tool_with_hooks,
)
from omni.skills_runtime.context import Tool


class ToolGateway:
    """Execute every tool-like operation through the same runtime policy."""

    def __init__(
        self,
        *,
        task_id: str,
        tools: list[Tool] | None = None,
        tasks: Any = None,
        event_family: str,
        upstream: Any = None,
        hooks: HookManager | None = None,
        approval_gate: Any = None,
        resource_locks: Any = None,
        resource_scope: str = "",
        policy: Any = None,
        per_tool_limits: dict[str, int] | None = None,
        origin: dict[str, str] | None = None,
    ) -> None:
        self.task_id = task_id
        self.tools = list(tools or [])
        self._tools_by_name = _index_tools(self.tools)
        self.tasks = tasks
        self.event_family = event_family
        self.upstream = upstream
        self.hooks = hooks
        self.approval_gate = approval_gate
        self.resource_locks = resource_locks
        self.resource_scope = resource_scope
        # Where in a workflow these tool calls are happening, when they are
        # happening in one at all. Empty for a coordinator turn, which has no
        # position and must not claim one.
        self.origin = {key: value for key, value in (origin or {}).items() if value}
        self._policy_guard = (
            ToolPolicyGuard.from_policy(policy)
            if policy is not None
            else ToolPolicyGuard(per_tool_limits=dict(per_tool_limits or {}))
        )

    @classmethod
    def from_context(
        cls,
        ctx: Any,
        *,
        event_family: str,
        tools: list[Tool] | None = None,
        policy: Any = None,
        per_tool_limits: dict[str, int] | None = None,
        upstream: Any = None,
        skill_name: str = "",
    ) -> ToolGateway:
        existing = getattr(ctx, "tool_gateway", None)
        if isinstance(existing, cls) and existing.event_family == event_family:
            return existing
        return cls(
            task_id=str(getattr(ctx, "task_id", "") or ""),
            tools=tools,
            tasks=getattr(ctx, "task_recorder", None),
            event_family=event_family,
            upstream=upstream,
            hooks=getattr(ctx, "hooks", None),
            approval_gate=ctx.approval_gate() if hasattr(ctx, "approval_gate") else None,
            resource_locks=getattr(ctx, "resource_locks", None),
            resource_scope=str(getattr(ctx, "resource_scope", "") or ""),
            policy=policy,
            per_tool_limits=per_tool_limits,
            origin={
                **{
                    key: str(getattr(ctx, key, "") or "")
                    for key in ("workflow_run_id", "workflow_step_id", "subtask_id")
                },
                "skill_name": skill_name,
            },
        )

    @property
    def tool_specs(self):  # noqa: ANN201
        """Every tool this gateway can dispatch — the reachable set.

        Reachability is decided upstream by ``ToolPolicy``: a denied tool is not
        in ``self.tools`` at all and cannot be reached by naming it. What remains
        here stays runnable whether or not the turn advertises it.
        """
        return [tool.spec for tool in self.tools]

    def model_visible_specs(self):  # noqa: ANN201
        """The subset whose schemas this turn pays to send (Codex's naming).

        A deferred tool is absent from here and present in :attr:`tool_specs`,
        which is the whole point: withholding a schema to save per-iteration
        tokens must not withdraw the capability, or a turn can do its work and
        then fail to save it.
        """
        return [tool.spec for tool in self.tools if tool.spec.exposure == "direct"]

    def invoker(self):  # noqa: ANN201
        """Return the compatibility invoker without interpreting result JSON."""
        return self._build_invoker(resolve_owned_outcomes=False)

    def react_invoker(self):  # noqa: ANN201
        """Return the ReAct invoker that carries trusted adapter outcomes."""
        return self._build_invoker(resolve_owned_outcomes=True)

    def _build_invoker(self, *, resolve_owned_outcomes: bool):  # noqa: ANN202
        async def invoke(name: str, args: dict[str, Any]) -> Any:
            tool = self._tools_by_name.get(name)
            if tool is None:
                return policy_violation(name, "unknown_tool")
            sealed_arguments = copy.deepcopy(args)
            return await self.invoke_operation(
                name,
                sealed_arguments,
                invoke=lambda: tool.handler(copy.deepcopy(sealed_arguments)),
                sensitive=tool.sensitive,
                emit_events=False,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                delegated_target_resolver=tool.admission_target,
                outcome_resolver=(
                    tool.outcome_resolver if resolve_owned_outcomes else None
                ),
            )

        return invoke

    async def invoke_operation(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        invoke: Callable[[], Awaitable[Any]],
        sensitive: bool = False,
        emit_events: bool = True,
        input_schema: dict[str, Any] | bool | None = None,
        output_schema: dict[str, Any] | bool | None = None,
        contract: Any = None,
        delegated_target: str = "",
        delegated_target_resolver: Callable[[dict[str, Any]], str] | None = None,
        outcome_resolver: Callable[[Any], ToolCallOutcome | None] | None = None,
    ) -> Any:
        """Invoke an operation using one immutable admission snapshot.

        The zero-argument ``invoke`` callback is retained for compatibility and
        must execute the supplied ``arguments`` value. The gateway rejects any
        drift in that value immediately before provider execution. Hooks and
        approvals may deny or continue; rewriting arguments is not supported.
        """
        return await self._invoke_operation(
            name,
            arguments,
            invoke=invoke,
            sensitive=sensitive,
            emit_events=emit_events,
            input_schema=input_schema,
            output_schema=output_schema,
            contract=contract,
            delegated_target=delegated_target,
            delegated_target_resolver=delegated_target_resolver,
            outcome_resolver=outcome_resolver,
            apply_runtime_policy=True,
            apply_tool_authorization=True,
            apply_tool_budget=True,
            apply_hooks=True,
        )

    async def invoke_contract_operation(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        invoke: Callable[[], Awaitable[Any]],
        sensitive: bool,
        emit_events: bool = False,
        input_schema: dict[str, Any] | bool | None = None,
        output_schema: dict[str, Any] | bool | None = None,
        contract: Any = None,
    ) -> Any:
        """Validate an exact operation already admitted by the active frame.

        Exact coverage means the same concrete name, arguments, and sensitivity
        already passed authorization, hooks, approval, locking, and budget. Only
        its input/output contract remains here; a generic wrapper never qualifies
        as exact coverage for the concrete target it selects.
        """
        if not claim_execution_policy_contract(
            name,
            arguments,
            sensitive=sensitive,
        ):
            raise RuntimeError(
                "contract-only execution requires exact active policy coverage"
            )
        return await self._invoke_operation(
            name,
            arguments,
            invoke=invoke,
            emit_events=emit_events,
            input_schema=input_schema,
            output_schema=output_schema,
            contract=contract,
            delegated_target="",
            delegated_target_resolver=None,
            outcome_resolver=None,
            apply_runtime_policy=False,
            apply_tool_authorization=False,
            apply_tool_budget=False,
            apply_hooks=False,
        )

    async def invoke_nested_operation(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        invoke: Callable[[], Awaitable[Any]],
        sensitive: bool = False,
        emit_events: bool = False,
        input_schema: dict[str, Any] | bool | None = None,
        output_schema: dict[str, Any] | bool | None = None,
        contract: Any = None,
    ) -> Any:
        """Admit a concrete operation selected inside a generic wrapper.

        An outer operation already paid the one logical tool-budget charge, but
        it is not authority for a different concrete target. This path therefore
        rechecks concrete authorization and runs the concrete owner hook,
        approval, and resource lock. Hooks are not duplicates: they name the
        concrete operation rather than the outer transport/composite operation.
        """
        if not claim_execution_policy_delegation(name):
            raise RuntimeError(
                "nested execution requires an active delegated policy frame"
            )
        return await self._invoke_operation(
            name,
            arguments,
            invoke=invoke,
            sensitive=sensitive,
            emit_events=emit_events,
            input_schema=input_schema,
            output_schema=output_schema,
            contract=contract,
            delegated_target="",
            delegated_target_resolver=None,
            outcome_resolver=None,
            apply_runtime_policy=True,
            apply_tool_authorization=True,
            apply_tool_budget=False,
            apply_hooks=True,
        )

    async def _invoke_operation(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        invoke: Callable[[], Awaitable[Any]],
        sensitive: bool = False,
        emit_events: bool = True,
        input_schema: dict[str, Any] | bool | None = None,
        output_schema: dict[str, Any] | bool | None = None,
        contract: Any = None,
        delegated_target: str,
        delegated_target_resolver: Callable[[dict[str, Any]], str] | None,
        outcome_resolver: Callable[[Any], ToolCallOutcome | None] | None,
        apply_runtime_policy: bool,
        apply_tool_authorization: bool,
        apply_tool_budget: bool,
        apply_hooks: bool,
    ) -> Any:
        admitted_arguments = copy.deepcopy(arguments)
        admitted_arguments_hash = execution_arguments_hash(admitted_arguments)
        input_schema_declared: bool | None = None
        output_schema_declared: bool | None = None
        if contract is not None:
            input_schema = getattr(contract, "input_schema", input_schema)
            output_schema = getattr(contract, "output_schema", output_schema)
            input_schema_declared = getattr(contract, "input_schema_declared", None)
            output_schema_declared = getattr(contract, "output_schema_declared", None)
        input_contract = prepare_json_schema(
            input_schema,
            declared=input_schema_declared,
        )
        output_contract = prepare_json_schema(
            output_schema,
            declared=output_schema_declared,
        )
        input_errors = validate_prepared_json_schema(
            admitted_arguments,
            input_contract,
        )
        if input_errors:
            rejected = _contract_violation(
                name,
                "input_contract_violation",
                input_errors,
                execution_started=False,
            )
            if emit_events:
                await self.emit(
                    "done",
                    {
                        "name": name,
                        "arguments": admitted_arguments,
                        "result": rejected,
                        **_terminal_event_fields(rejected),
                    },
                )
            return rejected
        output_schema_errors = output_contract.definition_errors
        if output_schema_errors:
            rejected = _contract_violation(
                name,
                "output_contract_violation",
                output_schema_errors,
                execution_started=False,
            )
            if emit_events:
                await self.emit(
                    "done",
                    {
                        "name": name,
                        "arguments": admitted_arguments,
                        "result": rejected,
                        **_terminal_event_fields(rejected),
                    },
                )
            return rejected
        if delegated_target_resolver is not None:
            delegated_target = str(
                delegated_target_resolver(copy.deepcopy(admitted_arguments)) or ""
            ).strip()
        if apply_tool_authorization:
            targets = [name]
            if delegated_target and delegated_target != name:
                targets.append(delegated_target)
            rejected = next(
                (
                    rejection
                    for target in targets
                    if (
                        rejection
                        := self._policy_guard.authorization_rejection(target)
                    )
                    is not None
                ),
                None,
            )
            if rejected is not None:
                if emit_events:
                    await self.emit(
                        "done",
                        {
                            "name": name,
                            "arguments": admitted_arguments,
                            "result": rejected,
                            **_terminal_event_fields(rejected),
                        },
                    )
                return rejected
        if apply_tool_budget:
            rejected = self._policy_guard.budget_rejection(
                delegated_target or name
            )
            if rejected is not None:
                if emit_events:
                    await self.emit(
                        "done",
                        {
                            "name": name,
                            "arguments": admitted_arguments,
                            "result": rejected,
                            **_terminal_event_fields(rejected),
                        },
                    )
                return rejected
        call_id = uuid.uuid4().hex
        if emit_events:
            persisted = await self.emit(
                "start",
                {
                    "name": name,
                    "arguments": admitted_arguments,
                    "call_id": call_id,
                },
            )
            if persisted is False and tool_is_mutating(name):
                rejected = policy_violation(name, "start_event_not_persisted")
                rejected.update(
                    {
                        "error": (
                            "The tool call was not executed because its start "
                            "event could not be recorded."
                        ),
                        "execution_started": False,
                    }
                )
                await self.emit(
                    "done",
                    {
                        "name": name,
                        "arguments": admitted_arguments,
                        "result": rejected,
                        "call_id": call_id,
                        **_terminal_event_fields(rejected),
                    },
                )
                return rejected

        def validate_execution_arguments() -> Any:
            errors = list(
                validate_prepared_json_schema(arguments, input_contract)
            )
            try:
                current_hash = execution_arguments_hash(arguments)
            except Exception as exc:  # noqa: BLE001 - fail closed on unstable input
                errors.append(
                    {
                        "path": "",
                        "validator": "admission_snapshot",
                        "message": (
                            "arguments could not be compared with the admission "
                            f"snapshot: {type(exc).__name__}: {exc}"
                        ),
                    }
                )
                current_hash = ""
            if current_hash != admitted_arguments_hash:
                errors.append(
                    {
                        "path": "",
                        "validator": "admission_snapshot",
                        "message": "arguments changed after admission snapshot",
                    }
                )
            if not errors:
                return None
            return _contract_violation(
                name,
                "input_contract_violation",
                tuple(errors),
                execution_started=False,
            )

        def finalize_result(result: Any, execution_started: bool) -> Any:
            event_result = tool_event_output(result)
            if is_tool_rejection(event_result):
                return result
            if (
                not execution_started
                and isinstance(event_result, dict)
                and event_result.get("contract_violation") is True
            ):
                return result
            output_errors = (
                *_reserved_host_marker_errors(event_result),
                *validate_prepared_json_schema(event_result, output_contract),
            )
            if not output_errors:
                return attach_tool_outcome(
                    result,
                    outcome_resolver(event_result)
                    if outcome_resolver is not None
                    else None,
                )
            return _contract_violation(
                name,
                "output_contract_violation",
                output_errors,
                execution_started=execution_started,
                side_effect_maybe_committed=execution_started,
            )

        try:
            if delegated_target:
                # The wrapper is a model-facing routing/transport operation.
                # Enter a transport-only frame (no wrapper hook, approval, or
                # lock) so the selected concrete operation takes the nested
                # admission path: concrete hook/approval/lock/contract, no
                # second budget charge or user-facing lifecycle event.
                result = await invoke_tool_with_hooks(
                    None,
                    task_id=self.task_id,
                    tool_name=name,
                    arguments=admitted_arguments,
                    family=self.event_family,
                    invoke=invoke,
                    delegated_target=delegated_target,
                    result_finalizer=finalize_result,
                    pre_invoke_validator=validate_execution_arguments,
                )
            elif apply_runtime_policy:
                result = await invoke_tool_with_hooks(
                    self.hooks if apply_hooks else None,
                    task_id=self.task_id,
                    tool_name=name,
                    arguments=admitted_arguments,
                    family=self.event_family,
                    invoke=invoke,
                    approval_gate=self.approval_gate,
                    resource_locks=self.resource_locks,
                    resource_scope=self.resource_scope,
                    sensitive=sensitive,
                    result_finalizer=finalize_result,
                    pre_invoke_validator=validate_execution_arguments,
                )
            else:
                result = validate_execution_arguments()
                if result is None:
                    result = await invoke()
                    result = finalize_result(result, True)
                else:
                    result = finalize_result(result, False)
        except HookDeniedError as exc:
            rejected = policy_violation(name, "hook_denied")
            rejected.update(
                {
                    "error": f"tool '{name}' was not run: {exc}",
                    "execution_started": False,
                }
            )
            if emit_events:
                await self.emit(
                    "done",
                    {
                        "name": name,
                        "arguments": admitted_arguments,
                        "result": rejected,
                        "call_id": call_id,
                        **_terminal_event_fields(rejected),
                    },
                )
            return rejected
        except Exception as exc:
            if emit_events:
                await self.emit(
                    "done",
                    {
                        "name": name,
                        "arguments": admitted_arguments,
                        "error": f"{type(exc).__name__}: {exc}",
                        "status": "failed",
                        "call_id": call_id,
                    },
                )
            raise
        event_result = tool_event_output(result)
        if emit_events:
            await self.emit(
                "done",
                {
                    "name": name,
                    "arguments": admitted_arguments,
                    "result": event_result,
                    "call_id": call_id,
                    **_terminal_event_fields(result),
                },
            )
        await self._record_observed_exec(name, admitted_arguments, result)
        return result

    async def _record_observed_exec(
        self, name: str, arguments: dict[str, Any], result: Any
    ) -> None:
        """Write an experiment-run row for exec the host already performed."""
        db = getattr(self.tasks, "db", None) or getattr(self.tasks, "_db", None)
        if db is None:
            return
        fields = _terminal_event_fields(result)
        session_id = ""
        if self.task_id and self.tasks is not None and hasattr(self.tasks, "get_task"):
            try:
                task = await self.tasks.get_task(self.task_id)
            except Exception:  # noqa: BLE001 - ROM write is best-effort
                task = None
            if task is not None:
                session_id = str(getattr(task, "session_id", "") or "")
        from omni.research.host_record import record_observed_exec

        await record_observed_exec(
            db,
            tool_name=name,
            command=str(arguments.get("command") or arguments.get("code") or ""),
            status=str(fields.get("status") or ""),
            session_id=session_id,
            subtask_id=str(self.origin.get("subtask_id") or ""),
        )

    async def emit(self, phase: str, data: dict[str, Any]) -> bool:
        data = copy.deepcopy(data)
        if "result" in data:
            data = {**data, "result": tool_event_output(data["result"])}
        if phase == "done":
            error = str(data.get("error") or "")
            data = {
                **data,
                "error": error,
                "status": tool_transport_status(data.get("status"), error),
            }
            data.setdefault(
                "lifecycle_status",
                _lifecycle_from_transport_status(data["status"]),
            )
            data.setdefault(
                "result_success",
                True if data["lifecycle_status"] == "completed" else None,
            )
        if self.upstream is not None:
            emitted = self.upstream(phase, copy.deepcopy(data))
            if inspect.isawaitable(emitted):
                await emitted
        if self.tasks is None:
            return True

        def _landed(row: Any) -> bool:
            if row is not None:
                return True
            return not getattr(self.tasks, "drops_unpersisted_events", False)

        if phase == "notice" and str(data.get("kind") or "") == "context_rollover":
            row = await self.tasks.append_event(
                self.task_id,
                event_type=f"{self.event_family}.context.compacted",
                status="succeeded",
                name="context",
                output_json=copy.deepcopy(data),
                summary=f"{self.event_family} context compacted; continuing same run",
            )
            return _landed(row)
        if phase == "budget":
            status = str(data.get("status") or "warning")
            row = await self.tasks.append_event(
                self.task_id,
                event_type=f"{self.event_family}.budget.{status}",
                status="degraded",
                name="budget",
                output_json=copy.deepcopy(data),
                summary=f"{self.event_family} budget {status}: {data.get('reason') or 'unspecified'}",
            )
            return _landed(row)
        if phase == "transcript":
            row = await self.tasks.append_event(
                self.task_id,
                event_type=f"{self.event_family}.transcript.repaired",
                status="succeeded",
                name="transcript",
                output_json={
                    "repairs": copy.deepcopy(list(data.get("repairs") or []))
                },
                summary=f"{self.event_family} transcript repaired",
            )
            return _landed(row)
        name = str(data.get("name") or "")
        if not name:
            return True
        call_id = str(data.get("call_id") or "")
        input_json = copy.deepcopy(data.get("arguments") or {})
        if call_id:
            input_json["_call_id"] = call_id
        if phase == "start":
            row = await self.tasks.append_event(
                self.task_id,
                event_type=f"{self.event_family}.tool.start",
                status="running",
                name=name,
                tool_name=name,
                **self.origin,
                step_id=call_id or str(self.origin.get("step_id") or ""),
                input_json=input_json,
                summary=f"{self.event_family} tool {name} start",
            )
            return _landed(row)
        if phase == "done":
            error = str(data.get("error") or "")
            result = copy.deepcopy(data.get("result"))
            status = str(data.get("status") or "succeeded")
            event_suffix = tool_event_suffix(status)
            row = await self.tasks.append_event(
                self.task_id,
                event_type=f"{self.event_family}.tool.{event_suffix}",
                status=status,
                name=name,
                tool_name=name,
                **self.origin,
                step_id=call_id or str(self.origin.get("step_id") or ""),
                input_json=input_json,
                output_json=copy.deepcopy(result or {}),
                error=error,
                summary=_result_brief(result, error),
                duration_ms=float(data.get("duration_ms") or 0.0),
                lifecycle_status=str(data.get("lifecycle_status") or ""),
                result_success=data.get("result_success"),
            )
            return _landed(row)
        return True


__all__ = ["ToolGateway"]


def _terminal_event_fields(result: Any) -> dict[str, Any]:
    """Build one consistent lifecycle payload for non-raising failures."""
    if is_tool_rejection(result):
        return {
            "status": "rejected",
            "error": tool_rejection_error(result),
            "lifecycle_status": "blocked",
            "result_success": None,
        }
    output = tool_event_output(result)
    if isinstance(output, dict) and output.get("contract_violation") is True:
        execution_started = output.get("execution_started") is True
        return {
            "status": "failed" if execution_started else "rejected",
            "error": str(output.get("error") or "tool contract validation failed"),
            "lifecycle_status": "failed" if execution_started else "blocked",
            "result_success": None,
        }
    if failure := tool_result_failure(result):
        status, error = failure
        return {
            "status": status,
            "error": error,
            **tool_outcome_event_fields(result),
        }
    return {
        "status": "succeeded",
        "error": "",
        **tool_outcome_event_fields(result),
    }


def _lifecycle_from_transport_status(status: str) -> str:
    return {
        "succeeded": "completed",
        "failed": "failed",
        "rejected": "blocked",
        "cancelled": "aborted",
        "timed_out": "timed_out",
    }.get(status, "completed")


def _contract_violation(
    name: str,
    reason: str,
    errors: tuple[dict[str, Any], ...],
    *,
    execution_started: bool,
    side_effect_maybe_committed: bool = False,
) -> dict[str, Any]:
    phase = (
        "output"
        if reason.startswith("output_")
        else "input"
    )
    return {
        "status": "error",
        "error": f"tool '{name}' {phase} failed contract validation",
        "contract_violation": True,
        "tool_name": name,
        "reason": reason,
        "errors": [dict(error) for error in errors],
        "execution_started": execution_started,
        **(
            {"side_effect_maybe_committed": side_effect_maybe_committed}
            if phase == "output"
            else {}
        ),
    }


def _reserved_host_marker_errors(value: Any) -> tuple[dict[str, Any], ...]:
    """Reject provider attempts to impersonate host admission outcomes."""
    if not isinstance(value, dict):
        return ()
    forged = [
        key
        for key in ("approval_required", "policy_violation")
        if value.get(key) is True
    ]
    if not forged:
        return ()
    return (
        {
            "path": "",
            "validator": "host_reserved",
            "message": (
                "provider output contains host-reserved rejection marker(s): "
                + ", ".join(forged)
            ),
        },
    )


def _index_tools(tools: list[Tool]) -> dict[str, Tool]:
    indexed: dict[str, Tool] = {}
    for tool in tools:
        name = tool.spec.name
        if name in indexed:
            raise ValueError(f"duplicate tool name '{name}'")
        indexed[name] = tool
    return indexed


def _result_brief(result: Any, error: str = "", limit: int = 70) -> str:
    if error:
        return f"failed: {error[:limit]}"
    if isinstance(result, dict):
        for key in ("summary", "title", "text", "abstract", "message"):
            if result.get(key):
                return str(result[key])[:limit]
        if result.get("status"):
            return f"status={result['status']}"
    if result not in (None, ""):
        return str(result)[:limit]
    return "done"
