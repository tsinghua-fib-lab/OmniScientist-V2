"""`omni memory` — inspect and curate the memory store + notebook."""

from __future__ import annotations

import os
import subprocess
from contextlib import nullcontext

import typer

from omni.cli.render import console, data_table, error, info, one_line, success, warn
from omni.cli.state import AppState, make_agent, run_async
from omni.memory.notebook import append_entry, read_recent
from omni.memory.service import MemoryLayer

app = typer.Typer(help="Inspect and manage long-term memory and the research notebook.", no_args_is_help=True)

_MEMORY_SUBCOMMANDS = (
    "list", "search", "add", "pin", "detail", "graph", "link", "rm", "delete",
    "remove", "clear", "edit", "sync", "profile", "notebook", "path", "help",
)


def render_memory_usage_help() -> None:
    """Render memory command details for both shell and REPL users."""
    info("Use `/memory ...` in the REPL or `omni memory ...` in the shell.")
    info("In the REPL, `/memory <text>` performs semantic recall.")
    info(f"Available subcommands: {', '.join(_MEMORY_SUBCOMMANDS)}.")
    data_table(
        "memory subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "List recent memories; filter with --type or --layer", "/memory list --type preference"],
            ["search <q>", "Search memories semantically and by keyword", "/memory search RAG hallucination"],
            ["add <text>", "Record a long-term memory; use --pin to pin it", "/memory add \"Target venue is NeurIPS\" --pin"],
            ["pin <id>", "Pin or unpin a memory with --on or --off", "/memory pin 1a2b3c --on"],
            ["detail <id>", "Show complete memory content and provenance", "/memory detail 1a2b3c"],
            ["graph <id>", "Show cross-session graph neighbors with --depth N", "/memory graph 1a2b3c --depth 2"],
            ["link <src> <dst>", "Create a graph edge with --relation", "/memory link 1a2b3c 4d5e6f"],
            ["rm/delete/remove <id>", "Delete one memory; pinned entries require --force", "/memory rm 1a2b3c"],
            ["clear", "Bulk-delete by --type, --layer, or --scope; requires --yes", "/memory clear --type episode --yes"],
            ["edit", "Open the active Omni MEMORY.md in $EDITOR and sync saved bullets", "/memory edit"],
            ["sync", "Import marked lines from MEMORY.md and NOTEBOOK.md", "/memory sync"],
            ["profile", "Show the automatically distilled user profile", "/memory profile"],
            ["notebook", "View or append to NOTEBOOK.md", "/memory notebook"],
            ["path", "Show SQLite and curated memory paths", "/memory path"],
            ["help", "Show this help", "/memory help"],
        ],
    )
    info(
        "Memory entries live in the current workspace's sessions.sqlite3 memory_entries table. "
        "Personal preferences in the active Omni data directory's MEMORY.md are available across workspaces."
    )


@app.command("help")
def help_cmd() -> None:
    """Show memory subcommands and common examples."""
    render_memory_usage_help()


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    limit: int = typer.Option(20),
    page: int = typer.Option(1, "--page", "-p", help="Page number, starting at 1"),
    pager: bool = typer.Option(False, "--pager", help="Display through the terminal pager"),
    mem_type: str = typer.Option("", "--type", "-t", help="Filter by memory_type"),
    layer: str = typer.Option("", "--layer", "-l", help="Filter by layer (M1..M5)"),
) -> None:
    """List recent memory entries."""
    state: AppState = ctx.obj
    page = max(1, page)
    limit = max(1, limit)
    offset = (page - 1) * limit

    async def _run():
        agent = await make_agent(state)
        rows = await agent.memory.list_recent(
            limit=limit + 1,
            memory_type=mem_type,
            layer=layer,
            offset=offset,
        )
        await agent.aclose()
        return rows

    rows = run_async(_run())
    has_next = len(rows) > limit
    shown = rows[:limit]
    with (console.pager(styles=True) if pager else nullcontext()):
        data_table(f"Recent memories (page {page})", ["id", "layer", "type", "pinned", "summary"],
                   [[r.id[:8], r.layer, r.memory_type, "📌" if r.pinned else "",
                     one_line(r.summary, 72)] for r in shown])
    if shown:
        info(f"Showing {len(shown)} entries with truncated summaries. Use memory detail <id> or memory rm <id>.")
    if has_next:
        info(f"Next page: memory list --page {page + 1} --limit {limit}")


