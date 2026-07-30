"""`omni project` — manage research project workspaces."""

from __future__ import annotations

import tomllib

import tomli_w
import typer

from omni.cli.render import data_table, info, success
from omni.config.paths import get_paths, user_home

app = typer.Typer(help="Manage research projects and workspaces.", no_args_is_help=True)


@app.command("help")
def help_cmd() -> None:
    """Show project commands and examples."""
    info("Use `/project ...` in the REPL or `omni project ...` in the shell.")
    data_table(
        "project subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "List named projects and path workspaces", "omni project list"],
            ["new <name>", "Create a named research project", "omni project new rag-study --description \"RAG hallucination mitigation\""],
            ["info", "Show project, database, notebook, and artifact paths", "omni project info"],
        ],
    )


@app.command("list")
def list_cmd(ctx: typer.Context) -> None:
    """List named projects and path-keyed workspaces."""
    home = user_home()
    active_dir = str(ctx.obj.settings().paths.project_dir)
    rows: list[list[str]] = []
    for kind, base in (("named", home / "projects"), ("path", home / "workspaces")):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            mark = "●" if str(d) == active_dir else " "
            rows.append([mark, kind, d.name,
                         "✓" if (d / "sessions.sqlite3").exists() else "—",
                         "✓" if (d / "NOTEBOOK.md").exists() else "—"])
    active = ctx.obj.settings().paths.project_name
    data_table(f"Projects and workspaces (active: {active})",
               ["", "kind", "name", "has-db", "notebook"],
               rows or [["", "", "(none)", "", ""]])


@app.command("new")
def new_cmd(ctx: typer.Context, name: str,
            description: str = typer.Option("", help="Project description.")) -> None:
    """Create a project workspace."""
    paths = get_paths(project=name)
    paths.ensure_dirs()
    cfg = paths.project_config
    data = {"omni": {"project": {"name": name, "description": description}}}
    with cfg.open("wb") as fh:
        tomli_w.dump(data, fh)
    if not paths.notebook.exists():
        paths.notebook.write_text(f"# {name} - Research notebook\n\n{description}\n", encoding="utf-8")
    success(f"Created project '{name}' at {paths.project_dir}")
    info(f"Use: omni --project {name} \"your research question\"")


@app.command("info")
def info_cmd(ctx: typer.Context) -> None:
    """Show current project information."""
    p = ctx.obj.settings().paths
    meta = {}
    if p.project_config.is_file():
        meta = tomllib.loads(p.project_config.read_text())
    data_table("Current project", ["field", "value"], [
        ["name", p.project_name],
        ["dir", str(p.project_dir)],
        ["db", str(p.project_db)],
        ["notebook", str(p.notebook)],
        ["artifacts", str(p.artifacts_dir)],
        ["config", str(meta)],
    ])
