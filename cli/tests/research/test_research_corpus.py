"""Literature corpus: chunking, ingest, grounded retrieval (offline)."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.core.llm.providers import MockProvider
from omni.research.corpus import chunk_text, ingest_source, search_corpus
from omni.research.store import ResearchStore
from omni.storage.db import get_database


async def _store() -> ResearchStore:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ResearchStore(db)


def test_chunk_text_paragraphs_and_long_split():
    short = "Para one.\n\nPara two."
    assert len(chunk_text(short, target_words=100)) == 1
    long = " ".join(f"w{i}" for i in range(500))
    chunks = chunk_text(long, target_words=100, overlap=20)
    assert len(chunks) >= 5
    assert all(c["tokens"] > 0 for c in chunks)
    assert [c["ord"] for c in chunks] == list(range(len(chunks)))


def test_chunk_text_empty():
    assert chunk_text("") == []


@pytest.mark.asyncio
async def test_ingest_and_keyword_search_ranks_relevant_passage():
    store = await _store()
    # No-embedding path → deterministic keyword-overlap ranking.
    await ingest_source(
        store, None,
        meta={"arxiv_id": "1706.03762", "title": "Attention Is All You Need"},
        full_text="The Transformer relies entirely on self-attention.\n\n"
                  "It dispenses with recurrence and convolutions entirely.",
    )
    await ingest_source(
        store, None,
        meta={"arxiv_id": "1234.5678", "title": "A paper about photosynthesis in plants"},
        full_text="Chloroplasts capture sunlight.\n\nThe Calvin cycle fixes carbon dioxide.",
    )
    hits = await search_corpus(store, None, "self-attention transformer recurrence", k=3)
    assert hits, "expected at least one passage"
    assert hits[0].arxiv_id == "1706.03762"
    assert hits[0].cite_label(1) == "S1"


@pytest.mark.asyncio
async def test_ingest_dedupes_chunks():
    store = await _store()
    meta = {"arxiv_id": "2310.06825", "title": "Mistral 7B"}
    r1 = await ingest_source(store, None, meta=meta, full_text="alpha beta gamma. " * 50)
    assert r1["chunks_added"] >= 1
    r2 = await ingest_source(store, None, meta=meta, full_text="should not re-add")
    assert r2["deduped"] is True and r2["chunks_added"] == 0


@pytest.mark.asyncio
async def test_search_with_embeddings_executes_cosine_path():
    store = await _store()
    llm = MockProvider()  # deterministic 256-dim hash embeddings
    await ingest_source(store, llm, meta={"title": "doc one"},
                        full_text="vector databases and retrieval augmented generation")
    hits = await search_corpus(store, llm, "retrieval augmented generation", k=2)
    assert hits  # cosine path runs and returns something


@pytest.mark.asyncio
async def test_as_of_filters_out_newer_sources():
    store = await _store()
    await ingest_source(store, None, meta={"title": "future paper"},
                        full_text="quantum widget breakthrough results here")
    # An as-of date far in the past should exclude the just-retrieved source.
    hits = await search_corpus(store, None, "quantum widget", k=5, as_of="2000-01-01")
    assert hits == []
