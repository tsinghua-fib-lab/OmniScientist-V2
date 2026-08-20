"""Home-scoped skill catalog for the loopback web UI.

Same files and install helpers as ``omni skills``. The catalog is only the
sources Omni manages by default (packaged built-ins + ``~/.omni/skills``).
Mutations never touch built-ins, project trees, or external tool libraries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omni.config.paths import get_paths
from omni.config.settings import load_settings
from omni.skills_runtime.install import (
    _EXECUTABLE_SUFFIXES,
    _SAFE_SKILL_NAME,
    import_skill,
    looks_like_git_url,
    remove_loaded_skill,
    set_imported_skill_trust,
)
from omni.skills_runtime.manifest import SkillEntry
from omni.skills_runtime.registry import SkillRegistry
from omni.web.protocol import RpcError

SKILL_METHODS = frozenset(
    {
        "skill.list",
        "skill.info",
        "skill.add",
        "skill.trust",
        "skill.untrust",
        "skill.remove",
    }
)

MANAGED_SOURCES = ("builtin", "user_omni")
_WRITE_METHODS = frozenset({"skill.add", "skill.trust", "skill.untrust", "skill.remove"})
_RELOAD_NOTICE = (
    "This omni web process will rediscover skills on the next turn. "
    "A new CLI command reads the files immediately. Restart an open REPL "
    "or `omni serve` to apply it there."
)


def writes_skills(method: str) -> bool:
    return method in _WRITE_METHODS


async def handle_skill(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a ``skill.*`` RPC. Callers drop the web agent cache after writes."""
    if method == "skill.list":
        catalog = _catalog()
        return {"skills": [_list_item(entry, catalog.winners) for entry in catalog.entries]}
    if method == "skill.info":
        return _info(params)
    if method == "skill.add":
        return _add(params)
    if method == "skill.trust":
        return _set_trust(params, trusted=True)
    if method == "skill.untrust":
        return _set_trust(params, trusted=False)
    if method == "skill.remove":
        return _remove(params)
    raise RpcError("unknown_method", f"unknown method: {method}")


def _paths():
    settings = load_settings()
    return settings.paths or get_paths(), settings


def _registry() -> SkillRegistry:
    _home, settings = _paths()
    registry = SkillRegistry(settings, sources=MANAGED_SOURCES)
    registry.build_index()
    return registry


class _Catalog:
    def __init__(self, entries: list[SkillEntry], winners: dict[str, SkillEntry]) -> None:
        self.entries = entries
        self.winners = winners


def _catalog() -> _Catalog:
    registry = _registry()
    winners = {entry.name: entry for entry in registry.list_all() if entry.source in MANAGED_SOURCES}
    entries: list[SkillEntry] = []
    seen: set[tuple[str, str]] = set()
    for entry in (*registry.list_all(), *registry.shadowed_entries()):
        if entry.source not in MANAGED_SOURCES:
            continue
        key = (entry.source, entry.name)
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    entries.sort(key=lambda entry: (0 if entry.source == "builtin" else 1, entry.name))
    return _Catalog(entries, winners)


def _skill_id(entry: SkillEntry) -> str:
    return f"{entry.source}:{entry.name}"


def _list_item(entry: SkillEntry, winners: dict[str, SkillEntry]) -> dict[str, Any]:
    winner = winners.get(entry.name)
    active = winner is not None and winner.source == entry.source
    shadowed_by = "" if active or winner is None else _skill_id(winner)
    user = entry.source == "user_omni"
    return {
        "skill_id": _skill_id(entry),
        "name": entry.name,
        "source": entry.source,
        "description": entry.short_desc(160),
        "kind": entry.kind.value,
        "delivery_mode": entry.delivery_mode.value,
        "version": entry.version,
        "license": entry.license,
        "trusted": bool(entry.trusted),
        "origin": entry.origin,
        "active": active,
        "shadowed": bool(shadowed_by),
        "shadowed_by": shadowed_by,
        "allow_implicit": bool(entry.allow_implicit),
        "can_trust": user and not entry.trusted,
        "can_untrust": user and bool(entry.trusted),
        "can_remove": user,
    }


def _parse_skill_ref(params: dict[str, Any]) -> tuple[str, str]:
    ident = str(params.get("skill_id") or params.get("id") or "").strip()
    name = str(params.get("name") or "").strip()
    source = str(params.get("source") or "").strip()
    if ident:
        if ":" in ident:
            source, _, name = ident.partition(":")
        else:
            name = ident
    return source, name


def _find_entry(catalog: _Catalog, params: dict[str, Any]) -> SkillEntry:
    source, name = _parse_skill_ref(params)
    if not name:
        raise RpcError("invalid_params", "skill_id is required")
    if source and source not in MANAGED_SOURCES:
        raise RpcError("not_found", f"unknown skill: {source}:{name}")
    matches = [
        entry
        for entry in catalog.entries
        if entry.name == name and (not source or entry.source == source)
    ]
    if not matches:
        label = f"{source}:{name}" if source else name
        raise RpcError("not_found", f"unknown skill: {label}")
    if source:
        return matches[0]
    return catalog.winners.get(name) or matches[0]


def _info(params: dict[str, Any]) -> dict[str, Any]:
    catalog = _catalog()
    entry = _find_entry(catalog, params)
    payload = _list_item(entry, catalog.winners)
    dest = entry.path if entry.path is not None and entry.path.is_dir() else None
    payload.update(
        {
            "description": entry.description,
            "when_to_use": entry.when_to_use,
            "body": entry.load_body(),
            "path": str(entry.path) if entry.path is not None else "",
            "allowed_tools": list(entry.allowed_tools),
            "requires_bins": list(entry.requires_bins),
            "requires_env": list(entry.requires_env),
            "capabilities": list(entry.capabilities),
            "executable_files": _executable_files(dest) if dest is not None else [],
        }
    )
    return {"skill": payload}


