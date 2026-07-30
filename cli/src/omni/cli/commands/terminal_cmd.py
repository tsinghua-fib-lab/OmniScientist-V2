"""Terminal keyboard diagnostics and explicit, reversible setup."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer

from omni.cli.render import console, data_table, info, success, warn
from omni.cli.terminal_harness import (
    TerminalReport,
    apply_tmux_setup,
    inspect_terminal,
    plan_tmux_setup,
    reload_tmux_config,
)

app = typer.Typer(
    help="Inspect and configure terminal keyboard support.",
    no_args_is_help=False,
    invoke_without_command=True,
)


def _status_rows(report: TerminalReport) -> list[list[str]]:
    tmux_detail = "not active"
    if report.tmux.active:
        tmux_detail = (
            f"{report.tmux.version or 'tmux'}; extended-keys="
            f"{report.tmux.extended_keys or 'unknown'}; format="
            f"{report.tmux.extended_keys_format or 'unknown'}; allow-passthrough="
            f"{report.tmux.allow_passthrough or 'unknown'}"
        )
    keyboard = "ready" if report.shift_enter_ready else "needs setup"
    detail = "Shift+Enter is distinguishable from Enter; repair: omni terminal setup"
    if report.issues:
        detail = "Shift+Enter needs setup: " + "; ".join(report.issues) + f"; run `{report.repair_command}`"
    return [
        ["Platform", report.platform, report.term],
        ["Host terminal", report.host_terminal, "interactive" if report.interactive else "not a TTY"],
        ["tmux", "active" if report.tmux.active else "inactive", tmux_detail],
        ["Extended keyboard", keyboard, detail],
        ["Portable fallback", report.fallback_shortcut, "inserts chat:newline on every supported OS"],
    ]


def _show_status(report: TerminalReport | None = None) -> TerminalReport:
    current = report or inspect_terminal()
    data_table("Terminal keyboard", ["check", "status", "detail"], _status_rows(current))
    return current


@app.callback()
def terminal_root(ctx: typer.Context) -> None:
    """Show terminal status when no subcommand is supplied."""
    if ctx.invoked_subcommand is None:
        _show_status()


@app.command("status")
def status_command() -> None:
    """Show host, tmux, and extended-key readiness."""
    _show_status()


@app.command("setup")
def setup_command(
    check: bool = typer.Option(False, "--check", help="Inspect only; do not offer changes."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the displayed change without prompting."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the proposed tmux block only."),
    tmux_config: Path | None = typer.Option(
        None,
        "--tmux-config",
        help="Advanced: configure this tmux file instead of ~/.tmux.conf.",
    ),
) -> None:
    """Enable Shift+Enter safely; tmux edits are confirmed and backed up."""
    report = _show_status()
    if check:
        return

    explicit_path = tmux_config is not None
    if not explicit_path and not report.tmux.available and shutil.which("tmux") is None:
        info(
            "tmux is not installed. Omni will negotiate modified keys with the host terminal; "
            "use Ctrl+J if Shift+Enter is not exposed by that terminal."
        )
        return

    plan = plan_tmux_setup(tmux_config)
    if not plan.changed:
        success(f"Terminal keyboard configuration is already current: {plan.path}")
        return

    console.print("\n[bold]Proposed tmux configuration[/bold]")
    console.print(plan.updated[len(plan.original):] if plan.updated.startswith(plan.original) else plan.updated)
    if dry_run:
        info("Dry run only; no terminal configuration was changed.")
        return
    if not yes and not typer.confirm(f"Update {plan.path}?", default=False):
        info("No terminal configuration was changed.")
        return

    result = apply_tmux_setup(plan)
    success(f"Updated terminal keyboard configuration: {result.path}")
    if result.backup_path is not None:
        info(f"Backup: {result.backup_path}")
    reloaded, detail = reload_tmux_config(result.path)
    (success if reloaded else warn)(detail)
    info("Restart the host terminal outside tmux if Shift+Enter is still indistinguishable from Enter.")


@app.command("help")
def help_command() -> None:
    """Show terminal setup workflow and key contract."""
    console.print(
        "[bold]Terminal commands[/bold]\n"
        "  omni terminal status            inspect host/tmux keyboard support\n"
        "  omni terminal setup             preview, confirm, back up, and configure tmux\n"
        "  omni terminal setup --check     diagnostics only\n"
        "  /terminal-setup                 same setup flow inside the REPL\n\n"
        "[bold]Chat input contract[/bold]\n"
        "  Enter submits; Ctrl+J, Shift+Enter, Option/Alt+Enter, or \\ + Enter insert a newline."
    )


def terminal_setup_alias(
    check: bool = typer.Option(False, "--check", help="Inspect only; do not offer changes."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the displayed change without prompting."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the proposed tmux block only."),
    tmux_config: Path | None = typer.Option(None, "--tmux-config", help="Advanced tmux config path."),
) -> None:
    """REPL-friendly alias for ``omni terminal setup``."""
    setup_command(check=check, yes=yes, dry_run=dry_run, tmux_config=tmux_config)
