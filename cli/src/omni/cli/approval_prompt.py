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

from omni.core.approval import ApprovalChoice, ApprovalDecision, ApprovalRequest, Approver
from omni.core.turn_clock import pause_clocks

if TYPE_CHECKING:
    from omni.cli.repl_tui import ReplTui

_RISK_STYLE = {
    "destructive": ("red", "Destructive command"),
    "exec": ("yellow", "Command execution"),
    "write": ("yellow", "File write"),
    "tool": ("cyan", "Tool call"),
}

_DEFAULT_CHOICES = (
    ApprovalChoice("approve", "Approve once"),
    ApprovalChoice("deny", "Deny"),
)

_APPROVE_ALL_VALUE = "approve_all_bash"
_TASK_BASH_ENABLE = "enable"
_TASK_BASH_CANCEL = "cancel"


def _choices(req: ApprovalRequest) -> tuple[ApprovalChoice, ...]:
    if req.choices:
        return req.choices
    return _DEFAULT_CHOICES


def _task_bash_confirmation_detail(req: ApprovalRequest) -> str:
    task = (req.task_id or "")[:8] or "this task"
    return (
        f"Allow task `{task}` to write and run sandboxed commands in this "
        "workspace without asking?\n"
        "Later bash / run_compute on this task will not prompt, including "
        "workspace-destructive commands such as git push and rm -rf.\n"
        "In-workspace writes stay auto-approved; leaving the project still asks "
        "or fails closed. The grant is saved on the task so retry/recovery "
        "does not re-probe.\n"
        "The workspace sandbox and system hard-blocks still apply."
    )


def _decision(value: str) -> ApprovalDecision:
    if value == "approve_rule":
        return ApprovalDecision(True, scope="rule", reason="approved-rule-for-session")
    if value == _APPROVE_ALL_VALUE:
        return ApprovalDecision(
            True, scope="task_bash", reason="approved-all-bash-for-task"
        )
    if value in {"approve_session", "session"}:
        return ApprovalDecision(True, scope="session", reason="approved-exact-for-session")
    if value in {"approve", "once"}:
        return ApprovalDecision(True, scope="once", reason="approved-once")
    return ApprovalDecision(False, reason="denied-by-owner")


def _decide(req: ApprovalRequest) -> ApprovalDecision:
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.text import Text

    from omni.cli.render import console

    style, label = _RISK_STYLE.get(req.risk, ("cyan", req.risk))
    key_by_value = {
        "approve": "y",
        "approve_session": "s",
        "approve_rule": "a",
        _APPROVE_ALL_VALUE: "t",
        "deny": "n",
    }
    value_by_key = {key: value for value, key in key_by_value.items()}
    choices = _choices(req)
    while True:
        body = Text()
        body.append(f"{label}\n", style=f"bold {style}")
        body.append(f"Tool: {req.tool_name}\n")
        if req.detail:
            body.append(f"Details: {req.detail}")
        console.print(Panel(body, title="Approval required", border_style=style, expand=False))
        prompt = "Allow? " + " / ".join(
            f"[{key_by_value[choice.value]}] {choice.label}" for choice in choices
        )
        default = key_by_value[choices[0].value]
        answer = Prompt.ask(
            prompt,
            choices=[key_by_value[choice.value] for choice in choices],
            default=default,
        )
        value = value_by_key[answer]
        if value != _APPROVE_ALL_VALUE:
            return _decision(value)
        if _confirm_task_bash(req):
            return _decision(_APPROVE_ALL_VALUE)


def _confirm_task_bash(req: ApprovalRequest) -> bool:
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.text import Text

    from omni.cli.render import console

    body = Text(_task_bash_confirmation_detail(req))
    console.print(
        Panel(
            body,
            title="Enable workspace trust for this task?",
            border_style="red",
            expand=False,
        )
    )
    answer = Prompt.ask(
        "Enable? [e] Enable for this task / [c] Cancel",
        choices=["e", "c"],
        default="c",
    )
    return answer == "e"


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

        with pause_clocks():
            threading.Thread(target=worker, name="omni-approval", daemon=True).start()
            return await fut

    approver._omni_manages_turn_clock = True  # type: ignore[attr-defined]
    return approver


def _tui_title(req: ApprovalRequest) -> str:
    _, label = _RISK_STYLE.get(req.risk, ("cyan", req.risk))
    return f"{label}: {req.tool_name}"


def build_tui_approver(tui: ReplTui) -> Approver:
    """Approve via the in-app modal so cancel never blocks on a worker thread.

    Falls back to suspending the inline dock and prompting on real stdin if
    the running TUI predates :meth:`ReplTui.request_approval`.
    """

    prompt_lock = asyncio.Lock()

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        async with prompt_lock:
            request = getattr(tui, "request_approval", None)
            if request is None:
                async with tui.suspended():
                    with pause_clocks():
                        return await asyncio.to_thread(_decide, req)

            from omni.cli.repl_tui import ApprovalOption

            options = tuple(ApprovalOption(choice.value, choice.label) for choice in _choices(req))

            with pause_clocks():
                while True:
                    choice = await request(_tui_title(req), req.detail or "", options=options)
                    if choice != _APPROVE_ALL_VALUE:
                        return _decision(choice)
                    confirm = await request(
                        "Enable workspace trust for this task?",
                        _task_bash_confirmation_detail(req),
                        options=(
                            ApprovalOption(_TASK_BASH_ENABLE, "Enable for this task"),
                            ApprovalOption(_TASK_BASH_CANCEL, "Cancel"),
                        ),
                        default=_TASK_BASH_CANCEL,
                    )
                    if confirm == _TASK_BASH_ENABLE:
                        return _decision(_APPROVE_ALL_VALUE)
                    if confirm == "deny":
                        return _decision("deny")

    approver._omni_manages_turn_clock = True  # type: ignore[attr-defined]
    return approver


__all__ = ["build_cli_approver", "build_tui_approver"]
