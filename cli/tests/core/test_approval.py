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
    assert resolve_policy(_settings(approval_policy="on-request")) == "on-request"
    assert resolve_policy(_settings(approval_policy="on_request")) == "on-request"
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
@pytest.mark.parametrize("channel", ["wechat", "feishu", "dingtalk"])
async def test_im_channel_failclosed_message_mentions_local_confirmation(channel: str):
    # A command names nothing to assess, so IM still fail-closes. write_file is
    # decided by destination (in-workspace auto-approves; an escape uses the
    # Codex outside-project reason) and is covered elsewhere.
    gate = ApprovalGate(_settings(), channel=channel, approver=None)
    result, _ = await _run(gate, "bash", {"command": "echo hi"})
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
async def test_session_scope_is_remembered_for_the_command_it_was_given_for():
    """The owner approved a command, so that is what stays approved.

    Keyed on the tool, one "yes" to a harmless command also cleared every later
    command in the session — the grant was wider than the sentence the owner
    read before granting it. Codex remembers the approval request itself.
    """
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="session")

    gate = ApprovalGate(_settings(), approver=approver)
    await _run(gate, "bash", {"command": "echo 1"})
    await _run(gate, "bash", {"command": "echo 1"})
    assert asked == ["echo 1"], "the approved command was asked about twice"

    await _run(gate, "bash", {"command": "echo 2"})
    assert asked == ["echo 1", "echo 2"], "a different command inherited the grant"


@pytest.mark.asyncio
async def test_a_write_still_takes_its_session_grant_tool_wide():
    """A write names its destination, not a command, so it has no narrower key."""
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="session")

    # ``always`` keeps the in-workspace fast path from settling these first.
    gate = ApprovalGate(_settings(approval_policy="always"), approver=approver)
    await _run(gate, "write_file", {"path": "/outside/a.md", "contents": "x"})
    await _run(gate, "write_file", {"path": "/outside/b.md", "contents": "y"})
    assert asked["n"] == 1


@pytest.mark.asyncio
async def test_reading_the_log_is_not_worth_a_prompt():
    """A prompt that fires for reading is dismissed by habit before it matters.

    ``git log`` reached the owner through exactly the gate that has to stop
    ``rm -rf``, because the gate could tell a shell call from a file write but
    not one command from another.
    """
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="once")

    gate = ApprovalGate(_settings(), approver=approver)
    _, calls = await _run(
        gate, "bash", {"command": "cd /tmp/project && git log --oneline -30"}
    )

    assert calls, "a read-only command was not run"
    assert asked["n"] == 0


@pytest.mark.asyncio
async def test_bounded_git_output_is_not_worth_a_prompt():
    """A stdin-only formatter must not turn a safe Git read into an approval."""
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="once")

    command = (
        "cd /Users/antonio/work/omniscientist_v2 && "
        "git show e16c8e0d -p -- "
        "cli/src/omni/cli/commands/tasks_cmd.py "
        "cli/src/omni/runtime/task_recorder.py | head -250"
    )
    gate = ApprovalGate(_settings(), approver=approver)
    _, calls = await _run(gate, "bash", {"command": command})

    assert calls, "a bounded read-only pipeline was not run"
    assert asked["n"] == 0


@pytest.mark.asyncio
async def test_a_trailing_stderr_merge_does_not_make_a_read_worth_a_prompt():
    """``2>&1`` is already how the bash tool delivers output.

    The classifier used to treat any ``>`` as a shell effect, so a known-safe
    ``git show`` that the model had wrapped in a redundant stderr merge reached
    the owner as if it were about to write a file.
    """
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="once")

    command = (
        "git show 4c4ea93e -- "
        "cli/src/omni/scheduling/temporal.py "
        "cli/src/omni/agent/schedule_tools.py 2>&1"
    )
    gate = ApprovalGate(_settings(), approver=approver)
    _, calls = await _run(gate, "bash", {"command": command})

    assert calls, "a read-only command was not run"
    assert asked["n"] == 0


@pytest.mark.asyncio
async def test_a_command_that_only_looks_read_only_still_asks():
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(False, reason="nope")

    gate = ApprovalGate(_settings(), approver=approver)
    for command in (
        "git log > /tmp/stolen.txt",          # redirection writes
        "git log && rm -rf build",            # one unsafe segment taints the whole
        "./git log",                          # path-qualified: some other binary
        "git -c core.pager=sh log",           # redirects where git reads config
        "git push",                           # not a read-only subcommand
        "echo $(cat ~/.ssh/id_rsa)",          # substitution
    ):
        await _run(gate, "bash", {"command": command})

    assert len(asked) == 6, f"a command bypassed the gate: {asked}"


@pytest.mark.asyncio
async def test_always_policy_still_asks_about_reading():
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="once")

    gate = ApprovalGate(_settings(approval_policy="always"), approver=approver)
    await _run(gate, "bash", {"command": "git status"})

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
async def test_workspace_auto_runs_sandboxed_bash_without_an_approver(tmp_path):  # noqa: ANN001
    events: list[str] = []

    async def sink(kind: str, _payload: dict) -> None:
        events.append(kind)

    gate = ApprovalGate(
        _settings(),
        channel="cli",
        approver=None,
        workspace_auto=True,
        working_dir=tmp_path,
        workspace=tmp_path,
        on_event=sink,
    )
    result, calls = await _run(gate, "bash", {"command": "python -c 'print(1)'"})
    assert calls and result.get("ran") == "bash"
    assert "approval.auto" in events
    assert "approval.requested" not in events


