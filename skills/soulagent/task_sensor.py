from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from llm_client import LLMClientError, complete_chat, is_configured

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
    "单卡",
    "single gpu",
    "only one gpu",
    "limited compute",
)
COMPUTE_FALSE = ("gpu 管够", "gpu管够", "算力充足", "资源充足")
TIME_TRUE = (
    "赶 deadline",
    "赶deadline",
    "deadline",
    "urgent",
    "快速",
    "紧急",
    "时间有限",
    "尽快",
    "小时内",
    "天内",
    "周内",
)
TIME_FALSE = ("不着急", "时间充足", "不用赶", "慢慢来")

CONTINUITY_MARKERS = (
    "继续",
    "刚才",
    "同一个",
    "还是这个",
    "仍然",
    "目标不变",
    "其他目标不变",
    "continue",
    "same task",
    "same experiment",
    "as before",
    "previous task",
)


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


def _constraint_override(latest: str, key: str) -> bool | None:
    """Return an explicit constraint update, or ``None`` when not mentioned."""
    lowered = latest.casefold()
    if key == "compute_constraint":
        positive, negative = COMPUTE_TRUE, COMPUTE_FALSE
        positive_pattern = r"(?:gpu|算力|计算资源).{0,8}(?:有限|不足|不够|紧张)"
        negative_pattern = r"(?:gpu|算力|计算资源|资源).{0,8}(?:充足|足够|管够)"
    else:
        positive, negative = TIME_TRUE, TIME_FALSE
        positive_pattern = (
            r"(?:必须|需要|要).{0,12}(?:小时|天|周).{0,8}(?:完成|结束)"
            r"|within\s+\d+\s*(?:hours?|days?|weeks?)"
        )
        negative_pattern = r"(?:时间).{0,6}(?:充足|足够)|(?:无需|不用).{0,4}(?:赶|着急)"
    if any(phrase in lowered for phrase in negative) or re.search(
        negative_pattern, lowered
    ):
        return False
    if any(phrase in lowered for phrase in positive) or re.search(
        positive_pattern, lowered
    ):
        return True
    return None


def inherit_task_context(
    task_frame: dict[str, Any],
    previous: dict[str, Any] | None,
    conversation: str | list[dict[str, Any]] | list[str],
) -> dict[str, Any]:
    """Carry stable task context across a clearly continuous user turn.

    The task sensor classifies the latest message. A later message may only
    refine the same experiment or update one resource constraint, so treating
    every omitted field as a reset causes needless persona regeneration.
    Explicit phase transitions and explicit constraint relaxations still win.
    """
    if not previous:
        return task_frame
    latest = _latest_user(_messages(conversation))
    lowered = latest.casefold()
    continuous = any(marker in lowered for marker in CONTINUITY_MARKERS) or bool(
        re.search(
            r"(?:这个|该|上述|前面).{0,8}(?:实验|问题|模型|任务|结果)"
            r"|this\s+(?:experiment|issue|model|task|result)",
            lowered,
        )
    )
    overrides = {
        key: _constraint_override(latest, key)
        for key in ("compute_constraint", "time_pressure")
    }
    merged = dict(task_frame)
    current_constraints = dict(task_frame.get("constraints") or {})
    previous_constraints = dict(previous.get("constraints") or {})
    mentions_constraint = any(value is not None for value in overrides.values()) or any(
        bool(current_constraints.get(key)) and not bool(previous_constraints.get(key))
        for key in overrides
    )
    for key, override in overrides.items():
        if override is not None:
            current_constraints[key] = override
        elif bool(current_constraints.get(key)):
            current_constraints[key] = True
        elif key in previous_constraints:
            current_constraints[key] = bool(previous_constraints[key])
    merged["constraints"] = current_constraints

    previous_phase = str(previous.get("phase") or "")
    current_phase = str(task_frame.get("phase") or "")
    if current_phase == "general" and previous_phase and (
        continuous or mentions_constraint
    ):
        merged["phase"] = previous_phase
        current_phase = previous_phase

    if continuous and current_phase == previous_phase:
        previous_objective = str(previous.get("objective") or "").strip()
        if previous_objective:
            merged["objective"] = previous_objective
    return merged


def _check_llm_available() -> bool:
    """Check whether LLM environment variables are set."""
    return is_configured()


