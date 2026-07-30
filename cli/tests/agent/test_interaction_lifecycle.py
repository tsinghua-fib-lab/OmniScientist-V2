"""Plan/review modes, lifecycle hooks, and durable run control."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from typing import Any

import pytest

from omni.agent import OmniAgent
from omni.agent.intent_plan import (
    ContextPolicy,
    IntentPlan,
    IntentType,
    SkillSelection,
    ToolPolicy,
    VerificationPlan,
)
from omni.agent.plan_revision import (
    create_execution_authority,
    create_revision,
    deep_clone_plan,
)
from omni.agent.plan_runner_utils import approval_tools_for_plan
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, LLMClient
from omni.core.react_agent import ReActLoopAgent
from omni.core.tool_result import ToolResultEnvelope
from omni.runtime.hooks import HookManager, invoke_tool_with_hooks
from tests.conftest import CapturingLLM, ScriptedLLM


@pytest.mark.asyncio
async def test_plan_mode_persists_plan_without_execution_and_approve_reuses_run():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.llm = ScriptedLLM([ChatWithToolsResult(content="approved execution result")])
    try:
        planned = await agent.handle_turn(
            "先规划如何比较 RAG 检索策略，不要执行",
            channel="cli",
            interaction_mode="plan",
            drain_tasks=False,
        )
        run = await agent.tasks.get_task(planned.task_id)
        assert planned.kind == "plan"
        assert planned.terminated_reason == "awaiting_approval"
        assert run is not None and run.status == "awaiting_approval"
        assert run.plan_json and run.plan_status == "awaiting_approval"
        assert run.current_authority_fingerprint
        assert (
            run.approval_authority_fingerprint
            == run.current_authority_fingerprint
        )
        assert run.approved_tools == []
        assert agent.llm.calls == 0

        executed = await agent.approve_task(planned.task_id, drain_tasks=False)
        assert executed.task_id == planned.task_id
        assert executed.text
        assert executed.kind in {"text", "workflow"}
        run = await agent.tasks.get_task(planned.task_id)
        assert run is not None and run.status in {"running", "succeeded"}
        events = [event.event_type for event in await agent.tasks.list_events(run.id)]
        assert "task.awaiting_approval" in events
        assert "plan.approved" in events
        # The approval claim already changes awaiting_approval -> running
        # atomically; beginning execution must not emit a running -> running
        # lifecycle transition.
        assert "task.resumed" not in events
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_approval_rejects_a_new_self_consistent_revision_and_grants_nothing():
    agent = await OmniAgent.create(load_settings())
    try:
        planned = await agent.handle_turn(
            "Plan a short comparison of retrieval strategies.",
            interaction_mode="plan",
            drain_tasks=False,
        )
        task = await agent.tasks.get_task(planned.task_id)
        assert task is not None
        original = IntentPlan.from_dict(task.plan_json)
        replaced = deep_clone_plan(original)
        replaced.intent_type = IntentType.SINGLE_SKILL_TASK
        replaced.selected_skills = [
            SkillSelection(
                skill="scientific-figure",
                reason="stale writer added a mutating provider",
                matched_capabilities=["artifact.figure"],
                contract_level="full",
            )
        ]
        sealed = create_revision(
            replaced,
            revision=original.revision + 1,
            parent_hash=original.revision_hash,
            source="concurrent_writer",
        )
        await agent.tasks.record_plan(
            task.id,
            sealed.plan,
            status="awaiting_approval",
        )

        with pytest.raises(ValueError, match="changed|approval|review"):
            await agent.approve_task(task.id, drain_tasks=False)

        stored = await agent.tasks.get_task(task.id)
        assert stored is not None
        assert stored.approved_tools == []
        events = await agent.tasks.list_events(task.id)
        assert not any(event.event_type == "plan.approval.bound" for event in events)
        assert not any(event.event_type == "plan.execution.bound" for event in events)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_concurrent_approval_claim_executes_the_plan_exactly_once():
    agent = await OmniAgent.create(load_settings())
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(content="approved once"),
            ChatWithToolsResult(content="must not execute twice"),
        ]
    )
    try:
        planned = await agent.handle_turn(
            "Plan a short comparison of retrieval strategies.",
            interaction_mode="plan",
            drain_tasks=False,
        )

        outcomes = await asyncio.gather(
            agent.approve_task(planned.task_id, drain_tasks=False),
            agent.approve_task(planned.task_id, drain_tasks=False),
            return_exceptions=True,
        )

        assert sum(not isinstance(item, Exception) for item in outcomes) == 1
        assert sum(isinstance(item, (ValueError, RuntimeError)) for item in outcomes) == 1
        events = await agent.tasks.list_events(planned.task_id)
        assert sum(
            event.event_type == "plan.approval.bound" for event in events
        ) == 1
        assert sum(
            event.event_type == "plan.execution.bound" for event in events
        ) == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_approval_rejects_contract_snapshot_drift_and_grants_nothing():
    agent = await OmniAgent.create(load_settings())
    try:
        planned = await agent.handle_turn(
            "Plan a short comparison of retrieval strategies.",
            interaction_mode="plan",
            drain_tasks=False,
        )
        entry = agent.registry.get("arxiv-fetch")
        assert entry is not None
        entry.input_schema = copy.deepcopy(entry.input_schema)
        entry.input_schema["x-test-contract-revision"] = "changed-after-review"

        with pytest.raises(ValueError, match="changed|approval|review|authority"):
            await agent.approve_task(planned.task_id, drain_tasks=False)

        stored = await agent.tasks.get_task(planned.task_id)
        assert stored is not None
        assert stored.status == "awaiting_approval"
        assert stored.approved_tools == []
        events = await agent.tasks.list_events(planned.task_id)
        assert not any(event.event_type == "plan.approval.bound" for event in events)
        assert not any(event.event_type == "plan.execution.bound" for event in events)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_plan_change_between_approval_read_and_claim_loses_the_cas(monkeypatch):
    agent = await OmniAgent.create(load_settings())
    try:
        planned = await agent.handle_turn(
            "Plan a short comparison of retrieval strategies.",
            interaction_mode="plan",
            drain_tasks=False,
        )
        original_claim = agent.tasks.claim_plan_approval

        async def racing_claim(task_id: str, **kwargs: Any) -> bool:
            stored = await agent.tasks.get_task(task_id)
            assert stored is not None
            changed = IntentPlan.from_dict(stored.plan_json)
            changed.rationale = f"{changed.rationale}; concurrent rewrite"
            changed = create_revision(
                changed,
                revision=changed.revision + 1,
                parent_hash=changed.revision_hash,
                source="concurrent_writer",
            ).plan
            await agent.tasks.record_plan(
                task_id,
                changed,
                status="awaiting_approval",
                emit_event=False,
            )
            return await original_claim(task_id, **kwargs)

        monkeypatch.setattr(agent.tasks, "claim_plan_approval", racing_claim)

        with pytest.raises(ValueError, match="approval|changed|claimed"):
            await agent.approve_task(planned.task_id, drain_tasks=False)

        stored = await agent.tasks.get_task(planned.task_id)
        assert stored is not None
        assert stored.approved_tools == []
        assert stored.status == "awaiting_approval"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_reapproval_replaces_old_sensitive_grants_instead_of_unioning():
    agent = await OmniAgent.create(load_settings())
    try:
        task = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="approval scope test",
        )
        broad = IntentPlan(
            task_id=task.id,
            user_message=task.user_input,
            intent_type=IntentType.REACT_FALLBACK,
            tool_policy=ToolPolicy(allowed_tools=["bash", "write_file"]),
        )
        broad = create_revision(broad, revision=0, source="test").plan
        broad_tools = approval_tools_for_plan(broad, agent.registry)
        broad_authority = create_execution_authority(
            broad,
            registry=agent.registry,
            approval_tools=broad_tools,
        )
        await agent.tasks.record_plan(
            task.id,
            broad,
            status="validated",
            emit_event=False,
            current_authority_fingerprint=broad_authority.fingerprint,
        )
        assert await agent.tasks.mark_awaiting_approval(
            task.id,
            authority_fingerprint=broad_authority.fingerprint,
            expected_plan_json=broad.to_dict(),
        )
        assert await agent.tasks.claim_plan_approval(
            task.id,
            authority_fingerprint=broad_authority.fingerprint,
            expected_plan_json=broad.to_dict(),
            approved_tools=broad_tools,
        )
        stored = await agent.tasks.get_task(task.id)
        assert stored is not None
        assert set(stored.approved_tools) == {"bash", "write_file"}

        narrow = deep_clone_plan(broad)
        narrow.tool_policy = ToolPolicy(allowed_tools=["write_file"])
        narrow = create_revision(
            narrow,
            revision=broad.revision + 1,
            parent_hash=broad.revision_hash,
            source="replan",
        ).plan
        narrow_tools = approval_tools_for_plan(narrow, agent.registry)
        narrow_authority = create_execution_authority(
            narrow,
            registry=agent.registry,
            approval_tools=narrow_tools,
        )
        await agent.tasks.record_plan(
            task.id,
            narrow,
            status="validated",
            emit_event=False,
            current_authority_fingerprint=narrow_authority.fingerprint,
        )
        assert await agent.tasks.mark_awaiting_approval(
            task.id,
            authority_fingerprint=narrow_authority.fingerprint,
            expected_plan_json=narrow.to_dict(),
        )
        assert await agent.tasks.claim_plan_approval(
            task.id,
            authority_fingerprint=narrow_authority.fingerprint,
            expected_plan_json=narrow.to_dict(),
            approved_tools=narrow_tools,
        )

        stored = await agent.tasks.get_task(task.id)
        assert stored is not None
        assert stored.approved_tools == ["write_file"]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_review_mode_is_read_only_and_forces_review_path():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = CapturingLLM([ChatWithToolsResult(content="reviewed result")])
    agent.llm = llm
    try:
        turn = await agent.handle_turn(
            "审查当前研究方案的风险",
            channel="cli",
            interaction_mode="review",
            drain_tasks=False,
        )
        run = await agent.tasks.get_task(turn.task_id)
        assert run is not None
        assert run.plan_json["execution_mode"] == "review"
        blocked = set(run.plan_json["tool_policy"]["blocked_tools"])
        assert {"bash", "write_file", "edit_file", "run_compute", "run_skill", "run_workflow"} <= blocked
        assert not ({"bash", "write_file", "edit_file", "run_compute", "run_skill", "run_workflow"} & set(llm.tool_names))
        events = [event.event_type for event in await agent.tasks.list_events(run.id)]
        assert "self_review" in events
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_hook_manager_can_deny_and_redacts_secrets():
    command = (
        f"{sys.executable} -c \"import json,sys; d=json.load(sys.stdin); "
        "print(json.dumps({'action':'deny','reason':'policy','seen':d['payload']}))\""
    )
    settings = load_settings(
        overrides={
            "hooks": {
                "enabled": True,
                "commands": {"pre_tool": [command]},
            }
        }
    )
    manager = HookManager(settings)
    decision = await manager.emit(
        "pre_tool",
        payload={"tool": "bash", "api_key": "secret-value"},
        deny_capable=True,
    )
    assert not decision.allowed
    assert decision.reason == "policy"


@pytest.mark.asyncio
async def test_shared_hook_invoker_blocks_handler_before_every_execution_path():
    command = (
        f"{sys.executable} -c \"import json,sys; json.load(sys.stdin); "
        "print(json.dumps({'action':'deny','reason':'owner policy'}))\""
    )
    settings = load_settings(
        overrides={"hooks": {"enabled": True, "commands": {"pre_tool": [command]}}}
    )
    called = False

    async def handler() -> str:
        nonlocal called
        called = True
        return "should not run"

    with pytest.raises(PermissionError, match="owner policy"):
        await invoke_tool_with_hooks(
            HookManager(settings),
            task_id="run",
            tool_name="run_compute",
            arguments={"command": "python experiment.py"},
            family="workflow_step",
            invoke=handler,
        )
    assert called is False


@pytest.mark.asyncio
async def test_shared_hook_invoker_projects_structured_result_before_post_hook():
    class RecordingHooks:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def emit(self, event: str, **kwargs: Any):  # noqa: ANN201
            self.events.append((event, kwargs["payload"]))

            class Decision:
                allowed = True
                reason = ""

            return Decision()

    hooks = RecordingHooks()
    envelope = ToolResultEnvelope(
        observation="[exit=1]\n",
        event_output={
            "result_schema": "omni.command-result.v1",
            "command_status": "failed",
            "exit_code": 1,
        },
    )

    returned = await invoke_tool_with_hooks(
        hooks,  # type: ignore[arg-type]
        task_id="run",
        tool_name="bash",
        arguments={"command": "false"},
        family="react",
        invoke=lambda: _return_tool_result(envelope),
    )

    post_payload = next(payload for event, payload in hooks.events if event == "post_tool")
    assert returned is envelope
    assert post_payload["status"] == "succeeded"
    assert post_payload["result"]["command_status"] == "failed"
    assert "ToolResultEnvelope" not in repr(post_payload)


async def _return_tool_result(value):  # noqa: ANN001, ANN201
    return value


class _RecordingLLM(LLMClient):
    model = "recording"

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatWithToolsResult:  # noqa: ANN001
        self.calls += 1
        self.messages = list(messages)
        return ChatWithToolsResult(content="steered answer")

    async def chat(self, system: str, user: str, **kwargs: Any) -> str:
        return json.dumps({"verdict": "pass", "score": 1.0, "notes": "ok"})

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


@pytest.mark.asyncio
async def test_react_consumes_steer_and_cancel_at_boundaries():
    llm = _RecordingLLM()
    loop = ReActLoopAgent(llm, lambda _name, _args: None)
    controls = [[{"action": "steer", "instruction": "只比较有引用的证据"}]]

    async def steer():
        return controls.pop(0) if controls else []

    result = await loop.run(
        system_prompt="system",
        user_message="compare",
        tools=[],
        on_control=steer,
    )
    assert result.content == "steered answer"
    assert any("只比较有引用的证据" in str(message.get("content")) for message in llm.messages)

    cancelled = await loop.run(
        system_prompt="system",
        user_message="compare",
        tools=[],
        on_control=lambda: [{"action": "cancel", "instruction": ""}],
    )
    assert cancelled.kind == "partial"
    assert cancelled.terminated_reason == "cancelled"


@pytest.mark.asyncio
async def test_react_cancels_an_inflight_model_request() -> None:
    started = asyncio.Event()

    class SlowLLM(_RecordingLLM):
        async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatWithToolsResult:  # noqa: ANN001
            started.set()
            await asyncio.sleep(30)
            return ChatWithToolsResult(content="late")

    async def controls() -> list[dict[str, str]]:
        return [{"action": "cancel", "instruction": ""}] if started.is_set() else []

    loop = ReActLoopAgent(SlowLLM(), lambda _name, _args: None)
    result = await asyncio.wait_for(
        loop.run(system_prompt="system", user_message="compare", tools=[], on_control=controls),
        timeout=1,
    )

    assert result.kind == "partial"
    assert result.terminated_reason == "cancelled"


@pytest.mark.asyncio
async def test_run_control_is_durable_and_consumed_once():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    try:
        run = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="long research",
        )
        await agent.tasks.record_plan(
            run.id,
            IntentPlan(
                task_id=run.id,
                user_message="long research",
                intent_type=IntentType.REACT_FALLBACK,
            ),
            status="validated",
        )
        await agent.tasks.request_control(run.id, action="steer", instruction="优先处理统计验证")
        first = await agent.tasks.consume_controls(run.id)
        second = await agent.tasks.consume_controls(run.id)
        assert first[0]["action"] == "steer"
        assert first[0]["instruction"] == "优先处理统计验证"
        assert second == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_cancelled_turn_skips_verifier_and_settles_cancelled(monkeypatch) -> None:  # noqa: ANN001
    agent = await OmniAgent.create(load_settings())
    session_id = await agent.ensure_session(channel="cli")
    task = await agent.tasks.create_task(
        session_id=session_id,
        channel="cli",
        user_input="long research",
    )

    async def verifier_must_not_run(_self, _task_id: str) -> str:  # noqa: ANN001
        raise AssertionError("cancelled tasks must bypass content verification")

    monkeypatch.setattr(type(agent.verifier), "verify", verifier_must_not_run)
    try:
        await agent.task_controller.finish_turn(
            task.id,
            kind="partial",
            text="cancelled",
            task_status="cancelled",
        )
        stored = await agent.tasks.get_task(task.id)
        events = await agent.tasks.list_events(task.id)
    finally:
        await agent.aclose()

    assert stored is not None and stored.status == "cancelled"
    assert "task.cancelled" in [event.event_type for event in events]
    assert not any(event.event_type.startswith("verification.") for event in events)


@pytest.mark.asyncio
async def test_agent_cancels_inflight_model_and_persists_terminal_status(monkeypatch) -> None:  # noqa: ANN001
    from omni.agent import orchestrator as orchestrator_mod

    started = asyncio.Event()

    class Planner:
        def __init__(self, registry) -> None:  # noqa: ANN001
            self.registry = registry

        def boundary_plan(self, user_message: str, *, task_id: str = "") -> IntentPlan:
            return IntentPlan(
                task_id=task_id,
                user_message=user_message,
                intent_type=IntentType.REACT_FALLBACK,
                context_policy=ContextPolicy(),
                tool_policy=ToolPolicy(allowed_tools=[]),
                verification_plan=VerificationPlan(required_events=["react.finished"]),
                rationale="test cancellation",
            )

    class SlowLLM(_RecordingLLM):
        async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatWithToolsResult:  # noqa: ANN001
            started.set()
            await asyncio.sleep(30)
            return ChatWithToolsResult(content="late")

    monkeypatch.setattr(orchestrator_mod, "IntentPlanner", Planner)
    agent = await OmniAgent.create(load_settings())
    agent.llm = SlowLLM()
    task_ref: dict[str, str] = {}

    def ack(data: dict[str, str]) -> None:
        task_ref["task_id"] = data["task_id"]

    try:
        running = asyncio.create_task(
            agent.handle_turn("open research question", channel="cli", on_task_ack=ack)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await agent.tasks.request_control(task_ref["task_id"], action="cancel")
        turn = await asyncio.wait_for(running, timeout=1)
        stored = await agent.tasks.get_task(turn.task_id)
        events = await agent.tasks.list_events(turn.task_id)
    finally:
        await agent.aclose()

    assert turn.terminated_reason == "cancelled"
    assert turn.verification_status == "skipped"
    assert stored is not None and stored.status == "cancelled"
    assert "react.finished" in [event.event_type for event in events]
    assert "verification.passed" not in [event.event_type for event in events]
