"""Hybrid retrieval fusion (RRF) — pure offline (P0-B′b)."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.core.llm.providers import MockProvider
from omni.research.corpus import ingest_source, search_corpus
from omni.research.rerank import fuse_rankings, reciprocal_rank_fusion
from omni.research.store import ResearchStore
from omni.storage.db import get_database


async def _store() -> ResearchStore:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ResearchStore(db)


def test_rrf_rewards_agreement_across_lists():
    # ``a`` is #2 on BOTH lists; ``x`` / ``y`` each top exactly one list. With a
    # large k, showing up in both lists beats a single top placement.
    scores = reciprocal_rank_fusion([["x", "a"], ["y", "a"]], k=60)
    assert scores["a"] > scores["x"]
    assert scores["a"] > scores["y"]
    assert scores["x"] == pytest.approx(scores["y"])


def test_rrf_ignores_empty_ids_and_missing():
    scores = reciprocal_rank_fusion([["x", ""], ["x"]], k=1)
    # x: 1/(1+0) + 1/(1+0) = 2.0 ; empty id contributes nothing
    assert scores == {"x": pytest.approx(2.0)}
    assert "" not in scores


def test_fuse_rankings_sorted_desc():
    fused = fuse_rankings(["a", "b"], ["b", "a"], k=60)
    assert [cid for cid, _ in fused] == ["b", "a"] or [cid for cid, _ in fused] == ["a", "b"]
    assert fused[0][1] >= fused[1][1]


@pytest.mark.asyncio
async def test_hybrid_path_runs_with_embeddings():
    store = await _store()
    llm = MockProvider()  # deterministic hash embeddings → semantic signal present
    await ingest_source(
        store, llm,
        meta={"arxiv_id": "1706.03762", "title": "Attention Is All You Need"},
        full_text="The Transformer relies entirely on self-attention.\n\n"
                  "It dispenses with recurrence and convolutions entirely.",
    )
    await ingest_source(
        store, llm,
        meta={"arxiv_id": "1234.5678", "title": "Photosynthesis in plants"},
        full_text="Chloroplasts capture sunlight.\n\nThe Calvin cycle fixes carbon.",
    )
    hits = await search_corpus(store, llm, "self-attention transformer", k=3, hybrid=True)
    assert hits, "hybrid fusion must return passages"
    assert hits[0].arxiv_id == "1706.03762"


@pytest.mark.asyncio
async def test_hybrid_off_matches_single_signal_path():
    store = await _store()
    # No embeddings (llm=None) → hybrid has no semantic signal → identical to
    # the keyword-only path whether the flag is on or off.
    await ingest_source(
        store, None,
        meta={"arxiv_id": "1706.03762", "title": "Attention Is All You Need"},
        full_text="The Transformer relies entirely on self-attention.",
    )
    on = await search_corpus(store, None, "self-attention transformer", k=3, hybrid=True)
    off = await search_corpus(store, None, "self-attention transformer", k=3, hybrid=False)
    assert [p.chunk_id for p in on] == [p.chunk_id for p in off]
    assert on and on[0].arxiv_id == "1706.03762"
