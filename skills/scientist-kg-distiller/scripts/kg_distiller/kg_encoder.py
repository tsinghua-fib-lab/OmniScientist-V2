from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .ids import l2_id
from .io_utils import read_json, read_jsonl, write_json
from .schemas import KG_SCHEMA_VERSION, validate_kg_schema


def encode_kg(project_root: Path, scientist_id: str) -> Path:
    cards_path = project_root / "evidence_cards" / f"{scientist_id}.jsonl"
    assignments_path = project_root / "l2" / f"{scientist_id}_assignments.json"
    l2_path = project_root / "l2" / f"{scientist_id}_l2.json"
    l3_path = project_root / "l3" / f"{scientist_id}_l3.json"
    edges_path = project_root / "edges" / f"{scientist_id}_edges.json"
    for path in [cards_path, assignments_path, l2_path, l3_path, edges_path]:
        if not path.exists():
            raise FileNotFoundError(f"Missing KG input: {path}")
    cards = read_jsonl(cards_path)
    assignments = read_json(assignments_path)
    l2_nodes = read_json(l2_path)
    l3_nodes = read_json(l3_path)
    edges = read_json(edges_path)
    assignment_map = {item["card_id"]: item["category"] for item in assignments}
    l1_nodes: list[dict[str, Any]] = []
    for card in cards:
        category = assignment_map.get(card["card_id"])
        if not category:
            raise ValueError(f"Card has no L2 assignment: {card['card_id']}")
        l1_nodes.append(
            {
                "node_id": card["card_id"],
                "level": "L1",
                "source_id": card["source_id"],
                "source_title": card["source_title"],
                "source_type": card["source_type"],
                "year": card.get("year"),
                "quote_or_excerpt": card["excerpt"],
                "location": card["location"],
                "observation": card["observation"],
                "fact_type": card["fact_type"],
                "author_role": card["author_role"],
                "parent_L2": l2_id(scientist_id, category),
            }
        )
    supports = [
        {"from": node["parent_L2"], "to": node["node_id"]} for node in l1_nodes
    ]
    summarizes = [
        {"from": node["node_id"], "to": target}
        for node in l3_nodes
        for target in node.get("summarized_from_L2", [])
    ]
    graph = {
        "meta": {
            "scientist_id": scientist_id,
            "scientist_name": _scientist_name(project_root, scientist_id),
            "kg_version": "1.0",
            "schema_version": KG_SCHEMA_VERSION,
            "total_L1": len(l1_nodes),
            "total_L2": len(l2_nodes),
            "total_L3": len(l3_nodes),
            "review_status": {
                "L1_L2": "automatic",
                "L3": "automatic",
                "L2_edges": "automatic",
            },
        },
        "L3_stances": l3_nodes,
        "L2_patterns": l2_nodes,
        "L1_facts": l1_nodes,
        "edges": {
            "summarizes": summarizes,
            "supports": supports,
            "reinforces": edges["reinforces"],
            "enables": edges["enables"],
            "tension": edges["tension"],
        },
    }
    validate_kg(graph)
    return write_json(
        project_root / "scientist-kg" / f"{scientist_id}.kg.json", graph
    )


