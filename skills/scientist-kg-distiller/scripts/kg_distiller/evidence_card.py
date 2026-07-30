from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .ids import l1_id
from .io_utils import read_jsonl, write_jsonl
from .llm import JsonLLM
from .prompts import FACT_TYPES, SYSTEM_JSON, evidence_prompt
from .schemas import SCHEMA_VERSION, validate_evidence_card


def extract_evidence_cards(
    project_root: Path,
    scientist_id: str,
    llm: JsonLLM,
    *,
    batch_size: int = 1,
    chunk_size: int = 6_000,
    chunk_overlap: int = 400,
    request_concurrency: int | None = None,
) -> Path:
    source_path = (
        project_root / "scientist-corpus" / scientist_id / "source_objects.jsonl"
    )
    if not source_path.exists():
        raise FileNotFoundError(f"Missing Phase 1 output: {source_path}")
    sources = read_jsonl(source_path)
    cards: list[dict[str, Any]] = []
    seen_cards: set[tuple[str, str, str]] = set()
    audit_path = (
        project_root / "evidence_cards" / f"{scientist_id}.extraction_attempts.jsonl"
    )
    audit_rows = read_jsonl(audit_path) if audit_path.exists() else []
    audited_requests = {
        row["request_key"]: row
        for row in audit_rows
        if isinstance(row.get("request_key"), str) and "response" in row
    }
    concurrency = request_concurrency or int(
        os.environ.get("KG_DISTILLER_L1_CONCURRENCY", "8")
    )
    if concurrency < 1:
        raise ValueError("request_concurrency must be at least 1")
    requests: list[tuple[int, int, list[dict[str, Any]], str]] = []
    for start in range(0, len(sources), batch_size):
        source_batch = sources[start : start + batch_size]
        chunks_by_id = {
            source["source_id"]: _source_chunks(
                source,
                chunk_size=chunk_size,
                overlap=chunk_overlap,
            )
            for source in source_batch
        }
        rounds = max(len(chunks) for chunks in chunks_by_id.values())
        for round_index in range(rounds):
            chunk_batch = [
                chunks[round_index]
                for chunks in chunks_by_id.values()
                if round_index < len(chunks)
            ]
            request_key = _request_key(chunk_batch)
            requests.append((start, round_index, chunk_batch, request_key))

    pending = [request for request in requests if request[3] not in audited_requests]
    if pending:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(pending))) as executor:
            future_to_request = {
                executor.submit(
                    llm.complete_json,
                    system=SYSTEM_JSON,
                    user=evidence_prompt(chunk_batch),
                ): (start, round_index, chunk_batch, request_key)
                for start, round_index, chunk_batch, request_key in pending
            }
            for future in as_completed(future_to_request):
                start, round_index, chunk_batch, request_key = future_to_request[future]
                audit_row = {
                    "request_key": request_key,
                    "batch_start": start,
                    "round_index": round_index,
                    "chunks": [
                        {
                            "source_id": chunk["source_id"],
                            "chunk_start": chunk["chunk_start"],
                            "chunk_end": chunk["chunk_end"],
                        }
                        for chunk in chunk_batch
                    ],
                    "response": future.result(),
                    "matches": [],
                }
                audit_rows.append(audit_row)
                audited_requests[request_key] = audit_row
                write_jsonl(audit_path, audit_rows)

    for start, _, chunk_batch, request_key in requests:
        audit_row = audited_requests[request_key]
        payload = audit_row["response"]
        audit_row["matches"] = []
        audit_row["rejected_cards"] = []
        source_batch = sources[start : start + batch_size]
        chunks = {chunk["source_id"]: chunk for chunk in chunk_batch}
        sources_by_id = {source["source_id"]: source for source in source_batch}
        try:
            raw_cards = _list(payload, "cards")
        except (TypeError, ValueError) as exc:
            audit_row["rejected_response"] = {
                "reason": "invalid_cards_response_shape",
                "detail": str(exc),
            }
            write_jsonl(audit_path, audit_rows)
            continue
        for raw in raw_cards:
            source_id = raw.get("source_id")
            if source_id not in chunks:
                _reject_card(
                    audit_row,
                    raw,
                    f"unknown_source_id:{source_id}",
                )
                continue
            source = sources_by_id[source_id]
            chunk = chunks[source_id]
            proposed_excerpt = str(raw.get("excerpt", ""))
            if not proposed_excerpt:
                _reject_card(audit_row, raw, "empty_excerpt")
                continue
            chunk_text = chunk["full_text"]
            match = _locate_excerpt(chunk_text, proposed_excerpt)
            if match is None:
                _reject_card(audit_row, raw, "excerpt_not_verbatim")
                continue
            relative_start, relative_end, match_mode = match
            excerpt = chunk_text[relative_start:relative_end]
            audit_row["matches"].append(
                {
                    "source_id": source_id,
                    "proposed_excerpt": proposed_excerpt,
                    "stored_excerpt": excerpt,
                    "match_mode": match_mode,
                    "relative_start": relative_start,
                    "relative_end": relative_end,
                }
            )
            start_char = int(chunk["chunk_start"]) + relative_start
            end_char = int(chunk["chunk_start"]) + relative_end
            if source["full_text"][start_char:end_char] != excerpt:
                raise ValueError(
                    f"Evidence excerpt for {source_id} did not map back to full_text"
                )
            fact_type = raw.get("fact_type")
            if fact_type not in FACT_TYPES:
                _reject_card(audit_row, raw, f"unknown_fact_type:{fact_type}")
                continue
            observation = str(raw.get("observation", "")).strip()
            if not observation:
                _reject_card(audit_row, raw, "empty_observation")
                continue
            identity = (str(source_id), excerpt, observation)
            if identity in seen_cards:
                continue
            seen_cards.add(identity)
            location = dict(raw.get("location") or {})
            card = {
                "schema_version": SCHEMA_VERSION,
                "card_id": l1_id(scientist_id, len(cards) + 1),
                "source_id": source_id,
                "source_title": source["title"],
                "source_type": source["source_type"],
                "year": source.get("year"),
                "excerpt": excerpt,
                "location": {
                    "section": str(location.get("section") or "unknown"),
                    "start_char": start_char,
                    "end_char": end_char,
                },
                "observation": observation,
                "fact_type": fact_type,
                "author_role": source["author_role"],
            }
            validate_evidence_card(card)
            cards.append(card)
        write_jsonl(audit_path, audit_rows)
    output = project_root / "evidence_cards" / f"{scientist_id}.jsonl"
    return write_jsonl(output, cards)


