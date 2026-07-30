"""OmniScientist CLI entry point (`omni`).

`omni` (no args) → interactive REPL. `omni "<prompt>"` → one-shot. Subcommands
(`config`, `skills`, `mcp`, `project`, `memory`, `task`, `artifacts`, `profile`, `session`,
`channel`, `cite`, `exec`, `replay`, `init`, `doctor`, `terminal`, `serve`, `uninstall`) work as usual,
as do the research commands (`lit`, `verify`, `bench`, `hypo`, `claim`,
`evidence`, `run`, `source`). The REPL exposes the same verbs as slash commands
(`/lit`, `/verify`, `/bench`, `/hypo`, …); `/lit`, `/verify`, `/bench` run
in-process so they share the active session.

A bare natural-language token (`omni "explain diffusion models"`) is still routed to chat,
but a *mistyped or unimplemented* command (e.g. `omni profil list`, or an
unknown token carrying option flags) surfaces a clear "unknown command" error
with a suggestion instead of being silently swallowed as a prompt.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import click
import typer
from rich.table import Table
from rich.text import Text
from typer.core import TyperGroup

from omni import __version__
from omni.agent.persona_stoma import PersonaStatus, persona_status
from omni.cli.commands import (
    artifacts_cmd,
    bench_cmd,
    channel_cmd,
    cite_cmd,
    config_cmd,
    doctor_cmd,
    eval_cmd,
    exec_cmd,
    init_cmd,
    lit_cmd,
    mcp_cmd,
    memory_cmd,
    profile_cmd,
    project_cmd,
    replay_cmd,
    research_cmd,
    resume_cmd,
    schedule_cmd,
    serve_cmd,
    session_cmd,
    skills_cmd,
    status_cmd,
    tasks_cmd,
    terminal_cmd,
    trust_cmd,
    uninstall_cmd,
    update_cmd,
    verify_cmd,
)
from omni.cli.live_display import VERBOSITY_LEVELS, TurnDisplay, resolve_verbosity
from omni.cli.render import assistant_answer, banner, console, error, info, success, warn
from omni.cli.repl_command_policy import (
    classify_repl_command,
    consume_restart_notice,
    redact_repl_command,
    remember_restart_notice,
)
from omni.cli.repl_commands import CommandCatalog, build_command_catalog
from omni.cli.repl_input import ReplInputBox
from omni.cli.repl_output import (
    clear_active_output,
    get_output_sink,
    publish_transcript_event,
    redraw_active_output,
    use_output_turn,
)
from omni.cli.repl_transcript import DataTableData, TranscriptEvent, TranscriptKind
from omni.cli.repl_tui import (
    ReplInterrupt,
    ReplSubmission,
    ReplTui,
    TuiApplicationError,
    resolve_ui_mode,
)
from omni.cli.runner import (
    render_tasks,
    render_turn_diagnostics,
    run_one_shot,
    should_suppress_assistant_text,
    task_ack_cb,
)
from omni.cli.state import AppState, make_agent, run_async
from omni.cli.timefmt import format_local_time
from omni.config.settings import ModelCfg, OmniSettings
from omni.core.execution_control import CancellationEscalator
from omni.runtime import update_check
from omni.runtime.daemon import daemon_info, is_daemon_running

try:
    import termios
except ImportError:  # pragma: no cover - POSIX terminals provide this.
    termios = None  # type: ignore[assignment]

try:
    import readline as _readline
except ImportError:  # pragma: no cover - readline is optional.
    _readline = None
else:
    try:
        _readline.parse_and_bind("set editing-mode emacs")
    except (OSError, ValueError):
        pass

# A token that *could* be a mistyped command name: a bare ASCII word. Commands
# are ASCII; non-ASCII text, punctuation, quoted phrases, and digit-leading
# tokens are treated as prompts.
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _botched_command(first: str, rest: list[str], known: list[str]) -> str | None:
    """Return an error message if ``first`` looks like a wrong command.

    Heuristic (keeps ``omni "<prompt>"`` working while unmasking mistakes):
    - non-identifier first token → genuine prompt (``None``).
    - near-miss of a real command → suggest it.
    - bare word carrying option flags → prompts don't take bare flags, so it's
      almost certainly a botched command.
    - otherwise → treat as a prompt (``None``).
    """
    if not _IDENT_RE.match(first):
        return None
    suggestion = difflib.get_close_matches(first, known, n=1, cutoff=0.7)
    flag = next((a for a in rest if a.startswith("-")), None)
    if suggestion:
        msg = f"Unknown command '{first}'. Did you mean: omni {suggestion[0]}"
        if not flag:
            tail = (" " + " ".join(rest)) if rest else ""
            msg += f"\nTo send it as a prompt, use: omni chat \"{first}{tail}\""
        return msg
    if flag is not None:
        return (
            f"Unknown command '{first}' with option '{flag}'. See `omni --help`."
            f"\nTo send a prompt, quote it: omni chat \"{first} ...\""
        )
    return None


class DefaultGroup(TyperGroup):
    """Route bare prompts to ``chat`` but surface mistyped/unknown commands.

    We check ``get_command`` first (instead of catching the not-found error)
    because Typer raises its own ``UsageError`` class, not click's.
    """

    def resolve_command(self, ctx: click.Context, args: list[str]):  # noqa: ANN201
        if args:
            first = args[0]
            if not first.startswith("-") and self.get_command(ctx, first) is None:
                if first.lower() == "help":
                    # ``omni help`` mirrors ``omni --help`` and the per-group
                    # ``<command> help`` convention. Never route it to ``chat``:
                    # that would build an agent, trip the workspace-trust prompt,
                    # and try to *answer* "help" as a research question. This runs
                    # before the group callback (Click resolves the command first),
                    # so no agent is created. Matches what ``--help`` does.
                    click.echo(ctx.get_help(), color=ctx.color)
                    ctx.exit()
                known = [n for n in self.list_commands(ctx) if n != "chat"]
                hint = _botched_command(first, args[1:], known)
                if hint is not None:
                    # ``ctx.fail`` raises Typer's *vendored* click UsageError so
                    # it's formatted cleanly (a top-level click.UsageError would
                    # slip past Typer's handler and dump a traceback).
                    ctx.fail(hint)
                chat_cmd = self.get_command(ctx, "chat")
                if chat_cmd is not None:
                    return "chat", chat_cmd, args
        return super().resolve_command(ctx, args)


def _control_char_like(current: object, value: int) -> int | bytes:
    return value if isinstance(current, int) else bytes([value])


def _termios_flags(*names: str) -> int:
    if termios is None:
        return 0
    flags = 0
    for name in names:
        flags |= getattr(termios, name, 0)
    return flags


def _set_control_char(cc: list[object], index: int | None, value: int) -> None:
    if index is None:
        return
    try:
        cc[index] = _control_char_like(cc[index], value)
    except (IndexError, TypeError):
        return


class _TerminalInputGuard:
    """Keep REPL line editing usable after pagers or subprocesses touch the tty."""

    def __init__(self, stream: object | None = None) -> None:
        self._fd: int | None = None
        self._original: list[object] | None = None
        if termios is None:
            return
        stream = sys.stdin if stream is None else stream
        try:
            if not stream.isatty():
                return
            self._fd = stream.fileno()
            self._original = termios.tcgetattr(self._fd)
        except (AttributeError, OSError, termios.error):
            self._fd = None
            self._original = None

    def prepare(self) -> None:
        if termios is None or self._fd is None:
            return
        try:
            attrs = termios.tcgetattr(self._fd)
            attrs[0] |= getattr(termios, "ICRNL", 0)
            attrs[3] |= _termios_flags(
                "ECHO",
                "ECHOE",
                "ECHOK",
                "ECHOKE",
                "ECHOCTL",
                "ICANON",
                "ISIG",
                "IEXTEN",
            )
            cc = attrs[6]
            _set_control_char(cc, termios.VERASE, 0x7F)
            _set_control_char(cc, getattr(termios, "VKILL", None), 0x15)
            _set_control_char(cc, getattr(termios, "VWERASE", None), 0x17)
            _set_control_char(cc, getattr(termios, "VREPRINT", None), 0x12)
            termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
        except (OSError, termios.error, IndexError):
            return

    def restore(self) -> None:
        if termios is None or self._fd is None or self._original is None:
            return
        try:
            termios.tcsetattr(self._fd, termios.TCSANOW, self._original)
        except (OSError, termios.error):
            return


def _read_repl_line(
    input_guard: _TerminalInputGuard,
    prompt: str = "[bold cyan]›[/bold cyan] ",
    *,
    input_box: ReplInputBox | None = None,
    mode: str = "auto",
) -> str | ReplSubmission:
    input_guard.prepare()

    def fallback() -> str:
        return console.input(prompt)

    if input_box is None:
        return fallback()
    return input_box.read_line(mode=mode, fallback=fallback)


async def _read_repl_line_async(
    input_guard: _TerminalInputGuard,
    *,
    input_box: ReplInputBox,
    mode: str,
    prompt: str = "[bold cyan]›[/bold cyan] ",
) -> str:
    if not getattr(input_box, "manages_terminal", False):
        input_guard.prepare()

    def fallback() -> str:
        return console.input(prompt)

    if isinstance(input_box, ReplTui):
        return await input_box.read_turn_async(mode=mode, fallback=fallback)
    return await input_box.read_line_async(mode=mode, fallback=fallback)


async def _refresh_repl_input_status(
    input_box: ReplInputBox,
    agent,  # noqa: ANN001
    session_id: str,
) -> None:
    """Refresh advisory prompt status without making input depend on diagnostics."""
    try:
        snapshot = await agent.context_snapshot(session_id)
    except Exception:  # noqa: BLE001 - status diagnostics must never break input.
        return
    input_box.update_status(
        model=snapshot.model,
        focus=snapshot.focus_label,
        context_tokens=snapshot.total_tokens,
        context_window=snapshot.context_window_tokens,
        clearable_tokens=snapshot.clearable_tokens,
    )


class _ReplInboxWatcher:
    """Render daemon-written task notifications in the active REPL window."""

    def __init__(self, paths, session_id: str) -> None:  # noqa: ANN001
        self._paths = paths
        self._session_id = session_id
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: set[str] = set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="omni-repl-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def set_session(self, session_id: str) -> None:
        with self._lock:
            self._session_id = session_id

    def _current_session(self) -> str:
        with self._lock:
            return self._session_id

    def _run(self) -> None:
        from omni.runtime.artifact_preview import inline_text_artifacts
        from omni.runtime.notifications import task_notification_from_dict
        from omni.runtime.presentation import task_presentation_from_notification

        inbox = self._paths.project_dir / "inbox.jsonl"
        offset = inbox.stat().st_size if inbox.exists() else 0
        while not self._stop.wait(0.6):
            if not is_daemon_running(self._paths):
                continue
            try:
                if not inbox.exists():
                    offset = 0
                    continue
                size = inbox.stat().st_size
                if size < offset:
                    offset = 0
                if size == offset:
                    continue
                with inbox.open("r", encoding="utf-8") as fh:
                    fh.seek(offset)
                    lines = fh.readlines()
                    offset = fh.tell()
            except OSError:
                continue
            for raw in lines:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict) or not self._matches(data):
                    continue
                key = f"{data.get('subtask_id')}:{data.get('status')}:{data.get('created_at')}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                note = task_notification_from_dict(data)
                presentation = inline_text_artifacts(
                    task_presentation_from_notification(note),
                    self._paths.artifacts_dir,
                )
                info("Background task completed")
                assistant_answer(presentation.to_markdown())

    def _matches(self, data: dict[str, object]) -> bool:
        if str(data.get("channel") or "cli") != "cli":
            return False
        current = self._current_session()
        note_session = str(data.get("session_id") or "")
        return not note_session or not current or note_session == current


app = typer.Typer(
    cls=DefaultGroup,
    add_completion=True,
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="OmniScientist - a local-first personal research agent for CLI and messaging channels.",
)

app.add_typer(config_cmd.app, name="config")
app.add_typer(skills_cmd.app, name="skills")
app.add_typer(mcp_cmd.app, name="mcp")
app.add_typer(project_cmd.app, name="project")
app.add_typer(profile_cmd.app, name="profile")
app.add_typer(session_cmd.app, name="session")
app.add_typer(schedule_cmd.app, name="schedule")
app.add_typer(channel_cmd.app, name="channel")
app.add_typer(cite_cmd.app, name="cite")
app.add_typer(memory_cmd.app, name="memory")
app.add_typer(tasks_cmd.app, name="task")
app.add_typer(artifacts_cmd.app, name="artifacts")
app.add_typer(research_cmd.hypo_app, name="hypo")
app.add_typer(research_cmd.claim_app, name="claim")
app.add_typer(research_cmd.evidence_app, name="evidence")
app.add_typer(research_cmd.run_app, name="run")
app.add_typer(research_cmd.source_app, name="source")
app.add_typer(init_cmd.app, name="init")
app.add_typer(doctor_cmd.app, name="doctor")
app.add_typer(serve_cmd.app, name="serve")
app.add_typer(status_cmd.app, name="status")
app.add_typer(resume_cmd.app, name="resume")
app.add_typer(terminal_cmd.app, name="terminal")

# Single-token commands defined in their own modules.
app.command("lit", help=lit_cmd.app_help)(lit_cmd.lit_command)
app.command("verify", help=verify_cmd.app_help)(verify_cmd.verify_command)
app.command("bench", help=bench_cmd.app_help)(bench_cmd.bench_command)
app.command("eval", help=eval_cmd.app_help)(eval_cmd.eval_command)
app.command("exec")(exec_cmd.exec_command)
app.command("replay")(replay_cmd.replay_command)
app.command("trust", help=trust_cmd.app_help)(trust_cmd.trust_command)
app.add_typer(update_cmd.app, name="update")
app.command("upgrade", hidden=True)(update_cmd.update_command)
app.command("uninstall")(uninstall_cmd.uninstall_command)
app.command("terminal-setup", hidden=True)(terminal_cmd.terminal_setup_alias)
app.command("_record-install", hidden=True)(uninstall_cmd.record_install_command)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"OmniScientist {__version__}")
        raise typer.Exit()


def _terminal_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _maybe_ensure_home_service(state: AppState) -> None:
    """Bring the single home service up on launch (always-on, OpenClaw-style).

    omni is not usable without its one supervised background service per
    ``OMNI_HOME``: channels, schedules, and inbound IM all flow through it. So a
    bare ``omni`` guarantees it is running — enabling + installing it on first
    need and repairing it if it drifted down. This is deliberately: (1) skippable
    only via the ``service.ensure_on_launch`` escape hatch (CI / power users who
    manage the unit themselves); (2) a no-op when the service is already healthy
    (the common path, zero added latency); and (3) dispatched on a daemon thread
    so it never blocks the REPL, and never raises. Because the model is always-on,
    a prior ``omni serve stop`` is *transient* — it pauses the service until the
    next launch, which brings it back here.
    """
    try:
        settings = state.settings()
    except Exception:  # noqa: BLE001 - settings failures surface elsewhere.
        return
    if not settings.service.ensure_on_launch:
        return
    from omni.runtime import service_state

    observation = service_state.observe_service(settings.paths)
    if observation.phase in {"starting", "ready"}:
        service_state.clear_start_request(settings.paths)
        return
    # Record intent synchronously before dispatching the non-blocking worker.
    # If `omni update` wins the lifecycle lock, it will consume this marker and
    # start the service on the updated code instead of losing this bare launch.
    try:
        service_state.request_start(settings.paths)
    except OSError:
        # The launch hook is best-effort and must never break the interactive
        # CLI when OMNI_HOME is temporarily read-only or out of space.
        pass

    def _bring_up() -> None:
        try:
            from omni.runtime import service_control

            # Enable + install + start on first need; repair if enabled-but-down.
            service_control.lazy_enable(settings, reason="launch", wait_s=4.0)
        except Exception:  # noqa: BLE001 - launch-time bring-up is best-effort.
            pass

    threading.Thread(target=_bring_up, name="omni-service-ensure", daemon=True).start()


def _run_first_time_setup(state: AppState) -> None:
    """Run setup before the first bare interactive launch."""
    if not init_cmd.first_run_setup_required(state):
        return
    if not _terminal_is_interactive():
        error(
            "First-time setup is required. Run `omni init` interactively, or "
            "`omni init --non-interactive` for offline defaults."
        )
        raise typer.Exit(2)
    info("No user configuration was found; starting first-time setup.")
    init_cmd.run_setup_wizard(state)
    info("Setup complete; starting interactive mode.")


def _maybe_converge_installation(state: AppState, *, force: bool = False) -> None:
    """Finish local lifecycle work after a package-manager installation.

    ``pipx upgrade`` and ``uv tool upgrade`` replace the Python distribution,
    but they cannot know which ``OMNI_HOME`` this launch uses or migrate its
    managed runtime and Home Service. A package fingerprint makes the common
    launch path a no-op and the first launch after an external install a
    blocking, retryable local convergence step.

    This path never checks PyPI and never installs the Python package again.
    """
    from omni.runtime import service_control, update_state
    from omni.runtime.daemon import stop_legacy_daemons

    settings = state.settings()
    fingerprint = update_state.current_fingerprint()
    if not force and not update_state.convergence_needed(
        settings.paths, fingerprint
    ):
        return

    info(
        "Finishing OmniScientist setup for the installed package "
        f"({fingerprint.version})..."
    )
    retired = 0
    service_detail = ""
    try:
        with service_control.update_guard(
            settings, restart_serve=True
        ) as service_guard:
            # This process started after the external package operation, so its
            # imported setup implementation is already the newly installed one.
            update_cmd._prepare_bundled_skill_runtimes(settings.paths)
            retired = len(stop_legacy_daemons(settings.paths.home))
            service_detail = service_guard.restore()
            update_state.record_converged(settings.paths, fingerprint)
    except typer.Exit:
        # The setup helper already rendered an actionable error. Leaving the
        # fingerprint absent makes the next launch retry automatically.
        raise
    except Exception as exc:  # noqa: BLE001 - lifecycle failure must fail closed.
        error(
            "Could not finish the installed OmniScientist update: "
            f"{exc}. Run `omni update` to retry."
        )
        raise typer.Exit(1) from exc

    details = []
    if retired:
        details.append(f"retired {retired} legacy daemon(s)")
    if service_detail:
        details.append(service_detail)
    suffix = f" ({'; '.join(details)})" if details else ""
    success(f"Installed package and local runtime are synchronized{suffix}.")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    project: str = typer.Option(None, "--project", "-P", help="Use a named research project."),
    profile: str = typer.Option(None, "--profile", help="Use a configuration profile."),
    model: str = typer.Option(None, "--model", "-m", help="Override the model name."),
    ui: str = typer.Option(None, "--ui", help="Interactive UI: auto, tui (inline dock), or classic."),
    cont: bool = typer.Option(False, "--continue", "-c", help="Continue the latest workspace session."),
    trust: bool = typer.Option(None, "--trust/--no-trust", help="Trust (or refuse) the current directory without prompting."),
    out: str = typer.Option(None, "--out", help="Directory for generated files (default: the launch directory)."),
    version: bool = typer.Option(None, "--version", "-V", callback=_version_callback, is_eager=True),
) -> None:
    if ui is not None and ui.lower() not in {"auto", "tui", "classic"}:
        raise typer.BadParameter("--ui must be auto, tui, or classic")
    overrides: dict = {"display": {"ui_mode": ui.lower()}} if ui else {}
    if out:
        overrides.setdefault("artifacts", {})["output_dir"] = out
    ctx.obj = AppState(project=project, profile=profile, model=model, overrides=overrides, trust_flag=trust)
    if ctx.invoked_subcommand is None:
        # A first bare launch runs the same setup flow as ``omni init``. Later
        # launches go directly to the REPL (optionally continuing a session).
        _run_first_time_setup(ctx.obj)
        _maybe_converge_installation(ctx.obj)
        _maybe_ensure_home_service(ctx.obj)
        resume_id = resume_cmd.resolve_last(ctx.obj) if cont else None
        if cont and resume_id is None:
            info("No previous workspace session was found; started a new session.")
        _repl(ctx.obj, resume_session_id=resume_id)


@app.command("_converge-install", hidden=True)
def _converge_install_command(ctx: typer.Context) -> None:
    """Complete installer-owned runtime and service convergence."""
    _maybe_converge_installation(ctx.obj or AppState(), force=True)


# ── one-shot chat ──
@app.command("chat")
def chat(
    ctx: typer.Context,
    prompt: list[str] = typer.Argument(None, help="Research question or instruction."),
    cont: bool = typer.Option(False, "--continue", "-c", help="Continue the most recent session."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Hide tool progress."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Expand live progress: full arguments, results, and stages."),
    detach: bool = typer.Option(False, "--detach", help="Submit background tasks without waiting."),
    mode: str = typer.Option("auto", "--mode", help="Interaction mode: auto, plan, or review."),
) -> None:
    """Run a single prompt, as in `omni \"question\"`."""
    state: AppState = ctx.obj or AppState()
    text = " ".join(prompt).strip() if prompt else ""
    if not text:
        resume_id = resume_cmd.resolve_last(state) if cont else None
        if cont and resume_id is None:
            info("No previous workspace session was found; started a new session.")
        _repl(state, resume_session_id=resume_id)
        return
    if mode not in {"auto", "plan", "review"}:
        raise typer.BadParameter("--mode must be auto, plan, or review")
    run_async(run_one_shot(state, text, cont=cont, quiet=quiet, verbose=verbose, detach=detach, interaction_mode=mode))


@app.command("current")
def current_cmd(
    ctx: typer.Context,
    session: str = typer.Option("", "--session", "-s", help="Session ID/prefix; defaults to the latest workspace session."),
) -> None:
    """Show objects bound to the session focus."""
    state: AppState = ctx.obj or AppState()
    run_async(_render_current_command(state, session=session))


@app.command("why")
def why_cmd(
    ctx: typer.Context,
    task_id: str = typer.Argument("", help="Task ID/prefix; defaults to the latest workspace task."),
    session: str = typer.Option("", "--session", "-s", help="Session ID/prefix used to filter the latest task."),
) -> None:
    """Explain the route, plan, provider selection, and verification for a task."""
    state: AppState = ctx.obj or AppState()
    run_async(_render_why_command(state, task_id=task_id, session=session))


async def _latest_session_id(agent, session: str = "") -> str:  # noqa: ANN001
    if session:
        row = await agent.get_session(session)
        return row.id if row is not None else ""
    rows = await agent.list_sessions(limit=1)
    return rows[0][0].id if rows else ""


async def _render_current_command(state: AppState, *, session: str = "") -> None:
    agent = await make_agent(state)
    try:
        session_id = await _latest_session_id(agent, session)
        if not session_id:
            warn(f"Session {session} was not found." if session else "This workspace has no sessions.")
            return
        target = await agent.focus.latest(session_id)
        if target is None:
            info(f"Session {session_id[:8]} has no active artifact or source focus.")
            return
        focus = target.focus

        def display_path(value: object) -> str:
            raw = str(value or "")
            if not raw:
                return "-"
            try:
                return str(Path(raw).expanduser().resolve().relative_to(agent.paths.project_dir.resolve()))
            except (OSError, ValueError):
                return raw

        table = Table(title="current focus", title_justify="left", title_style="cyan", header_style="cyan")
        table.add_column("field", style="cyan")
        table.add_column("value", overflow="fold")
        rows = [
            ("session", session_id),
            ("origin", focus.origin or "-"),
            ("skill", focus.skill_name or "-"),
            ("target_kind", focus.target_kind or "-"),
            ("task", focus.task_id[:8] if focus.task_id else "-"),
            ("workflow", focus.workflow_run_id[:8] if focus.workflow_run_id else "-"),
            ("workflow_step", focus.workflow_step_id or "-"),
            ("skill_execution", focus.subtask_id[:8] if focus.subtask_id else "-"),
            ("child_task", focus.child_task_id[:8] if focus.child_task_id else "-"),
            ("title", focus.artifact_title or "-"),
            ("artifact", focus.artifact_uri or display_path(focus.artifact_path)),
            ("source", display_path(target.source_path or focus.source_path or focus.source_uri)),
            ("confidence", f"{float(focus.confidence or 0.0):.2f}"),
        ]
        for key, value in rows:
            table.add_row(key, value)
        console.print(table)
    finally:
        await agent.aclose()


async def _render_why_command(state: AppState, *, task_id: str = "", session: str = "") -> None:
    agent = await make_agent(state)
    try:
        run = await agent.tasks.get_task(task_id) if task_id else None
        if run is None:
            rows = await agent.tasks.list_tasks(limit=30, kind="turn")
            if session:
                sess = await agent.get_session(session)
                session_id = sess.id if sess is not None else session
                rows = [row for row in rows if row.session_id.startswith(session_id)]
            run = rows[0] if rows else None
        if run is None:
            warn(f"Task {task_id} was not found." if task_id else "This workspace has no tasks.")
            return
        events = await agent.tasks.list_events(run.id)
        important = [
            event for event in events
            if event.event_type.startswith("plan.")
            or event.event_type in {
                "route.arbitration",
                "plan.target.artifact",
                "subtask.submitted",
                "verification.passed",
                "verification.failed",
                "assistant.message",
            }
        ]
        table = Table(title=f"why task {run.id[:8]}", title_justify="left", title_style="cyan", header_style="cyan")
        table.add_column("seq", justify="right")
        table.add_column("event", no_wrap=True)
        table.add_column("summary", overflow="fold", min_width=32)
        table.add_column("decision/detail", overflow="fold", min_width=20)
        for event in important[:24]:
            payload = event.output_json if isinstance(event.output_json, dict) else {}
            detail = (
                str(payload.get("decision") or payload.get("intent_type") or payload.get("status") or "")
                or str(payload.get("subtask_id") or "")
            )
            if event.skill_name:
                detail = f"{detail} skill={event.skill_name}".strip()
            if event.subtask_id:
                detail = f"{detail} subtask={event.subtask_id[:8]}".strip()
            table.add_row(str(event.seq), event.event_type, event.summary or "-", detail or "-")
        console.print(table)
    finally:
        await agent.aclose()


# ── REPL ──


def _is_mock_provider(provider: str) -> bool:
    return (provider or "").lower() in ("", "mock", "offline")


def _missing_model_fields(model: ModelCfg) -> list[str]:
    if _is_mock_provider(model.provider):
        return []
    missing: list[str] = []
    if not model.base_url:
        missing.append("model.base_url")
    if not model.api_key:
        missing.append("model.api_key")
    if not model.model or model.model == "omni-mock":
        missing.append("model.model")
    return missing


def _model_setup_commands(model: ModelCfg) -> list[tuple[str, str]]:
    if _is_mock_provider(model.provider):
        return [
            ("config set model.provider openai", "select an OpenAI-compatible provider"),
            ("config set model.base_url https://api.deepseek.com", "set the API endpoint"),
            ("config set model.api_key sk-xxx", "store the key in secrets.toml"),
            ("config set model.model deepseek-v4-pro", "set the model name"),
        ]

    templates = {
        "model.base_url": ("config set model.base_url https://api.deepseek.com", "set the API endpoint"),
        "model.api_key": ("config set model.api_key sk-xxx", "set the API key"),
        "model.model": ("config set model.model deepseek-v4-pro", "set the model name"),
    }
    return [templates[field] for field in _missing_model_fields(model)]


def _repl_banner_text(project_name: str, settings: OmniSettings) -> Text:
    model = settings.model
    missing = _missing_model_fields(model)
    mock = _is_mock_provider(model.provider)
    model_style = "yellow" if missing or mock else "green"

    text = Text()
    text.append("OmniScientist interactive mode", "bold cyan")
    text.append(" · project ")
    text.append(project_name, "bright_green")
    text.append(" · model ")
    text.append(f"{model.provider}/{model.model}", model_style)
    if missing:
        text.append(" · missing ", "yellow")
        text.append(", ".join(missing), "yellow")
    elif mock:
        text.append(" · offline mock", "yellow")
    else:
        text.append(" · ready", "green")
    text.append(f"\nworkspace {settings.paths.project_dir}", "dim")
    text.append("\nEnter /help for examples or /exit to quit.", "dim")
    return text


def _render_persona_status(working_dir: Path | None, *, startup: bool) -> None:
    """Surface the active scientist persona and any loadable ``scientist-kg/``.

    Read-only and boundary-respecting: reads SoulAgent's on-disk state via
    ``persona_stoma`` and never imports or mutates the skill. At startup it stays
    silent unless a persona is active or a ``scientist-kg/`` is present, so
    ordinary sessions are unaffected; ``/soul`` always reports.
    """
    status: PersonaStatus = persona_status(working_dir)
    if status.active:
        who = status.overlay.scientist_name or status.overlay.scientist_id
        info(
            f'Active scientist persona: {who} — answers reflect this persona. '
            'Say "restore yourself" or run $soulagent to unload it.'
        )
        return
    if status.scientist_kg_present:
        listed = ", ".join(status.available[:6]) if status.available else "none validated yet"
        info(
            f'Scientist personas available ({listed}). Say "think like <name>" or run '
            "$soulagent to load one; /soul shows status."
        )
        return
    if not startup:
        info(
            "No scientist persona active and no scientist-kg/ here. Build one with "
            "$scientist-kg-distiller, then load it with $soulagent."
        )


def _typer_children_summary(command_app: typer.Typer) -> str:
    """Render registered children once, keeping the conventional help-last order."""
    names = [command.name for command in command_app.registered_commands if command.name]
    names.extend(group.name for group in command_app.registered_groups if group.name)
    names = [name for name in names if name != "help"] + (["help"] if "help" in names else [])
    return " / ".join(names) or "none"


def _repl_quickstart_rows() -> list[tuple[str, str, str, str]]:
    """Rows for the entry quickstart table.

    Columns are (command, subcommands, purpose/details, example): the detail column is
    self-explanatory and the example column is a concrete, runnable command
    chosen for what a first-time user reaches for first. The first three rows
    are ordered by what a new user does first — just ask, then run the setup
    wizard, then tune the model config — and the caller colour-highlights the
    top "ask" and "config" rows.
    """
    rows = [
        ("Ask directly", "none", "Start a conversation by entering a question", "Summarize the contributions of arXiv 2310.06825"),
        (
            "/init",
            "none",
            "Configure the model, retrieval, workspace, and skill library; optionally export skills or register MCP",
            "/init",
        ),
        (
            "/config",
            _typer_children_summary(config_cmd.app),
            "Configure model and embedding endpoints, keys, and models; changes apply immediately",
            "config model -p openai -u <BASE_URL> -m <MODEL> -k <API_KEY>",
        ),
        (
            "/skills",
            _typer_children_summary(skills_cmd.app),
            "Inspect, search, import, and export skills; --all includes external libraries",
            "/skills examples",
        ),
        ("/status", "none", "Show workspace, database, daemon, and task status", "/status"),
        ("/current", "none", "Show the active artifact, paper, task, or source focus", "/current"),
        ("/why", "[task_id]", "Explain route, plan, provider selection, and verification", "/why"),
        (
            "/mode",
            "auto|plan|review",
            "Switch REPL mode; plan waits for approval and review uses read-only tools",
            "/mode plan",
        ),
        (
            "/verbose",
            "quiet|normal|verbose",
            "Set live progress detail: plan decisions, tool calls, step hierarchy",
            "/verbose verbose",
        ),
        ("/plan", "<request>", "Create and persist a plan without executing it", "/plan Evaluate this research design"),
        ("/review", "<request>", "Review a request with read-only tools", "/review Check this experimental conclusion"),
        ("/lit", "help", "Grounded local-corpus QA with [S#] citations and optional verification", "/lit \"How does RAG reduce hallucination?\""),
        (
            "/task",
            _typer_children_summary(tasks_cmd.app),
            "Inspect tasks and their subtasks, results, recovery, archival, and cleanup",
            "/task show <id>",
        ),
        (
            "/artifacts",
            _typer_children_summary(artifacts_cmd.app),
            "Preview, version, diff, and review artifacts with research provenance",
            "/artifacts review <id>",
        ),
        (
            "/channel",
            _typer_children_summary(channel_cmd.app),
            "Configure messaging channels and reuse the background service with --start",
            "/channel help",
        ),
        (
            "/serve",
            _typer_children_summary(serve_cmd.app),
            "Start task workers, the daemon, and channel listeners",
            "/serve status",
        ),
        (
            "/schedule",
            _typer_children_summary(schedule_cmd.app),
            "Recurring/one-time jobs; list shows next fire + last-run status, show adds results and artifacts (fires from /serve)",
            "/schedule show <id>",
        ),
        (
            "/memory",
            _typer_children_summary(memory_cmd.app),
            "Search and maintain long-term memory and the research notebook",
            "/memory search retrieval augmented generation",
        ),
        ("/verify", "none", "Audit unsupported, contradicted, and overconfident claims", "/verify --session"),
        ("/compact", "none", "Compact older turns and report estimated token savings", "/compact"),
        ("/context", "none", "Show the session context budget and injected sections", "/context"),
        ("/resume", _typer_children_summary(resume_cmd.app), "Resume workspace sessions", "/resume --last"),
        ("/session", _typer_children_summary(session_cmd.app), "Inspect, resume, fork, and export sessions", "/session list"),
        ("/project", _typer_children_summary(project_cmd.app), "Manage project workspaces", "/project list"),
        ("/mcp", _typer_children_summary(mcp_cmd.app), "Expose Omni capabilities to Codex or Claude Code", "/mcp install"),
        ("/profile", _typer_children_summary(profile_cmd.app), "Manage model and credential profiles", "/profile list"),
        ("/cite", _typer_children_summary(cite_cmd.app), "Browse literature and export citations", "/cite list"),
        ("/hypo", _typer_children_summary(research_cmd.hypo_app), "Propose, track, and adjudicate hypotheses", "/hypo new \"Diffusion models outperform GANs\" -c 0.6"),
        ("/claim", _typer_children_summary(research_cmd.claim_app), "Record and inspect research claims", "/claim list"),
        ("/evidence", _typer_children_summary(research_cmd.evidence_app), "Bind claims to source evidence", "/evidence add 1a2b --source 9f8e"),
        ("/run", _typer_children_summary(research_cmd.run_app), "Inspect experiment and computation runs", "/run list"),
        ("/source", _typer_children_summary(research_cmd.source_app), "Inspect sources and rebuild indexes", "/source reindex"),
        ("/bench", "none", "Evaluate retrieval recall@k and MRR offline", "/bench --k 3"),
        (
            "/eval",
            "see /eval --help",
            "Run offline behavior and research-quality benchmarks",
            "/eval --research-quality",
        ),
        ("/doctor", "none", "Run environment and configuration diagnostics", "/doctor"),
        (
            "/terminal",
            _typer_children_summary(terminal_cmd.app),
            "Inspect terminal/tmux modified-key support; /terminal-setup runs the confirmed setup flow",
            "/terminal status",
        ),
        (
            "/uninstall",
            "options",
            "Preview or remove Omni-owned services, integrations, program files, and optional data",
            "/uninstall --dry-run",
        ),
        ("/inbox", "none", "Inspect completions (this workspace + IM channel anchor)", "/inbox"),
        (
            "/clear",
            "[--screen]",
            "Start a clean context while retaining history, tasks, artifacts, and durable memory; --screen only redraws",
            "/clear",
        ),
        ("/new", "none", "Start another session without clearing terminal scrollback", "/new"),
        (
            "/stop /steer",
            "[instruction]",
            "Cancel or redirect the active turn without leaving the REPL",
            "/steer use only primary sources",
        ),
        (
            "/help /exit /quit",
            "none",
            "Show help or leave cleanly; Ctrl+D on an empty draft also exits",
            "/help",
        ),
    ]
    shown = {
        token.lstrip("/")
        for row in rows
        for token in row[0].split()
        if token.startswith("/")
    }
    registered = [
        (command.name, command.help or "")
        for command in app.registered_commands
        if command.name and command.name != "chat" and not command.hidden
    ]
    registered.extend(
        (group.name, group.help or "")
        for group in app.registered_groups
        if group.name and group.name != "chat"
    )
    for name, help_text in registered:
        if name in shown:
            continue
        rows.append((f"/{name}", "see --help", help_text or "Show command help", f"/{name} --help"))
    return rows


def _command_table(
    title: str,
    columns: tuple[str, ...],
    rows: list[tuple[str, ...]],
    *,
    row_styles: list[str | None] | None = None,
) -> None:
    event = TranscriptEvent(
        kind=TranscriptKind.DATA_TABLE,
        payload=DataTableData(
            title=title,
            columns=columns,
            rows=tuple(rows),
            layout="commands",
            row_styles=tuple(style or "" for style in (row_styles or ())),
        ),
        foldable=True,
        initially_collapsed=True,
    )
    if publish_transcript_event(event, stream=console.file):
        return
    table = Table(
        title=title,
        title_justify="left",
        title_style="cyan",
        header_style="cyan",
        box=None,
        padding=(0, 2),
    )
    for index, column in enumerate(columns):
        table.add_column(column, overflow="fold", no_wrap=index == 0)
    for i, row in enumerate(rows):
        style = row_styles[i] if row_styles and i < len(row_styles) else None
        table.add_row(*row, style=style)
    console.print(table)


def _print_model_setup_hint() -> None:
    """Show the two ways to configure a real model (command + config file)."""
    text = Text()
    text.append("Configure a real model; changes apply immediately:\n", "bold")
    text.append("  1. One command: ", "dim")
    text.append("config model -p openai -u <BASE_URL> -m <MODEL> -k <API_KEY>\n", "cyan")
    text.append("       -p provider · -u base_url · -m model · -k api_key\n", "dim")
    text.append("       use provider=openai for OpenAI-compatible APIs; base_url selects the service, for example https://api.deepseek.com/v1\n", "dim")
    text.append("  2. Or edit config.toml and secrets.toml under the active Omni data directory.\n", "dim")
    text.append("       Run `omni config path` to show their exact paths.\n", "cyan")
    console.print(text)


def _quickstart_row_style(command: str) -> str | None:
    """Colour the three rows a first-time user reaches for first.

    ``Ask directly``, ``/init`` (setup wizard), and ``/config`` (model
    setup) are the first things a new user touches, so they share one highlight
    (bold cyan). Everything else uses the default row colour so these three stand
    out together as the key commands.
    """
    if command in ("Ask directly", "/init", "/config"):
        return "bold cyan"
    return None


def _show_repl_quickstart(model: ModelCfg) -> None:
    missing = _missing_model_fields(model)
    if _is_mock_provider(model.provider):
        warn("The offline mock model is active; configure a real model for full answers.")
        _print_model_setup_hint()
    elif missing:
        warn(f"Model configuration is incomplete; missing {', '.join(missing)}. Changes apply on the next turn.")
        _print_model_setup_hint()
    else:
        info("Model configuration is loaded; you can ask a question now.")
    rows = _repl_quickstart_rows()
    styles = [_quickstart_row_style(row[0]) for row in rows]
    _command_table(
        "Common commands and subcommands",
        ("command", "subcommands", "purpose and details", "example"),
        rows,
        row_styles=styles,
    )


def _show_repl_help() -> None:
    # A concise, hierarchical overview: one row per command (command · its
    # subcommands · purpose · one example), no per-subcommand rows. The detailed
    # subcommand params/examples live under each command's own ``help`` (e.g.
    # `/config help`, `/skills help`), so the top level stays scannable.
    rows = _repl_quickstart_rows()
    styles = [_quickstart_row_style(row[0]) for row in rows]
    _command_table(
        "Interactive mode commands",
        ("command", "subcommands", "purpose", "example"),
        rows,
        row_styles=styles,
    )
    info(
        "For subcommands, options, and examples, enter `<command> help`, for example "
        "`/config help`, `/skills help`, `/task help`, `/serve help`, or `/channel help`."
    )
    # Echo what /init sets and how to adjust each item later, so users don't
    # re-run the whole wizard to tweak one thing (config/channel/... own these).
    init_cmd.render_init_config_map()


def _render_update_menu(latest: str) -> None:
    """Codex-style "update available" banner + numbered choices."""
    text = Text()
    text.append("A new version is available: ", "bold yellow")
    text.append(f"{__version__} -> {latest}\n", "bold")
    text.append("  Update with `omni update` or reinstall using your package manager.\n", "dim")
    text.append("› 1. Update now\n", "cyan")
    text.append("  2. Skip\n")
    text.append("  3. Ignore this version")
    console.print(text)


def _fresh_cli_command(args: list[str]) -> list[str]:
    """Run a child through the directly invoked launcher when available.

    An installed uv/pipx launcher carries its external bin directory into the
    child. That lets owner-aware package updates bind both the exact registry
    and its app exposure directory. ``python -m`` remains the development and
    test fallback.
    """
    candidate = Path(sys.argv[0]).expanduser()
    if (
        candidate.name.lower() in {"omni", "omni.exe"}
        and candidate.exists()
    ):
        return [str(candidate.absolute()), *args]
    return [sys.executable, "-m", "omni.cli.main", *args]


def _run_update_now() -> bool:
    """Run the same parameterless update command exposed to shell users."""
    info("Updating with `omni update`...")
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell.
            _fresh_cli_command(["update"]), check=False
        )
    except Exception as exc:  # noqa: BLE001
        warn(f"Automatic update failed: {exc}. Run `omni update` manually.")
        return False
    if proc.returncode == 0:
        success("Update completed; restarting omni on the installed version.")
        return True
    warn("Update did not complete. Run `omni update` for details.")
    return False


def _maybe_prompt_update(settings: OmniSettings) -> bool:
    """Prompt to update at REPL startup when a newer version is cached.

    Reads the cache a prior background refresh wrote, so startup never blocks.
    Only an interactive TTY gets the menu; a piped REPL gets a one-line hint.
    Returns ``True`` when an update was launched (so the caller skips the
    background refresh it would otherwise kick off).
    """
    paths = settings.paths
    if paths is None:
        return False
    try:
        latest = update_check.pending_update_notice(__version__, paths, settings)
    except Exception:  # noqa: BLE001 - the notifier must never break startup.
        return False
    if not latest:
        # No versioned update, but a git branch channel (e.g. --channel master)
        # keeps a static version as it advances — hint on new commits instead.
        try:
            branch = update_check.pending_channel_notice(paths, settings)
        except Exception:  # noqa: BLE001 - the notifier must never break startup.
            branch = None
        if branch:
            console.print(
                f"[dim]OmniScientist has new commits on {branch}; run `omni update` to get the latest.[/dim]"
            )
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print(
            f"[dim]OmniScientist {latest} is available (current {__version__}); run `omni update`.[/dim]"
        )
        return False
    _render_update_menu(latest)
    try:
        choice = console.input("[bold cyan]›[/bold cyan] Select an option (Enter=1): ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return False
    if choice in ("", "1"):
        return _run_update_now()
    if choice == "3":
        update_check.mark_skip_version(paths, latest)
        info(f"Ignored version {latest}; notifications resume for a newer release.")
    else:
        info("Update skipped. Run `omni update` at any time.")
    return False


def _repl(state: AppState, *, resume_session_id: str | None = None) -> None:
    run_async(_repl_async(state, resume_session_id=resume_session_id))


@dataclass
class ReplCommandResult:
    """State returned by the single REPL slash-command dispatcher."""

    agent: object
    session_id: str
    restart: bool = False
    resume_after_restart: bool = False


@dataclass
class _ReplControls:
    """Mutable REPL-loop settings shared with the in-turn command fast-path.

    ``/mode`` and ``/verbose`` must take effect immediately even when typed while a
    turn is running, so the loop keeps them here (rather than as plain locals) and the
    turn monitor mutates the same object.
    """

    interaction_mode: str
    display_verbosity: str


@dataclass
class _ForegroundTurnOutcome:
    turn: object | None
    queued_lines: list[ReplSubmission]
    exit_requested: bool = False
    turn_error: BaseException | None = None


def _settle_foreground_outcome(
    pending_lines: deque[ReplSubmission],
    tui: ReplTui,
    outcome: _ForegroundTurnOutcome,
) -> tuple[object | None, Exception | None]:
    """Commit queued input before classifying the completed turn outcome.

    An ordinary turn exception fails only that turn; the REPL stays alive and
    consumes the preserved queue on its next iteration. Process/control
    exceptions still propagate after the queue has been committed.
    """
    pending_lines.extend(outcome.queued_lines)
    _reindex_pending_turns(tui, pending_lines)
    turn_error = outcome.turn_error
    if turn_error is None:
        return outcome.turn, None
    if isinstance(
        turn_error,
        (asyncio.CancelledError, KeyboardInterrupt, SystemExit, ReplInterrupt),
    ):
        raise turn_error
    if isinstance(turn_error, Exception):
        return None, turn_error
    raise turn_error


async def _run_live_repl_command(
    *,
    agent,  # noqa: ANN001
    state: AppState,
    session_id: str,
    tui: ReplTui,
    controls: _ReplControls,
    submitted: ReplSubmission,
) -> None:
    """Run one read-only / UI slash command immediately during an active turn.

    Codex parity: ``available_during_task`` commands run in the UI *concurrently* with
    the running task instead of queuing behind it. Output binds to the submitting
    turn's slot via a task-local ``use_output_turn`` (``_output_turn_id`` is a
    ``ContextVar``), so it stays isolated from the turn's own live output. Best-effort:
    a failure here must never disturb the turn it runs alongside.
    """
    turn_id = submitted.turn_id
    value = submitted.text.strip()
    cmd = value.split(maxsplit=1)[0].lstrip("/").lower()
    try:
        with use_output_turn(turn_id):
            if cmd == "copy":
                tui.copy_last_answer()
            elif cmd == "soul":
                _render_persona_status(agent.paths.local_ops_dir, startup=False)
            elif cmd == "mode":
                requested = value.removeprefix("/mode").strip().lower()
                if not requested:
                    info(
                        "Current interaction mode: "
                        f"{controls.interaction_mode} (auto, plan, or review)"
                    )
                elif requested in {"auto", "plan", "review"}:
                    controls.interaction_mode = requested
                    info(f"Interaction mode changed to {controls.interaction_mode}")
                else:
                    warn("Usage: /mode auto|plan|review")
            elif cmd == "verbose":
                requested = value.removeprefix("/verbose").strip().lower()
                if not requested:
                    info(
                        "Live progress verbosity: "
                        f"{controls.display_verbosity} (quiet, normal, or verbose)"
                    )
                elif requested in VERBOSITY_LEVELS:
                    controls.display_verbosity = requested
                    info(f"Live progress verbosity changed to {controls.display_verbosity}")
                else:
                    warn("Usage: /verbose quiet|normal|verbose")
            else:
                # /task, /context, /inbox, /help — all read-only in ``_repl_command``.
                await _repl_command(agent, state, value, session_id)
    except Exception as exc:  # noqa: BLE001 - a live helper must never sink the turn.
        with use_output_turn(turn_id):
            warn(f"Could not run {value!r} during the active turn: {exc}")
    finally:
        if turn_id:
            tui.set_turn_state(turn_id, "control")


async def _monitor_foreground_turn(
    turn_task: asyncio.Task,
    *,
    tui: ReplTui,
    agent,
    task_ref: dict[str, str],
    state: AppState,
    session_id: str,
    controls: _ReplControls,
) -> _ForegroundTurnOutcome:  # noqa: ANN001
    """Keep the composer responsive while one agent turn is running."""
    queued: list[ReplSubmission] = []
    queued_turn_ids: set[str] = set()
    pending_steers: dict[str, tuple[ReplSubmission, str, str]] = {}
    live_tasks: list[asyncio.Task] = []
    cancellation = CancellationEscalator()
    exit_requested = False

    def queue_for_next(submitted: ReplSubmission, *, text: str | None = None) -> None:
        """Queue one submission exactly once, including rejected same-tick steers."""
        if submitted.turn_id in queued_turn_ids:
            return
        queued_turn_ids.add(submitted.turn_id)
        queued.append(
            ReplSubmission(
                turn_id=submitted.turn_id,
                text=submitted.text if text is None else text,
                disposition="queue",
            )
        )
        for index, item in enumerate(queued, start=1):
            tui.set_turn_state(item.turn_id, f"queued {index}")

    async def request_control(action: str, instruction: str = "") -> str:
        task_id = task_ref.get("task_id", "")
        if action == "cancel":
            cancel_mode = cancellation.request()
            tui.set_status(
                "stopping" if cancel_mode == "cooperative" else "forcing stop"
            )
            if cancel_mode == "force" or not task_id:
                turn_task.cancel()
                return "forced"
        if not task_id:
            return ""
        try:
            try_request = getattr(agent.tasks, "try_request_control", None)
            if callable(try_request):
                accepted = await try_request(
                    task_id,
                    action=action,
                    instruction=instruction,
                )
                if accepted is None:
                    return ""
                return str(getattr(accepted, "id", "") or "accepted")
            request = getattr(agent.tasks, "request_control", None)
            if not callable(request):
                return False
            await request(task_id, action=action, instruction=instruction)
            return "accepted"
        except (LookupError, ValueError) as exc:
            if not turn_task.done():
                warn(str(exc))
            return ""

    async def _outcome() -> _ForegroundTurnOutcome:
        cancellation.reset()
        # Let any in-flight read-only helpers finish streaming before the loop repaints.
        if live_tasks:
            await asyncio.gather(*live_tasks, return_exceptions=True)
        turn: object | None = None
        turn_error: BaseException | None = None
        try:
            turn = turn_task.result()
        except BaseException as exc:  # preserve queued input before the caller re-raises
            turn_error = exc
        delivered_control_ids = set(
            getattr(turn or turn_error, "_delivered_control_ids", ()) or ()
        )
        status_reader = getattr(agent.tasks, "control_status", None)
        requeue_control = getattr(
            agent.tasks,
            "requeue_unapplied_control",
            None,
        )
        if callable(status_reader):
            for submitted, control_id, instruction in list(
                pending_steers.values()
            ):
                if control_id in delivered_control_ids:
                    status = "applied"
                elif callable(requeue_control):
                    status = (
                        "requeued"
                        if await requeue_control(control_id)
                        else await status_reader(control_id)
                    )
                else:
                    status = await status_reader(control_id)
                if status == "requeued" or (
                    not callable(requeue_control) and status != "applied"
                ):
                    queue_for_next(submitted, text=instruction)
                else:
                    tui.set_turn_state(submitted.turn_id, "control")
        pending_steers.clear()
        return _ForegroundTurnOutcome(
            turn=turn,
            queued_lines=queued,
            exit_requested=exit_requested,
            turn_error=turn_error,
        )

    while True:
        if turn_task.done():
            return await _outcome()
        submission = asyncio.create_task(tui.read_submission_async())
        done, _ = await asyncio.wait(
            {turn_task, submission},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if turn_task in done and submission not in done:
            submission.cancel()
            await asyncio.gather(submission, return_exceptions=True)
            return await _outcome()
        try:
            submitted = submission.result()
            value = submitted.text.strip()
        except (KeyboardInterrupt, ReplInterrupt):
            await request_control("cancel")
            continue
        except EOFError:
            exit_requested = True
            await request_control("cancel")
            continue

        if submitted.disposition == "queue":
            queue_for_next(submitted)
        elif submitted.disposition == "steer":
            with use_output_turn(submitted.turn_id):
                control_id = await request_control("steer", value)
                if control_id:
                    tui.set_turn_state(submitted.turn_id, "control")
                    tui.set_status("steering queued")
                    if control_id not in {"accepted", "forced"}:
                        pending_steers[submitted.turn_id] = (
                            submitted,
                            control_id,
                            value,
                        )
                else:
                    queue_for_next(submitted)
        elif value in {"/exit", "/quit"}:
            tui.set_turn_state(submitted.turn_id, "control")
            exit_requested = True
            with use_output_turn(submitted.turn_id):
                await request_control("cancel")
        elif value == "/stop":
            tui.set_turn_state(submitted.turn_id, "control")
            with use_output_turn(submitted.turn_id):
                await request_control("cancel")
        elif value == "/steer" or value.startswith("/steer "):
            instruction = value.removeprefix("/steer").strip()
            with use_output_turn(submitted.turn_id):
                if instruction:
                    control_id = await request_control("steer", instruction)
                    if control_id:
                        tui.set_turn_state(submitted.turn_id, "control")
                        tui.set_status("steering queued")
                        if control_id not in {"accepted", "forced"}:
                            pending_steers[submitted.turn_id] = (
                                submitted,
                                control_id,
                                instruction,
                            )
                    else:
                        queue_for_next(submitted, text=instruction)
                else:
                    tui.set_turn_state(submitted.turn_id, "control")
                    warn("Usage: /steer <instruction>")
        else:
            cmd_name = (
                value.split(maxsplit=1)[0].lstrip("/").lower()
                if value.startswith("/")
                else ""
            )
            if cmd_name in _REPL_LIVE_DURING_TURN:
                # Fast-path: run the read-only / UI verb now, concurrent with the turn.
                tui.set_turn_state(submitted.turn_id, "control")
                live_tasks.append(
                    asyncio.create_task(
                        _run_live_repl_command(
                            agent=agent,
                            state=state,
                            session_id=session_id,
                            tui=tui,
                            controls=controls,
                            submitted=submitted,
                        )
                    )
                )
            elif cmd_name in _REPL_BLOCKED_DURING_TURN:
                # Cannot run now and unsafe to defer: report unavailable (Codex parity).
                tui.set_turn_state(submitted.turn_id, "control")
                with use_output_turn(submitted.turn_id):
                    warn(
                        f"/{cmd_name} is unavailable while a turn is running; "
                        "press /stop first, then run it."
                    )
            else:
                queue_for_next(submitted)


def _turn_header_state(turn: object) -> str:
    """Map a completed agent result to the compact state shown on its input row."""
    kind = str(getattr(turn, "kind", "") or "").lower()
    verification = str(getattr(turn, "verification_status", "") or "").lower()
    termination = str(getattr(turn, "terminated_reason", "") or "").lower()
    if kind == "needs_input" or verification == "needs_input":
        return "needs input"
    if kind == "error" or verification == "failed":
        return "failed"
    if termination.split(":", 1)[0] == "cancelled":
        return "cancelled"
    if kind == "partial" or verification in {"degraded", "salvaged"}:
        return "degraded"
    return ""


def _reindex_pending_turns(tui: ReplTui | None, pending: deque[ReplSubmission]) -> None:
    if tui is None:
        return
    for index, item in enumerate(pending, start=1):
        tui.set_turn_state(item.turn_id, f"queued {index}")


async def _await_classic_foreground_turn(
    turn_task: asyncio.Task,
    *,
    agent,
    task_ref: dict[str, str],
):  # noqa: ANN001, ANN201
    """Map classic-mode Ctrl+C to cooperative turn cancellation."""
    try:
        return await asyncio.shield(turn_task)
    except asyncio.CancelledError:
        task_id = task_ref.get("task_id", "")
        if task_id:
            await agent.tasks.request_control(task_id, action="cancel")
            warn("Cancellation requested; waiting for the active operation to stop cleanly.")
        else:
            turn_task.cancel()
        return await asyncio.shield(turn_task)


async def _shutdown_repl_resources(
    *,
    agent,
    session_id: str,
    inbox_watcher,
    tui: ReplTui | None,
    timeout_seconds: float = 5.0,
) -> None:  # noqa: ANN001
    """Close REPL resources independently within one bounded deadline."""
    try:
        inbox_watcher.stop()
    except Exception as exc:  # noqa: BLE001
        warn(f"Could not stop the inbox watcher cleanly: {exc}")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, timeout_seconds)

    async def close_step(label: str, awaitable) -> None:  # noqa: ANN001
        remaining = deadline - loop.time()
        if remaining <= 0:
            warn(f"Shutdown deadline reached before closing {label}.")
            remaining = 0.01
        try:
            await asyncio.wait_for(awaitable, timeout=remaining)
        except TimeoutError:
            warn(f"Timed out while closing {label}.")
        except Exception as exc:  # noqa: BLE001
            warn(f"Could not close {label} cleanly: {exc}")

    await close_step("session", agent.end_session(session_id))
    await close_step("agent", agent.aclose())
    if tui is not None:
        await close_step("terminal UI", tui.close())


async def _repl_async(state: AppState, *, resume_session_id: str | None = None) -> None:
    # The cached check is local and fast. If the user accepts an update, replace
    # this process before constructing any agent from the old imported package.
    launch_settings = state.settings()
    if _maybe_prompt_update(launch_settings):
        _relaunch_omni()
        return
    update_check.maybe_refresh_in_background(launch_settings)
    agent = await make_agent(state)
    s = agent.settings
    if resume_session_id:
        session_id = resume_session_id
        warn(f"Resumed session {session_id[:8]}.")
    else:
        session_id = await agent.ensure_session(channel="cli", reuse_latest=False)
    input_guard = _TerminalInputGuard()
    # One catalog (command names + subcommands + options) drives completion on both
    # interactive surfaces, so the menu, the dispatcher, and /help stay in sync.
    commands: CommandCatalog = build_command_catalog(app)
    tui: ReplTui | None = None
    if resolve_ui_mode(str(getattr(s.display, "ui_mode", "auto") or "auto")) == "tui":
        candidate = ReplTui(
            commands=commands,
            diagnostic_log_path=agent.paths.logs_dir / "omni-tui.log",
        )
        try:
            await candidate.start()
        except Exception as exc:  # noqa: BLE001 - auto mode must safely fall back.
            await candidate.close()
            warn(f"Full-screen UI unavailable ({exc}); using classic mode.")
        else:
            tui = candidate
            from omni.cli.approval_prompt import build_tui_approver

            agent.approver = build_tui_approver(tui)
    input_box = tui or ReplInputBox(commands=commands)
    banner(_repl_banner_text(agent.paths.project_name, s))
    _show_repl_quickstart(s.model)
    # Contextual SoulAgent discovery hint: silent unless a persona is active or the
    # project ships a ``scientist-kg/`` (so ordinary sessions look identical).
    _render_persona_status(agent.paths.local_ops_dir, startup=True)
    restart_notice = consume_restart_notice(os.environ)
    if restart_notice:
        success(restart_notice)
    inbox_watcher = _ReplInboxWatcher(agent.paths, session_id)
    inbox_watcher.start()
    restart_after = False
    resume_after_restart = False
    controls = _ReplControls(
        interaction_mode=str(getattr(s.interaction, "default_mode", "auto") or "auto"),
        display_verbosity=resolve_verbosity(s),
    )
    pending_lines: deque[ReplSubmission] = deque()
    try:
        while True:
            try:
                await _refresh_repl_input_status(input_box, agent, session_id)
                if pending_lines:
                    submitted: str | ReplSubmission = pending_lines.popleft()
                    _reindex_pending_turns(tui, pending_lines)
                else:
                    submitted = await _read_repl_line_async(
                        input_guard,
                        input_box=input_box,
                        mode=controls.interaction_mode,
                    )
                if isinstance(submitted, ReplSubmission):
                    turn_id = submitted.turn_id
                    line = submitted.text.strip()
                    if tui is not None:
                        tui.set_turn_state(turn_id, "planning")
                else:
                    turn_id = ""
                    line = submitted.strip()
            except EOFError:
                console.print()
                break
            except (KeyboardInterrupt, ReplInterrupt):
                console.print()
                warn("Current input cancelled. Enter /exit to quit.")
                continue
            except TuiApplicationError as exc:
                if tui is None:
                    raise
                await tui.close()
                warn(f"Full-screen UI stopped ({exc}); continuing in classic mode.")
                from omni.cli.approval_prompt import build_cli_approver

                agent.approver = build_cli_approver()
                tui = None
                input_box = ReplInputBox(commands=commands)
                continue
            if not line:
                if tui is not None and turn_id:
                    tui.set_turn_state(turn_id, "control")
                continue
            if line in ("/exit", "/quit"):
                if tui is not None and turn_id:
                    tui.set_turn_state(turn_id, "control")
                break
            if line == "/mode" or line.startswith("/mode "):
                with use_output_turn(turn_id):
                    requested = line.removeprefix("/mode").strip().lower()
                    if not requested:
                        info(
                            "Current interaction mode: "
                            f"{controls.interaction_mode} (auto, plan, or review)"
                        )
                    elif requested in {"auto", "plan", "review"}:
                        controls.interaction_mode = requested
                        info(f"Interaction mode changed to {controls.interaction_mode}")
                    else:
                        warn("Usage: /mode auto|plan|review")
                if tui is not None and turn_id:
                    tui.set_turn_state(turn_id, "control")
                continue
            if line == "/verbose" or line.startswith("/verbose "):
                with use_output_turn(turn_id):
                    requested = line.removeprefix("/verbose").strip().lower()
                    if not requested:
                        info(
                            "Live progress verbosity: "
                            f"{controls.display_verbosity} (quiet, normal, or verbose)"
                        )
                    elif requested in VERBOSITY_LEVELS:
                        controls.display_verbosity = requested
                        info(f"Live progress verbosity changed to {controls.display_verbosity}")
                    else:
                        warn("Usage: /verbose quiet|normal|verbose")
                if tui is not None and turn_id:
                    tui.set_turn_state(turn_id, "control")
                continue
            if line == "/soul" or line.startswith("/soul "):
                with use_output_turn(turn_id):
                    _render_persona_status(agent.paths.local_ops_dir, startup=False)
                if tui is not None and turn_id:
                    tui.set_turn_state(turn_id, "control")
                continue
            if line == "/copy" or line.startswith("/copy "):
                # Copy the last answer to the clipboard (OSC 52). Only meaningful in
                # the managed dock, which owns the transcript and the terminal.
                if tui is not None:
                    tui.copy_last_answer()
                    if turn_id:
                        tui.set_turn_state(turn_id, "control")
                else:
                    with use_output_turn(turn_id):
                        info("/copy is available in the interactive dock (omni UI mode).")
                continue
            turn_mode = controls.interaction_mode
            if line.startswith("/plan "):
                turn_mode = "plan"
                line = line.removeprefix("/plan ").strip()
            elif line.startswith("/review "):
                turn_mode = "review"
                line = line.removeprefix("/review ").strip()
            first_tok = line.split(maxsplit=1)[0].lstrip("/")
            # Preserve the historical no-slash shorthand for the two setup
            # groups while routing both spellings through the same dispatcher.
            if not line.startswith("/") and first_tok in {"config", "skills"}:
                line = "/" + line
            if line.startswith("/"):
                try:
                    with use_output_turn(turn_id):
                        command_result = await _repl_command(agent, state, line, session_id)
                except Exception:
                    if tui is not None and turn_id:
                        tui.set_turn_state(turn_id, "failed")
                    raise
                if tui is not None and turn_id:
                    tui.set_turn_state(turn_id, "control")
                agent = command_result.agent
                session_id = command_result.session_id
                inbox_watcher.set_session(session_id)
                if command_result.restart:
                    restart_after = True
                    resume_after_restart = command_result.resume_after_restart
                    break
                continue
            display = TurnDisplay(
                verbosity=controls.display_verbosity,
                status_line=bool(getattr(agent.settings.display, "status_line", True)),
            )
            task_ref = {"task_id": ""}
            render_ack = task_ack_cb(False)

            def capture_ack(
                data: dict,
                task_ref: dict[str, str] = task_ref,
                render_ack=render_ack,
            ) -> None:  # noqa: ANN001
                task_ref["task_id"] = str(data.get("task_id") or "")
                render_ack(data)

            # Stream the answer live only in the managed TUI: it owns an in-place
            # markdown slot, so there is no classic raw-print/markdown double render.
            stream_on = tui is not None and bool(getattr(agent.settings.react, "stream", True))
            exit_requested = False
            try:
                with use_output_turn(turn_id):
                    display.begin("planning")
                    turn_task = asyncio.create_task(
                        agent.handle_turn(
                            line, session_id=session_id, channel="cli",
                            drain_tasks=not is_daemon_running(agent.paths),
                            on_tool_event=display.tool_event,
                            on_task_ack=capture_ack,
                            on_token=display.token if stream_on else None,
                            interaction_mode=turn_mode,
                        )
                    )
                    if tui is None:
                        turn = await _await_classic_foreground_turn(
                            turn_task,
                            agent=agent,
                            task_ref=task_ref,
                        )
                    else:
                        foreground = await _monitor_foreground_turn(
                            turn_task,
                            tui=tui,
                            agent=agent,
                            task_ref=task_ref,
                            state=state,
                            session_id=session_id,
                            controls=controls,
                        )
                        turn, turn_error = _settle_foreground_outcome(
                            pending_lines,
                            tui,
                            foreground,
                        )
                        exit_requested = foreground.exit_requested
                        if turn_error is not None:
                            tui.set_turn_state(turn_id, "failed")
                            warn(
                                "Current turn failed; queued input was preserved. "
                                f"{type(turn_error).__name__}: {turn_error}"
                            )
                            if exit_requested:
                                break
                            continue
                        if turn is None:  # pragma: no cover - outcome invariant
                            raise RuntimeError("foreground turn completed without a result")
                    render_turn_diagnostics(turn)
                    if not should_suppress_assistant_text(turn):
                        # In the TUI the answer already streamed into a live slot;
                        # finalize replaces the partial with the authoritative text.
                        if not display.finalize_answer(turn.text):
                            assistant_answer(turn.text)
                    render_tasks(turn, artifacts_dir=agent.paths.artifacts_dir)
            except Exception:
                if tui is not None and turn_id:
                    tui.set_turn_state(turn_id, "failed")
                raise
            finally:
                input_box.set_last_elapsed(display.end())
                if tui is not None and not pending_lines:
                    tui.set_busy(False)
            if tui is not None and turn_id:
                tui.set_turn_state(turn_id, _turn_header_state(turn))
            if exit_requested:
                break
    finally:
        hint = _background_service_exit_hint(agent.paths)
        try:
            await _shutdown_repl_resources(
                agent=agent,
                session_id=session_id,
                inbox_watcher=inbox_watcher,
                tui=tui,
            )
        finally:
            input_guard.restore()
        if hint and not restart_after:
            warn(hint)
        info("Restarting interactive mode..." if restart_after else "Exited interactive mode.")
    if restart_after:
        _relaunch_omni(continue_session=resume_after_restart)


def _relaunch_omni(*, continue_session: bool = False) -> None:
    """Replace this process with a fresh ``omni`` after a runtime-level change.

    Uses ``sys.orig_argv`` (the exact launch command, incl. interpreter) so the
    reopened REPL preserves the original global flags. This is used after an
    update or data-home switch. Best-effort: on failure it tells the user to
    restart manually rather than leaving a half-exited process.
    """
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    if continue_session and not any(arg in {"--continue", "-c"} for arg in argv[1:]):
        argv.append("--continue")
    remember_restart_notice(os.environ)
    try:
        os.execv(argv[0], argv)
    except OSError as exc:
        consume_restart_notice(os.environ)
        warn(f"Automatic restart failed ({exc}). Restart omni manually.")


async def _repl_update(state: AppState, line: str) -> bool:
    """Run ``omni update`` from the REPL; return True to relaunch on success.

    The update executes in a subprocess (which sees the new code and restarts any
    tracked ``omni serve``). This REPL process still holds the old code, so on a
    successful interactive update we offer to re-exec into the new version.
    """
    tui = _active_repl_tui()
    if tui is not None:
        try:
            args = _external_repl_command_args(state, line)
            command_tokens = shlex.split(line[1:] if line.startswith("/") else line)
        except ValueError:
            warn("Could not parse the command; check quotation marks.")
            return False
        if not classify_repl_command(command_tokens).mode.requires_terminal:
            returncode = await _stream_repl_external_command(tui, args)
            if returncode:
                warn(f"Command exited with code {returncode}.")
            return False
        async with tui.suspended():
            restart = _repl_update_in_terminal(state, line)
        # Committed through the dock's clean-repaint path (not info()) so the note
        # lands in scrollback rather than the input row after the child returns.
        tui.note_after_interactive(
            f"Interactive command finished: {_display_repl_command(line)} "
            "(terminal output was temporary)."
        )
        return restart
    return _repl_update_in_terminal(state, line)


def _repl_update_in_terminal(state: AppState, line: str) -> bool:
    try:
        args = _external_repl_command_args(state, line)
        command_tokens = shlex.split(line[1:] if line.startswith("/") else line)
    except ValueError:
        warn("Could not parse the command; check quotation marks.")
        return False
    proc = subprocess.run(  # noqa: S603 - args are shlex-parsed, no shell.
        _fresh_cli_command(args),
        check=False,
    )
    if proc.returncode:
        warn(f"Command exited with code {proc.returncode}.")
        return False
    read_only = not classify_repl_command(command_tokens).mode.requires_terminal
    if read_only or not sys.stdin.isatty():
        return False  # nothing changed, or not interactive → don't relaunch
    success("Update completed; restarting interactive mode on the installed version.")
    return True


def _background_service_exit_hint(paths) -> str:  # noqa: ANN001
    d = daemon_info(paths)
    if not d:
        return ""
    channels = d.get("channels")
    if isinstance(channels, list):
        channel_text = ",".join(str(c) for c in channels if str(c).strip()) or "configured channels"
    else:
        channel_text = str(d.get("channels_arg") or "configured channels")
    return (
        f"omni serve is still running (pid={d['pid']}, channels={channel_text}). "
        "Connected channels remain available; stop it with `omni serve stop`."
    )


def _refresh_repl_skill_registry(agent, state: AppState) -> None:  # noqa: ANN001
    settings = state.settings()
    if hasattr(agent, "settings"):
        agent.settings = settings
    if hasattr(agent, "paths"):
        agent.paths = settings.paths
    registry = getattr(agent, "registry", None)
    if registry is not None:
        registry.refresh_settings(settings)


def _registered_typer_children(command_app: typer.Typer) -> frozenset[str]:
    """Return direct command/group names from a Typer app."""
    names = {command.name for command in command_app.registered_commands if command.name}
    names.update(group.name for group in command_app.registered_groups if group.name)
    return frozenset(names)


def _registered_repl_bare_group_actions(command_app: typer.Typer) -> dict[str, str]:
    """Return successful REPL defaults for groups that otherwise exit with usage code 2."""
    actions: dict[str, str] = {}
    for group in command_app.registered_groups:
        if not group.name or group.typer_instance.info.no_args_is_help is not True:
            continue
        children = _registered_typer_children(group.typer_instance)
        actions[group.name] = "help" if "help" in children else "--help"
    if "task" in actions:
        actions["task"] = "list"
    if "schedule" in actions:
        actions["schedule"] = "list"
    return actions


_REPL_IN_PROCESS_COMMANDS = frozenset({
    "lit",
    "verify",
    "memory",
    "resume",
    "update",
    "upgrade",
})


# Codex parity (``codex-rs/tui/src/slash_command.rs`` → ``available_during_task``):
# read-only / pure-UI verbs run *immediately* even while a turn is in flight, instead
# of queuing behind it. They only read state (or touch the dock), so they run
# concurrently with the turn's own output — each gets a task-local output turn, and a
# failure is contained (never sinks the running turn). ``/task`` streams a child
# ``omni task …`` process; the rest dispatch in-process.
_REPL_LIVE_DURING_TURN = frozenset({
    "task", "context", "inbox", "copy", "help", "soul", "verbose", "mode",
})

# Verbs that relaunch the process (``os.execv``) and so cannot be deferred to fire right
# after a turn without silently killing the session. Like Codex, we report these as
# unavailable while a task runs rather than queuing them.
_REPL_BLOCKED_DURING_TURN = frozenset({"update", "upgrade"})


def _registered_repl_external_commands() -> set[str]:
    names = {command.name for command in app.registered_commands if command.name}
    names.update(group.name for group in app.registered_groups if group.name)
    return names - _REPL_IN_PROCESS_COMMANDS


_REPL_EXTERNAL_COMMANDS = _registered_repl_external_commands()
_REPL_BARE_GROUP_ACTIONS = _registered_repl_bare_group_actions(app)
_MEMORY_SUBCOMMANDS = _registered_typer_children(memory_cmd.app)
_UPDATE_SUBCOMMANDS = _registered_typer_children(update_cmd.app)
_SKILLS_SUBCOMMANDS = _registered_typer_children(skills_cmd.app)


def _repl_slash_commands() -> tuple[str, ...]:
    """Top-level ``/``-prefixed command names offered by the interactive completer.

    Derived from the single :class:`CommandCatalog` so it can never drift from what
    the dispatcher handles or ``/help`` documents (the historical ``/inbox`` gap).
    """
    return build_command_catalog(app).slash_names()


def _session_aware_external_line(line: str, session_id: str) -> str:
    """Translate a slash command to canonical CLI syntax plus REPL defaults."""
    tokens = shlex.split(line[1:] if line.startswith("/") else line)
    if not tokens:
        return ""

    command = tokens[0]
    if len(tokens) == 1 and command in _REPL_BARE_GROUP_ACTIONS:
        tokens.append(_REPL_BARE_GROUP_ACTIONS[command])

    if command == "skills" and len(tokens) > 1:
        candidate = tokens[1]
        if not candidate.startswith("-") and candidate not in _SKILLS_SUBCOMMANDS:
            tokens.insert(1, "search")

    if command == "task":
        if len(tokens) == 1:
            tokens.append("list")
        action = tokens[1]
        if action == "session" and (len(tokens) == 2 or tokens[2].startswith("-")):
            tokens.insert(2, session_id)
        if action == "attach" and not any(token in {"--session", "-s"} for token in tokens[2:]):
            tokens.extend(["--session", session_id])
        for option in ("--session", "-s"):
            if option in tokens:
                index = tokens.index(option)
                if index + 1 == len(tokens) or tokens[index + 1].startswith("-"):
                    tokens.insert(index + 1, session_id)
                break

    if command in {"current", "why"} and not any(
        token in {"--session", "-s"} for token in tokens[1:]
    ):
        tokens.extend(["--session", session_id])

    return shlex.join(tokens)


def _parse_lit_args(arg: str) -> tuple[str, int, bool, bool]:
    """Parse REPL `/lit "<q>" [--verify] [--k N] [-q]` → (question, k, verify, quiet)."""
    try:
        tokens = shlex.split(arg)
    except ValueError:
        warn("Could not parse /lit arguments; check quotation marks.")
        return "", 0, False, False
    q_parts: list[str] = []
    k, verify, quiet = 0, False, False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--verify":
            verify = True
        elif tok in {"--quiet", "-q"}:
            quiet = True
        elif tok == "--k" and i + 1 < len(tokens):
            try:
                k = int(tokens[i + 1])
            except ValueError:
                warn("--k requires an integer and was ignored.")
            i += 1
        else:
            q_parts.append(tok)
        i += 1
    return " ".join(q_parts), k, verify, quiet


def _parse_verify_session(arg: str, current_session_id: str) -> str:
    """Parse REPL `/verify [--session [id]]`; bare `--session` → active session."""
    try:
        tokens = shlex.split(arg)
    except ValueError:
        return ""
    i = 0
    while i < len(tokens):
        if tokens[i] in {"--session", "-s"}:
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                return tokens[i + 1]
            return current_session_id
        i += 1
    return ""  # whole workspace (mirrors `omni verify`)


def _external_repl_command_args(state: AppState, line: str) -> list[str]:
    """Translate `/foo ...` in the REPL to `omni [global flags] foo ...`."""
    tokens = shlex.split(line[1:] if line.startswith("/") else line)
    args: list[str] = []
    if state.project:
        args.extend(["--project", state.project])
    if state.profile:
        args.extend(["--profile", state.profile])
    if state.model:
        args.extend(["--model", state.model])
    args.extend(tokens)
    return args


async def _run_repl_external_command(state: AppState, line: str) -> int:
    """Run a normal CLI command from inside the REPL."""
    try:
        args = _external_repl_command_args(state, line)
    except ValueError:
        warn("Could not parse the command; check quotation marks.")
        return 2
    command_tokens = shlex.split(line[1:] if line.startswith("/") else line)
    policy = classify_repl_command(command_tokens)
    tui = _active_repl_tui()
    if tui is None:
        returncode = int(
            subprocess.run(  # noqa: S603 - arguments are shlex parsed, no shell.
                [sys.executable, "-m", "omni.cli.main", *args],
                check=False,
            ).returncode
        )
    elif policy.mode.requires_terminal:
        async with tui.suspended():
            proc = subprocess.run(  # noqa: S603 - arguments are shlex parsed, no shell.
                [sys.executable, "-m", "omni.cli.main", *args],
                check=False,
                env=_repl_child_env(tui),
            )
        returncode = int(proc.returncode)
        # The child owned the raw terminal, so route the completion note through the
        # dock's clean-repaint path — a plain info()/warn() here would commit at a
        # stale cursor position and land inside the input row.
        if returncode == 0:
            tui.note_after_interactive(
                f"Interactive command finished: {_display_repl_command(line)} "
                "(terminal output was temporary)."
            )
        else:
            tui.note_after_interactive(
                f"Interactive command exited with code {returncode}: "
                f"{_display_repl_command(line)}.",
                style="warn",
            )
    else:
        returncode = await _stream_repl_external_command(tui, args)
    if returncode and not (tui is not None and policy.mode.requires_terminal):
        warn(f"Command exited with code {returncode}.")
    return returncode


async def _stream_repl_external_command(tui: ReplTui, args: list[str]) -> int:
    """Stream a non-interactive child command into the managed transcript."""
    from omni.cli.repl_output import TranscriptWireDecoder
    from omni.runtime.processes import process_group_options, stop_process_tree

    env = _repl_child_env(tui, structured=True)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "omni.cli.main",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        **process_group_options(),
    )
    decoder = TranscriptWireDecoder()
    try:
        if proc.stdout is not None:
            while chunk := await proc.stdout.read(4096):
                for event in decoder.feed(chunk):
                    tui.publish_event(event)
        for event in decoder.finish():
            tui.publish_event(event)
        return int(await proc.wait())
    except asyncio.CancelledError:
        await stop_process_tree(proc)
        raise


def _repl_child_env(tui: ReplTui, *, structured: bool = False) -> dict[str, str]:
    """Build a child environment from the live TUI size, never stale shell columns."""
    from omni.cli.repl_output import TRANSCRIPT_PROTOCOL_ENV

    rows, columns = tui.terminal_size()
    env = os.environ.copy()
    env["COLUMNS"] = str(columns)
    env["LINES"] = str(rows)
    if structured:
        env[TRANSCRIPT_PROTOCOL_ENV] = "1"
    else:
        env.pop(TRANSCRIPT_PROTOCOL_ENV, None)
    return env


def _active_repl_tui() -> ReplTui | None:
    sink = get_output_sink()
    return sink if isinstance(sink, ReplTui) else None


def _external_requires_terminal(tokens: list[str]) -> bool:
    """Compatibility wrapper for the argument-aware command execution policy."""
    return classify_repl_command(tokens).mode.requires_terminal


def _display_repl_command(line: str) -> str:
    """Return a slash-prefixed, credential-safe command for transcript summaries."""
    command = line.strip()
    if command and not command.startswith("/"):
        command = f"/{command}"
    return redact_repl_command(command)


async def _repl_command(
    agent, state: AppState, line: str, session_id: str,  # noqa: ANN001
) -> ReplCommandResult:
    """Dispatch one slash command through Typer unless live session state is required."""
    parts = line.split(maxsplit=1)
    cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    command = cmd.lstrip("/")
    restart = False
    resume_after_restart = False

    if cmd == "/help":
        _show_repl_help()
    elif cmd == "/clear":
        option = arg.strip()
        if option == "--screen":
            if not redraw_active_output():
                console.clear()
        elif option:
            warn("Usage: /clear [--screen]")
        else:
            before = await agent.context_snapshot(session_id, include_injected=False)
            info("Persisting durable memory and starting a clean context...")
            await agent.end_session(session_id)
            session_id = await agent.ensure_session(channel="cli", reuse_latest=False)
            if not clear_active_output():
                console.clear()
            info(
                f"Started clean session {session_id[:8]}; removed about "
                f"{before.clearable_tokens:,} transcript tokens from active context."
            )
            info("Previous history, tasks, artifacts, research records, and durable memory remain available.")
    elif cmd == "/new":
        await agent.end_session(session_id)
        session_id = await agent.ensure_session(channel="cli", reuse_latest=False)
        info("Started a new session.")
    elif cmd == "/resume":
        session_id = await _repl_resume(agent, state, arg, session_id)
    elif cmd == "/inbox":
        from omni.cli.commands.tasks_cmd import render_inbox

        render_inbox(agent.paths, limit=10)
    elif cmd == "/stop":
        task = await agent.tasks.active_task_for_session(session_id)
        if task is None:
            info("No active task is running in this session.")
        else:
            await agent.tasks.request_control(task.id, action="cancel")
            info(f"Cancellation requested for task {task.id[:8]}.")
    elif cmd == "/steer":
        task = await agent.tasks.active_task_for_session(session_id)
        if not arg.strip():
            warn("Usage: /steer <instruction>")
        elif task is None:
            info("No active task is running in this session.")
        else:
            await agent.tasks.request_control(task.id, action="steer", instruction=arg)
            info(f"Steering submitted to task {task.id[:8]}.")
    elif cmd == "/memory":
        await _repl_memory(agent, state, arg)
    elif cmd == "/compact":
        info("Persisting memory and compacting older conversation history...")
        stats = await agent.compact_session(session_id, keep_last=8)
        if stats.get("compacted"):
            info(
                f"Compacted {stats['compacted']} older messages and kept the latest "
                f"{stats.get('kept', 8)}; prompt history changed from about "
                f"{stats.get('before_tokens', 0):,} to {stats.get('after_tokens', 0):,} tokens "
                f"(saved about {stats.get('saved_tokens', 0):,})."
            )
        else:
            info(
                "This session is short enough that compaction is unnecessary "
                f"(about {stats.get('before_tokens', 0):,} transcript tokens)."
            )
    elif cmd == "/context":
        report = await agent.context_report(session_id)
        console.print(report)
    elif cmd == "/lit":
        # Keep literature QA in-process so claims bind to the active session.
        from omni.cli.commands.lit_cmd import render_lit, render_lit_usage_help

        question, k, verify, quiet = _parse_lit_args(arg)
        if question in {"help", "--help", "-h"}:
            render_lit_usage_help()
        elif not question:
            warn('Usage: /lit "your question" [--verify] [--k N]')
        else:
            await render_lit(
                agent,
                question,
                k=k,
                verify=verify,
                quiet=quiet,
                session_id=session_id,
            )
    elif cmd == "/verify":
        try:
            tokens = shlex.split(arg)
        except ValueError:
            warn("Could not parse /verify arguments; check quotation marks.")
            return ReplCommandResult(agent=agent, session_id=session_id)
        supported = (
            not tokens
            or tokens in (["--session"], ["-s"])
            or (
                len(tokens) == 2
                and tokens[0] in {"--session", "-s"}
                and not tokens[1].startswith("-")
            )
        )
        if supported:
            from omni.cli.commands.verify_cmd import render_verify_report

            await render_verify_report(agent, session=_parse_verify_session(arg, session_id))
        else:
            await _run_repl_external_command(state, line)
    elif command in {"update", "upgrade"}:
        restart = await _repl_update(state, line)
        resume_after_restart = restart
    elif command in _REPL_EXTERNAL_COMMANDS:
        try:
            external_line = _session_aware_external_line(line, session_id)
        except ValueError:
            warn("Could not parse the command; check quotation marks.")
        else:
            returncode = await _run_repl_external_command(state, external_line)
            if returncode == 0 and command == "skills":
                _refresh_repl_skill_registry(agent, state)
            elif returncode == 0 and command == "config":
                tokens = shlex.split(external_line)
                subcommand = tokens[1] if len(tokens) > 1 else ""
                home_changed = subcommand == "home" and (
                    len(tokens) > 2 or "--reset" in tokens
                )
                if home_changed:
                    # Re-exec the whole REPL so its agent, session, inbox watcher,
                    # and daemon checks all resolve the same newly selected home.
                    restart = True
                elif subcommand in {"set", "model", "embeddings", "unset"}:
                    await agent.aclose()
                    agent = await make_agent(state)
    else:
        warn(f"Unknown command: {cmd}. Use /help.")

    return ReplCommandResult(
        agent=agent,
        session_id=session_id,
        restart=restart,
        resume_after_restart=resume_after_restart,
    )


async def _repl_memory(agent, state: AppState, arg: str) -> None:  # noqa: ANN001
    """REPL `/memory` — dispatch to `omni memory <sub>`; bare text → recall.

    `/memory list`, `/memory rm <id>`, `/memory help`, … run the real
    subcommand (so the REPL matches the advertised subcommands), while
    `/memory <free text>` stays a quick semantic recall shortcut.
    """
    from omni.cli.commands.memory_cmd import render_memory_usage_help

    try:
        first = shlex.split(arg)[0] if arg.strip() else ""
    except ValueError:
        first = arg.split(maxsplit=1)[0] if arg.strip() else ""
    if first in _MEMORY_SUBCOMMANDS or first in {"--help", "-h"}:
        await _run_repl_external_command(state, f"/memory {arg}".rstrip())
        return
    if not arg.strip():
        render_memory_usage_help()
        return
    res = await agent.memory.recall(arg, limit=8, cross_session=True)
    if not res:
        info("No relevant memory was found. Use `/memory help` for subcommands.")
        return
    for m in res:
        console.print(f"  [{m.entry.layer}/{m.entry.memory_type}] {m.entry.summary[:90]}")


async def _repl_resume(
    agent, state: AppState, arg: str, session_id: str,  # noqa: ANN001
) -> str:
    """Switch the live REPL to another session in this workspace."""
    from omni.cli.render import data_table

    try:
        tokens = shlex.split(arg)
    except ValueError:
        warn("Could not parse /resume arguments; check quotation marks.")
        return session_id
    if tokens and tokens[0] in {"help", "--help", "-h"}:
        await _run_repl_external_command(state, "/resume help")
        return session_id
    if tokens and tokens[0] in {"--thread", "-t"}:
        if len(tokens) != 2:
            await _run_repl_external_command(state, "/resume " + arg)
            return session_id
        from omni.cli.commands.resume_cmd import _thread_resume

        brief, thread_session = await _thread_resume(state, tokens[1])
        if brief is None:
            warn(f"Research thread (hypothesis) {tokens[1]} was not found.")
            return session_id
        console.print(brief)
        console.rule(style="cyan")
        if thread_session is None:
            await agent.end_session(session_id)
            session_id = await agent.ensure_session(channel="cli", reuse_latest=False)
            info("This research thread has no associated session; started a new one.")
            return session_id
        tokens = [thread_session]
    elif len(tokens) > 1 or (tokens and tokens[0].startswith("-") and tokens[0] not in {"--last", "-l"}):
        await _run_repl_external_command(state, "/resume " + arg)
        return session_id

    rows = await agent.list_sessions(limit=30)
    if not rows:
        info("This workspace has no previous sessions.")
        return session_id
    target = tokens[0] if tokens else ""
    if target in {"--last", "-l"}:
        sid = rows[0][0].id
        await agent.touch_session(sid)
        info(f"Switched to the most recent session {sid[:8]}.")
        return sid
    if not target:
        data_table(
            "Session history (current workspace)", ["#", "id", "title", "updated", "msgs"],
            [[str(i), s.id[:8], (s.title or "-")[:40], format_local_time(s.updated_at), n]
             for i, (s, n) in enumerate(rows, 1)],
        )
        selection_guard = _TerminalInputGuard()
        try:
            tui = _active_repl_tui()
            if tui is None:
                target = _read_repl_line(
                    selection_guard,
                    "Select a session by number or ID prefix; Enter cancels: ",
                ).strip()
            else:
                async with tui.suspended():
                    target = _read_repl_line(
                        selection_guard,
                        "Select a session by number or ID prefix; Enter cancels: ",
                    ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            warn("Session resume cancelled.")
            return session_id
        finally:
            selection_guard.restore()
        if not target:
            return session_id
    if target.isdigit():
        idx = int(target)
        if not (1 <= idx <= len(rows)):
            warn("Selection is out of range.")
            return session_id
        sid = rows[idx - 1][0].id
    else:
        sess = await agent.get_session(target)
        if sess is None:
            warn(f"Session {target} was not found.")
            return session_id
        sid = sess.id
    await agent.touch_session(sid)
    info(f"Switched to session {sid[:8]}.")
    return sid


if __name__ == "__main__":
    app()