def _add(params: dict[str, Any]) -> dict[str, Any]:
    raw = str(params.get("path") or params.get("spec") or "").strip()
    if not raw:
        raise RpcError("invalid_params", "skill.add requires path")
    if looks_like_git_url(raw):
        raise RpcError("invalid_params", "git URLs are not supported; choose a local directory")
    path = Path(raw).expanduser()
    if ":" in raw and not path.exists():
        raise RpcError("invalid_params", "only a local skill directory can be added")
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise RpcError("invalid_params", f"cannot resolve path: {exc}") from exc
    if not resolved.exists():
        raise RpcError("not_found", f"path does not exist: {resolved}")
    paths, _settings = _paths()
    paths.user_skills_dir.mkdir(parents=True, exist_ok=True)
    result = import_skill(str(resolved), paths, force=False)
    if result.status.startswith("error"):
        raise RpcError("invalid_params", _status_detail(result.status))
    if result.status.startswith("skipped"):
        raise RpcError("already_exists", f"{result.name} is already imported")
    return {
        "name": result.name,
        "skill_id": f"user_omni:{result.name}",
        "status": result.status,
        "dest": str(result.dest),
        "notice": _RELOAD_NOTICE,
    }


def _set_trust(params: dict[str, Any], *, trusted: bool) -> dict[str, Any]:
    name = _user_skill_name(params)
    paths, _settings = _paths()
    _require_user_skill_dir(paths, name)
    result = set_imported_skill_trust(name, paths, trusted=trusted, allow_missing_license=False)
    if result.status == "absent":
        raise RpcError("not_found", result.message or f"imported skill not found: {name}")
    if result.status in {"refused", "error"}:
        raise RpcError("invalid_params", result.message or result.status)
    return {
        "name": result.name,
        "skill_id": f"user_omni:{result.name}",
        "status": result.status,
        "executable_files": list(result.executable_files),
        "notice": _RELOAD_NOTICE,
    }


def _remove(params: dict[str, Any]) -> dict[str, Any]:
    name = _user_skill_name(params)
    paths, _settings = _paths()
    dest = _require_user_skill_dir(paths, name)
    result = remove_loaded_skill(
        name,
        paths,
        entry=_user_omni_remove_entry(name, dest),
        physical=False,
        force=False,
    )
    if result.status == "absent":
        raise RpcError("not_found", result.message or f"imported skill not found: {name}")
    if result.status == "error":
        raise RpcError("invalid_params", result.message or result.status)
    if result.status != "removed" or result.action != "physical_delete":
        raise RpcError("forbidden", result.message or "only imported user skills can be removed")
    return {
        "name": result.name,
        "skill_id": f"user_omni:{result.name}",
        "status": result.status,
        "action": result.action,
        "tombstone": str(result.tombstone) if result.tombstone is not None else "",
        "notice": f"{result.message} {_RELOAD_NOTICE}".strip(),
    }


def _user_skill_name(params: dict[str, Any]) -> str:
    skill_id = str(params.get("skill_id") or params.get("id") or "").strip()
    name = str(params.get("name") or "").strip()
    source = str(params.get("source") or "").strip()
    if skill_id:
        if ":" in skill_id:
            source, _, name = skill_id.partition(":")
        else:
            name = skill_id
    if source and source != "user_omni":
        raise RpcError("forbidden", "only imported user skills can be changed")
    if not name or not _SAFE_SKILL_NAME.fullmatch(name):
        raise RpcError("invalid_params", "invalid skill name")
    return name


def _user_omni_remove_entry(name: str, dest: Path) -> SkillEntry:
    """Bind remove to the user library path. Never pass a builtin/project entry."""
    entry = _registry().get_scoped("user_omni", name)
    if entry is not None and entry.source == "user_omni" and _same_user_skill_path(entry.path, dest):
        return entry
    return SkillEntry(name=name, description="", source="user_omni", path=dest)


def _same_user_skill_path(entry_path: Path | None, dest: Path) -> bool:
    if entry_path is None:
        return False
    try:
        return entry_path.expanduser().resolve() == dest.expanduser().resolve()
    except OSError:
        return False


def _require_user_skill_dir(paths: Any, name: str) -> Path:
    """Return ``~/.omni/skills/<name>`` only when it is a direct, contained child."""
    root = Path(paths.user_skills_dir).expanduser().resolve()
    dest = Path(paths.user_skills_dir).expanduser() / name
    if dest.name != name:
        raise RpcError("invalid_params", "invalid skill name")
    try:
        parent = dest.parent.resolve()
    except OSError as exc:
        raise RpcError("invalid_params", f"cannot resolve skill path: {exc}") from exc
    if parent != root:
        raise RpcError("forbidden", "skill path is not inside the user skills directory")
    if dest.exists() or dest.is_symlink():
        try:
            resolved = dest.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise RpcError("forbidden", "skill path escapes the user skills directory") from exc
    return dest


def _executable_files(dest: Path) -> list[str]:
    files: list[str] = []
    try:
        children = sorted(dest.rglob("*"))
    except OSError:
        return []
    for path in children:
        if path.is_file() and path.suffix.lower() in _EXECUTABLE_SUFFIXES:
            files.append(str(path.relative_to(dest)))
    return files


def _status_detail(status: str) -> str:
    return status.removeprefix("error:").strip() or status
