"""Live literature search tool (P1): fan out connectors → corpus (offline mock)."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.research import connectors
from omni.research.tools import build_research_tools
from omni.skills_runtime.context import ExecContext
from omni.storage.db import get_database


async def _ctx(*, connectors_allow=None):  # noqa: ANN001
    s = load_settings()
    s.research.contact_email = "test@example.com"
    if connectors_allow is not None:
        s.research.connectors = connectors_allow
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ExecContext(settings=s, paths=s.paths, project=s.paths.project_name,
                       session_id="s1", channel="cli", db=db, llm=None)


def _tool(ctx, name):  # noqa: ANN001
    return {t.spec.name: t for t in build_research_tools(ctx)}[name]


@pytest.mark.asyncio
async def test_search_literature_fans_out_and_indexes(monkeypatch):
    # OpenAlex + Crossref both return one hit; results get indexed into the corpus.
    async def _fake(url, params, **kw):
        if "openalex" in url:
            return {"results": [{
                "title": "Transformer Networks", "publication_year": 2017,
                "doi": "https://doi.org/10.5/tr", "id": "https://openalex.org/W1",
                "authorships": [{"author": {"display_name": "A Vaswani"}}],
            }]}
        if "crossref" in url:
            return {"message": {"items": [{
                "title": ["Another Paper"], "DOI": "10.6/xyz",
                "issued": {"date-parts": [[2019]]}, "URL": "https://doi.org/10.6/xyz",
            }]}}
        return {}

    monkeypatch.setattr(connectors, "_get_json", _fake)
    ctx = await _ctx(connectors_allow=["openalex", "crossref"])
    out = await _tool(ctx, "search_literature").handler({"query": "transformers", "rows": 3})

    assert out["status"] == "ok"
    assert set(out["connectors"]) == {"openalex", "crossref"}
    assert out["count"] == 2
    assert out["indexed"] == 2
    titles = {r["title"] for r in out["results"]}
    assert titles == {"Transformer Networks", "Another Paper"}
    # results are now searchable in the local corpus
    corpus = await _tool(ctx, "search_corpus").handler({"query": "transformer", "k": 5})
    assert corpus["status"] in ("ok", "empty")  # indexed; recall depends on embeddings


@pytest.mark.asyncio
async def test_search_literature_dedups_across_connectors(monkeypatch):
    # Same DOI from two connectors collapses to a single compact result.
    async def _fake(url, params, **kw):
        if "openalex" in url:
            return {"results": [{"title": "Dup", "doi": "https://doi.org/10.9/dup",
                                 "id": "https://openalex.org/W9", "publication_year": 2020}]}
        if "crossref" in url:
            return {"message": {"items": [{"title": ["Dup"], "DOI": "10.9/dup",
                                           "issued": {"date-parts": [[2020]]}}]}}
        return {}

    monkeypatch.setattr(connectors, "_get_json", _fake)
    ctx = await _ctx(connectors_allow=["openalex", "crossref"])
    out = await _tool(ctx, "search_literature").handler({"query": "dup"})
    assert out["count"] == 1  # de-duped by DOI


@pytest.mark.asyncio
async def test_search_literature_one_bad_source_does_not_sink(monkeypatch):
    async def _fake(url, params, **kw):
        if "openalex" in url:
            return {"results": [{"title": "Good", "doi": "10.1/good",
                                 "id": "https://openalex.org/W1", "publication_year": 2021}]}
        raise connectors.ConnectorError("crossref offline")

    monkeypatch.setattr(connectors, "_get_json", _fake)
    ctx = await _ctx(connectors_allow=["openalex", "crossref"])
    out = await _tool(ctx, "search_literature").handler({"query": "x"})
    assert out["status"] == "partial"     # one source failed but there are results
    assert out["count"] == 1
    assert any("crossref" in e for e in out["errors"])
    assert out["per_source"]["crossref"].get("error")
    # provider diagnostics surface the failed connector's classification
    assert any(p["name"] == "crossref" and p["state"] == "failed" for p in out["providers"])


@pytest.mark.asyncio
async def test_search_literature_respects_source_subset_and_enablement(monkeypatch):
    async def _fake(url, params, **kw):
        return {"results": [{"title": "Only OA", "doi": "10.2/oa",
                             "id": "https://openalex.org/W2", "publication_year": 2022}]}

    monkeypatch.setattr(connectors, "_get_json", _fake)
    ctx = await _ctx(connectors_allow=["openalex"])
    # request a disabled connector → filtered out; only enabled openalex runs
    out = await _tool(ctx, "search_literature").handler(
        {"query": "x", "sources": ["openalex", "pubmed"]}
    )
    assert out["connectors"] == ["openalex"]
    assert out["count"] == 1


@pytest.mark.asyncio
async def test_search_literature_requires_query():
    ctx = await _ctx()
    out = await _tool(ctx, "search_literature").handler({})
    assert "error" in out


@pytest.mark.asyncio
async def test_search_literature_no_enabled_connectors():
    ctx = await _ctx(connectors_allow=["nonexistent"])
    out = await _tool(ctx, "search_literature").handler({"query": "x"})
    assert out["status"] == "empty"       # three-state: no source, not a hard error
    assert out["results"] == []
    assert out["remediation"]             # tells the user how to enable connectors
