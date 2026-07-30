from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .ids import l2_id, l3_id
from .io_utils import read_json, read_jsonl, write_json
from .llm import JsonLLM
from .prompts import L2_CATEGORIES, L3_QUESTIONS, SYSTEM_JSON, l3_prompt
from .tone_extraction import extract_tone_node


def abstract_l3(
    project_root: Path,
    scientist_id: str,
    llm: JsonLLM,
    *,
    question_concurrency: int | None = None,
) -> Path:
    l2_path = project_root / "l2" / f"{scientist_id}_l2.json"
    if not l2_path.exists():
        raise FileNotFoundError(f"Missing L2 nodes: {l2_path}")
    l2_nodes = read_json(l2_path)
    cards_path = project_root / "evidence_cards" / f"{scientist_id}.jsonl"
    if not cards_path.exists():
        raise FileNotFoundError(f"Missing L1 evidence: {cards_path}")
    cards = read_jsonl(cards_path)
    profile_path = (
        project_root / "scientist-corpus" / scientist_id / "profile.json"
    )
    if not profile_path.exists():
        raise FileNotFoundError(f"Missing identity profile: {profile_path}")
    profile = read_json(profile_path)
    prompts = {
        question: l3_prompt(question, definition, l2_nodes, cards, profile)
        for question, definition in L3_QUESTIONS.items()
    }
    request_keys = {
        question: hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        for question, prompt in prompts.items()
    }
    audit_path = (
        project_root / "l3" / f"{scientist_id}.abstraction_attempts.json"
    )
    audit_rows = read_json(audit_path) if audit_path.exists() else []
    audited = {
        row["request_key"]: row["response"]
        for row in audit_rows
        if isinstance(row, dict)
        and isinstance(row.get("request_key"), str)
        and "response" in row
    }
    concurrency = question_concurrency or int(
        os.environ.get("KG_DISTILLER_L3_CONCURRENCY", "3")
    )
    if concurrency < 1:
        raise ValueError("question_concurrency must be at least 1")
    responses = {
        question: audited[request_key]
        for question, request_key in request_keys.items()
        if request_key in audited
    }
    pending = [
        question for question in L3_QUESTIONS if question not in responses
    ]
    with ThreadPoolExecutor(
        max_workers=min(concurrency, len(pending) or 1)
    ) as executor:
        futures = {
            executor.submit(
                llm.complete_json,
                system=SYSTEM_JSON,
                user=prompts[question],
            ): question
            for question in pending
        }
        for future in as_completed(futures):
            question = futures[future]
            responses[question] = future.result()
            audit_rows.append(
                {
                    "question": question,
                    "request_key": request_keys[question],
                    "response": responses[question],
                }
            )
            write_json(audit_path, audit_rows)
    by_question: dict[str, dict[str, Any]] = {}
    for expected_question, payload in responses.items():
        item = payload.get("stance") if isinstance(payload, dict) else None
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("stance"), str)
            and isinstance(payload.get("explanation"), str)
        ):
            item = dict(payload)
            item["question"] = expected_question
        if not isinstance(item, dict):
            shape = list(payload) if isinstance(payload, dict) else type(payload).__name__
            raise TypeError(
                f"L3 {expected_question} response requires a stance object; got {shape!r}"
            )
        if str(item.get("question")) != expected_question:
            raise ValueError(
                f"L3 response question mismatch: expected {expected_question}"
            )
        by_question[expected_question] = item
    nodes: list[dict[str, Any]] = []
    valid_l1 = {card["card_id"] for card in cards}
    l1_by_id = {card["card_id"].lower(): card for card in cards}
    l2_by_category = {node["category"]: node for node in l2_nodes}
    for question, definition in L3_QUESTIONS.items():
        item = by_question[question]
        stance = _agent_facing_text(
            str(item.get("stance", "")).strip(), l1_by_id, l2_by_category
        )
        explanation = _agent_facing_text(
            str(item.get("explanation", "")).strip(), l1_by_id, l2_by_category
        )
        categories = [
            str(value).rsplit("_", 1)[-1]
            for value in item.get("relevant_L2", [])
        ] if isinstance(item.get("relevant_L2"), list) else []
        exemplars = [
            _canonical_l1_id(str(value), valid_l1)
            for value in item.get("exemplar_L1", [])
        ]
        if not stance or not explanation:
            raise ValueError(f"L3 {question} requires stance and explanation")
        if not isinstance(categories, list) or not categories or any(
            category not in {node["category"] for node in l2_nodes}
            for category in categories
        ):
            raise ValueError(
                f"L3 {question} has invalid relevant_L2: {item.get('relevant_L2')!r}"
            )
        required_categories = set(definition.get("from") or [])
        if not required_categories.issubset(categories) or len(categories) > 4:
            raise ValueError(
                f"L3 {question} relevant_L2 must include "
                f"{sorted(required_categories)} and contain at most 4 categories"
            )
        minimum_exemplars = min(3, len(valid_l1))
        if (
            len(exemplars) < minimum_exemplars
            or len(exemplars) > 8
            or any(value not in valid_l1 for value in exemplars)
        ):
            raise ValueError(f"L3 {question} has invalid exemplar_L1")
        _validate_focused_explanation(question, explanation)
        if question == "P02":
            _validate_p02_abstract_stance(stance)
        values = item.get("value_dimensions", []) if question == "P01" else []
        if question == "P01":
            expected = {"准确性", "一致性", "范围", "简单性", "丰产性"}
            if not isinstance(values, list) or {str(value.get("name")) for value in values} != expected:
                raise ValueError("P01 must explain Kuhn's five original values")
            if any(not str(value.get("relative_priority", "")).strip() or not str(value.get("explanation", "")).strip() for value in values):
                raise ValueError("P01 value dimensions require priority and explanation")
            p01_text = " ".join(
                [
                    stance,
                    explanation,
                    *[
                        f"{value.get('relative_priority', '')} "
                        f"{value.get('explanation', '')}"
                        for value in values
                    ],
                ]
            )
            _validate_p01_fecundity(p01_text)
            values = [
                {
                    **value,
                    "relative_priority": _agent_facing_text(
                        str(value["relative_priority"]), l1_by_id, l2_by_category
                    ),
                    "explanation": _agent_facing_text(
                        str(value["explanation"]), l1_by_id, l2_by_category
                    ),
                }
                for value in values
            ]
        if question == "P03":
            stance = f"{_identity_summary(profile)} {stance}".strip()
        nodes.append(
            {
                "node_id": l3_id(scientist_id, question),
                "level": "L3",
                "question": question,
                "question_label": definition["label"],
                "stance": stance,
                "explanation": explanation,
                "considered_L2": [node["node_id"] for node in l2_nodes],
                "summarized_from_L2": [
                    l2_id(scientist_id, category) for category in categories
                ],
                "exemplar_L1": exemplars,
                "value_dimensions": values,
                "identity_context": (
                    _identity_context(profile) if question == "P03" else None
                ),
                "human_review_required": False,
            }
        )
    tone_path = extract_tone_node(project_root, scientist_id, llm)
    nodes.append(read_json(tone_path))
    return write_json(project_root / "l3" / f"{scientist_id}_l3.json", nodes)


