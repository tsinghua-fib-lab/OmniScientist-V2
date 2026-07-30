"""`omni trust` — manage per-directory workspace trust (Claude Code style).

Trusting a directory lets omni write generated files there and apply its
repo-local ``.omni`` config; the decision persists in ``~/.omni/trust.json``
(never inside a repo) and inherits downward.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from omni.cli.render import data_table, error, info, success
from omni.config import trust as trustmod
from omni.config.paths import user_home


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except (OSError, OverflowError, ValueError):
        return "-"

app_help = "Trust the current directory, or list/revoke trusted directories."


def trust_command(
    path: str = typer.Argument("", help="Directory to trust (default: current directory)."),
    list_: bool = typer.Option(False, "--list", help="List trusted directories."),
    revoke: str = typer.Option("", "--revoke", help="Remove a trusted directory (path)."),
) -> None:
    """Manage which directories omni is allowed to write into."""
    home = user_home()

    if list_:
        rows = trustmod.list_trusted(home)
        if not rows:
            info("No trusted directories yet. Run `omni trust` in a folder to trust it.")
            return
        data_table(
            "Trusted directories",
            ["path", "trusted at"],
            [[r["path"], _fmt_ts(r["ts"])] for r in rows],
        )
        return

    if revoke:
        if trustmod.revoke(revoke, home=home):
            success(f"Revoked trust: {revoke}")
        else:
            error(f"Not a trusted directory: {revoke}")
        return

    target = Path(path).expanduser() if path else Path.cwd()
    key = trustmod.set_trusted(target, home=home)
    success(f"Trusted {key}")
    info("Generated files will be written here; this folder's .omni config now applies.")
