"""`omni mcp` — run the MCP server bridge and register with Codex / Claude Code."""

from __future__ import annotations

import typer

from omni.cli.render import data_table, error, info, success
from omni.cli.state import AppState, run_async

app = typer.Typer(help="Expose Omni tools to Claude Code/Codex or connect external MCP servers.",
                  no_args_is_help=True)


@app.command("serve")
def serve_cmd(ctx: typer.Context) -> None:
    """Run the OmniScientist MCP server over stdio."""
    state: AppState = ctx.obj
    from omni.compat.mcp_server import serve_stdio

    try:
        run_async(serve_stdio(state.settings()))
    except RuntimeError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        pass


@app.command("install")
def install_cmd(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="codex | claude | both"),
) -> None:
    """Register `omni mcp serve` with Codex or Claude Code."""
    from omni.compat.integrations import register_with_claude, register_with_codex

    targets = ["codex", "claude"] if target == "both" else [target]
    for t in targets:
        if t == "codex":
            path = register_with_codex()
            success(f"Registered with Codex: {path}")
        elif t == "claude":
            path = register_with_claude()
            success(f"Registered with Claude Code: {path}")
        else:
            error(f"Unknown target: {t}; expected codex, claude, or both")
            raise typer.Exit(1)
    info("`omni_ask` and the research skills are now available in Codex or Claude Code.")


@app.command("uninstall")
def uninstall_cmd(
    target: str = typer.Argument("both", help="codex | claude | both"),
) -> None:
    """Remove Omni's MCP registration without touching other MCP servers."""
    from omni.compat.integrations import unregister_with_claude, unregister_with_codex

    targets = ["codex", "claude"] if target == "both" else [target]
    removed = 0
    for name in targets:
        if name == "codex":
            path, changed = unregister_with_codex()
        elif name == "claude":
            path, changed = unregister_with_claude()
        else:
            error(f"Unknown target: {name}; expected codex, claude, or both")
            raise typer.Exit(1)
        if changed:
            success(f"Removed OmniScientist MCP registration from {name}: {path}")
            removed += 1
        else:
            info(f"No OmniScientist MCP registration was found for {name}: {path}")
    if removed:
        info("Other MCP server registrations were preserved.")


@app.command("list")
def list_cmd(ctx: typer.Context) -> None:
    """List configured external MCP servers."""
    s = ctx.obj.settings()
    if not s.mcp_servers:
        info("No external MCP servers are configured. Add `[mcp_servers.<name>]` to the config.")
        return
    data_table("External MCP servers", ["name", "command/url", "enabled"],
               [[n, c.command or c.url, c.enabled] for n, c in s.mcp_servers.items()])


@app.command("agents")
def agents_cmd(ctx: typer.Context) -> None:
    """Write a shared AGENTS.md guide at the project root."""
    from omni.compat.integrations import emit_agents_md

    p = ctx.obj.settings().paths
    target = p.project_dir.parent if p.project_dir.name == ".omni" else p.project_dir
    path = emit_agents_md(target)
    success(f"Wrote {path}")


@app.command("help")
def help_cmd() -> None:
    """Show MCP subcommands and common examples (`/mcp help` in the REPL)."""
    data_table(
        "MCP subcommands",
        ["command", "purpose", "example"],
        [
            ["serve", "Run the OmniScientist MCP server over stdio", "omni mcp serve"],
            ["install <codex|claude|both>", "Register `omni mcp serve` with Codex and/or Claude Code", "/mcp install both"],
            ["uninstall <codex|claude|both>", "Remove Omni's MCP registration (other servers untouched)", "/mcp uninstall codex"],
            ["list", "List configured external MCP servers", "/mcp list"],
            ["agents", "Write a shared AGENTS.md guide at the project root", "/mcp agents"],
            ["help", "Show this MCP command reference", "/mcp help"],
        ],
    )
    info("`install` exposes `omni_ask` and the research skills inside Codex or Claude Code; add external servers under `[mcp_servers.<name>]` in config.")