def _validate_focused_explanation(question: str, explanation: str) -> None:
    mentioned = [
        label for label in L2_CATEGORIES.values() if label in explanation
    ]
    if len(mentioned) > 4:
        raise ValueError(
            f"L3 {question} explanation must not enumerate the seven patterns"
        )
    if re.search(r"(?:基于|根据|依据)全部?七个", explanation):
        raise ValueError(
            f"L3 {question} explanation uses the forbidden seven-pattern preamble"
        )


def _validate_p01_fecundity(text: str) -> None:
    for match in re.finditer(r"(?:产出|成果|论文)数量", text):
        prefix = text[max(0, match.start() - 16) : match.start()]
        if re.search(r"(?:而非|不是|不指|并非|不(?:直接)?以).*$", prefix):
            continue
        raise ValueError(
            "P01 must treat fecundity as generating new inquiry, not output count"
        )


def _validate_p02_abstract_stance(stance: str) -> None:
    belief_and_prohibition = stance.split("知识边界", 1)[0]
    if re.search(
        r"(?:例如|譬如|诸如|比如|[（(]如)", belief_and_prohibition
    ):
        raise ValueError(
            "P02 belief and prohibition must remain abstract; "
            "concrete examples belong in explanation"
        )


def _identity_context(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "scientist_name": str(profile.get("scientist_name") or ""),
        "aliases": [str(value) for value in profile.get("aliases") or []],
        "occupations": [str(value) for value in profile.get("occupations") or []],
        "research_fields": [str(value) for value in profile.get("fields") or []],
        "education_history": list(profile.get("education_history") or []),
        "employment_history": list(profile.get("employment_history") or []),
        "institutions": [str(value) for value in profile.get("institutions") or []],
        "sources": [str(value) for value in profile.get("biography_sources") or []],
        "google_scholar_url": str(profile.get("google_scholar_url") or ""),
        "portrait_url": str(profile.get("portrait_url") or ""),
        "portrait_source_url": str(profile.get("portrait_source_url") or ""),
    }


