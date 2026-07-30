"""OS-level sandbox wrapping for local command execution (P2-F).

Turns the agent's coarse denylist guard into real kernel confinement when the
platform offers it: macOS ``sandbox-exec`` (seatbelt), Linux ``bwrap``
(bubblewrap) or ``firejail``. The confinement allows reads everywhere but
restricts **writes** to the user source directory, the host-owned outbox, and
scratch — never the Omni control-state store. Codex ``WorkspaceWrite`` is cwd
+ additional roots + a host temp, and does not include ``CODEX_HOME``.

``os_sandbox="auto"`` uses a backend after a functional probe. When none is
available it falls back to the coarse guard with a one-shot warning — stock
Linux (and GitHub-hosted runners) have no bubblewrap, and Omni must still
run. Explicit backend selections fail closed. ``bash_sandbox="full"`` or
``os_sandbox="off"`` explicitly opt out.
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MACOS = platform.system() == "Darwin"

# Warn at most once per process when confinement silently falls back to "off".
_UNSANDBOXED_WARNED = False

# Device nodes a normal command needs. Whole-tree ``/tmp`` is *not* a default
# write root: Codex includes it, but Omni's outbox/scratch are precise paths,
# and opening ``/tmp`` re-opens a world-writable ancestor.
SYSTEM_WRITE_ROOTS = ("/dev",)
_SYSTEM_WRITE_ROOTS = SYSTEM_WRITE_ROOTS
_TMP_MOUNT_POINTS = frozenset({"/tmp", "/private/tmp"})


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


def _explicit_sandbox_off(setting: str) -> bool:
    return (setting or "auto").strip().lower() in ("off", "none", "disabled", "")


def _write_roots(paths: Any) -> list[str]:
    """Fallback write set when a caller does not pass ``writable_roots``.

    Never includes the control-state home, the project store, or whole ``/tmp``.
    """
    from omni.config.paths import opens_any_control_store
    from omni.core.sensitive_paths import PROTECTED_METADATA_NAMES

    roots: list[str] = []
    for attr in ("workspace_root", "invocation_cwd"):
        raw = getattr(paths, attr, None)
        if not raw:
            continue
        path = Path(raw)
        if opens_any_control_store(path):
            continue
        if path.name.lower() in PROTECTED_METADATA_NAMES:
            continue
        roots.append(str(path.resolve()))
    roots.extend(_SYSTEM_WRITE_ROOTS)
    seen: set[str] = set()
    out: list[str] = []
    for root in roots:
        if root and root not in seen:
            seen.add(root)
            out.append(root)
    return out


def _granted_metadata_paths(write_roots: list[str]) -> set[str]:
    """Write roots that *are* a protected metadata directory (e.g. granted ``.git``)."""
    from omni.core.sensitive_paths import PROTECTED_METADATA_NAMES

    granted: set[str] = set()
    for raw in write_roots:
        path = Path(raw)
        if path.name.lower() not in PROTECTED_METADATA_NAMES:
            continue
        try:
            granted.add(str(path.resolve()))
        except OSError:
            granted.add(str(path))
    return granted


def _is_granted_metadata(path: str, granted: set[str]) -> bool:
    if path in granted:
        return True
    try:
        return str(Path(path).resolve()) in granted
    except OSError:
        return False


def _metadata_deny_paths(paths: Any, write_roots: list[str]) -> list[str]:
    """``.git`` / ``.omni`` / ``.agents`` / ``.codex`` under user-source roots."""
    from omni.core.sensitive_paths import PROTECTED_METADATA_NAMES

    granted = _granted_metadata_paths(write_roots)
    bases: list[Path] = []
    for attr in ("invocation_cwd", "workspace_root"):
        raw = getattr(paths, attr, None)
        if raw:
            bases.append(Path(raw))
    skip = set(_SYSTEM_WRITE_ROOTS)
    for raw in write_roots:
        if raw in skip:
            continue
        path = Path(raw)
        if path.name.lower() in PROTECTED_METADATA_NAMES:
            continue
        bases.append(path)
    out: list[str] = []
    seen: set[str] = set()
    for base in bases:
        for name in sorted(PROTECTED_METADATA_NAMES):
            target = str((base / name).resolve()) if base.exists() else str(base / name)
            if _is_granted_metadata(target, granted):
                continue
            if target not in seen:
                seen.add(target)
                out.append(target)
    return out


def _seatbelt_literal(path: str) -> str:
    """Escape a filesystem path for a seatbelt ``subpath`` / ``literal`` atom."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _seatbelt_regex_escape(text: str) -> str:
    return re.escape(text).replace('"', '\\"')


