"""Host-owned control stores are not a bash write root.

``write_file`` already refuses frozen Omni stores. Bash must name the same
fact before the OS seatbelt returns ``readonly database``. Codex never puts
``CODEX_HOME`` in ``WorkspaceWrite``; this is that policy as a tool
observation, not a SQLite special case.
"""

from __future__ import annotations

import re
from pathlib import Path

from omni.config.paths import sits_in_any_control_store, user_home

_QUOTED = re.compile(r"'([^']+)'|\"([^\"]+)\"")
_BARE_PATH = re.compile(
    r"(?:~|\$HOME|\$OMNI_HOME|\$\{HOME\}|\$\{OMNI_HOME\}|/)[^\s;|&<>()'\"`]+"
)
_WRITE_WORDS = re.compile(
    r"\b(touch|rm|rmdir|mv|cp|mkdir|chmod|chown|tee|unlink|truncate|install)\b",
    re.IGNORECASE,
)
_INTERPRETER = re.compile(
    r"\b(python3?(?:\.\d+)?|sqlite3|ipython)\b",
    re.IGNORECASE,
)
_SQLITE_READ = re.compile(r"\b(select|pragma|explain|\.schema|\.tables)\b", re.IGNORECASE)


def command_writes_frozen_control_store(command: str) -> Path | None:
    """Return the control-store path a mutating command would write, if any."""
    text = str(command or "").strip()
    if not text:
        return None
    hits = [path for raw in _path_strings(text) if (path := _resolve_candidate(raw)) is not None]
    store_hits = [path for path in hits if sits_in_any_control_store(path)]
    if not store_hits:
        return None
    if _WRITE_WORDS.search(text) or _has_write_redirect(text):
        return store_hits[0]
    interpreter = _INTERPRETER.search(text)
    if interpreter is None:
        return None
    name = interpreter.group(1).lower()
    if name.startswith("sqlite") and _SQLITE_READ.search(text) and not _WRITE_WORDS.search(text):
        return None
    return store_hits[0]


def control_store_write_observation(path: Path) -> str:
    return (
        "ERROR: write denied to the Omni control store. "
        f"The host owns {path}; use `omni task show` or `omni task list`. "
        "Do not settle a task by editing the ledger."
    )


def _path_strings(command: str) -> list[str]:
    found: list[str] = []
    for match in _QUOTED.finditer(command):
        token = match.group(1) or match.group(2) or ""
        if token:
            found.append(token)
    for match in _BARE_PATH.finditer(command):
        token = match.group(0).rstrip(".,;:")
        if token:
            found.append(token)
    return found


def _resolve_candidate(raw: str) -> Path | None:
    text = str(raw or "").strip()
    if not text or "\x00" in text:
        return None
    home = str(Path.home())
    omni_home = str(user_home())
    replacements = (
        ("${HOME}", home),
        ("$HOME", home),
        ("${OMNI_HOME}", omni_home),
        ("$OMNI_HOME", omni_home),
    )
    for needle, value in replacements:
        if text.startswith(needle):
            text = value + text[len(needle) :]
            break
    try:
        path = Path(text).expanduser()
    except (OSError, ValueError, RuntimeError):
        return None
    if not path.is_absolute() and not str(raw).startswith(("~", "$")):
        return None
    try:
        return path.resolve()
    except OSError:
        return path


def _has_write_redirect(command: str) -> bool:
    stripped = re.sub(r"2>&1", " ", command)
    return bool(re.search(r"(?:^|[\s])>>?(?:\s|/|~|\$)", stripped))


__all__ = [
    "command_writes_frozen_control_store",
    "control_store_write_observation",
]
