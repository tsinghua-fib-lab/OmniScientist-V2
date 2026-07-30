from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class KGValidationError(RuntimeError):
    pass


REQUIRED_FILES = {
    "manifest.json",
    "identity.json",
    "l3-stances.json",
    "l2-patterns.json",
    "edges.json",
    "l1-evidence/index.json",
}
EDGE_TYPES = {"summarizes", "supports", "reinforces", "enables", "tension"}
L2_CATEGORIES = {f"C{i:02d}" for i in range(1, 8)}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KGValidationError(f"无法读取 JSON：{path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise KGValidationError(f"无法计算哈希：{path}: {exc}") from exc
    return digest.hexdigest()


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise KGValidationError(f"manifest 路径越界：{relative}") from exc
    return candidate


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8-sig").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise KGValidationError(
                    f"JSONL 解析失败：{path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(record, dict):
                raise KGValidationError(f"JSONL 行不是对象：{path}:{line_number}")
            records.append(record)
    except OSError as exc:
        raise KGValidationError(f"无法读取 JSONL：{path}: {exc}") from exc
    return records


def load_kg(kg_dir: str | Path) -> dict[str, Any]:
    root = Path(kg_dir).resolve()
    if not root.is_dir():
        raise KGValidationError(f"KG 目录不存在：{root}")

    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise KGValidationError(f"KG 缺少必要文件：{', '.join(sorted(missing))}")

    manifest_path = root / "manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise KGValidationError("manifest.json 缺少 files 数组")

    declared_paths: set[str] = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or not entry.get("path") or not entry.get("sha256"):
            raise KGValidationError("manifest.files 中存在无效条目")
        relative = str(entry["path"]).replace("\\", "/")
        file_path = _safe_child(root, relative)
        if not file_path.is_file():
            raise KGValidationError(f"manifest 声明的文件不存在：{relative}")
        actual = _sha256(file_path)
        expected = str(entry["sha256"]).lower()
        if actual != expected:
            raise KGValidationError(
                f"manifest 哈希不匹配：{relative}，期望 {expected}，实际 {actual}"
            )
        declared_paths.add(relative)

    undeclared_required = REQUIRED_FILES - {"manifest.json"} - declared_paths
    if undeclared_required:
        raise KGValidationError(
            "manifest 未声明必要文件：" + ", ".join(sorted(undeclared_required))
        )

    identity = _read_json(root / "identity.json")
    l3 = _read_json(root / "l3-stances.json")
    l2 = _read_json(root / "l2-patterns.json")
    edges = _read_json(root / "edges.json")
    evidence_index = _read_json(root / "l1-evidence" / "index.json")

    if not isinstance(identity, dict) or not identity.get("scientist_name"):
        raise KGValidationError("identity.json 缺少 scientist_name")
    if not isinstance(l3, list) or len(l3) != 4:
        raise KGValidationError(f"L3 节点必须恰好为 4 个，实际 {len(l3) if isinstance(l3, list) else '非数组'}")
    questions = {
        node.get("question") for node in l3 if isinstance(node, dict)
    }
    if questions != {"P01", "P02", "P03", "P04"}:
        raise KGValidationError(
            f"L3 question 必须为 P01-P04，实际 {sorted(str(x) for x in questions)}"
        )
    p04 = next(node for node in l3 if node.get("question") == "P04")
    if set(p04) != {
        "node_id",
        "level",
        "question",
        "question_label",
        "tone_exemplars",
    }:
        raise KGValidationError("P04 只能包含语气样例字段")
    tone_exemplars = p04.get("tone_exemplars")
    if (
        not isinstance(tone_exemplars, list)
        or not 3 <= len(tone_exemplars) <= 5
        or any(not isinstance(value, str) or not value for value in tone_exemplars)
        or len(set(tone_exemplars)) != len(tone_exemplars)
    ):
        raise KGValidationError("P04 tone_exemplars 必须包含 3-5 条原句")
    if not isinstance(l2, list) or len(l2) != 7:
        raise KGValidationError(f"L2 节点必须恰好为 7 个，实际 {len(l2) if isinstance(l2, list) else '非数组'}")
    categories = {node.get("category") for node in l2 if isinstance(node, dict)}
    if categories != L2_CATEGORIES:
        raise KGValidationError(f"L2 category 必须为 C01-C07，实际 {sorted(str(x) for x in categories)}")
    if not isinstance(edges, dict) or set(edges) != EDGE_TYPES:
        raise KGValidationError(
            f"edges.json 必须只含五类边 {sorted(EDGE_TYPES)}，实际 {sorted(edges) if isinstance(edges, dict) else '非对象'}"
        )
    if not isinstance(evidence_index, dict) or not isinstance(
        evidence_index.get("partitions"), list
    ):
        raise KGValidationError("l1-evidence/index.json 缺少 partitions")

    evidence_by_category: dict[str, list[dict[str, Any]]] = {}
    all_l1_ids: set[str] = set()
    for partition in evidence_index["partitions"]:
        category = partition.get("category")
        relative = partition.get("path")
        if category not in L2_CATEGORIES or not relative:
            raise KGValidationError(f"无效证据分区：{partition}")
        records = _load_jsonl(_safe_child(root / "l1-evidence", str(relative)))
        expected_count = partition.get("count")
        if expected_count is not None and len(records) != expected_count:
            raise KGValidationError(
                f"证据分区数量不匹配：{relative}，期望 {expected_count}，实际 {len(records)}"
            )
        for record in records:
            node_id = record.get("node_id")
            if not node_id:
                raise KGValidationError(f"证据缺少 node_id：{relative}")
            if node_id in all_l1_ids:
                raise KGValidationError(f"重复 L1 node_id：{node_id}")
            all_l1_ids.add(str(node_id))
        evidence_by_category[str(category)] = records

    l2_ids = {str(node["node_id"]) for node in l2}
    summarized_l3_ids = {
        str(node["node_id"]) for node in l3 if node.get("question") != "P04"
    }
    for edge_type in ("summarizes", "reinforces", "enables"):
        if not isinstance(edges[edge_type], list):
            raise KGValidationError(f"{edge_type} 必须为数组")
        for edge in edges[edge_type]:
            source = str(edge.get("from", ""))
            target = str(edge.get("to", ""))
            if edge_type == "summarizes":
                valid = source in summarized_l3_ids and target in l2_ids
            else:
                valid = source in l2_ids and target in l2_ids
            if not valid:
                raise KGValidationError(f"{edge_type} 边端点无效：{edge}")

    if not isinstance(edges["supports"], list):
        raise KGValidationError("supports 必须为数组")
    for edge in edges["supports"]:
        if str(edge.get("from", "")) not in l2_ids or str(
            edge.get("to", "")
        ) not in all_l1_ids:
            raise KGValidationError(f"supports 边端点无效：{edge}")

    if not isinstance(edges["tension"], list):
        raise KGValidationError("tension 必须为数组")
    for edge in edges["tension"]:
        between = edge.get("between")
        if (
            not isinstance(between, list)
            or len(between) != 2
            or any(str(node_id) not in l2_ids for node_id in between)
        ):
            raise KGValidationError(f"tension 边端点无效：{edge}")

    return {
        "root": str(root),
        "manifest": manifest,
        "manifest_sha256": _sha256(manifest_path),
        "scientist_id": manifest.get("scientist_id") or root.name,
        "identity": identity,
        "l3": l3,
        "l2": l2,
        "edges": edges,
        "evidence_index": evidence_index,
        "evidence_by_category": evidence_by_category,
    }
