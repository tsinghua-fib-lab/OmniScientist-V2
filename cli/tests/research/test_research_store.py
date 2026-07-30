"""ROM store: sources (dedup), hypotheses/claims/evidence graph, runs."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.research.store import ResearchStore, source_dedup_key
from omni.storage.db import get_database


async def _store() -> ResearchStore:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ResearchStore(db)


def test_source_dedup_key_prefers_arxiv_then_doi_then_url_then_title():
    assert source_dedup_key({"arxiv_id": "2310.06825", "doi": "10.x"}) == "arxiv_id:2310.06825"
    assert source_dedup_key({"doi": "10.1/AB"}) == "doi:10.1/ab"
    assert source_dedup_key({"url": "https://X/p"}) == "url:https://x/p"
    assert source_dedup_key({"title": "Hello  World"}) == "title:hello world"


@pytest.mark.asyncio
async def test_add_source_dedupes():
    store = await _store()
    a = await store.add_source({"arxiv_id": "2310.06825", "title": "Mistral 7B",
                                "authors": ["A", "B"], "published": "2023-10-10"})
    b = await store.add_source({"arxiv_id": "2310.06825", "title": "dup"})
    assert a.id == b.id
    assert a.year == "2023"
    assert a.authors == ["A", "B"]
    sources = await store.list_sources()
    assert len(sources) == 1


@pytest.mark.asyncio
async def test_hypothesis_claim_evidence_graph():
    store = await _store()
    src = await store.add_source({"arxiv_id": "1706.03762", "title": "Attention Is All You Need"})
    hyp = await store.add_hypothesis("Transformers beat RNNs on long context",
                                     session_id="s1", confidence=0.4)
    claim = await store.add_claim("Self-attention removes recurrence",
                                  session_id="s1", hypothesis_id=hyp.id, confidence=0.8)
    ev = await store.add_evidence(claim.id, source_id=src.id, stance="supports",
                                  quote="The Transformer ... dispensing with recurrence",
                                  locator="p.2")
    assert ev.stance == "supports"
    got = await store.evidence_for_claim(claim.id)
    assert len(got) == 1 and got[0].source_id == src.id

    updated = await store.set_hypothesis_status(hyp.id, "supported", confidence=0.9)
    assert updated.status == "supported" and updated.confidence == 0.9

    # prefix resolution
    assert (await store.get_claim(claim.id[:8])).id == claim.id


@pytest.mark.asyncio
async def test_runs_and_counts():
    store = await _store()
    run = await store.add_run(title="ablation", session_id="s1", seed=7,
                              cmd="python exp.py", metrics={"auc": 0.91}, status="succeeded")
    assert run.metrics["auc"] == 0.91
    assert run.finished_at is not None
    runs = await store.list_runs(session_id="s1")
    assert len(runs) == 1

    await store.add_hypothesis("h", session_id="s1")
    counts = await store.counts()
    assert counts["runs"] == 1 and counts["hypotheses"] == 1
    assert counts["sources"] == 0


@pytest.mark.asyncio
async def test_clamp_confidence_bounds():
    store = await _store()
    hyp = await store.add_hypothesis("x", confidence=5.0)
    assert hyp.confidence == 1.0
    claim = await store.add_claim("y", confidence=-2.0)
    assert claim.confidence == 0.0
