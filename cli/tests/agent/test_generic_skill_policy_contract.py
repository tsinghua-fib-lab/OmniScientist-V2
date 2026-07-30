"""Generic skill wrappers cannot weaken concrete approval or contract policy."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from omni.agent.intent_plan import ToolPolicy
from omni.agent.tool_surface import ToolSurfaceBuilder
from omni.config import load_settings
from omni.core.approval import ApprovalDecision, ApprovalGate
from omni.runtime.hooks import HookDecision
from omni.runtime.tool_gateway import ToolGateway
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.manifest import EngineSpec, SkillEntry, SkillKind


class _SensitiveEngine:
    calls = 0
    malformed = False

    async def execute(self, **_kwargs: Any) -> dict[str, Any]:
        type(self).calls += 1
        return {
            "status": "ok",
            "count": "wrong" if type(self).malformed else 1,
        }


class _Registry:
    def __init__(self, entry: SkillEntry) -> None:
        self.entry = entry

    def list_sync_tools(self) -> list[SkillEntry]:
        return [self.entry]

    def list_selectable(self) -> list[SkillEntry]:
        return [self.entry]

    def get(self, name: str) -> SkillEntry | None:
        return self.entry if name == self.entry.name else None


class _Hooks:
    def __init__(self, *, deny_tool: str = "") -> None:
        self.events: list[tuple[str, str]] = []
        self.deny_tool = deny_tool

    async def emit(self, event: str, **kwargs: Any) -> HookDecision:
        payload = kwargs.get("payload") or {}
        tool_name = str(payload.get("tool_name") or "")
        self.events.append((event, tool_name))
        if event == "pre_tool" and tool_name == self.deny_tool:
            return HookDecision(action="deny", reason="fixture denied concrete target")
        return HookDecision()


class _Events:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append_event(self, task_id: str, **kwargs: Any) -> None:
        self.events.append({"task_id": task_id, **kwargs})


class _WorkflowRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue_workflow(
        self,
        goal: str,
        steps: list[dict[str, Any]],
        notify_channel: str,
        **kwargs: Any,
    ) -> str:
        self.calls.append(
            {
                "goal": goal,
                "steps": steps,
                "notify_channel": notify_channel,
                **kwargs,
            }
        )
        return "workflow-run-1"


class _SkillRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue(
        self,
        skill_name: str,
        params: dict[str, Any],
        notify_channel: str,
        **kwargs: Any,
    ) -> str:
        self.calls.append(
            {
                "skill_name": skill_name,
                "params": params,
                "notify_channel": notify_channel,
                **kwargs,
            }
        )
        return "subtask-1"

    async def process(self, _subtask_id: str, **_kwargs: Any) -> None:
        return None

    async def get_subtask(self, _subtask_id: str) -> Any:
        return SimpleNamespace(
            status="succeeded",
            result_json={"status": "ok", "count": 1},
            error="",
        )


def _entry() -> SkillEntry:
    module = types.ModuleType("sensitive_policy_engine")
    module.SensitiveEngine = _SensitiveEngine
    sys.modules[module.__name__] = module
    return SkillEntry(
        name="sensitive-skill",
        description="sensitive fixture",
        kind=SkillKind.PYTHON_ENGINE,
        engine=EngineSpec(
            module=module.__name__,
            class_name="SensitiveEngine",
        ),
        execution={"requires_approval": True},
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {"const": "ok"},
                "count": {"type": "integer"},
            },
            "required": ["status", "count"],
            "additionalProperties": False,
        },
    )


async def _surface(
    tmp_path,
    *,
    approver=None,  # noqa: ANN001
    policy: ToolPolicy | None = None,
    deny_hook_tool: str = "",
) -> tuple[ToolGateway, _Hooks, _Events]:
    settings = load_settings(cwd=tmp_path)
    entry = _entry()
    hooks = _Hooks(deny_tool=deny_hook_tool)
    events = _Events()
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        task_id="task-1",
        session_id="session-1",
        hooks=hooks,
        task_recorder=events,
        approval_gate_factory=lambda _task, channel, _session, sensitive: ApprovalGate(
            settings,
            channel=channel,
            approver=approver,
            additional_sensitive_tools=sensitive,
        ),
    )

    async def no_mcp(_ctx: ExecContext) -> list:
        return []

    builder = ToolSurfaceBuilder(
        runtime=SimpleNamespace(),
        tasks=SimpleNamespace(),
        registry=_Registry(entry),
        mcp_loader=no_mcp,
    )
    tools = await builder.build(ctx, wait_for_tasks=True)
    return (
        ToolGateway.from_context(
            ctx,
            event_family="react",
            tools=tools,
            policy=policy,
        ),
        hooks,
        events,
    )


@pytest.mark.parametrize("route", ["sensitive-skill", "run_skill", "use_skill"])
@pytest.mark.asyncio
async def test_sensitive_skill_requires_concrete_approval_on_every_route(
    tmp_path,
    route: str,
) -> None:
    _SensitiveEngine.calls = 0
    _SensitiveEngine.malformed = False
    gateway, _hooks, _events = await _surface(tmp_path)
    arguments = (
        {"query": "attention"}
        if route == "sensitive-skill"
        else {
            "skill_name": "sensitive-skill",
            "input": {"query": "attention"},
            **({"mode": "inline"} if route == "run_skill" else {}),
        }
    )

    result = await gateway.invoker()(route, arguments)

    assert result["approval_required"] is True
    assert result["tool_name"] == "sensitive-skill"
    assert _SensitiveEngine.calls == 0


@pytest.mark.asyncio
async def test_generic_wrapper_approves_concrete_skill_once_without_double_hooks(
    tmp_path,
) -> None:
    _SensitiveEngine.calls = 0
    _SensitiveEngine.malformed = False
    requests = []

    async def approver(request):  # noqa: ANN001, ANN202
        requests.append(request)
        return ApprovalDecision(True, scope="once")

    gateway, hooks, events = await _surface(tmp_path, approver=approver)

    result = await gateway.invoker()(
        "run_skill",
        {
            "skill_name": "sensitive-skill",
            "mode": "inline",
            "input": {"query": "attention"},
        },
    )

    assert result["status"] == "succeeded"
    assert result["result"]["count"] == 1
    assert _SensitiveEngine.calls == 1
    assert [(item.tool_name, item.arguments) for item in requests] == [
        ("sensitive-skill", {"query": "attention"})
    ]
    assert hooks.events == [
        ("pre_tool", "sensitive-skill"),
        ("post_tool", "sensitive-skill"),
    ]
    # ReAct owns the single user-facing ``run_skill`` lifecycle. The concrete
    # admission is security/audit metadata, not a second nested tool lifecycle.
    assert events.events == []


@pytest.mark.asyncio
async def test_recovery_run_workflow_reuses_exact_step_authority(tmp_path) -> None:
    settings = load_settings(cwd=tmp_path)
    runtime = _WorkflowRuntime()
    authority = {
        "fingerprint": "accepted-plan",
        "provider_authorities": [
            {
                "consumer_kind": "workflow_step",
                "consumer_id": "figure",
                "provider_name": "scientific-figure",
                "provider_source": "builtin",
                "assessment_identity": {
                    "capability": "artifact.figure",
                    "deliverable_id": "figure",
                },
            },
            {
                "consumer_kind": "react_turn",
                "consumer_id": "react",
                "provider_name": "react_delegate",
                "provider_source": "omni_runtime",
            },
        ],
    }
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        task_id="task-1",
        session_id="session-1",
        execution_authority=authority,
    )
    builder = ToolSurfaceBuilder(
        runtime=runtime,
        tasks=SimpleNamespace(),
        registry=SimpleNamespace(),
        mcp_loader=lambda _ctx: [],
    )
    tool = builder._run_workflow(  # noqa: SLF001
        ctx,
        wait_for_tasks=False,
        on_tool_event=None,
    )

    result = await tool.handler(
        {
            "goal": "repair the original objective inputs",
            "mode": "background",
            "steps": [
                {
                    "id": "figure",
                    "skill": "scientific-figure",
                    "input": {"input": "RAG architecture"},
                }
            ],
        }
    )

    assert result["status"] == "submitted"
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert call["execution_authority"] == authority
    assert call["steps"][0]["skill_source"] == "builtin"
    assert call["steps"][0]["capability"] == "artifact.figure"
    assert call["steps"][0]["deliverable"] == "figure"


@pytest.mark.asyncio
async def test_recovery_run_workflow_rejects_provider_replacement(tmp_path) -> None:
    settings = load_settings(cwd=tmp_path)
    runtime = _WorkflowRuntime()
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        task_id="task-1",
        session_id="session-1",
        execution_authority={
            "provider_authorities": [
                {
                    "consumer_kind": "workflow_step",
                    "consumer_id": "figure",
                    "provider_name": "scientific-figure",
                    "provider_source": "builtin",
                }
            ]
        },
    )
    builder = ToolSurfaceBuilder(
        runtime=runtime,
        tasks=SimpleNamespace(),
        registry=SimpleNamespace(),
        mcp_loader=lambda _ctx: [],
    )
    tool = builder._run_workflow(  # noqa: SLF001
        ctx,
        wait_for_tasks=False,
        on_tool_event=None,
    )

    result = await tool.handler(
        {
            "goal": "replace the provider",
            "mode": "background",
            "steps": [
                {
                    "id": "figure",
                    "skill": "other-figure",
                    "input": {"input": "RAG architecture"},
                }
            ],
        }
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "workflow_provider_authority_mismatch"
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_recovery_run_skill_persists_under_exact_selected_authority(
    tmp_path,
) -> None:
    settings = load_settings(cwd=tmp_path)
    runtime = _SkillRuntime()
    entry = _entry()
    selected_authority = {
        "consumer_kind": "selected_skill",
        "consumer_id": "0",
        "provider_name": entry.name,
        "provider_source": entry.source,
        "fingerprint": "selected-provider",
    }
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        task_id="task-1",
        session_id="session-1",
        execution_authority={
            "provider_authorities": [selected_authority]
        },
    )
    builder = ToolSurfaceBuilder(
        runtime=runtime,
        tasks=_Events(),
        registry=_Registry(entry),
        mcp_loader=lambda _ctx: [],
    )
    tool = builder._run_skill(  # noqa: SLF001
        ctx,
        wait_for_tasks=True,
        on_tool_event=None,
    )

    result = await tool.handler(
        {
            "skill_name": entry.name,
            "mode": "inline",
            "input": {"query": "attention"},
        }
    )

    assert result["status"] == "succeeded"
    assert result["mode"] == "foreground"
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["provider_authority"] == selected_authority
    assert runtime.calls[0]["notify_channel"] == ""


@pytest.mark.asyncio
async def test_recovery_run_skill_rejects_unplanned_provider(tmp_path) -> None:
    settings = load_settings(cwd=tmp_path)
    runtime = _SkillRuntime()
    entry = _entry()
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        task_id="task-1",
        session_id="session-1",
        execution_authority={
            "provider_authorities": [
                {
                    "consumer_kind": "selected_skill",
                    "consumer_id": "0",
                    "provider_name": "other-provider",
                    "provider_source": entry.source,
                }
            ]
        },
    )
    builder = ToolSurfaceBuilder(
        runtime=runtime,
        tasks=_Events(),
        registry=_Registry(entry),
        mcp_loader=lambda _ctx: [],
    )
    tool = builder._run_skill(  # noqa: SLF001
        ctx,
        wait_for_tasks=True,
        on_tool_event=None,
    )

    result = await tool.handler(
        {
            "skill_name": entry.name,
            "mode": "foreground",
            "input": {"query": "attention"},
        }
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "selected_skill_provider_authority_mismatch"
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_run_skill_projects_concrete_output_contract_failure(tmp_path) -> None:
    _SensitiveEngine.calls = 0
    _SensitiveEngine.malformed = True

    async def approver(_request):  # noqa: ANN001, ANN202
        return ApprovalDecision(True, scope="once")

    gateway, _hooks, _events = await _surface(tmp_path, approver=approver)

    result = await gateway.invoker()(
        "run_skill",
        {
            "skill_name": "sensitive-skill",
            "mode": "inline",
            "input": {"query": "attention"},
        },
    )

    assert result["status"] == "error"
    assert result["contract_violation"] is True
    assert result["reason"] == "output_contract_violation"
    assert result["execution_started"] is True
    assert _SensitiveEngine.calls == 1


def _route_arguments(route: str) -> dict[str, Any]:
    if route == "sensitive-skill":
        return {"query": "attention"}
    return {
        "skill_name": "sensitive-skill",
        "input": {"query": "attention"},
        **({"mode": "inline"} if route == "run_skill" else {}),
    }


@pytest.mark.parametrize("route", ["sensitive-skill", "run_skill", "use_skill"])
@pytest.mark.asyncio
async def test_blocked_concrete_skill_is_rejected_on_every_route(
    tmp_path,
    route: str,
) -> None:
    _SensitiveEngine.calls = 0
    _SensitiveEngine.malformed = False
    gateway, hooks, _events = await _surface(
        tmp_path,
        policy=ToolPolicy(blocked_tools=["sensitive-skill"]),
    )

    result = await gateway.invoker()(route, _route_arguments(route))

    assert result["policy_violation"] is True
    assert result["tool_name"] == "sensitive-skill"
    assert result["reason"] == "blocked_by_plan"
    assert _SensitiveEngine.calls == 0
    assert hooks.events == []


@pytest.mark.parametrize("route", ["sensitive-skill", "run_skill", "use_skill"])
@pytest.mark.asyncio
async def test_concrete_skill_allowlist_has_route_parity(
    tmp_path,
    route: str,
) -> None:
    _SensitiveEngine.calls = 0
    _SensitiveEngine.malformed = False

    async def approver(_request):  # noqa: ANN001, ANN202
        return ApprovalDecision(True, scope="once")

    allowed = ["sensitive-skill"]
    if route != "sensitive-skill":
        allowed.append(route)
    gateway, hooks, _events = await _surface(
        tmp_path,
        approver=approver,
        policy=ToolPolicy(allowed_tools=allowed, max_tool_calls=1),
    )

    result = await gateway.invoker()(route, _route_arguments(route))

    assert result["status"] == ("succeeded" if route == "run_skill" else "ok")
    assert _SensitiveEngine.calls == 1
    assert hooks.events == [
        ("pre_tool", "sensitive-skill"),
        ("post_tool", "sensitive-skill"),
    ]


@pytest.mark.parametrize("route", ["run_skill", "use_skill"])
@pytest.mark.asyncio
async def test_wrapper_allowlist_cannot_expand_to_unlisted_concrete_skill(
    tmp_path,
    route: str,
) -> None:
    _SensitiveEngine.calls = 0
    _SensitiveEngine.malformed = False
    gateway, hooks, _events = await _surface(
        tmp_path,
        policy=ToolPolicy(allowed_tools=[route]),
    )

    result = await gateway.invoker()(route, _route_arguments(route))

    assert result["policy_violation"] is True
    assert result["tool_name"] == "sensitive-skill"
    assert result["reason"] == "not_in_allowed_tools"
    assert _SensitiveEngine.calls == 0
    assert hooks.events == []


@pytest.mark.parametrize("route", ["sensitive-skill", "run_skill", "use_skill"])
@pytest.mark.asyncio
async def test_concrete_hook_can_deny_every_route_without_wrapper_duplicates(
    tmp_path,
    route: str,
) -> None:
    _SensitiveEngine.calls = 0
    _SensitiveEngine.malformed = False
    gateway, hooks, _events = await _surface(
        tmp_path,
        deny_hook_tool="sensitive-skill",
    )

    result = await gateway.invoker()(route, _route_arguments(route))

    assert result["policy_violation"] is True
    assert result["tool_name"] == "sensitive-skill"
    assert result["reason"] == "hook_denied"
    assert _SensitiveEngine.calls == 0
    assert hooks.events == [("pre_tool", "sensitive-skill")]
