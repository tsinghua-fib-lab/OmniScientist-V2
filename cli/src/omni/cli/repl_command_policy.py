"""Argument-aware execution policy and display redaction for REPL commands."""

from __future__ import annotations

import re
import shlex
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit


class ReplCommandMode(StrEnum):
    """How a child command shares the terminal with the managed REPL."""

    CAPTURED = "captured"
    INTERACTIVE_TTY = "interactive_tty"
    FOREGROUND_TTY = "foreground_tty"

    @property
    def requires_terminal(self) -> bool:
        """Return whether prompt_toolkit must temporarily release the TTY."""
        return self is not ReplCommandMode.CAPTURED


@dataclass(frozen=True)
class ReplCommandPolicy:
    """Execution contract selected from canonical command tokens."""

    mode: ReplCommandMode
    reason: str = "ordinary command output belongs in the transcript"


_CAPTURED = ReplCommandPolicy(ReplCommandMode.CAPTURED)
_INTERACTIVE = ReplCommandPolicy(
    ReplCommandMode.INTERACTIVE_TTY,
    "command may prompt, page, or launch an editor",
)
_FOREGROUND = ReplCommandPolicy(
    ReplCommandMode.FOREGROUND_TTY,
    "command owns the terminal until stopped",
)
_PAIRING_OUTPUT = ReplCommandPolicy(
    ReplCommandMode.CAPTURED,
    "the one-time pairing code has to stay readable in the transcript",
)

_SENSITIVE_OPTIONS = frozenset({
    "-k",
    "--api-key",
    "--app-secret",
    "--bot-token",
    "--client-secret",
    "--credential",
    "--embedding-api-key",
    "--password",
    "--semantic-scholar-api-key",
    "--secret",
    "--token",
})
_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "password", "secret", "token")
_ENDPOINT_OPTIONS = frozenset({"-u", "--base-url", "--endpoint"})
_REDACTED = "REDACTED"
_RESTART_NOTICE_ENV = "OMNI_REPL_RESTART_NOTICE"
_RESTART_NOTICE = "Previous command completed; Omni restarted to load the new runtime state."


def classify_repl_command(tokens: Sequence[str]) -> ReplCommandPolicy:
    """Classify a slash command by behavior and flags, defaulting to capture.

    Help, dry-run, check, one-shot, and explicit non-interactive variants stay
    inside the app-managed transcript. Only commands that actually read a TTY
    or deliberately remain in the foreground are allowed to suspend the TUI.
    """
    args = [str(token) for token in tokens]
    if not args or _is_help(args):
        return _CAPTURED

    command = args[0]
    subcommand = args[1] if len(args) > 1 and not args[1].startswith("-") else ""

    if command == "init":
        return _CAPTURED if _has_option(args, "--non-interactive", "-y") else _INTERACTIVE

    if command == "channel":
        return _channel_login_policy(args) if subcommand == "login" else _CAPTURED

    if command in {"update", "upgrade"}:
        return (
            _CAPTURED
            if subcommand == "status" or _has_option(args, "--check")
            else _FOREGROUND
        )

    if command == "uninstall":
        if _has_option(args, "--dry-run", "--yes", "-y"):
            return _CAPTURED
        return _INTERACTIVE

    if command == "terminal-setup":
        return _CAPTURED if _terminal_setup_is_noninteractive(args) else _INTERACTIVE

    if command == "terminal" and subcommand == "setup":
        return _CAPTURED if _terminal_setup_is_noninteractive(args) else _INTERACTIVE

    if command == "memory":
        if subcommand == "edit" or (subcommand == "list" and _has_option(args, "--pager")):
            return _INTERACTIVE
        return _CAPTURED

    if command == "skills":
        if subcommand == "list" and _has_option(args, "--pager"):
            return _INTERACTIVE
        if subcommand == "trust" and not _has_option(args, "--yes", "-y"):
            return _INTERACTIVE
        return _CAPTURED

    if command == "task" and subcommand == "watch":
        return _CAPTURED if _has_option(args, "--once") else _FOREGROUND

    if command == "serve":
        if subcommand == "prune":
            return _CAPTURED if _has_option(args, "--yes", "-y") else _INTERACTIVE
        if subcommand in {"", "daemon", "poller"}:
            return _FOREGROUND
        return _CAPTURED

    if command == "mcp" and subcommand == "serve":
        return _FOREGROUND

    if command == "resume" and not subcommand:
        return _INTERACTIVE

    return _CAPTURED


