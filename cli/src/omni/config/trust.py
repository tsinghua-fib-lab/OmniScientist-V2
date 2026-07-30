"""Per-directory workspace trust — a global consent gate for launching omni.

Mirrors Claude Code / VS Code "workspace trust": before omni writes generated
files into a directory or applies that directory's repo-local config, the user
vouches for it once. The decision is keyed on the enclosing VCS root (falling
back to the launch directory, exactly how workspaces are keyed) and persisted
OUTSIDE any repository in ``~/.omni/trust.json`` — a cloned repo must never be
able to declare itself trusted. Trust inherits downward: trusting a directory
trusts everything beneath it.

This module is pure state + queries; the interactive prompt and the launch-time
decision (named projects, home dir, in-place adoption, CLI flags) live in the
CLI layer (``omni.cli.state``).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .paths import find_vcs_root, user_home


def trust_store_path(home: Path | None = None) -> Path:
    """The global trust ledger (never inside a repository)."""
    return (home or user_home()) / "trust.json"


def trust_key(cwd: Path | None = None) -> Path:
    """The directory a trust decision is keyed on: the enclosing VCS root, else
    the resolved launch directory (matching ``config.paths`` workspace keying)."""
    base = cwd or Path.cwd()
    return (find_vcs_root(base) or base).resolve()


def _covers(parent: Path, child: Path) -> bool:
    """True when ``parent`` trusts ``child`` — the same dir or an ancestor.

    Trust inherits downward only: trusting a parent trusts its children, but
    trusting a child never retroactively trusts the parent.
    """
    try:
        parent = parent.expanduser().resolve()
        child = child.expanduser().resolve()
    except OSError:
        return False
    return child == parent or parent in child.parents


def _load(home: Path | None = None) -> dict[str, dict]:
    path = trust_store_path(home)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict], home: Path) -> None:
    path = trust_store_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # trust ledger is best-effort; a write failure fails closed on read


def is_trusted(
    cwd: Path | None = None,
    *,
    home: Path | None = None,
    allow: list[str] | None = None,
) -> bool:
    """Whether the launch directory is trusted, via the config allowlist or the
    persisted ledger (both inherit downward)."""
    key = trust_key(cwd)
    for entry in allow or []:
        if entry and _covers(Path(str(entry)), key):
            return True
    for raw, rec in _load(home).items():
        if isinstance(rec, dict) and rec.get("trusted") and _covers(Path(raw), key):
            return True
    return False


def set_trusted(cwd: Path | None = None, *, home: Path | None = None) -> Path:
    """Persist trust for the launch directory's key; returns the stored key."""
    key = trust_key(cwd)
    resolved_home = home or user_home()
    data = _load(resolved_home)
    data[str(key)] = {"trusted": True, "ts": time.time()}
    _save(data, resolved_home)
    return key


def revoke(path: str | Path, *, home: Path | None = None) -> bool:
    """Remove a trusted entry (matched by exact key or resolved path)."""
    resolved_home = home or user_home()
    data = _load(resolved_home)
    try:
        target = str(Path(str(path)).expanduser().resolve())
    except OSError:
        target = str(path)
    removed = False
    for raw in list(data.keys()):
        try:
            same = raw == target or str(Path(raw).expanduser().resolve()) == target
        except OSError:
            same = raw == target
        if same:
            data.pop(raw, None)
            removed = True
    if removed:
        _save(data, resolved_home)
    return removed


def list_trusted(home: Path | None = None) -> list[dict]:
    """All trusted directories, most-recently-trusted first."""
    out = [
        {"path": raw, "ts": float(rec.get("ts", 0.0))}
        for raw, rec in _load(home).items()
        if isinstance(rec, dict) and rec.get("trusted")
    ]
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out
