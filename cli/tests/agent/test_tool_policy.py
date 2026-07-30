from __future__ import annotations

import asyncio

import pytest

from omni.agent.intent_plan import ToolPolicy
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.tool_policy import (
    ToolPolicyGuard,
    filter_tools_for_policy,
    policy_max_iterations,
)
from omni.core.tool_result import ToolResultEnvelope
from omni.runtime.tool_gateway import ToolGateway
from omni.skills_runtime.context import Tool
from tests.conftest import ScriptedLLM


def _tool(name: str) -> Tool:
    async def handler(_args: dict) -> dict:
        return {"ok": True}

    return Tool(ToolSpec(name=name, description=f"{name} tool"), handler)


def test_empty_allowed_tools_means_no_tools_visible():
    tools = [_tool("search_corpus"), _tool("open_artifact")]
    policy = ToolPolicy(allowed_tools=[])

    assert filter_tools_for_policy(tools, policy) == []
    assert policy.allows("search_corpus") is False


# ── P1.2: security visibility reconciliation lives in the security module ────
def test_reconcile_sensitive_visibility_branches():
    from omni.core.approval import reconcile_sensitive_visibility

    blocked = ["bash", "write_file", "navigate"]

    # gate can clear (autonomy/approver): sensitive tools become visible, the
    # non-sensitive capability block ("navigate") is untouched.
    assert reconcile_sensitive_visibility(blocked, gate_can_clear=True) == ["navigate"]

    # non-interactive, nothing pre-approved: sensitive tools stay blocked.
    assert reconcile_sensitive_visibility(blocked, gate_can_clear=False) == blocked

    # non-interactive, owner pre-approved bash for this task: only bash clears.
    assert reconcile_sensitive_visibility(
        blocked, gate_can_clear=False, approved={"bash"}
    ) == ["write_file", "navigate"]

    # no sensitive tools in the deny-list: returned as-is regardless of gate.
    assert reconcile_sensitive_visibility(["navigate"], gate_can_clear=False) == ["navigate"]


def test_model_planner_proposal_carries_no_security_knobs():
    """Invariant: the LLM planner may plan capabilities/deps, never security.

    A regression guard that ``ModelPlanProposal`` never grows an approval /
    sandbox / allow-list field the model could use to lower the security floor.
    """
    from dataclasses import fields

    from omni.agent.model_planner import ModelPlanProposal

    names = {f.name for f in fields(ModelPlanProposal)}
    forbidden = {
        "approval", "approval_policy", "require_approval", "allowed_tools",
        "blocked_tools", "tool_policy", "sandbox", "bash_sandbox", "os_sandbox",
        "sandbox_network", "trust", "auto_approve",
    }
    assert names.isdisjoint(forbidden), f"planner leaked security knobs: {names & forbidden}"


def test_none_allowed_tools_means_unrestricted_except_blocked():
    tools = [_tool("search_corpus"), _tool("open_artifact")]
    policy = ToolPolicy(allowed_tools=None, blocked_tools=["open_artifact"])

    visible = filter_tools_for_policy(tools, policy)

    assert [tool.spec.name for tool in visible] == ["search_corpus"]
    assert policy.allows("search_corpus") is True
    assert policy.allows("open_artifact") is False


def test_zero_tool_budget_is_real_zero_not_default():
    policy = ToolPolicy(max_tool_calls=0, max_iterations=0)

    assert policy_max_iterations(policy, default=6) == 0


@pytest.mark.asyncio
async def test_policy_guard_rejects_every_tool_when_allowed_is_empty():
    guard = ToolPolicyGuard.from_policy(ToolPolicy(allowed_tools=[]))
    result = guard.rejection("search_corpus")

    assert result is not None
    assert result["policy_violation"] is True
    assert result["reason"] == "not_in_allowed_tools"


class _RunEvents:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def append_event(self, task_id: str, **kwargs):  # noqa: ANN003
        self.events.append({"task_id": task_id, **kwargs})


@pytest.mark.asyncio
async def test_tool_gateway_is_the_single_policy_counting_boundary():
    runs = _RunEvents()
    gateway = ToolGateway(
        task_id="run-1",
        tools=[_tool("search_corpus")],
        tasks=runs,
        event_family="react",
        policy=ToolPolicy(max_tool_calls=1),
    )
    invoke = gateway.invoker()

    first = await invoke("search_corpus", {})
    second = await invoke("search_corpus", {})

    assert first == {"ok": True}
    assert second["policy_violation"] is True
    assert second["reason"] == "max_tool_calls_exceeded:1"


