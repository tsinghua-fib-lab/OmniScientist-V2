"""Terminal approval prompt for sensitive tool calls (P0 security).

Builds the interactive :data:`omni.core.approval.Approver` the CLI wires onto
the agent when a TTY is present.

Cancellation matters here: the approval wait sits on the turn's critical path
but is *excluded* from the deadline (see ``omni.core.turn_clock``). If the owner
cancels while a prompt is open, the decision must return promptly instead of
wedging on a blocked stdin read:

* TUI — prefer the Future-based modal (``ReplTui.request_approval``). It renders
  inside the running app, so Esc/Ctrl-C resolve the future to *deny* and an
  outer cancel simply drops the ``await`` — no worker thread is involved.
* Plain terminal — ``Prompt.ask`` blocks stdin and cannot be interrupted, so it
  runs in a *daemon* thread whose answer is delivered through a future. On cancel
  the ``await`` returns at once and the abandoned thread (and its late answer)
  is discarded without blocking interpreter shutdown.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from omni.core.approval import ApprovalDecision, ApprovalRequest, Approver

if TYPE_CHECKING:
    from omni.cli.repl_tui import ReplTui

_RISK_STYLE = {
    "destructive": ("red", "Destructive command"),
    "exec": ("yellow", "Command execution"),
    "write": ("yellow", "File write"),
    "tool": ("cyan", "Tool call"),
}


def _decide(req: ApprovalRequest) -> ApprovalDecision:
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.text import Text

    from omni.cli.render import console

    style, label = _RISK_STYLE.get(req.risk, ("cyan", req.risk))
    body = Text()
    body.append(f"{label}\n", style=f"bold {style}")
    body.append(f"Tool: {req.tool_name}\n")
    if req.detail:
        body.append(f"Details: {req.detail}")
    console.print(Panel(body, title="Approval required", border_style=style, expand=False))

    # Destructive calls default to deny; ordinary writes/exec default to once.
    default = "n" if req.risk == "destructive" else "y"
    choice = Prompt.ask(
        "Allow? [y] once / [s] this session / [n] deny",
        choices=["y", "s", "n"],
        default=default,
    )
    if choice == "s":
        return ApprovalDecision(True, scope="session", reason="approved-for-session")
    if choice == "y":
        return ApprovalDecision(True, scope="once", reason="approved-once")
    return ApprovalDecision(False, reason="denied-by-owner")


def _resolve(fut: asyncio.Future, decision: ApprovalDecision | None, exc: BaseException | None) -> None:
    if fut.done():  # cancelled by the caller; discard the late stdin answer
        return
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(decision)


def build_cli_approver() -> Approver:
    """Return an approver that prompts the owner on the real terminal.

    Runs the blocking prompt in a daemon thread so an owner cancel returns
    immediately; ``input()`` can't be interrupted, so the thread is abandoned
    (daemon => never blocks shutdown) and its late answer is dropped.
    """

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalDecision] = loop.create_future()

        def worker() -> None:
            decision: ApprovalDecision | None = None
            err: BaseException | None = None
            try:
                decision = _decide(req)
            except BaseException as exc:  # noqa: BLE001 - relay to the awaiting task
                err = exc
            try:
                loop.call_soon_threadsafe(_resolve, fut, decision, err)
            except RuntimeError:
                pass  # event loop already closed; nothing awaits the answer

        threading.Thread(target=worker, name="omni-approval", daemon=True).start()
        return await fut

    return approver


def _tui_title(req: ApprovalRequest) -> str:
    _, label = _RISK_STYLE.get(req.risk, ("cyan", req.risk))
    return f"{label}: {req.tool_name}"


def build_tui_approver(tui: ReplTui) -> Approver:
    """Approve via the in-app modal so cancel never blocks on a worker thread.

    Falls back to suspending the inline dock and prompting on real stdin if
    the running TUI predates :meth:`ReplTui.request_approval`.
    """

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        request = getattr(tui, "request_approval", None)
        if request is None:
            async with tui.suspended():
                return await asyncio.to_thread(_decide, req)

        options = None
        if req.risk == "destructive":
            # Surface deny first so the default cursor position denies.
            from omni.cli.repl_tui import ApprovalOption

            options = (
                ApprovalOption("deny", "Deny"),
                ApprovalOption("approve", "Approve once"),
                ApprovalOption("approve_session", "Approve for this session"),
            )

        choice = await request(_tui_title(req), req.detail or "", options=options)
        if choice == "approve":
            return ApprovalDecision(True, scope="once", reason="approved-once")
        if choice == "approve_session":
            return ApprovalDecision(True, scope="session", reason="approved-for-session")
        return ApprovalDecision(False, reason="denied-by-owner")

    return approver


__all__ = ["build_cli_approver", "build_tui_approver"]
