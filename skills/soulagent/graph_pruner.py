from __future__ import annotations

import re
from typing import Any

PHASE_TO_L2: dict[str, list[str] | None] = {
    "problem_formulation": ["C01"],
    "method_selection": ["C02", "C05"],
    "experiment_design": ["C02", "C03"],
    "result_analysis": ["C03", "C04"],
    "review": ["C03", "C04"],
    "failure_diagnosis": ["C06", "C03"],
    "ideation": ["C07", "C01"],
    "implementation": ["C02", "C06"],
    "general": None,
}

CATEGORY_HINTS = {
    "C01": ("定义", "问题", "假设", "约束", "统一", "本质"),
    "C02": ("选择", "方法", "架构", "工具", "实现", "框架"),
    "C03": ("验证", "实验", "消融", "对比", "基线", "指标"),
    "C04": ("解释", "结果", "现象", "因果", "归因"),
    "C05": ("美丑", "简洁", "优雅", "复杂", "效率"),
    "C06": ("失败", "排查", "错误", "没效果", "不收敛"),
    "C07": ("想法", "方向", "创新", "探索", "假设"),
}


def _category(node_id: str) -> str:
    match = re.search(r"_C(0[1-7])$", node_id)
    if not match:
        raise ValueError(f"无法从 L2 node_id 提取 category：{node_id}")
    return f"C{match.group(1)}"


def _terms(text: str) -> set[str]:
    lower = text.lower()
    terms = set(re.findall(r"[a-z0-9_]+", lower))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lower))
    terms.update(chinese[i : i + 2] for i in range(max(0, len(chinese) - 1)))
    return {term for term in terms if term}


def _semantic_score(text: str, objective: str) -> float:
    left = _terms(text)
    right = _terms(objective)
    if not left or not right:
        return 0.0
    return len(left & right) / len(right)


def _general_seeds(objective: str, l2_by_category: dict[str, dict[str, Any]]) -> list[str]:
    scores: list[tuple[float, str]] = []
    objective_lower = objective.lower()
    for category, node in l2_by_category.items():
        trigger_text = " ".join(str(x) for x in node.get("trigger_contexts", []))
        description = str(node.get("description", ""))
        label = str(node.get("category_label", ""))
        score = _semantic_score(f"{trigger_text} {description} {label}", objective)
        score += sum(
            0.25 for hint in CATEGORY_HINTS[category] if hint in objective_lower
        )
        scores.append((score, category))
    scores.sort(key=lambda item: (-item[0], item[1]))
    return [category for _, category in scores[:2]]


def _constraint_matches(context: str, constraints: dict[str, Any]) -> bool:
    if context == "limited_experimental_budget":
        return bool(constraints.get("compute_constraint"))
    if context == "project_time_constraints":
        return bool(constraints.get("time_pressure"))
    return bool(constraints.get(context))


def _select_evidence(
    candidates: list[dict[str, Any]],
    objective: str,
    exemplar_ids: set[str],
    limit: int = 5,
) -> list[dict[str, Any]]:
    remaining = list(candidates)
    selected: list[dict[str, Any]] = []
    used_types: set[str] = set()
    while remaining and len(selected) < limit:
        best_index = 0
        best_key: tuple[float, float, str] | None = None
        for index, item in enumerate(remaining):
            exemplar = 1.0 if str(item.get("node_id")) in exemplar_ids else 0.0
            source_type = str(item.get("source_type", "unknown"))
            diversity = 1.0 if source_type not in used_types else 0.0
            relevance = _semantic_score(
                f"{item.get('source_title', '')} {item.get('observation', '')}",
                objective,
            )
            key = (
                exemplar * 100.0 + diversity * 10.0 + relevance,
                relevance,
                str(item.get("node_id", "")),
            )
            if best_key is None or key > best_key:
                best_key = key
                best_index = index
        chosen = remaining.pop(best_index)
        selected.append(chosen)
        used_types.add(str(chosen.get("source_type", "unknown")))
    return selected


