"""`omni serve` — run the always-on daemon (task worker + channels)."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer

from omni import __version__
from omni.cli.render import banner, data_table, info, success, warn
from omni.cli.state import AppState, make_agent
from omni.runtime.daemon import (
    daemon_info,
    daemon_info_from_pidfile,
    list_running_daemons,
    pid_alive,
)

app = typer.Typer(help="Run and manage the always-on home service (channels + schedules).")
logger = logging.getLogger(__name__)
_SERVE_SUBCOMMANDS = (
    "run", "daemon", "poller", "start", "stop", "restart", "status", "doctor", "prune", "help",
)


class DaemonAlreadyRunning(RuntimeError):
    """Raised when this workspace already has a live daemon owner."""


def _is_ghost_record(record: dict) -> bool:
    """True for a daemon keyed off a workspace *inside* ``~/.omni``.

    These appear when ``omni serve`` was launched from within a workspace's data
    dir (``~/.omni/workspaces/<x>``); they re-poll the same IM bots as the real
    project daemon. Named ``--project`` daemons (no ``workspace_root``) are never
    ghosts.
    """
    from omni.config.paths import is_within_home

    ws_root = str(record.get("workspace_root") or "").strip()
    return bool(ws_root) and is_within_home(Path(ws_root))


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


def _serve_child_argv(state: AppState, *, channels: str, workers: int) -> list[str]:
    """Argv for the detached ``omni serve`` child.

    The daemon should mirror deliverables into the directory the operator
    launched it from (where they scan any login QR) — not the git root it is
    later re-homed to via ``cwd=workspace_root``. So pin the absolute launch CWD
    as ``--out`` unless the operator already passed one.
    """
    global_args = _global_args(state)
    if "--out" not in global_args:
        global_args += ["--out", os.getcwd()]
    argv = [
        sys.executable,
        "-m",
        "omni.cli.main",
        *global_args,
        "serve",
        "--workers",
        str(workers),
    ]
    if channels:
        argv += ["--channels", channels]
    return argv


def _adopt_launch_dir_for_serve() -> None:
    """Record the serve launch directory as trusted.

    Starting ``omni serve`` in a directory (and scanning any channel login QR
    there) is an explicit, local act of trust by the operator. Persisting it
    turns on output mirroring so IM/task deliverables land next to where omni
    was started, rather than only in the ``~/.omni`` store.
    """
    from omni.config import trust as trustmod

    trustmod.set_trusted(Path.cwd())


def _pid_alive(pid: int) -> bool:
    return pid_alive(pid)


def _detached_popen_kwargs() -> dict[str, object]:
    """Spawn-detach kwargs so a background ``omni serve`` outlives its launcher.

    POSIX uses a new session (``setsid``). Windows has no ``setsid``; we instead
    detach from the console and start a new process group so the daemon keeps
    running after the launching shell/REPL exits and never steals Ctrl+C.
    """
    if sys.platform == "win32":
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": detached | new_group}
    return {"start_new_session": True}


def _terminate_pid(pid: int) -> None:
    """Best-effort terminate ``pid`` across platforms.

    ``os.kill(pid, SIGTERM)`` works on both POSIX (graceful) and Windows (maps to
    ``TerminateProcess``); on Windows we never send signal 0 because that would
    also terminate the process.
    """
    os.kill(pid, signal.SIGTERM)


# A daemon bound to the Feishu (lark) WebSocket channel needs several seconds to
# unwind its SDK thread + event loop on SIGTERM, so the graceful stop window must
# comfortably exceed that teardown (it returns as soon as the pid is gone).
_STOP_GRACE_SECONDS = 12.0
_STOP_KILL_GRACE_SECONDS = 3.0


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _wait_pid_gone(pid: int, timeout: float) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def _terminate_and_wait(
    pid: int,
    *,
    timeout: float = _STOP_GRACE_SECONDS,
    kill_grace: float = _STOP_KILL_GRACE_SECONDS,
) -> bool:
    """Stop ``pid`` reliably: graceful SIGTERM, then escalate if it lingers.

    Returns ``True`` once the process is gone. If it doesn't exit within
    ``timeout`` we escalate to SIGKILL on POSIX (Windows ``os.kill`` already maps
    to a forceful ``TerminateProcess``). Escalation matters for restart: if stop
    silently timed out, ``restart`` would abort *without* launching the new
    fresh-config daemon, leaving the stale one to keep answering (e.g. with an old
    ``402``). Channel locks and pidfiles are self-healing against a dead pid, so a
    hard kill is safe here.
    """
    try:
        _terminate_pid(pid)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return not _pid_alive(pid)
    if _wait_pid_gone(pid, timeout):
        return True
    if sys.platform != "win32":
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        else:
            if _wait_pid_gone(pid, kill_grace):
                return True
    return not _pid_alive(pid)


def start_daemon_process(state: AppState, *, channels: str = "", workers: int = 1) -> tuple[bool, str]:
    """Start ``omni serve`` in the background for the current workspace."""
    settings = state.settings()
    paths = settings.paths
    existing = daemon_info(paths)
    if existing:
        return True, (
            f"omni serve is already running: pid={existing['pid']}, heartbeat {existing['age']:.0f}s ago; "
            "the service will reconcile channel configuration automatically."
        )

    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = paths.logs_dir / f"serve-{paths.project_name}.log"
    argv = _serve_child_argv(state, channels=channels, workers=workers)
    with log_path.open("ab") as log:
        subprocess.Popen(  # noqa: S603 - argv is constructed from trusted CLI state.
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(paths.workspace_root or os.getcwd()),
            **_detached_popen_kwargs(),
        )

    deadline = time.time() + 10
    while time.time() < deadline:
        info_d = daemon_info(paths)
        if info_d:
            return True, f"omni serve started in the background: pid={info_d['pid']}, log={log_path}"
        time.sleep(0.2)
    return False, f"omni serve started but produced no heartbeat within five seconds; inspect {log_path}"


def _channels_from_info(info_d: dict | None) -> str:
    if not info_d:
        return ""
    if str(info_d.get("channels_mode") or "") == "dynamic":
        return ""
    channels_arg = str(info_d.get("channels_arg") or "").strip()
    if channels_arg:
        return channels_arg
    channels = info_d.get("channels") or []
    if isinstance(channels, list):
        return ",".join(str(c) for c in channels if str(c).strip())
    return str(channels or "").strip()


def restart_daemon_process(
    state: AppState,
    *,
    channels: str = "",
    workers: int | None = None,
) -> tuple[bool, str]:
    """Restart the current workspace daemon, preserving launch options by default."""
    paths = state.settings().paths
    existing = daemon_info(paths)
    next_channels = channels or _channels_from_info(existing)
    next_workers = workers if workers is not None else int((existing or {}).get("workers") or 1)

    if existing:
        stopped, stop_detail = stop_daemon_process(state)
        if not stopped:
            return False, stop_detail
        ok, start_detail = start_daemon_process(state, channels=next_channels, workers=next_workers)
        if ok:
            return True, f"Restarted omni serve. {start_detail}"
        return False, start_detail

    ok, start_detail = start_daemon_process(state, channels=next_channels, workers=next_workers)
    if ok:
        return True, f"omni serve was not running and has now started. {start_detail}"
    return False, start_detail


def stop_daemon_process(state: AppState) -> tuple[bool, str]:
    """Stop the background ``omni serve`` for the current workspace."""
    from omni.runtime.daemon import clear_pidfile, read_pidfile

    paths = state.settings().paths
    data = read_pidfile(paths)
    if not data:
        return True, "The current workspace has no omni serve pidfile."
    try:
        pid = int(data.get("pid", 0) or 0)
    except (TypeError, ValueError):
        clear_pidfile(paths)
        return True, "Removed an invalid pidfile."
    if not _pid_alive(pid):
        clear_pidfile(paths)
        return True, "omni serve is not running; removed the stale pidfile."
    if _terminate_and_wait(pid):
        clear_pidfile(paths)
        return True, f"Stopped omni serve: pid={pid}"
    return False, f"Could not stop omni serve after SIGTERM/SIGKILL: pid={pid}"


def _stop_daemon_record(record: dict, *, timeout: float = _STOP_GRACE_SECONDS) -> tuple[bool, str]:
    pid = int(record.get("pid", 0) or 0)
    pidfile = Path(str(record.get("pidfile") or ""))
    if pid <= 0:
        return True, "pid invalid"
    if not _pid_alive(pid):
        if pidfile.is_file():
            _unlink_quietly(pidfile)
        return True, f"daemon already stopped pid={pid}"
    if _terminate_and_wait(pid, timeout=timeout):
        if pidfile.is_file():
            _unlink_quietly(pidfile)
        return True, f"stopped pid={pid}"
    return False, f"timeout stopping pid={pid}"


def _restart_argv_from_record(record: dict) -> list[str]:
    argv = record.get("argv")
    if isinstance(argv, list) and argv:
        out = [str(part) for part in argv]
        if len(out) >= 3 and out[1:3] == ["-m", "omni.cli.main"]:
            out[0] = sys.executable
        return out

    global_args = record.get("global_args")
    args = [str(part) for part in global_args] if isinstance(global_args, list) else []
    mode = str(record.get("mode") or "daemon")
    command = "poller" if mode == "poller" else "serve"
    workers = int(record.get("workers") or 1)
    out = [sys.executable, "-m", "omni.cli.main", *args, command, "--workers", str(workers)]
    channels = _channels_from_info(record)
    if channels and command == "serve":
        out += ["--channels", channels]
    return out


def _restart_cwd_from_record(record: dict) -> str | None:
    """Working dir for a restarted daemon — never from inside ``~/.omni``.

    Path-keyed workspaces are identified purely by their cwd, so we must restart
    from the real ``workspace_root`` and never from inside the Omni home (that
    would nest a ghost ``<ws>-<hash>-<hash>`` workspace). Named ``--project``
    daemons carry their selector in argv, so any neutral cwd outside the home
    works. Returns ``None`` to signal "refuse to (re)start this record" when the
    only candidates would land inside ``~/.omni`` (i.e. a ghost/moved workspace).
    """
    from omni.config.paths import is_within_home, user_home

    home = user_home()
    ws_root = str(record.get("workspace_root") or "").strip()
    if ws_root:
        root = Path(ws_root)
        if is_within_home(root, home):
            return None  # ghost/data-dir workspace → don't resurrect it
        return str(root) if root.is_dir() else None
    # Named project (no workspace_root): cwd doesn't affect keying.
    cwd = str(record.get("cwd") or "").strip()
    if cwd and not is_within_home(Path(cwd), home) and Path(cwd).is_dir():
        return cwd
    return str(Path.home())


def _start_daemon_record(record: dict) -> tuple[bool, str]:
    argv = _restart_argv_from_record(record)
    cwd = _restart_cwd_from_record(record)
    if cwd is None:
        target = record.get("project_dir") or record.get("project_name") or record.get("pid")
        return False, (
            f"Skipped a ghost or invalid workspace whose root is missing or under the Omni data directory: {target}"
        )
    log_path_raw = record.get("log_path")
    log_path = Path(str(log_path_raw)) if log_path_raw else Path.cwd() / "omni-serve.log"
    pidfile_raw = record.get("pidfile")
    pidfile = Path(str(pidfile_raw)) if pidfile_raw else None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        subprocess.Popen(  # noqa: S603 - argv is the recorded omni launch command.
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            **_detached_popen_kwargs(),
        )
    if pidfile is None:
        return True, f"started {' '.join(argv)}"
    deadline = time.time() + 5
    while time.time() < deadline:
        if daemon_info_from_pidfile(pidfile):
            return True, f"started {' '.join(argv)}"
        time.sleep(0.2)
    return False, f"started process but no heartbeat appeared for {pidfile}"


def restart_daemon_record(record: dict) -> tuple[str, str]:
    """Restart one recorded daemon. Returns ``(status, detail)``.

    ``status`` is ``"ok"`` (restarted), ``"skipped"`` (ghost/invalid workspace —
    stopped but deliberately not resurrected, so ``omni update`` doesn't nest a
    new ghost), or ``"failed"``.
    """
    project = record.get("project_name") or record.get("project_dir") or record.get("pid")
    if _restart_cwd_from_record(record) is None:
        stopped, stop_detail = _stop_daemon_record(record)
        if stopped:
            return "skipped", f"{project}: stopped ghost/invalid workspace without restarting"
        return "skipped", f"{project}: ghost/invalid workspace; stop failed: {stop_detail}"
    stopped, stop_detail = _stop_daemon_record(record)
    if not stopped:
        return "failed", stop_detail
    started, start_detail = _start_daemon_record(record)
    if not started:
        return "failed", start_detail
    return "ok", f"{project}: {start_detail}"


def restart_daemon_records(records: list[dict]) -> tuple[bool, str]:
    buckets: dict[str, list[str]] = {"ok": [], "skipped": [], "failed": []}
    for record in records:
        status, detail = restart_daemon_record(record)
        buckets[status].append(detail)
    summary: list[str] = []
    if buckets["ok"]:
        summary.append(f"restarted {len(buckets['ok'])} omni serve instances")
    if buckets["skipped"]:
        summary.append(f"skipped {len(buckets['skipped'])} ghost/invalid workspaces: " + "; ".join(buckets["skipped"]))
    if buckets["failed"]:
        msg = "Some omni serve restarts failed: " + "; ".join(buckets["failed"])
        if summary:
            msg += "（" + "；".join(summary) + "）"
        return False, msg
    return True, ("; ".join(summary) + ".") if summary else "No omni serve instance needs restarting."


def render_serve_usage_help() -> None:
    """Render serve command details.

    ``omni serve`` is the always-on **home** service: one OS-supervised process
    per ``OMNI_HOME`` that owns messaging channels and dispatches every
    workspace's schedules. ``start``/``stop``/``restart`` manage that persisted,
    supervised service; the foreground ``run``/``daemon``/``poller`` variants run
    it in this terminal (for debugging or hosts without a supervisor).
    """
    info("Use `/serve ...` in the REPL or `omni serve ...` in the shell.")
    info(f"Available subcommands: {', '.join(_SERVE_SUBCOMMANDS)}.")
    data_table(
        "serve subcommands",
        ["command", "purpose", "example"],
        [
            ["start", "Enable + start the always-on home service (survives logout)", "/serve start"],
            ["stop", "Disable + stop the home service (also --all/--ghosts legacy)", "/serve stop"],
            ["restart", "Restart the home service onto current config/code", "/serve restart"],
            ["status", "Desired state, live runtime, anchor, channels", "/serve status"],
            ["doctor", "Diagnose supervisor availability and drift", "/serve doctor"],
            ["run", "Run the home service in the foreground (supervisor entrypoint)", "/serve run"],
            ["daemon", "Foreground home service (alias of a bare `omni serve`)", "/serve daemon"],
            ["poller", "Foreground home service without messaging channels", "/serve poller"],
            ["prune", "Stop and remove ghost legacy daemon workspaces", "/serve prune"],
            ["help", "Show this help", "/serve help"],
        ],
    )
    info("`omni serve start` installs an OS-supervised service; `omni serve status` shows the channel anchor.")


@app.command("help")
def help_cmd() -> None:
    """Show serve commands and examples."""
    render_serve_usage_help()


async def _run_service(state: AppState, *, channels: str, workers: int, task_only: bool) -> None:
    from omni.channels.manager import ChannelManager
    from omni.runtime.daemon import clear_pidfile_if_owner, touch_pidfile, write_pidfile
    from omni.runtime.notifications import CompositeNotifier, InboxNotifier
    from omni.storage.db import code_schema_version, schema_drifted

    # The daemon owns its launch directory: treat it as trusted so IM/task
    # deliverables mirror there (see ``_adopt_launch_dir_for_serve``).
    _adopt_launch_dir_for_serve()
    agent = await make_agent(state)
    hb: asyncio.Task | None = None
    manager_task: asyncio.Task | None = None
    channel_manager: ChannelManager | None = None
    runtime_started = False
    pidfile_written = False
    stop_event = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

        existing = daemon_info(agent.paths)
        if existing:
            warn(f"omni serve is already running for workspace {agent.paths.project_dir}")
            warn(f"Existing daemon/poller: pid={existing['pid']}, heartbeat {existing['age']:.0f}s ago.")
            raise DaemonAlreadyRunning(f"daemon already running for {agent.paths.project_dir}")

        explicit_names = [] if task_only else [c.strip() for c in channels.split(",") if c.strip()]
        if not task_only:
            channel_manager = ChannelManager(
                agent.settings,
                agent,
                explicit_channels=explicit_names or None,
            )

        inbox = InboxNotifier(agent.paths.project_dir / "inbox.jsonl")
        notifiers = [inbox]
        if channel_manager is not None:
            notifiers.append(channel_manager)
        agent.runtime.set_notifier(CompositeNotifier(notifiers))

        await agent.runtime.start(workers=workers)
        runtime_started = True
        log_path = agent.paths.logs_dir / f"serve-{agent.paths.project_name}.log"
        channels_mode = "poller" if task_only else ("static" if explicit_names else "dynamic")
        metadata = {
            "version": __version__,
            "schema_version": code_schema_version(),
            "executable": sys.executable,
            "argv": [sys.executable, *sys.argv],
            "global_args": _global_args(state),
            "cwd": str(agent.paths.workspace_root or os.getcwd()),
            "workspace_root": str(agent.paths.workspace_root) if agent.paths.workspace_root else "",
            "project_name": agent.paths.project_name,
            "project_dir": str(agent.paths.project_dir),
            "log_path": str(log_path),
            "mode": "poller" if task_only else "daemon",
            "channels_mode": channels_mode,
            "channels": [] if channel_manager is None else channel_manager.desired_names(),
            "channels_arg": ",".join(explicit_names),
            "channel_health": {} if channel_manager is None else channel_manager.snapshot(),
            "workers": workers,
            # Non-secret model identity so `serve status` can reveal exactly which
            # provider/base_url the *running* daemon resolved — the quickest way to
            # spot a daemon still on stale config after a config change (never the
            # api_key).
            "model_provider": agent.settings.model.provider,
            "model_name": agent.settings.model.model,
            "model_base_url": agent.settings.model.base_url,
        }
        write_pidfile(agent.paths, metadata=metadata)
        pidfile_written = True
        banner("OmniScientist service started")
        success(f"Background tasks are running with {workers} workers and DB polling; press Ctrl+C to stop.")
        info(f"Workspace: {agent.paths.project_dir}")
        info(f"Task DB: {agent.paths.project_db}")
        info("Mode: poller-only" if task_only else "Mode: daemon with task workers and channels")
        if not task_only:
            if explicit_names:
                info(f"Channels: {', '.join(explicit_names)} (explicit filter)")
            else:
                info("Channels: dynamic configuration reconciliation")

        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(5)
                # Schema-drift guard: if a newer Omni build rebuilt this store
                # (e.g. after `omni update` bumped the schema), this daemon is now
                # running stale code against a schema it can't query. Stop cleanly
                # — releasing channel locks so a fresh daemon can take over —
                # instead of spamming "no such column" on every request.
                if await schema_drifted(agent.db):
                    logger.warning(
                        "The storage schema was upgraded by a newer OmniScientist version. "
                        "This daemon is stopping to release channel locks; update and restart it."
                    )
                    stop_event.set()
                    return
                if channel_manager is not None:
                    metadata["channels"] = channel_manager.desired_names()
                    metadata["channel_health"] = channel_manager.snapshot()
                touch_pidfile(agent.paths, metadata=metadata)

        hb = asyncio.create_task(_heartbeat())
        if channel_manager is not None:
            await channel_manager.reconcile_once()
            metadata["channels"] = channel_manager.desired_names()
            metadata["channel_health"] = channel_manager.snapshot()
            touch_pidfile(agent.paths, metadata=metadata)
            manager_task = asyncio.create_task(channel_manager.start(), name="channel-manager")
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        if hb is not None:
            hb.cancel()
        if manager_task is not None:
            manager_task.cancel()
            await asyncio.gather(manager_task, return_exceptions=True)
        if channel_manager is not None:
            await channel_manager.stop()
        if runtime_started:
            await agent.runtime.stop()
        await agent.aclose()
        if pidfile_written:
            clear_pidfile_if_owner(agent.paths)


def _run_home_foreground(
    state: AppState,
    *,
    channels: str,
    workers: int,
    task_only: bool,
) -> None:
    """Run the home service in the foreground until interrupted.

    This is the converged foreground path for a bare ``omni serve`` and for the
    supervisor's ``omni serve run`` entrypoint: one home service per
    ``OMNI_HOME`` that hosts every workspace's task runtime + schedules and (when
    channels are enabled) owns the messaging channels from the anchor workspace.
    """
    from omni.runtime.home_service import run_home_service
    from omni.runtime.service_state import (
        launcher_service_instance_id,
        service_instance_id,
    )

    settings = state.settings()
    try:
        launcher_id = launcher_service_instance_id()
    except ValueError as exc:
        raise typer.BadParameter(
            str(exc),
            param_hint="service launcher identity",
        ) from exc
    if (
        launcher_id is not None
        and launcher_id != service_instance_id(settings.paths)
    ):
        raise typer.BadParameter(
            "service id does not match the active OMNI_HOME",
            param_hint="service launcher identity",
        )
    logging.basicConfig(level=getattr(logging, settings.observability.log_level, logging.INFO))
    try:
        asyncio.run(
            run_home_service(
                settings,
                workers=workers,
                enable_channels=not task_only,
                channels_filter=channels,
            )
        )
    except KeyboardInterrupt:
        info("Stopped.")


@app.callback(invoke_without_command=True)
def serve(
    ctx: typer.Context,
    channels: str = typer.Option("", help="Optional channel filter; defaults to dynamic configuration."),
    workers: int = typer.Option(1, help="Per-workspace background task concurrency."),
    no_channels: bool = typer.Option(False, "--no-channels", help="Dispatch schedules only; do not own channels."),
) -> None:
    """Run the always-on home service in the foreground (channels + schedules)."""
    if ctx.invoked_subcommand is not None:
        return
    _run_home_foreground(ctx.obj, channels=channels, workers=workers, task_only=no_channels)


@app.command("run")
def run_cmd(
    ctx: typer.Context,
    channels: str = typer.Option("", help="Optional channel filter; defaults to dynamic configuration."),
    workers: int = typer.Option(1, help="Per-workspace background task concurrency."),
    no_channels: bool = typer.Option(False, "--no-channels", help="Dispatch schedules only; do not own channels."),
) -> None:
    """Run the home service in the foreground (the OS supervisor's entrypoint)."""
    _run_home_foreground(ctx.obj, channels=channels, workers=workers, task_only=no_channels)


@app.command("daemon")
def daemon_cmd(
    ctx: typer.Context,
    channels: str = typer.Option("", help="Optional channel filter; defaults to dynamic configuration."),
    workers: int = typer.Option(1, help="Per-workspace background task concurrency."),
) -> None:
    """Run the home service in the foreground, equivalent to a bare `omni serve`."""
    _run_home_foreground(ctx.obj, channels=channels, workers=workers, task_only=False)


@app.command("poller")
def poller_cmd(
    ctx: typer.Context,
    workers: int = typer.Option(1, help="Per-workspace background task concurrency."),
) -> None:
    """Run the home service in the foreground without messaging channels."""
    _run_home_foreground(ctx.obj, channels="", workers=workers, task_only=True)


@app.command("start")
def start_cmd(
    ctx: typer.Context,
    manager: str = typer.Option("auto", help="OS supervisor: auto, launchd, systemd, schtasks, or detached."),
    no_channels: bool = typer.Option(False, "--no-channels", help="Dispatch schedules only; do not own channels."),
) -> None:
    """Enable and start the always-on, OS-supervised home service."""
    from omni.runtime import service_control

    result = service_control.enable(ctx.obj.settings(), manager=manager, channels=not no_channels)
    (success if result.ok else warn)(result.detail)
    if not result.ok:
        raise typer.Exit(1)


@app.command("restart")
def restart_cmd(ctx: typer.Context) -> None:
    """Restart the home service onto the current configuration and code."""
    from omni.runtime import service_control

    result = service_control.restart(ctx.obj.settings(), wait_s=8.0)
    (success if result.ok else warn)(result.detail)
    if not result.ok:
        raise typer.Exit(1)


@app.command("stop")
def stop_cmd(
    ctx: typer.Context,
    all_workspaces: bool = typer.Option(False, "--all", help="Also stop every legacy per-workspace daemon under OMNI_HOME."),
    ghosts: bool = typer.Option(False, "--ghosts", help="Stop only legacy ghost daemons (implies leaving the home service alone)."),
) -> None:
    """Stop the home service now (it restarts on your next ``omni`` launch).

    The home service is always-on: plain ``omni serve stop`` is a *transient*
    pause — it stops the service immediately, but the next time you run ``omni``
    it is brought back automatically (or run ``omni serve start`` now). Use it for
    maintenance or before a manual restart. It also stops any legacy per-workspace
    daemon for this workspace; ``--all``/``--ghosts`` additionally sweep legacy
    daemons across every workspace (migration hygiene). To keep the service off,
    set ``service.ensure_on_launch = false``.
    """
    from omni.runtime import service_control

    state: AppState = ctx.obj

    # --ghosts is a pure legacy-cleanup switch: never touch the home service.
    if ghosts and not all_workspaces:
        _stop_legacy_records(state, ghosts_only=True)
        return

    result = service_control.stop(state.settings())
    (success if result.ok else warn)(result.detail)

    if result.ok:
        info("It will start again next time you run `omni` (or run `omni serve start` now).")

    if all_workspaces:
        _stop_legacy_records(state, ghosts_only=False)
    else:
        # Still stop this workspace's legacy daemon so "stop" means "nothing left
        # running here" for anyone mid-migration off the old per-workspace serve.
        ok, detail = stop_daemon_process(state)
        if "no omni serve pidfile" not in detail:
            info(detail)

def _stop_legacy_records(state: AppState, *, ghosts_only: bool) -> None:
    records = list_running_daemons(state.settings().paths.home)
    if ghosts_only:
        records = [r for r in records if _is_ghost_record(r)]
    if not records:
        info("No legacy per-workspace daemon is running.")
        return
    stopped, failures = 0, []
    for record in records:
        ok, detail = _stop_daemon_record(record)
        if ok:
            stopped += 1
        else:
            failures.append(detail)
    success(f"Stopped {stopped} legacy omni serve daemon(s).")
    if failures:
        warn("Some stops failed: " + "; ".join(failures))
        raise typer.Exit(1)


@app.command("prune")
def prune_cmd(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Prune without confirmation."),
    keep_data: bool = typer.Option(False, "--keep-data", help="Stop ghosts without deleting data directories."),
) -> None:
    """Stop ghost daemons and optionally remove their data directories."""
    state: AppState = ctx.obj
    ghosts = [r for r in list_running_daemons(state.settings().paths.home) if _is_ghost_record(r)]
    if not ghosts:
        success("No ghost daemons were found.")
        return

    rows = [
        [str(g.get("pid")), f"{float(g.get('age') or 0):.0f}s", str(g.get("project_dir") or "")]
        for g in ghosts
    ]
    data_table("Ghost daemons to prune", ["pid", "heartbeat", "dir"], rows)
    action = "stop and delete data directories" if not keep_data else "stop and keep data directories"
    if not yes and not typer.confirm(f"Apply '{action}' to these {len(ghosts)} ghosts?"):
        warn("Cancelled.")
        raise typer.Exit(1)

    from omni.config.paths import is_within_home

    stopped, removed, failures = 0, 0, []
    for record in ghosts:
        ok, detail = _stop_daemon_record(record)
        if ok:
            stopped += 1
        else:
            failures.append(detail)
            continue
        if keep_data:
            continue
        project_dir = Path(str(record.get("project_dir") or ""))
        # Defensive: only ever delete a path-keyed dir inside ~/.omni/workspaces.
        if "workspaces" in project_dir.parts and is_within_home(project_dir):
            shutil.rmtree(project_dir, ignore_errors=True)
            removed += 1

    success(f"Stopped {stopped} ghosts and removed {removed} data directories.")
    if failures:
        warn("Some stops failed: " + "; ".join(failures))
        raise typer.Exit(1)


@app.command("status")
def status_cmd(
    ctx: typer.Context,
    all_workspaces: bool = typer.Option(False, "--all", help="List all legacy per-workspace daemons under OMNI_HOME."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show the per-workspace schedule-dispatch breakdown."),
) -> None:
    """Show the home service: desired state, live runtime, anchor, and channels."""
    from omni.runtime import service_control

    state: AppState = ctx.obj

    if all_workspaces:
        _render_legacy_daemons(state)
        return

    snap = service_control.status(state.settings())
    rt = snap.get("runtime") or {}
    rows = [
        ["Enabled", str(snap["enabled"])],
        ["Configured", str(snap["configured"])],
        ["Supervisor", f"{snap['manager']} ({snap['supervisor_status']})"],
        ["Active", str(snap["active"])],
        ["Ready", str(snap["running"])],
        ["Phase", str(snap["phase"])],
        ["Version", str(rt.get("version") or "-")],
        ["Anchor", str(rt.get("anchor") or snap.get("channel_anchor") or "-")],
        ["Workspaces", str(len(rt.get("workspaces") or []))],
        ["Channels", ", ".join(rt.get("channels") or []) or "-"],
    ]
    if snap.get("last_error"):
        rows.append(["Last error", str(snap["last_error"])])
    data_table("omni serve (home service)", ["field", "value"], rows)

    # There is exactly one home service. Its schedules happen to be stored per
    # research workspace (each keeps its own working dir + notebook), but that is
    # an implementation detail — by default present the service as a single unit
    # and only expand the per-workspace breakdown on request.
    workspaces = rt.get("workspaces") or []
    if workspaces and verbose:
        data_table(
            "Schedule dispatch (one home service, per-workspace working dirs)",
            ["workspace", "role", "dir"],
            [
                [
                    w.get("name", ""),
                    "channels+schedules" if w.get("anchor") else "schedules",
                    w.get("dir", ""),
                ]
                for w in workspaces
            ],
        )
    elif workspaces:
        names = ", ".join(w.get("name", "") for w in workspaces if w.get("name"))
        info(
            f"Dispatching schedules for {len(workspaces)} workspace(s): {names}. "
            "Run `omni serve status --verbose` for the per-workspace breakdown."
        )

    # The runtime pidfile is last-writer-wins, so a single row there can hide
    # duplicate services. Cross-check the process table and warn loudly.
    serve_pids = snap.get("serve_pids") or []
    if len(serve_pids) > 1:
        warn(
            f"{len(serve_pids)} `omni serve run` processes are live (expected exactly 1): "
            f"pids={serve_pids}. Run `omni serve stop --all` then `omni serve start` to converge."
        )

    legacy = list_running_daemons(state.settings().paths.home)
    if legacy:
        warn(
            f"{len(legacy)} legacy per-workspace daemon(s) are still running. "
            "See `omni serve status --all`; the home service retires them on start."
        )


def _render_legacy_daemons(state: AppState) -> None:
    rows = []
    ghost_count = 0
    for d in list_running_daemons(state.settings().paths.home):
        ghost = _is_ghost_record(d)
        ghost_count += int(ghost)
        rows.append([
            str(d.get("project_name") or Path(str(d.get("project_dir"))).name),
            str(d.get("pid")),
            f"{float(d.get('age') or 0):.0f}s",
            _format_channels(d),
            str(d.get("version") or "unknown"),
            "ghost (~/.omni)" if ghost else "",
            str(d.get("project_dir") or ""),
        ])
    if not rows:
        info("No legacy per-workspace daemon is running.")
        return
    data_table(
        "Legacy per-workspace daemons",
        ["workspace", "pid", "heartbeat", "channels", "version", "note", "dir"],
        rows,
    )
    if ghost_count:
        warn(f"Found {ghost_count} ghost daemons. Run `omni serve prune` to clean them up.")


@app.command("doctor")
def doctor_cmd(ctx: typer.Context) -> None:
    """Diagnose supervisor availability, drift, and lingering legacy daemons."""
    from omni.runtime import service_control

    snap = service_control.doctor(ctx.obj.settings())
    plat = snap.get("platform") or {}
    data_table(
        "Supervisor availability",
        ["field", "value"],
        [
            ["Platform", str(plat.get("platform"))],
            ["launchd", str(plat.get("launchd"))],
            ["systemd (user)", str(plat.get("systemd"))],
            ["schtasks", str(plat.get("schtasks"))],
            ["auto selects", str(plat.get("auto"))],
            ["Enabled", str(snap["enabled"])],
            ["Active", str(snap["active"])],
            ["Ready", str(snap["running"])],
            ["Phase", str(snap["phase"])],
        ],
    )
    for finding in snap.get("findings") or []:
        warn(finding)
    if not snap.get("findings"):
        success("Home service configuration looks healthy.")


def _format_channels(record: dict | None) -> str:
    if not record:
        return ""
    channels = record.get("channels") or []
    if isinstance(channels, list) and channels:
        base = ",".join(str(c) for c in channels if str(c).strip())
    else:
        base = str(record.get("channels_arg") or "")
    mode = str(record.get("channels_mode") or "")
    if mode == "dynamic":
        return f"dynamic:{base or 'configured'}"
    if mode == "static":
        return f"static:{base}"
    return base
