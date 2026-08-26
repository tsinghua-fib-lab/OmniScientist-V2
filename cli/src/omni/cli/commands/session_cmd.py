"""`omni session` — list, inspect, resume, export, and delete research sessions."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from omni.cli.render import data_table, error, info, success, warn
from omni.cli.runner import run_one_shot
from omni.cli.state import AppState, make_agent, run_async
from omni.cli.timefmt import format_local_iso, format_local_time

app = typer.Typer(help="Inspect, resume, fork, export, and delete sessions.", no_args_is_help=True)


@app.command("list")
def list_cmd(ctx: typer.Context, limit: int = typer.Option(20, help="Maximum sessions to show.")) -> None:
    """List sessions for the current project, newest first."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            return await agent.list_sessions(limit=limit)
        finally:
            await agent.aclose()

    rows = run_async(_run())
    data_table(
        "Sessions",
        ["id", "title", "channel", "updated", "msgs"],
        [
            [s.id[:8], (s.title or "-")[:40], s.channel, format_local_time(s.updated_at), n]
            for s, n in rows
        ]
        or [["(none)", "", "", "", ""]],
    )


@app.command("show")
def show_cmd(ctx: typer.Context, session: str) -> None:
    """Show the full transcript for a session."""
    from omni.cli.commands.replay_cmd import render_transcript

    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            resolution = await agent.resolve_session(session)
            if resolution.status != "ok" or resolution.row is None:
                return None, [], resolution.error_message(session)
            return resolution.row, await agent.session_messages(resolution.row.id), ""
        finally:
            await agent.aclose()

    sess, msgs, message = run_async(_run())
    if sess is None:
        error(message or f"Session {session} was not found.")
        raise typer.Exit(1)
    info(f"Session {sess.id[:8]} · {sess.channel} · {(sess.title or 'untitled')} · {len(msgs)} messages")
    render_transcript(msgs)