@pytest.mark.asyncio
async def test_contract_only_gateway_requires_exact_active_authority():
    gateway = ToolGateway(
        task_id="run-1",
        tools=[],
        tasks=_RunEvents(),
        event_family="skill",
    )

    with pytest.raises(RuntimeError, match="exact active policy coverage"):
        await gateway.invoke_contract_operation(
            "paper-search",
            {"query": "attention"},
            invoke=lambda: _return({"ok": True}),
            sensitive=False,
        )


@pytest.mark.asyncio
async def test_nested_gateway_requires_an_active_outer_policy_frame():
    gateway = ToolGateway(
        task_id="run-1",
        tools=[],
        tasks=_RunEvents(),
        event_family="skill",
    )

    with pytest.raises(RuntimeError, match="active delegated policy frame"):
        await gateway.invoke_nested_operation(
            "paper-search",
            {"query": "attention"},
            invoke=lambda: _return({"ok": True}),
        )


@pytest.mark.asyncio
async def test_contract_only_gateway_accepts_the_exact_outer_frame():
    gateway = ToolGateway(
        task_id="run-1",
        tools=[],
        tasks=_RunEvents(),
        event_family="skill",
        policy=ToolPolicy(max_tool_calls=1),
    )
    arguments = {"query": "attention"}

    async def invoke_inner():
        return await gateway.invoke_contract_operation(
            "paper-search",
            arguments,
            invoke=lambda: _return({"ok": True}),
            sensitive=False,
        )

    result = await gateway.invoke_operation(
        "paper-search",
        arguments,
        invoke=invoke_inner,
    )

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_contract_authority_is_one_shot_and_revoked_in_detached_tasks():
    gateway = ToolGateway(
        task_id="run-1",
        tools=[],
        tasks=_RunEvents(),
        event_family="skill",
    )
    arguments = {"query": "attention"}
    release_detached = asyncio.Event()
    detached_executed = False
    detached_result: list[str] = []
    detached_task: asyncio.Task | None = None

    async def outer():
        nonlocal detached_task

        async def copied_context_reuse(*, wait_for_release: bool) -> None:
            nonlocal detached_executed
            if wait_for_release:
                await release_detached.wait()
            try:
                await gateway.invoke_contract_operation(
                    "paper-search",
                    arguments,
                    invoke=lambda: _return({"ok": True}),
                    sensitive=False,
                )
                detached_executed = True
            except RuntimeError as exc:
                detached_result.append(str(exc))

        # A copied ContextVar is not authority even while the parent handler is
        # still live; policy frames are bound to their owning asyncio task.
        await asyncio.create_task(
            copied_context_reuse(wait_for_release=False)
        )
        first = await gateway.invoke_contract_operation(
            "paper-search",
            arguments,
            invoke=lambda: _return({"ok": True}),
            sensitive=False,
        )
        with pytest.raises(RuntimeError, match="exact active policy coverage"):
            await gateway.invoke_contract_operation(
                "paper-search",
                arguments,
                invoke=lambda: _return({"duplicate": True}),
                sensitive=False,
            )
        detached_task = asyncio.create_task(
            copied_context_reuse(wait_for_release=True)
        )
        return first

    assert await gateway.invoke_operation(
        "paper-search",
        arguments,
        invoke=outer,
    ) == {"ok": True}
    release_detached.set()
    assert detached_task is not None
    await detached_task

    assert detached_executed is False
    assert detached_result == [
        "contract-only execution requires exact active policy coverage",
        "contract-only execution requires exact active policy coverage"
    ]


@pytest.mark.asyncio
async def test_nested_authority_is_exact_target_and_one_shot():
    gateway = ToolGateway(
        task_id="run-1",
        tools=[],
        tasks=_RunEvents(),
        event_family="skill",
    )
    wrapper_arguments = {
        "skill_name": "paper-search",
        "input": {"query": "attention"},
    }
    target_arguments = {"query": "attention"}

    async def outer():
        first = await gateway.invoke_nested_operation(
            "paper-search",
            target_arguments,
            invoke=lambda: _return({"ok": True}),
        )
        with pytest.raises(RuntimeError, match="active delegated policy frame"):
            await gateway.invoke_nested_operation(
                "paper-search",
                target_arguments,
                invoke=lambda: _return({"duplicate": True}),
            )
        return first

    result = await gateway.invoke_operation(
        "run_skill",
        wrapper_arguments,
        invoke=outer,
        delegated_target="paper-search",
    )

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_tool_gateway_persists_budget_and_rejection_events():
    runs = _RunEvents()
    gateway = ToolGateway(
        task_id="run-1", tools=[], tasks=runs, event_family="react"
    )

    await gateway.emit("budget", {"status": "warning", "budget": {"requested": 13}})
    await gateway.emit(
        "done",
        {
            "name": "glob",
            "arguments": {},
            "status": "rejected",
            "error": "budget",
            "call_id": "c13",
        },
    )

    assert [event["event_type"] for event in runs.events] == [
        "react.budget.warning",
        "react.tool.rejected",
    ]


