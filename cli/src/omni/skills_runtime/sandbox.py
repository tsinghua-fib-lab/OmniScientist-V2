"""OS-level sandbox wrapping for local command execution (P2-F).

Turns the agent's coarse denylist guard into real kernel confinement when the
platform offers it: macOS ``sandbox-exec`` (seatbelt), Linux ``bwrap``
(bubblewrap) or ``firejail``. The confinement allows reads everywhere but
restricts **writes** to the workspace/home/tmp — so a hijacked command can't
scribble over the user's filesystem.

``os_sandbox="auto"`` uses a backend only after a functional probe and otherwise
falls back to the documented coarse guard. Explicit backend selections fail
closed when the sandbox cannot actually run. ``bash_sandbox="full"`` explicitly
opts out of confinement.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MACOS = platform.system() == "Darwin"

# Warn at most once per process when confinement silently falls back to "off".
_UNSANDBOXED_WARNED = False

# Always-writable system paths a normal command needs (tmp, stdio, dev null).
_SYSTEM_WRITE_ROOTS = (
    "/tmp", "/private/tmp", "/private/var/tmp", "/private/var/folders", "/dev",
)


class SandboxUnavailableError(RuntimeError):
    """An explicitly requested sandbox backend is not usable."""


@cache
def _sandbox_works(name: str) -> bool:
    binary = shutil.which(name)
    if not binary:
        return False
    commands = {
        "sandbox-exec": [binary, "-p", "(version 1)(allow default)", "/usr/bin/true"],
        "bwrap": [binary, "--ro-bind", "/", "/", "--", "/bin/true"],
        "firejail": [binary, "--quiet", "--noprofile", "/bin/true"],
    }
    try:
        result = subprocess.run(commands[name], capture_output=True, check=False, timeout=3)
    except (KeyError, OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def detect_sandbox() -> str:
    """Return the best available sandbox backend name, or ``""``."""
    if _MACOS and _sandbox_works("sandbox-exec"):
        return "sandbox-exec"
    for name in ("bwrap", "firejail"):
        if _sandbox_works(name):
            return name
    return ""


def resolve_sandbox(setting: str) -> str:
    """Resolve ``security.os_sandbox`` to an available backend name (or ``""``)."""
    s = (setting or "auto").strip().lower()
    if s in ("off", "none", "disabled", ""):
        return ""
    if s == "auto":
        return detect_sandbox()
    if s == "sandbox-exec":
        if _MACOS and _sandbox_works("sandbox-exec"):
            return "sandbox-exec"
        raise SandboxUnavailableError("sandbox-exec is unavailable or failed its functional probe")
    if s in ("bwrap", "firejail"):
        if _sandbox_works(s):
            return s
        raise SandboxUnavailableError(f"{s} is unavailable or failed its functional probe")
    raise SandboxUnavailableError(f"unknown OS sandbox backend: {s}")


def _write_roots(paths: Any) -> list[str]:
    roots: list[str] = []
    for attr in ("project_dir", "workspace_root", "home"):
        p = getattr(paths, attr, None)
        if p:
            roots.append(str(Path(p)))
    roots.extend(_SYSTEM_WRITE_ROOTS)
    seen: set[str] = set()
    out: list[str] = []
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _seatbelt_profile(write_roots: list[str], *, deny_network: bool = False) -> str:
    """A permissive-read / confined-write seatbelt profile string."""
    allow = " ".join(f'(subpath "{r}")' for r in write_roots)
    net = "(deny network*)" if deny_network else ""
    return (
        "(version 1)"
        "(allow default)"
        f"{net}"
        "(deny file-write*)"
        f'(allow file-write* {allow} (literal "/dev/null") '
        '(literal "/dev/stdout") (literal "/dev/stderr"))'
    )


def _deny_network(security: Any) -> bool:
    return (getattr(security, "sandbox_network", "allow") or "allow").strip().lower() == "deny"


def _warn_unsandboxed_once(security: Any) -> None:
    """One-shot warning when ``os_sandbox=auto`` silently runs unconfined.

    We stay quiet for the *explicit* opt-outs (``bash_sandbox=full`` or
    ``os_sandbox=off``) — those are deliberate. The dangerous case is "auto with
    no working backend", where the user believes they are confined but aren't.
    """
    global _UNSANDBOXED_WARNED
    if _UNSANDBOXED_WARNED:
        return
    if getattr(security, "bash_sandbox", "readonly") == "full":
        return
    if (getattr(security, "os_sandbox", "auto") or "auto").strip().lower() in (
        "off", "none", "disabled", "",
    ):
        return
    _UNSANDBOXED_WARNED = True
    logger.warning(
        "OS sandbox unavailable (security.os_sandbox=auto): local commands run "
        "WITHOUT kernel confinement — only the coarse denylist applies. Install "
        "bubblewrap or firejail (Linux), or set security.os_sandbox=off to accept "
        "this explicitly and silence this warning."
    )


def sandbox_prefix(security: Any, paths: Any, *, warn_on_fallback: bool = False) -> list[str]:
    """Return an argv prefix that confines a following command, or ``[]``.

    Prepend the result to a command argv (``[*prefix, "/bin/sh", "-c", cmd]``).
    Empty when confinement is disabled/unavailable — callers run unwrapped. When
    ``warn_on_fallback`` is set, a one-shot warning is logged if ``auto`` silently
    degrades to unconfined execution (so a "sandboxed" run isn't secretly bare).
    """
    if getattr(security, "bash_sandbox", "readonly") == "full":
        return []
    backend = resolve_sandbox(getattr(security, "os_sandbox", "auto"))
    if not backend:
        if warn_on_fallback:
            _warn_unsandboxed_once(security)
        return []
    roots = _write_roots(paths)
    deny_net = _deny_network(security)
    if backend == "sandbox-exec":
        return ["sandbox-exec", "-p", _seatbelt_profile(roots, deny_network=deny_net)]
    if backend == "bwrap":
        argv = ["bwrap", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev",
                "--proc", "/proc", "--tmpfs", "/tmp"]
        if deny_net:
            argv.append("--unshare-net")
        for r in roots:
            if r not in ("/tmp",) and Path(r).exists():
                argv += ["--bind", r, r]
        return argv
    if backend == "firejail":
        argv = ["firejail", "--quiet", "--noprofile"]
        if deny_net:
            argv.append("--net=none")
        for r in roots:
            if Path(r).exists():
                argv += [f"--whitelist={r}"]
        return argv
    return []


__all__ = ["SandboxUnavailableError", "detect_sandbox", "resolve_sandbox", "sandbox_prefix"]