def redact_repl_command(command_line: str) -> str:
    """Return a display-safe command while preserving ordinary input exactly."""
    original = command_line.strip()
    if not original:
        return original
    slash = original.startswith("/")
    source = original[1:] if slash else original
    try:
        tokens = shlex.split(source)
    except ValueError:
        return _redact_unparsed(original)

    changed = _redact_tokens(tokens)
    if not changed:
        return original
    rendered = shlex.join(tokens)
    return f"/{rendered}" if slash else rendered


def command_contains_sensitive_data(command_line: str) -> bool:
    """Return whether the command should be omitted from recallable history."""
    original = command_line.strip()
    return bool(original) and redact_repl_command(original) != original


def remember_restart_notice(environ: MutableMapping[str, str]) -> None:
    """Leave a safe one-shot notice for the process created by ``exec``."""
    environ[_RESTART_NOTICE_ENV] = _RESTART_NOTICE


def consume_restart_notice(environ: MutableMapping[str, str]) -> str:
    """Consume the post-restart notice so later launches do not repeat it."""
    return str(environ.pop(_RESTART_NOTICE_ENV, "") or "").strip()


def _is_help(tokens: Sequence[str]) -> bool:
    return len(tokens) > 1 and (
        tokens[1] == "help" or any(token in {"--help", "-h"} for token in tokens[1:])
    )


_LOGIN_CREDENTIALS = {
    "feishu": ("--app-id", "--app-secret"),
    "dingtalk": ("--client-id", "--client-secret"),
}


def _channel_login_policy(tokens: Sequence[str]) -> ReplCommandPolicy:
    """Suspend the TUI for a login only when the child truly reads the terminal.

    WeChat blocks on a QR scan, so it has to own the raw terminal. Feishu and
    DingTalk return the moment they print the one-time pairing code: running
    those interactively repaints that code away before it can be read.
    """
    if _has_option(tokens, "--non-interactive"):
        return _CAPTURED
    platform = tokens[2] if len(tokens) > 2 and not tokens[2].startswith("-") else ""
    if platform == "wechat":
        return _CAPTURED if _has_option(tokens, "--no-wait") else _INTERACTIVE
    credentials = _LOGIN_CREDENTIALS.get(platform)
    if credentials is None:
        return _INTERACTIVE
    if all(_has_option(tokens, option) for option in credentials):
        return _PAIRING_OUTPUT
    return _INTERACTIVE


def _has_option(tokens: Sequence[str], *names: str) -> bool:
    for token in tokens[1:]:
        if token in names or any(token.startswith(f"{name}=") for name in names if name.startswith("--")):
            return True
    return False


def _terminal_setup_is_noninteractive(tokens: Sequence[str]) -> bool:
    return _has_option(tokens, "--check", "--dry-run", "--yes", "-y")


