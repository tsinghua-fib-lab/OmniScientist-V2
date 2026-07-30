"""Generalized interactive confirmation for skill checkpoints.

Some skills pause and hand a decision back to the human: research-pptx asks the
user to approve an outline before it renders, scientific-poster asks for an exact
approval phrase before it publishes, and soulagent asks whether to distill a
scientist. Today each surfaces only as end-of-turn prose or an action card, so
the user has to notice it and hand-type the follow-up.

This module turns those signals into the redesign's *interactive confirmation
region*: it detects the checkpoint on a completed turn, presents one prompt (the
TUI approval modal, or a plain-terminal prompt in classic mode), and returns the
follow-up submission the user's choice implies -- the resume instruction, the
exact approval phrase, or a yes -- which the caller feeds back as the next turn.

Detection is deliberately conservative: an ordinary turn returns ``None`` and the
loop is untouched.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omni.cli.repl_tui import ReplTui

_MAX_DEPTH = 6


@dataclass(frozen=True)
class ConfirmationOption:
    """One choice in a confirmation prompt.

    ``submit`` is the user turn to enqueue when the option is chosen; an empty
    string means "just dismiss" (the user will type their own follow-up).
    """

    value: str
    label: str
    submit: str = ""


@dataclass(frozen=True)
class ConfirmationRequest:
    source: str
    title: str
    detail: str
    options: tuple[ConfirmationOption, ...] = field(default_factory=tuple)

    def option(self, value: str) -> ConfirmationOption | None:
        return next((opt for opt in self.options if opt.value == value), None)


def _walk_dicts(obj: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    """Yield every dict in a result tree, bounded so a cycle can't run away."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _walk_dicts(value, depth + 1)


def _result_dicts(turn: Any) -> list[dict[str, Any]]:
    """Every result dict reachable from a turn, most-recent source first.

    A skill checkpoint can arrive as a synchronous tool result (``tool_trace``)
    or a drained background task (``drained_results``); scanning the tool trace
    in reverse first means the newest checkpoint wins when several exist.
    """
    roots: list[Any] = []
    for record in reversed(list(getattr(turn, "tool_trace", []) or [])):
        result = getattr(record, "result", None)
        if isinstance(result, dict):
            roots.append(result)
    for drained in reversed(list(getattr(turn, "drained_results", []) or [])):
        if isinstance(drained, dict):
            roots.append(drained)
    return roots


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _detect_pptx(data: dict[str, Any]) -> ConfirmationRequest | None:
    outcome = data.get("outcome")
    code = str(outcome.get("code") or "") if isinstance(outcome, dict) else ""
    token = str(data.get("resume_token") or "").strip()
    if code != "awaiting_review" or not token:
        return None
    detail = _first_text(data, "summary", "message", "text") or (
        "The presentation outline is ready for your review before rendering."
    )
    resume = (
        f'Approved. Resume research-pptx with resume_token="{token}" and render '
        "the approved outline."
    )
    return ConfirmationRequest(
        source="research-pptx",
        title="Approve the presentation outline?",
        detail=detail,
        options=(
            ConfirmationOption("approve", "Approve and render", submit=resume),
            ConfirmationOption("deny", "Not yet"),
        ),
    )


def _detect_poster(data: dict[str, Any]) -> ConfirmationRequest | None:
    approval = data.get("approval")
    phrase = ""
    if isinstance(approval, dict):
        phrase = str(approval.get("operator_confirmation") or "").strip()
    if not phrase:
        return None
    detail = _first_text(data, "summary", "message", "text") or (
        "The poster draft is ready; approve it to author and export the final poster."
    )
    return ConfirmationRequest(
        source="scientific-poster",
        title="Approve the poster draft?",
        detail=detail,
        options=(
            ConfirmationOption("approve", "Approve poster", submit=phrase),
            ConfirmationOption("deny", "Not yet"),
        ),
    )


def _detect_soulagent(data: dict[str, Any]) -> ConfirmationRequest | None:
    action = data.get("action_required")
    if not isinstance(action, dict):
        return None
    if str(action.get("action") or "") != "confirm_scientist_distillation":
        return None
    detail = _first_text(data, "message", "summary", "text") or (
        "Distill this scientist into a soul knowledge graph?"
    )
    return ConfirmationRequest(
        source="soulagent",
        title="Distill this scientist?",
        detail=detail,
        options=(
            ConfirmationOption(
                "approve",
                "Yes, distill",
                submit="Yes, please proceed with the scientist distillation.",
            ),
            ConfirmationOption("deny", "No, cancel"),
        ),
    )


_DETECTORS = (_detect_pptx, _detect_poster, _detect_soulagent)


def detect_confirmation(turn: Any) -> ConfirmationRequest | None:
    """Recognize a skill checkpoint on a completed turn, or ``None`` for the rest."""
    for data in _result_dicts(turn):
        for node in _walk_dicts(data):
            for detector in _DETECTORS:
                request = detector(node)
                if request is not None:
                    return request
    return None


def _present_classic(request: ConfirmationRequest) -> str:
    """Prompt on a real terminal; never block a non-interactive session."""
    if not sys.stdin.isatty():
        return "deny"
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.text import Text

    from omni.cli.render import console

    body = Text()
    if request.detail:
        body.append(request.detail)
    console.print(Panel(body, title=request.title, border_style="cyan", expand=False))
    choices = [opt.value for opt in request.options]
    labels = "  ".join(f"[{opt.value}] {opt.label}" for opt in request.options)
    console.print(labels, style="dim")
    default = choices[0] if choices else "deny"
    return Prompt.ask("Your choice", choices=choices or None, default=default)


async def present_confirmation(request: ConfirmationRequest, *, tui: ReplTui | None) -> str:
    """Show the confirmation and return the chosen option value.

    Prefers the running TUI's approval modal (cancel-safe, in-dock); falls back
    to a plain-terminal prompt in classic mode.
    """
    ask = getattr(tui, "request_approval", None) if tui is not None else None
    if ask is not None:
        from omni.cli.repl_tui import ApprovalOption

        options = tuple(ApprovalOption(opt.value, opt.label) for opt in request.options)
        return await ask(request.title, request.detail, options=options)
    return _present_classic(request)


__all__ = [
    "ConfirmationOption",
    "ConfirmationRequest",
    "detect_confirmation",
    "present_confirmation",
]
