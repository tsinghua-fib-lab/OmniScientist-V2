"""Offline retrieval/grounding benchmark for the research subsystem.

A tiny, fully self-contained eval so progress on omni's *research* core is
measurable without any network or external dataset. It ingests a small bundled
corpus into a throwaway store, runs the real ``search_corpus`` pipeline, and
reports retrieval quality (recall@k, MRR). ``omni bench`` exposes it; the same
function backs the offline bench tests so regressions in chunking/ranking are
caught in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omni.research.corpus import ingest_source, search_corpus
from omni.research.store import ResearchStore

# A small, unambiguous gold set: each query has exactly one relevant document.
DEFAULT_DOCS: list[dict[str, str]] = [
    {"id": "d1", "title": "Transformer architecture",
     "text": "The transformer relies entirely on self-attention and multi-head "
             "attention, dispensing with recurrence and convolutions for sequence "
             "transduction."},
    {"id": "d2", "title": "Dropout regularization",
     "text": "Dropout is a regularization technique that randomly drops hidden "
             "units during training to prevent co-adaptation and reduce overfitting."},
    {"id": "d3", "title": "Batch normalization",
     "text": "Batch normalization normalizes layer inputs using minibatch "
             "statistics to stabilize and accelerate the training of deep networks."},
    {"id": "d4", "title": "Residual networks",
     "text": "Residual networks introduce identity skip connections so that very "
             "deep networks can be optimized without the degradation problem."},
    {"id": "d5", "title": "Adam optimizer",
     "text": "Adam is an adaptive optimizer that combines momentum with per-parameter "
             "learning-rate scaling from estimates of first and second gradient moments."},
]

DEFAULT_QUERIES: list[dict[str, Any]] = [
    {"q": "what mechanism do transformers use instead of recurrence", "rel": ["d1"]},
    {"q": "prevent overfitting by randomly dropping units during training", "rel": ["d2"]},
    {"q": "normalize layer inputs with minibatch statistics to speed up training", "rel": ["d3"]},
    {"q": "identity skip connections to train very deep networks", "rel": ["d4"]},
    {"q": "adaptive optimizer using first and second moment estimates of gradients", "rel": ["d5"]},
]


@dataclass
class BenchResult:
    n: int = 0
    k: int = 3
    hits: int = 0
    mrr: float = 0.0
    per_query: list[dict[str, Any]] = field(default_factory=list)

    @property
    def recall_at_k(self) -> float:
        return self.hits / self.n if self.n else 0.0


async def run_retrieval_bench(
    store: ResearchStore,
    llm: Any,
    *,
    docs: list[dict[str, str]] | None = None,
    queries: list[dict[str, Any]] | None = None,
    k: int = 3,
) -> BenchResult:
    """Ingest ``docs`` then evaluate ``search_corpus`` retrieval on ``queries``."""
    docs = docs or DEFAULT_DOCS
    queries = queries or DEFAULT_QUERIES
    id_map: dict[str, str] = {}
    for d in docs:
        res = await ingest_source(store, llm, meta={"title": d["title"]},
                                  full_text=d["text"], origin="bench")
        id_map[d["id"]] = res["source_id"]

    result = BenchResult(n=len(queries), k=k)
    rr_sum = 0.0
    for item in queries:
        gold = {id_map[r] for r in item["rel"] if r in id_map}
        passages = await search_corpus(store, llm, item["q"], k=k)
        rank = 0
        seen: list[str] = []
        for sid in (p.source_id for p in passages):
            if sid not in seen:
                seen.append(sid)
        for i, sid in enumerate(seen, 1):
            if sid in gold:
                rank = i
                break
        if rank:
            result.hits += 1
            rr_sum += 1.0 / rank
        result.per_query.append({"q": item["q"], "rank": rank, "hit": bool(rank)})
    result.mrr = rr_sum / result.n if result.n else 0.0
    return result


__all__ = ["BenchResult", "run_retrieval_bench", "DEFAULT_DOCS", "DEFAULT_QUERIES"]
