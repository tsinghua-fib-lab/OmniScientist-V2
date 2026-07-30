"""Workspace registry + read catalog for cross-workspace discovery.

Path-keyed workspaces live under ``~/.omni/workspaces/<slug>-<hash8>``. In-place
``.omni`` projects are scattered across the filesystem. The registry
(``~/.omni/workspaces.json``) records each one so cross-workspace views and the
daemon can find them. It is best-effort metadata: last-writer-wins, never
load-bearing for correctness.

The **catalog** (:func:`iter_catalog_workspaces`) is the read API for those
views: registry records **plus** on-disk path-keyed stores, the home-service
channel anchor, and any named project that already has a ``sessions.sqlite3``.
``/task all`` must keep working after a missing or rewritten registry — a
stale ``workspaces.json`` is not allowed to look like the tasks were deleted.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from .paths import OmniPaths, user_home

_DEFAULT_ANCHOR = "default"
logger = logging.getLogger(__name__)


def registry_path(home: Path | None = None) -> Path:
    return (home or user_home()) / "workspaces.json"


def _load(home: Path | None = None) -> dict[str, dict]:
    path = registry_path(home)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("workspace registry %s is unreadable; catalog will scan the disk", path)
        return {}
    return data if isinstance(data, dict) else {}


def _slug_from_workspace_dir(name: str) -> str:
    """Invert :func:`omni.config.paths.workspace_key` ``<slug>-<hash8>``."""
    if len(name) > 9 and name[-9] == "-":
        return name[:-9] or name
    return name


def _store_record(
    project_dir: Path,
    *,
    kind: str,
    name: str,
    root: str | None = None,
    last_seen: float = 0.0,
) -> dict | None:
    db = project_dir / "sessions.sqlite3"
    if not db.is_file():
        return None
    try:
        mtime = db.stat().st_mtime
    except OSError:
        mtime = 0.0
    return {
        "name": name,
        "root": root,
        "project_dir": str(project_dir),
        "db": str(db),
        "kind": kind,
        "last_seen": last_seen or mtime,
    }


def _disk_workspace_records(home: Path) -> list[dict]:
    """Path-keyed + named stores that already have a ``sessions.sqlite3``."""
    records: list[dict] = []
    workspaces_root = home / "workspaces"
    if workspaces_root.is_dir():
        for child in sorted(workspaces_root.iterdir()):
            if not child.is_dir():
                continue
            rec = _store_record(
                child, kind="path", name=_slug_from_workspace_dir(child.name)
            )
            if rec is not None:
                records.append(rec)
    projects_root = home / "projects"
    if projects_root.is_dir():
        for child in sorted(projects_root.iterdir()):
            if not child.is_dir():
                continue
            rec = _named_record(home, child.name)
            if rec is not None:
                records.append(rec)
    return records


def _kind(paths: OmniPaths) -> str:
    if paths.workspace_root is None:
        return "named"
    if paths.project_dir == paths.workspace_root / ".omni":
        return "in-place"
    return "path"


def register_workspace(paths: OmniPaths) -> None:
    """Upsert the active workspace into the registry (best-effort, atomic).

    An empty or unreadable registry is rebuilt from on-disk stores before the
    upsert so one launch cannot hide every other workspace from ``/task all``.
    """
    home = paths.home
    data = _load(home)
    if not data:
        for rec in _disk_workspace_records(home):
            key = str(Path(str(rec.get("project_dir") or "")).resolve())
            if key:
                data[key] = rec
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
    """Workspaces for cross-workspace list/show — registry ∪ disk ∪ anchor.

    Unlike :func:`list_workspaces` (write-through cache of what has been opened),
    this is the **read** surface for ``task all`` / ``schedule all`` / object
    routing. It always includes:

    * registered workspaces whose DB still exists
    * path-keyed stores under ``home/workspaces/*/sessions.sqlite3``
    * the channel-anchor named project and any other ``projects/<name>`` store

    so a rewritten ``workspaces.json`` or a launch from a different clone cannot
    hide tasks that are still on disk.

    Records with a missing DB path are skipped (never materialise empty stores).
    Order: most-recently-seen first (registry timestamps), then stable name order
    for disk-discovered stores.
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

    for rec in _disk_workspace_records(base):
        key = str(Path(str(rec.get("project_dir") or "")).resolve())
        if key:
            by_dir.setdefault(key, rec)

    records = list(by_dir.values())
    records.sort(
        key=lambda r: (
            -float(r.get("last_seen") or 0.0),
            str(r.get("name") or ""),
        )
    )
    return records


def prior_user_data_summary(home: Path | None = None) -> str | None:
    """Describe existing stores in *home*, or ``None`` when it looks unused.

    Used by first-launch setup so a missing ``config.toml`` is not presented as
    a blank install when tasks or secrets are already on disk. An empty
    ``projects/default`` created by home-service converge does not count.
    """
    base = home or user_home()
    bits: list[str] = []
    config = base / "config.toml"
    secrets = base / "secrets.toml"
    if config.is_file():
        bits.append(f"config {config}")
    try:
        if secrets.is_file() and secrets.stat().st_size > 0:
            bits.append("secrets.toml")
    except OSError:
        pass
    if registry_path(base).is_file():
        bits.append("workspace registry")
    stores: list[str] = []
    workspaces_root = base / "workspaces"
    if workspaces_root.is_dir():
        for child in sorted(workspaces_root.iterdir()):
            if child.is_dir() and (child / "sessions.sqlite3").is_file():
                stores.append(child.name)
    if stores:
        shown = ", ".join(stores[:6])
        extra = "…" if len(stores) > 6 else ""
        bits.append(f"{len(stores)} path workspace(s): {shown}{extra}")
    if not bits:
        return None
    return f"Existing data in {base} — {'; '.join(bits)}."


__all__ = [
    "channel_anchor_name",
    "channel_anchor_project_dir",
    "iter_catalog_workspaces",
    "list_workspaces",
    "prior_user_data_summary",
    "register_workspace",
    "registry_path",
]
