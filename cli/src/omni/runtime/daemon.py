"""`omni serve` liveness — a per-workspace pidfile + heartbeat.

Other processes (a REPL window, ``omni status``) use this to tell whether a
daemon currently *owns* this workspace's background tasks. When a daemon is
live the REPL stops draining tasks inline and lets the daemon run them; when no
daemon is live the REPL falls back to inline execution so single-window users
keep working.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from omni.config.paths import OmniPaths

# A daemon touches its heartbeat every few seconds; treat anything older than
# this (with a live pid) as a stale/hung daemon rather than an owner.
HEARTBEAT_STALE_SECONDS = 30.0


def pidfile_path(paths: OmniPaths) -> Path:
    return paths.project_dir / "serve.pid"


def _write_pidfile_path(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _started_at() -> str:
    return datetime.now(UTC).isoformat()


def write_pidfile(paths: OmniPaths, *, metadata: dict | None = None) -> None:
    """Write this process's serve pidfile.

    ``metadata`` captures the launch contract (version, argv, channels, workers)
    so a later ``omni update`` can restart the same service with the new code.
    """
    payload = dict(metadata or {})
    payload["pid"] = os.getpid()
    payload["ts"] = time.time()
    payload.setdefault("started_at", _started_at())
    _write_pidfile_path(pidfile_path(paths), payload)


def touch_pidfile(paths: OmniPaths, *, metadata: dict | None = None) -> None:
    """Refresh heartbeat while preserving launch metadata."""
    payload = read_pidfile(paths) or {}
    if metadata:
        payload.update(metadata)
    payload["pid"] = os.getpid()
    payload["ts"] = time.time()
    payload.setdefault("started_at", _started_at())
    _write_pidfile_path(pidfile_path(paths), payload)


def clear_pidfile(paths: OmniPaths) -> None:
    try:
        pidfile_path(paths).unlink()
    except OSError:
        pass


def read_pidfile(paths: OmniPaths) -> dict | None:
    return read_pidfile_path(pidfile_path(paths))


def read_pidfile_path(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pidfile_owned_by_current_process(paths: OmniPaths) -> bool:
    """Return true when this process wrote the current daemon pidfile."""
    data = read_pidfile(paths)
    if not data:
        return False
    try:
        return int(data.get("pid", 0) or 0) == os.getpid()
    except (TypeError, ValueError):
        return False


def clear_pidfile_if_owner(paths: OmniPaths) -> bool:
    """Clear the pidfile only if it still belongs to this process."""
    if not pidfile_owned_by_current_process(paths):
        return False
    try:
        pidfile_path(paths).unlink()
    except OSError:
        return False
    return True


def pid_alive(pid: int) -> bool:
    """Cross-platform check for whether ``pid`` is a live process.

    On POSIX this probes with ``os.kill(pid, 0)``. On Windows ``os.kill`` maps
    *every* signal — including ``0`` — to ``TerminateProcess``, which would kill
    the very daemon we only want to inspect, so we query the process handle
    instead. A genuinely dead pid that happens to report exit code 259
    (``STILL_ACTIVE``) is still caught by the heartbeat-staleness check.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5

        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Access denied means the pid exists but is owned by someone else.
            return ctypes.get_last_error() == error_access_denied
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - never let a probe crash status output.
        return False


def daemon_info(paths: OmniPaths) -> dict | None:
    """Return ``{pid, ts, age}`` for a *live* daemon, else ``None``."""
    return daemon_info_from_pidfile(pidfile_path(paths))


