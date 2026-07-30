from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .io_utils import read_json, read_jsonl, write_json, write_jsonl
from .kg_encoder import validate_kg

STORE_VERSION = "1.0.0"
CATEGORIES = tuple(f"C{index:02d}" for index in range(1, 8))
SCIENTIST_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class KGInstallError(RuntimeError):
    """A validated KG could not be safely exposed to a scanner root."""


def write_kg_store(
    graph: dict[str, Any],
    result_root: Path,
    scientist_id: str,
) -> Path:
    graph = {
        **graph,
        "L1_facts": sorted(
            graph["L1_facts"], key=lambda node: str(node["node_id"])
        ),
    }
    validate_kg(graph)
    if graph["meta"]["scientist_id"] != scientist_id:
        raise ValueError("KG scientist_id does not match delivery target")

    delivery_dir = result_root / scientist_id
    store_dir = delivery_dir / "kg"
    evidence_dir = store_dir / "l1-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    bundle_path = write_json(delivery_dir / "kg.json", graph)
    write_json(store_dir / "meta.json", graph["meta"])
    write_json(store_dir / "l3-stances.json", graph["L3_stances"])
    write_json(store_dir / "l2-patterns.json", graph["L2_patterns"])
    write_json(store_dir / "edges.json", graph["edges"])

    p03 = next(
        node for node in graph["L3_stances"] if node["question"] == "P03"
    )
    write_json(store_dir / "identity.json", p03["identity_context"])

    label_by_id = {
        node["node_id"]: (node["category"], node["category_label"])
        for node in graph["L2_patterns"]
    }
    grouped = {category: [] for category in CATEGORIES}
    for node in graph["L1_facts"]:
        parent = str(node["parent_L2"])
        if parent not in label_by_id:
            raise ValueError(f"L1 parent missing from L2 store: {parent}")
        grouped[label_by_id[parent][0]].append(node)

    partitions = []
    for category in CATEGORIES:
        filename = f"{category.lower()}.jsonl"
        path = write_jsonl(evidence_dir / filename, grouped[category])
        l2 = next(
            node for node in graph["L2_patterns"]
            if node["category"] == category
        )
        partitions.append(
            {
                "category": category,
                "label": l2["category_label"],
                "path": filename,
                "count": len(grouped[category]),
                "sha256": _file_hash(path),
            }
        )
    index_path = write_json(
        evidence_dir / "index.json",
        {
            "total": len(graph["L1_facts"]),
            "partition_key": "parent_L2.category",
            "partitions": partitions,
        },
    )

    files = [
        "meta.json",
        "identity.json",
        "l3-stances.json",
        "l2-patterns.json",
        "edges.json",
        "l1-evidence/index.json",
        *(f"l1-evidence/{category.lower()}.jsonl" for category in CATEGORIES),
    ]
    manifest_path = write_json(
        store_dir / "manifest.json",
        {
            "store_version": STORE_VERSION,
            "scientist_id": scientist_id,
            "bundle": "../kg.json",
            "bundle_sha256": _file_hash(bundle_path),
            "files": [
                {
                    "path": relative,
                    "sha256": _file_hash(store_dir / relative),
                }
                for relative in files
            ],
            "counts": {
                "L1": graph["meta"]["total_L1"],
                "L2": graph["meta"]["total_L2"],
                "L3": graph["meta"]["total_L3"],
            },
            "evidence_index_sha256": _file_hash(index_path),
        },
    )
    _write_delivery_manifest(delivery_dir, scientist_id)
    validate_kg_store(store_dir)
    return manifest_path


def read_kg_store(store_dir: Path) -> dict[str, Any]:
    store_dir = store_dir.resolve()
    manifest = read_json(store_dir / "manifest.json")
    meta = read_json(store_dir / "meta.json")
    l3 = read_json(store_dir / "l3-stances.json")
    l2 = read_json(store_dir / "l2-patterns.json")
    edges = read_json(store_dir / "edges.json")
    index = read_json(store_dir / "l1-evidence" / "index.json")

    l1: list[dict[str, Any]] = []
    for partition in index["partitions"]:
        path = _safe_child(
            store_dir / "l1-evidence", str(partition["path"])
        )
        rows = read_jsonl(path)
        if len(rows) != partition["count"]:
            raise ValueError(
                f"KG evidence partition count mismatch: {partition['path']}"
            )
        if _file_hash(path) != partition["sha256"]:
            raise ValueError(
                f"KG evidence partition hash mismatch: {partition['path']}"
            )
        l1.extend(rows)

    graph = {
        "meta": meta,
        "L3_stances": l3,
        "L2_patterns": l2,
        "L1_facts": sorted(l1, key=lambda node: str(node["node_id"])),
        "edges": edges,
    }
    validate_kg(graph)
    if manifest["scientist_id"] != meta["scientist_id"]:
        raise ValueError("KG store manifest scientist_id mismatch")
    if len(l1) != index["total"]:
        raise ValueError("KG evidence index total mismatch")
    return graph


