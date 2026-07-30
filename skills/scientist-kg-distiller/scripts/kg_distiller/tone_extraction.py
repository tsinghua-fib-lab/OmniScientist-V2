from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .ids import l3_id
from .io_utils import read_json, read_jsonl, write_json
from .llm import JsonLLM
from .prompts import SYSTEM_JSON, tone_candidate_prompt, tone_selection_prompt

_INTRODUCTION = re.compile(
    r"(?im)^[ \t]*(?:#+[ \t]*)?(?:1(?:\.0)?\.?[ \t]+)?introduction[ \t]*$"
)
_NEXT_SECTION = re.compile(
    r"(?im)^[ \t]*(?:#+[ \t]*)?(?:2(?:\.0)?\.?[ \t]+)"
)
_UNNUMBERED_NEXT_SECTION = re.compile(
    r"(?im)^[ \t]*(?:#+[ \t]*)?"
    r"(?:related work|background|method(?:s|ology)?|approach)[ \t]*$"
)


def extract_tone_node(
    project_root: Path,
    scientist_id: str,
    llm: JsonLLM,
    *,
    request_concurrency: int | None = None,
) -> Path:
    source_path = (
        project_root
        / "scientist-corpus"
        / scientist_id
        / "source_objects.jsonl"
    )
    if not source_path.exists():
        raise FileNotFoundError(f"Missing SourceObjects: {source_path}")
    sources = read_jsonl(source_path)
    requests: list[dict[str, Any]] = []
    for source in sources:
        source_type = str(source.get("source_type") or "")
        full_text = str(source.get("full_text") or "")
        if source_type == "paper":
            passage = _introduction_text(full_text)
            passages = [passage] if passage else []
        elif source_type == "talk":
            passages = _text_chunks(full_text)
        else:
            passages = []
        for index, passage in enumerate(passages):
            prompt = tone_candidate_prompt(source, passage)
            requests.append(
                {
                    "source": source,
                    "passage": passage,
                    "chunk_index": index,
                    "prompt": prompt,
                    "request_key": _sha256(prompt),
                }
            )
    if not requests:
        raise ValueError(
            "P04 requires at least one paper introduction or talk transcript"
        )

    audit_path = project_root / "l3" / f"{scientist_id}.tone_attempts.json"
    audit_rows = read_json(audit_path) if audit_path.exists() else []
    cached = {
        row["request_key"]: row["response"]
        for row in audit_rows
        if isinstance(row, dict)
        and isinstance(row.get("request_key"), str)
        and "response" in row
    }
    responses = {
        item["request_key"]: cached[item["request_key"]]
        for item in requests
        if item["request_key"] in cached
    }
    pending = [
        item for item in requests if item["request_key"] not in responses
    ]
    concurrency = request_concurrency or int(
        os.environ.get("KG_DISTILLER_TONE_CONCURRENCY", "8")
    )
    if concurrency < 1:
        raise ValueError("request_concurrency must be at least 1")
    with ThreadPoolExecutor(
        max_workers=min(concurrency, len(pending) or 1)
    ) as executor:
        futures = {
            executor.submit(
                llm.complete_json,
                system=SYSTEM_JSON,
                user=item["prompt"],
            ): item
            for item in pending
        }
        for future in as_completed(futures):
            item = futures[future]
            response = future.result()
            responses[item["request_key"]] = response
            audit_rows.append(
                {
                    "kind": "candidate_extraction",
                    "source_id": item["source"]["source_id"],
                    "chunk_index": item["chunk_index"],
                    "request_key": item["request_key"],
                    "response": response,
                }
            )
            write_json(audit_path, audit_rows)

    candidates: list[str] = []
    seen: set[str] = set()
    for item in requests:
        payload = responses[item["request_key"]]
        values = (
            payload.get("tone_exemplars")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(values, list):
            raise TypeError("P04 candidate response requires tone_exemplars")
        full_text = str(item["source"]["full_text"])
        for value in values:
            if not isinstance(value, str) or not value:
                raise TypeError("P04 tone exemplar must be a non-empty string")
            exact_value = _exact_whitespace_variant(value, item["passage"])
            if exact_value is None or exact_value not in full_text:
                raise ValueError(
                    "P04 tone exemplar is not a verbatim SourceObject excerpt"
                )
            if exact_value not in seen:
                seen.add(exact_value)
                candidates.append(exact_value)
    if len(candidates) < 3:
        raise ValueError(
            f"P04 requires at least 3 verified tone candidates; got {len(candidates)}"
        )

    selection_prompt = tone_selection_prompt(candidates)
    selection_key = _sha256(selection_prompt)
    selection = cached.get(selection_key)
    if selection is None:
        selection = llm.complete_json(
            system=SYSTEM_JSON,
            user=selection_prompt,
        )
        audit_rows.append(
            {
                "kind": "final_selection",
                "request_key": selection_key,
                "response": selection,
            }
        )
        write_json(audit_path, audit_rows)
    selected_values = (
        selection.get("tone_exemplars")
        if isinstance(selection, dict)
        else None
    )
    exemplars = (
        [_exact_candidate(value, candidates) for value in selected_values]
        if isinstance(selected_values, list)
        and all(isinstance(value, str) for value in selected_values)
        else selected_values
    )
    if (
        not isinstance(exemplars, list)
        or not 3 <= len(exemplars) <= 5
        or any(not isinstance(value, str) for value in exemplars)
        or len(set(exemplars)) != len(exemplars)
        or any(value not in seen for value in exemplars)
    ):
        raise ValueError(
            "P04 final selection must contain 3-5 unique verbatim candidates"
        )
    node = {
        "node_id": l3_id(scientist_id, "P04"),
        "level": "L3",
        "question": "P04",
        "question_label": "语气",
        "tone_exemplars": exemplars,
    }
    return write_json(project_root / "l3" / f"{scientist_id}_tone.json", node)


def _exact_whitespace_variant(value: str, passage: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    if candidate in passage:
        return candidate
    tokens = re.split(r"\s+", candidate)
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    match = re.search(pattern, passage)
    if match is None:
        return None
    return match.group(0)


def _exact_candidate(value: str, candidates: list[str]) -> str | None:
    if value in candidates:
        return value
    normalized_value = _normalized_whitespace(value)
    matches = [
        candidate
        for candidate in candidates
        if _normalized_whitespace(candidate) == normalized_value
    ]
    return matches[0] if len(matches) == 1 else None


def _normalized_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _introduction_text(full_text: str) -> str | None:
    start = _INTRODUCTION.search(full_text)
    if not start:
        return _historical_opening_text(full_text)
    tail = full_text[start.end() :]
    numbered = _NEXT_SECTION.search(tail)
    unnumbered = _UNNUMBERED_NEXT_SECTION.search(tail)
    ends = [
        match.start()
        for match in (numbered, unnumbered)
        if match is not None
    ]
    return tail[: min(ends) if ends else len(tail)]


def _historical_opening_text(full_text: str) -> str | None:
    marker = re.search(r"(?im)^[ \t]*##[ \t]+Full Text[ \t]*$", full_text)
    body = full_text[marker.end() :] if marker else full_text
    body = body.lstrip()
    if not body:
        return None
    fourth_page = re.search(r"(?im)^[ \t]*##[ \t]+Page[ \t]+4[ \t]*$", body)
    if fourth_page:
        body = body[: fourth_page.start()]
    else:
        body = body[:12000]
    return body.strip() or None


def _text_chunks(
    text: str, *, chunk_size: int = 24000, overlap: int = 512
) -> list[str]:
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("invalid tone chunk configuration")
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
