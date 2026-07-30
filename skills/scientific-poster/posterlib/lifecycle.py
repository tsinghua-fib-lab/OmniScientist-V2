"""Immutable three-layer evolution for components and layout policies."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .contracts import (
    COMPONENT_FILES,
    LAYOUT_POLICY_FILES,
    RESOURCE_KINDS,
    ContractError,
    ResourceIdentity,
    load_resource_identity,
)
from .registry import (
    RegistryConflict,
    RegistryRoots,
    ResourceRecord,
    load_registry,
    validate_resource_index,
)

_COLLECTIONS = {
    "component": "components",
    "layout-policy": "layout-policies",
}
_FILES = {
    "component": COMPONENT_FILES,
    "layout-policy": LAYOUT_POLICY_FILES,
}
MAX_RESOURCE_BYTES = 1024 * 1024


class LifecycleError(ValueError):
    """A resource evolution request failed a stable policy boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def propose_resource(
    roots: RegistryRoots,
    *,
    kind: str,
    source: str | Path,
) -> ResourceRecord:
    """Copy exact validated bytes into the project candidate layer."""

    identity, contents = _source_package(kind, Path(source).expanduser())
    project = Path(roots.project).expanduser().resolve()
    destination = _publish_package(project, identity, contents)
    _write_index(
        root=project,
        layer="project",
        identity=identity,
        path=destination,
        make_default=True,
    )
    return _find_record(
        roots,
        layer="project",
        kind=kind,
        resource_id=identity.resource_id,
        version=identity.version,
    )


def promote_resource(
    roots: RegistryRoots,
    *,
    kind: str,
    resource_id: str,
    version: str,
    content_sha256: str,
    approved: bool,
    decision: dict[str, str],
) -> ResourceRecord:
    """Promote exact candidate bytes after a hash-bound operator decision."""

    if approved is not True:
        _fail("promotion_approval_required", "literal approval is required")
    if (
        decision.get("target_kind") != kind
        or decision.get("target_sha256") != content_sha256
        or not decision.get("session_id")
    ):
        _fail("promotion_approval_required", "approval decision does not match the resource")
    try:
        candidate = _find_record(
            roots,
            layer="project",
            kind=kind,
            resource_id=resource_id,
            version=version,
        )
    except RegistryConflict as exc:
        _fail("resource_identity_changed", f"candidate bytes changed: {exc}")
    if candidate.content_sha256 != content_sha256:
        _fail("resource_identity_changed", "candidate hash does not match the approval")
    try:
        identity, contents = _source_package(kind, candidate.path)
    except LifecycleError as exc:
        _fail("resource_identity_changed", f"candidate bytes changed: {exc}")
    if identity.content_sha256 != content_sha256:
        _fail("resource_identity_changed", "candidate bytes changed before promotion")
    user = Path(roots.user).expanduser().resolve()
    destination = _publish_package(user, identity, contents)
    _write_index(
        root=user,
        layer="user",
        identity=identity,
        path=destination,
        make_default=True,
    )
    return _find_record(
        roots,
        layer="user",
        kind=kind,
        resource_id=resource_id,
        version=version,
    )


def rollback_default(
    roots: RegistryRoots,
    *,
    kind: str,
    scope: str,
    resource_id: str,
    target_version: str,
    reason: str,
) -> dict[str, str]:
    """Repoint one mutable layer default to an existing immutable package."""

    if kind not in RESOURCE_KINDS:
        _fail("rollback_target_missing", f"unsupported resource kind: {kind}")
    if scope not in {"project", "user"} or not reason.strip():
        _fail("rollback_target_missing", "scope and a non-empty reason are required")
    record = _find_record(
        roots,
        layer=scope,
        kind=kind,
        resource_id=resource_id,
        version=target_version,
    )
    root = Path(getattr(roots, scope)).expanduser().resolve()
    index_path = root / _COLLECTIONS[kind] / "registry.json"
    value = _read_index(index_path, layer=scope, kind=kind)
    value["defaults"][resource_id] = target_version
    _store_index(index_path, validate_resource_index(value))
    return {
        "kind": record.kind,
        "scope": scope,
        "resource_id": record.resource_id,
        "target_version": record.version,
    }


