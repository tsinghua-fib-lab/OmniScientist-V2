"""Interactive approver construction (TUI modal + cancellable stdin prompt).

Covers the path-f behaviour from the approval-clock work: the TUI approver maps
the Future-based modal's choices to decisions (no worker thread), and the plain
stdin approver returns promptly when the owner cancels instead of wedging on the
blocked prompt.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import omni.cli.approval_prompt as ap
from omni.core.approval import ApprovalDecision, ApprovalRequest


def _req(risk: str = "exec") -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="bash", arguments={"command": "x"}, risk=risk, detail="rm -rf /tmp/x",
    )


class _FakeModalTui:
    """A TUI exposing the Future-based ``request_approval`` modal."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.calls: list[tuple[str, str, object]] = []

    async def request_approval(self, title, detail="", *, options=None):  # noqa: ANN001
        self.calls.append((title, detail, options))
        return self.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "approved", "scope"),
    [
        ("approve", True, "once"),
        ("approve_session", True, "session"),
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
async def test_tui_approver_orders_deny_first_for_destructive():
    tui = _FakeModalTui("deny")
    await ap.build_tui_approver(tui)(_req(risk="destructive"))
    _title, _detail, options = tui.calls[0]
    assert options is not None
    assert options[0].value == "deny"  # default cursor lands on deny


@pytest.mark.asyncio
async def test_tui_approver_propagates_cancel_without_a_thread():
    class _Cancelling:
        async def request_approval(self, *_a, **_k):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ap.build_tui_approver(_Cancelling())(_req())


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
