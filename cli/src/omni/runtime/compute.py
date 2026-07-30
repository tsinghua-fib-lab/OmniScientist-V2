"""Compute backends (P1): run a command locally, in Docker, over SSH, on Slurm,
or on Modal — behind one small ``ComputeBackend`` abstraction.

Design goals:

* **One extensible abstraction.** Every backend implements the same
  :class:`ComputeBackend` protocol — ``available`` (is it usable under this
  config?), ``build`` (turn a command into an argv/shell :class:`ExecPlan`), and
  ``finalize`` (shape the captured output into a :class:`ComputeResult`). Adding
  a backend is one small object + a registry entry, not another ``if`` arm in a
  growing dispatcher. Named backends live in :data:`BACKENDS`.
* **Local-first / offline-safe.** ``backend="local"`` runs a plain subprocess.
  ``docker`` / ``ssh`` / ``slurm`` shell out to the system ``docker`` / ``ssh`` /
  ``sbatch`` binaries (no Python deps); ``modal`` uses the optional ``modal``
  package/CLI. When a remote backend is unavailable (missing binary/SDK or
  unconfigured) it **degrades to local** if ``fallback_local`` — the tool never
  hard-fails for lack of a cluster.
* **Testable.** Command construction (``plan_*`` / ``build``) is pure and
  deterministic, and execution goes through a single injectable ``_exec`` so
  tests can drive docker / SSH / Slurm paths without a real daemon or server.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import shlex
import shutil
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from omni.runtime.processes import process_group_options, signal_process_tree, stop_process_tree


@dataclass(slots=True)
class ComputeResult:
    backend: str
    status: str            # ok | error | timeout | submitted | cancelled
    returncode: int
    stdout: str
    command: str
    detail: str = ""       # slurm job id / ssh host / docker image / fallback note

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "status": self.status,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "command": self.command,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ExecPlan:
    """How to run a command: an ``argv`` (exec) or a single shell string."""

    argv: list[str]
    shell: bool = False


_MAX_OUTPUT = 100_000
CancelCheck = Callable[[], bool | Awaitable[bool]]


class ComputeCancelled(RuntimeError):
    """A managed compute job observed a durable cancellation request."""


# ── command construction (pure) ─────────────────────────────────────────────


def plan_ssh(command: str, cfg: Any) -> list[str]:
    """Build the local ``ssh`` argv that runs ``command`` on the remote host."""
    remote = command
    if cfg.workdir:
        remote = f"cd {shlex.quote(cfg.workdir)} && {command}"
    argv = ["ssh", "-o", "BatchMode=yes"]
    if cfg.ssh_port and int(cfg.ssh_port) != 22:
        argv += ["-p", str(cfg.ssh_port)]
    if cfg.ssh_key:
        argv += ["-i", cfg.ssh_key]
    argv += [cfg.ssh_host, remote]
    return argv


def plan_slurm(command: str, cfg: Any) -> list[str]:
    """Build the ``sbatch`` argv (optionally tunnelled through ssh)."""
    sbatch = ["sbatch", "--parsable"]
    if cfg.slurm_partition:
        sbatch += ["--partition", cfg.slurm_partition]
    if cfg.slurm_cpus:
        sbatch += ["--cpus-per-task", str(cfg.slurm_cpus)]
    if cfg.slurm_mem:
        sbatch += ["--mem", cfg.slurm_mem]
    if cfg.slurm_time:
        sbatch += ["--time", cfg.slurm_time]
    sbatch += [f"--wrap={command}"]
    if cfg.slurm_via_ssh and cfg.ssh_host:
        return plan_ssh(shlex.join(sbatch), cfg)
    return sbatch


def plan_modal(command: str, cfg: Any) -> list[str]:
    """Build a ``modal run`` argv for the configured app ref."""
    return ["modal", "run", cfg.modal_app, "--command", command]


def plan_docker(command: str, cfg: Any) -> list[str]:
    """Build a ``docker run`` argv for the configured image.

    Ephemeral (``--rm``); GPUs and bind-mounts are opt-in via config. ``workdir``
    doubles as the in-container working dir (``-w``). The command is run through
    ``/bin/sh -c`` inside the image so shell syntax (pipes, ``&&``) works.
    """
    argv = ["docker", "run", "--rm"]
    gpus = str(getattr(cfg, "docker_gpus", "") or "").strip()
    if gpus:
        argv += ["--gpus", gpus]
    for mount in getattr(cfg, "docker_mounts", None) or []:
        mount = str(mount).strip()
        if mount:
            argv += ["-v", mount]
    if cfg.workdir:
        argv += ["-w", cfg.workdir]
    argv += [cfg.docker_image, "/bin/sh", "-c", command]
    return argv


# ── availability ────────────────────────────────────────────────────────────


def modal_available() -> bool:
    return importlib.util.find_spec("modal") is not None or shutil.which("modal") is not None


# ── backend abstraction ──────────────────────────────────────────────────────


@runtime_checkable
class ComputeBackend(Protocol):
    """A place to run a command. Pure planning + result shaping; execution is
    shared through :func:`_exec` so every backend stays testable offline."""

    name: str

    def available(self, cfg: Any) -> tuple[bool, str]:
        """``(usable, reason)`` for this backend under ``cfg``."""
        ...

    def build(self, command: str, cfg: Any, *, exec_prefix: list[str] | None) -> ExecPlan:
        """Turn ``command`` into an :class:`ExecPlan` (argv or shell string)."""
        ...

    def finalize(self, code: int, out: str, command: str, cfg: Any) -> ComputeResult:
        """Shape captured output into a :class:`ComputeResult`."""
        ...


@dataclass(slots=True)
class LocalBackend:
    name: str = "local"

    def available(self, cfg: Any) -> tuple[bool, str]:
        return True, ""

    def build(self, command: str, cfg: Any, *, exec_prefix: list[str] | None) -> ExecPlan:
        if exec_prefix:
            return ExecPlan([*exec_prefix, "/bin/sh", "-c", command], shell=False)
        return ExecPlan([command], shell=True)

    def finalize(self, code: int, out: str, command: str, cfg: Any) -> ComputeResult:
        return ComputeResult("local", "ok" if code == 0 else "error", code, out, command)


@dataclass(slots=True)
class SSHBackend:
    name: str = "ssh"

    def available(self, cfg: Any) -> tuple[bool, str]:
        if not cfg.ssh_host:
            return False, "ssh_host not configured"
        if shutil.which("ssh") is None:
            return False, "ssh binary not found"
        return True, ""

    def build(self, command: str, cfg: Any, *, exec_prefix: list[str] | None) -> ExecPlan:
        return ExecPlan(plan_ssh(command, cfg), shell=False)

    def finalize(self, code: int, out: str, command: str, cfg: Any) -> ComputeResult:
        return ComputeResult("ssh", "ok" if code == 0 else "error", code, out, command, cfg.ssh_host)


@dataclass(slots=True)
class DockerBackend:
    name: str = "docker"

    def available(self, cfg: Any) -> tuple[bool, str]:
        if not getattr(cfg, "docker_image", ""):
            return False, "docker_image not configured"
        if shutil.which("docker") is None:
            return False, "docker binary not found"
        return True, ""

    def build(self, command: str, cfg: Any, *, exec_prefix: list[str] | None) -> ExecPlan:
        return ExecPlan(plan_docker(command, cfg), shell=False)

    def finalize(self, code: int, out: str, command: str, cfg: Any) -> ComputeResult:
        return ComputeResult(
            "docker", "ok" if code == 0 else "error", code, out, command, cfg.docker_image
        )


@dataclass(slots=True)
class SlurmBackend:
    name: str = "slurm"

    def available(self, cfg: Any) -> tuple[bool, str]:
        if cfg.slurm_via_ssh and cfg.ssh_host:
            if shutil.which("ssh") is None:
                return False, "ssh binary not found"
            return True, ""
        if shutil.which("sbatch") is None:
            return False, "sbatch not found (set compute.ssh_host + slurm_via_ssh for remote)"
        return True, ""

    def build(self, command: str, cfg: Any, *, exec_prefix: list[str] | None) -> ExecPlan:
        return ExecPlan(plan_slurm(command, cfg), shell=False)

    def finalize(self, code: int, out: str, command: str, cfg: Any) -> ComputeResult:
        job_id = out.strip().split(";")[0].strip() if code == 0 else ""
        status = "submitted" if code == 0 and job_id else ("error" if code else "ok")
        detail = f"job {job_id}" if job_id else ""
        return ComputeResult("slurm", status, code, out, command, detail)


@dataclass(slots=True)
class ModalBackend:
    name: str = "modal"

    def available(self, cfg: Any) -> tuple[bool, str]:
        if not cfg.modal_app:
            return False, "modal_app not configured"
        if not modal_available():
            return False, "modal package/CLI not installed (pip install 'omniscientist[compute]')"
        return True, ""

    def build(self, command: str, cfg: Any, *, exec_prefix: list[str] | None) -> ExecPlan:
        return ExecPlan(plan_modal(command, cfg), shell=False)

    def finalize(self, code: int, out: str, command: str, cfg: Any) -> ComputeResult:
        return ComputeResult(
            "modal", "ok" if code == 0 else "error", code, out, command, cfg.modal_app
        )


BACKENDS: dict[str, ComputeBackend] = {
    b.name: b
    for b in (LocalBackend(), DockerBackend(), SSHBackend(), SlurmBackend(), ModalBackend())
}


def get_backend(name: str) -> ComputeBackend | None:
    return BACKENDS.get(name)


def backend_names() -> tuple[str, ...]:
    return tuple(BACKENDS)


def backend_available(backend: str, cfg: Any) -> tuple[bool, str]:
    """Return ``(available, reason)`` for a backend under ``cfg``."""
    impl = BACKENDS.get(backend)
    if impl is None:
        return False, f"unknown backend '{backend}'"
    return impl.available(cfg)


# ── execution ───────────────────────────────────────────────────────────────


async def _exec(
    argv: list[str],
    *,
    shell: bool,
    cwd: str,
    timeout: float,
    cancel_check: CancelCheck | None = None,
) -> tuple[int, str]:
    """Run ``argv`` (or a shell string when ``shell``) and capture combined output.

    Isolated behind one function so tests can monkeypatch remote execution.
    """
    if shell:
        proc = await asyncio.create_subprocess_shell(
            argv[0], stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, cwd=cwd or None,
            **process_group_options(),
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, cwd=cwd or None,
            **process_group_options(),
        )
    communicate = asyncio.create_task(proc.communicate())
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                signal_process_tree(proc, force=True)
                with suppress(Exception):
                    await communicate
                raise TimeoutError
            done, _ = await asyncio.wait({communicate}, timeout=min(0.2, remaining))
            if done:
                out, _ = communicate.result()
                break
            if cancel_check is not None:
                requested = cancel_check()
                if inspect.isawaitable(requested):
                    requested = await requested
                if requested:
                    signal_process_tree(proc)
                    try:
                        await asyncio.wait_for(asyncio.shield(communicate), timeout=2.0)
                    except TimeoutError:
                        signal_process_tree(proc, force=True)
                        with suppress(Exception):
                            await communicate
                    raise ComputeCancelled
    except asyncio.CancelledError:
        await stop_process_tree(proc)
        await asyncio.gather(communicate, return_exceptions=True)
        raise
    return proc.returncode or 0, (out or b"").decode("utf-8", errors="replace")


async def run_compute(
    command: str,
    *,
    cfg: Any,
    cwd: str = "",
    timeout: float | None = None,
    backend: str = "",
    exec_prefix: list[str] | None = None,
    cancel_check: CancelCheck | None = None,
) -> ComputeResult:
    """Execute ``command`` on the configured (or overridden) compute backend.

    Resolves the backend from :data:`BACKENDS`, checks availability (degrading to
    local when ``fallback_local`` and the backend is unusable), then builds one
    :class:`ExecPlan`, runs it through :func:`_exec`, and lets the backend shape
    the result. ``exec_prefix`` (e.g. an OS-sandbox wrapper argv) is applied only
    to the local backend; remote backends confine on their own side.
    """
    command = (command or "").strip()
    if not command:
        return ComputeResult("local", "error", -1, "", command, "empty command")

    name = backend or cfg.backend or "local"
    impl = BACKENDS.get(name)
    if impl is None:
        return ComputeResult(name, "error", -1, "", command, f"unknown backend '{name}'")

    timeout = float(timeout if timeout is not None else cfg.timeout_s)

    if impl.name != "local":
        ok, reason = impl.available(cfg)
        if not ok:
            if cfg.fallback_local:
                res = await _dispatch(
                    BACKENDS["local"],
                    command,
                    cfg=cfg,
                    cwd=cwd,
                    timeout=timeout,
                    exec_prefix=exec_prefix,
                    cancel_check=cancel_check,
                )
                res.detail = f"fell back to local: {reason}"
                return res
            return ComputeResult(impl.name, "error", -1, "", command, reason)

    return await _dispatch(
        impl,
        command,
        cfg=cfg,
        cwd=cwd,
        timeout=timeout,
        exec_prefix=exec_prefix,
        cancel_check=cancel_check,
    )


async def _dispatch(
    impl: ComputeBackend, command: str, *, cfg: Any, cwd: str, timeout: float,
    exec_prefix: list[str] | None, cancel_check: CancelCheck | None = None,
) -> ComputeResult:
    """Build → exec → finalize for one backend, mapping failures to results."""
    plan = impl.build(command, cfg, exec_prefix=exec_prefix)
    try:
        kwargs = {"shell": plan.shell, "cwd": cwd, "timeout": timeout}
        if cancel_check is not None:
            kwargs["cancel_check"] = cancel_check
        code, out = await _exec(plan.argv, **kwargs)
    except ComputeCancelled:
        return ComputeResult(impl.name, "cancelled", -1, "", command, "cancelled by user")
    except TimeoutError:
        return ComputeResult(impl.name, "timeout", -1, "", command, f"timed out after {timeout}s")
    except OSError as exc:
        return ComputeResult(impl.name, "error", -1, "", command, f"exec failed: {exc}")
    return impl.finalize(code, out[:_MAX_OUTPUT], command, cfg)


__all__ = [
    "BACKENDS",
    "ComputeBackend",
    "ComputeCancelled",
    "ComputeResult",
    "DockerBackend",
    "ExecPlan",
    "LocalBackend",
    "ModalBackend",
    "SSHBackend",
    "SlurmBackend",
    "backend_available",
    "backend_names",
    "get_backend",
    "modal_available",
    "plan_docker",
    "plan_modal",
    "plan_slurm",
    "plan_ssh",
    "run_compute",
]
