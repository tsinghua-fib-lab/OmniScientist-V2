"""Unit tests for the human-in-the-loop approval gate (P0 security)."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.core.approval import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
    classify_tool_call,
    resolve_policy,
)


def _settings(**security):  # noqa: ANN003
    s = load_settings()
    for key, value in security.items():
        setattr(s.security, key, value)
    return s


def test_classify_flags_only_sensitive_tools():
    assert classify_tool_call("read_file", {"path": "x"}) is None
    assert classify_tool_call("grep", {"pattern": "x"}) is None
    assert classify_tool_call("write_file", {"path": "a"}).risk == "write"
    assert classify_tool_call("edit_file", {"path": "a"}).risk == "write"
    assert classify_tool_call("run_compute", {"command": "x"}).risk == "exec"


def test_classify_bash_distinguishes_destructive_from_plain_exec():
    assert classify_tool_call("bash", {"command": "pytest -q"}).risk == "exec"
    assert classify_tool_call("bash", {"command": "sudo rm -rf /"}).risk == "destructive"
    # the human-facing preview carries the command
    assert "rm -rf" in classify_tool_call("bash", {"command": "rm -rf /tmp/x"}).detail


def test_resolve_policy_honours_master_switch_and_aliases():
    assert resolve_policy(_settings(require_approval=False)) == "never"
    assert resolve_policy(_settings(require_approval=True, approval_policy="untrusted")) == "untrusted"
    assert resolve_policy(_settings(approval_policy="on-request")) == "untrusted"
    assert resolve_policy(_settings(approval_policy="always")) == "always"
    assert resolve_policy(_settings(approval_policy="bogus")) == "untrusted"


async def _run(gate: ApprovalGate, name: str, args: dict):
    calls: list[tuple[str, dict]] = []

    async def inner(n: str, a: dict):
        calls.append((n, a))
        return {"status": "ok", "ran": n}

    result = await gate.wrap(inner)(name, args)
    return result, calls


@pytest.mark.asyncio
async def test_safe_tool_passes_through_untouched():
    gate = ApprovalGate(_settings(), approver=None)
    result, calls = await _run(gate, "read_file", {"path": "x"})
    assert calls == [("read_file", {"path": "x"})]
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_sensitive_without_approver_fails_closed():
    gate = ApprovalGate(_settings(), channel="cli", approver=None)
    result, calls = await _run(gate, "bash", {"command": "echo hi"})
    assert calls == []  # never reached the inner tool
    assert result["approval_required"] is True
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_im_channel_failclosed_message_mentions_local_confirmation():
    gate = ApprovalGate(_settings(), channel="wechat", approver=None)
    result, _ = await _run(gate, "write_file", {"path": "a", "contents": "x"})
    assert "IM" in result["reason"] or "local confirmation" in result["reason"]


@pytest.mark.asyncio
async def test_interactive_approve_runs_and_records_events():
    events: list[str] = []

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(True, scope="once")

    async def sink(kind: str, _payload: dict) -> None:
        events.append(kind)

    gate = ApprovalGate(_settings(), approver=approver, on_event=sink)
    result, calls = await _run(gate, "bash", {"command": "echo hi"})
    assert calls and result.get("ran") == "bash"
    assert "approval.requested" in events
    assert "approval.granted" in events


@pytest.mark.asyncio
async def test_deny_blocks_and_records():
    events: list[str] = []

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(False, reason="nope")

    async def sink(kind: str, _payload: dict) -> None:
        events.append(kind)

    gate = ApprovalGate(_settings(), approver=approver, on_event=sink)
    result, calls = await _run(gate, "bash", {"command": "echo hi"})
    assert calls == []
    assert result["approval_required"] is True
    assert "approval.denied" in events


@pytest.mark.asyncio
async def test_session_scope_is_remembered_across_calls():
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="session")

    gate = ApprovalGate(_settings(), approver=approver)
    await _run(gate, "bash", {"command": "echo 1"})
    await _run(gate, "bash", {"command": "echo 2"})
    # approved once for the session → the approver is not consulted again.
    assert asked["n"] == 1


@pytest.mark.asyncio
async def test_allowlist_auto_approves_without_prompt():
    events: list[str] = []

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:  # pragma: no cover
        raise AssertionError("allowlisted call must not prompt")

    async def sink(kind: str, _payload: dict) -> None:
        events.append(kind)

    gate = ApprovalGate(
        _settings(approval_allowlist=["bash:git status"]),
        approver=approver,
        on_event=sink,
    )
    result, calls = await _run(gate, "bash", {"command": "git status --short"})
    assert calls and result.get("ran") == "bash"
    assert "approval.auto" in events


@pytest.mark.asyncio
async def test_never_policy_is_full_autonomy():
    gate = ApprovalGate(_settings(require_approval=False), approver=None)
    result, calls = await _run(gate, "bash", {"command": "rm -rf /tmp/x"})
    assert calls and result.get("ran") == "bash"  # auto-approved, no gate


@pytest.mark.asyncio
async def test_always_policy_gates_even_safe_tools():
    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(False, reason="no")

    gate = ApprovalGate(_settings(approval_policy="always"), approver=approver)
    result, calls = await _run(gate, "read_file", {"path": "x"})
    assert calls == []
    assert result["approval_required"] is True
