from __future__ import annotations

import re
from typing import Any


PHASE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (
        "failure_diagnosis",
        (
            "为什么没效果",
            "为什么不对",
            "结果不对",
            "哪里错了",
            "排查",
            "失败",
            "不收敛",
            "报错",
            "debug",
            "diagnose",
        ),
    ),
    (
        "experiment_design",
        ("怎么设计实验", "实验方案", "消融", "基线", "对比实验", "ablation", "baseline"),
    ),
    (
        "review",
        ("审稿", "有什么问题", "靠谱吗", "批判", "评审", "review"),
    ),
    (
        "result_analysis",
        ("结果说明什么", "为什么变好", "解释结果", "分析结果", "现象", "归因"),
    ),
    (
        "problem_formulation",
        ("怎么定义", "换个角度", "重新想", "重新定义", "问题设定", "reframe"),
    ),
    (
        "method_selection",
        ("选哪个", "用什么框架", "用哪种", "backbone", "架构选择", "方法选择"),
    ),
    (
        "ideation",
        ("有什么新方向", "还值得做", "新想法", "研究方向", "idea", "创新点"),
    ),
    (
        "implementation",
        ("帮我实现", "怎么写代码", "落地为代码", "实现模型", "implement"),
    ),
]

SCIENCE_MARKERS = (
    "科学",
    "研究",
    "论文",
    "实验",
    "模型",
    "算法",
    "训练",
    "数据集",
    "假设",
    "方法",
    "基线",
    "消融",
    "指标",
    "精度",
    "benchmark",
    "network",
    "loss",
    "gradient",
)

COMPUTE_TRUE = (
    "资源有限",
    "gpu 不够",
    "gpu不够",
    "算力有限",
    "跑不了太多实验",
    "计算资源有限",
)
COMPUTE_FALSE = ("gpu 管够", "gpu管够", "算力充足", "资源充足")
TIME_TRUE = ("赶 deadline", "赶deadline", "快速", "紧急", "时间有限", "尽快")
TIME_FALSE = ("不着急", "时间充足", "不用赶", "慢慢来")


def _messages(conversation: str | list[dict[str, Any]] | list[str]) -> list[dict[str, str]]:
    if isinstance(conversation, str):
        return [{"role": "user", "content": conversation}]
    normalized: list[dict[str, str]] = []
    for item in conversation:
        if isinstance(item, str):
            normalized.append({"role": "user", "content": item})
        elif isinstance(item, dict):
            content = item.get("content")
            if content:
                normalized.append(
                    {"role": str(item.get("role", "user")), "content": str(content)}
                )
    return normalized


def _latest_user(messages: list[dict[str, str]]) -> str:
    for message in reversed(messages):
        if message["role"].lower() == "user":
            return message["content"].strip()
    return ""


def _constraint_value(
    latest: str, all_text: str, positive: tuple[str, ...], negative: tuple[str, ...]
) -> bool:
    latest_lower = latest.lower()
    for phrase in negative:
        if phrase in latest_lower:
            return False
    for phrase in positive:
        if phrase in latest_lower:
            return True
    all_lower = all_text.lower()
    last_positive = max((all_lower.rfind(x) for x in positive), default=-1)
    last_negative = max((all_lower.rfind(x) for x in negative), default=-1)
    return last_positive > last_negative


def sense_task(
    conversation: str | list[dict[str, Any]] | list[str],
) -> dict[str, Any] | None:
    messages = _messages(conversation)
    latest = _latest_user(messages)
    if not latest:
        return None
    recent = "\n".join(message["content"] for message in messages[-8:])
    lower = latest.lower()

    phase = ""
    matched_keyword = False
    for candidate, keywords in PHASE_KEYWORDS:
        if any(keyword in lower for keyword in keywords):
            phase = candidate
            matched_keyword = True
            break

    has_science_marker = any(marker in lower for marker in SCIENCE_MARKERS)
    if phase == "implementation" and not has_science_marker:
        return None
    if not matched_keyword and not has_science_marker:
        return None
    if not phase:
        phase = "general"

    objective = re.sub(r"\s+", " ", latest).strip()
    constraints = {
        "compute_constraint": _constraint_value(
            latest, recent, COMPUTE_TRUE, COMPUTE_FALSE
        ),
        "time_pressure": _constraint_value(latest, recent, TIME_TRUE, TIME_FALSE),
    }
    return {"phase": phase, "objective": objective, "constraints": constraints}


def objective_similarity(left: str, right: str) -> float:
    def tokens(text: str) -> set[str]:
        lowered = text.lower()
        words = set(re.findall(r"[a-z0-9_]+", lowered))
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
        words.update(chinese[i : i + 2] for i in range(max(0, len(chinese) - 1)))
        return {token for token in words if token}

    a = tokens(left)
    b = tokens(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
