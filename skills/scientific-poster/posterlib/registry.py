"""Read-only resolution for three-layer poster resource libraries."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import (
    RESOURCE_KINDS,
    ContractError,
    load_component_package,
    load_layout_policy_package,
    load_resource_identity,
)

MAX_REGISTRY_BYTES = 1024 * 1024
_COLLECTIONS = {
    "component": "components",
    "layout-policy": "layout-policies",
}
_LAYER_ORDER = {"user": 0, "builtin": 1, "project": 2}
_STATUS = {"builtin": "builtin", "user": "approved", "project": "candidate"}
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RegistryRoots:
    """Independent built-in, project-candidate, and user-approved roots."""

    builtin: Path
    project: Path
    user: Path


@dataclass(frozen=True)
class ResourceRecord:
    """One hash-verified immutable package visible in a registry layer."""

    kind: str
    resource_id: str
    version: str
    content_sha256: str
    layer: str
    path: Path
    status: str
    semantic_roles: tuple[str, ...]
    page_modes: tuple[str, ...]
    reason: Mapping[str, object]


@dataclass(frozen=True)
class RegistryIndex:
    """Immutable snapshot of all visible resource indexes."""

    roots: RegistryRoots
    records: tuple[ResourceRecord, ...]
    defaults: Mapping[tuple[str, str, str], str]


class RegistryError(ValueError):
    """Base class for resource index failures."""


class RegistrySecurityError(RegistryError):
    """A registry path escapes or aliases its declared layer root."""


class RegistryConflict(RegistryError):
    """One immutable identity maps to different package bytes."""


def resolve_registry_roots(
    *,
    cwd: str | Path,
    skill_dir: str | Path,
    project_library: str | Path | None = None,
    user_library: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> RegistryRoots:
    """Resolve all layer roots without creating or mutating them."""

    working = Path(cwd).expanduser().absolute()
    environ = os.environ if environment is None else environment
    home_path = Path.home() if home is None else Path(home).expanduser()
    project_value = (
        Path(project_library)
        if project_library is not None
        else working / ".scientific-poster"
    )
    configured_user = environ.get("SCIENTIFIC_POSTER_HOME")
    if user_library is not None:
        user_value = Path(user_library)
    elif configured_user:
        user_value = Path(configured_user)
    else:
        user_value = home_path / ".scientific-poster"
    if (
        project_library is None
        and user_library is None
        and not configured_user
        and _canonical(project_value) == _canonical(user_value)
    ):
        project_value = working / ".scientific-poster-project"
    return RegistryRoots(
        builtin=_absolute(skill_dir, relative_to=working),
        project=_absolute(project_value, relative_to=working),
        user=_absolute(user_value, relative_to=working),
    )


def validate_resource_index(value: object) -> dict[str, Any]:
    """Validate one closed machine-owned resource index."""

    expected = {"schema", "layer", "kind", "defaults", "records"}
    if not isinstance(value, dict) or set(value) != expected:
        raise RegistryError("resource index fields are invalid")
    if value["schema"] != "scientific-poster.resource-index.v1":
        raise RegistryError("unsupported resource index schema")
    layer = value["layer"]
    kind = value["kind"]
    if layer not in _STATUS:
        raise RegistryError("invalid resource index layer")
    if kind not in RESOURCE_KINDS:
        raise RegistryError("invalid resource index kind")
    defaults = value["defaults"]
    records = value["records"]
    if not isinstance(defaults, dict) or not isinstance(records, list):
        raise RegistryError("resource index defaults and records must be containers")
    normalized_records: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    record_fields = {
        "resource_id",
        "version",
        "content_sha256",
        "relative_path",
        "status",
        "semantic_roles",
        "page_modes",
    }
    for record in records:
        if not isinstance(record, dict) or set(record) != record_fields:
            raise RegistryError("resource index record fields are invalid")
        resource_id = record["resource_id"]
        version = record["version"]
        if not isinstance(resource_id, str) or _ID_RE.fullmatch(resource_id) is None:
            raise RegistryError("invalid resource id")
        if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
            raise RegistryError("invalid resource version")
        if not isinstance(record["content_sha256"], str) or _HASH_RE.fullmatch(
            record["content_sha256"]
        ) is None:
            raise RegistryError("invalid resource hash")
        if not isinstance(record["relative_path"], str):
            raise RegistryError("invalid resource relative path")
        if record["status"] != _STATUS[layer]:
            raise RegistryError("resource status does not match its layer")
        semantic_roles = _string_list(record["semantic_roles"], "semantic_roles")
        page_modes = _string_list(record["page_modes"], "page_modes")
        if set(page_modes) - {"portrait", "landscape"}:
            raise RegistryError("invalid page mode")
        identity = (resource_id, version)
        if identity in identities:
            raise RegistryConflict(f"duplicate resource identity: {resource_id}@{version}")
        identities.add(identity)
        normalized_records.append(
            {
                **record,
                "semantic_roles": semantic_roles,
                "page_modes": page_modes,
            }
        )
    normalized_defaults: dict[str, str] = {}
    for resource_id, version in defaults.items():
        if (resource_id, version) not in identities:
            raise RegistryError(f"default is not registered: {resource_id}@{version}")
        normalized_defaults[str(resource_id)] = str(version)
    return {
        "schema": value["schema"],
        "layer": layer,
        "kind": kind,
        "defaults": normalized_defaults,
        "records": normalized_records,
    }


def load_registry(roots: RegistryRoots) -> RegistryIndex:
    """Load and hash-verify every declared package in the three layers."""

    checked = _validated_roots(roots)
    records: list[ResourceRecord] = []
    defaults: dict[tuple[str, str, str], str] = {}
    hashes: dict[tuple[str, str, str], str] = {}
    for layer, root in (
        ("builtin", checked.builtin),
        ("user", checked.user),
        ("project", checked.project),
    ):
        if not root.exists():
            continue
        if not root.is_dir() or root.is_symlink():
            raise RegistrySecurityError(f"registry root must be a regular directory: {root}")
        for kind in RESOURCE_KINDS:
            registry_path = root / _COLLECTIONS[kind] / "registry.json"
            if not registry_path.exists():
                continue
            safe_registry = _safe_path(root, registry_path.relative_to(root).as_posix())
            value = _read_index(safe_registry)
            if value["layer"] != layer or value["kind"] != kind:
                raise RegistryError(f"registry identity mismatch: {safe_registry}")
            for raw_record in value["records"]:
                record = _record_from_value(
                    root=root,
                    layer=layer,
                    kind=kind,
                    registry_path=safe_registry,
                    value=raw_record,
                )
                immutable = (kind, record.resource_id, record.version)
                previous = hashes.get(immutable)
                if previous is not None and previous != record.content_sha256:
                    raise RegistryConflict(
                        f"same identity has different bytes: {record.resource_id}@{record.version}"
                    )
                hashes[immutable] = record.content_sha256
                records.append(record)
            for resource_id, version in value["defaults"].items():
                defaults[(layer, kind, resource_id)] = version
    records.sort(
        key=lambda item: (
            _LAYER_ORDER[item.layer],
            item.kind,
            item.resource_id,
            item.version,
        )
    )
    return RegistryIndex(
        roots=checked,
        records=tuple(records),
        defaults=MappingProxyType(defaults),
    )


def query_resources(
    roots: RegistryRoots,
    *,
    kind: str,
    semantic_roles: set[str] | tuple[str, ...] | list[str] | None = None,
    page_mode: str | None = None,
    allow_candidates: bool = False,
) -> list[ResourceRecord]:
    """Return reviewed resources matching optional semantic guidance."""

    if kind not in RESOURCE_KINDS:
        raise ValueError(f"unsupported resource kind: {kind}")
    requested_roles = set(semantic_roles or ())
    if not all(isinstance(item, str) and item for item in requested_roles):
        raise ValueError("semantic_roles must contain non-empty strings")
    if page_mode is not None and page_mode not in {"portrait", "landscape"}:
        raise ValueError("page_mode must be portrait or landscape")
    index = load_registry(roots)
    records = []
    for record in _active_default_records(index, kind=kind, allow_candidates=allow_candidates):
        if requested_roles and not requested_roles.issubset(set(record.semantic_roles)):
            continue
        if page_mode and record.page_modes and page_mode not in record.page_modes:
            continue
        records.append(record)
    return records


def _active_default_records(
    index: RegistryIndex,
    *,
    kind: str,
    allow_candidates: bool,
) -> list[ResourceRecord]:
    """Select one active default per resource id using explicit layer precedence."""

    priorities = ("project", "user", "builtin") if allow_candidates else ("user", "builtin")
    selected: dict[str, ResourceRecord] = {}
    for layer in priorities:
        for record in index.records:
            if record.kind != kind or record.layer != layer:
                continue
            default_version = index.defaults.get((layer, kind, record.resource_id))
            if default_version != record.version or record.resource_id in selected:
                continue
            selected[record.resource_id] = ResourceRecord(
                kind=record.kind,
                resource_id=record.resource_id,
                version=record.version,
                content_sha256=record.content_sha256,
                layer=record.layer,
                path=record.path,
                status=record.status,
                semantic_roles=record.semantic_roles,
                page_modes=record.page_modes,
                reason=MappingProxyType(
                    {
                        **dict(record.reason),
                        "selection": "layer-default",
                        "default_version": default_version,
                        "layer_precedence": list(priorities),
                    }
                ),
            )
    return [selected[resource_id] for resource_id in sorted(selected)]


def load_resource_package(record: ResourceRecord) -> Any:
    """Load the validated package behind one verified registry record."""

    if record.kind == "component":
        return load_component_package(record.path)
    return load_layout_policy_package(record.path)


def _record_from_value(
    *,
    root: Path,
    layer: str,
    kind: str,
    registry_path: Path,
    value: dict[str, Any],
) -> ResourceRecord:
    resource_id = str(value["resource_id"])
    version = str(value["version"])
    expected_path = f"{_COLLECTIONS[kind]}/{resource_id}/{version}"
    if value["relative_path"] != expected_path:
        raise RegistrySecurityError(
            f"registered path does not match package identity: {value['relative_path']}"
        )
    path = _safe_path(root, expected_path)
    try:
        identity = load_resource_identity(kind, path)
    except (ContractError, OSError) as exc:
        raise RegistryError(f"invalid registered package {expected_path}: {exc}") from exc
    expected = (resource_id, version, value["content_sha256"])
    observed = (identity.resource_id, identity.version, identity.content_sha256)
    if observed != expected:
        raise RegistryConflict(
            f"registry identity/hash mismatch for {resource_id}@{version} in {layer}"
        )
    if tuple(value["semantic_roles"]) != identity.semantic_roles:
        raise RegistryConflict(f"registry semantic roles changed for {resource_id}@{version}")
    if tuple(value["page_modes"]) != identity.page_modes:
        raise RegistryConflict(f"registry page modes changed for {resource_id}@{version}")
    return ResourceRecord(
        kind=kind,
        resource_id=resource_id,
        version=version,
        content_sha256=identity.content_sha256,
        layer=layer,
        path=path,
        status=str(value["status"]),
        semantic_roles=identity.semantic_roles,
        page_modes=identity.page_modes,
        reason=MappingProxyType(
            {
                "selection": "registry-load",
                "registry": str(registry_path),
                "relative_path": expected_path,
                "hash_verified": True,
            }
        ),
    )


def _validated_roots(roots: RegistryRoots) -> RegistryRoots:
    normalized = RegistryRoots(
        builtin=_canonical(Path(roots.builtin)),
        project=_canonical(Path(roots.project)),
        user=_canonical(Path(roots.user)),
    )
    named = (
        ("builtin", normalized.builtin),
        ("project", normalized.project),
        ("user", normalized.user),
    )
    for index, (left_name, left) in enumerate(named):
        for right_name, right in named[index + 1 :]:
            if _contains(left, right) or _contains(right, left):
                raise RegistrySecurityError(
                    f"registry roots overlap: {left_name}={left}, {right_name}={right}"
                )
    return normalized


def _read_index(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RegistryError(f"cannot read resource index: {path}") from exc
    if len(raw) > MAX_REGISTRY_BYTES:
        raise RegistryError(f"resource index exceeds 1 MiB: {path}")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise RegistryError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"invalid UTF-8 resource index: {path}") from exc
    return validate_resource_index(value)


def _safe_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise RegistrySecurityError(f"unsafe registry path: {relative}")
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise RegistrySecurityError(f"registry paths may not contain symlinks: {relative}")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise RegistryError(f"registered path is missing: {relative}") from exc
    if not _contains(root, resolved):
        raise RegistrySecurityError(f"registered path escapes its root: {relative}")
    return resolved


def _absolute(path: str | Path, *, relative_to: Path) -> Path:
    candidate = Path(path).expanduser()
    return (candidate if candidate.is_absolute() else relative_to / candidate).absolute()


def _canonical(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except OSError as exc:
        raise RegistrySecurityError(f"cannot resolve registry root: {path}") from exc


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RegistryError(f"{label} must be a string array")
    if len(value) != len(set(value)) or any(not item or len(item) > 128 for item in value):
        raise RegistryError(f"{label} contains invalid values")
    return list(value)


__all__ = [
    "RESOURCE_KINDS",
    "RegistryConflict",
    "RegistryError",
    "RegistryIndex",
    "RegistryRoots",
    "RegistrySecurityError",
    "ResourceRecord",
    "load_registry",
    "load_resource_package",
    "query_resources",
    "resolve_registry_roots",
    "validate_resource_index",
]