@app.command("search")
def search_cmd(
    ctx: typer.Context,
    query: str,
    limit: int = typer.Option(8),
    mem_type: str = typer.Option("", "--type", "-t", help="Filter by memory_type"),
) -> None:
    """Search memories semantically and by keyword."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        res = await agent.memory.recall(query, limit=limit * 2, cross_session=True)
        await agent.aclose()
        return res

    res = run_async(_run())
    if mem_type:
        res = [m for m in res if m.entry.memory_type == mem_type]
    res = res[:limit]
    data_table(f"Recall for '{query}'", ["id", "score", "layer", "type", "summary"],
               [[m.entry.id[:8], f"{m.score:.2f}", m.entry.layer, m.entry.memory_type,
                 one_line(m.entry.summary, 72)] for m in res])
    if res:
        info("Summaries are truncated. Use `/memory detail <id>` for full content or `/memory rm <id>` to delete.")
    info(
        "Search mirrors what the agent recalls, so it sees this session plus the "
        "cross-session layers. `/memory list --layer M1` shows every entry, including "
        "dialogue written by other sessions."
    )


@app.command("pin")
def pin_cmd(ctx: typer.Context, mem_id: str,
            on: bool = typer.Option(False, "--on", help="Pin the memory (the default action)"),
            off: bool = typer.Option(False, "--off", help="Unpin the memory")) -> None:
    """Pin or unpin a memory to control recall priority."""
    state: AppState = ctx.obj
    if on and off:
        error("Choose only one of --on and --off.")
        raise typer.Exit(1)
    pinned = not off

    async def _run():
        agent = await make_agent(state)
        ok = await agent.memory.set_pinned(mem_id, pinned)
        await agent.aclose()
        return ok

    if run_async(_run()):
        success(f"Memory {mem_id[:8]} was {'pinned' if pinned else 'unpinned'}.")
    else:
        error(f"Memory {mem_id} was not found.")
        raise typer.Exit(1)


@app.command("detail")
def detail_cmd(ctx: typer.Context, mem_id: str) -> None:
    """Show complete memory content and provenance."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        row = await agent.memory.get(mem_id)
        await agent.aclose()
        return row

    row = run_async(_run())
    if row is None:
        error(f"Memory {mem_id} was not found.")
        raise typer.Exit(1)
    console.print(f"[bold]{row.id}[/bold]  [{row.layer}/{row.memory_type}]"
                  f"  {'📌' if row.pinned else ''}")
    console.print(f"importance={row.importance:.2f}  scope={row.scope}/{row.scope_id}"
                  f"  recall={row.recall_count}")
    if row.payload_ref:
        console.print(f"provenance payload_ref: {row.payload_ref}")
    console.print(f"tags: {', '.join(row.tags) if row.tags else '—'}")
    console.rule(style="cyan")
    console.print(row.summary)


_GRAPH_RELATIONS = ("related", "same_topic", "derived_from", "contradicts")


