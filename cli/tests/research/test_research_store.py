"""ROM store: sources (dedup), hypotheses/claims/evidence graph, runs."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.research.store import ResearchStore, source_dedup_key
from omni.storage.db import get_database
from omni.storage.models import ClaimORM, HypothesisORM, RunORM, SourceORM


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

    other = await store.add_hypothesis("Other session hypothesis", session_id="s2")
    await store.add_claim("Other claim", session_id="s2", hypothesis_id=other.id)
    scoped = await store.list_hypotheses(session_id="s1")
    assert [row.id for row in scoped] == [hyp.id]
    scoped_claims = await store.list_claims(session_id="s1")
    assert [row.id for row in scoped_claims] == [claim.id]
    scoped_sources = await store.list_sources(session_id="s1")
    assert [row.id for row in scoped_sources] == [src.id]
    counts = await store.counts(session_id="s1")
    assert counts["hypotheses"] == 1
    assert counts["claims"] == 1
    assert counts["sources"] == 1
    assert counts["evidence"] == 1


@pytest.mark.asyncio
async def test_hypothesis_prefix_clash_is_ambiguous():
    store = await _store()
    async with store._db.session() as session:
        session.add(HypothesisORM(id="aa11111111111111111111111111111111", statement="one"))
        session.add(HypothesisORM(id="aa22222222222222222222222222222222", statement="two"))
        await session.commit()
    row, ids = await store.resolve_hypothesis("aa")
    assert row is None
    assert set(ids) == {
        "aa11111111111111111111111111111111",
        "aa22222222222222222222222222222222",
    }
    assert await store.get_hypothesis("aa") is None
    exact, empty = await store.resolve_hypothesis("aa11111111111111111111111111111111")
    assert exact is not None and exact.statement == "one"
    assert empty == []


@pytest.mark.asyncio
async def test_source_claim_run_prefix_clash_is_ambiguous():
    store = await _store()
    async with store._db.session() as session:
        session.add(SourceORM(id="bb11111111111111111111111111111111", title="one"))
        session.add(SourceORM(id="bb22222222222222222222222222222222", title="two"))
        session.add(ClaimORM(id="cc11111111111111111111111111111111", text="one"))
        session.add(ClaimORM(id="cc22222222222222222222222222222222", text="two"))
        session.add(RunORM(id="dd11111111111111111111111111111111", title="one"))
        session.add(RunORM(id="dd22222222222222222222222222222222", title="two"))
        await session.commit()
    assert await store.get_source("bb") is None
    assert (await store.get_source("bb11111111111111111111111111111111")).title == "one"
    assert await store.get_claim("cc") is None
    assert (await store.get_claim("cc11111111111111111111111111111111")).text == "one"
    assert await store.get_run("dd") is None
    assert (await store.get_run("dd11111111111111111111111111111111")).title == "one"


@pytest.mark.asyncio
async def test_list_runs_filters_by_hypothesis():
    store = await _store()
    hyp = await store.add_hypothesis("h")
    other = await store.add_hypothesis("other")
    await store.add_run(title="a", hypothesis_id=hyp.id)
    await store.add_run(title="b", hypothesis_id=other.id)
    rows = await store.list_runs(hypothesis_id=hyp.id)
    assert [row.title for row in rows] == ["a"]


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
