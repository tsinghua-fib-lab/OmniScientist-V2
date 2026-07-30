"""Hybrid retrieval fusion (pure-offline, no extra deps).

The corpus has two independent relevance signals: a *semantic* one (cosine over
embeddings) and a *lexical* one (keyword overlap). Each is strong where the
other is weak — embeddings catch paraphrase, keywords catch rare identifiers
(``BM25``, an arXiv id, a gene symbol). Rather than pick one per chunk (the old
behaviour), we fuse the two ranked lists with **Reciprocal Rank Fusion (RRF)**.

RRF is deliberately score-agnostic: it combines *ranks*, not raw scores, so the
tiny cosine numbers a hash-embedding mock produces and the 0–1 keyword overlaps
never need to be normalised onto the same scale. It is the same fusion Elastic /
Weaviate / the TREC literature use for hybrid search, and it needs nothing but
the standard library — so it works in the fully offline ``mock`` path too.
"""

from __future__ import annotations

# Standard RRF constant (Cormack et al. 2009). Larger ``k`` flattens the
# contribution of top ranks; 60 is the widely-used default.
DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    rankings: list[list[str]], *, k: int = DEFAULT_RRF_K
) -> dict[str, float]:
    """Fuse several ranked id lists into one score map via RRF.

    ``rankings`` is a list of id lists, each already ordered best→worst. An id's
    fused score is ``sum(1 / (k + rank))`` over the lists it appears in (rank is
    0-based), so appearing near the top of *several* lists beats topping a single
    one. Ids absent from a list simply contribute nothing for that list.
    """
    fused: dict[str, float] = {}
    kk = max(1, int(k))
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            if not item:
                continue
            fused[item] = fused.get(item, 0.0) + 1.0 / (kk + rank)
    return fused


def fuse_rankings(
    *ranked_lists: list[str], k: int = DEFAULT_RRF_K
) -> list[tuple[str, float]]:
    """RRF-fuse ranked id lists and return ``(id, score)`` sorted best→worst."""
    scores = reciprocal_rank_fusion(list(ranked_lists), k=k)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


__all__ = ["DEFAULT_RRF_K", "reciprocal_rank_fusion", "fuse_rankings"]
