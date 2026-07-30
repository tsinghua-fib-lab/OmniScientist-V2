"""OS supervisor adapters for the home background service.

The home service must survive logout/login and crashes, so it is registered
with the platform's user-session supervisor rather than merely spawned:

* macOS  → a per-user LaunchAgent (``launchctl``)
* Linux  → a systemd *user* unit (``systemctl --user``)
* Windows → a per-user Scheduled Task firing ``ONLOGON`` (``schtasks``)

When no supervisor is available (headless Linux without a user bus, locked-down
hosts, tests) we fall back to a **detached** best-effort process that at least
keeps the service up for the current login session.

Unit/plist/command *generation* is pure and deterministic (unit-testable
without touching the host); only ``install``/``start``/``stop`` shell out, and
every host call is guarded so an unavailable supervisor degrades to a clear
message instead of raising.
"""

from __future__ import annotations

import hashlib
import os
import platform
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from omni.config.paths import OmniPaths
from omni.runtime.daemon import pid_alive
from omni.runtime.service_state import service_instance_id, singleton_holder_info


def service_label(home: Path, *, style: str = "reverse-dns") -> str:
    """Stable, per-``OMNI_HOME`` supervisor label.

    Hashing the home path keeps two data directories (e.g. a test ``OMNI_HOME``
    and the real one) from registering the same unit, so ``enable`` in one never
    clobbers the other.
    """
    digest = hashlib.sha256(str(home.resolve()).encode("utf-8")).hexdigest()[:8]
    if style == "reverse-dns":
        return f"com.omniscientist.omni.{digest}"
    return f"omni-home-service-{digest}"


@dataclass
class SupervisorSpec:
    """Everything a supervisor needs to (re)install and run the service."""

    paths: OmniPaths
    argv: list[str]
    workdir: Path
    log_path: Path
    env: dict[str, str] = field(default_factory=dict)

    @property
    def home(self) -> Path:
        return self.paths.home


# ── generation (pure) ────────────────────────────────────────────────────────


def render_launchd_plist(label: str, spec: SupervisorSpec) -> bytes:
    plist = {
        "Label": label,
        "ProgramArguments": list(spec.argv),
        "RunAtLoad": True,
        # KeepAlive with SuccessfulExit=False restarts on crash but not after a
        # clean stop (so ``omni serve stop`` / ``disable`` stays stopped).
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "WorkingDirectory": str(spec.workdir),
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.log_path),
        "ProcessType": "Background",
    }
    if spec.env:
        plist["EnvironmentVariables"] = dict(spec.env)
    return plistlib.dumps(plist)