def validate_kg_store(store_dir: Path, *, require_bundle: bool = True) -> None:
    """Validate the layered KG store and, for delivery stores, its bundle."""
    store_dir = store_dir.resolve()
    manifest_path = store_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing KG store manifest: {manifest_path}")
    manifest = read_json(manifest_path)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise TypeError("KG store manifest requires a files array")
    declared: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            raise TypeError("KG store manifest contains an invalid file entry")
        relative = str(item["path"]).replace("\\", "/")
        if relative in declared:
            raise ValueError(f"KG store manifest path is duplicated: {relative}")
        declared.add(relative)
        path = _safe_child(store_dir, relative)
        if not path.exists():
            raise ValueError(f"KG store file missing: {item['path']}")
        if _file_hash(path) != item["sha256"]:
            raise ValueError(f"KG store file hash mismatch: {item['path']}")

    graph = read_kg_store(store_dir)
    identity = read_json(store_dir / "identity.json")
    p03 = next(
        node for node in graph["L3_stances"] if node["question"] == "P03"
    )
    if identity != p03["identity_context"]:
        raise ValueError("KG identity view does not match P03 identity context")
    if not require_bundle:
        return

    bundle_path = store_dir.parent / "kg.json"
    if not bundle_path.exists():
        raise ValueError("KG store requires a sibling kg.json bundle")
    if _file_hash(bundle_path) != manifest["bundle_sha256"]:
        raise ValueError("KG bundle hash does not match store manifest")
    if read_json(bundle_path) != graph:
        raise ValueError("KG bundle content does not match layered store")


def install_kg_store(
    store_dir: Path,
    install_root: Path,
    scientist_id: str,
) -> Path:
    """Validate and atomically install one KG at ``<install_root>/<id>``.

    Refuse to overwrite an existing persona. The temporary directory is a
    sibling of the scanner root so it stays invisible to scanners while still
    permitting a same-volume atomic rename on Windows.
    """
    if not SCIENTIST_ID_PATTERN.fullmatch(scientist_id):
        raise KGInstallError(f"Unsafe scientist_id: {scientist_id!r}")

    source = store_dir.expanduser().resolve()
    root = install_root.expanduser().resolve()
    try:
        validate_kg_store(source)
        manifest = read_json(source / "manifest.json")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise KGInstallError(f"Source KG validation failed: {exc}") from exc
    if str(manifest.get("scientist_id") or "") != scientist_id:
        raise KGInstallError("Source KG scientist_id does not match install target")

    root.mkdir(parents=True, exist_ok=True)
    target = root / scientist_id
    if target.exists():
        raise KGInstallError(f"Install target already exists; refusing overwrite: {target}")

    lock_path = root / f".install-{scientist_id}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise KGInstallError(f"Another process is installing {scientist_id}") from exc

    staging: Path | None = None
    try:
        os.close(lock_fd)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".scientist-kg-distill-{scientist_id}-",
                dir=root.parent,
            )
        )
        shutil.copy2(source / "manifest.json", staging / "manifest.json")
        for item in manifest["files"]:
            relative = str(item["path"]).replace("\\", "/")
            source_file = _safe_child(source, relative)
            destination = _safe_child(staging, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
        validate_kg_store(staging, require_bundle=False)
        if target.exists():
            raise KGInstallError(
                f"Install target appeared during publication; refusing overwrite: {target}"
            )
        os.replace(staging, target)
        return target / "manifest.json"
    except KGInstallError:
        raise
    except OSError as exc:
        raise KGInstallError(f"KG installation failed: {exc}") from exc
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _write_delivery_manifest(delivery_dir: Path, scientist_id: str) -> None:
    capsule = delivery_dir / "capsule" / "manifest.json"
    write_json(
        delivery_dir / "manifest.json",
        {
            "scientist_id": scientist_id,
            "canonical_store": "kg/manifest.json",
            "portable_bundle": "kg.json",
            "capsule": "capsule/manifest.json" if capsule.exists() else None,
        },
    )


def update_delivery_manifest_for_capsule(
    delivery_dir: Path, scientist_id: str
) -> None:
    _write_delivery_manifest(delivery_dir, scientist_id)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"KG store path escapes its root: {relative}") from exc
    return candidate
