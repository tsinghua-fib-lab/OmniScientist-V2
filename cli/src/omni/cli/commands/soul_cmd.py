"""``omni soul`` — discover and create scientist personas.

The command is intentionally a thin product boundary over the two portable
persona Skills:

* ``list`` and ``status`` inspect SoulAgent's on-disk contract without importing
  Skill internals;
* ``create`` submits a focused, explicit ``scientist-kg-distiller`` task through
  OmniScientist's normal one-shot runner, so it uses the configured Host model,
  approvals, task ledger, and artifact reporting.

Loading an existing persona remains a SoulAgent action (for example, "think
like Kaiming He").  Keeping creation and activation separate prevents a long
distillation run from silently changing the active persona.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from omni.agent.persona_stoma import load_persona_overlay
from omni.cli.render import console, data_table, info
from omni.cli.runner import run_one_shot
from omni.cli.state import AppState, run_async

app = typer.Typer(
    help="List, inspect, and create scientist personas.",
    no_args_is_help=False,
    invoke_without_command=True,
)


@dataclass(frozen=True)
class SoulPaths:
    """Resolved SoulAgent project and scanner roots for one CLI invocation."""

    project_root: Path
    kg_root: Path


def _soul_paths(paths: Any) -> SoulPaths:
    """Mirror SoulAgent's project-local-first KG root resolution."""
    project_root = Path(paths.local_ops_dir).resolve()
    project_kg = project_root / "scientist-kg"
    kg_root = project_kg if project_kg.is_dir() else Path(paths.scientist_kg_dir).resolve()
    return SoulPaths(project_root=project_root, kg_root=kg_root)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _inventory(kg_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Read a lightweight persona inventory without depending on Skill code."""
    scientists: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    if not kg_root.is_dir():
        return scientists, invalid
    try:
        candidates = sorted(path for path in kg_root.iterdir() if path.is_dir())
    except OSError as exc:
        return scientists, [{"directory": str(kg_root), "error": str(exc)}]
    for candidate in candidates:
        identity = _read_json_object(candidate / "identity.json")
        manifest = _read_json_object(candidate / "manifest.json")
        scientist_id = str(
            identity.get("scientist_id")
            or manifest.get("scientist_id")
            or candidate.name
        ).strip()
        scientist_name = str(identity.get("scientist_name") or "").strip()
        if not scientist_name or scientist_id != candidate.name:
            invalid.append(
                {
                    "directory": candidate.name,
                    "error": "missing identity or scientist_id does not match the directory",
                }
            )
            continue
        aliases = [
            str(value).strip()
            for value in identity.get("aliases") or []
            if str(value).strip()
        ]
        scientists.append(
            {
                "scientist_id": scientist_id,
                "scientist_name": scientist_name,
                "aliases": aliases,
                "path": str(candidate),
            }
        )
    return scientists, invalid


def _status_payload(paths: Any) -> dict[str, Any]:
    resolved = _soul_paths(paths)
    overlay = load_persona_overlay(resolved.project_root)
    scientists, invalid = _inventory(resolved.kg_root)
    return {
        "active": overlay.active,
        "scientist_id": overlay.scientist_id if overlay.active else "",
        "scientist_name": overlay.scientist_name if overlay.active else "",
        "project_root": str(resolved.project_root),
        "kg_root": str(resolved.kg_root),
        "available": scientists,
        "invalid": invalid,
    }


def render_status(paths: Any, *, json_output: bool = False, startup: bool = False) -> None:
    """Render the active persona and scanner inventory for shell or REPL use."""
    payload = _status_payload(paths)
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    if payload["active"]:
        who = payload["scientist_name"] or payload["scientist_id"]
        info(
            f"Active scientist persona: {who} — answers reflect this persona. "
            'Say "restore yourself" or run $soulagent to unload it.'
        )
        return
    available = payload["available"]
    if available:
        listed = ", ".join(row["scientist_id"] for row in available[:6])
        suffix = f" (+{len(available) - 6} more)" if len(available) > 6 else ""
        info(
            f"Scientist personas available ({listed}{suffix}). "
            'Run `/soul list`; say "think like <name>" or run $soulagent to load one.'
        )
        return
    if not startup:
        info(
            "No scientist persona is active and the scanner contains no discoverable personas. "
            "Create one with `/soul create <scientist>`."
        )


def render_list(paths: Any, *, json_output: bool = False) -> None:
    """Render the scanner inventory; shared by shell and in-process ``/soul``."""
    payload = _status_payload(paths)
    if json_output:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    rows: list[list[str]] = []
    for scientist in payload["available"]:
        active = payload["active"] and payload["scientist_id"] == scientist["scientist_id"]
        rows.append(
            [
                "●" if active else "",
                scientist["scientist_id"],
                scientist["scientist_name"],
                ", ".join(scientist["aliases"]) or "—",
            ]
        )
    for invalid in payload["invalid"]:
        rows.append(["!", invalid["directory"], "invalid", invalid["error"]])
    data_table(
        f"Scientist personas (scanner: {payload['kg_root']})",
        ["", "id", "name", "aliases / issue"],
        rows or [["", "(none)", "", "Create one with `/soul create <scientist>`"]],
    )


@app.callback(invoke_without_command=True)
def soul_root(ctx: typer.Context) -> None:
    """Show persona status when no subcommand is supplied."""
    if ctx.invoked_subcommand is None:
        state: AppState = ctx.obj or AppState()
        render_status(state.settings().paths)


@app.command("status")
def status_cmd(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Show the active persona and the scanner root used by SoulAgent."""
    state: AppState = ctx.obj or AppState()
    render_status(state.settings().paths, json_output=json_output)


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """List scientist personas discoverable in SoulAgent's active scanner root."""
    state: AppState = ctx.obj or AppState()
    render_list(state.settings().paths, json_output=json_output)


def _create_prompt(
    *,
    scientist: str,
    project_root: Path,
    workspace: Path,
    install_root: Path,
    field: str,
    institution: str,
    resume: bool,
    max_sources: int,
) -> str:
    hints = []
    if field:
        hints.append(f"Research-field hint: {field}")
    if institution:
        hints.append(f"Institution hint: {institution}")
    hint_block = "\n".join(f"- {hint}" for hint in hints) or "- No identity hints supplied."
    return (
        "Use the built-in $scientist-kg-distiller skill to create a new, traceable "
        "scientist persona KG. Execute the production pipeline; do not merely explain it or "
        "fabricate a persona from general knowledge.\n\n"
        f"Scientist: {scientist}\n"
        f"Invocation project root: {project_root}\n"
        f"Distillation workspace: {workspace}\n"
        f"Install the validated KG into the exact SoulAgent scanner root: {install_root}\n"
        f"Resume hash-validated checkpoints: {'yes' if resume else 'no'}\n"
        f"Maximum source candidates: {max_sources}\n"
        f"{hint_block}\n\n"
        "Preserve provenance, stop for unresolved identity ambiguity, validate the complete KG, "
        "and use the distiller's atomic install path. Never overwrite an existing persona "
        "directory. Report the installed scientist_id, manifest path, capsule path, and any "
        "action still required from the user. Creating the KG must not activate it."
    )


@app.command("create")
def create_cmd(
    ctx: typer.Context,
    scientist: str = typer.Argument(..., help="Scientist name to distill."),
    field: str = typer.Option("", "--field", help="Research-field hint for identity resolution."),
    institution: str = typer.Option(
        "", "--institution", help="Institution hint for identity resolution."
    ),
    workspace: Path | None = typer.Option(
        None,
        "--workspace",
        help="Checkpoint/output workspace; defaults to <project>/scientist-distillations.",
    ),
    resume: bool = typer.Option(
        True,
        "--resume/--no-resume",
        help="Reuse hash-validated distillation checkpoints.",
    ),
    max_sources: int = typer.Option(
        200,
        "--max-sources",
        min=1,
        help="Maximum source candidates collected by the distiller.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show the resolved creation request without running the agent.",
    ),
    detach: bool = typer.Option(False, "--detach", help="Submit the creation task in background."),
) -> None:
    """Create, validate, and install a new scientist persona KG."""
    state: AppState = ctx.obj or AppState()
    paths = state.settings().paths
    resolved = _soul_paths(paths)
    output_workspace = (
        workspace.expanduser().resolve()
        if workspace is not None
        else (resolved.project_root / "scientist-distillations").resolve()
    )
    prompt = _create_prompt(
        scientist=scientist,
        project_root=resolved.project_root,
        workspace=output_workspace,
        install_root=resolved.kg_root,
        field=field,
        institution=institution,
        resume=resume,
        max_sources=max_sources,
    )
    if dry_run:
        data_table(
            "Soul creation plan",
            ["field", "value"],
            [
                ["scientist", scientist],
                ["project root", str(resolved.project_root)],
                ["workspace", str(output_workspace)],
                ["install root", str(resolved.kg_root)],
                ["resume", "yes" if resume else "no"],
                ["max sources", str(max_sources)],
                ["field", field or "—"],
                ["institution", institution or "—"],
            ],
        )
        info("Dry run only; no model call or filesystem write was performed.")
        return
    info(f"Creating scientist persona for {scientist}; validated output will remain inactive.")
    run_async(run_one_shot(state, prompt, quiet=False, verbose=False, detach=detach))


@app.command("help")
def help_cmd() -> None:
    """Explain scientist personas, their lifecycle, and the available commands."""
    console.print("[bold]Scientist personas in OmniScientist[/bold]")
    console.print(
        "A soul is a traceable scientist-persona KG that can temporarily shape "
        "OmniScientist's research judgment and voice. It does not replace Omni's "
        "identity, tools, safety rules, or citation duties.\n"
    )
    data_table(
        "Soul lifecycle",
        ["step", "meaning", "command / request"],
        [
            ["1. Discover", "List persona KGs already available", "/soul list"],
            [
                "2. Create",
                "Build, validate, and install a new KG; it stays inactive",
                '/soul create "Geoffrey Hinton"',
            ],
            ["3. Activate", "Load an existing KG for the current project", 'say "think like <name>"'],
            ["4. Check", "Show which persona is active", "/soul or /soul status"],
            ["5. Unload", "Restore OmniScientist without a scientist overlay", 'say "restore yourself"'],
        ],
    )
    data_table(
        "Command reference",
        ["command", "purpose", "useful options"],
        [
            ["/soul", "Show current status", "—"],
            ["/soul status", "Show active persona and scanner root", "--json"],
            ["/soul list", "List discoverable local personas", "--json"],
            [
                "/soul create <scientist>",
                "Run the scientist KG distiller",
                "--field, --institution, --workspace, --resume/--no-resume, "
                "--max-sources, --detach, --dry-run",
            ],
            ["/soul help", "Show this guide", "—"],
        ],
    )
    console.print("[bold]Examples[/bold]")
    console.print('  /soul create "Geoffrey Hinton" --field "machine learning" --dry-run')
    console.print('  /soul create "Geoffrey Hinton" --institution "University of Toronto"')
    console.print("  /soul list --json")
    console.print()
    info(
        "Create and activate are deliberately separate: creation may be a long, "
        "source-grounded distillation task, while activation only loads an existing KG."
    )
    info(
        "Use `/soul ...` in the interactive CLI. Shell equivalents use `omni soul ...`. "
        "The scanner prefers "
        "<project>/scientist-kg and falls back to <OMNI_HOME>/scientist-kg."
    )
