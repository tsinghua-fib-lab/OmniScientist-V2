"""Terminal capability detection, keyboard negotiation, and safe tmux setup."""

from __future__ import annotations

import os
import platform as platform_module
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MODIFY_OTHER_KEYS_ENABLE = "\x1b[>4;2m"
MODIFY_OTHER_KEYS_DISABLE = "\x1b[>4m"

_MANAGED_START = "# >>> OmniScientist terminal keyboard support >>>"
_MANAGED_END = "# <<< OmniScientist terminal keyboard support <<<"
_MANAGED_BLOCK = "\n".join(
    (
        _MANAGED_START,
        "set -g allow-passthrough on",
        "set -s extended-keys on",
        "set -as terminal-features 'xterm*:extkeys'",
        _MANAGED_END,
    )
)

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class TmuxStatus:
    active: bool
    available: bool
    version: str = ""
    extended_keys: str = ""
    extended_keys_format: str = ""
    allow_passthrough: str = ""
    terminal_features: str = ""


@dataclass(frozen=True)
class TerminalReport:
    platform: str
    host_terminal: str
    term: str
    interactive: bool
    tmux: TmuxStatus
    shift_enter_ready: bool
    issues: tuple[str, ...]
    repair_command: str
    fallback_shortcut: str = "Ctrl+J"


@dataclass(frozen=True)
class TmuxSetupPlan:
    path: Path
    original: str
    updated: str
    existed: bool
    changed: bool


@dataclass(frozen=True)
class TmuxSetupResult:
    path: Path
    changed: bool
    backup_path: Path | None = None


def _default_command_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


def _command_value(runner: CommandRunner, command: list[str]) -> str:
    result = runner(command)
    return result.stdout.strip() if result.returncode == 0 else ""


def _platform_label(value: str) -> str:
    if value.startswith("win"):
        return "windows"
    if value == "darwin":
        return "macos"
    if value.startswith("linux"):
        return "linux"
    return value or platform_module.system().lower() or "unknown"


def _host_terminal(environ: Mapping[str, str]) -> str:
    if environ.get("WT_SESSION"):
        return "Windows Terminal"
    if environ.get("GHOSTTY_RESOURCES_DIR"):
        return "Ghostty"
    if environ.get("KITTY_WINDOW_ID"):
        return "kitty"
    if environ.get("WEZTERM_PANE"):
        return "WezTerm"
    if environ.get("VSCODE_PID"):
        return environ.get("TERM_PROGRAM", "VS Code")
    return environ.get("TERM_PROGRAM") or environ.get("LC_TERMINAL") or environ.get("TERM", "unknown")


def inspect_terminal(
    *,
    environ: Mapping[str, str] | None = None,
    interactive: bool | None = None,
    platform_name: str | None = None,
    command_runner: CommandRunner = _default_command_runner,
) -> TerminalReport:
    """Inspect the host and the active tmux server without changing either."""
    env = os.environ if environ is None else environ
    if interactive is None:
        try:
            interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())
        except (AttributeError, OSError, ValueError):
            interactive = False
    tmux_active = bool(env.get("TMUX"))
    tmux_available = bool(shutil.which("tmux")) or tmux_active
    if tmux_active:
        tmux = TmuxStatus(
            active=True,
            available=tmux_available,
            version=_command_value(command_runner, ["tmux", "-V"]),
            extended_keys=_command_value(
                command_runner, ["tmux", "show-options", "-gqv", "extended-keys"]
            ),
            extended_keys_format=_command_value(
                command_runner, ["tmux", "show-options", "-sqv", "extended-keys-format"]
            ),
            allow_passthrough=_command_value(
                command_runner, ["tmux", "show-options", "-gqv", "allow-passthrough"]
            ),
            terminal_features=_command_value(
                command_runner, ["tmux", "show-options", "-gqv", "terminal-features"]
            ),
        )
    else:
        tmux = TmuxStatus(active=False, available=tmux_available)

    issues: list[str] = []
    term = env.get("TERM", "")
    if not interactive:
        issues.append("stdin/stdout are not interactive TTYs")
    if term.lower() == "dumb":
        issues.append("TERM=dumb cannot report modified keys")
    if tmux.active:
        if tmux.extended_keys not in {"on", "always"}:
            issues.append(f"tmux extended-keys={tmux.extended_keys or 'unknown'}")
        if tmux.allow_passthrough not in {"on", "all"}:
            issues.append(f"tmux allow-passthrough={tmux.allow_passthrough or 'unknown'}")
        if "extkeys" not in tmux.terminal_features:
            issues.append("tmux terminal-features does not advertise extkeys")

    ready = bool(interactive and term.lower() != "dumb" and not issues)
    repair = "omni terminal status" if ready else "omni terminal setup"
    return TerminalReport(
        platform=_platform_label(platform_name or sys.platform),
        host_terminal=_host_terminal(env),
        term=term or "unknown",
        interactive=bool(interactive),
        tmux=tmux,
        shift_enter_ready=ready,
        issues=tuple(issues),
        repair_command=repair,
    )


