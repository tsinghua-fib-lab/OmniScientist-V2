"""`omni exec` — non-interactive execution of a task from a file/stdin/args.

Mirrors ``codex exec``: read an instruction (from ``-f file``, piped stdin, or
inline arguments), run a single turn, and optionally write the answer to a file.
Ideal for scripting and CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from omni.cli.render import error, info
from omni.cli.runner import run_one_shot
from omni.cli.state import AppState, run_async


def exec_command(
    ctx: typer.Context,
    prompt: list[str] = typer.Argument(None, help="Inline task text, or use -f to read a file."),
    file: str = typer.Option("", "--file", "-f", help="Read the task from a file; '-' means stdin."),
    output: str = typer.Option("", "--output", "-o", help="Write the answer to a file."),
    cont: bool = typer.Option(False, "--continue", "-c", help="Continue the most recent session."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Hide tool progress."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Expand live progress: full arguments, results, and stages."),
    detach: bool = typer.Option(False, "--detach", help="Submit background tasks without waiting."),
) -> None:
    """Run a task non-interactively.

    Examples:
      omni exec -f task.md
      echo "Summarize 2310.06825" | omni exec
      omni exec "Explain diffusion models in three sentences" -o answer.md
    """
    state: AppState = ctx.obj or AppState()
    text = _read_task(file, prompt)
    if not text.strip():
        error('No task was provided. Use `omni exec -f task.md` or `omni exec "task"`.')
        raise typer.Exit(2)
    turn = run_async(run_one_shot(state, text, cont=cont, quiet=quiet, verbose=verbose, detach=detach))
    if output and turn is not None:
        out_path = Path(output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(turn.text or "", encoding="utf-8")
        info(f"Answer written to {out_path}")


def _read_task(file: str, prompt: list[str] | None) -> str:
    if file == "-":
        return sys.stdin.read()
    if file:
        path = Path(file).expanduser()
        if not path.is_file():
            raise typer.BadParameter(f"File not found: {file}")
        return path.read_text(encoding="utf-8")
    text = " ".join(prompt or []).strip()
    if text:
        return text
    # No file and no inline args: accept piped stdin (e.g. `… | omni exec`).
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""
