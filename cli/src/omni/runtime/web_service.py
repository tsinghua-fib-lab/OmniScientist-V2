"""Per-workspace ``omni web`` pidfile.

Foreground ``omni web`` and the detached child from ``omni web start`` both
write ``web.pid``. Stop/status trust a live pid only — unlike ``serve.pid``,
there is no heartbeat window, so a healthy UI older than 30s is still live.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omni.config.paths import OmniPaths
from omni.runtime.daemon import pid_alive
from omni.web.bind import DEFAULT_HOST, DEFAULT_PORT, ready_url

WEB_PIDFILE_NAME = "web.pid"
STOP_GRACE_S = 3.0
STOP_KILL_S = 2.0


def pidfile_path(paths: OmniPaths) -> Path:
    return paths.project_dir / WEB_PIDFILE_NAME


def write_pidfile(
    paths: OmniPaths,
    *,
    pid: int | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    path = pidfile_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": int(pid if pid is not None else os.getpid()),
        "host": host,
        "port": int(port),
        "url": ready_url(host, int(port)),
        "ts": time.time(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def read_pidfile(paths: OmniPaths) -> dict[str, Any] | None:
    path = pidfile_path(paths)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_pidfile(paths: OmniPaths) -> None:
    try:
        pidfile_path(paths).unlink()
    except OSError:
        pass


def clear_pidfile_if_owner(paths: OmniPaths) -> bool:
    data = read_pidfile(paths)
    if not data:
        return False
    try:
        if int(data.get("pid", 0) or 0) != os.getpid():
            return False
    except (TypeError, ValueError):
        return False
    clear_pidfile(paths)
    return True


def web_info(paths: OmniPaths) -> dict[str, Any] | None:
    """Return live web metadata, clearing a stale pidfile when the pid is dead."""
    data = read_pidfile(paths)
    if not data:
        return None
    try:
        pid = int(data.get("pid", 0) or 0)
        port = int(data.get("port", 0) or 0)
    except (TypeError, ValueError):
        clear_pidfile(paths)
        return None
    if pid <= 0 or not pid_alive(pid):
        clear_pidfile(paths)
        return None
    host = str(data.get("host") or DEFAULT_HOST)
    url = str(data.get("url") or "") or ready_url(host, port or DEFAULT_PORT)
    info = dict(data)
    info.update({"pid": pid, "host": host, "port": port or DEFAULT_PORT, "url": url})
    return info


def port_listening(host: str, port: int, *, timeout: float = 0.2) -> bool:
    """True when something accepts TCP on ``host:port``."""
    raw = (host or DEFAULT_HOST).strip()
    if raw in {"localhost", "127.0.0.1"}:
        targets = ("127.0.0.1",)
    elif raw in {"::1", "[::1]"}:
        targets = ("::1",)
    else:
        targets = (raw.strip("[]"),)
    for target in targets:
        family = socket.AF_INET6 if ":" in target else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect((target, int(port)))
            return True
        except OSError:
            continue
    return False


def terminate_pid(pid: int) -> bool:
    """SIGTERM, then SIGKILL. True once the process is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return not pid_alive(pid)
    if _wait_pid_gone(pid, STOP_GRACE_S):
        return True
    hard_kill = getattr(signal, "SIGKILL", None)
    if hard_kill is not None:
        try:
            os.kill(pid, hard_kill)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        else:
            if _wait_pid_gone(pid, STOP_KILL_S):
                return True
    return not pid_alive(pid)


def _wait_pid_gone(pid: int, timeout: float) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)