@app.command("graph")
def graph_cmd(
    ctx: typer.Context,
    mem_id: str,
    depth: int = typer.Option(1, "--depth", "-d", help="Traversal depth (1 means direct neighbors)"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum number of neighbors"),
) -> None:
    """Show cross-session graph neighbors for a memory."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            row, status = await agent.memory.resolve(mem_id)
            if status == "ambiguous" or row is None:
                return status, None, []
            neigh = await agent.memory.graph.neighbors(
                row.id, depth=max(1, depth), limit=max(1, limit)
            )
            return "ok", row, neigh
        finally:
            await agent.aclose()

    status, row, neigh = run_async(_run())
    if status == "ambiguous":
        error(f"Prefix {mem_id} matches multiple memories; provide a longer ID.")
        raise typer.Exit(1)
    if row is None:
        error(f"Memory {mem_id} was not found.")
        raise typer.Exit(1)
    console.print(f"[bold]{row.id[:8]}[/bold]  [{row.layer}/{row.memory_type}]  {one_line(row.summary, 60)}")
    if not neigh:
        info("This memory has no graph neighbors yet. Add related memories or use memory link.")
        return
    data_table(
        f"Memory graph neighbors (depth <= {max(1, depth)})",
        ["id", "relation", "weight", "hop", "summary"],
        [[n.id[:8], n.relation, f"{n.weight:.2f}", str(n.depth), one_line(n.summary, 56)] for n in neigh],
    )
    info("Relations: related=semantic neighbor; same_topic=shared tag; derived_from/contradicts=manual annotations.")


@app.command("link")
def link_cmd(
    ctx: typer.Context,
    src: str,
    dst: str,
    relation: str = typer.Option("related", "--relation", "-r",
                                 help="related|same_topic|derived_from|contradicts"),
    weight: float = typer.Option(1.0, "--weight", "-w", help="Edge weight (0..1)"),
) -> None:
    """Create a cross-session graph edge between two memories."""
    state: AppState = ctx.obj
    if relation not in _GRAPH_RELATIONS:
        error(f"Unknown relation {relation!r}; choose from {', '.join(_GRAPH_RELATIONS)}.")
        raise typer.Exit(1)

    async def _run():
        agent = await make_agent(state)
        try:
            srow, ss = await agent.memory.resolve(src)
            drow, ds = await agent.memory.resolve(dst)
            if "ambiguous" in (ss, ds):
                return "ambiguous", None
            if srow is None or drow is None:
                return "not_found", None
            if srow.id == drow.id:
                return "same", None
            eid = await agent.memory.graph.add_edge(
                srow.id, drow.id, relation=relation, weight=weight, origin="manual"
            )
            return ("ok" if eid else "failed"), (srow, drow)
        finally:
            await agent.aclose()

    status, pair = run_async(_run())
    if status == "ambiguous":
        error("An ID prefix matches multiple memories; provide a longer ID.")
        raise typer.Exit(1)
    if status in ("not_found", "failed") or pair is None:
        error("A specified memory was not found. Use memory list to inspect valid IDs.")
        raise typer.Exit(1)
    if status == "same":
        error("Source and destination refer to the same memory; no edge was created.")
        raise typer.Exit(1)
    srow, drow = pair
    success(f"Linked {srow.id[:8]} -[{relation}]-> {drow.id[:8]}.")


@app.command("rm")
def rm_cmd(
    ctx: typer.Context,
    mem_id: str,
    force: bool = typer.Option(False, "--force", "-f", help="Also delete a pinned memory"),
) -> None:
    """Delete a memory by ID or unique prefix; pinned entries require --force."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            row, status = await agent.memory.resolve(mem_id)
            if status == "ambiguous":
                return "ambiguous", None
            if row is None:
                return "not_found", None
            if row.pinned and not force:
                return "pinned", row
            ok = await agent.memory.delete(row.id)
            return ("ok" if ok else "not_found"), row
        finally:
            await agent.aclose()

    status, row = run_async(_run())
    if status == "ambiguous":
        error(f"Prefix {mem_id} matches multiple memories; provide a longer ID.")
        raise typer.Exit(1)
    if status == "not_found" or row is None:
        error(f"Memory {mem_id} was not found.")
        raise typer.Exit(1)
    if status == "pinned":
        warn(f"Memory {row.id[:8]} is pinned; add --force to delete it.")
        raise typer.Exit(1)
    success(f"Deleted memory {row.id[:8]}: {one_line(row.summary, 48)}")


@app.command("delete")
def delete_cmd(
    ctx: typer.Context,
    mem_id: str,
    force: bool = typer.Option(False, "--force", "-f", help="Also delete a pinned memory"),
) -> None:
    """Delete a memory; alias for rm."""
    rm_cmd(ctx, mem_id, force=force)


@app.command("remove")
def remove_cmd(
    ctx: typer.Context,
    mem_id: str,
    force: bool = typer.Option(False, "--force", "-f", help="Also delete a pinned memory"),
) -> None:
    """Delete a memory; alias for rm."""
    rm_cmd(ctx, mem_id, force=force)


@app.command("clear")
def clear_cmd(
    ctx: typer.Context,
    mem_type: str = typer.Option("", "--type", "-t", help="Clear only this type"),
    layer: str = typer.Option("", "--layer", "-l", help="Clear only this layer"),
    scope: str = typer.Option("", "--scope", "-s", help="Clear only this scope"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete matching memories while always preserving pinned entries."""
    state: AppState = ctx.obj
    if not yes:
        filt = mem_type or layer or scope or "all unpinned entries"
        warn(f"This will delete memories matching {filt}; pinned entries remain. Add --yes to confirm.")
        raise typer.Exit(1)

    async def _run():
        agent = await make_agent(state)
        n = await agent.memory.clear(memory_type=mem_type, layer=layer, scope=scope)
        await agent.aclose()
        return n

    success(f"Deleted {run_async(_run())} memories.")


@app.command("sync")
def sync_cmd(ctx: typer.Context) -> None:
    """Import marked bullets from MEMORY.md and NOTEBOOK.md as memories."""
    state: AppState = ctx.obj

    async def _run():
        from omni.memory.files import import_curated_memory

        agent = await make_agent(state)
        n = await import_curated_memory(agent.paths, agent.memory)
        await agent.aclose()
        return n

    info(f"Imported {run_async(_run())} new pinned memories from curated files.")


@app.command("edit")
def edit_cmd(ctx: typer.Context) -> None:
    """Open ~/.omni/MEMORY.md in $EDITOR and import saved bullets.

    Human-readable, versionable files remain the source of curated preferences
    and facts. Lines beginning with ``-``, ``[pin]``, or ``!`` become pinned memories.
    """
    from omni.memory.files import user_memory_file

    state: AppState = ctx.obj
    paths = state.settings().paths
    mem_file = user_memory_file(paths)
    if not mem_file.exists():
        mem_file.parent.mkdir(parents=True, exist_ok=True)
        mem_file.write_text(
            "# Personal long-term memory (MEMORY.md)\n\n"
            "> Each `- item` is imported as a pinned preference. Lines marked with "
            "`[pin]` or a leading `!` are imported as pinned facts.\n\n"
            "- \n",
            encoding="utf-8",
        )
    editor = os.environ.get("OMNI_EDITOR") or os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        editor = "notepad" if os.name == "nt" else "vi"
    try:
        subprocess.run([*editor.split(), str(mem_file)], check=False)  # noqa: S603
    except (OSError, ValueError) as exc:
        error(f"Could not start editor '{editor}': {exc}")
        info(f"Edit {mem_file} manually, then run `omni memory sync`.")
        raise typer.Exit(1) from exc

    async def _run():
        from omni.memory.files import import_curated_memory

        agent = await make_agent(state)
        try:
            return await import_curated_memory(agent.paths, agent.memory)
        finally:
            await agent.aclose()

    n = run_async(_run())
    success(f"Saved {mem_file} and imported {n} new pinned memories.")


@app.command("path")
def path_cmd(ctx: typer.Context) -> None:
    """Show SQLite and human-editable Markdown memory paths."""
    from omni.memory.files import project_memory_files, user_memory_file

    paths = ctx.obj.settings().paths
    project_files = project_memory_files(paths)
    data_table(
        "Memory paths",
        ["item", "location"],
        [
            ["Workspace structured database", str(paths.project_db)],
            ["SQLite memory table", "memory_entries in the workspace database"],
            ["Personal long-term memory", str(user_memory_file(paths))],
            ["Project instructions and memory", "\n".join(str(p) for p in project_files) if project_files else "No workspace root for this named project"],
            ["Research notebook", str(paths.notebook)],
        ],
    )


@app.command("profile")
def profile_cmd(ctx: typer.Context) -> None:
    """Show the automatically distilled user profile."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        rows = await agent.memory.list_recent(limit=1, memory_type="user_profile")
        await agent.aclose()
        return rows

    rows = run_async(_run())
    if not rows:
        info("No user profile exists yet. Use the agent more or add preferences to MEMORY.md.")
        return
    console.print(rows[0].summary)


@app.command("add")
def add_cmd(ctx: typer.Context, text: str,
            pin: bool = typer.Option(False, "--pin", help="Pin for persistent recall"),
            importance: float = typer.Option(0.7)) -> None:
    """Record a long-term semantic memory manually."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        mid = await agent.memory.record(
            layer=MemoryLayer.SEMANTIC, scope="user", summary=text,
            memory_type="user_note", importance=importance, pinned=pin,
        )
        await agent.aclose()
        return mid

    mid = run_async(_run())
    success(f"Recorded memory {mid[:8]}{' (pinned)' if pin else ''}.")


@app.command("notebook")
def notebook_cmd(ctx: typer.Context,
                 add: str = typer.Option("", help="Append one entry as title:body")) -> None:
    """View or append to the research notebook."""
    p = ctx.obj.settings().paths
    if add:
        title, _, body = add.partition(":")
        append_entry(p.notebook, title.strip() or "Note", body.strip() or title.strip())
        success(f"Appended to {p.notebook}.")
        return
    from omni.cli.render import console
    console.print(read_recent(p.notebook, max_chars=4000) or "[dim](notebook is empty)[/dim]")