def _source_package(kind: str, source: Path) -> tuple[ResourceIdentity, dict[str, bytes]]:
    if kind not in RESOURCE_KINDS:
        _fail("candidate_validation_failed", f"unsupported resource kind: {kind}")
    if source.is_symlink() or not source.is_dir():
        _fail("candidate_validation_failed", "resource source must be a regular directory")
    expected = _FILES[kind]
    try:
        names = {item.name for item in source.iterdir()}
    except OSError as exc:
        _fail("candidate_validation_failed", f"cannot inspect resource source: {exc}")
    if names != set(expected):
        _fail(
            "candidate_validation_failed",
            "resource source must contain exactly: " + ", ".join(expected),
        )
    try:
        identity = load_resource_identity(kind, source)
    except (ContractError, OSError) as exc:
        _fail("candidate_validation_failed", str(exc))
    contents: dict[str, bytes] = {}
    for name in expected:
        member = source / name
        if member.is_symlink() or not member.is_file():
            _fail("candidate_validation_failed", f"resource member is not regular: {name}")
        try:
            raw = member.read_bytes()
        except OSError as exc:
            _fail("candidate_validation_failed", f"cannot read {name}: {exc}")
        if len(raw) > MAX_RESOURCE_BYTES:
            _fail("candidate_validation_failed", f"resource member exceeds 1 MiB: {name}")
        contents[name] = raw
    return identity, contents


def _publish_package(
    root: Path,
    identity: ResourceIdentity,
    contents: dict[str, bytes],
) -> Path:
    destination = (
        root
        / _COLLECTIONS[identity.kind]
        / identity.resource_id
        / identity.version
    )
    if destination.exists():
        try:
            observed = load_resource_identity(identity.kind, destination)
        except (ContractError, OSError) as exc:
            _fail("resource_conflict", f"existing package is invalid: {exc}")
        if observed != identity:
            _fail(
                "resource_conflict",
                f"different bytes already use {identity.resource_id}@{identity.version}",
            )
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".resource-", dir=destination.parent))
    try:
        for name, raw in contents.items():
            (temporary / name).write_bytes(raw)
        temporary.replace(destination)
    except FileExistsError:
        return _publish_package(root, identity, contents)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def _write_index(
    *,
    root: Path,
    layer: str,
    identity: ResourceIdentity,
    path: Path,
    make_default: bool,
) -> None:
    index_path = root / _COLLECTIONS[identity.kind] / "registry.json"
    value = _read_index(index_path, layer=layer, kind=identity.kind)
    record = {
        "resource_id": identity.resource_id,
        "version": identity.version,
        "content_sha256": identity.content_sha256,
        "relative_path": path.relative_to(root).as_posix(),
        "status": {"project": "candidate", "user": "approved"}[layer],
        "semantic_roles": list(identity.semantic_roles),
        "page_modes": list(identity.page_modes),
    }
    existing = [
        item
        for item in value["records"]
        if (item["resource_id"], item["version"])
        != (identity.resource_id, identity.version)
    ]
    existing.append(record)
    existing.sort(key=lambda item: (item["resource_id"], item["version"]))
    value["records"] = existing
    if make_default:
        value["defaults"][identity.resource_id] = identity.version
    _store_index(index_path, validate_resource_index(value))


def _read_index(path: Path, *, layer: str, kind: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema": "scientific-poster.resource-index.v1",
            "layer": layer,
            "kind": kind,
            "defaults": {},
            "records": [],
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        normalized = validate_resource_index(value)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _fail("candidate_validation_failed", f"invalid resource index: {exc}")
    if normalized["layer"] != layer or normalized["kind"] != kind:
        _fail("candidate_validation_failed", "resource index has the wrong identity")
    return normalized


def _store_index(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _find_record(
    roots: RegistryRoots,
    *,
    layer: str,
    kind: str,
    resource_id: str,
    version: str,
) -> ResourceRecord:
    matches = [
        record
        for record in load_registry(roots).records
        if record.layer == layer
        and record.kind == kind
        and record.resource_id == resource_id
        and record.version == version
    ]
    if len(matches) != 1:
        _fail("rollback_target_missing", f"resource is unavailable: {resource_id}@{version}")
    return matches[0]


def _fail(code: str, message: str) -> None:
    raise LifecycleError(code, message)


__all__ = ["LifecycleError", "promote_resource", "propose_resource", "rollback_default"]
