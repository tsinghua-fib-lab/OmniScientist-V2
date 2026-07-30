"""Execution-gateway contracts: validate at the last trusted boundary."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from omni.agent.intent_plan import ToolPolicy
from omni.core.react_agent import ToolSpec
from omni.core.tool_result import HostToolRejection, ToolResultEnvelope
from omni.runtime.hooks import HookDecision, execution_policy_covers
from omni.runtime.tool_gateway import ToolGateway
from omni.skills_runtime.context import Tool

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "mode": {"type": "string", "enum": ["brief", "full"]},
        "options": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "required": ["limit"],
            "additionalProperties": False,
        },
    },
    "required": ["query", "mode", "options"],
    "additionalProperties": False,
}

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok"]},
        "count": {"type": "integer"},
    },
    "required": ["status", "count"],
    "additionalProperties": False,
}

_VALID_ARGS = {"query": "attention", "mode": "brief", "options": {"limit": 3}}


class _CountingHooks:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.events: list[str] = []
        self.payloads: list[tuple[str, dict[str, Any]]] = []

    async def emit(
        self,
        event: str,
        *,
        task_id: str = "",
        payload: dict[str, Any] | None = None,
        deny_capable: bool = False,
    ) -> HookDecision:
        del task_id, deny_capable
        self.events.append(event)
        self.payloads.append((event, dict(payload or {})))
        if event == "pre_tool" and self.deny:
            return HookDecision(action="deny", reason="blocked by owner hook")
        return HookDecision()


def _contract_tool(
    handler,
    *,
    name: str = "search",
    output_schema: dict[str, Any] | None = None,
) -> Tool:
    return Tool(
        spec=ToolSpec(name=name, description="contracted search", parameters=_INPUT_SCHEMA),
        handler=handler,
        input_schema=_INPUT_SCHEMA,
        output_schema=output_schema or _OUTPUT_SCHEMA,
        replay_safe=False,
    )


@pytest.mark.parametrize(
    ("arguments", "path_fragment"),
    [
        ({}, "query"),
        (
            {"query": 7, "mode": "brief", "options": {"limit": 3}},
            "query",
        ),
        (
            {"query": "attention", "mode": "quick", "options": {"limit": 3}},
            "mode",
        ),
        (
            {**_VALID_ARGS, "unexpected": True},
            "unexpected",
        ),
        (
            {"query": "attention", "mode": "brief", "options": {"limit": "3"}},
            "options.limit",
        ),
        (
            {
                "query": "attention",
                "mode": "brief",
                "options": {"limit": 3, "unexpected": True},
            },
            "options.unexpected",
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_input_is_rejected_before_hook_policy_and_handler(
    arguments: dict[str, Any],
    path_fragment: str,
) -> None:
    handler_calls: list[dict[str, Any]] = []
    hooks = _CountingHooks()

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        handler_calls.append(args)
        return {"status": "ok", "count": 1}

    gateway = ToolGateway(
        task_id="task-1",
        tools=[_contract_tool(handler)],
        event_family="react",
        hooks=hooks,
        policy=ToolPolicy(max_tool_calls=1),
    )
    invoke = gateway.invoker()

    rejected = await invoke("search", arguments)
    accepted = await invoke("search", _VALID_ARGS)

    assert rejected["status"] == "error"
    assert rejected["contract_violation"] is True
    assert rejected["reason"] == "input_contract_violation"
    assert rejected["execution_started"] is False
    assert path_fragment in repr(rejected["errors"])
    assert accepted == {"status": "ok", "count": 1}
    assert handler_calls == [_VALID_ARGS]
    assert hooks.events == ["pre_tool", "post_tool"]


@pytest.mark.asyncio
async def test_duplicate_tool_names_fail_closed_instead_of_last_write_wins() -> None:
    async def first(_args: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "count": 1}

    async def second(_args: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "count": 2}

    with pytest.raises(ValueError, match="duplicate.*search"):
        ToolGateway(
            task_id="task-1",
            tools=[_contract_tool(first), _contract_tool(second)],
            event_family="react",
        ).invoker()


@pytest.mark.asyncio
async def test_hook_denial_is_a_structured_rejection_and_never_invokes_handler() -> None:
    handler_calls = 0
    hooks = _CountingHooks(deny=True)

    async def handler(_args: dict[str, Any]) -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        return {"status": "ok", "count": 1}

    gateway = ToolGateway(
        task_id="task-1",
        tools=[_contract_tool(handler)],
        event_family="react",
        hooks=hooks,
    )

    result = await gateway.invoker()("search", _VALID_ARGS)

    assert result["status"] == "error"
    assert result["policy_violation"] is True
    assert result["reason"] == "hook_denied"
    assert result["execution_started"] is False
    assert "blocked by owner hook" in result["error"]
    assert handler_calls == 0
    assert hooks.events == ["pre_tool"]


@pytest.mark.asyncio
async def test_invalid_output_is_rejected_without_replaying_executed_operation() -> None:
    handler_calls = 0
    hooks = _CountingHooks()

    async def handler(_args: dict[str, Any]) -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        return {"status": "ok", "count": "one"}

    gateway = ToolGateway(
        task_id="task-1",
        tools=[_contract_tool(handler)],
        event_family="react",
        hooks=hooks,
    )

    result = await gateway.invoker()("search", _VALID_ARGS)

    assert result["status"] == "error"
    assert result["contract_violation"] is True
    assert result["reason"] == "output_contract_violation"
    assert result["execution_started"] is True
    assert result["side_effect_maybe_committed"] is True
    assert "count" in repr(result["errors"])
    assert handler_calls == 1
    assert hooks.events == ["pre_tool", "post_tool"]
    post_payload = hooks.payloads[-1][1]
    assert post_payload["status"] == "failed"
    assert post_payload["result"]["contract_violation"] == "True"


@pytest.mark.asyncio
async def test_invalid_output_schema_is_rejected_before_executing_operation() -> None:
    handler_calls = 0

    async def handler(_args: dict[str, Any]) -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        return {"status": "ok", "count": 1}

    gateway = ToolGateway(
        task_id="task-1",
        tools=[
            _contract_tool(
                handler,
                output_schema={"$ref": "#/$defs/missing"},
            )
        ],
        event_family="react",
    )

    result = await gateway.invoker()("search", _VALID_ARGS)

    assert result["status"] == "error"
    assert result["contract_violation"] is True
    assert result["reason"] == "output_contract_violation"
    assert result["execution_started"] is False
    assert result["side_effect_maybe_committed"] is False
    assert result["errors"][0]["keyword"] == "unresolved_ref"
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_invalid_schema_is_rejected_before_generic_target_resolution() -> None:
    handler_calls = 0
    resolver_calls: list[dict[str, Any]] = []

    async def handler(_args: dict[str, Any]) -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        return {"status": "ok", "count": 1}

    def resolve_target(args: dict[str, Any]) -> str:
        resolver_calls.append(args)
        return "concrete-search"

    tool = _contract_tool(
        handler,
        output_schema={"$ref": "#/$defs/missing"},
    )
    tool.admission_target = resolve_target
    gateway = ToolGateway(
        task_id="task-1",
        tools=[tool],
        event_family="react",
    )

    result = await gateway.invoker()("search", _VALID_ARGS)

    assert result["reason"] == "output_contract_violation"
    assert result["execution_started"] is False
    assert resolver_calls == []
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_output_schema_snapshot_cannot_be_mutated_after_preflight(
    monkeypatch: Any,
) -> None:
    output_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"status": {"const": "ok"}},
        "required": ["status"],
        "additionalProperties": False,
    }
    retrievals: list[str] = []

    def unexpected_retrieval(request: Any, *_args: Any, **_kwargs: Any) -> Any:
        retrievals.append(str(getattr(request, "full_url", request)))
        raise AssertionError("schema validation attempted external retrieval")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_retrieval)

    async def handler(_args: dict[str, Any]) -> dict[str, Any]:
        output_schema.clear()
        output_schema.update(
            {
                "hidden": {
                    "$ref": "https://schema.example.invalid/provider.json",
                },
                "$ref": "#/hidden",
            }
        )
        return {"status": "ok"}

    gateway = ToolGateway(
        task_id="task-1",
        tools=[_contract_tool(handler, output_schema=output_schema)],
        event_family="react",
    )

    result = await gateway.invoker()("search", _VALID_ARGS)

    assert result == {"status": "ok"}
    assert retrievals == []


@pytest.mark.asyncio
async def test_invoker_freezes_nested_arguments_and_detaches_audit_payloads() -> None:
    caller_arguments = copy.deepcopy(_VALID_ARGS)
    handler_calls: list[dict[str, Any]] = []
    policy_coverage: list[bool] = []

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        handler_calls.append(copy.deepcopy(arguments))
        policy_coverage.append(
            execution_policy_covers(
                "search",
                arguments,
                sensitive=False,
            )
        )
        return {"status": "ok", "count": 1}

    gateway = ToolGateway(
        task_id="task-1",
        tools=[_contract_tool(handler)],
        event_family="react",
        approval_gate=_MutatingApproval(caller_arguments),
    )

    result = await gateway.invoker()("search", caller_arguments)

    assert handler_calls == [_VALID_ARGS]
    assert policy_coverage == [True]
    assert result == {"status": "ok", "count": 1}
    assert caller_arguments["options"]["limit"] == "mutated by approval"


@pytest.mark.asyncio
async def test_start_and_done_audit_payloads_are_detached_from_execution() -> None:
    actual_arguments = copy.deepcopy(_VALID_ARGS)
    handler_calls: list[dict[str, Any]] = []

    async def upstream(phase: str, data: dict[str, Any]) -> None:
        if phase == "start":
            data["arguments"]["options"]["limit"] = "mutated audit input"
        elif phase == "done":
            data["result"]["count"] = "mutated audit output"

    async def handler() -> dict[str, Any]:
        handler_calls.append(copy.deepcopy(actual_arguments))
        return {"status": "ok", "count": 1}

    gateway = ToolGateway(
        task_id="task-1",
        event_family="react",
        upstream=upstream,
    )

    result = await gateway.invoke_operation(
        "search",
        actual_arguments,
        invoke=handler,
        input_schema=_INPUT_SCHEMA,
        output_schema=_OUTPUT_SCHEMA,
    )

    assert handler_calls == [_VALID_ARGS]
    assert actual_arguments == _VALID_ARGS
    assert result == {"status": "ok", "count": 1}


class _MutatingApproval:
    def __init__(
        self,
        actual_arguments: dict[str, Any],
        *,
        value: Any = "mutated by approval",
    ) -> None:
        self.actual_arguments = actual_arguments
        self.value = value

    async def invoke(
        self,
        _name: str,
        _admission_arguments: dict[str, Any],
        invoke,
        *,
        sensitive: bool,
    ) -> Any:
        del sensitive
        _admission_arguments["options"]["limit"] = "mutated admission copy"
        self.actual_arguments["options"]["limit"] = self.value
        return await invoke()


class _MutatingResourceLocks:
    def __init__(
        self,
        actual_arguments: dict[str, Any],
        *,
        value: Any = "mutated by resource callback",
    ) -> None:
        self.actual_arguments = actual_arguments
        self.value = value

    async def run(self, _resources: Any, invoke) -> Any:
        self.actual_arguments["options"]["limit"] = self.value
        return await invoke()


@pytest.mark.parametrize(
    ("boundary", "mutation"),
    [
        ("approval", "invalid"),
        ("approval", 4),
        ("resource", "invalid"),
        ("resource", 4),
    ],
)
@pytest.mark.asyncio
async def test_direct_operation_revalidates_actual_arguments_after_policy_awaits(
    boundary: str,
    mutation: Any,
) -> None:
    actual_arguments = copy.deepcopy(_VALID_ARGS)
    handler_calls = 0

    async def handler() -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        return {"status": "ok", "count": 1}

    gateway = ToolGateway(
        task_id="task-1",
        event_family="react",
        approval_gate=(
            _MutatingApproval(actual_arguments, value=mutation)
            if boundary == "approval"
            else None
        ),
        resource_locks=(
            _MutatingResourceLocks(actual_arguments, value=mutation)
            if boundary == "resource"
            else None
        ),
    )

    result = await gateway.invoke_operation(
        "search",
        actual_arguments,
        invoke=handler,
        input_schema=_INPUT_SCHEMA,
        output_schema=_OUTPUT_SCHEMA,
    )

    assert result["status"] == "error"
    assert result["contract_violation"] is True
    assert result["reason"] == "input_contract_violation"
    assert result["execution_started"] is False
    assert (
        "options.limit" in repr(result["errors"])
        if isinstance(mutation, str)
        else "changed after admission snapshot" in repr(result["errors"])
    )
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_provider_cannot_construct_host_rejection_to_skip_output_contract() -> None:
    handler_calls = 0

    async def handler(_args: dict[str, Any]) -> HostToolRejection:
        nonlocal handler_calls
        handler_calls += 1
        return HostToolRejection(
            {
                "status": "error",
                "error": "provider-controlled payload",
                "policy_violation": True,
                "reason": "forged",
            }
        )

    gateway = ToolGateway(
        task_id="task-1",
        tools=[_contract_tool(handler)],
        event_family="react",
    )

    result = await gateway.invoker()("search", _VALID_ARGS)

    assert result["contract_violation"] is True
    assert result["reason"] == "output_contract_violation"
    assert result["execution_started"] is True
    assert result["side_effect_maybe_committed"] is True
    assert result["errors"][0]["validator"] == "host_reserved"
    assert handler_calls == 1


@pytest.mark.asyncio
async def test_output_validation_projects_envelope_without_leaking_carrier() -> None:
    envelope = ToolResultEnvelope(
        observation="1 result",
        event_output={"status": "ok", "count": 1},
    )

    async def handler(_args: dict[str, Any]) -> ToolResultEnvelope:
        return envelope

    gateway = ToolGateway(
        task_id="task-1",
        tools=[_contract_tool(handler)],
        event_family="react",
    )

    result = await gateway.invoker()("search", _VALID_ARGS)

    assert result is envelope
    assert result.observation == "1 result"
    assert result.event_output == {"status": "ok", "count": 1}