def prune_graph(
    task_frame: dict[str, Any], kg: dict[str, Any]
) -> dict[str, Any]:
    phase = str(task_frame["phase"])
    if phase not in PHASE_TO_L2:
        raise ValueError(f"未知 phase：{phase}")

    l2_by_category = {str(node["category"]): node for node in kg["l2"]}
    l2_by_id = {str(node["node_id"]): node for node in kg["l2"]}
    categories = PHASE_TO_L2[phase]
    if categories is None:
        categories = _general_seeds(str(task_frame["objective"]), l2_by_category)

    selected: dict[str, str] = {
        str(l2_by_category[category]["node_id"]): "seed" for category in categories
    }
    seed_ids = set(selected)

    for edge in kg["edges"]["reinforces"]:
        left = str(edge["from"])
        right = str(edge["to"])
        if left in seed_ids and right not in selected:
            selected[right] = "reinforced"
        if right in seed_ids and left not in selected:
            selected[left] = "reinforced"

    before_enables = set(selected)
    for edge in kg["edges"]["enables"]:
        prerequisite = str(edge["from"])
        dependent = str(edge["to"])
        if dependent in before_enables and prerequisite not in selected:
            selected[prerequisite] = "prerequisite"

    tension_resolved: list[dict[str, Any]] = []
    tension_dropped: set[str] = set()
    constraints = task_frame.get("constraints", {})
    for edge in kg["edges"]["tension"]:
        left, right = (str(x) for x in edge["between"])
        context = str(edge.get("context", ""))
        if left not in selected or right not in selected:
            continue
        if not _constraint_matches(context, constraints):
            continue
        left_count = int(l2_by_id[left].get("supporting_L1_count", 0))
        right_count = int(l2_by_id[right].get("supporting_L1_count", 0))
        if left_count == right_count:
            tension_resolved.append(
                {
                    "context": context,
                    "kept": [l2_by_id[left]["category_label"], l2_by_id[right]["category_label"]],
                    "dropped": None,
                    "reason": "两侧支持证据数相同，保留显式取舍。",
                }
            )
            continue
        kept, dropped = (left, right) if left_count > right_count else (right, left)
        selected.pop(dropped, None)
        tension_dropped.add(dropped)
        tension_resolved.append(
            {
                "context": context,
                "kept": l2_by_id[kept]["category_label"],
                "dropped": l2_by_id[dropped]["category_label"],
                "reason": edge.get("reason", ""),
                "supporting_counts": {
                    str(l2_by_id[kept]["category_label"]): max(left_count, right_count),
                    str(l2_by_id[dropped]["category_label"]): min(left_count, right_count),
                },
            }
        )

    if len(selected) < 2:
        remaining = [
            node
            for node in kg["l2"]
            if str(node["node_id"]) not in selected
            and str(node["node_id"]) not in tension_dropped
        ]
        remaining.sort(
            key=lambda node: (
                -_semantic_score(
                    f"{node.get('category_label', '')} {node.get('description', '')}",
                    str(task_frame["objective"]),
                ),
                -int(node.get("supporting_L1_count", 0)),
                str(node["category"]),
            )
        )
        if remaining:
            selected[str(remaining[0]["node_id"])] = "related"

    if len(selected) > 5:
        role_priority = {"seed": 0, "prerequisite": 1, "reinforced": 2, "related": 3}
        ranked = sorted(
            selected,
            key=lambda node_id: (
                role_priority[selected[node_id]],
                -int(l2_by_id[node_id].get("supporting_L1_count", 0)),
                str(l2_by_id[node_id]["category"]),
            ),
        )
        selected = {node_id: selected[node_id] for node_id in ranked[:5]}

    active_l2 = []
    for node in kg["l2"]:
        node_id = str(node["node_id"])
        if node_id in selected:
            active_l2.append({**node, "activation_role": selected[node_id]})

    exemplar_ids = {
        str(node_id)
        for stance in kg["l3"]
        for node_id in stance.get("exemplar_L1", [])
    }
    evidence: list[dict[str, Any]] = []
    for node in active_l2:
        category = str(node["category"])
        chosen = _select_evidence(
            kg["evidence_by_category"][category],
            str(task_frame["objective"]),
            exemplar_ids,
            limit=5,
        )
        evidence.extend(chosen)

    stance_nodes = [
        node
        for node in kg["l3"]
        if node.get("question") in {"P01", "P02", "P03"}
    ]
    tone_node = next(
        node for node in kg["l3"] if node.get("question") == "P04"
    )
    philosophy_kernel = {
        "stances": stance_nodes,
        "tone_exemplars": list(tone_node["tone_exemplars"]),
    }
    return {
        "scientist_id": kg["scientist_id"],
        "identity": kg["identity"],
        "philosophy_kernel": philosophy_kernel,
        "l3": stance_nodes,
        "active_l2": active_l2,
        "l1_evidence": evidence,
        "tension_resolved": tension_resolved,
    }
