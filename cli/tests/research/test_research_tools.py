"""Research builtin tools: record/cite/evidence/search/log_run + wiring."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.memory.library import load_library
from omni.research.store import ResearchStore
from omni.research.tools import build_research_tools, capture_env_lock
from omni.skills_runtime.builtin_tools import build_builtin_tools
from omni.skills_runtime.context import ExecContext
from omni.storage.db import get_database


async def _ctx(llm=None) -> ExecContext:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ExecContext(
        settings=s, paths=s.paths, project=s.paths.project_name,
        session_id="sess1", channel="cli", db=db, llm=llm,
    )


def _by_name(tools):
    return {t.spec.name: t for t in tools}


@pytest.mark.asyncio
async def test_tools_present_only_when_db_available():
    ctx = await _ctx()
    names = {t.spec.name for t in build_builtin_tools(ctx)}
    assert {"record_hypothesis", "record_claim", "cite_source",
            "add_evidence", "search_corpus", "log_run",
            "build_research_artifact"} <= names
    # DB-free context → no research tools (but baseline still there).
    bare = ExecContext(settings=ctx.settings, paths=ctx.paths, db=None)
    bare_names = {t.spec.name for t in build_builtin_tools(bare)}
    assert "record_claim" not in bare_names
    assert "web_fetch" in bare_names


@pytest.mark.asyncio
async def test_record_hypothesis_writes_store_and_notebook():
    ctx = await _ctx()
    tools = _by_name(build_research_tools(ctx))
    res = await tools["record_hypothesis"].handler(
        {"statement": "Sparse attention scales better", "confidence": 0.4})
    assert res["status"] == "ok"
    store = ResearchStore(ctx.db)
    hyps = await store.list_hypotheses()
    assert len(hyps) == 1 and hyps[0].confidence == 0.4
    assert ctx.paths.notebook.exists()
    assert "Hypothesis" in ctx.paths.notebook.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cite_source_also_writes_library():
    ctx = await _ctx()
    tools = _by_name(build_research_tools(ctx))
    res = await tools["cite_source"].handler(
        {"arxiv_id": "1706.03762", "title": "Attention Is All You Need",
         "authors": ["Vaswani"], "year": "2017"})
    assert res["status"] == "ok"
    assert res.get("observation", {}).get("created_refs")
    assert any(ref.startswith("source:") for ref in res["observation"]["created_refs"])
    lib = load_library(ctx.paths.library)
    assert any(e.get("arxiv_id") == "1706.03762" for e in lib)


@pytest.mark.asyncio
async def test_claim_evidence_flow_and_unknown_claim_error():
    ctx = await _ctx()
    tools = _by_name(build_research_tools(ctx))
    src = await tools["cite_source"].handler({"title": "Some source", "arxiv_id": "2401.00001"})
    claim = await tools["record_claim"].handler({"text": "X improves Y", "confidence": 0.7})
    ev = await tools["add_evidence"].handler(
        {"claim_id": claim["claim_id"], "source_id": src["source_id"],
         "stance": "supports", "quote": "we observe X improves Y"})
    assert ev["status"] == "ok"
    store = ResearchStore(ctx.db)
    got = await store.evidence_for_claim(claim["claim_id"])
    assert len(got) == 1

    bad = await tools["add_evidence"].handler({"claim_id": "nope"})
    assert "error" in bad


@pytest.mark.asyncio
async def test_search_corpus_tool_empty_then_populated():
    ctx = await _ctx(llm=None)
    tools = _by_name(build_research_tools(ctx))
    empty = await tools["search_corpus"].handler({"query": "anything"})
    assert empty["status"] == "empty"

    from omni.research.corpus import ingest_source
    await ingest_source(ResearchStore(ctx.db), None,
                        meta={"title": "RAG paper", "arxiv_id": "2005.11401"},
                        full_text="retrieval augmented generation combines a retriever and generator")
    hit = await tools["search_corpus"].handler({"query": "retrieval augmented generation"})
    assert hit["status"] == "ok" and hit["matches"]
    assert hit["matches"][0]["cite"] == "S1"


@pytest.mark.asyncio
async def test_log_run_captures_env_and_metrics():
    ctx = await _ctx()
    tools = _by_name(build_research_tools(ctx))
    res = await tools["log_run"].handler(
        {"title": "ablation", "cmd": "python e.py", "seed": 7, "metrics": {"auc": 0.9}})
    assert res["status"] == "ok"
    store = ResearchStore(ctx.db)
    runs = await store.list_runs()
    assert runs[0].seed == 7 and runs[0].metrics["auc"] == 0.9
    assert runs[0].env_lock.startswith("sha256:")


def test_capture_env_lock_is_deterministic():
    assert capture_env_lock() == capture_env_lock()
    assert capture_env_lock().startswith("sha256:")
