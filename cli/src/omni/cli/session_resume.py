"""Resume orientation: session card + last N conversational turns.

Codex hydrates resume with ``HistoryHydrationScope::Initial`` and
``INITIAL_HISTORY_TURN_LIMIT = 5`` — newest turns, no tool re-execution.
Full transcript lives on another surface (Ctrl+T there, ``/replay`` here).
This module is display-only: it does not write sessions, compact, or change
the prompt history the model already loads via ``ConversationStore.history``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from omni.agent.conversation_store import ConversationStore
from omni.cli.render import assistant_answer, console, info, warn
from omni.cli.repl_output import get_output_sink, publish_transcript_event
from omni.cli.repl_transcript import TranscriptEvent, TranscriptKind, long_output_needs_folding
from omni.cli.timefmt import format_local_time
from omni.runtime.presentation import artifact_display_label

# Codex ``INITIAL_HISTORY_TURN_LIMIT``: bound *turns*, not one message's body.
INITIAL_HISTORY_TURN_LIMIT = 5
# Classic REPL has no Ctrl+T fold. Keep a generous cap so short answers stay
# intact; a manuscript still points at ``/replay``.
CLASSIC_ASSISTANT_LINE_CAP = 80
_BRIDGE_PREVIEW_CHARS = 240
_ARTIFACT_LIMIT = 3
_TITLE_CLIP = 60


@dataclass(frozen=True)
class ResumeView:
    """Read-only snapshot printed when a REPL binds an existing session."""

    session_id: str
    title: str
    updated_at: str
    stored_messages: int
    hidden_user_turns: int
    bridge_preview: str = ""
    messages: list[Any] = field(default_factory=list)
    task_id: str = ""
    task_status: str = ""
    task_title: str = ""
    task_active: bool = False
    artifacts: tuple[str, ...] = ()


def take_last_user_turns(
    rows: Sequence[Any],
    *,
    limit: int = INITIAL_HISTORY_TURN_LIMIT,
) -> tuple[list[Any], int]:
    """Keep messages from the last ``limit`` user turns.

    A turn starts at a user message. Earlier user turns are counted as hidden
    so the card can point at ``/replay`` instead of dumping them.
    """
    records = list(rows)
    if limit <= 0:
        return [], len([row for row in records if getattr(row, "role", "") == "user"])
    user_idxs = [i for i, row in enumerate(records) if getattr(row, "role", "") == "user"]
    if not user_idxs:
        return records[-(limit * 2) :], 0
    hidden = max(0, len(user_idxs) - limit)
    start = user_idxs[-limit] if hidden else user_idxs[0]
    return records[start:], hidden


def classic_assistant_preview(
    text: str,
    *,
    line_cap: int = CLASSIC_ASSISTANT_LINE_CAP,
) -> tuple[str, str]:
    """Return ``(shown, hint)`` for a classic REPL that cannot fold."""
    body = text or ""
    lines = body.splitlines()
    if len(lines) <= line_cap:
        return body, ""
    hidden = len(lines) - line_cap
    return "\n".join(lines[:line_cap]), f"{hidden} more lines"


async def collect_resume_view(agent: Any, session_id: str) -> ResumeView | None:
    """Load orientation + the last initial turns for ``session_id``."""
    sess = await agent.get_session(session_id)
    if sess is None:
        return None
    rows = await agent.session_messages(session_id)
    bridges = [
        row
        for row in rows
        if (row.content_type or "") == "compaction" and not (row.meta or {}).get("compacted")
    ]
    bridge = (bridges[-1].content or "").strip() if bridges else ""
    if len(bridge) > _BRIDGE_PREVIEW_CHARS:
        bridge = bridge[:_BRIDGE_PREVIEW_CHARS].rstrip() + "…"
    visible = ConversationStore.normal_rows(rows)
    messages, hidden = take_last_user_turns(visible)
    active = await agent.tasks.active_task_for_session(session_id)
    latest = active or await agent.tasks.latest_task_for_session(session_id)
    artifacts = await agent.artifacts.list_by_session(session_id, limit=_ARTIFACT_LIMIT)
    labels: list[str] = []
    for art in artifacts:
        kind = artifact_display_label(getattr(art, "kind", "") or "artifact")
        name = (getattr(art, "title", "") or getattr(art, "rel_path", "") or art.id[:8]).strip()
        labels.append(f"{kind} {name}" if name else kind)
    return ResumeView(
        session_id=sess.id,
        title=(sess.title or "").strip(),
        updated_at=format_local_time(sess.updated_at),
        stored_messages=len(rows),
        hidden_user_turns=hidden,
        bridge_preview=bridge,
        messages=messages,
        task_id=getattr(latest, "id", "") or "",
        task_status=getattr(latest, "status", "") or "",
        task_title=_clip(getattr(latest, "title", "") or "", _TITLE_CLIP),
        task_active=active is not None,
        artifacts=tuple(labels),
    )


def render_resume_view(view: ResumeView) -> None:
    """Print the orientation card and the bounded transcript tail."""
    title = view.title or "untitled"
    info(
        f"Resumed session {view.session_id[:8]} · {title} · "
        f"{view.stored_messages} messages · updated {view.updated_at}"
    )
    if view.task_id:
        extra = " · /steer /stop still apply" if view.task_active else ""
        task_title = view.task_title or "—"
        info(f"Last task {view.task_id[:8]} · {view.task_status} · {task_title}{extra}")
    if view.artifacts:
        info("This-session artifacts: " + " · ".join(view.artifacts))
    if view.bridge_preview:
        info(f"Earlier turns compacted: {view.bridge_preview}")
    sid = view.session_id[:8]
    if view.hidden_user_turns:
        info(
            f"{view.hidden_user_turns} earlier turn(s) omitted · "
            f"/replay {sid} for the full transcript including tools"
        )
    else:
        info(f"/replay {sid} shows tool traces · /task session · /context")
    _render_resume_messages(view.messages, session_prefix=sid)


async def render_session_resume(agent: Any, session_id: str) -> None:
    """Collect and print resume orientation for an already-bound session."""
    view = await collect_resume_view(agent, session_id)
    if view is None:
        warn(f"Session {session_id[:8]} was not found.")
        return
    render_resume_view(view)


def _render_resume_messages(messages: Sequence[Any], *, session_prefix: str) -> None:
    if not messages:
        console.print("[dim](session has no messages)[/dim]")
        return
    for message in messages:
        ts = format_local_time(getattr(message, "created_at", ""))
        role = getattr(message, "role", "")
        content = getattr(message, "content", "") or ""
        if role == "user":
            console.print(f"\n[bold green]› You[/bold green] [dim]{ts}[/dim]")
            console.print(content)
            continue
        if role == "assistant":
            tools = (getattr(message, "meta", None) or {}).get("tools") or []
            tag = f" [dim](tools: {', '.join(tools)})[/dim]" if tools else ""
            console.print(f"\n[bold cyan]› OmniScientist[/bold cyan] [dim]{ts}[/dim]{tag}")
            _render_assistant_body(content, session_prefix=session_prefix)
            continue
        name = getattr(message, "name", "") or ""
        console.print(f"\n[dim]› {role} {name}: {content[:300]}[/dim]")


def _render_assistant_body(text: str, *, session_prefix: str) -> None:
    """Full markdown in TUI (fold when long); classic truncates with /replay."""
    if get_output_sink() is not None:
        publish_transcript_event(
            TranscriptEvent(
                kind=TranscriptKind.MARKDOWN,
                payload=text,
                foldable=long_output_needs_folding(text),
            )
        )
        return
    shown, extra = classic_assistant_preview(text)
    assistant_answer(shown)
    if extra:
        console.print(f"[dim]… {extra} · /replay {session_prefix}[/dim]")


def _clip(text: str, limit: int) -> str:
    value = " ".join(text.split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


__all__ = [
    "CLASSIC_ASSISTANT_LINE_CAP",
    "INITIAL_HISTORY_TURN_LIMIT",
    "ResumeView",
    "classic_assistant_preview",
    "collect_resume_view",
    "render_resume_view",
    "render_session_resume",
    "take_last_user_turns",
]
