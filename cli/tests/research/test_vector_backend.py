"""sqlite-vec vector index wiring + pure-Python fallback (P0-B′a)."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.core.llm.providers import MockProvider
from omni.memory import vectors
from omni.research.corpus import ingest_source, search_corpus
from omni.research.store import ResearchStore
from omni.storage.db import get_database


async def _store() -> ResearchStore:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ResearchStore(db)


def test_backend_resolution():
    # ``none``/``off`` never use sqlite-vec regardless of availability.
    assert vectors.use_sqlite_vec("none") is False
    assert vectors.use_sqlite_vec("off") is False
    # ``auto`` / ``sqlite_vec`` track availability.
    expected = vectors.sqlite_vec_available()
    assert vectors.use_sqlite_vec("auto") is expected
    assert vectors.use_sqlite_vec("sqlite_vec") is expected


def test_similarity_scores_python_fallback_matches_cosine(monkeypatch):
    monkeypatch.setenv("OMNI_DISABLE_SQLITE_VEC", "1")
    monkeypatch.setattr(vectors, "_VEC_TRIED", False)
    monkeypatch.setattr(vectors, "_VEC_MOD", None)
    q = [1.0, 0.0, 0.0]
    cands = [("a", [1.0, 0.0, 0.0]), ("b", [0.0, 1.0, 0.0]), ("c", [])]
    scores = vectors.similarity_scores(q, cands, backend="auto")
    assert scores["a"] == pytest.approx(1.0)
    assert scores["b"] == pytest.approx(0.0)
    assert "c" not in scores  # empty vector skipped


@pytest.mark.skipif(not vectors.sqlite_vec_available(), reason="sqlite-vec not installed")
def test_similarity_scores_vec_matches_python():
    q = [0.2, 0.9, 0.1, 0.4]
    cands = [
        ("a", [0.2, 0.9, 0.1, 0.4]),   # identical → sim 1
        ("b", [0.9, 0.1, 0.2, 0.0]),   # different
        ("c", [0.1, 0.8, 0.2, 0.5]),   # similar
    ]
    vec = vectors.similarity_scores(q, cands, backend="sqlite_vec")
    py = vectors.similarity_scores(q, cands, backend="none")
    assert set(vec) == set(py)
    for cid in py:
        assert vec[cid] == pytest.approx(py[cid], abs=1e-4)
    # same ranking either way
    assert sorted(vec, key=vec.get, reverse=True) == sorted(py, key=py.get, reverse=True)


@pytest.mark.asyncio
async def test_search_corpus_none_backend_is_keyword_only():
    store = await _store()
    llm = MockProvider()
    await ingest_source(store, llm, meta={"arxiv_id": "1706.03762", "title": "Attention"},
                        full_text="The Transformer relies entirely on self-attention.")
    # ``none`` disables embeddings → keyword path still finds the passage.
    hits = await search_corpus(store, llm, "self-attention transformer", k=3,
                               vector_backend="none")
    assert hits and hits[0].arxiv_id == "1706.03762"


@pytest.mark.asyncio
async def test_search_corpus_vec_and_python_agree_on_ranking():
    store = await _store()
    llm = MockProvider()
    await ingest_source(store, llm, meta={"arxiv_id": "1706.03762", "title": "Attention"},
                        full_text="The Transformer relies entirely on self-attention mechanisms.")
    await ingest_source(store, llm, meta={"arxiv_id": "1234.5678", "title": "Photosynthesis"},
                        full_text="Chloroplasts capture sunlight in the Calvin cycle.")
    q = "self-attention transformer"
    vec_hits = await search_corpus(store, llm, q, k=3, vector_backend="auto")
    py_hits = await search_corpus(store, llm, q, k=3, vector_backend="none")
    assert vec_hits and py_hits
    assert vec_hits[0].arxiv_id == py_hits[0].arxiv_id == "1706.03762"