def render_systemd_unit(label: str, spec: SupervisorSpec) -> str:
    env_lines = "".join(f"Environment={k}={v}\n" for k, v in sorted(spec.env.items()))
    exec_start = " ".join(_shell_quote(part) for part in spec.argv)
    return (
        "[Unit]\n"
        "Description=OmniScientist home background service\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={spec.workdir}\n"
        f"{env_lines}"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f"StandardOutput=append:{spec.log_path}\n"
        f"StandardError=append:{spec.log_path}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_startup_cmd(spec: SupervisorSpec) -> str:
    """A ``.cmd`` wrapper for Windows (schtasks ``/TR`` cannot hold a full argv)."""
    env_lines = "".join(f'set "{k}={v}"\r\n' for k, v in sorted(spec.env.items()))
    argv = " ".join(_win_quote(part) for part in spec.argv)
    return (
        "@echo off\r\n"
        f'cd /d "{spec.workdir}"\r\n'
        f"{env_lines}"
        f'{argv} >> "{spec.log_path}" 2>&1\r\n'
    )


def render_schtasks_create(label: str, wrapper: Path) -> list[str]:
    return [
        "schtasks", "/Create", "/F",
        "/SC", "ONLOGON",
        "/TN", label,
        "/TR", f'"{wrapper}"',
        "/RL", "LIMITED",
    ]


def _shell_quote(part: str) -> str:
    if part and all(c.isalnum() or c in "-_./:=@%+" for c in part):
        return part
    return "'" + part.replace("'", "'\\''") + "'"


def _win_quote(part: str) -> str:
    if part and all(c.isalnum() or c in "-_./:=@%+\\" for c in part):
        return part
    return '"' + part.replace('"', '\\"') + '"'


# ── host invocation ──────────────────────────────────────────────────────────


def _run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: float = 20.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - argv is built from trusted service state.
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
            env={**os.environ, **(env or {})},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _detached_popen_kwargs() -> dict[str, object]:
    if sys.platform == "win32":
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": detached | new_group}
    return {"start_new_session": True}


def _terminate_and_wait(pid: int, *, timeout: float = 12.0) -> bool:
    if pid <= 0:
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return not pid_alive(pid)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    if sys.platform != "win32":
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        else:
            time.sleep(0.5)
    return not pid_alive(pid)


# ── supervisor adapters ──────────────────────────────────────────────────────


class Supervisor:
    """Common interface; concrete adapters override the host-specific parts."""

    id = "base"

    def __init__(self, spec: SupervisorSpec) -> None:
        self.spec = spec
        self.label = service_label(spec.home, style=self._label_style())

    def _label_style(self) -> str:
        return "reverse-dns"

    @classmethod
    def available(cls) -> bool:  # pragma: no cover - overridden
        return False

    def install(self) -> tuple[bool, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def uninstall(self) -> tuple[bool, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def start(self) -> tuple[bool, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def activate(self) -> tuple[bool, str]:
        """Install/register and issue exactly one launch-producing action.

        The default is correct for adapters whose ``install`` is registration
        only (detached and schtasks). Adapters whose install command already
        starts the job must override this method.
        """
        installed, install_detail = self.install()
        if not installed:
            return False, install_detail
        started, start_detail = self.start()
        return started, f"{install_detail}; {start_detail}"

    def stop(self) -> tuple[bool, str]:
        """Terminate only the kernel-verified singleton owner.

        A raw runtime pid is not authority: after a crash its pid can be reused
        by an unrelated process. Native supervisors stop their own registered
        job first, while detached/compatibility cleanup uses the singleton and
        service-id process marker.
        """
        holder = singleton_holder_info(self.spec.paths) or {}
        try:
            pid = int(holder.get("pid", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        if holder.get("role") == "update":
            return True, "service not running"
        if pid <= 0 or not pid_alive(pid):
            return True, "service not running"
        if pid == os.getpid():
            return False, "refusing to stop the lifecycle controller process"
        if _terminate_and_wait(pid):
            return True, f"stopped pid={pid}"
        return False, f"could not stop pid={pid}"

    def status(self) -> str:  # pragma: no cover - overridden by liveness in state
        return "unknown"

    def is_quiescent(self) -> bool:
        """Whether this manager can no longer launch/relaunch the service."""
        return self.status() != "running"


class DetachedSupervisor(Supervisor):
    """Best-effort fallback: spawn a detached process, no OS supervision.

    Keeps the service up for the current login session only; it will not restart
    after logout/reboot. Used when no user-session supervisor is available.
    """

    id = "detached"

    @classmethod
    def available(cls) -> bool:
        return True

    def install(self) -> tuple[bool, str]:
        return True, "detached: no unit to install"

    def uninstall(self) -> tuple[bool, str]:
        return True, "detached: no unit to remove"

    def start(self) -> tuple[bool, str]:
        self.spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.spec.log_path.open("ab") as log:
            subprocess.Popen(  # noqa: S603 - argv is built from trusted service state.
                self.spec.argv,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                cwd=str(self.spec.workdir),
                env={**os.environ, **self.spec.env},
                **_detached_popen_kwargs(),
            )
        return True, "detached service started"


class LaunchdSupervisor(Supervisor):
    id = "launchd"

    def _plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.label}.plist"

    def _legacy_label(self) -> str:
        return f"com.omniscientist.service.{service_instance_id(self.spec.paths)[:10]}"

    def _legacy_plist_path(self) -> Path:
        return (
            Path.home()
            / "Library"
            / "LaunchAgents"
            / f"{self._legacy_label()}.plist"
        )

    @classmethod
    def available(cls) -> bool:
        return sys.platform == "darwin" and shutil.which("launchctl") is not None

    def _domain_target(self) -> str:
        return f"gui/{os.getuid()}"

    def _job_status(self, label: str) -> str:
        rc, out = _run(
            ["launchctl", "print", f"{self._domain_target()}/{label}"]
        )
        if rc != 0:
            return "not-installed"
        return "running" if "state = running" in out.lower() else "loaded"

    def _wait_job_absent(self, label: str, *, timeout: float = 5.0) -> bool:
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if self._job_status(label) == "not-installed":
                return True
            time.sleep(0.1)
        return self._job_status(label) == "not-installed"

    def _retire_legacy(self) -> tuple[bool, str]:
        """Unload the pre-home-service LaunchAgent for this exact OMNI_HOME."""
        path = self._legacy_plist_path()
        if not path.is_file():
            label = self._legacy_label()
            _run(["launchctl", "bootout", f"{self._domain_target()}/{label}"])
            if not self._wait_job_absent(label):
                return False, f"legacy LaunchAgent remained loaded: {label}"
            return True, ""
        try:
            payload = plistlib.loads(path.read_bytes())
            recorded_home = str(
                (payload.get("EnvironmentVariables") or {}).get("OMNI_HOME") or ""
            )
        except (OSError, ValueError, TypeError):
            return False, f"could not validate legacy LaunchAgent {path}"
        try:
            same_home = Path(recorded_home).expanduser().resolve() == self.spec.home.resolve()
        except (OSError, RuntimeError):
            same_home = False
        if not same_home:
            return False, f"legacy LaunchAgent home mismatch: {path}"

        label = self._legacy_label()
        _run(["launchctl", "bootout", f"{self._domain_target()}/{label}"])
        _run(["launchctl", "bootout", self._domain_target(), str(path)])
        _run(["launchctl", "unload", "-w", str(path)])
        if not self._wait_job_absent(label):
            return False, f"legacy LaunchAgent remained loaded: {label}"
        try:
            path.unlink()
        except OSError as exc:
            return False, f"could not remove legacy LaunchAgent {path}: {exc}"
        return True, f"retired legacy LaunchAgent {label}"

    def install(self) -> tuple[bool, str]:
        legacy_ok, legacy_detail = self._retire_legacy()
        if not legacy_ok:
            return False, legacy_detail
        path = self._plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_launchd_plist(self.label, self.spec))
        # ``bootout`` first so a re-install picks up new argv/env; ignore failure.
        _run(["launchctl", "bootout", self._domain_target(), str(path)])
        if not self._wait_job_absent(self.label):
            return False, f"launchd job did not unload before bootstrap: {self.label}"
        rc, out = _run(["launchctl", "bootstrap", self._domain_target(), str(path)])
        if rc != 0:
            # Older macOS: fall back to load -w.
            rc, out = _run(["launchctl", "load", "-w", str(path)])
        return (rc == 0), (out or f"installed {path}")

    def uninstall(self) -> tuple[bool, str]:
        path = self._plist_path()
        _run(["launchctl", "bootout", self._domain_target(), str(path)])
        _run(["launchctl", "unload", "-w", str(path)])
        try:
            path.unlink()
        except OSError:
            pass
        legacy_ok, legacy_detail = self._retire_legacy()
        detail = f"removed {path}"
        if legacy_detail:
            detail = f"{detail}; {legacy_detail}"
        return legacy_ok, detail

    def start(self) -> tuple[bool, str]:
        rc, out = _run(["launchctl", "kickstart", "-k", f"{self._domain_target()}/{self.label}"])
        if rc == 0:
            return True, "kickstarted launchd service"
        return self.install()

    def activate(self) -> tuple[bool, str]:
        """Bootstrap+RunAtLoad is itself the one launch action."""
        return self.install()

    def stop(self) -> tuple[bool, str]:
        legacy_ok, legacy_detail = self._retire_legacy()
        rc, out = _run(
            ["launchctl", "bootout", f"{self._domain_target()}/{self.label}"]
        )
        absent = self._wait_job_absent(self.label)
        pid_ok, pid_detail = super().stop()
        ok = legacy_ok and absent and pid_ok
        details = [out or ("booted out" if rc == 0 else "bootout failed")]
        if legacy_detail:
            details.append(legacy_detail)
        details.append(pid_detail)
        if not absent:
            details.append("launchd job remains loaded")
        return ok, "; ".join(details)

    def status(self) -> str:
        return self._job_status(self.label)

    def is_quiescent(self) -> bool:
        return (
            self.status() == "not-installed"
            and self._job_status(self._legacy_label()) == "not-installed"
            and not self._legacy_plist_path().exists()
        )


class SystemdUserSupervisor(Supervisor):
    id = "systemd"

    def _label_style(self) -> str:
        return "plain"

    def _unit_name(self) -> str:
        return f"{self.label}.service"

    def _unit_path(self) -> Path:
        base = os.environ.get("XDG_CONFIG_HOME", "").strip()
        root = Path(base) if base else Path.home() / ".config"
        return root / "systemd" / "user" / self._unit_name()

    def _legacy_unit_name(self) -> str:
        return f"omni-{service_instance_id(self.spec.paths)[:10]}.service"

    def _legacy_unit_path(self) -> Path:
        return self._unit_path().with_name(self._legacy_unit_name())

    def _unit_status(self, unit_name: str) -> str:
        rc, out = _run(["systemctl", "--user", "is-active", unit_name])
        state = out.strip().lower().splitlines()[0] if out.strip() else ""
        if state == "active":
            return "running"
        if state in {"inactive", "failed"}:
            return "stopped"
        if state in {"unknown", "not-found"} or (
            rc != 0 and ("not found" in state or "could not be found" in state)
        ):
            return "not-installed"
        return state or "unknown"

    def _legacy_recorded_home(self, path: Path) -> str:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        for line in lines:
            key, separator, raw_value = line.strip().partition("=")
            if not separator or key.lower() != "environment":
                continue
            try:
                values = shlex.split(raw_value)
            except ValueError:
                continue
            for value in values:
                if value.startswith("OMNI_HOME="):
                    return value.removeprefix("OMNI_HOME=")
        return ""

    def _retire_legacy(self) -> tuple[bool, str]:
        """Disable and remove the pre-home-service unit for this exact home."""
        path = self._legacy_unit_path()
        if path.is_file():
            recorded_home = self._legacy_recorded_home(path)
            try:
                same_home = (
                    Path(recorded_home).expanduser().resolve()
                    == self.spec.home.resolve()
                )
            except (OSError, RuntimeError):
                same_home = False
            if not same_home:
                return False, f"legacy systemd unit home mismatch: {path}"

        unit = self._legacy_unit_name()
        _run(["systemctl", "--user", "stop", unit])
        _run(["systemctl", "--user", "disable", unit])
        if self._unit_status(unit) not in {"stopped", "not-installed"}:
            return False, f"legacy systemd unit remained active: {unit}"
        if path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                return False, f"could not remove legacy systemd unit {path}: {exc}"
            _run(["systemctl", "--user", "daemon-reload"])
            return True, f"retired legacy systemd unit {unit}"
        return True, ""

    @classmethod
    def available(cls) -> bool:
        if sys.platform not in ("linux", "linux2"):
            return False
        if shutil.which("systemctl") is None:
            return False
        # A user manager needs a session bus; absent it (headless cron, docker)
        # ``systemctl --user`` fails, so fall back to detached.
        rc, _ = _run(["systemctl", "--user", "is-system-running"], timeout=5.0)
        return rc in (0, 1)  # 1 == degraded but usable

    def install(self) -> tuple[bool, str]:
        legacy_ok, legacy_detail = self._retire_legacy()
        if not legacy_ok:
            return False, legacy_detail
        path = self._unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_systemd_unit(self.label, self.spec), encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        rc, out = _run(["systemctl", "--user", "enable", "--now", self._unit_name()])
        return (rc == 0), (out or f"installed {path}")

    def uninstall(self) -> tuple[bool, str]:
        legacy_ok, legacy_detail = self._retire_legacy()
        _run(["systemctl", "--user", "disable", "--now", self._unit_name()])
        try:
            self._unit_path().unlink()
        except OSError:
            pass
        _run(["systemctl", "--user", "daemon-reload"])
        detail = f"removed {self._unit_name()}"
        if legacy_detail:
            detail = f"{detail}; {legacy_detail}"
        return legacy_ok, detail

    def start(self) -> tuple[bool, str]:
        rc, out = _run(["systemctl", "--user", "restart", self._unit_name()])
        if rc == 0:
            return True, "restarted systemd unit"
        return self.install()

    def activate(self) -> tuple[bool, str]:
        """``enable --now`` installs and starts; do not immediately restart."""
        return self.install()

    def stop(self) -> tuple[bool, str]:
        legacy_ok, legacy_detail = self._retire_legacy()
        rc, out = _run(["systemctl", "--user", "stop", self._unit_name()])
        pid_ok, pid_detail = super().stop()
        quiescent = self.is_quiescent()
        ok = legacy_ok and quiescent and pid_ok
        detail = out or ("stopped systemd unit" if rc == 0 else "systemctl stop failed")
        if legacy_detail:
            detail = f"{detail}; {legacy_detail}"
        if pid_detail != "service not running":
            detail = f"{detail}; {pid_detail}"
        if not quiescent:
            detail = f"{detail}; systemd unit remains active"
        return ok, detail

    def status(self) -> str:
        return self._unit_status(self._unit_name())

    def is_quiescent(self) -> bool:
        quiet = {"stopped", "not-installed"}
        return (
            self.status() in quiet
            and self._unit_status(self._legacy_unit_name()) in quiet
            and not self._legacy_unit_path().exists()
        )


class SchtasksSupervisor(Supervisor):
    id = "schtasks"

    def _label_style(self) -> str:
        return "plain"

    def _wrapper_path(self) -> Path:
        return self.spec.paths.service_dir / "home-service.cmd"

    def _legacy_label(self) -> str:
        return f"OmniScientist-{service_instance_id(self.spec.paths)[:10]}"

    def _legacy_wrapper_path(self) -> Path:
        return self.spec.paths.service_dir / "omni-service.cmd"

    def _task_status(self, label: str) -> str:
        rc, out = _run(["schtasks", "/Query", "/TN", label])
        if rc != 0:
            return "not-installed"
        return "running" if "running" in out.lower() else "stopped"

    def _legacy_recorded_home(self, path: Path) -> str:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        prefix = 'set "OMNI_HOME='
        for line in lines:
            stripped = line.strip()
            if stripped.lower().startswith(prefix.lower()) and stripped.endswith('"'):
                return stripped[len(prefix):-1]
        return ""

    def _retire_legacy(self) -> tuple[bool, str]:
        """Delete the old scheduled task after validating its launcher home."""
        path = self._legacy_wrapper_path()
        if path.is_file():
            recorded_home = self._legacy_recorded_home(path)
            try:
                same_home = os.path.normcase(
                    str(Path(recorded_home).expanduser().resolve())
                ) == os.path.normcase(str(self.spec.home.resolve()))
            except (OSError, RuntimeError):
                same_home = False
            if not same_home:
                return False, f"legacy scheduled task home mismatch: {path}"

        label = self._legacy_label()
        _run(["schtasks", "/End", "/TN", label])
        _run(["schtasks", "/Delete", "/F", "/TN", label])
        if self._task_status(label) != "not-installed":
            return False, f"legacy scheduled task remained installed: {label}"
        if path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                return False, f"could not remove legacy task wrapper {path}: {exc}"
            return True, f"retired legacy scheduled task {label}"
        return True, ""

    @classmethod
    def available(cls) -> bool:
        return sys.platform == "win32" and shutil.which("schtasks") is not None

    def install(self) -> tuple[bool, str]:
        legacy_ok, legacy_detail = self._retire_legacy()
        if not legacy_ok:
            return False, legacy_detail
        wrapper = self._wrapper_path()
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(render_startup_cmd(self.spec), encoding="utf-8")
        rc, out = _run(render_schtasks_create(self.label, wrapper))
        return (rc == 0), (out or f"installed task {self.label}")

    def uninstall(self) -> tuple[bool, str]:
        legacy_ok, legacy_detail = self._retire_legacy()
        _run(["schtasks", "/Delete", "/F", "/TN", self.label])
        try:
            self._wrapper_path().unlink()
        except OSError:
            pass
        detail = f"removed task {self.label}"
        if legacy_detail:
            detail = f"{detail}; {legacy_detail}"
        return legacy_ok, detail

    def start(self) -> tuple[bool, str]:
        rc, out = _run(["schtasks", "/Run", "/TN", self.label])
        if rc == 0:
            return True, "ran scheduled task"
        return self.install()

    def stop(self) -> tuple[bool, str]:
        legacy_ok, legacy_detail = self._retire_legacy()
        _run(["schtasks", "/End", "/TN", self.label])
        pid_ok, pid_detail = super().stop()
        detail = pid_detail
        if legacy_detail:
            detail = f"{legacy_detail}; {detail}"
        return legacy_ok and pid_ok, detail

    def status(self) -> str:
        return self._task_status(self.label)

    def is_quiescent(self) -> bool:
        return (
            self.status() in {"stopped", "not-installed"}
            and self._task_status(self._legacy_label()) == "not-installed"
            and not self._legacy_wrapper_path().exists()
        )


_MANAGERS: dict[str, type[Supervisor]] = {
    "launchd": LaunchdSupervisor,
    "systemd": SystemdUserSupervisor,
    "schtasks": SchtasksSupervisor,
    "detached": DetachedSupervisor,
}


def _platform_default(plat: str) -> type[Supervisor]:
    if plat == "darwin":
        return LaunchdSupervisor
    if plat.startswith("linux"):
        return SystemdUserSupervisor
    if plat == "win32":
        return SchtasksSupervisor
    return DetachedSupervisor


def select_supervisor_class(manager: str = "auto", *, plat: str | None = None) -> type[Supervisor]:
    """Resolve the supervisor class for ``manager`` on ``plat`` (defaults to host).

    ``auto`` picks the platform-native supervisor when it is actually available
    and otherwise degrades to :class:`DetachedSupervisor`. An explicit id is
    honoured even when unavailable (so ``doctor`` can report *why* it failed).
    """
    plat = plat or sys.platform
    key = (manager or "auto").strip().lower()
    if key in _MANAGERS:
        return _MANAGERS[key]
    native = _platform_default(plat)
    return native if native.available() else DetachedSupervisor


def make_supervisor(spec: SupervisorSpec, manager: str = "auto") -> Supervisor:
    return select_supervisor_class(manager)(spec)


def describe_platform() -> dict[str, object]:
    """Diagnostic snapshot of which supervisors this host supports."""
    return {
        "platform": sys.platform,
        "system": platform.system(),
        "launchd": LaunchdSupervisor.available(),
        "systemd": SystemdUserSupervisor.available(),
        "schtasks": SchtasksSupervisor.available(),
        "auto": select_supervisor_class("auto").id,
    }


__all__ = [
    "DetachedSupervisor",
    "LaunchdSupervisor",
    "SchtasksSupervisor",
    "Supervisor",
    "SupervisorSpec",
    "SystemdUserSupervisor",
    "describe_platform",
    "make_supervisor",
    "render_launchd_plist",
    "render_schtasks_create",
    "render_startup_cmd",
    "render_systemd_unit",
    "select_supervisor_class",
    "service_label",
]
