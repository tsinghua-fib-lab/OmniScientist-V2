"""Top-level ownership-aware OmniScientist uninstall command."""

from __future__ import annotations

import typer

from omni.cli.render import confirm, console, data_table, error, info, success, warn
from omni.cli.state import AppState
from omni.runtime.uninstall import (
    build_uninstall_plan,
    execute_uninstall_plan,
    record_installation,
)


def uninstall_command(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show the complete ownership-aware removal plan without changing anything.",
    ),
    purge: bool = typer.Option(
        False,
        "--purge",
        help="Also delete OMNI_HOME configuration, credentials, tasks, memory, and artifacts.",
    ),
    all_project_data: bool = typer.Option(
        False,
        "--all-project-data",
        help="With --purge, also delete registered in-place project .omni directories.",
    ),
    all_installations: bool = typer.Option(
        False,
        "--all-installations",
        help="Remove every Omni installation found in the install manifest and PATH.",
    ),
    everything: bool = typer.Option(
        False,
        "--everything",
        help="Full wipe: purge data, in-place projects, identical exported skills, and all installations.",
    ),
    remove_program: bool = typer.Option(
        True,
        "--remove-program/--keep-program",
        help="Remove the Python package/command after resource cleanup (default: remove).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without interactive confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Render the plan/report as JSON."),
) -> None:
    """Safely uninstall OmniScientist, optionally purging all owned data.

    Examples:
      omni uninstall --dry-run
      omni uninstall --yes
      omni uninstall --purge --yes
      omni uninstall --everything --yes
    """
    state: AppState = ctx.obj
    paths = state.settings().paths
    if everything:
        purge = True
        all_project_data = True
        all_installations = True
    try:
        plan = build_uninstall_plan(
            paths,
            purge=purge,
            all_project_data=all_project_data,
            all_installations=all_installations,
            remove_program=remove_program,
            remove_untracked_exports=everything,
        )
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(2) from exc

    if json_output and not dry_run and not yes:
        error("--json execution requires --yes; use --dry-run --json to inspect safely.")
        raise typer.Exit(2)

    if not json_output:
        data_table(
            "OmniScientist uninstall plan",
            ["category", "action", "target", "detail"],
            [
                [
                    action.category,
                    action.action,
                    action.target,
                    action.detail + (" [irreversible]" if action.irreversible else ""),
                ]
                for action in plan.actions
            ],
        )
        for message in plan.warnings:
            warn(message)

    if dry_run:
        if json_output:
            console.print_json(data=plan.to_dict())
        else:
            info("Dry run only; no services, files, integrations, credentials, or packages were changed.")
        return

    if not yes:
        prompt = (
            "Permanently remove all listed Omni resources?"
            if purge
            else "Uninstall Omni while preserving the listed research data?"
        )
        if not confirm(prompt, default=False):
            info("Cancelled; no changes were made.")
            return

    report = execute_uninstall_plan(paths, plan)
    if json_output:
        console.print_json(data={"plan": plan.to_dict(), "report": report.to_dict()})
    else:
        data_table(
            "Uninstall result",
            ["status", "detail"],
            [
                *[["completed", item] for item in report.completed],
                *[["skipped", item] for item in report.skipped],
                *[["error", item] for item in report.errors],
            ],
        )
    if report.program_removal_deferred and not json_output:
        info("Program removal was scheduled and will finish after this process exits.")
    if report.errors:
        if not json_output:
            error(f"Uninstall completed with {len(report.errors)} error(s); review the result above.")
        raise typer.Exit(1)
    if not json_output:
        if report.program_removal_deferred:
            success("OmniScientist cleanup completed; program removal is finishing in the background.")
        else:
            success("OmniScientist uninstall completed.")
    if not purge and not json_output:
        info(f"Research data was preserved under {paths.home}.")


def record_install_command(
    ctx: typer.Context,
    method: str = typer.Option(..., "--method", help="env | uv"),
    source: str = typer.Option("", "--source", help="Installed package/source specification."),
    editable: bool = typer.Option(False, "--editable", help="Record an editable source install."),
    channel: str = typer.Option(
        "", "--channel", help="Update channel intent (master | pypi | local | editable | pinned | <branch>)."
    ),
) -> None:
    """Record installer ownership metadata (hidden installer hook)."""
    path = record_installation(
        ctx.obj.settings().paths,
        method=method,
        source=source,
        editable=editable,
        channel=channel,
    )
    info(f"Recorded Omni installation ownership: {path}")
