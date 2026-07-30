"""`omni exec` — non-interactive execution of a task from a file/stdin/args.

Mirrors ``codex exec``: read an instruction (from ``-f file``, piped stdin, or
inline arguments), run a single turn, and optionally write the answer to a file.
Ideal for scripting and CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from omni.cli.render import error, info, warn
from omni.cli.runner import run_one_shot
from omni.cli.state import AppState, run_async
from omni.runtime.turn_outcome import exec_exit_code, persist_exec_output


def exec_command(
    ctx: typer.Context,
    prompt: list[str] = typer.Argument(None, help="Inline task text, or use -f to read a file."),
    file: str = typer.Option("", "--file", "-f", help="Read the task from a file; '-' means stdin."),
    output: str = typer.Option("", "--output", "-o", help="Write the answer to a file."),
    cont: bool = typer.Option(False, "--continue", "-c", help="Continue the most recent session."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Hide tool progress."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Expand live progress: full arguments, results, and stages."),
    detach: bool = typer.Option(False, "--detach", help="Submit background tasks without waiting."),
    ask: bool = typer.Option(
        False,
        "--ask",
        help="Prompt on a TTY for each sensitive tool instead of workspace-auto.",
    ),
) -> None:
    """Run a task non-interactively.

    Defaults to workspace-auto (Codex ``exec`` / Never) in a trusted
    workspace-write sandbox: in-workspace writes and sandboxed ``bash`` /
    ``run_compute`` run without a prompt. An untrusted directory stays
    read-only. Out-of-workspace writes and IM-origin calls still fail closed.
    Use ``--ask`` to keep the human approval loop on a terminal.

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
    if ask and not (sys.stdin.isatty() and sys.stdout.isatty()):
        warn(
            "`--ask` needs a terminal; without one, sensitive tools still fail closed. "
            "Omit --ask to use workspace-auto."
        )
    turn = run_async(
        run_one_shot(
            state,
            text,
            cont=cont,
            quiet=quiet,
            verbose=verbose,
            detach=detach,
            workspace_auto=not ask,
        )
    )
    if output and turn is not None:
        # Codex ``-o`` writes the last agent message for scripting and exits 1
        # on a failed turn; it never claims the file is an answer. Omni keeps
        # that file (so CI still has something to inspect) but labels it:
        # a 429 diagnostic is an error report, a degraded draft is stamped.
        out_path = Path(output).expanduser()
        kind, code = persist_exec_output(out_path, turn)
        if kind == "answer":
            info(f"Answer written to {out_path}")
        elif kind == "partial":
            warn(f"Partial answer written to {out_path}")
        elif kind == "message":
            info(f"Last message written to {out_path}")
        else:
            error(f"Error report written to {out_path}")
        if code:
            raise typer.Exit(code)
    elif exec_exit_code(turn):
        raise typer.Exit(1)


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
