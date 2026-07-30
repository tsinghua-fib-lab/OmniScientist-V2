"""Home-level background service state (desired vs. observed).

OmniScientist's recurring schedules and messaging channels need a continuously
available runtime. The legacy model was one detached ``omni serve`` per
*workspace*, which cannot dispatch another workspace's schedules and makes
several daemons race for the single home-level channel lock. The home service
replaces that with ONE supervised control service per ``OMNI_HOME``.

This module owns its persisted state, split deliberately into two files under
``<OMNI_HOME>/service/``:

* ``settings.json`` — the *desired* state: ``enabled`` / ``disabled`` (an
  explicit user preference), the chosen OS supervisor, the channel-anchor
  workspace, and the launcher argv. It is authoritative for "should the service
  be up" and is never inferred from process liveness, so a stale config or a
  transient crash can never silently reverse an explicit ``disable``.
* ``service.pid`` — the *observed* runtime state: pid, version, heartbeat,
  readiness and the active workspace set. It is liveness information only.

Mutating lifecycle operations take a home-level :func:`lifecycle_lock` so two
``enable``/``start``/``update`` paths cannot race.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from omni.config.paths import OmniPaths
from omni.runtime.daemon import pid_alive

# A running home service refreshes its heartbeat every few seconds; treat a
# heartbeat older than this (even with a live pid) as unhealthy, matching the
# per-workspace daemon staleness window.
HEARTBEAT_STALE_SECONDS = 30.0

_SETTINGS_NAME = "settings.json"
_RUNTIME_NAME = "service.pid"
_LOCK_NAME = "lifecycle.lock"
_SINGLETON_NAME = "service.lock"
_START_REQUEST_NAME = "start.requested"
_SERVICE_ID_XOPTION = "omni_service_id"


def service_dir(paths: OmniPaths) -> Path:
    return paths.service_dir


def desired_path(paths: OmniPaths) -> Path:
    return paths.service_dir / _SETTINGS_NAME


def runtime_path(paths: OmniPaths) -> Path:
    return paths.service_dir / _RUNTIME_NAME


def start_request_path(paths: OmniPaths) -> Path:
    return paths.service_dir / _START_REQUEST_NAME


@dataclass
class ServiceDesiredState:
    """Persisted user intent for the home background service.

    ``enabled`` is the load-bearing field: ``True`` means "keep this service
    supervised and running"; ``False`` (the default) means "explicitly off".
    ``configured`` records whether the user has made an explicit choice yet, so
    onboarding can distinguish "never asked" from "declined".
    """

    enabled: bool = False
    configured: bool = False
    manager: str = "auto"  # auto | launchd | systemd | schtasks | detached
    channel_anchor: str = ""  # project_dir of the anchor workspace (inbound + locks)
    launcher: list[str] = field(default_factory=list)  # argv prefix to run the service
    version: str = ""  # omni version recorded at last enable/repair
    updated_at: float = 0.0
    last_error: str = ""


@dataclass(frozen=True)
class ServiceObservation:
    """One coherent view of the home-service process lifecycle."""

    phase: str  # down | starting | ready | stopping | unhealthy | stale
    pid: int | None
    runtime: dict | None

    @property
    def active(self) -> bool:
        return self.phase in {"starting", "ready", "stopping", "unhealthy"}

    @property
    def ready(self) -> bool:
        return self.phase == "ready"


class LifecycleLockTimeout(RuntimeError):
    """Another process owns the lifecycle transaction past the caller's budget."""


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def read_desired(paths: OmniPaths) -> ServiceDesiredState:
    """Load desired state, returning defaults (disabled/unconfigured) when absent."""
    path = desired_path(paths)
    if not path.exists():
        return ServiceDesiredState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ServiceDesiredState()
    if not isinstance(data, dict):
        return ServiceDesiredState()
    known = {f for f in ServiceDesiredState().__dict__}
    return ServiceDesiredState(**{k: v for k, v in data.items() if k in known})


def write_desired(paths: OmniPaths, state: ServiceDesiredState) -> None:
    state.updated_at = time.time()
    _atomic_write_json(desired_path(paths), asdict(state))


def service_instance_id(paths: OmniPaths) -> str:
    """Non-secret stable identity used to scope process discovery to one home."""
    canonical = str(paths.home.expanduser().resolve())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def launcher_service_instance_id() -> str | None:
    """Return the supervising Python process's home identity, if present.

    The launcher uses a Python ``-X`` option instead of a ``serve run`` option.
    Python consumes that marker before importing Omni, so a newly written
    supervisor definition remains executable by an older Omni CLI during a
    rolling self-update. A present but valueless marker is rejected instead of
    silently weakening scoped process ownership.
    """
    options = getattr(sys, "_xoptions", {})
    if _SERVICE_ID_XOPTION not in options:
        return None
    raw = options[_SERVICE_ID_XOPTION]
    if not isinstance(raw, str) or not raw:
        raise ValueError("service launcher identity must be a non-empty value")
    return raw


