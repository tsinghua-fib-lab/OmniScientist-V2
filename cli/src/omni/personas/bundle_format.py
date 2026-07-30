"""Validate the stdlib-only format used for bundled scientist personas.

This module intentionally has no imports from SoulAgent. The CLI and the
portable Skill remain independently usable while agreeing on the on-disk KG
contract. The build hook also loads this file directly, before OmniScientist is
installed, so keep it limited to the Python standard library.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

CATALOG_FILENAME = "index.json"
REQUIRED_FILES = {
    "meta.json",
    "identity.json",
    "l2-patterns.json",
    "l3-stances.json",
    "edges.json",
    "l1-evidence/index.json",
}
L2_CATEGORIES = {f"C{number:02d}" for number in range(1, 8)}
EDGE_TYPES = {"summarizes", "supports", "reinforces", "enables", "tension"}
_SCIENTIST_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BundledPersonaValidationError(RuntimeError):
    """Raised when a bundled scientist-persona snapshot is unsafe or invalid."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundledPersonaValidationError(f"cannot read JSON {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BundledPersonaValidationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _safe_file(root: Path, relative: str) -> tuple[str, Path]:
    normalized = relative.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or ".." in pure.parts
        or "." in pure.parts
        or str(pure) != normalized
    ):
        raise BundledPersonaValidationError(f"unsafe manifest path: {relative}")
    unresolved = root / Path(*pure.parts)
    if unresolved.is_symlink():
        raise BundledPersonaValidationError(f"manifest file is a symlink: {relative}")
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise BundledPersonaValidationError(f"manifest path escapes KG: {relative}") from exc
    if not candidate.is_file():
        raise BundledPersonaValidationError(f"manifest file is missing or unsafe: {relative}")
    return normalized, candidate


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise BundledPersonaValidationError(f"cannot read JSONL {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BundledPersonaValidationError(
                f"invalid JSONL {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise BundledPersonaValidationError(
                f"JSONL record is not an object: {path}:{line_number}"
            )
        records.append(record)
    return records