def _redact_tokens(tokens: list[str]) -> bool:
    changed = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            token in _ENDPOINT_OPTIONS
            and index + 1 < len(tokens)
            and not tokens[index + 1].startswith("-")
        ):
            safe = _redact_endpoint(tokens[index + 1])
            if safe != tokens[index + 1]:
                tokens[index + 1] = safe
                changed = True
            index += 2
            continue
        endpoint_matched = False
        for option in _ENDPOINT_OPTIONS:
            prefix = f"{option}="
            if option.startswith("--") and token.startswith(prefix):
                safe = _redact_endpoint(token.removeprefix(prefix))
                if safe != token.removeprefix(prefix):
                    tokens[index] = prefix + safe
                    changed = True
                endpoint_matched = True
                break
        if endpoint_matched:
            index += 1
            continue
        if token.startswith("-u") and token != "-u":
            separator = "=" if token.startswith("-u=") else ""
            raw_endpoint = token.removeprefix(f"-u{separator}")
            safe = _redact_endpoint(raw_endpoint)
            if safe != raw_endpoint:
                tokens[index] = f"-u{separator}{safe}"
                changed = True
            index += 1
            continue
        if token in _SENSITIVE_OPTIONS:
            if index + 1 < len(tokens):
                tokens[index + 1] = _REDACTED
                changed = True
                index += 2
                continue
        for option in _SENSITIVE_OPTIONS:
            prefix = f"{option}="
            if option.startswith("--") and token.startswith(prefix):
                tokens[index] = prefix + _REDACTED
                changed = True
                break
        else:
            if token.startswith("-k") and token != "-k":
                tokens[index] = "-k" + _REDACTED
                changed = True
        index += 1

    if (
        len(tokens) >= 4
        and tokens[0:2] == ["config", "set"]
        and _sensitive_key(tokens[2])
    ):
        tokens[3] = _REDACTED
        changed = True
    elif (
        len(tokens) >= 4
        and tokens[0:2] == ["config", "set"]
        and _endpoint_key(tokens[2])
    ):
        safe = _redact_endpoint(tokens[3])
        if safe != tokens[3]:
            tokens[3] = safe
            changed = True
    return changed


def _redact_endpoint(value: str) -> str:
    """Redact URL userinfo, query, and fragment before history/transcript use."""
    raw = str(value or "")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return _REDACTED if any(mark in raw for mark in ("@", "?", "#")) else raw
    if "@" in parsed.path and not parsed.netloc:
        return _REDACTED
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        hostname = f"{hostname}:{port}"
    safe = urlunsplit(
        (
            parsed.scheme,
            hostname if parsed.netloc else "",
            parsed.path,
            _REDACTED if parsed.query else "",
            "",
        )
    )
    return safe


def _sensitive_key(value: str) -> bool:
    lowered = value.casefold()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _endpoint_key(value: str) -> bool:
    lowered = value.casefold()
    return lowered.endswith(("base_url", "endpoint"))


def _redact_unparsed(value: str) -> str:
    redacted = value
    option_pattern = "|".join(
        re.escape(option) for option in sorted(_SENSITIVE_OPTIONS, key=len, reverse=True)
    )
    redacted = re.sub(
        rf"(?i)(?P<option>{option_pattern})(?P<separator>=|\s+)(?P<value>\S+)",
        lambda match: f"{match.group('option')}{match.group('separator')}{_REDACTED}",
        redacted,
    )
    endpoint_pattern = "|".join(
        re.escape(option) for option in sorted(_ENDPOINT_OPTIONS, key=len, reverse=True)
    )
    redacted = re.sub(
        rf"(?i)(?P<option>{endpoint_pattern})(?P<separator>=|\s+)(?P<value>\S+)",
        lambda match: (
            f"{match.group('option')}{match.group('separator')}"
            f"{_redact_endpoint(match.group('value'))}"
        ),
        redacted,
    )
    redacted = re.sub(
        r"(?i)(\bconfig\s+set\s+\S*(?:api[_-]?key|secret|token|password)\S*\s+)\S+",
        rf"\1{_REDACTED}",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(\bconfig\s+set\s+\S*(?:base[_-]?url|endpoint)\s+).*$",
        rf"\1{_REDACTED}",
        redacted,
    )
    # When quoting is malformed, a reliable argument boundary no longer exists.
    # Fail closed from a credential/endpoint option onward instead of guessing
    # where an embedded secret ends.
    redacted = re.sub(
        r"(?i)(?<!\w)(?:-k|--api-key|-u|--base-url|--endpoint)(?:=|\s*)\S.*$",
        _REDACTED,
        redacted,
    )
    return redacted


__all__ = [
    "ReplCommandMode",
    "ReplCommandPolicy",
    "classify_repl_command",
    "command_contains_sensitive_data",
    "consume_restart_notice",
    "redact_repl_command",
    "remember_restart_notice",
]
