"""Persistent Web-only visibility for the workspace sidebar.

The cross-workspace catalog is a correctness surface for ``/task all`` and the
home service.  Hiding a row in the Web UI therefore lives in a separate file;
it never removes a registry entry or touches the workspace store/source tree.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Iterable
from pathlib import Path

from omni.config.paths import user_home

_VERSION = 1
_LOCK = threading.RLock()


def visibility_path(home: Path | None = None) -> Path:
    """Return the owner-local Web workspace visibility file."""
    return (home or user_home()) / "web-workspace-visibility.json"


def canonical_project_dir(value: str | Path) -> str:
    """Normalize a project-store identity without requiring it to exist."""
    return str(Path(value).expanduser().resolve(strict=False))


def hidden_project_dirs(home: Path | None = None) -> set[str]:
    """Load hidden canonical project directories, tolerating bad state."""
    path = visibility_path(home)
    with _LOCK:
        if not path.is_file():
            return set()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
    values = payload.get("hidden_project_dirs", []) if isinstance(payload, dict) else []
    if not isinstance(values, list):
        return set()
    return {
        canonical_project_dir(value)
        for value in values
        if isinstance(value, str) and value.strip()
    }


def set_hidden_project_dirs(
    project_dirs: Iterable[str | Path],
    *,
    hidden: bool,
    home: Path | None = None,
) -> set[str]:
    """Atomically add or remove Web-hidden workspace identities."""
    base = home or user_home()
    path = visibility_path(base)
    requested = {
        canonical_project_dir(value)
        for value in project_dirs
        if str(value).strip()
    }
    with _LOCK:
        current = hidden_project_dirs(base)
        updated = current | requested if hidden else current - requested
        if updated != current:
            _write(path, updated)
    return updated


def _write(path: Path, values: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": _VERSION,
                    "hidden_project_dirs": sorted(values),
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "canonical_project_dir",
    "hidden_project_dirs",
    "set_hidden_project_dirs",
    "visibility_path",
]
