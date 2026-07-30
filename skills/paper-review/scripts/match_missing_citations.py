#!/usr/bin/env python3
"""Audit whether LLM-selected related papers appear in a paper's references."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import unicodedata
from collections.abc import Iterable


def normalize_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title or "").casefold()
    return re.sub(r"[\W_]+", " ", normalized, flags=re.UNICODE).strip()


def _author_name(author: object) -> str:
    if isinstance(author, dict):
        return str(author.get("name") or "")
    return str(author or "")


def _first_author(authors: Iterable[object] | None) -> str:
    authors = list(authors or [])
    if not authors:
        return ""
    name = _author_name(authors[0]).casefold().strip()
    return re.split(r"\s+", name)[-1] if name else ""


def _external_ids(item: dict) -> dict[str, str]:
    raw_ids = item.get("externalIds") or item.get("external_ids") or {}
    return {
        str(namespace).casefold(): str(value).casefold().strip()
        for namespace, value in raw_ids.items()
        if value
    }


def _title_similarity(left: str, right: str) -> float:
    normalized_left = normalize_title(left)
    normalized_right = normalize_title(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return difflib.SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _matching_external_id(left: dict, right: dict) -> str | None:
    left_ids = _external_ids(left)
    right_ids = _external_ids(right)
    for namespace, value in left_ids.items():
        if right_ids.get(namespace) == value:
            return namespace
    return None


def find_citation_match(candidate: dict, references: Iterable[dict]) -> dict | None:
    """Return deterministic match evidence, without judging scholarly relevance."""

    candidate_title = candidate.get("title") or ""
    normalized_candidate_title = normalize_title(candidate_title)
    candidate_year = candidate.get("year")
    candidate_author = _first_author(candidate.get("authors"))

    for reference in references:
        external_namespace = _matching_external_id(candidate, reference)
        if external_namespace:
            return {
                "method": f"external_id:{external_namespace}",
                "confidence": "exact",
                "matched_reference": reference,
            }

        reference_title = reference.get("title") or ""
        if normalized_candidate_title and normalized_candidate_title == normalize_title(reference_title):
            return {
                "method": "normalized_title",
                "confidence": "exact",
                "matched_reference": reference,
            }

        similarity = _title_similarity(candidate_title, reference_title)
        if similarity >= 0.92:
            return {
                "method": "fuzzy_title",
                "confidence": "high",
                "title_similarity": round(similarity, 3),
                "matched_reference": reference,
            }

        reference_author = _first_author(reference.get("authors"))
        same_year = False
        if candidate_year is not None and reference.get("year") is not None:
            try:
                same_year = int(candidate_year) == int(reference["year"])
            except (TypeError, ValueError):
                same_year = False
        if same_year and candidate_author and candidate_author == reference_author and similarity >= 0.78:
            return {
                "method": "author_year_and_fuzzy_title",
                "confidence": "medium",
                "title_similarity": round(similarity, 3),
                "matched_reference": reference,
            }

    return None


def is_candidate_cited(candidate: dict, references: Iterable[dict]) -> bool:
    return find_citation_match(candidate, references) is not None


def is_same_paper(candidate: dict, target_paper: dict | None) -> bool:
    if not target_paper:
        return False

    candidate_id = str(candidate.get("paperId") or candidate.get("paper_id") or "").casefold()
    target_id = str(target_paper.get("paperId") or target_paper.get("paper_id") or "").casefold()
    if candidate_id and target_id and candidate_id == target_id:
        return True

    if _matching_external_id(candidate, target_paper):
        return True

    candidate_title = candidate.get("title") or ""
    target_title = target_paper.get("title") or ""
    return bool(candidate_title and target_title and _title_similarity(candidate_title, target_title) >= 0.96)


def audit_citation_candidates(
    candidates: Iterable[dict],
    references: Iterable[dict],
    target_paper: dict | None = None,
) -> list[dict]:
    """Annotate every supplied candidate with deterministic citation status."""

    references = list(references)
    audited: list[dict] = []
    for candidate in candidates:
        enriched = dict(candidate)
        if is_same_paper(candidate, target_paper):
            enriched.update(
                {
                    "citation_status": "target_paper",
                    "cited": None,
                    "citation_match": None,
                }
            )
        else:
            match = find_citation_match(candidate, references)
            if match and match["confidence"] == "medium":
                citation_status = "possibly_cited"
                cited = None
            else:
                citation_status = "cited" if match else "not_cited"
                cited = bool(match)
            enriched.update(
                {
                    "citation_status": citation_status,
                    "cited": cited,
                    "citation_match": match,
                }
            )
        audited.append(enriched)
    return audited


def _candidate_list(payload: object) -> list[dict]:
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for key in ("top5", "top10", "candidates"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        else:
            raise ValueError("Candidates object must contain a top5, top10, or candidates list.")
    else:
        raise ValueError(  # noqa: TRY004 - malformed CLI JSON is one input-value error
            "Candidates must be a JSON list or an object containing top10/candidates."
        )
    if not all(isinstance(candidate, dict) for candidate in candidates):
        raise ValueError("Every candidate must be a JSON object.")
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit citation status for LLM-selected related papers."
    )
    parser.add_argument(
        "--candidates",
        required=True,
        help="JSON file containing the LLM top5 list (or a candidates list).",
    )
    parser.add_argument("--references", required=True, help="JSON file with extracted references.")
    parser.add_argument("--target-paper", help="JSON file with target paper metadata.")
    parser.add_argument("--target-title", help="Target paper title to identify self-matches.")
    parser.add_argument("--target-paper-id", help="Target Semantic Scholar paperId.")
    parser.add_argument("--output")
    args = parser.parse_args()

    with open(args.candidates, encoding="utf-8") as handle:
        candidate_payload = json.load(handle)
    candidates = _candidate_list(candidate_payload)

    with open(args.references, encoding="utf-8") as handle:
        references = json.load(handle)

    target_paper: dict = {}
    if isinstance(candidate_payload, dict):
        target_paper.update(candidate_payload.get("target_paper") or candidate_payload.get("target") or {})
    if args.target_paper:
        with open(args.target_paper, encoding="utf-8") as handle:
            target_paper.update(json.load(handle))
    if args.target_title:
        target_paper["title"] = args.target_title
    if args.target_paper_id:
        target_paper["paperId"] = args.target_paper_id

    result = audit_citation_candidates(
        candidates,
        references,
        target_paper=target_paper or None,
    )
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
