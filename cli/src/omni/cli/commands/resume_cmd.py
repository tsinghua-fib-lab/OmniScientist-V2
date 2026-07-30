"""`omni resume` — continue a previous session in the current workspace.

Like Claude's ``--resume``/``--continue``, resume is *per-workspace*: it lists
this workspace's sessions (path-keyed, so every terminal in the repo sees the
same list) and drops you back into the REPL bound to the one you pick. Use
``omni task all`` / the workspace catalog (registry ∪ channel anchor ∪ named
projects) for cross-workspace discovery.
"""

from __future__ import annotations

import sys

import typer

from omni.cli.render import console, data_table, error, info, warn
from omni.cli.state import AppState, make_agent, run_async
from omni.cli.timefmt import format_local_time

app = typer.Typer(help="Resume workspace sessions by selection, ID, or --last.")
_RESUME_SUBCOMMANDS = ("<id>", "--last", "help")


async def _list_sessions(state: AppState, limit: int):
    agent = await make_agent(state)
    try:
        return await agent.list_sessions(limit=limit)
    finally:
        await agent.aclose()


async def _resolve(state: AppState, session: str) -> str | None:
    agent = await make_agent(state)
    try:
        sess = await agent.get_session(session)
        if sess is None:
            return None
        await agent.touch_session(sess.id)
        return sess.id
    finally:
        await agent.aclose()


def resolve_last(state: AppState) -> str | None:
    """Most-recently-updated session id in this workspace (sync helper)."""

    async def _run():
        agent = await make_agent(state)
        try:
            rows = await agent.list_sessions(limit=1)
            if not rows:
                return None
            sid = rows[0][0].id
            await agent.touch_session(sid)
            return sid
        finally:
            await agent.aclose()

    return run_async(_run())


def _pick(state: AppState) -> str | None:
    rows = run_async(_list_sessions(state, 30))
    if not rows:
        info("This workspace has no previous sessions. Run `omni` to start one.")
        return None
    data_table(
        "Session history (current workspace)",
        ["#", "id", "title", "updated", "msgs"],
        [[str(i), s.id[:8], (s.title or "-")[:40], format_local_time(s.updated_at), n]
         for i, (s, n) in enumerate(rows, 1)],
    )
    if not sys.stdin.isatty():
        info("In non-interactive use, run `omni resume <id>` or `omni resume --last`.")
        return None
    raw = console.input("Select a session by number or ID prefix; press Enter to cancel: ").strip()
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(rows):
            return rows[idx - 1][0].id
        warn("Selection is out of range.")
        return None
    return run_async(_resolve(state, raw))


def render_resume_usage_help() -> None:
    """Render resume command details."""
    info("Use `/resume ...` in the REPL or `omni resume ...` in the shell.")
    info(f"Available forms: {', '.join(_RESUME_SUBCOMMANDS)}.")
    data_table(
        "resume usage",
        ["form", "purpose", "example"],
        [
            ["resume", "Select a session interactively", "/resume"],
            ["resume <id>", "Resume by ID or unique prefix", "/resume 1a2b3c4d"],
            ["resume --last", "Resume the most recent workspace session", "/resume --last"],
            ["resume --thread <hyp>", "Resume a research thread by hypothesis ID", "/resume --thread 1a2b3c4d"],
            ["session list", "List sessions without resuming", "/session list"],
            ["replay <id>", "Replay session messages and tool activity", "/replay 1a2b3c4d"],
        ],
    )
    info(
        "Resume is workspace-scoped; use `/task all` for catalog workspaces "
        "(incl. the IM channel anchor)."
    )


@app.command("help")
def help_cmd() -> None:
    """Show resume usage and examples."""
    render_resume_usage_help()


async def _thread_resume(state: AppState, hyp: str) -> tuple[str | None, str | None]:
    """Return (thread_brief, latest_session_id) for a hypothesis thread."""
    from omni.research.store import ResearchStore
    from omni.research.threads import build_thread_brief, latest_thread_session

    agent = await make_agent(state)
    try:
        store = ResearchStore(agent.db)
        brief = await build_thread_brief(store, hyp)
        sid = await latest_thread_session(store, hyp) if brief else None
        return brief, sid
    finally:
        await agent.aclose()


@app.callback(invoke_without_command=True)
def resume(
    ctx: typer.Context,
    session: str = typer.Argument(None, help="Session ID or prefix; omit for interactive selection."),
    last: bool = typer.Option(False, "--last", "-l", help="Resume the most recent session."),
    thread: str = typer.Option("", "--thread", "-t", help="Resume by research thread hypothesis ID."),
) -> None:
    """Resume a previous session in interactive mode."""
    state: AppState = ctx.obj or AppState()

    if session in {"help", "--help", "-h"} and not last:
        render_resume_usage_help()
        raise typer.Exit()

    if thread:
        brief, sid = run_async(_thread_resume(state, thread))
        if brief is None:
            error(f"Research thread (hypothesis) {thread} was not found.")
            raise typer.Exit(1)
        console.print(brief)
        console.rule(style="cyan")
        if sid is None:
            info("This research thread has no associated session; starting a new one.")
            from omni.cli.main import _repl
            _repl(state)
            raise typer.Exit()
        sid = run_async(_resolve(state, sid)) or sid
        from omni.cli.main import _repl
        _repl(state, resume_session_id=sid)
        raise typer.Exit()

    if last:
        sid = resolve_last(state)
        if sid is None:
            info("This workspace has no previous sessions.")
            raise typer.Exit()
    elif session:
        sid = run_async(_resolve(state, session))
        if sid is None:
            error(f"Session {session} was not found.")
            raise typer.Exit(1)
    else:
        sid = _pick(state)
        if sid is None:
            raise typer.Exit()

    from omni.cli.main import _repl

    _repl(state, resume_session_id=sid)