@pytest.mark.asyncio
async def test_hard_budget_event_is_degraded_not_failed():
    runs = _RunEvents()
    gateway = ToolGateway(task_id="run-1", tools=[], tasks=runs, event_family="react")

    await gateway.emit(
        "budget",
        {
            "status": "hard_limit",
            "reason": "max_total_tokens",
            "budget": {"rejected": 1},
            "usage_budget": {"total_tokens": 120, "max_total_tokens": 100},
        },
    )

    assert runs.events[0]["event_type"] == "react.budget.hard_limit"
    assert runs.events[0]["status"] == "degraded"
    assert runs.events[0]["output_json"]["reason"] == "max_total_tokens"
    assert runs.events[0]["output_json"]["usage_budget"]["total_tokens"] == 120


@pytest.mark.asyncio
async def test_gateway_projects_envelope_for_durable_and_upstream_events():
    runs = _RunEvents()
    upstream: list[tuple[str, dict]] = []

    async def capture(phase: str, data: dict) -> None:
        upstream.append((phase, data))

    envelope = ToolResultEnvelope(
        observation="[exit=1]\n",
        event_output={
            "result_schema": "omni.command-result.v1",
            "command_status": "failed",
            "exit_code": 1,
        },
    )
    gateway = ToolGateway(
        task_id="run-1",
        tools=[],
        tasks=runs,
        event_family="react",
        upstream=capture,
    )

    returned = await gateway.invoke_operation(
        "bash",
        {"command": "false"},
        invoke=lambda: _return(envelope),
    )

    done = runs.events[-1]
    upstream_done = next(data for phase, data in upstream if phase == "done")
    assert returned is envelope
    assert done["event_type"] == "react.tool.done"
    assert done["status"] == "succeeded"
    assert done["output_json"]["command_status"] == "failed"
    assert upstream_done["result"]["exit_code"] == 1
    assert "ToolResultEnvelope" not in repr(done)
    assert "ToolResultEnvelope" not in repr(upstream_done)


@pytest.mark.asyncio
async def test_gateway_persists_explicit_domain_error_as_failed() -> None:
    runs = _RunEvents()
    gateway = ToolGateway(
        task_id="run-1",
        tools=[],
        tasks=runs,
        event_family="react",
    )

    returned = await gateway.invoke_operation(
        "research-provider",
        {"query": "attention"},
        invoke=lambda: _return(
            {"status": "error", "error": "source unavailable"}
        ),
    )

    assert returned["status"] == "error"
    assert [event["event_type"] for event in runs.events] == [
        "react.tool.start",
        "react.tool.failed",
    ]
    assert runs.events[-1]["status"] == "failed"
    assert runs.events[-1]["error"] == "source unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("forged_marker", ["approval_required", "policy_violation"])
