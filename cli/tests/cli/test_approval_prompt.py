"""Interactive approver construction (TUI modal + cancellable stdin prompt).

Covers the path-f behaviour from the approval-clock work: the TUI approver maps
the Future-based modal's choices to decisions (no worker thread), and the plain
stdin approver returns promptly when the owner cancels instead of wedging on the
blocked prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import pytest

import omni.cli.approval_prompt as ap
from omni.core.approval import ApprovalChoice, ApprovalDecision, ApprovalRequest


def _req(risk: str = "exec") -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="bash", arguments={"command": "x"}, risk=risk, detail="rm -rf /tmp/x",
    )


class _FakeModalTui:
    """A TUI exposing the Future-based ``request_approval`` modal."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.calls: list[tuple[str, str, object]] = []

    async def request_approval(self, title, detail="", *, options=None, default=""):  # noqa: ANN001
        self.calls.append((title, detail, options))
        return self.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "approved", "scope"),
    [
        ("approve", True, "once"),
        ("deny", False, "once"),
    ],
)
async def test_tui_approver_maps_modal_choice(value, approved, scope):
    approver = ap.build_tui_approver(_FakeModalTui(value))
    decision = await approver(_req())
    assert decision.approved is approved
    if approved:
        assert decision.scope == scope


@pytest.mark.asyncio
async def test_tui_approver_keeps_codex_order_for_destructive():
    tui = _FakeModalTui("deny")
    await ap.build_tui_approver(tui)(_req(risk="destructive"))
    _title, _detail, options = tui.calls[0]
    assert options is not None
    assert [option.value for option in options] == ["approve", "deny"]


@pytest.mark.asyncio
async def test_tui_approver_renders_boundary_supplied_choices_and_maps_rule():
    tui = _FakeModalTui("approve_rule")
    req = ApprovalRequest(
        tool_name="bash",
        arguments={"command": "pytest -q tests/a.py"},
        risk="exec",
        detail="pytest -q tests/a.py",
        choices=(
            ApprovalChoice("approve", "Approve once"),
            ApprovalChoice("approve_rule", "Approve `pytest -q ...` for this session"),
            ApprovalChoice("deny", "Deny"),
        ),
    )

    decision = await ap.build_tui_approver(tui)(req)
    _title, _detail, options = tui.calls[0]

    assert decision.approved and decision.scope == "rule"
    assert [option.value for option in options] == [
        "approve",
        "approve_rule",
        "deny",
    ]
    assert "pytest -q" in options[1].label


def test_plain_prompt_uses_codex_order_and_defaults_to_once(monkeypatch):
    from rich.prompt import Prompt

    captured: dict[str, object] = {}

    def answer(_cls, prompt, **kwargs):  # noqa: ANN001
        captured.update(prompt=prompt, **kwargs)
        return "a"

    monkeypatch.setattr(Prompt, "ask", classmethod(answer))
    req = ApprovalRequest(
        tool_name="bash",
        arguments={"command": "pytest -q tests/a.py"},
        risk="exec",
        detail="pytest -q tests/a.py",
        choices=(
            ApprovalChoice("approve", "Approve once"),
            ApprovalChoice("approve_rule", "Approve `pytest -q ...` for this session"),
            ApprovalChoice("deny", "Deny"),
        ),
    )

    decision = ap._decide(req)

    assert decision.approved and decision.scope == "rule"
    assert "pytest -q" in str(captured["prompt"])
    assert captured["choices"] == ["y", "a", "n"]
    assert captured["default"] == "y"


def test_exact_session_decision_mapping_remains_compatible():
    decision = ap._decision("approve_session")
    assert decision.approved and decision.scope == "session"


@pytest.mark.asyncio
async def test_tui_approver_propagates_cancel_without_a_thread():
    class _Cancelling:
        async def request_approval(self, *_a, **_k):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ap.build_tui_approver(_Cancelling())(_req())


@pytest.mark.asyncio
async def test_tui_approver_queues_requests_across_session_gates():
    class _QueuedTui:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.releases = [asyncio.Event(), asyncio.Event()]

        async def request_approval(self, _title, detail="", *, options=None, default=""):  # noqa: ANN001
            del options, default
            index = len(self.calls)
            self.calls.append(detail)
            await self.releases[index].wait()
            return "approve"

    tui = _QueuedTui()
    approver = ap.build_tui_approver(tui)
    first = asyncio.create_task(approver(_req()))
    await asyncio.sleep(0)
    second = asyncio.create_task(approver(_req()))
    await asyncio.sleep(0)
    assert len(tui.calls) == 1

    tui.releases[0].set()
    await first
    await asyncio.sleep(0)
    assert len(tui.calls) == 2
    tui.releases[1].set()
    await second


@pytest.mark.asyncio
async def test_tui_queue_wait_precedes_the_human_decision_clock_pause(monkeypatch):
    entered_pause: list[str] = []

    @contextlib.contextmanager
    def tracked_pause():
        entered_pause.append("enter")
        try:
            yield
        finally:
            entered_pause.append("exit")

    monkeypatch.setattr(ap, "pause_clocks", tracked_pause)

    class _QueuedTui:
        def __init__(self) -> None:
            self.releases = [asyncio.Event(), asyncio.Event()]
            self.calls = 0

        async def request_approval(self, *_args, **_kwargs):
            index = self.calls
            self.calls += 1
            await self.releases[index].wait()
            return "approve"

    tui = _QueuedTui()
    approver = ap.build_tui_approver(tui)
    first = asyncio.create_task(approver(_req()))
    await asyncio.sleep(0)
    second = asyncio.create_task(approver(_req()))
    await asyncio.sleep(0)

    assert tui.calls == 1
    assert entered_pause == ["enter"]

    tui.releases[0].set()
    await first
    await asyncio.sleep(0)
    assert tui.calls == 2
    assert entered_pause == ["enter", "exit", "enter"]
    tui.releases[1].set()
    await second
    assert entered_pause == ["enter", "exit", "enter", "exit"]