@pytest.mark.asyncio
async def test_workspace_auto_denies_an_outside_write_without_prompting(tmp_path):  # noqa: ANN001
    asked = 0

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        nonlocal asked
        asked += 1
        return ApprovalDecision(True, scope="once")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "elsewhere.md"
    gate = ApprovalGate(
        _settings(),
        channel="cli",
        approver=approver,
        workspace_auto=True,
        writable_roots=[workspace],
        working_dir=workspace,
        workspace=workspace,
    )
    result, calls = await _run(
        gate, "write_file", {"path": str(outside), "contents": "x"}
    )
    assert calls == []
    assert asked == 0
    assert result["approval_required"] is True
    assert "outside" in result["reason"]


@pytest.mark.asyncio
async def test_workspace_auto_is_ignored_on_im():
    gate = ApprovalGate(
        _settings(), channel="wechat", approver=None, workspace_auto=True
    )
    result, calls = await _run(gate, "bash", {"command": "python -c 'print(1)'"})
    assert calls == []
    assert "local confirmation" in result["reason"] or "IM" in result["reason"]


@pytest.mark.asyncio
async def test_preauthorizer_cannot_approve_an_outside_write(tmp_path):  # noqa: ANN001
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "elsewhere.md"

    async def preauthorizer(name: str, _args: dict) -> bool:
        return name == "write_file"

    gate = ApprovalGate(
        _settings(),
        channel="cli",
        approver=None,
        preauthorizer=preauthorizer,
        writable_roots=[workspace],
        working_dir=workspace,
        workspace=workspace,
    )
    result, calls = await _run(
        gate, "write_file", {"path": str(outside), "contents": "x"}
    )
    assert calls == []
    assert "outside" in result["reason"]


@pytest.mark.asyncio
async def test_always_policy_gates_even_safe_tools():
    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(False, reason="no")

    gate = ApprovalGate(_settings(approval_policy="always"), approver=approver)
    result, calls = await _run(gate, "read_file", {"path": "x"})
    assert calls == []
    assert result["approval_required"] is True


_DOT_RENDER = (
    "command -v dot && "
    "dot -Tsvg figures/agent-loop-engineering-architecture.dot "
    "-o figures/agent-loop-engineering-architecture.svg && "
    "dot -Tpng figures/agent-loop-engineering-architecture.dot "
    "-o figures/agent-loop-engineering-architecture.png && "
    "ls -la figures/agent-loop-engineering-architecture.*"
)


@pytest.mark.asyncio
async def test_on_request_auto_allows_non_destructive_bash() -> None:
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="once")

    gate = ApprovalGate(_settings(approval_policy="on-request"), approver=approver)
    _, calls = await _run(gate, "bash", {"command": _DOT_RENDER})

    assert calls, "on-request must run a non-destructive workspace command"
    assert asked["n"] == 0


@pytest.mark.asyncio
async def test_untrusted_still_asks_for_dot_render() -> None:
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="once")

    gate = ApprovalGate(_settings(approval_policy="untrusted"), approver=approver)
    _, calls = await _run(gate, "bash", {"command": _DOT_RENDER})

    assert calls, "the owner approved the command"
    assert asked["n"] == 1


@pytest.mark.asyncio
async def test_on_request_still_asks_for_destructive_bash() -> None:
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(False, reason="nope")

    gate = ApprovalGate(_settings(approval_policy="on-request"), approver=approver)
    result, calls = await _run(gate, "bash", {"command": "rm -rf build"})

    assert calls == []
    assert asked["n"] == 1
    assert result["approval_required"] is True


@pytest.mark.asyncio
async def test_trusted_auto_preset_runs_toolchain_probe_without_prompt() -> None:
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="once")

    settings = load_settings(trusted=True)
    assert settings.security.approval_policy == "on-request"
    gate = ApprovalGate(settings, approver=approver)
    _, calls = await _run(
        gate,
        "bash",
        {
            "command": (
                "which dot; dot -V 2>&1; which python3; "
                "python3 -c \"import matplotlib; print(1)\" 2>&1"
            )
        },
    )
    assert calls
    assert asked["n"] == 0


@pytest.mark.asyncio
async def test_untrusted_readonly_asks_for_ls_and_workspace_write(tmp_path) -> None:  # noqa: ANN001
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="once")

    settings = load_settings(trusted=False)
    assert settings.security.bash_sandbox == "readonly"
    gate = ApprovalGate(
        settings,
        approver=approver,
        writable_roots=[tmp_path],
        working_dir=tmp_path,
        workspace=tmp_path,
    )
    _, ls_calls = await _run(gate, "bash", {"command": "ls -la"})
    assert ls_calls
    assert asked["n"] == 1
    _, write_calls = await _run(
        gate, "write_file", {"path": str(tmp_path / "note.md"), "contents": "x"}
    )
    assert write_calls
    assert asked["n"] == 2


@pytest.mark.asyncio
async def test_workspace_auto_does_not_widen_readonly_sandbox() -> None:
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="once")

    settings = load_settings(trusted=False)
    gate = ApprovalGate(
        settings,
        channel="cli",
        approver=approver,
        workspace_auto=True,
    )
    _, calls = await _run(gate, "bash", {"command": "python -c 'print(1)'"})
    assert calls
    assert asked["n"] == 1


@pytest.mark.asyncio
async def test_on_request_readonly_sandbox_still_asks() -> None:
    asked = {"n": 0}

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        asked["n"] += 1
        return ApprovalDecision(True, scope="once")

    gate = ApprovalGate(
        _settings(approval_policy="on-request", bash_sandbox="readonly"),
        approver=approver,
    )
    _, calls = await _run(gate, "bash", {"command": _DOT_RENDER})

    assert calls
    assert asked["n"] == 1
