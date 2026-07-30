"""Workspace registry + read catalog for cross-workspace discovery.

Path-keyed workspaces live under ``~/.omni/workspaces/<slug>-<hash8>`` and could
be enumerated by globbing, but in-place ``.omni`` projects are scattered across
the filesystem. The registry (``~/.omni/workspaces.json``) records each one so
cross-workspace views and the daemon can find them. It is best-effort metadata:
last-writer-wins, rebuilt on the next agent start, never load-bearing for
correctness.

The **catalog** (:func:`iter_catalog_workspaces`) is the read API for those
views: registry records **plus** the home-service channel anchor and any named
project that already has a ``sessions.sqlite3``. That way IM work on
``projects/default`` stays visible even when nothing has called
:func:`register_workspace` for the anchor yet.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .paths import OmniPaths, user_home

_DEFAULT_ANCHOR = "default"


def registry_path(home: Path | None = None) -> Path:
    return (home or user_home()) / "workspaces.json"


def _load(home: Path | None = None) -> dict[str, dict]:
    path = registry_path(home)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _kind(paths: OmniPaths) -> str:
    if paths.workspace_root is None:
        return "named"
    if paths.project_dir == paths.workspace_root / ".omni":
        return "in-place"
    return "path"


def register_workspace(paths: OmniPaths) -> None:
    """Upsert the active workspace into the registry (best-effort, atomic)."""
    home = paths.home
    data = _load(home)
    data[str(paths.project_dir)] = {
        "name": paths.project_name,
        "root": str(paths.workspace_root) if paths.workspace_root else None,
        "project_dir": str(paths.project_dir),
        "db": str(paths.project_db),
        "kind": _kind(paths),
        "last_seen": time.time(),
    }
    path = registry_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # registry is advisory; never block the agent on it


def list_workspaces(home: Path | None = None) -> list[dict]:
    """All known workspaces, most-recently-seen first; prunes dead DB paths."""
    records = [r for r in _load(home).values() if isinstance(r, dict) and r.get("db")]
    records.sort(key=lambda r: r.get("last_seen", 0.0), reverse=True)
    return records


def channel_anchor_name(home: Path | None = None) -> str:
    """Project name of the home-service channel anchor (default ``default``).

    Reads ``service/settings.json`` directly so the catalog does not import the
    full service-control stack. Missing/invalid files fall back to ``default``.
    """
    base = home or user_home()
    path = base / "service" / "settings.json"
    if not path.is_file():
        return _DEFAULT_ANCHOR
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _DEFAULT_ANCHOR
    if not isinstance(data, dict):
        return _DEFAULT_ANCHOR
    raw = str(data.get("channel_anchor") or "").strip()
    if not raw:
        return _DEFAULT_ANCHOR
    # Historical rows store a bare project name ("default"); tolerate a full
    # project_dir by taking the final path segment.
    name = Path(raw).name if ("/" in raw or "\\" in raw) else raw
    return name or _DEFAULT_ANCHOR


def channel_anchor_project_dir(home: Path | None = None) -> Path:
    """``~/.omni/projects/<channel_anchor>`` — may or may not exist on disk yet."""
    base = home or user_home()
    return base / "projects" / channel_anchor_name(base)


def _channel_anchor_name(home: Path) -> str:
    """Backward-compatible private alias for :func:`channel_anchor_name`."""
    return channel_anchor_name(home)


def _named_record(home: Path, name: str, *, last_seen: float = 0.0) -> dict | None:
    """Build a catalog record for ``home/projects/<name>`` when its DB exists."""
    name = (name or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    project_dir = home / "projects" / name
    db = project_dir / "sessions.sqlite3"
    if not db.is_file():
        return None
    return {
        "name": name,
        "root": None,
        "project_dir": str(project_dir),
        "db": str(db),
        "kind": "named",
        "last_seen": last_seen,
    }


def iter_catalog_workspaces(home: Path | None = None) -> list[dict]:
    """Workspaces for cross-workspace list/show — registry ∪ anchor ∪ named DBs.

    Unlike :func:`list_workspaces` (write-through cache of what has been opened),
    this is the **read** surface for ``task all`` / ``schedule all`` / object
    routing. It always includes the channel-anchor named project when its
    ``sessions.sqlite3`` exists, and any other ``projects/<name>`` store already
    on disk, so IM work on ``default`` cannot stay invisible merely because the
    interactive CLI never called :func:`register_workspace` for it.

    Records with a missing DB path are skipped (never materialise empty stores).
    Order: most-recently-seen first (registry timestamps), then stable name order
    for disk-discovered named projects.
    """
    base = home or user_home()
    by_dir: dict[str, dict] = {}

    for rec in list_workspaces(base):
        if not isinstance(rec, dict):
            continue
        db_path = Path(str(rec.get("db") or ""))
        if not db_path.is_file():
            continue
        key = str(Path(str(rec.get("project_dir") or db_path.parent)).resolve())
        by_dir[key] = dict(rec)

    # Channel anchor first among disk discoveries so IM work surfaces even when
    # the registry is empty or stale.
    anchor = _named_record(base, _channel_anchor_name(base), last_seen=time.time())
    if anchor is not None:
        key = str(Path(anchor["project_dir"]).resolve())
        by_dir.setdefault(key, anchor)

    projects_root = base / "projects"
    if projects_root.is_dir():
        for child in sorted(projects_root.iterdir()):
            if not child.is_dir():
                continue
            rec = _named_record(base, child.name)
            if rec is None:
                continue
            key = str(Path(rec["project_dir"]).resolve())
            by_dir.setdefault(key, rec)

    records = list(by_dir.values())
    records.sort(
        key=lambda r: (
            -float(r.get("last_seen") or 0.0),
            str(r.get("name") or ""),
        )
    )
    return records


__all__ = [
    "channel_anchor_name",
    "channel_anchor_project_dir",
    "iter_catalog_workspaces",
    "list_workspaces",
    "register_workspace",
    "registry_path",
]