@pytest.mark.asyncio
async def test_tui_approver_falls_back_to_stdin_when_modal_absent(monkeypatch):
    monkeypatch.setattr(ap, "_decide", lambda req: ApprovalDecision(True, scope="once"))

    class _LegacyTui:
        def __init__(self) -> None:
            self.suspended_used = False

        def suspended(self):
            outer = self

            class _Ctx:
                async def __aenter__(self):
                    outer.suspended_used = True

                async def __aexit__(self, *_exc):
                    return False

            return _Ctx()

    tui = _LegacyTui()
    decision = await ap.build_tui_approver(tui)(_req())
    assert decision.approved and tui.suspended_used


@pytest.mark.asyncio
async def test_cli_approver_returns_owner_decision(monkeypatch):
    monkeypatch.setattr(
        ap, "_decide", lambda req: ApprovalDecision(True, scope="session", reason="ok"),
    )
    decision = await ap.build_cli_approver()(_req())
    assert decision.approved and decision.scope == "session"


@pytest.mark.asyncio
async def test_cli_approver_returns_promptly_on_cancel(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocking_decide(_req):
        started.set()
        release.wait(5)  # mimic a human staring at the prompt (blocked stdin)
        return ApprovalDecision(True, scope="once")

    monkeypatch.setattr(ap, "_decide", blocking_decide)
    task = asyncio.create_task(ap.build_cli_approver()(_req()))
    await asyncio.to_thread(started.wait, 2)  # ensure the worker thread is blocked
    assert started.is_set()

    task.cancel()
    t0 = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - t0 < 1.0  # did not wait for the blocked prompt

    release.set()  # let the abandoned daemon thread unwind


class _ScriptedTui:
    """Return a sequence of modal choices, including the confirmation page."""

    def __init__(self, values: list[str]) -> None:
        self.values = list(values)
        self.calls: list[tuple[str, str, object, str]] = []

    async def request_approval(self, title, detail="", *, options=None, default=""):  # noqa: ANN001
        self.calls.append((title, detail, options, default))
        return self.values.pop(0)


def _task_bash_req() -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="bash",
        arguments={"command": "git push"},
        risk="destructive",
        detail="git push",
        task_id="2dc05b85deadbeef",
        choices=(
            ApprovalChoice("approve", "Approve once"),
            ApprovalChoice("approve_all_bash", "Approve all"),
            ApprovalChoice("deny", "Deny"),
        ),
    )


@pytest.mark.asyncio
async def test_tui_approve_all_requires_a_second_confirmation() -> None:
    tui = _ScriptedTui(["approve_all_bash", "enable"])
    decision = await ap.build_tui_approver(tui)(_task_bash_req())

    assert decision.approved and decision.scope == "task_bash"
    assert len(tui.calls) == 2
    _title, detail, options, default = tui.calls[1]
    lowered = detail.lower()
    assert "git push" in lowered and "rm -rf" in lowered
    assert "without asking" in lowered or "not be asked" in lowered
    assert "sandbox" in lowered
    assert [option.value for option in options] == ["enable", "cancel"]
    assert default == "cancel"


@pytest.mark.asyncio
async def test_tui_approve_all_cancel_returns_to_the_command_prompt() -> None:
    tui = _ScriptedTui(["approve_all_bash", "cancel", "approve"])
    decision = await ap.build_tui_approver(tui)(_task_bash_req())

    assert decision.approved and decision.scope == "once"
    assert [call[3] for call in tui.calls] == ["", "cancel", ""]


@pytest.mark.asyncio
async def test_tui_approve_all_escape_denies_the_command() -> None:
    tui = _ScriptedTui(["approve_all_bash", "deny"])
    decision = await ap.build_tui_approver(tui)(_task_bash_req())

    assert not decision.approved
    assert len(tui.calls) == 2


def test_plain_prompt_approve_all_confirms_with_cancel_as_default(monkeypatch) -> None:
    from rich.prompt import Prompt

    answers = ["t", "c", "y"]
    captured: list[dict[str, object]] = []

    def answer(_cls, prompt, **kwargs):  # noqa: ANN001
        captured.append({"prompt": prompt, **kwargs})
        return answers.pop(0)

    monkeypatch.setattr(Prompt, "ask", classmethod(answer))
    decision = ap._decide(_task_bash_req())

    assert decision.approved and decision.scope == "once"
    assert captured[1]["default"] == "c"
    assert captured[1]["choices"] == ["e", "c"]


def test_plain_prompt_approve_all_enable_grants_task_bash(monkeypatch) -> None:
    from rich.prompt import Prompt

    answers = ["t", "e"]

    def answer(_cls, prompt, **kwargs):  # noqa: ANN001
        del prompt, kwargs
        return answers.pop(0)

    monkeypatch.setattr(Prompt, "ask", classmethod(answer))
    decision = ap._decide(_task_bash_req())

    assert decision.approved and decision.scope == "task_bash"
