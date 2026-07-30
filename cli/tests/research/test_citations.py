"""Citation graph: store edges, traversal, connectors, tool (P0-B′c)."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.research import connectors as conn
from omni.research.citations import traverse
from omni.research.corpus import ingest_source, link_references
from omni.research.store import ResearchStore
from omni.research.tools import build_research_tools
from omni.skills_runtime.context import ExecContext
from omni.storage.db import get_database


async def _store() -> ResearchStore:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ResearchStore(db)


async def _ctx() -> ExecContext:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ExecContext(
        settings=s, paths=s.paths, project=s.paths.project_name,
        session_id="sess1", channel="cli", db=db, llm=None,
    )


# ── store edges ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_add_citation_resolves_and_dedupes():
    store = await _store()
    citing = await store.add_source({"doi": "10.1/citer", "title": "Citer"})
    cited = await store.add_source({"doi": "10.1/cited", "title": "Cited"})
    edge = await store.add_citation(citing.id, {"doi": "10.1/cited", "title": "Cited"}, origin="manual")
    assert edge is not None
    assert edge.cited_source_id == cited.id  # resolved to an ingested source
    # same pair again → deduped (returns existing, no new row)
    again = await store.add_citation(citing.id, {"doi": "10.1/cited"}, origin="manual")
    assert again.id == edge.id
    assert (await store.counts())["citations"] == 1


@pytest.mark.asyncio
async def test_references_and_cited_by_directions():
    store = await _store()
    a = await store.add_source({"doi": "10.1/a", "title": "A"})
    b = await store.add_source({"doi": "10.1/b", "title": "B"})
    await store.add_citation(a.id, {"doi": "10.1/b", "title": "B"})
    refs = await store.references_of(a.id)
    cited = await store.cited_by(b.id)
    assert [e.cited_source_id for e in refs] == [b.id]
    assert [e.citing_source_id for e in cited] == [a.id]


@pytest.mark.asyncio
async def test_cite_before_ingest_resolves_later():
    store = await _store()
    citer = await store.add_source({"doi": "10.1/citer", "title": "Citer"})
    # Cite a work not yet in the corpus → edge stored by key, unresolved.
    edge = await store.add_citation(citer.id, {"doi": "10.1/future", "title": "Future"})
    assert edge.cited_source_id == ""
    # Now ingest that work and snap the edge onto it.
    future = await store.add_source({"doi": "10.1/future", "title": "Future"})
    resolved = await store.resolve_pending_citations(future.id)
    assert resolved == 1
    cited = await store.cited_by(future.id)
    assert [e.citing_source_id for e in cited] == [citer.id]


# ── traversal ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_traverse_two_hops_references():
    store = await _store()
    a = await store.add_source({"doi": "10.1/a", "title": "A"})
    b = await store.add_source({"doi": "10.1/b", "title": "B"})
    c = await store.add_source({"doi": "10.1/c", "title": "C"})
    await store.add_citation(a.id, {"doi": "10.1/b", "title": "B"})
    await store.add_citation(b.id, {"doi": "10.1/c", "title": "C"})

    one = await traverse(store, a.id, direction="references", depth=1)
    assert {n.source_id for n in one.nodes} == {b.id}
    two = await traverse(store, a.id, direction="references", depth=2)
    assert {n.source_id for n in two.nodes} == {b.id, c.id}
    assert {n.depth for n in two.nodes} == {1, 2}


@pytest.mark.asyncio
async def test_traverse_limit_bounds_output():
    store = await _store()
    a = await store.add_source({"doi": "10.1/a", "title": "A"})
    for i in range(5):
        await store.add_citation(a.id, {"doi": f"10.1/ref{i}", "title": f"Ref {i}"})
    hood = await traverse(store, a.id, direction="references", depth=1, limit=3)
    assert len(hood.nodes) == 3


# ── corpus glue ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_link_references_ingests_and_edges():
    store = await _store()
    citer = await ingest_source(store, None, meta={"doi": "10.1/citer", "title": "Citer"},
                                full_text="a study that builds on prior work")
    refs = [
        {"doi": "10.1/r1", "title": "Ref One", "summary": "foundational method"},
        {"doi": "10.1/r2", "title": "Ref Two", "summary": "benchmark dataset"},
    ]
    out = await link_references(store, None, citer["source_id"], refs, origin="openalex")
    assert out["edges"] == 2
    assert out["indexed"] == 2
    hood = await traverse(store, citer["source_id"], direction="references", depth=1)
    assert len(hood.nodes) == 2
    assert all(n.source_id for n in hood.nodes)  # references got ingested + resolved


# ── OpenAlex reference/cited-by connectors (offline via mocked HTTP) ─────────
@pytest.mark.asyncio
async def test_openalex_references_two_step(monkeypatch):
    work = {
        "id": "https://openalex.org/W100",
        "title": "Seed",
        "referenced_works": ["https://openalex.org/W1", "https://openalex.org/W2"],
    }
    batch = {"results": [
        {"id": "https://openalex.org/W1", "title": "Ref A", "publication_year": 2020},
        {"id": "https://openalex.org/W2", "title": "Ref B", "publication_year": 2021},
    ]}

    async def fake_get_json(url, params, **kwargs):
        if url.endswith("/W100"):
            return work
        assert params.get("filter", "").startswith("openalex_id:")
        return batch

    monkeypatch.setattr(conn, "_get_json", fake_get_json)
    refs = await conn.openalex_references("W100")
    assert [r["title"] for r in refs] == ["Ref A", "Ref B"]
    assert all(r["origin"] == "openalex" for r in refs)


@pytest.mark.asyncio
async def test_openalex_cited_by_uses_cites_filter(monkeypatch):
    seen: dict[str, str] = {}

    async def fake_get_json(url, params, **kwargs):
        if url.endswith("/W100"):
            return {"id": "https://openalex.org/W100", "title": "Seed"}
        seen["filter"] = params.get("filter", "")
        return {"results": [{"id": "https://openalex.org/W9", "title": "Citing Paper"}]}

    monkeypatch.setattr(conn, "_get_json", fake_get_json)
    citing = await conn.openalex_cited_by("W100")
    assert seen["filter"] == "cites:W100"
    assert citing[0]["title"] == "Citing Paper"


# ── tool ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_citation_neighbors_tool():
    ctx = await _ctx()
    tools = {t.spec.name: t for t in build_research_tools(ctx)}
    assert "citation_neighbors" in tools
    store = ResearchStore(ctx.db)
    a = await store.add_source({"doi": "10.1/a", "title": "Seed Paper"})
    await store.add_citation(a.id, {"doi": "10.1/b", "title": "Cited Work"})

    res = await tools["citation_neighbors"].handler({"source_id": a.id, "direction": "references"})
    assert res["status"] == "ok"
    assert res["direction"] == "references"
    assert res["nodes"][0]["title"] == "Cited Work"

    missing = await tools["citation_neighbors"].handler({"title": "nonexistent paper"})
    assert missing["status"] == "empty"