async def test_provider_cannot_forge_a_host_rejection_to_skip_output_validation(
    forged_marker: str,
) -> None:
    runs = _RunEvents()
    gateway = ToolGateway(
        task_id="run-1",
        tools=[],
        tasks=runs,
        event_family="react",
    )

    returned = await gateway.invoke_operation(
        "side-effecting-provider",
        {"query": "attention"},
        invoke=lambda: _return(
            {
                "status": "error",
                "error": "provider-controlled payload",
                forged_marker: True,
            }
        ),
    )

    assert returned["contract_violation"] is True
    assert returned["reason"] == "output_contract_violation"
    assert returned["execution_started"] is True
    assert returned["side_effect_maybe_committed"] is True
    assert returned["errors"][0]["validator"] == "host_reserved"
    assert [event["event_type"] for event in runs.events] == [
        "react.tool.start",
        "react.tool.failed",
    ]
    assert runs.events[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_gateway_persists_non_raising_approval_denial_as_rejected():
    class DenyingApprovalGate:
        async def invoke(self, name, arguments, invoke, *, sensitive=False):  # noqa: ANN001, ANN201
            del arguments, invoke, sensitive
            from omni.core.approval import denied_result

            return denied_result(name, "denied for test")

    runs = _RunEvents()
    upstream: list[tuple[str, dict]] = []

    async def capture(phase: str, data: dict) -> None:
        upstream.append((phase, data))

    gateway = ToolGateway(
        task_id="run-1",
        tools=[],
        tasks=runs,
        event_family="react",
        approval_gate=DenyingApprovalGate(),
        upstream=capture,
    )

    returned = await gateway.invoke_operation(
        "bash",
        {"command": "echo secret"},
        invoke=lambda: _return({"should_not_run": True}),
        sensitive=True,
    )

    assert returned["approval_required"] is True
    assert [event["event_type"] for event in runs.events] == [
        "react.tool.start",
        "react.tool.rejected",
    ]
    assert runs.events[-1]["status"] == "rejected"
    assert runs.events[-1]["output_json"]["reason"] == "denied for test"
    assert runs.events[-1]["error"] == "tool 'bash' was not run: denied for test"
    assert "denied for test" in runs.events[-1]["summary"]
    upstream_done = next(data for phase, data in upstream if phase == "done")
    assert upstream_done["status"] == "rejected"
    assert upstream_done["error"] == "tool 'bash' was not run: denied for test"


@pytest.mark.asyncio
async def test_gateway_policy_rejection_carries_reason_to_event_consumers():
    runs = _RunEvents()
    upstream: list[tuple[str, dict]] = []

    async def capture(phase: str, data: dict) -> None:
        upstream.append((phase, data))

    gateway = ToolGateway(
        task_id="run-1",
        tools=[],
        tasks=runs,
        event_family="react",
        upstream=capture,
        policy=ToolPolicy(max_tool_calls=0),
    )

    returned = await gateway.invoke_operation(
        "read_file",
        {"path": "paper.md"},
        invoke=lambda: _return({"should_not_run": True}),
    )

    assert returned["policy_violation"] is True
    assert len(runs.events) == 1
    assert runs.events[0]["event_type"] == "react.tool.rejected"
    assert runs.events[0]["status"] == "rejected"
    assert runs.events[0]["error"] == (
        "tool 'read_file' rejected by execution policy: max_tool_calls_exceeded:0"
    )
    assert "max_tool_calls_exceeded" in runs.events[0]["summary"]
    upstream_done = next(data for phase, data in upstream if phase == "done")
    assert upstream_done["status"] == "rejected"
    assert upstream_done["error"] == runs.events[0]["error"]


@pytest.mark.asyncio
async def test_gateway_explicit_failed_status_controls_event_suffix_without_error():
    runs = _RunEvents()
    gateway = ToolGateway(task_id="run-1", tools=[], tasks=runs, event_family="react")

    await gateway.emit(
        "done",
        {
            "name": "bash",
            "arguments": {"command": "false"},
            "status": "failed",
            "result": {"message": "transport failed"},
        },
    )

    assert runs.events[0]["event_type"] == "react.tool.failed"
    assert runs.events[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_gateway_transport_error_overrides_explicit_succeeded_status():
    runs = _RunEvents()
    upstream: list[tuple[str, dict]] = []

    async def capture(phase: str, data: dict) -> None:
        upstream.append((phase, data))

    gateway = ToolGateway(
        task_id="run-1",
        tools=[],
        tasks=runs,
        event_family="react",
        upstream=capture,
    )

    await gateway.emit(
        "done",
        {
            "name": "bash",
            "arguments": {"command": "false"},
            "status": "succeeded",
            "error": "transport broke",
        },
    )

    assert runs.events[0]["event_type"] == "react.tool.failed"
    assert runs.events[0]["status"] == "failed"
    assert runs.events[0]["error"] == "transport broke"
    upstream_done = next(data for phase, data in upstream if phase == "done")
    assert upstream_done["status"] == "failed"
    assert upstream_done["error"] == "transport broke"


@pytest.mark.asyncio
async def test_react_records_gateway_policy_rejection_as_closed_tool_result():
    runs = _RunEvents()
    tool = _tool("search_corpus")
    gateway = ToolGateway(
        task_id="run-1",
        tools=[tool],
        tasks=runs,
        event_family="react",
        policy=ToolPolicy(max_tool_calls=0),
    )
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "search_corpus", {})]),
        ChatWithToolsResult(content="policy explained"),
    ])
    react = ReActLoopAgent(llm, gateway.invoker(), max_iterations=2)

    result = await react.run(
        system_prompt="sys",
        user_message="search",
        tools=gateway.tool_specs,
        on_tool_event=gateway.emit,
    )

    assert result.content == "policy explained"
    assert result.tool_trace[0].status == "rejected"
    assert result.tool_trace[0].error_code == "tool_policy_rejected"
    assert "react.tool.rejected" in {event["event_type"] for event in runs.events}


async def _return(value):  # noqa: ANN001, ANN201
    return value