def validate_persona_directory(
    root: Path,
    *,
    expected_id: str,
    expected_manifest_sha256: str,
) -> None:
    """Validate one complete KG directory against its pinned catalog digest."""
    if root.is_symlink():
        raise BundledPersonaValidationError(f"persona directory is a symlink: {root}")
    root = root.resolve()
    if not root.is_dir():
        raise BundledPersonaValidationError(f"persona directory is missing or unsafe: {root}")
    if not _SCIENTIST_ID.fullmatch(expected_id):
        raise BundledPersonaValidationError(f"invalid scientist_id: {expected_id}")
    if not _SHA256.fullmatch(expected_manifest_sha256):
        raise BundledPersonaValidationError(
            f"invalid manifest digest for {expected_id}: {expected_manifest_sha256}"
        )

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BundledPersonaValidationError(f"manifest.json missing for {expected_id}")
    actual_manifest_sha256 = _sha256(manifest_path)
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise BundledPersonaValidationError(
            f"manifest digest mismatch for {expected_id}: "
            f"expected {expected_manifest_sha256}, got {actual_manifest_sha256}"
        )
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("scientist_id") != expected_id:
        raise BundledPersonaValidationError(
            f"manifest scientist_id does not match directory id {expected_id}"
        )
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise BundledPersonaValidationError(f"manifest files missing for {expected_id}")

    declared: dict[str, Path] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise BundledPersonaValidationError(f"invalid manifest entry for {expected_id}")
        relative = entry.get("path")
        expected_hash = str(entry.get("sha256") or "").lower()
        if not isinstance(relative, str) or not _SHA256.fullmatch(expected_hash):
            raise BundledPersonaValidationError(f"invalid manifest entry for {expected_id}")
        normalized, file_path = _safe_file(root, relative)
        if normalized in declared:
            raise BundledPersonaValidationError(
                f"duplicate manifest path for {expected_id}: {normalized}"
            )
        actual_hash = _sha256(file_path)
        if actual_hash != expected_hash:
            raise BundledPersonaValidationError(
                f"file digest mismatch for {expected_id}/{normalized}: "
                f"expected {expected_hash}, got {actual_hash}"
            )
        declared[normalized] = file_path

    missing = REQUIRED_FILES - declared.keys()
    if missing:
        raise BundledPersonaValidationError(
            f"required files are undeclared for {expected_id}: {', '.join(sorted(missing))}"
        )
    actual_files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BundledPersonaValidationError(f"symlink is not allowed in {expected_id}: {path}")
        if path.is_file():
            actual_files.add(path.relative_to(root).as_posix())
    undeclared = actual_files - set(declared) - {"manifest.json"}
    if undeclared:
        raise BundledPersonaValidationError(
            f"undeclared files in {expected_id}: {', '.join(sorted(undeclared))}"
        )

    meta = _read_json(declared["meta.json"])
    identity = _read_json(declared["identity.json"])
    l2 = _read_json(declared["l2-patterns.json"])
    l3 = _read_json(declared["l3-stances.json"])
    edges = _read_json(declared["edges.json"])
    evidence_index = _read_json(declared["l1-evidence/index.json"])
    if (
        not isinstance(meta, dict)
        or meta.get("scientist_id") != expected_id
        or not str(meta.get("scientist_name") or "").strip()
    ):
        raise BundledPersonaValidationError(f"meta identity is invalid for {expected_id}")
    if not isinstance(identity, dict) or not str(identity.get("scientist_name") or "").strip():
        raise BundledPersonaValidationError(f"identity scientist_name missing for {expected_id}")
    if not isinstance(l2, list) or len(l2) != 7:
        raise BundledPersonaValidationError(f"L2 must contain exactly 7 nodes for {expected_id}")
    categories = {node.get("category") for node in l2 if isinstance(node, dict)}
    if categories != L2_CATEGORIES or any(not node.get("node_id") for node in l2):
        raise BundledPersonaValidationError(f"L2 categories are invalid for {expected_id}")
    if not isinstance(l3, list) or len(l3) != 4:
        raise BundledPersonaValidationError(f"L3 must contain exactly 4 nodes for {expected_id}")
    questions = {node.get("question") for node in l3 if isinstance(node, dict)}
    if questions != {"P01", "P02", "P03", "P04"}:
        raise BundledPersonaValidationError(f"L3 questions are invalid for {expected_id}")
    p04 = next(node for node in l3 if node.get("question") == "P04")
    exemplars = p04.get("tone_exemplars")
    if (
        set(p04)
        != {"node_id", "level", "question", "question_label", "tone_exemplars"}
        or
        not isinstance(exemplars, list)
        or not 3 <= len(exemplars) <= 5
        or any(not isinstance(value, str) or not value for value in exemplars)
        or len(set(exemplars)) != len(exemplars)
    ):
        raise BundledPersonaValidationError(f"P04 tone exemplars are invalid for {expected_id}")
    if not isinstance(edges, dict) or set(edges) != EDGE_TYPES:
        raise BundledPersonaValidationError(f"edge groups are invalid for {expected_id}")
    if any(not isinstance(edges[edge_type], list) for edge_type in EDGE_TYPES):
        raise BundledPersonaValidationError(f"edge group is not an array for {expected_id}")
    if not isinstance(evidence_index, dict) or not isinstance(
        evidence_index.get("partitions"), list
    ):
        raise BundledPersonaValidationError(f"evidence index is invalid for {expected_id}")

    evidence_count = 0
    partition_categories: set[str] = set()
    l1_ids: set[str] = set()
    for partition in evidence_index["partitions"]:
        if not isinstance(partition, dict):
            raise BundledPersonaValidationError(f"evidence partition is invalid for {expected_id}")
        category = partition.get("category")
        relative = partition.get("path")
        if category not in L2_CATEGORIES or not isinstance(relative, str):
            raise BundledPersonaValidationError(f"evidence partition is invalid for {expected_id}")
        partition_path = relative.replace("\\", "/")
        normalized = f"l1-evidence/{partition_path}"
        evidence_path = declared.get(normalized)
        if evidence_path is None:
            raise BundledPersonaValidationError(
                f"evidence partition is undeclared for {expected_id}: {normalized}"
            )
        records = _load_jsonl(evidence_path)
        expected_count = partition.get("count")
        if expected_count is not None and expected_count != len(records):
            raise BundledPersonaValidationError(
                f"evidence count mismatch for {expected_id}/{normalized}"
            )
        for record in records:
            node_id = str(record.get("node_id") or "")
            if not node_id or node_id in l1_ids:
                raise BundledPersonaValidationError(
                    f"evidence node_id is missing or duplicated for {expected_id}/{normalized}"
                )
            l1_ids.add(node_id)
        evidence_count += len(records)
        if str(category) in partition_categories:
            raise BundledPersonaValidationError(
                f"duplicate evidence partition category for {expected_id}: {category}"
            )
        partition_categories.add(str(category))
    if partition_categories != L2_CATEGORIES:
        raise BundledPersonaValidationError(
            f"evidence partitions must cover C01-C07 for {expected_id}"
        )
    counts = manifest.get("counts")
    if isinstance(counts, dict) and (
        counts.get("L1") != evidence_count
        or counts.get("L2") != len(l2)
        or counts.get("L3") != len(l3)
    ):
        raise BundledPersonaValidationError(f"manifest counts are invalid for {expected_id}")

    l2_ids = {str(node["node_id"]) for node in l2}
    l3_ids = {
        str(node["node_id"])
        for node in l3
        if node.get("question") != "P04" and node.get("node_id")
    }
    for edge in edges["summarizes"]:
        if not isinstance(edge, dict) or str(edge.get("from") or "") not in l3_ids or str(
            edge.get("to") or ""
        ) not in l2_ids:
            raise BundledPersonaValidationError(f"invalid summarizes edge for {expected_id}")
    for edge_type in ("reinforces", "enables"):
        for edge in edges[edge_type]:
            if not isinstance(edge, dict) or str(edge.get("from") or "") not in l2_ids or str(
                edge.get("to") or ""
            ) not in l2_ids:
                raise BundledPersonaValidationError(
                    f"invalid {edge_type} edge for {expected_id}"
                )
    for edge in edges["supports"]:
        if not isinstance(edge, dict) or str(edge.get("from") or "") not in l2_ids or str(
            edge.get("to") or ""
        ) not in l1_ids:
            raise BundledPersonaValidationError(f"invalid supports edge for {expected_id}")
    for edge in edges["tension"]:
        between = edge.get("between") if isinstance(edge, dict) else None
        if (
            not isinstance(between, list)
            or len(between) != 2
            or any(str(node_id) not in l2_ids for node_id in between)
        ):
            raise BundledPersonaValidationError(f"invalid tension edge for {expected_id}")


