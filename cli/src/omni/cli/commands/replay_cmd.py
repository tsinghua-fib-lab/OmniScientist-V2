"""`omni replay <session>` — replay a session's message + tool trace."""

from __future__ import annotations

import typer

from omni.cli.render import assistant_answer, console, error, info
from omni.cli.state import AppState, make_agent, run_async
from omni.cli.timefmt import format_local_time


def replay_command(
    ctx: typer.Context,
    session: str = typer.Argument(..., help="Session ID or unique prefix."),
    raw: bool = typer.Option(False, "--raw", help="Print plain text without Markdown rendering."),
) -> None:
    """Replay a research session in chronological order."""
    state: AppState = ctx.obj or AppState()

    async def _run():
        agent = await make_agent(state)
        try:
            sess = await agent.get_session(session)
            if sess is None:
                return None, []
            msgs = await agent.session_messages(sess.id)
            return sess, msgs
        finally:
            await agent.aclose()

    sess, msgs = run_async(_run())
    if sess is None:
        error(f"Session {session} was not found.")
        raise typer.Exit(1)
    title = sess.title or "(untitled)"
    info(f"Session {sess.id[:8]} · {sess.channel} · {title} · {len(msgs)} messages")
    console.rule(style="cyan")
    render_transcript(msgs, raw=raw)


def render_transcript(messages, *, raw: bool = False) -> None:  # noqa: ANN001
    """Render stored user/assistant messages in order (shared with `session show`)."""
    if not messages:
        console.print("[dim](session has no messages)[/dim]")
        return
    for m in messages:
        ts = format_local_time(getattr(m, "created_at", ""))
        if m.role == "user":
            console.print(f"\n[bold green]› You[/bold green] [dim]{ts}[/dim]")
            console.print(m.content or "")
        elif m.role == "assistant":
            tools = (m.meta or {}).get("tools") or []
            tag = f" [dim](tools: {', '.join(tools)})[/dim]" if tools else ""
            console.print(f"\n[bold cyan]› OmniScientist[/bold cyan] [dim]{ts}[/dim]{tag}")
            if raw:
                console.print(m.content or "")
            else:
                assistant_answer(m.content or "")
        else:  # tool/system — usually not persisted, shown dimmed if present
            console.print(f"\n[dim]› {m.role} {m.name}: {(m.content or '')[:300]}[/dim]")
