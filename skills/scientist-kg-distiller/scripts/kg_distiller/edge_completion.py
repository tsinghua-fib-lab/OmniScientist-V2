from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .io_utils import read_json, write_json
from .llm import JsonLLM
from .prompts import SYSTEM_JSON, edge_prompt

EDGE_TYPES = ("reinforces", "enables", "tension")
EDGE_LIMITS = {"reinforces": 6, "enables": 6, "tension": 3}


def complete_edges(
    project_root: Path,
    scientist_id: str,
    llm: JsonLLM,
    *,
    edge_concurrency: int | None = None,
) -> Path:
    l2_path = project_root / "l2" / f"{scientist_id}_l2.json"
    l3_path = project_root / "l3" / f"{scientist_id}_l3.json"
    if not l2_path.exists() or not l3_path.exists():
        raise FileNotFoundError("Edge completion requires both L2 and L3 outputs")
    l2_nodes = read_json(l2_path)
    l3_nodes = read_json(l3_path)
    concurrency = edge_concurrency or int(
        os.environ.get("KG_DISTILLER_EDGE_CONCURRENCY", "3")
    )
    if concurrency < 1:
        raise ValueError("edge_concurrency must be at least 1")
    allowed = {node["node_id"] for node in l2_nodes}
    labels_by_id = {
        str(node["node_id"]): str(node["category_label"])
        for node in l2_nodes
    }
    labels_by_category = {
        str(node["category"]): str(node["category_label"])
        for node in l2_nodes
    }
    horizontal: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(concurrency, len(EDGE_TYPES))) as executor:
        futures = {
            executor.submit(
                _complete_validated_edge_type,
                llm,
                edge_type,
                l2_nodes,
                allowed,
                labels_by_id,
                labels_by_category,
            ): edge_type
            for edge_type in EDGE_TYPES
        }
        for future in as_completed(futures):
            horizontal[futures[future]] = future.result()
    edges: dict[str, list[dict[str, Any]]] = {
        "reinforces": horizontal["reinforces"],
        "enables": horizontal["enables"],
        "tension": horizontal["tension"],
        "summarizes": [
            {"from": node["node_id"], "to": target}
            for node in l3_nodes
            for target in node.get("summarized_from_L2", [])
        ],
    }
    return write_json(project_root / "edges" / f"{scientist_id}_edges.json", edges)


def _complete_validated_edge_type(
    llm: JsonLLM,
    edge_type: str,
    l2_nodes: list[dict[str, Any]],
    allowed: set[str],
    labels_by_id: dict[str, str],
    labels_by_category: dict[str, str],
) -> list[dict[str, Any]]:
    base_prompt = edge_prompt(edge_type, l2_nodes)
    prompt = base_prompt
    for attempt in range(2):
        payload = llm.complete_json(system=SYSTEM_JSON, user=prompt)
        try:
            if edge_type == "tension":
                return _validate_tensions(
                    payload,
                    allowed,
                    labels_by_id,
                    labels_by_category,
                )
            return _validate_pair_edges(
                payload,
                edge_type,
                allowed,
                labels_by_id,
                labels_by_category,
            )
        except (TypeError, ValueError) as exc:
            if attempt == 1:
                raise
            prompt = (
                f"{base_prompt}\n\n上次输出未通过校验：{exc}。"
                "请修正该问题并重新输出完整 JSON。"
            )
    raise AssertionError("edge retry loop exhausted")


def _validate_pair_edges(
    payload: Any,
    key: str,
    allowed: set[str],
    labels_by_id: dict[str, str],
    labels_by_category: dict[str, str],
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise TypeError(f"Edge response requires '{key}' array")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in payload[key]:
        if not isinstance(item, dict):
            raise TypeError(f"{key} edges must be objects")
        source, target = item.get("from"), item.get("to")
        if source not in allowed or target not in allowed or source == target:
            raise ValueError(f"Invalid {key} edge: {source} -> {target}")
        reason = _humanize_reason(
            str(item.get("reason", "")).strip(),
            labels_by_id,
            labels_by_category,
        )
        if not reason:
            raise ValueError(f"{key} edge requires reason")
        identity = tuple(sorted((source, target))) if key == "reinforces" else (source, target)
        if identity not in seen:
            seen.add(identity)
            result.append(
                {
                    "from": source,
                    "to": target,
                    "reason": reason,
                }
            )
    if len(result) > EDGE_LIMITS[key]:
        raise ValueError(
            f"{key} returned {len(result)} edges; maximum is {EDGE_LIMITS[key]}"
        )
    return result


def _validate_tensions(
    payload: Any,
    allowed: set[str],
    labels_by_id: dict[str, str],
    labels_by_category: dict[str, str],
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("tension"), list):
        raise TypeError("Edge response requires 'tension' array")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in payload["tension"]:
        between = item.get("between") if isinstance(item, dict) else None
        if (
            not isinstance(between, list)
            or len(between) != 2
            or between[0] not in allowed
            or between[1] not in allowed
            or between[0] == between[1]
        ):
            raise ValueError(f"Invalid tension edge: {between}")
        context = str(item.get("context", "")).strip()
        if not context:
            raise ValueError("Tension edge requires context")
        reason = _humanize_reason(
            str(item.get("reason", "")).strip(),
            labels_by_id,
            labels_by_category,
        )
        if not reason:
            raise ValueError("Tension edge requires reason")
        identity = tuple(sorted(between))
        if identity not in seen:
            seen.add(identity)
            result.append(
                {
                    "between": between,
                    "context": context,
                    "reason": reason,
                }
            )
    if len(result) > EDGE_LIMITS["tension"]:
        raise ValueError(
            f"tension returned {len(result)} edges; "
            f"maximum is {EDGE_LIMITS['tension']}"
        )
    return result


def _humanize_reason(
    reason: str,
    labels_by_id: dict[str, str],
    labels_by_category: dict[str, str],
) -> str:
    for node_id, label in sorted(
        labels_by_id.items(), key=lambda item: len(item[0]), reverse=True
    ):
        reason = reason.replace(node_id, f"“{label}”")
    for category, label in labels_by_category.items():
        reason = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(category)}(?![A-Za-z0-9_])",
            f"“{label}”",
            reason,
        )
    reason = re.sub(r"“([^”]+)”[（(]\1[）)]", r"“\1”", reason)
    if re.search(
        r"trigger_contexts|contraindicated_contexts|"
        r"(?<![A-Za-z0-9_])[A-Za-z]+(?:_[A-Za-z]+)+(?![A-Za-z0-9_])",
        reason,
    ):
        raise ValueError("Edge reason must paraphrase task contexts in natural Chinese")
    if re.search(
        r"(?i)(?<![A-Za-z0-9_])(?:"
        r"l[123]_[a-z0-9_]+|L[123]|C0[1-7]"
        r")(?![A-Za-z0-9_])",
        reason,
    ):
        raise ValueError("Edge reason contains unresolved internal node IDs")
    return reason