def daemon_info_from_pidfile(path: Path) -> dict | None:
    """Return pidfile metadata for a live daemon, else ``None``."""
    data = read_pidfile_path(path)
    if not data:
        return None
    try:
        pid = int(data.get("pid", 0) or 0)
        ts = float(data.get("ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if not pid_alive(pid):
        return None
    age = time.time() - ts
    if age > HEARTBEAT_STALE_SECONDS:
        return None
    info = dict(data)
    info.update({"pid": pid, "ts": ts, "age": age, "pidfile": str(path), "project_dir": str(path.parent)})
    return info


def is_daemon_running(paths: OmniPaths) -> bool:
    return daemon_info(paths) is not None


def list_running_daemons(home: Path) -> list[dict]:
    """List live daemon pidfiles under an Omni home."""
    pidfiles: list[Path] = []
    for root_name in ("projects", "workspaces"):
        root = home / root_name
        if root.is_dir():
            pidfiles.extend(sorted(root.glob("*/serve.pid")))
    direct = home / "serve.pid"
    if direct.is_file():
        pidfiles.append(direct)
    out: list[dict] = []
    for path in pidfiles:
        info = daemon_info_from_pidfile(path)
        if info is not None:
            out.append(info)
    return out


_SERVE_CMD_RE = re.compile(r"\bserve\b")
_HOME_SERVICE_CMD_RE = re.compile(r"\bserve\s+run\b")


def scan_running_serve_pids(*, service_id: str | None = None) -> list[int]:
    """Best-effort PIDs of ``omni … serve`` processes, via ``ps`` (POSIX only).

    Complements :func:`list_running_daemons`: a daemon whose pidfile was lost or
    which was started by an older build won't appear there, yet it may still be
    running stale code. When ``service_id`` is supplied, only supervised home
    services explicitly tagged with that id are returned; this prevents one
    ``OMNI_HOME`` from warning about or terminating another home's service.
    Advisory only — returns ``[]`` on Windows or when ``ps`` is unavailable,
    and never raises.
    """
    if sys.platform == "win32":
        return []
    try:
        out = subprocess.run(
            ["ps", "-ww", "-eo", "pid=,args="],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:  # noqa: BLE001 - advisory scan; never fatal.
        return []
    if out.returncode != 0:
        return []
    me = os.getpid()
    pids: list[int] = []
    service_marker = (
        re.compile(
            rf"(?:^|\s)-x(?:\s+)?omni_service_id="
            rf"{re.escape(service_id.lower())}(?:\s|$)"
        )
        if service_id is not None
        else None
    )
    for line in out.stdout.splitlines():
        head, _, args = line.strip().partition(" ")
        if not head.isdigit():
            continue
        pid = int(head)
        low = args.lower()
        looks_like_omni = "omni.cli.main" in low or "main.py" in low or "/omni" in low or "bin/omni" in low
        if service_marker is not None and service_marker.search(low) is None:
            continue
        command_matches = (
            _HOME_SERVICE_CMD_RE.search(low)
            if service_id is not None
            else _SERVE_CMD_RE.search(low)
        )
        if pid != me and looks_like_omni and command_matches:
            pids.append(pid)
    return sorted(set(pids))


def untracked_serve_pids(
    tracked: list[dict],
    *,
    extra_pids: Iterable[int] = (),
    service_id: str | None = None,
) -> list[int]:
    """Running ``omni serve`` PIDs not accounted for by a known record.

    ``tracked`` are legacy per-workspace pidfile records; ``extra_pids`` lets the
    caller add pids tracked elsewhere — most importantly the **home service's own
    runtime pid** (recorded under ``<home>/service/service.pid``, not a
    ``serve.pid`` file). Without it a healthy home service would be misreported as
    an "untracked" orphan after every update. What remains is a genuine
    stray/duplicate the caller can reap or warn about.
    """
    known = {int(r.get("pid", 0) or 0) for r in tracked}
    known.update(int(p) for p in extra_pids if p)
    known.discard(0)
    running = (
        scan_running_serve_pids(service_id=service_id)
        if service_id is not None
        else scan_running_serve_pids()
    )
    return [pid for pid in running if pid not in known]


def _terminate(pid: int) -> bool:
    """Best-effort SIGTERM ``pid`` (Windows ``os.kill`` maps to TerminateProcess)."""
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def stop_legacy_daemons(home: Path) -> list[int]:
    """SIGTERM every *legacy per-workspace* ``omni serve`` daemon under ``home``.

    These are the pre-home-service daemons (identified by their ``serve.pid``
    files); the single home service supersedes them, so bringing the home service
    up should retire them to stop double-driving schedules and fighting over the
    channel lock. Best-effort: returns the pids we signalled and clears their
    pidfiles so a dead record does not linger.
    """
    reaped: list[int] = []
    for record in list_running_daemons(home):
        try:
            pid = int(record.get("pid", 0) or 0)
        except (TypeError, ValueError):
            continue
        if _terminate(pid):
            reaped.append(pid)
            pidfile = record.get("pidfile")
            if pidfile:
                try:
                    Path(str(pidfile)).unlink()
                except OSError:
                    pass
    return reaped


def _sigkill(pid: int) -> None:
    """Force-kill ``pid``; on Windows ``SIGTERM`` already maps to TerminateProcess."""
    sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def reap_serve_processes(
    pids: Iterable[int], *, grace_s: float = 8.0, kill_grace_s: float = 3.0
) -> list[int]:
    """SIGTERM every serve ``pid``, wait for exit, then SIGKILL any that linger.

    The single home service must be the only ``omni … serve`` process per
    ``OMNI_HOME``. A supervised ``stop`` only boots out the process under its
    launchd/systemd label, so a duplicate started *detached* (fallback) or under a
    stale label/argv survives it. This reaps them regardless of how they were
    started, escalating to a hard kill so a restart never spawns a second process
    alongside a stubborn old one. Returns the pids confirmed gone. Best-effort:
    never raises.
    """
    targets = [p for p in dict.fromkeys(int(x) for x in pids) if p > 0 and p != os.getpid()]
    if not targets:
        return []
    for pid in targets:
        _terminate(pid)
    reaped: set[int] = set()
    remaining = set(targets)
    deadline = time.time() + max(0.0, grace_s)
    while remaining and time.time() < deadline:
        for pid in list(remaining):
            if not pid_alive(pid):
                remaining.discard(pid)
                reaped.add(pid)
        if remaining:
            time.sleep(0.2)
    for pid in remaining:
        _sigkill(pid)
    kill_deadline = time.time() + max(0.0, kill_grace_s)
    while remaining and time.time() < kill_deadline:
        for pid in list(remaining):
            if not pid_alive(pid):
                remaining.discard(pid)
                reaped.add(pid)
        if remaining:
            time.sleep(0.1)
    return sorted(reaped)