def validate_builtin_persona_collection(root: Path) -> tuple[str, ...]:
    """Validate the pinned catalog and every declared bundled scientist KG."""
    if root.is_symlink():
        raise BundledPersonaValidationError(f"builtin persona collection is a symlink: {root}")
    root = root.resolve()
    catalog_path = root / CATALOG_FILENAME
    if catalog_path.is_symlink():
        raise BundledPersonaValidationError("builtin persona catalog must not be a symlink")
    catalog = _read_json(catalog_path)
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise BundledPersonaValidationError("builtin persona catalog schema_version must be 1")
    scientists = catalog.get("scientists")
    if not isinstance(scientists, list) or not scientists:
        raise BundledPersonaValidationError("builtin persona catalog has no scientists")

    declared: list[str] = []
    for entry in scientists:
        if not isinstance(entry, dict):
            raise BundledPersonaValidationError("invalid builtin persona catalog entry")
        scientist_id = entry.get("scientist_id")
        manifest_sha256 = str(entry.get("manifest_sha256") or "").lower()
        if not isinstance(scientist_id, str) or scientist_id in declared:
            raise BundledPersonaValidationError("invalid or duplicate catalog scientist_id")
        validate_persona_directory(
            root / scientist_id,
            expected_id=scientist_id,
            expected_manifest_sha256=manifest_sha256,
        )
        declared.append(scientist_id)

    actual_directories = {
        path.name for path in root.iterdir() if path.is_dir() and not path.is_symlink()
    }
    if actual_directories != set(declared):
        raise BundledPersonaValidationError(
            "builtin persona directories do not exactly match the catalog"
        )
    return tuple(declared)