def request_start(paths: OmniPaths) -> None:
    """Persist a bare-launch start intent before its async worker can race update."""
    _atomic_write_json(
        start_request_path(paths),
        {"pid": os.getpid(), "requested_at": time.time()},
    )


def start_requested(paths: OmniPaths) -> bool:
    return start_request_path(paths).is_file()


def clear_start_request(paths: OmniPaths) -> bool:
    try:
        start_request_path(paths).unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def default_launcher(paths: OmniPaths) -> list[str]:
    """Argv prefix that runs the home service in the foreground.

    Bound to the current interpreter so a supervised unit re-execs the exact
    Omni install that enabled it (mirrors ``omni update``'s ``sys.executable``
    discipline). The non-secret service id makes process-table reconciliation
    safe when several ``OMNI_HOME`` roots exist on one host. It is carried as a
    Python ``-X`` option so older Omni CLI parsers never receive an unknown
    application option during a rolling self-update.
    """
    return [
        sys.executable,
        "-X",
        f"{_SERVICE_ID_XOPTION}={service_instance_id(paths)}",
        "-m",
        "omni.cli.main",
        "serve",
        "run",
    ]


# ── observed runtime state ──────────────────────────────────────────────────


def read_runtime(paths: OmniPaths) -> dict | None:
    path = runtime_path(paths)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_runtime(paths: OmniPaths, payload: dict) -> None:
    body = dict(payload)
    previous = read_runtime(paths) or {}
    body["pid"] = os.getpid()
    body["heartbeat"] = time.time()
    try:
        previous_pid = int(previous.get("pid", 0) or 0)
    except (TypeError, ValueError):
        previous_pid = 0
    if previous_pid == os.getpid():
        body.setdefault("started_at", previous.get("started_at") or time.time())
    else:
        body.setdefault("started_at", time.time())
    ready = bool(body.get("ready", False))
    body.setdefault("phase", "ready" if ready else "starting")
    _atomic_write_json(runtime_path(paths), body)


def touch_runtime(paths: OmniPaths, *, metadata: dict | None = None) -> None:
    payload = read_runtime(paths) or {}
    if metadata:
        payload.update(metadata)
    write_runtime(paths, payload)


def clear_runtime_if_owner(paths: OmniPaths) -> bool:
    data = read_runtime(paths)
    if not data:
        return False
    try:
        owner = int(data.get("pid", 0) or 0)
    except (TypeError, ValueError):
        owner = 0
    if owner != os.getpid():
        return False
    try:
        runtime_path(paths).unlink()
    except OSError:
        return False
    return True


