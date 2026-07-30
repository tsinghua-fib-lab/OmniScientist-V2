"""Select and display literature hits without a second hidden LLM call.

OpenAlex ``search=`` is keyword retrieval. Omni used to index every hit into
the corpus and tell the owner only ``indexed N works``. Codex's analog is a
visible tool observation: the model (and the owner) see the list and judge it.
This module keeps Omni's persistence, but:

* scores title+abstract against the query with cheap lexical overlap
* keeps the best ``keep`` hits and drops near-zero overlap
* formats a compact list for the skill summary the presentation layer already
  shows

A chat-completion rerank is intentionally omitted. The ReAct model is already
the semantic judge once the list is visible; a hidden second call would cost
tokens, break the portable runner, and silently drop papers.
"""

from __future__ import annotations

import re
from typing import Any

_LATIN = re.compile(r"[a-z0-9]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "into",
        "onto",
        "using",
        "based",
        "via",
        "are",
        "was",
        "were",
        "been",
        "have",
        "has",
        "had",
        "not",
        "but",
        "its",
        "our",
        "their",
        "your",
        "of",
        "in",
        "on",
        "or",
        "an",
        "is",
        "it",
        "to",
        "as",
        "at",
        "by",
        "be",
        "we",
        "do",
        "if",
        "so",
        "no",
        "up",
    }
)


def _stem(tok: str) -> str:
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _tokens(text: str) -> list[str]:
    raw = str(text or "").lower()
    out: list[str] = []
    for tok in _LATIN.findall(raw):
        if tok not in _STOP:
            out.append(tok)
    for span in _CJK.findall(raw):
        if len(span) == 1:
            out.append(span)
        else:
            out.extend(span[i : i + 2] for i in range(len(span) - 1))
    return out


def _stems(tokens: list[str]) -> set[str]:
    return {_stem(tok) for tok in tokens}


def _distinct_hits(query_tokens: list[str], paper: dict[str, Any]) -> int:
    haystack = _stems(
        _tokens(str(paper.get("title") or ""))
        + _tokens(str(paper.get("summary") or paper.get("abstract") or ""))
    )
    return sum(1 for tok in query_tokens if _stem(tok) in haystack)


def _title_has_query_bigram(query_tokens: list[str], paper: dict[str, Any]) -> bool:
    if len(query_tokens) < 2:
        return False
    title_text = " ".join(_tokens(str(paper.get("title") or "")))
    return any(
        f"{left} {right}" in title_text
        for left, right in zip(query_tokens, query_tokens[1:], strict=False)
    )


def paper_relevance(query: str, paper: dict[str, Any]) -> float:
    """Return 0–1+ lexical overlap of ``query`` against title and abstract."""
    q = _tokens(query)
    if not q:
        return 0.0
    title = _tokens(str(paper.get("title") or ""))
    abstract = _tokens(str(paper.get("summary") or paper.get("abstract") or ""))
    title_stems = _stems(title)
    body_stems = _stems(abstract)
    hits = 0.0
    for tok in q:
        stem = _stem(tok)
        if stem in title_stems:
            hits += 2.0
        elif stem in body_stems:
            hits += 1.0
    # Consecutive query bigrams in the title are a stronger signal than
    # scattered single tokens ("question answering" vs two unrelated words).
    if len(q) >= 2:
        title_text = " ".join(title)
        for left, right in zip(q, q[1:], strict=False):
            if f"{left} {right}" in title_text:
                hits += 1.5
    return hits / (2.0 * len(q))


def select_relevant_papers(
    query: str,
    papers: list[dict[str, Any]],
    *,
    keep: int,
    min_score: float = 0.12,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep the best on-topic hits; drop near-zero keyword collisions.

    Always returns at least one paper when ``papers`` is non-empty so an
    awkward query cannot empty a successful search. Queries that do not
    tokenise (one-letter English, punctuation) are returned unfiltered.
    """
    keep = max(1, int(keep or 1))
    if not papers:
        return [], []
    q = _tokens(query)
    if not q:
        return list(papers[:keep]), list(papers[keep:])
    ranked = sorted(
        ((paper_relevance(query, paper), index, paper) for index, paper in enumerate(papers)),
        key=lambda item: (-item[0], item[1]),
    )
    selected: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for score, _index, paper in ranked:
        # A long query that only shares one generic token ("clinical" in an
        # ICU guideline) is the OpenAlex keyword-collision case. Demand a
        # second token or a title bigram before we persist the hit.
        weak = (
            len(q) >= 3
            and _distinct_hits(q, paper) < 2
            and not _title_has_query_bigram(q, paper)
        )
        effective = 0.0 if weak else score
        if len(selected) < keep and (effective >= min_score or not selected):
            selected.append(paper)
        else:
            dropped.append(paper)
    return selected, dropped


def format_literature_hits(papers: list[dict[str, Any]], *, limit: int = 12) -> str:
    """Compact owner-visible list: year · title · venue."""
    lines: list[str] = []
    for index, paper in enumerate(papers[: max(0, int(limit))], start=1):
        title = " ".join(str(paper.get("title") or "").split()) or "(untitled)"
        year = str(paper.get("year") or "").strip()
        venue = " ".join(str(paper.get("venue") or "").split())
        head = f"{index}. {year} · {title}" if year else f"{index}. {title}"
        if venue:
            head = f"{head} ({venue})"
        if len(head) > 220:
            head = head[:217].rstrip() + "..."
        lines.append(head)
    return "\n".join(lines)


def fetch_window(keep: int, *, cap: int = 25) -> int:
    """How many keyword hits to pull before selecting ``keep``."""
    keep = max(1, int(keep or 1))
    return max(keep, min(int(cap), max(keep * 2, keep + 4)))


__all__ = [
    "fetch_window",
    "format_literature_hits",
    "paper_relevance",
    "select_relevant_papers",
]