@app.command("resume")
def resume_cmd(
    ctx: typer.Context,
    session: str,
    prompt: list[str] = typer.Argument(None, help="Optional prompt to continue this session."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Hide tool progress."),
) -> None:
    """Resume a session once or enter interactive mode."""
    state: AppState = ctx.obj or AppState()

    async def _resolve():
        agent = await make_agent(state)
        try:
            resolution = await agent.resolve_session(session)
            if resolution.status != "ok" or resolution.row is None:
                return None, resolution.error_message(session)
            await agent.touch_session(resolution.row.id)
            return resolution.row.id, ""
        finally:
            await agent.aclose()

    sid, message = run_async(_resolve())
    if sid is None:
        error(message or f"Session {session} was not found.")
        raise typer.Exit(1)

    text = " ".join(prompt).strip() if prompt else ""
    if text:
        run_async(run_one_shot(state, text, quiet=quiet, session_id=sid))
        return
    # No inline prompt → drop into the REPL bound to this session.
    from omni.cli.main import _repl

    _repl(state, resume_session_id=sid)


@app.command("fork")
def fork_cmd(
    ctx: typer.Context,
    session: str,
    prompt: list[str] = typer.Argument(None, help="Optional prompt to continue the fork."),
    up_to: str = typer.Option("", "--up-to", help="Copy history only through this message ID/prefix."),
    title: str = typer.Option("", "--title", help="Fork title; defaults to the original title plus '(fork)'."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Hide tool progress."),
) -> None:
    """Fork a session into an independent conversation branch."""
    state: AppState = ctx.obj or AppState()

    async def _run():
        agent = await make_agent(state)
        try:
            new_id = await agent.fork_session(session, up_to_message=up_to, title=title)
            return new_id
        finally:
            await agent.aclose()

    new_id = run_async(_run())
    if new_id is None:
        error(f"Session {session} was not found.")
        raise typer.Exit(1)
    success(f"Forked session {session} into independent session {new_id[:8]}.")

    text = " ".join(prompt).strip() if prompt else ""
    if text:
        run_async(run_one_shot(state, text, quiet=quiet, session_id=new_id))
        return
    from omni.cli.main import _repl

    _repl(state, resume_session_id=new_id)


@app.command("export")
def export_cmd(
    ctx: typer.Context,
    session: str,
    output: str = typer.Option("", "--output", "-o", help="Output file; defaults to stdout."),
    fmt: str = typer.Option("md", "--format", "-f", help="md | json"),
) -> None:
    """Export a session as Markdown or JSON."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            sess = await agent.get_session(session)
            if sess is None:
                return None, []
            return sess, await agent.session_messages(sess.id)
        finally:
            await agent.aclose()

    sess, msgs = run_async(_run())
    if sess is None:
        error(f"Session {session} was not found.")
        raise typer.Exit(1)

    if fmt == "json":
        payload = {
            "id": sess.id,
            "title": sess.title,
            "channel": sess.channel,
            "messages": [
                {"role": m.role, "content": m.content, "name": m.name,
                 "created_at": format_local_iso(m.created_at), "meta": m.meta}
                for m in msgs
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = _to_markdown(sess, msgs)

    if output:
        out_path = Path(output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        success(f"Exported session {sess.id[:8]} to {out_path}")
    else:
        from omni.cli.render import console

        console.print(text)


@app.command("rm")
def rm_cmd(
    ctx: typer.Context,
    session: str,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm deletion when the session has associated tasks"
    ),
) -> None:
    """Delete a session, its transcript, and the tasks that belong to it.

    Artifact files are kept (same as ``/task rm``). A running or recovering
    task blocks deletion.
    """
    state: AppState = ctx.obj

    async def _preview():
        agent = await make_agent(state)
        try:
            sess = await agent.get_session(session)
            if sess is None:
                return None, 0, []
            msgs = await agent.session_messages(sess.id)
            tasks = await agent.tasks.list_tasks_for_session(sess.id)
            return sess, len(msgs), tasks
        finally:
            await agent.aclose()

    sess, n_msgs, tasks = run_async(_preview())
    if sess is None:
        error(f"Session {session} was not found.")
        raise typer.Exit(1)
    if tasks and not yes:
        info(
            f"Session {sess.id[:8]} · {sess.channel} · {n_msgs} messages · "
            f"{len(tasks)} task(s). Deleting the session also deletes those "
            "tasks; artifact files are kept."
        )
        warn("Re-run with --yes to confirm.")
        raise typer.Exit(0)

    async def _delete():
        agent = await make_agent(state)
        try:
            return await agent.delete_session(sess.id)
        finally:
            await agent.aclose()

    outcome = run_async(_delete())
    if not outcome.deleted:
        error(outcome.message or f"Could not delete session {sess.id[:8]}.")
        raise typer.Exit(1)
    extra = (
        f" and {len(outcome.deleted_task_ids)} task(s)"
        if outcome.deleted_task_ids
        else ""
    )
    success(f"Deleted session {outcome.session_id[:8]}{extra}.")


@app.command("delete")
def delete_cmd(
    ctx: typer.Context,
    session: str,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm deletion when the session has associated tasks"
    ),
) -> None:
    """Alias for ``session rm``."""
    rm_cmd(ctx, session, yes=yes)


@app.command("help")
def help_cmd() -> None:
    """Show session subcommands and common examples (`/session help` in the REPL)."""
    data_table(
        "Session subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "List sessions for the current project, newest first", "/session list"],
            ["show <id>", "Show the full transcript for a session", "/session show 1a2b3c"],
            ["resume <id> [prompt]", "Resume a session once, or enter interactive mode", "/session resume 1a2b3c"],
            ["fork <id> [prompt]", "Fork a session into an independent conversation branch", "/session fork 1a2b3c --up-to 9f8e"],
            ["export <id>", "Export a session as Markdown or JSON", "/session export 1a2b3c -f json -o out.json"],
            ["rm/delete <id>", "Delete a session and its tasks; --yes if it has tasks", "/session rm 1a2b3c --yes"],
            ["help", "Show this session command reference", "/session help"],
        ],
    )
    info("Options: `--up-to <msg>` / `--title` on fork; `--format md|json` and `--output <file>` on export; `--quiet` on resume/fork; `--yes` on rm.")


def _to_markdown(sess, msgs) -> str:  # noqa: ANN001
    lines = [f"# Session {sess.id[:8]} - {sess.title or 'untitled'}", "",
             f"- channel: {sess.channel}", f"- created: {format_local_time(sess.created_at)}", ""]
    for m in msgs:
        who = {"user": "You", "assistant": "OmniScientist"}.get(m.role, m.role)
        lines += [f"## {who}", "", (m.content or "").rstrip(), ""]
    return "\n".join(lines)