def _sense_task_llm(
    messages: list[dict[str, str]],
    completion_fn: Callable[[str, str], str] | None = None,
) -> dict[str, Any] | None:
    """Classify the latest task with an OpenAI-compatible scientific task sensor."""
    if completion_fn is None and not _check_llm_available():
        return None

    system_prompt = """You are a scientific task sensor. Analyze the conversation between a user and a coding agent. Determine whether the current turn involves a scientific research task, and if so, identify the task phase and resource constraints. The user may write in Chinese or English.

# Task Phases
Classify the user's latest message into one of these phases:
- problem_formulation: Defining or reframing a research problem
- method_selection: Choosing methods, tools, architectures, frameworks
- experiment_design: Designing experiments, ablations, baselines, control groups
- result_analysis: Analyzing results, interpreting phenomena, attributing causes
- review: Peer review, critical assessment, checking methodological flaws
- failure_diagnosis: Troubleshooting failures, locating bugs, debugging
- ideation: Generating new ideas, exploring directions
- implementation: Coding, implementing, writing code
- general: Scientific conversation that does not clearly fit the above

# Phase Transition Semantics
If the user's message indicates a prior phase has completed (e.g. "already identified the problem", "failure is resolved", "我们已经定位了问题") and then describes a new activity (e.g. "接下来设计实验", "now let's design the experiment"), infer the NEW phase rather than staying in the completed one. Do NOT classify a message as failure_diagnosis if it says the failure has already been found and the user is moving on.

# Scientific Task Detection
- Pure engineering tasks (changing titles, formatting, deployment scripts, config changes) with no scientific judgment required → is_scientific = false
- Tasks involving scientific reasoning (defining problems, choosing methods, designing experiments, analyzing results, diagnosing failures, generating ideas, reviewing papers) → is_scientific = true

# Resource Constraints
- compute_constraint = true: user mentions limited resources (including but not limited to: single GPU, 单卡, only one GPU, GPU shortage, limited compute, 算力有限, resources are tight, cannot run many experiments)
- time_pressure = true: user mentions time constraints or urgency (including but not limited to: must finish in N hours, 必须在N小时内, deadline, urgent, 快速, time is limited, within N days)

# Output Format
Output ONLY a JSON object. No markdown, no code blocks, no extra text:
{"is_scientific": false, "phase": "general", "constraints": {"compute_constraint": false, "time_pressure": false}}

# Examples
Input: "现在失败问题已经定位，接下来请设计 baseline、对照组和消融实验。"
Output: {"is_scientific": true, "phase": "experiment_design", "constraints": {"compute_constraint": false, "time_pressure": false}}

Input: "只能使用单卡GPU，必须在8小时内完成。"
Output: {"is_scientific": true, "phase": "general", "constraints": {"compute_constraint": true, "time_pressure": true}}

Input: "帮我把 README 标题改短"
Output: {"is_scientific": false, "phase": "general", "constraints": {"compute_constraint": false, "time_pressure": false}}
"""
    conversation_text = "\n".join(
        f"{message['role']}: {message['content']}" for message in messages[-8:]
    )

    try:
        content = (
            completion_fn(system_prompt, conversation_text)
            if completion_fn is not None
            else complete_chat(
                system_prompt,
                conversation_text,
                max_tokens=512,
                timeout_seconds=30,
                response_format={"type": "json_object"},
            )
        )
        result = json.loads(content)
    except (LLMClientError, json.JSONDecodeError, TypeError, ValueError):
        return None
    except Exception:  # noqa: BLE001 - optional host callback must fall back safely
        return None

    phases = {phase for phase, _ in PHASE_KEYWORDS} | {"general"}
    constraints = result.get("constraints") if isinstance(result, dict) else None
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("is_scientific"), bool)
        or result.get("phase") not in phases
        or not isinstance(constraints, dict)
        or not isinstance(constraints.get("compute_constraint"), bool)
        or not isinstance(constraints.get("time_pressure"), bool)
    ):
        return None
    return result


def _sense_task_keywords(
    conversation: str | list[dict[str, Any]] | list[str],
) -> dict[str, Any] | None:
    messages = conversation
    recent = "\n".join(message["content"] for message in messages[-8:])
    latest = _latest_user(messages)
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


def sense_task(
    conversation: str | list[dict[str, Any]] | list[str],
    completion_fn: Callable[[str, str], str] | None = None,
) -> dict[str, Any] | None:
    """Sense a scientific task with an LLM and fall back to keyword matching."""
    messages = _messages(conversation)
    latest = _latest_user(messages)
    if not latest:
        return None

    try:
        result = _sense_task_llm(messages, completion_fn=completion_fn)
    except Exception:  # noqa: BLE001 - sensing failure falls back to deterministic rules
        result = None

    if result is not None and not result.get("is_scientific", False):
        return None

    if result is not None and result.get("phase"):
        result["objective"] = re.sub(r"\s+", " ", latest).strip()
        return result

    return _sense_task_keywords(messages)


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
