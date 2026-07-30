"""Single permission, hook, locking, policy, and audit boundary for execution."""

from __future__ import annotations

import copy
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from omni.core.tool_contracts import (
    prepare_json_schema,
    validate_prepared_json_schema,
)
from omni.core.tool_policy import ToolPolicyGuard, policy_violation
from omni.core.tool_result import (
    is_tool_rejection,
    tool_event_output,
    tool_event_suffix,
    tool_rejection_error,
    tool_result_failure,
    tool_transport_status,
)
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
        )

    @property
    def tool_specs(self):  # noqa: ANN201
        return [tool.spec for tool in self.tools]

    def invoker(self):  # noqa: ANN201
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
        if emit_events:
            await self.emit(
                "start",
                {"name": name, "arguments": admitted_arguments},
            )

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
                return result
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
                    **_terminal_event_fields(event_result),
                },
            )
        return result

    async def emit(self, phase: str, data: dict[str, Any]) -> None:
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
        if self.upstream is not None:
            emitted = self.upstream(phase, copy.deepcopy(data))
            if inspect.isawaitable(emitted):
                await emitted
        if self.tasks is None:
            return
        if phase == "budget":
            status = str(data.get("status") or "warning")
            await self.tasks.append_event(
                self.task_id,
                event_type=f"{self.event_family}.budget.{status}",
                status="degraded",
                name="budget",
                output_json=copy.deepcopy(data),
                summary=f"{self.event_family} budget {status}: {data.get('reason') or 'unspecified'}",
            )
            return
        if phase == "transcript":
            await self.tasks.append_event(
                self.task_id,
                event_type=f"{self.event_family}.transcript.repaired",
                status="succeeded",
                name="transcript",
                output_json={
                    "repairs": copy.deepcopy(list(data.get("repairs") or []))
                },
                summary=f"{self.event_family} transcript repaired",
            )
            return
        name = str(data.get("name") or "")
        if not name:
            return
        if phase == "start":
            await self.tasks.append_event(
                self.task_id,
                event_type=f"{self.event_family}.tool.start",
                status="running",
                name=name,
                tool_name=name,
                input_json=copy.deepcopy(data.get("arguments") or {}),
                summary=f"{self.event_family} tool {name} start",
            )
            return
        if phase == "done":
            error = str(data.get("error") or "")
            result = copy.deepcopy(data.get("result"))
            status = str(data.get("status") or "succeeded")
            event_suffix = tool_event_suffix(status)
            await self.tasks.append_event(
                self.task_id,
                event_type=f"{self.event_family}.tool.{event_suffix}",
                status=status,
                name=name,
                tool_name=name,
                input_json={
                    **copy.deepcopy(data.get("arguments") or {}),
                    **({"_call_id": data.get("call_id")} if data.get("call_id") else {}),
                },
                output_json=copy.deepcopy(result or {}),
                error=error,
                summary=_result_brief(result, error),
                duration_ms=float(data.get("duration_ms") or 0.0),
            )


__all__ = ["ToolGateway"]


def _terminal_event_fields(result: Any) -> dict[str, str]:
    """Build one consistent lifecycle payload for non-raising failures."""
    if is_tool_rejection(result):
        return {
            "status": "rejected",
            "error": tool_rejection_error(result),
        }
    output = tool_event_output(result)
    if isinstance(output, dict) and output.get("contract_violation") is True:
        execution_started = output.get("execution_started") is True
        return {
            "status": "failed" if execution_started else "rejected",
            "error": str(output.get("error") or "tool contract validation failed"),
        }
    if failure := tool_result_failure(result):
        status, error = failure
        return {"status": status, "error": error}
    return {}


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
