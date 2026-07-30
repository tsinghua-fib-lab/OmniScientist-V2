"""Approval gate wired through the orchestrator ReAct loop (P0 security).

End-to-end coverage of the owner-facing outcomes: an interactive owner may
approve or deny a shell command; a daemon/IM caller never sees the shell (and
would fail closed); ``omni exec`` workspace-auto offers and runs sandboxed
bash; and full-autonomy mode runs it without a prompt. This is the
integration-level twin of ``tests/core/test_approval.py``.
"""

from __future__ import annotations

import pytest

from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.core.approval import ApprovalDecision, ApprovalRequest
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from tests.conftest import PlanningLLM

_REACT_PLAN = {"intent_type": "react_fallback", "confidence": 0.4, "rationale": "assistant"}


def _script_bash(command: str = "echo hi") -> list[ChatWithToolsResult]:
    """ReAct emits one bash call, then closes with a plain answer."""
    return [
        ChatWithToolsResult(content="", tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": command})]),
        ChatWithToolsResult(content="done"),
    ]


async def _event_kinds(agent: OmniAgent, task_id: str) -> list[str]:
    return [e.event_type for e in await agent.tasks.list_events(task_id)]


@pytest.mark.asyncio
async def test_interactive_owner_can_approve_shell():
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(_REACT_PLAN, script=_script_bash())

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(True, scope="once")

    agent.approver = approver
    try:
        turn = await agent.handle_turn("在项目里运行 echo hi", channel="cli", drain_tasks=False)
        assert turn.text == "done"
        kinds = await _event_kinds(agent, turn.task_id)
        assert "approval.granted" in kinds
        # the approved tool actually reached the catalog + ran
        assert "bash" in agent.llm.tool_names
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_owner_denial_blocks_shell():
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(_REACT_PLAN, script=_script_bash("rm -rf /tmp/x"))

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(False, reason="no")

    agent.approver = approver
    try:
        turn = await agent.handle_turn("清理临时目录", channel="cli", drain_tasks=False)
        kinds = await _event_kinds(agent, turn.task_id)
        assert "approval.denied" in kinds
        assert "approval.granted" not in kinds
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_noninteractive_hides_sensitive_tool_from_catalog():
    # Default settings are armed (require_approval=True) but no approver is wired
    # (daemon / non-interactive), so bash must not even be offered.
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(_REACT_PLAN, script=_script_bash())
    try:
        turn = await agent.handle_turn("在项目里运行 echo hi", channel="cli", drain_tasks=False)
        assert turn.text == "done"
        assert "bash" not in agent.llm.tool_names
        kinds = await _event_kinds(agent, turn.task_id)
        assert not any(k.startswith("approval.") for k in kinds)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_trusted_auto_runs_python_probe_without_prompt():
    agent = await OmniAgent.create(load_settings(trusted=True))
    agent.llm = PlanningLLM(_REACT_PLAN, script=_script_bash("python -c 'print(1)'"))
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="once")

    agent.approver = approver
    try:
        turn = await agent.handle_turn(
            "在项目里运行 python -c 'print(1)'", channel="cli", drain_tasks=False
        )
        assert turn.text == "done"
        assert asked["n"] == 0
        kinds = await _event_kinds(agent, turn.task_id)
        assert "approval.auto" in kinds
        assert "approval.requested" not in kinds
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_workspace_auto_offers_and_runs_sandboxed_bash():
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(_REACT_PLAN, script=_script_bash("python -c 'print(1)'"))
    agent.approver = None
    try:
        turn = await agent.handle_turn(
            "在项目里运行 python -c 'print(1)'",
            channel="cli",
            drain_tasks=False,
            workspace_auto=True,
        )
        assert turn.text == "done"
        assert "bash" in agent.llm.tool_names
        stored = await agent.tasks.get_task(turn.task_id)
        assert stored is not None
        assert set(stored.approved_tools) >= {"bash", "write_file", "edit_file", "run_compute"}
        kinds = await _event_kinds(agent, turn.task_id)
        assert "approval.task.granted" in kinds
        assert "approval.denied" not in kinds
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_workspace_auto_is_ignored_on_im():
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(_REACT_PLAN, script=_script_bash("python -c 'print(1)'"))
    agent.approver = None
    try:
        turn = await agent.handle_turn(
            "在项目里运行 python -c 'print(1)'",
            channel="wechat",
            drain_tasks=False,
            workspace_auto=True,
        )
        assert turn.text == "done"
        assert "bash" not in agent.llm.tool_names
        stored = await agent.tasks.get_task(turn.task_id)
        assert stored is not None
        assert "bash" not in set(stored.approved_tools or [])
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_autonomy_runs_shell_without_prompt():
    settings = load_settings()
    settings.security.require_approval = False
    agent = await OmniAgent.create(settings)
    agent.llm = PlanningLLM(_REACT_PLAN, script=_script_bash())
    try:
        turn = await agent.handle_turn("在项目里运行 echo hi", channel="cli", drain_tasks=False)
        assert turn.text == "done"
        # autonomy → tool offered and run, gate stays silent (no approval events)
        assert "bash" in agent.llm.tool_names
        kinds = await _event_kinds(agent, turn.task_id)
        assert not any(k.startswith("approval.") for k in kinds)
    finally:
        await agent.aclose()
