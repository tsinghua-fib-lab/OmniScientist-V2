#!/usr/bin/env python3
"""Retrieve and deduplicate Semantic Scholar candidates from LLM-authored queries."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path

FIELDS = (
    "paperId,title,abstract,authors,year,venue,url,externalIds,citationCount,"
    "influentialCitationCount,fieldsOfStudy"
)
MAX_QUERIES = 8
MAX_QUERY_CHARS = 500


def normalize_text(text: str) -> str:
    """Normalize text for identity checks without making semantic decisions."""

    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\W_]+", " ", normalized, flags=re.UNICODE).strip()


def validate_queries(
    queries: Iterable[str],
    max_queries: int = MAX_QUERIES,
    max_query_chars: int = MAX_QUERY_CHARS,
) -> list[str]:
    """Validate and deduplicate queries authored by the active LLM."""

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_query in queries:
        query = re.sub(r"\s+", " ", str(raw_query or "")).strip()
        if not query:
            continue
        if len(query) > max_query_chars:
            raise ValueError(f"Search query exceeds {max_query_chars} characters.")
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(query)

    if not cleaned:
        raise ValueError("Provide at least one non-empty LLM-generated search query.")
    if len(cleaned) > max_queries:
        raise ValueError(f"Provide at most {max_queries} unique search queries.")
    return cleaned


def _candidate_key(candidate: dict) -> str:
    external = candidate.get("externalIds") or {}
    for key in ("DOI", "ArXiv", "ACL"):
        value = external.get(key)
        if value:
            return f"{key.casefold()}:{str(value).casefold().strip()}"
    paper_id = candidate.get("paperId")
    if paper_id:
        return f"paper:{str(paper_id).casefold()}"
    return f"title:{normalize_text(candidate.get('title') or '')}"


def deduplicate_candidates(candidates: Iterable[dict]) -> list[dict]:
    """Deduplicate papers by stable identifier and normalized title."""

    deduped: list[dict] = []
    by_key: dict[str, dict] = {}
    by_title: dict[str, dict] = {}

    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        title = normalize_text(candidate.get("title") or "")
        if not title:
            continue
        key = _candidate_key(candidate)
        existing = by_key.get(key) or by_title.get(title)
        if existing is not None:
            existing_queries = existing.setdefault("retrievalQueries", [])
            for query in candidate.get("retrievalQueries") or []:
                if query not in existing_queries:
                    existing_queries.append(query)
            by_key[key] = existing
            by_title[title] = existing
            continue

        candidate["retrievalQueries"] = list(candidate.get("retrievalQueries") or [])
        deduped.append(candidate)
        by_key[key] = candidate
        by_title[title] = candidate

    return deduped


def _retry_delay(exc: urllib.error.HTTPError, sleep_seconds: float, attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    if retry_after:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    return max(sleep_seconds, 1.0) * attempt


def search_semantic_scholar(
    query: str,
    limit: int = 20,
    sleep_seconds: float = 1.0,
    max_retries: int = 2,
) -> list[dict]:
    params = urllib.parse.urlencode({"query": query, "limit": str(limit), "fields": FIELDS})
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}"
    headers = {}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(1, max_retries + 2):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if sleep_seconds:
                time.sleep(sleep_seconds)
            return payload.get("data") or []
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt > max_retries:
                raise
            time.sleep(_retry_delay(exc, sleep_seconds, attempt))
    return []


def _round_robin(groups: Iterable[list[dict]]) -> list[dict]:
    groups = list(groups)
    if not groups:
        return []
    interleaved: list[dict] = []
    for index in range(max(len(group) for group in groups)):
        for group in groups:
            if index < len(group):
                interleaved.append(group[index])
    return interleaved


def retrieve_related_papers(
    queries: Iterable[str],
    target_count: int = 20,
    per_query_limit: int = 50,
) -> dict:
    """Retrieve candidates without applying script-level semantic ranking."""

    if target_count < 1:
        raise ValueError("target_count must be positive.")
    if per_query_limit < 1:
        raise ValueError("per_query_limit must be positive.")

    validated_queries = validate_queries(queries)
    result_groups: list[list[dict]] = []
    errors: list[str] = []
    raw_count = 0

    for query in validated_queries:
        try:
            results = search_semantic_scholar(query, limit=per_query_limit)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - per-query isolation
            errors.append(f"{query}: {exc}")
            results = []
        raw_count += len(results)
        result_groups.append(
            [dict(candidate, retrievalQueries=[query]) for candidate in results]
        )

    candidates = deduplicate_candidates(_round_robin(result_groups))[:target_count]
    return {
        "queries": validated_queries,
        "raw_count": raw_count,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "errors": errors,
        "retrieval_limited": bool(errors) or len(candidates) < target_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retrieve Semantic Scholar candidates from active-LLM search queries."
    )
    parser.add_argument(
        "--query",
        action="append",
        required=True,
        help="A literature-search query generated by the active LLM; repeat for multiple queries.",
    )
    parser.add_argument("--target-count", type=int, default=20)
    parser.add_argument("--per-query-limit", type=int, default=50)
    parser.add_argument("--output")
    args = parser.parse_args()

    result = retrieve_related_papers(
        args.query,
        target_count=args.target_count,
        per_query_limit=args.per_query_limit,
    )
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        # The instructions send this into the staging directory, which the run
        # does not create until the section-writing step that comes after.
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
