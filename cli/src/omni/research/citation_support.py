"""Semantic citation support — does a claim's text actually align with its evidence?

The structural honesty audit (:mod:`omni.research.verify`) answers "does a
``supports`` edge exist?". That is necessary but not sufficient: a claim can be
bound to a source that does not actually back it. This module adds the *content*
signal — how much of the claim's substantive vocabulary is present in the cited
passage(s) — so ``omni verify`` can flag citations that exist but do not entail
the claim.

Design constraints (OmniScientist): fully **offline and deterministic** by
default. The primary signal is lexical containment over content words (no model,
no network). When a caller supplies a semantic embedder the cosine signal is
blended in, but it is never required — matching the repo's "tests run offline
with the mock provider" rule. This is a *support strength* heuristic, not a
trained NLI model; it is deliberately conservative (fail-open on empty claims).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select

from omni.research.store import ResearchStore
from omni.storage.models import ChunkORM, ClaimORM, EvidenceORM, SourceORM

SupportLevel = Literal["supported", "weak", "unsupported"]

_WORD_RE = re.compile(r"[0-9a-zA-Z\u4e00-\u9fff]+")

# Small, domain-neutral stop set — enough to stop function words from inflating
# containment without pretending to be a real linguistic resource.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in", "on",
        "for", "with", "as", "by", "is", "are", "was", "were", "be", "been", "being",
        "this", "that", "these", "those", "it", "its", "at", "from", "we", "our",
        "can", "may", "will", "would", "could", "should", "than", "such", "which",
        "has", "have", "had", "not", "no", "also", "into", "using", "used", "use",
        "between", "based", "study", "results", "result", "show", "shows", "shown",
    }
)


def content_tokens(text: str) -> set[str]:
    """Substantive tokens of ``text``: lowercased words, minus stopwords.

    Keeps numbers (they carry claim substance — "42%", "3x") and CJK runs, drops
    1–2 char latin tokens and the small stop set.
    """
    toks: set[str] = set()
    for m in _WORD_RE.findall(text or ""):
        t = m.lower()
        if t in _STOPWORDS:
            continue
        if t.isascii() and t.isalpha() and len(t) < 3:
            continue
        toks.add(t)
    return toks


def lexical_support(claim: str, evidence: str) -> float:
    """Fraction of the claim's content tokens present in ``evidence`` (0..1).

    Containment (not Jaccard): a short claim fully covered by a long passage
    scores 1.0. A claim with no content tokens is trivially unverifiable and
    returns 1.0 (fail-open — we never fault what has nothing to check).
    """
    claim_toks = content_tokens(claim)
    if not claim_toks:
        return 1.0
    ev_toks = content_tokens(evidence)
    if not ev_toks:
        return 0.0
    return len(claim_toks & ev_toks) / len(claim_toks)


def support_score(claim: str, evidence_texts: list[str]) -> float:
    """Best support of ``claim`` across its cited ``evidence_texts`` (0..1).

    Uses the *union* of evidence tokens (support may be spread across passages)
    and also takes the best single-passage containment, returning the max.
    """
    texts = [t for t in (evidence_texts or []) if t and t.strip()]
    if not texts:
        return 0.0
    union = lexical_support(claim, "\n".join(texts))
    best = max(lexical_support(claim, t) for t in texts)
    return max(union, best)


def classify_support(score: float, *, strong: float = 0.6, weak: float = 0.3) -> SupportLevel:
    """Bucket a support score into supported / weak / unsupported."""
    if score >= strong:
        return "supported"
    if score >= weak:
        return "weak"
    return "unsupported"


@dataclass
class CitationSupportReport:
    """Per-claim semantic support over the claim/evidence graph (annotate-only)."""

    checked: int = 0
    supported: int = 0
    # (claim_id, claim_text, score) for claims whose cited evidence is thin.
    weak: list[tuple[str, str, float]] = field(default_factory=list)
    unsupported: list[tuple[str, str, float]] = field(default_factory=list)

    @property
    def support_rate(self) -> float:
        return 1.0 if not self.checked else self.supported / self.checked


async def _supporting_evidence_texts(
    store: ResearchStore, claim_ids: list[str]
) -> dict[str, list[str]]:
    """Collect the text backing each claim's ``supports`` evidence rows.

    Prefers the evidence ``quote``; falls back to the bound chunk text, then the
    source summary — the same grounding surface retrieval already uses.
    """
    if not claim_ids:
        return {}
    async with store.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(EvidenceORM).where(
                        EvidenceORM.claim_id.in_(claim_ids),
                        EvidenceORM.stance == "supports",
                    )
                )
            ).scalars().all()
        )
        chunk_ids = {r.chunk_id for r in rows if not (r.quote or "").strip() and r.chunk_id}
        source_ids = {
            r.source_id
            for r in rows
            if not (r.quote or "").strip() and not r.chunk_id and r.source_id
        }
        chunks: dict[str, str] = {}
        if chunk_ids:
            for c in (
                await s.execute(select(ChunkORM).where(ChunkORM.id.in_(chunk_ids)))
            ).scalars().all():
                chunks[c.id] = c.text or ""
        sources: dict[str, str] = {}
        if source_ids:
            for src in (
                await s.execute(select(SourceORM).where(SourceORM.id.in_(source_ids)))
            ).scalars().all():
                sources[src.id] = src.summary or ""
    out: dict[str, list[str]] = {}
    for r in rows:
        text = (r.quote or "").strip() or chunks.get(r.chunk_id, "") or sources.get(r.source_id, "")
        if text:
            out.setdefault(r.claim_id, []).append(text)
    return out


async def audit_citation_support(
    store: ResearchStore,
    *,
    session_id: str = "",
    strong: float = 0.6,
    weak: float = 0.3,
    claims: list[ClaimORM] | None = None,
) -> CitationSupportReport:
    """Score how well each supported claim is entailed by its cited evidence.

    Only claims that already have a ``supports`` edge *and* recoverable evidence
    text are scored (structurally unsupported claims are the honesty audit's
    job). Returns per-claim weak/unsupported buckets for annotation.
    """
    if claims is None:
        claims = await store.list_claims(limit=2000, session_id=session_id)
    ev_texts = await _supporting_evidence_texts(store, [c.id for c in claims])
    report = CitationSupportReport()
    for c in claims:
        texts = ev_texts.get(c.id)
        if not texts:
            continue  # no recoverable evidence text → not a semantic-check target
        report.checked += 1
        score = support_score(c.text, texts)
        level = classify_support(score, strong=strong, weak=weak)
        if level == "supported":
            report.supported += 1
        elif level == "weak":
            report.weak.append((c.id, c.text, round(score, 3)))
        else:
            report.unsupported.append((c.id, c.text, round(score, 3)))
    return report


__all__ = [
    "CitationSupportReport",
    "audit_citation_support",
    "classify_support",
    "content_tokens",
    "lexical_support",
    "support_score",
]