def clear_runtime(paths: OmniPaths) -> bool:
    """Remove observed state after a lifecycle controller confirmed shutdown."""
    try:
        runtime_path(paths).unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def service_runtime_info(paths: OmniPaths) -> dict | None:
    """Return runtime metadata for a *live and fresh* home service, else ``None``."""
    data = read_runtime(paths)
    if not data:
        return None
    try:
        pid = int(data.get("pid", 0) or 0)
        hb = float(data.get("heartbeat", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if not pid_alive(pid):
        return None
    age = time.time() - hb
    if age > HEARTBEAT_STALE_SECONDS:
        return None
    info = dict(data)
    info.update({"pid": pid, "heartbeat": hb, "age": age})
    return info


def observe_service(paths: OmniPaths) -> ServiceObservation:
    """Distinguish a live-but-starting owner from a ready runtime.

    The singleton owner is published before heavy initialization. Control-plane
    READY follows once agents and task runtimes can accept work; IM channels
    connect afterwards and are tracked in ``channel_health``. Treating STARTING
    as DOWN is the update/orphan race this state model prevents.
    """
    raw_runtime = read_runtime(paths)
    runtime = service_runtime_info(paths)
    holder_info = singleton_holder_info(paths)
    holder = (
        int(holder_info["pid"])
        if holder_info is not None and holder_info.get("role") != "update"
        else None
    )
    runtime_pid = int((runtime or {}).get("pid", 0) or 0) or None
    if holder is not None:
        if runtime is not None and runtime_pid == holder:
            phase = str(runtime.get("phase") or "")
            if bool(runtime.get("ready")) and phase not in {"stopping", "unhealthy"}:
                return ServiceObservation("ready", holder, runtime)
            if phase in {"stopping", "unhealthy"}:
                return ServiceObservation(phase, holder, runtime)
        if raw_runtime is not None:
            try:
                raw_pid = int(raw_runtime.get("pid", 0) or 0)
                heartbeat = float(raw_runtime.get("heartbeat", 0.0) or 0.0)
            except (TypeError, ValueError):
                raw_pid, heartbeat = 0, 0.0
            if raw_pid == holder and time.time() - heartbeat > HEARTBEAT_STALE_SECONDS:
                return ServiceObservation("unhealthy", holder, raw_runtime)
        return ServiceObservation("starting", holder, runtime)

    # Compatibility for a fresh runtime published by an older service build
    # that did not yet hold the singleton lock.
    if runtime is not None and runtime_pid is not None:
        phase = str(runtime.get("phase") or "")
        if bool(runtime.get("ready")) and phase not in {"stopping", "unhealthy"}:
            return ServiceObservation("ready", runtime_pid, runtime)
        if phase in {"stopping", "unhealthy"}:
            return ServiceObservation(phase, runtime_pid, runtime)
        return ServiceObservation("starting", runtime_pid, runtime)
    if raw_runtime is not None:
        return ServiceObservation("stale", None, raw_runtime)
    return ServiceObservation("down", None, None)


def service_is_active(paths: OmniPaths) -> bool:
    return observe_service(paths).active


def service_is_ready(paths: OmniPaths) -> bool:
    return observe_service(paths).ready


def service_is_running(paths: OmniPaths) -> bool:
    """Backward-compatible spelling for READY, not merely process existence."""
    return service_is_ready(paths)


# ── home-level lifecycle lock (serialize enable/start/stop/update) ───────────


class LifecycleLock:
    """An exclusive lock guarding service lifecycle mutations.

    The lock is kernel-backed and held on one stable inode. A pid-only
    ``O_EXCL`` lockfile is vulnerable to an unlink/recreate race where two
    contenders end up owning different inodes. Kernel release on process exit
    also means a crashed updater can never wedge future lifecycle operations.
    Contention fails closed: proceeding without ownership would let
    update/install/start interleave and load a mixture of code versions.
    """

    def __init__(self, paths: OmniPaths, *, timeout_s: float = 10.0) -> None:
        self._path = paths.service_dir / _LOCK_NAME
        self._timeout = timeout_s
        self._fd: int | None = None
        self._acquired = False

    def __enter__(self) -> LifecycleLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise LifecycleLockTimeout(
                f"Could not open home-service lifecycle lock: {self._path}"
            ) from exc
        if sys.platform == "win32":
            # ``msvcrt.locking`` locks a byte range; ensure byte zero exists.
            with contextlib.suppress(OSError):
                os.lseek(self._fd, 0, os.SEEK_END)
                if os.lseek(self._fd, 0, os.SEEK_CUR) == 0:
                    os.write(self._fd, b"\0")
        deadline = time.time() + max(0.0, self._timeout)
        while True:
            if _flock_exclusive_nonblocking(self._fd):
                self._acquired = True
                with contextlib.suppress(OSError):
                    os.ftruncate(self._fd, 0)
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    os.write(
                        self._fd,
                        json.dumps({"pid": os.getpid(), "ts": time.time()}).encode(),
                    )
                return self
            if time.time() >= deadline:
                with contextlib.suppress(OSError):
                    os.close(self._fd)
                self._fd = None
                raise LifecycleLockTimeout(
                    f"Timed out waiting for home-service lifecycle lock: {self._path}"
                )
            time.sleep(0.1)

    def __exit__(self, *exc: object) -> None:
        if not self._acquired or self._fd is None:
            return
        _flock_release(self._fd)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None
        self._acquired = False


def lifecycle_lock(paths: OmniPaths, *, timeout_s: float = 10.0) -> LifecycleLock:
    return LifecycleLock(paths, timeout_s=timeout_s)


# ── home-service singleton lock (exactly one live service per OMNI_HOME) ──────
#
# Unlike ``lifecycle_lock`` (a short, self-healing guard around *CLI* lifecycle
# mutations), this is a true OS advisory lock held for the *entire lifetime* of
# the home-service process. It is the authoritative guarantee that only one
# ``omni serve run`` is active per ``OMNI_HOME``: a redundant spawn (from a
# repeated ``enable``/``ensure``, a detached fallback, or a duplicate OS unit)
# fails to acquire it and exits, instead of coexisting and fighting over the
# runtime pidfile / channel locks. The kernel releases it automatically when the
# holder dies, so a crashed service never wedges the next start.


def singleton_path(paths: OmniPaths) -> Path:
    return paths.service_dir / _SINGLETON_NAME


def _ensure_windows_lock_byte(fd: int) -> bool:
    """Ensure byte zero exists before ``msvcrt.locking`` locks its range."""
    if sys.platform != "win32":
        return True
    try:
        os.lseek(fd, 0, os.SEEK_END)
        if os.lseek(fd, 0, os.SEEK_CUR) == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        return False
    return True


def _flock_exclusive_nonblocking(fd: int) -> bool:
    """Take an exclusive, non-blocking lock on ``fd``; ``False`` if held elsewhere."""
    if sys.platform == "win32":
        import msvcrt

        if not _ensure_windows_lock_byte(fd):
            return False
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _flock_release(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)


def acquire_singleton(paths: OmniPaths, *, role: str = "service") -> int | None:
    """Acquire the home-service singleton lock, or ``None`` if another holds it.

    On success returns an open, locked file descriptor that the caller must keep
    open for the life of the process (and release with :func:`release_singleton`
    on clean shutdown). The lock file also records the holder pid so status/doctor
    can report *which* process owns the service.
    """
    paths.service_dir.mkdir(parents=True, exist_ok=True)
    path = singleton_path(paths)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    if not _flock_exclusive_nonblocking(fd):
        with contextlib.suppress(OSError):
            os.close(fd)
        return None
    recorded_role = role if role in {"service", "update"} else "service"
    try:
        # On Windows byte zero is the msvcrt lock sentinel. Keep JSON outside
        # that locked range so status/doctor can read holder metadata through a
        # second handle while the service owns the lock.
        metadata_offset = 1 if sys.platform == "win32" else 0
        os.ftruncate(fd, metadata_offset)
        os.lseek(fd, metadata_offset, os.SEEK_SET)
        os.write(
            fd,
            json.dumps(
                {"pid": os.getpid(), "ts": time.time(), "role": recorded_role}
            ).encode("utf-8"),
        )
    except OSError:
        _flock_release(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
        return None
    return fd


def release_singleton(fd: int | None) -> None:
    if fd is None:
        return
    _flock_release(fd)
    with contextlib.suppress(OSError):
        os.close(fd)


def singleton_holder_info(paths: OmniPaths) -> dict | None:
    """Metadata recorded by the process currently holding the singleton lock."""
    path = singleton_path(paths)
    if not path.exists():
        return None
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return None
    if _flock_exclusive_nonblocking(fd):
        _flock_release(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
        return None
    try:
        if sys.platform == "win32":
            os.lseek(fd, 1, os.SEEK_SET)
            payload = os.read(fd, 64 * 1024).decode("utf-8")
        else:
            payload = path.read_text(encoding="utf-8")
        data = json.loads(payload)
        pid = int(data.get("pid", 0) or 0)
    except (OSError, ValueError, TypeError):
        with contextlib.suppress(OSError):
            os.close(fd)
        return None
    # The holder may have exited between the first failed probe and metadata
    # read. Re-probe before trusting a live-but-now-stale recorded PID.
    if _flock_exclusive_nonblocking(fd):
        _flock_release(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
        return None
    with contextlib.suppress(OSError):
        os.close(fd)
    if pid <= 0 or not pid_alive(pid) or not isinstance(data, dict):
        return None
    info = dict(data)
    info["pid"] = pid
    info["role"] = str(info.get("role") or "service")
    return info


def singleton_holder_pid(paths: OmniPaths) -> int | None:
    """PID recorded by the process that currently holds the singleton lock."""
    info = singleton_holder_info(paths)
    return int(info["pid"]) if info is not None else None


__all__ = [
    "HEARTBEAT_STALE_SECONDS",
    "LifecycleLock",
    "LifecycleLockTimeout",
    "ServiceDesiredState",
    "ServiceObservation",
    "acquire_singleton",
    "clear_runtime",
    "clear_runtime_if_owner",
    "clear_start_request",
    "default_launcher",
    "desired_path",
    "lifecycle_lock",
    "launcher_service_instance_id",
    "observe_service",
    "read_desired",
    "read_runtime",
    "release_singleton",
    "runtime_path",
    "request_start",
    "service_dir",
    "service_instance_id",
    "service_is_active",
    "service_is_ready",
    "service_is_running",
    "service_runtime_info",
    "singleton_holder_pid",
    "singleton_holder_info",
    "singleton_path",
    "start_request_path",
    "start_requested",
    "touch_runtime",
    "write_desired",
    "write_runtime",
]