def validate_kg(graph: dict[str, Any]) -> None:
    validate_kg_schema(graph)
    if len(graph["L2_patterns"]) != 7 or len(graph["L3_stances"]) != 4:
        raise ValueError("KG must contain exactly 7 L2 and 4 L3 nodes")
    if {node["question"] for node in graph["L3_stances"]} != {
        "P01",
        "P02",
        "P03",
        "P04",
    }:
        raise ValueError("KG must contain P01-P04 exactly once")
    all_nodes = {
        node["node_id"]
        for key in ("L1_facts", "L2_patterns", "L3_stances")
        for node in graph[key]
    }
    if len(all_nodes) != sum(
        len(graph[key]) for key in ("L1_facts", "L2_patterns", "L3_stances")
    ):
        raise ValueError("KG contains duplicate node IDs")
    for key in ("summarizes", "supports", "reinforces", "enables"):
        for edge in graph["edges"][key]:
            if edge["from"] not in all_nodes or edge["to"] not in all_nodes:
                raise ValueError(f"{key} edge references an unknown node")
    for edge in graph["edges"]["tension"]:
        if any(node_id not in all_nodes for node_id in edge["between"]):
            raise ValueError("tension edge references an unknown node")
    support_targets = {edge["to"] for edge in graph["edges"]["supports"]}
    if (
        support_targets != {node["node_id"] for node in graph["L1_facts"]}
        or len(graph["edges"]["supports"]) != len(graph["L1_facts"])
    ):
        raise ValueError("Every L1 node must have exactly one support edge")
    l1_ids = {node["node_id"] for node in graph["L1_facts"]}
    l2_ids = {node["node_id"] for node in graph["L2_patterns"]}
    for node in graph["L3_stances"]:
        if node["question"] == "P04":
            if not 3 <= len(node["tone_exemplars"]) <= 5:
                raise ValueError("P04 requires 3-5 tone exemplars")
            continue
        if any(exemplar not in l1_ids for exemplar in node.get("exemplar_L1", [])):
            raise ValueError("L3 exemplar_L1 references an unknown L1 node")
        if set(node.get("considered_L2", [])) != l2_ids:
            raise ValueError("Every L3 node must consider all seven L2 patterns")
        if any(target not in l2_ids for target in node.get("summarized_from_L2", [])):
            raise ValueError("L3 summarized_from_L2 references an unknown L2 node")
        if node["question"] == "P01":
            names = {value.get("name") for value in node.get("value_dimensions", [])}
            if names != {"准确性", "一致性", "范围", "简单性", "丰产性"}:
                raise ValueError("P01 must use Kuhn's five original value terms")
        if node["question"] == "P03" and not isinstance(
            node.get("identity_context"), dict
        ):
            raise ValueError("P03 requires structured identity context")
        if node["question"] != "P03" and node.get("identity_context") is not None:
            raise ValueError("Only P03 may contain identity context")
    p04_id = next(
        node["node_id"]
        for node in graph["L3_stances"]
        if node["question"] == "P04"
    )
    if any(
        edge["from"] == p04_id for edge in graph["edges"]["summarizes"]
    ):
        raise ValueError("P04 must not have summarizes edges")
    expected_summarizes = {
        (node["node_id"], target)
        for node in graph["L3_stances"]
        for target in node.get("summarized_from_L2", [])
    }
    actual_summarizes = {
        (edge["from"], edge["to"])
        for edge in graph["edges"]["summarizes"]
    }
    if actual_summarizes != expected_summarizes:
        raise ValueError("summarizes edges must match P01-P03 exactly")
    _validate_agent_facing_text(graph)


def _validate_agent_facing_text(graph: dict[str, Any]) -> None:
    texts: list[str] = []
    for node in graph["L2_patterns"]:
        texts.append(str(node["description"]))
        texts.extend(str(value) for value in node["trigger_contexts"])
        texts.extend(str(value) for value in node["contraindicated_contexts"])
    for node in graph["L3_stances"]:
        if node["question"] == "P04":
            continue
        texts.extend([str(node["stance"]), str(node["explanation"])])
        for value in node.get("value_dimensions", []):
            texts.extend(
                [
                    str(value.get("relative_priority", "")),
                    str(value.get("explanation", "")),
                ]
            )
    offending = next(
        (
            text
            for text in texts
            if re.search(
                r"(?i)(?<![A-Za-z0-9_])(?:"
                r"l[123]_[a-z0-9_]+|L[123]|C0[1-7]"
                r")(?![A-Za-z0-9_])",
                text,
            )
        ),
        None,
    )
    if offending is not None:
        raise ValueError(
            "Agent-facing L2/L3 text must not contain internal node IDs"
        )


def _scientist_name(project_root: Path, scientist_id: str) -> str:
    profile = (
        project_root / "scientist-corpus" / scientist_id / "profile.json"
    )
    if not profile.exists():
        return scientist_id
    value = read_json(profile)
    return str(value.get("scientist_name") or scientist_id)