_INTERNAL_L1 = re.compile(
    r"(?i)(?<![A-Za-z0-9_])l1_[a-z0-9_]+(?![A-Za-z0-9_])"
)
_INTERNAL_L2 = re.compile(
    r"(?i)(?<![A-Za-z0-9_])l2_[a-z0-9_]+(?![A-Za-z0-9_])"
)
_CATEGORY = re.compile(
    r"(?i)(?<![A-Za-z0-9_])C0[1-7](?![A-Za-z0-9_])"
)
_LAYER_NAME = re.compile(
    r"(?i)(?<![A-Za-z0-9_])L([123])(?![A-Za-z0-9_])"
)


def _canonical_l1_id(value: str, valid_l1: set[str]) -> str:
    if value in valid_l1:
        return value
    exact = {candidate.lower(): candidate for candidate in valid_l1}
    if value.lower() in exact:
        return exact[value.lower()]
    short = re.fullmatch(r"(?i)l1_(\d+)", value)
    if short:
        suffix = f"_{int(short.group(1)):04d}"
        matches = [
            candidate for candidate in valid_l1 if candidate.endswith(suffix)
        ]
        if len(matches) == 1:
            return matches[0]
    raise ValueError(f"Unknown L1 evidence reference: {value!r}")


def _agent_facing_text(
    text: str,
    l1_by_id: dict[str, dict[str, Any]],
    l2_by_category: dict[str, dict[str, Any]],
) -> str:
    valid_l1 = {card["card_id"] for card in l1_by_id.values()}

    def replace_l1(match: re.Match[str]) -> str:
        canonical = _canonical_l1_id(match.group(0), valid_l1)
        card = l1_by_id[canonical.lower()]
        title = str(card.get("source_title") or "相关论文")
        observation = str(card.get("observation") or "").strip()
        return f"《{title}》中关于“{observation}”的原始证据"

    def replace_l2(match: re.Match[str]) -> str:
        category = match.group(0).rsplit("_", 1)[-1].upper()
        node = l2_by_category.get(category)
        if not node:
            raise ValueError(f"Unknown L2 pattern reference: {match.group(0)!r}")
        return f"“{node['category_label']}”这一思维模式"

    def replace_category(match: re.Match[str]) -> str:
        category = match.group(0).upper()
        node = l2_by_category.get(category)
        if not node:
            raise ValueError(f"Unknown L2 category reference: {category!r}")
        return f"“{node['category_label']}”"

    text = _INTERNAL_L1.sub(replace_l1, text)
    text = _INTERNAL_L2.sub(replace_l2, text)
    text = _CATEGORY.sub(replace_category, text)
    return _LAYER_NAME.sub(
        lambda match: {
            "1": "原始论文证据",
            "2": "七类科研思维模式",
            "3": "高层人格结论",
        }[match.group(1)],
        text,
    )


def _identity_summary(profile: dict[str, Any]) -> str:
    name = str(profile.get("scientist_name") or "该科学家")
    occupations = "、".join(str(value) for value in profile.get("occupations") or [])
    fields = "、".join(str(value) for value in profile.get("fields") or [])
    clauses = [f"{name} 是{occupations or '身份尚未完整确认的科学家'}"]
    if fields:
        clauses.append(f"研究领域包括{fields}")

    education = []
    for item in profile.get("education_history") or []:
        institution = str(item.get("institution") or "").strip()
        degree = str(item.get("degree") or "").strip()
        if institution:
            education.append(
                f"在 {institution} 获得{degree}" if degree else f"就读于 {institution}"
            )
    clauses.append(
        f"学习经历为{'，'.join(education)}"
        if education
        else "现有身份资料未记录其学习经历"
    )

    employment = []
    rows = sorted(
        profile.get("employment_history") or [],
        key=lambda item: (
            item.get("start_year") is None,
            item.get("start_year") or 0,
        ),
    )
    for item in rows:
        organization = str(item.get("organization") or "").strip()
        if not organization:
            continue
        start = item.get("start_year")
        end = item.get("end_year")
        if start and end:
            period = f"{start}—{end}"
        elif start:
            period = f"{start}年至今"
        else:
            period = "时间未记录"
        employment.append(f"{organization}（{period}）")
    clauses.append(
        f"就职轨迹为{'、'.join(employment)}"
        if employment
        else "现有身份资料未记录其就职轨迹"
    )
    return "；".join(clauses) + "。"