class TerminalKeyboardProtocol:
    """Request xterm modified-key reporting while Omni owns the terminal."""

    def __init__(self, output: object, *, enabled: bool | None = None) -> None:
        self._output = output
        self._enabled = _interactive_vt_terminal() if enabled is None else bool(enabled)
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        if not self._enabled or self._active:
            return
        if self._write(MODIFY_OTHER_KEYS_ENABLE):
            self._active = True

    def stop(self) -> None:
        if not self._active:
            return
        self._write(MODIFY_OTHER_KEYS_DISABLE)
        self._active = False

    def __enter__(self) -> TerminalKeyboardProtocol:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _write(self, value: str) -> bool:
        writer = getattr(self._output, "write_raw", None)
        flusher = getattr(self._output, "flush", None)
        if not callable(writer) or not callable(flusher):
            return False
        try:
            writer(value)
            flusher()
        except (OSError, RuntimeError, ValueError):
            return False
        return True


def _interactive_vt_terminal() -> bool:
    try:
        interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, OSError, ValueError):
        return False
    if not interactive or os.environ.get("TERM", "").lower() == "dumb":
        return False
    if os.name != "nt":
        return True
    return bool(
        os.environ.get("WT_SESSION")
        or os.environ.get("ANSICON")
        or os.environ.get("ConEmuANSI") == "ON"
        or os.environ.get("TERM_PROGRAM")
    )


def plan_tmux_setup(path: Path | None = None) -> TmuxSetupPlan:
    """Build an idempotent managed-block edit without touching the filesystem."""
    target = (path or Path.home() / ".tmux.conf").expanduser()
    existed = target.exists()
    original = target.read_text(encoding="utf-8") if existed else ""
    pattern = re.compile(
        rf"{re.escape(_MANAGED_START)}.*?{re.escape(_MANAGED_END)}",
        flags=re.DOTALL,
    )
    if pattern.search(original):
        updated = pattern.sub(_MANAGED_BLOCK, original)
    else:
        prefix = original
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix:
            prefix += "\n"
        updated = prefix + _MANAGED_BLOCK + "\n"
    return TmuxSetupPlan(
        path=target,
        original=original,
        updated=updated,
        existed=existed,
        changed=updated != original,
    )


def apply_tmux_setup(plan: TmuxSetupPlan) -> TmuxSetupResult:
    """Atomically apply a confirmed setup plan and preserve the prior file."""
    if not plan.changed:
        return TmuxSetupResult(path=plan.path, changed=False)
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if plan.existed:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        backup = plan.path.with_name(f"{plan.path.name}.omni-backup-{stamp}")
        shutil.copy2(plan.path, backup)
    temporary = plan.path.with_name(f".{plan.path.name}.omni-{os.getpid()}.tmp")
    try:
        temporary.write_text(plan.updated, encoding="utf-8")
        if plan.existed:
            temporary.chmod(stat.S_IMODE(plan.path.stat().st_mode))
        else:
            temporary.chmod(0o600)
        temporary.replace(plan.path)
    finally:
        temporary.unlink(missing_ok=True)
    return TmuxSetupResult(path=plan.path, changed=True, backup_path=backup)


def reload_tmux_config(
    path: Path,
    *,
    command_runner: CommandRunner = _default_command_runner,
) -> tuple[bool, str]:
    """Reload a confirmed config into the current tmux server, if any."""
    if not os.environ.get("TMUX"):
        return False, "not currently inside tmux; settings apply to the next tmux server"
    result = command_runner(["tmux", "source-file", str(path)])
    if result.returncode == 0:
        return True, "current tmux server reloaded"
    detail = (result.stderr or result.stdout).strip() or "tmux source-file failed"
    return False, detail
