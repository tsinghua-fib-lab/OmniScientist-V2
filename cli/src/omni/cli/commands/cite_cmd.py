"""`omni cite` — browse and export the project reference library.

The library (``<project>/library.jsonl``) is populated automatically as
``arxiv-fetch`` / ``openalex-search`` resolve papers. ``cite export`` renders
it to BibTeX / JSON / CSV for use in a manuscript.
"""

from __future__ import annotations

from pathlib import Path

import typer

from omni.cli.render import console, data_table, info, success, warn
from omni.memory.library import load_library, to_bibtex, to_csv

app = typer.Typer(help="Browse the literature library and export citations.", no_args_is_help=True)


@app.command("help")
def help_cmd() -> None:
    """Show citation commands and examples."""
    info("Use `/cite ...` in the REPL or `omni cite ...` in the shell.")
    data_table(
        "cite subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "List the current workspace literature", "omni cite list --limit 20"],
            ["export", "Export BibTeX, JSON, or CSV", "omni cite export --format bibtex --output refs.bib"],
        ],
    )


@app.command("list")
def list_cmd(ctx: typer.Context, limit: int = typer.Option(30)) -> None:
    """List papers in the literature library."""
    p = ctx.obj.settings().paths
    entries = load_library(p.library)
    if not entries:
        warn(f"The literature library is empty ({p.library}). Search or fetch papers to populate it.")
        return
    data_table(
        f"Literature library ({len(entries)} papers)",
        ["arxiv_id", "year", "title", "authors"],
        [
            [
                e.get("arxiv_id", "") or "-",
                e.get("year", "") or "-",
                (e.get("title", "") or "")[:50],
                ", ".join((e.get("authors") or [])[:2]) + (" et al." if len(e.get("authors") or []) > 2 else ""),
            ]
            for e in entries[:limit]
        ],
    )


@app.command("export")
def export_cmd(
    ctx: typer.Context,
    fmt: str = typer.Option("bibtex", "--format", "-f", help="bibtex | json | csv"),
    output: str = typer.Option("", "--output", "-o", help="Output file; defaults to stdout."),
) -> None:
    """Export the literature library as BibTeX, JSON, or CSV."""
    p = ctx.obj.settings().paths
    entries = load_library(p.library)
    if not entries:
        warn(f"The literature library is empty ({p.library}). Search or fetch papers first.")
        raise typer.Exit(0)

    fmt = fmt.lower()
    if fmt == "bibtex":
        text = to_bibtex(entries)
    elif fmt == "csv":
        text = to_csv(entries)
    elif fmt == "json":
        import json

        text = json.dumps(entries, ensure_ascii=False, indent=2)
    else:
        raise typer.BadParameter("format must be one of: bibtex, json, csv")

    if output:
        out_path = Path(output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        success(f"Exported {len(entries)} papers to {out_path}")
    else:
        console.print(text)
        info(f"{len(entries)} papers total; use -o to save them to a file")