def _seatbelt_metadata_regex(root: str, name: str) -> str:
    """Codex-style regex: deny ``{root}/{name}`` even when the name is missing."""
    trimmed = root.rstrip("/") or "/"
    escaped_root = _seatbelt_regex_escape(trimmed)
    escaped_name = _seatbelt_regex_escape(name)
    if trimmed == "/":
        return f"^/{escaped_name}(/.*)?$"
    return f"^{escaped_root}/{escaped_name}(/.*)?$"


def _seatbelt_write_atom(root: str, granted: set[str]) -> str:
    """Allow writes under *root*, minus protected metadata that was not granted."""
    from omni.core.sensitive_paths import PROTECTED_METADATA_NAMES

    literal = _seatbelt_literal(root)
    if root in SYSTEM_WRITE_ROOTS or Path(root).name.lower() in PROTECTED_METADATA_NAMES:
        return f'(subpath "{literal}")'
    requires = [f'(subpath "{literal}")']
    base = Path(root)
    for name in sorted(PROTECTED_METADATA_NAMES):
        meta = str((base / name).resolve()) if base.exists() else str(base / name)
        if _is_granted_metadata(meta, granted):
            continue
        requires.append(
            f'(require-not (regex #"{_seatbelt_metadata_regex(root, name)}"))'
        )
    if len(requires) == 1:
        return requires[0]
    return f'(require-all {" ".join(requires)})'