def _reject_card(audit_row: dict[str, Any], raw: dict[str, Any], reason: str) -> None:
    audit_row["rejected_cards"].append(
        {
            "source_id": raw.get("source_id"),
            "proposed_excerpt": raw.get("excerpt"),
            "reason": reason,
        }
    )


def _request_key(chunks: list[dict[str, Any]]) -> str:
    request_identity = [
        {
            "source_id": chunk["source_id"],
            "chunk_start": chunk["chunk_start"],
            "chunk_end": chunk["chunk_end"],
            "text_sha256": hashlib.sha256(
                chunk["full_text"].encode("utf-8")
            ).hexdigest(),
        }
        for chunk in chunks
    ]
    canonical = json.dumps(
        request_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _locate_excerpt(text: str, proposed: str) -> tuple[int, int, str] | None:
    exact_start = text.find(proposed)
    if exact_start >= 0:
        return exact_start, exact_start + len(proposed), "exact"

    normalized_text, source_map = _normalize_pdf_text(text)
    normalized_proposed, _ = _normalize_pdf_text(proposed)
    if not normalized_proposed:
        return None
    normalized_start = normalized_text.find(normalized_proposed)
    if normalized_start < 0:
        return None
    normalized_end = normalized_start + len(normalized_proposed)
    return (
        source_map[normalized_start],
        source_map[normalized_end - 1] + 1,
        "normalized_pdf_layout",
    )


def _normalize_pdf_text(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    source_map: list[int] = []
    index = 0
    while index < len(text):
        if text[index] == "-" and index + 1 < len(text) and text[index + 1] == "\n":
            index += 2
            continue
        expanded = unicodedata.normalize("NFKC", text[index])
        for character in expanded:
            if character.isspace():
                if normalized and normalized[-1] != " ":
                    normalized.append(" ")
                    source_map.append(index)
            else:
                normalized.append(character)
                source_map.append(index)
        index += 1
    if normalized and normalized[-1] == " ":
        normalized.pop()
        source_map.pop()
    return "".join(normalized), source_map


def _list(payload: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise TypeError(f"LLM response must contain a '{key}' array")
    if not all(isinstance(item, dict) for item in payload[key]):
        raise TypeError(f"LLM '{key}' must contain objects")
    return payload[key]


def _source_chunks(
    source: dict[str, Any],
    *,
    chunk_size: int,
    overlap: int,
    max_chunks_per_source: int = 3,
) -> list[dict[str, Any]]:
    if chunk_size <= overlap or overlap < 0:
        raise ValueError("chunk_size must be greater than non-negative overlap")
    if max_chunks_per_source < 1:
        raise ValueError("max_chunks_per_source must be positive")
    text = str(source["full_text"])
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            boundary = text.rfind("\n\n", start + chunk_size // 2, end)
            if boundary > start:
                end = boundary
        chunk = {
            "source_id": source["source_id"],
            "title": source["title"],
            "year": source.get("year"),
            "source_type": source["source_type"],
            "authors": source.get("authors", []),
            "author_role": source["author_role"],
            "chunk_start": start,
            "chunk_end": end,
            "full_text": text[start:end],
        }
        chunks.append(chunk)
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    if len(chunks) <= max_chunks_per_source:
        return chunks
    if max_chunks_per_source == 1:
        return [chunks[0]]
    if max_chunks_per_source == 2:
        return [chunks[0], chunks[-1]]
    selected_indices = {0, len(chunks) // 2, len(chunks) - 1}
    return [chunk for index, chunk in enumerate(chunks) if index in selected_indices]
