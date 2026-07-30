from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .evidence_card import _list
from .ids import l2_id
from .io_utils import read_json, read_jsonl, write_json
from .llm import JsonLLM
from .prompts import (
    L2_CATEGORIES,
    SYSTEM_JSON,
    classification_prompt,
    induction_prompt,
)


def induce_l2(
    project_root: Path,
    scientist_id: str,
    llm: JsonLLM,
    *,
    classification_batch_size: int = 100,
    induction_concurrency: int | None = None,
) -> Path:
    cards_path = project_root / "evidence_cards" / f"{scientist_id}.jsonl"
    if not cards_path.exists():
        raise FileNotFoundError(f"Missing evidence cards: {cards_path}")
    cards = read_jsonl(cards_path)
    audit_path = project_root / "l2" / f"{scientist_id}.induction_attempts.json"
    audit_rows = read_json(audit_path) if audit_path.exists() else []
    audited = {
        row["request_key"]: row["response"]
        for row in audit_rows
        if isinstance(row, dict)
        and isinstance(row.get("request_key"), str)
        and "response" in row
    }
    assignments: list[dict[str, str]] = []
    for start in range(0, len(cards), classification_batch_size):
        batch = cards[start : start + classification_batch_size]
        prompt = classification_prompt(batch)
        request_key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        payload = audited.get(request_key)
        if payload is None:
            payload = llm.complete_json(system=SYSTEM_JSON, user=prompt)
            audit_rows.append(
                {
                    "stage": "classification",
                    "batch_start": start,
                    "request_key": request_key,
                    "response": payload,
                }
            )
            audited[request_key] = payload
            write_json(audit_path, audit_rows)
        raw = _list(payload, "assignments")
        expected = {card["card_id"] for card in batch}
        received = [str(item.get("card_id")) for item in raw]
        if set(received) != expected or len(received) != len(expected):
            raise ValueError("L2 classification must assign every card exactly once")
        for item in raw:
            category = item.get("category")
            if category not in L2_CATEGORIES:
                raise ValueError(f"Invalid L2 category: {category}")
            assignments.append(
                {"card_id": str(item["card_id"]), "category": str(category)}
            )
    assignment_path = project_root / "l2" / f"{scientist_id}_assignments.json"
    write_json(assignment_path, assignments)

    category_by_card = {item["card_id"]: item["category"] for item in assignments}
    grouped = {
        category: [
            card
            for card in cards
            if category_by_card.get(card["card_id"]) == category
        ]
        for category in L2_CATEGORIES
    }
    concurrency = induction_concurrency or int(
        os.environ.get("KG_DISTILLER_L2_CONCURRENCY", "7")
    )
    if concurrency < 1:
        raise ValueError("induction_concurrency must be at least 1")
    induced: dict[str, tuple[str, list[str], list[str]]] = {}
    populated = [category for category in L2_CATEGORIES if grouped[category]]
    prompts = {
        category: induction_prompt(category, grouped[category])
        for category in populated
    }
    request_keys = {
        category: hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        for category, prompt in prompts.items()
    }
    responses = {
        category: audited[request_key]
        for category, request_key in request_keys.items()
        if request_key in audited
    }
    pending = [category for category in populated if category not in responses]
    with ThreadPoolExecutor(max_workers=min(concurrency, len(populated) or 1)) as executor:
        futures = {
            executor.submit(
                llm.complete_json,
                system=SYSTEM_JSON,
                user=prompts[category],
            ): category
            for category in pending
        }
        for future in as_completed(futures):
            category = futures[future]
            responses[category] = future.result()
            audit_rows.append(
                {
                    "stage": "induction",
                    "category": category,
                    "request_key": request_keys[category],
                    "response": responses[category],
                }
            )
            write_json(audit_path, audit_rows)
    for category, payload in responses.items():
        induced[category] = (
            _humanize_l2_description(
                _nonempty_string(payload, "description"), cards
            ),
            _string_list(payload, "trigger_contexts"),
            _string_list(payload, "contraindicated_contexts"),
        )

    nodes: list[dict[str, Any]] = []
    for category, label in L2_CATEGORIES.items():
        evidence = grouped[category]
        if evidence:
            description, triggers, contraindicated = induced[category]
        else:
            description = "当前材料中没有足够证据可靠归纳这一思维模式。"
            triggers = []
            contraindicated = []
        nodes.append(
            {
                "node_id": l2_id(scientist_id, category),
                "level": "L2",
                "category": category,
                "category_label": label,
                "description": description,
                "trigger_contexts": triggers,
                "contraindicated_contexts": contraindicated,
                "supporting_L1_count": len(evidence),
            }
        )
    counts = Counter(category_by_card.values())
    if sum(counts.values()) != len(cards):
        raise AssertionError("internal assignment count mismatch")
    return write_json(project_root / "l2" / f"{scientist_id}_l2.json", nodes)


def _nonempty_string(payload: Any, key: str) -> str:
    if not isinstance(payload, dict) or not str(payload.get(key, "")).strip():
        raise ValueError(f"LLM response requires non-empty '{key}'")
    return str(payload[key]).strip()


def _string_list(payload: Any, key: str) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise TypeError(f"LLM response requires '{key}' array")
    values = [str(item).strip() for item in payload[key]]
    if any(not item for item in values):
        raise ValueError(f"LLM '{key}' contains an empty value")
    return list(dict.fromkeys(values))


_CATEGORY_CONTEXT = re.compile(r"(?i)在\s*(C0[1-7])\s*中")
_CATEGORY = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(C0[1-7])(?![A-Za-z0-9_])"
)
_LAYER_NAME = re.compile(
    r"(?i)(?<![A-Za-z0-9_])L([123])(?![A-Za-z0-9_])"
)
_INTERNAL_L1 = re.compile(
    r"(?i)(?<![A-Za-z0-9_])l1_[a-z0-9_]+(?![A-Za-z0-9_])"
)


def _humanize_l2_description(
    text: str, cards: list[dict[str, Any]] | None = None
) -> str:
    def label(code: str) -> str:
        return L2_CATEGORIES[code.upper()]

    text = _CATEGORY_CONTEXT.sub(
        lambda match: f"在回答“{label(match.group(1))}”时",
        text,
    )
    text = _CATEGORY.sub(
        lambda match: f"“{label(match.group(1))}”",
        text,
    )
    if cards:
        cards_by_id = {str(card["card_id"]).lower(): card for card in cards}

        def replace_l1(match: re.Match[str]) -> str:
            card = cards_by_id.get(match.group(0).lower())
            if not card:
                return "一条原始论文证据"
            title = str(card.get("source_title") or "相关论文")
            observation = str(card.get("observation") or "具体研究观察").strip()
            return f"《{title}》中关于“{observation}”的原始证据"

        text = _INTERNAL_L1.sub(replace_l1, text)
    return _LAYER_NAME.sub(
        lambda match: {
            "1": "原始论文证据",
            "2": "七类科研思维模式",
            "3": "高层人格结论",
        }[match.group(1)],
        text,
    )