def _seatbelt_profile(
    write_roots: list[str],
    *,
    deny_network: bool = False,
    deny_paths: list[str] | None = None,
) -> str:
    """A permissive-read / confined-write seatbelt profile string."""
    granted = _granted_metadata_paths(write_roots)
    allow = " ".join(_seatbelt_write_atom(root, granted) for root in write_roots)
    net = "(deny network*)" if deny_network else ""
    extra_deny = ""
    if deny_paths:
        denied = [
            f'(subpath "{_seatbelt_literal(path)}")'
            for path in deny_paths
            if not _is_granted_metadata(path, granted)
        ]
        if denied:
            extra_deny = f"(deny file-write* {' '.join(denied)})"
    return (
        "(version 1)"
        "(allow default)"
        f"{net}"
        "(deny file-write*)"
        f'(allow file-write* {allow} (literal "/dev/null") '
        '(literal "/dev/stdout") (literal "/dev/stderr"))'
        f"{extra_deny}"
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


def sandbox_prefix(
    security: Any,
    paths: Any,
    *,
    writable_roots: list[str] | None = None,
    persist_tmp: Path | str | None = None,
    warn_on_fallback: bool = False,
) -> list[str]:
    """Return an argv prefix that confines a following command, or ``[]``.

    Prepend the result to a command argv (``[*prefix, "/bin/sh", "-c", cmd]``).
    Empty when confinement is disabled/unavailable — callers run unwrapped. When
    ``warn_on_fallback`` is set, a one-shot warning is logged if ``auto`` silently
    degrades to unconfined execution (so a "sandboxed" run isn't secretly bare).

    ``writable_roots`` is the turn's permission envelope (working directory +
    outbox + scratch). When omitted, the path-object defaults apply.
    ``persist_tmp`` is bind-mounted over ``/tmp`` on bwrap so scratch survives
    across invocations instead of a fresh ``--tmpfs``.
    """
    if getattr(security, "bash_sandbox", "readonly") == "full":
        return []
    setting = getattr(security, "os_sandbox", "auto")
    backend = resolve_sandbox(setting)
    if not backend:
        if warn_on_fallback and not _explicit_sandbox_off(setting):
            _warn_unsandboxed_once(security)
        return []
    roots = [str(root) for root in writable_roots] if writable_roots is not None else _write_roots(paths)
    persist = _safe_persist_tmp(persist_tmp)
    if persist and persist not in roots:
        roots = [*roots, persist]
    deny_net = _deny_network(security)
    deny_meta = _metadata_deny_paths(paths, roots)
    if backend == "sandbox-exec":
        return ["sandbox-exec", "-p", _seatbelt_profile(
            roots, deny_network=deny_net, deny_paths=deny_meta,
        )]
    if backend == "bwrap":
        return _bwrap_prefix(roots, persist, deny_net=deny_net, deny_paths=deny_meta)
    if backend == "firejail":
        argv = ["firejail", "--quiet", "--noprofile"]
        if deny_net:
            argv.append("--net=none")
        for root in roots:
            if Path(root).exists():
                argv += [f"--whitelist={root}"]
        for denied in deny_meta:
            if Path(denied).exists():
                argv.append(f"--read-only={denied}")
        return argv
    return []


def _under_tmp_mount(path: Path) -> bool:
    """True when *path* lives under a host ``/tmp`` mount (resolved aliases)."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for raw in _TMP_MOUNT_POINTS:
        mount = Path(raw)
        try:
            resolved_mount = mount.resolve()
        except OSError:
            resolved_mount = mount
        for candidate in (resolved_mount, mount):
            try:
                if resolved == candidate or resolved.is_relative_to(candidate):
                    return True
            except ValueError:
                continue
    return False


def _safe_persist_tmp(persist_tmp: Path | str | None) -> str:
    """Resolve scratch without following a leaf symlink into control state."""
    if not persist_tmp:
        return ""
    from omni.config.paths import opens_any_control_store
    from omni.skills_runtime.exec_io import ensure_private_dir

    raw = Path(persist_tmp).expanduser()
    if raw.is_symlink():
        raise SandboxUnavailableError(f"exec scratch is a symlink: {raw}")
    dest = ensure_private_dir(raw)
    if opens_any_control_store(dest):
        raise SandboxUnavailableError(f"exec scratch opens Omni control state: {dest}")
    return str(dest)


def _bwrap_prefix(
    roots: list[str],
    persist_tmp: str,
    *,
    deny_net: bool,
    deny_paths: list[str] | None = None,
) -> list[str]:
    """Read-everywhere / write-listed-roots.

    Scratch is bound at its real path. Guest ``/tmp`` is overlaid with scratch
    only when no other write root lives under ``/tmp`` — otherwise the overlay
    would hide the outbox (the Linux delivery bug).
    """
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev",
            "--proc", "/proc"]
    if deny_net:
        argv.append("--unshare-net")
    other_under_tmp = any(
        root not in _TMP_MOUNT_POINTS and _under_tmp_mount(Path(root))
        for root in roots
        if root != persist_tmp
    )
    for root in roots:
        if root in _TMP_MOUNT_POINTS:
            continue
        if Path(root).exists():
            argv += ["--bind", root, root]
    if persist_tmp and Path(persist_tmp).exists() and persist_tmp not in roots:
        argv += ["--bind", persist_tmp, persist_tmp]
    if persist_tmp and not other_under_tmp:
        argv += ["--bind", persist_tmp, "/tmp"]
    for denied in deny_paths or ():
        if Path(denied).exists():
            argv += ["--ro-bind", denied, denied]
    return argv


__all__ = [
    "SYSTEM_WRITE_ROOTS",
    "SandboxUnavailableError",
    "detect_sandbox",
    "resolve_sandbox",
    "sandbox_prefix",
]

