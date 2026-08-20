"""Folder-exact Persona Root resolution shared by CLI, Web, and overlay.

Scientist persona state belongs to the directory the user opened or launched
in. A parent ``scientist-kg`` must not move that root, and a named project
uses its store folder rather than the process CWD.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _as_path(value: Any) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text).expanduser()


def _usable(value: Any) -> Path | None:
    candidate = _as_path(value)
    if candidate is None:
        return None
    resolved = candidate.resolve()
    if resolved.parent == resolved:  # filesystem root is never a persona folder
        return None
    return resolved


def persona_state_root(paths: Any) -> Path:
    """Return the exact folder that owns ``role.md`` / ``.soulagent/``.

    Path-keyed and in-place workspaces use the launch/open directory
    (``invocation_cwd``). Named projects and the home fallback have no
    user-chosen research folder, so the stable store (``project_dir``) is
    the root. KG location is not consulted.
    """
    workspace_root = getattr(paths, "workspace_root", None)
    if workspace_root is None:
        return Path(paths.project_dir).resolve()
    for candidate in (
        getattr(paths, "invocation_cwd", None),
        workspace_root,
        getattr(paths, "project_dir", None),
    ):
        resolved = _usable(candidate)
        if resolved is not None:
            return resolved
    return Path(paths.project_dir).resolve()


def persona_kg_root(paths: Any) -> Path:
    """Scanner root: ``<persona-root>/scientist-kg`` if present, else Home."""
    project_root = persona_state_root(paths)
    project_kg = project_root / "scientist-kg"
    if project_kg.is_dir():
        return project_kg
    home_kg = getattr(paths, "scientist_kg_dir", None)
    if home_kg is not None:
        return Path(home_kg).resolve()
    home = getattr(paths, "home", None)
    if home is not None:
        return Path(home).expanduser().resolve() / "scientist-kg"
    return Path.home() / ".omni" / "scientist-kg"


def persona_overlay_root(paths: Any, *, channel: str = "cli") -> Path:
    """Directory ReAct reads for the persona overlay.

    Every channel, including WeChat and other IM workspaces, uses the same
    folder-exact root as CLI/Web. ``channel`` is kept for callers.
    """
    _ = channel
    return persona_state_root(paths)


def bind_soulagent_project_root(
    arguments: dict[str, Any],
    paths: Any,
    *,
    channel: str = "cli",
) -> dict[str, Any]:
    """Pin SoulAgent state writes to the folder root when the caller omitted one.

    An explicit ``project_root`` is kept as-is so a host that already chose a
    folder is not overridden. IM uses the same pin as CLI/Web so WeChat on a
    named project (for example ``~/.omni/projects/default``) does not fall
    through to ``Path.cwd()``.
    """
    _ = channel
    if str(arguments.get("project_root") or "").strip():
        return arguments
    bound = dict(arguments)
    bound["project_root"] = str(persona_state_root(paths))
    return bound
