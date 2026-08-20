"""In-app directory listing for the workspace picker.

Starts at the account home (``Path.home()``), not the Omni control store.
``~/.omni`` and anything inside it is hidden and cannot be entered — those
paths are data stores, not project directories the user opens as a cwd.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omni.config.paths import (
    is_control_store_path,
    is_within_home,
    sits_in_any_control_store,
    user_home,
)
from omni.web.protocol import RpcError


def control_store_reason(path: Path) -> str | None:
    """Why ``path`` must not be listed or opened as a workspace, if any."""
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        raise RpcError("invalid_path", f"cannot resolve path: {exc}") from exc
    home = user_home()
    if is_control_store_path(resolved, home) or sits_in_any_control_store(resolved):
        return "cannot use the Omni control store as a workspace"
    if is_within_home(resolved, home):
        return "cannot use a path inside the Omni home as a workspace"
    if resolved.parent == resolved:
        return "cannot open the filesystem root as a workspace"
    return None


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def list_directory(path: str | None = None, *, show_hidden: bool = False) -> dict[str, Any]:
    """Return breadcrumbs + directory entries for the picker modal."""
    root = Path.home()
    target = Path(path).expanduser() if path else root
    try:
        target = target.resolve()
    except OSError as exc:
        raise RpcError("invalid_path", f"cannot resolve path: {exc}") from exc
    if not target.exists():
        raise RpcError("not_found", f"directory does not exist: {target}")
    if not target.is_dir():
        raise RpcError("not_a_directory", f"not a directory: {target}")
    blocked = control_store_reason(target)
    if blocked:
        raise RpcError("control_store", blocked)

    entries: list[dict[str, Any]] = []
    try:
        children = list(target.iterdir())
    except OSError as exc:
        raise RpcError("list_failed", f"cannot list directory: {exc}") from exc
    children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    for child in children:
        name = child.name
        hidden = _is_hidden(name)
        if hidden and not show_hidden:
            continue
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        blocked_child = None
        if is_dir:
            try:
                blocked_child = control_store_reason(child)
            except RpcError:
                continue
        if blocked_child:
            continue
        entries.append(
            {
                "name": name,
                "path": str(child),
                "is_dir": is_dir,
                "is_hidden": hidden,
            }
        )

    breadcrumbs: list[dict[str, str]] = []
    chain = [target, *target.parents]
    chain = list(reversed(chain))
    for part in chain:
        if part.parent == part and breadcrumbs:
            continue
        breadcrumbs.append({"name": part.name or str(part), "path": str(part)})

    parent = str(target.parent) if target.parent != target else None
    if parent and control_store_reason(target.parent):
        parent = None

    return {
        "path": str(target),
        "parent": parent,
        "home": str(root),
        "show_hidden": show_hidden,
        "breadcrumbs": breadcrumbs,
        "entries": entries,
    }
