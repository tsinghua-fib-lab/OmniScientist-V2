"""`omni web` — loopback SPA over the same cwd-keyed stores as the CLI.

Bare ``omni web`` stays in the foreground (Ctrl+C / Ctrl+D). ``omni web start``
and REPL ``/web`` detach that same process and return so the prompt stays usable.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import typer

from omni.cli.render import data_table, error, info, success, warn
from omni.cli.state import AppState
from omni.runtime.daemon import pid_alive
from omni.runtime.logging_config import (
    configure_process_logging,
    parse_log_level,
    prepare_log_file,
)
from omni.runtime.web_service import (
    clear_pidfile,
    clear_pidfile_if_owner,
    port_listening,
    read_pidfile,
    terminate_pid,
    web_info,
    write_pidfile,
)
from omni.web.bind import DEFAULT_HOST, DEFAULT_PORT, ready_url, validate_bind_host
from omni.web.serve import run_web_server
from omni.web.static import (
    WebUiMissing,
    ensure_web_ui,
    package_version,
    spa_version,
    web_dist_dir,
)

app_help = "Serve the local Omni web UI on loopback (default 127.0.0.1:1088)."

app = typer.Typer(help=app_help, invoke_without_command=True, no_args_is_help=False)

_READY_WAIT_S = 8.0
_MANAGE = ("start", "stop", "status", "restart", "port", "help")
_MANAGED_LOG_STREAM_ENV = "OMNI_WEB_MANAGED_LOG_STREAM"

logger = logging.getLogger(__name__)


def _web_ui_mismatch_warning(*, ui_version: str, package_version: str) -> str:
    """Explain recovery without pretending an up-to-date check reinstalls assets."""
    return (
        f"Web UI {ui_version} does not match OmniScientist {package_version}. "
        "Reinstall the package and UI together, then restart omni web. "
        "For a published install, use `omni update` to install a newer published release; "
        "use `omni update --force` only when that release is known good and local assets "
        "are damaged. From a source checkout, use `omni update --local`. If either update "
        "path is unavailable or fails, run the repository installer: "
        "`./cli/scripts/install.sh --local` on macOS/Linux or "
        "`powershell -ExecutionPolicy Bypass -File .\\cli\\scripts\\install.ps1 -Local` "
        "on Windows."
    )


def _state(ctx: typer.Context) -> AppState:
    return ctx.obj if isinstance(ctx.obj, AppState) else AppState()


def _bind(host: str, port: int) -> tuple[str, int]:
    try:
        return validate_bind_host(host), int(port)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(2) from exc


def _require_web_extra() -> None:
    try:
        import uvicorn  # noqa: F401
    except ImportError as exc:
        error("omni web requires the [web] extra: pip install 'OmniScientist-V2[web]'")
        raise typer.Exit(2) from exc
    try:
        from omni.web.app import create_app  # noqa: F401
    except ImportError as exc:
        error("omni web requires the [web] extra: pip install 'OmniScientist-V2[web]'")
        raise typer.Exit(2) from exc


def _run_foreground(state: AppState, *, host: str, port: int) -> None:
    """Block this terminal on the loopback UI until Ctrl+C / Ctrl+D."""
    host, port = _bind(host, port)
    _require_web_extra()
    try:
        ensure_web_ui()
    except WebUiMissing as exc:
        error(str(exc).rstrip())
        raise typer.Exit(2) from exc

    settings = state.settings()
    paths = settings.paths
    existing = web_info(paths)
    if existing:
        error(f"omni web is already running: pid={existing['pid']} {existing['url']}")
        raise typer.Exit(1)

    url = ready_url(host, port)
    log_path = paths.logs_dir / f"web-{paths.project_name}.log"
    managed_stream = os.environ.get(_MANAGED_LOG_STREAM_ENV) == "1"
    process_logging = configure_process_logging(
        component="web",
        level=settings.observability.log_level,
        stream=sys.stderr if managed_stream else None,
        path=None if managed_stream else log_path,
        settings=settings,
    )

    def _on_ready() -> None:
        ui = spa_version(web_dist_dir()) or "unversioned"
        logger.info(
            "web server ready at %s with UI %s",
            url,
            ui,
            extra={"event": "server.ready"},
        )
        if not managed_stream:
            sys.stdout.write(f"omni web: {url}  UI {ui}\n")
            sys.stdout.flush()
        pkg = package_version()
        if ui not in {"", "unversioned"} and pkg and ui != pkg:
            mismatch = _web_ui_mismatch_warning(ui_version=ui, package_version=pkg)
            logger.warning(mismatch, extra={"event": "ui.version_mismatch"})
            if not managed_stream:
                warn(mismatch)

    try:
        from omni.web.app import create_app

        app_obj = create_app(trusted_hosts=(host,))
        write_pidfile(paths, host=host, port=port)
        run_web_server(
            app_obj,
            host=host,
            port=port,
            on_ready=_on_ready,
            log_level=parse_log_level(settings.observability.log_level),
        )
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as exc:
        logger.exception(
            "web server failed",
            extra={"event": "server.failed"},
        )
        if not managed_stream:
            error(f"omni web failed to start or run. See {log_path}")
        raise typer.Exit(1) from exc
    finally:
        clear_pidfile_if_owner(paths)
        process_logging.close()


def _detached_popen_kwargs() -> dict[str, object]:
    if sys.platform == "win32":
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": detached | new_group}
    return {"start_new_session": True}


def _global_args(state: AppState) -> list[str]:
    args: list[str] = []
    if state.project:
        args += ["--project", state.project]
    if state.profile:
        args += ["--profile", state.profile]
    if state.model:
        args += ["--model", state.model]
    artifacts = state.overrides.get("artifacts")
    if isinstance(artifacts, dict) and artifacts.get("output_dir"):
        args += ["--out", str(artifacts["output_dir"])]
    return args


def _foreground_argv(state: AppState, *, host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "omni.cli.main",
        *_global_args(state),
        "web",
        "--host",
        host,
        "--port",
        str(int(port)),
    ]


def _log_tail(path: Path, *, limit: int = 240) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return text[-limit:].replace("\n", " ").strip()


def start_web_process(state: AppState, *, host: str, port: int) -> tuple[bool, str]:
    """Detach a foreground ``omni web`` child and wait until it listens."""
    host, port = _bind(host, port)
    paths = state.settings().paths
    existing = web_info(paths)
    if existing:
        return True, f"omni web is already running: pid={existing['pid']} {existing['url']}"

    log_path = prepare_log_file(paths.logs_dir / f"web-{paths.project_name}.log")
    argv = _foreground_argv(state, host=host, port=port)
    child_env = os.environ.copy()
    child_env.pop(_MANAGED_LOG_STREAM_ENV, None)
    proc = subprocess.Popen(  # noqa: S603 - argv is constructed from trusted CLI state.
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=child_env,
        **_detached_popen_kwargs(),
    )
    deadline = time.time() + _READY_WAIT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _log_tail(log_path)
            return False, (
                f"omni web failed to start (exit {proc.returncode}). "
                f"See {log_path}" + (f": {tail}" if tail else "")
            )
        if port_listening(host, port):
            return True, f"omni web: {ready_url(host, port)}  pid={proc.pid} (background)"
        time.sleep(0.1)
    if pid_alive(int(proc.pid)):
        return True, (
            f"omni web started pid={proc.pid} but {ready_url(host, port)} "
            f"is not listening yet; see {log_path}"
        )
    tail = _log_tail(log_path)
    return False, (
        f"omni web exited before it listened on {ready_url(host, port)}. "
        f"See {log_path}" + (f": {tail}" if tail else "")
    )


def stop_web_process(state: AppState) -> tuple[bool, str]:
    paths = state.settings().paths
    data = read_pidfile(paths)
    if not data:
        return True, "omni web is not running."
    try:
        pid = int(data.get("pid", 0) or 0)
        port = int(data.get("port", 0) or DEFAULT_PORT)
    except (TypeError, ValueError):
        clear_pidfile(paths)
        return True, "Removed an invalid web pidfile."
    host = str(data.get("host") or DEFAULT_HOST)
    url = str(data.get("url") or ready_url(host, port))
    if pid <= 0 or not pid_alive(pid):
        clear_pidfile(paths)
        return True, "omni web is not running; removed the stale pidfile."
    if not terminate_pid(pid):
        return False, f"omni web pid={pid} did not exit ({url})."
    clear_pidfile(paths)
    return True, f"omni web stopped (was pid={pid} {url})."


def restart_web_process(
    state: AppState,
    *,
    host: str | None = None,
    port: int | None = None,
) -> tuple[bool, str]:
    current = web_info(state.settings().paths)
    next_host = host or str((current or {}).get("host") or DEFAULT_HOST)
    next_port = int(port if port is not None else (current or {}).get("port") or DEFAULT_PORT)
    stopped, stop_detail = stop_web_process(state)
    if not stopped:
        return False, stop_detail
    ok, start_detail = start_web_process(state, host=next_host, port=next_port)
    if current:
        return ok, f"{stop_detail} {start_detail}"
    return ok, start_detail


def _report(ok: bool, detail: str) -> None:
    (success if ok else error)(detail)
    if not ok:
        raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def web(
    ctx: typer.Context,
    host: str = typer.Option(DEFAULT_HOST, "--host", help="Bind address (loopback only)."),
    port: int = typer.Option(DEFAULT_PORT, "--port", help="TCP port."),
) -> None:
    """Host the web surface in this terminal. Directory identity is chosen in the UI."""
    if ctx.invoked_subcommand is not None:
        return
    _run_foreground(_state(ctx), host=host, port=port)


@app.command("start")
def start_cmd(
    ctx: typer.Context,
    host: str = typer.Option(DEFAULT_HOST, "--host", help="Bind address (loopback only)."),
    port: int = typer.Option(DEFAULT_PORT, "--port", help="TCP port."),
) -> None:
    """Start the UI in the background and return (what REPL ``/web`` does)."""
    _report(*start_web_process(_state(ctx), host=host, port=port))


@app.command("stop")
def stop_cmd(ctx: typer.Context) -> None:
    """Stop the background (or recorded foreground) UI for this workspace."""
    _report(*stop_web_process(_state(ctx)))


@app.command("status")
def status_cmd(ctx: typer.Context) -> None:
    """Show pid, URL, and UI version when the workspace web process is live."""
    paths = _state(ctx).settings().paths
    current = web_info(paths)
    if current is None:
        info("omni web is not running.")
        return
    ui = spa_version(web_dist_dir()) or "unversioned"
    info(
        f"omni web: {current['url']}  pid={current['pid']}  UI {ui}  "
        f"pidfile {paths.project_dir / 'web.pid'}"
    )


@app.command("restart")
def restart_cmd(
    ctx: typer.Context,
    host: str | None = typer.Option(None, "--host", help="Bind address (loopback only)."),
    port: int | None = typer.Option(None, "--port", help="TCP port."),
) -> None:
    """Stop the current UI if needed, then start it again (optionally on a new port)."""
    _report(*restart_web_process(_state(ctx), host=host, port=port))


@app.command("port")
def port_cmd(
    ctx: typer.Context,
    port: int = typer.Argument(..., min=1, max=65535, help="TCP port."),
    host: str = typer.Option(DEFAULT_HOST, "--host", help="Bind address (loopback only)."),
) -> None:
    """Start on PORT, or restart onto PORT when the UI is already running."""
    state = _state(ctx)
    current = web_info(state.settings().paths)
    if current and int(current.get("port") or 0) == int(port):
        info(f"omni web is already running: pid={current['pid']} {current['url']}")
        return
    if current:
        _report(*restart_web_process(state, host=host, port=int(port)))
        return
    _report(*start_web_process(state, host=host, port=int(port)))


@app.command("help")
def help_cmd() -> None:
    """Show web commands and the REPL vs shell contract."""
    info("In the REPL, `/web` starts the UI in the background. In the shell, `omni web` stays in the foreground.")
    info(f"Available subcommands: {', '.join(_MANAGE)}.")
    data_table(
        "web subcommands",
        ["command", "purpose", "example"],
        [
            ["(none)", "Foreground server in this terminal (shell only)", "omni web"],
            ["start", "Background start; REPL `/web` is rewritten to this", "/web start"],
            ["stop", "Stop the recorded process for this workspace", "/web stop"],
            ["status", "Show pid and URL", "/web status"],
            ["restart", "Stop + start; pass --port to rebind", "/web restart --port 1290"],
            ["port", "Start or move the UI onto a port", "/web port 1290"],
            ["help", "Show this help", "/web help"],
        ],
    )


# Backwards-compatible alias used by older tests and `app.command("web")` callers.
def web_command(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    """Host the web surface. Directory identity is chosen in the UI, not here."""
    _run_foreground(AppState(), host=host, port=port)
