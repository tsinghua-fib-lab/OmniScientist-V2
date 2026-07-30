"""M4 connectors: OpenAlex/Crossref/Unpaywall normalisers + skill engines (offline)."""

from __future__ import annotations

import asyncio

import pytest

from omni.config import load_settings
from omni.core.llm.providers import MockProvider
from omni.research import connectors
from omni.research.store import ResearchStore
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.executor import execute_skill
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database


def _run(coro):
    async def _wrap():
        from omni.storage.db import reset_databases

        try:
            return await coro
        finally:
            await reset_databases()

    return asyncio.run(_wrap())


async def _ctx(llm=None) -> ExecContext:
    s = load_settings()
    s.research.contact_email = "test@example.com"
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ExecContext(settings=s, paths=s.paths, project=s.paths.project_name,
                       session_id="s1", channel="cli", db=db, llm=llm)


# ── normalisers (no network) ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_openalex_normaliser(monkeypatch):
    async def _fake(url, params, **kw):
        return {"results": [{
            "title": "Attention Is All You Need", "publication_year": 2017,
            "doi": "https://doi.org/10.5/transformer",
            "authorships": [{"author": {"display_name": "A Vaswani"}}],
            "primary_location": {"source": {"display_name": "NeurIPS"}},
            "abstract_inverted_index": {"Hello": [0], "world": [1]},
            "id": "https://openalex.org/W1",
        }]}

    monkeypatch.setattr(connectors, "_get_json", _fake)
    out = await connectors.openalex_search("transformer", email="x@y.z")
    assert out[0]["title"].startswith("Attention")
    assert out[0]["year"] == "2017"
    assert out[0]["doi"] == "10.5/transformer"  # normalised
    assert out[0]["authors"] == ["A Vaswani"]
    assert out[0]["venue"] == "NeurIPS"
    assert out[0]["summary"] == "Hello world"  # inverted index reconstructed
    assert out[0]["origin"] == "openalex"


@pytest.mark.asyncio
async def test_crossref_normaliser_strips_jats(monkeypatch):
    async def _fake(url, params, **kw):
        return {"message": {"items": [{
            "title": ["A Title"], "author": [{"given": "Jane", "family": "Roe"}],
            "issued": {"date-parts": [[2019, 3]]}, "DOI": "10.6/abc",
            "URL": "https://doi.org/10.6/abc", "container-title": ["JMLR"],
            "abstract": "<jats:p>Real abstract text.</jats:p>",
        }]}}

    monkeypatch.setattr(connectors, "_get_json", _fake)
    out = await connectors.crossref_search("title")
    assert out[0]["authors"] == ["Jane Roe"]
    assert out[0]["year"] == "2019"
    assert out[0]["summary"] == "Real abstract text."  # JATS stripped
    assert out[0]["venue"] == "JMLR"


@pytest.mark.asyncio
async def test_unpaywall_normaliser_and_email_required(monkeypatch):
    async def _fake(url, params, **kw):
        return {"title": "OA Paper", "year": 2018, "is_oa": True, "oa_status": "gold",
                "best_oa_location": {"url_for_pdf": "https://x/pdf"},
                "journal_name": "PLOS", "z_authors": [{"given": "C", "family": "D"}]}

    monkeypatch.setattr(connectors, "_get_json", _fake)
    out = await connectors.unpaywall_lookup("https://doi.org/10.7/z", email="x@y.z")
    assert out["doi"] == "10.7/z" and out["is_oa"] is True
    assert out["url"] == "https://x/pdf" and out["origin"] == "unpaywall"

    with pytest.raises(connectors.ConnectorError):
        await connectors.unpaywall_lookup("10.7/z", email="")  # email required, no network


@pytest.mark.asyncio
async def test_pubmed_normaliser_two_step(monkeypatch):
    async def _fake(url, params, **kw):
        if url.endswith("esearch.fcgi"):
            return {"esearchresult": {"idlist": ["111", "222"]}}
        return {"result": {
            "uids": ["111", "222"],
            "111": {"title": "CRISPR base editing", "pubdate": "2020 Jan",
                    "authors": [{"name": "A Zhang"}], "fulljournalname": "Nature",
                    "articleids": [{"idtype": "doi", "value": "10.9/crispr"}]},
            "222": {"title": "Prime editing", "pubdate": "2021",
                    "authors": [{"name": "B Li"}], "source": "Cell", "articleids": []},
        }}

    monkeypatch.setattr(connectors, "_get_json", _fake)
    out = await connectors.pubmed_search("gene editing", email="x@y.z")
    assert [p["title"] for p in out] == ["CRISPR base editing", "Prime editing"]
    assert out[0]["doi"] == "10.9/crispr"
    assert out[0]["year"] == "2020" and out[0]["venue"] == "Nature"
    assert out[0]["url"].endswith("/111/") and out[0]["pmid"] == "111"
    assert out[0]["origin"] == "pubmed"


@pytest.mark.asyncio
async def test_pubmed_empty_idlist_short_circuits(monkeypatch):
    async def _fake(url, params, **kw):
        assert url.endswith("esearch.fcgi")  # esummary must NOT be called
        return {"esearchresult": {"idlist": []}}

    monkeypatch.setattr(connectors, "_get_json", _fake)
    assert await connectors.pubmed_search("nothing") == []


@pytest.mark.asyncio
async def test_semanticscholar_normaliser(monkeypatch):
    captured: dict = {}

    async def _fake(url, params, *, headers=None, **kw):
        captured["headers"] = headers
        return {"data": [{
            "title": "RAG survey", "abstract": "A survey of retrieval augmented generation.",
            "year": 2023, "authors": [{"name": "E Fan"}],
            "externalIds": {"DOI": "10.10/rag", "ArXiv": "2312.00001"},
            "venue": "ACL", "url": "https://s2/paper/1",
        }]}

    monkeypatch.setattr(connectors, "_get_json", _fake)
    out = await connectors.semanticscholar_search("rag", api_key="secret")
    assert out[0]["title"] == "RAG survey"
    assert out[0]["doi"] == "10.10/rag" and out[0]["arxiv_id"] == "2312.00001"
    assert out[0]["summary"].startswith("A survey")
    assert out[0]["origin"] == "semanticscholar"
    assert captured["headers"] == {"x-api-key": "secret"}  # api key passed as header


@pytest.mark.asyncio
async def test_biorxiv_recent_search_filters_and_normalises(monkeypatch):
    async def _fake(url, params, **kw):  # noqa: ARG001
        assert "/details/biorxiv/30d/0/json" in url
        return {"collection": [
            {"title": "Protein language models", "abstract": "binding site prediction",
             "category": "bioinformatics", "authors": "A One; B Two",
             "date": "2026-01-03", "doi": "10.1101/2026.01.01.123", "version": "2"},
            {"title": "Unrelated ecology", "abstract": "forest dynamics"},
        ]}

    monkeypatch.setattr(connectors, "_get_json", _fake)
    out = await connectors.biorxiv_search("protein language", rows=5)

    assert len(out) == 1
    assert out[0]["authors"] == ["A One", "B Two"]
    assert out[0]["year"] == "2026"
    assert out[0]["origin"] == "biorxiv"


@pytest.mark.asyncio
async def test_clinicaltrials_v2_normaliser(monkeypatch):
    async def _fake(url, params, **kw):  # noqa: ARG001
        assert url.endswith("/api/v2/studies")
        assert params["query.term"] == "glioblastoma vaccine"
        return {"studies": [{
            "hasResults": True,
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT01234567", "briefTitle": "Vaccine trial",
                    "organization": {"fullName": "Example University"},
                },
                "statusModule": {
                    "overallStatus": "RECRUITING", "startDateStruct": {"date": "2025-04"},
                },
                "designModule": {"phases": ["PHASE2"]},
                "conditionsModule": {"conditions": ["Glioblastoma"]},
                "descriptionModule": {"briefSummary": "A randomized vaccine study."},
                "armsInterventionsModule": {"interventions": [{"name": "Vaccine X"}]},
                "outcomesModule": {"primaryOutcomes": [{"measure": "Overall survival"}]},
            },
        }]}

    monkeypatch.setattr(connectors, "_get_json", _fake)
    out = await connectors.clinicaltrials_search("glioblastoma vaccine")

    assert out[0]["nct_id"] == "NCT01234567"
    assert out[0]["status"] == "RECRUITING"
    assert out[0]["phases"] == ["PHASE2"]
    assert out[0]["primary_outcomes"] == ["Overall survival"]
    assert out[0]["has_results"] is True


# ── skill engines (offline via monkeypatch of the connector fn) ────────────
def test_connector_skills_registered():
    reg = SkillRegistry(load_settings())
    reg.build_index()
    assert reg.get("openalex-search") is not None


def test_openalex_engine_indexes_into_corpus(monkeypatch):
    async def _fake_search(query, *, rows=8, email=""):
        return [{"title": "RAG", "authors": ["L"], "year": "2020", "doi": "10.9/rag",
                 "url": "https://doi.org/10.9/rag", "venue": "NeurIPS",
                 "summary": "retrieval augmented generation for knowledge intensive nlp",
                 "origin": "openalex"}]

    monkeypatch.setattr(connectors, "openalex_search", _fake_search)

    async def _go():
        reg = SkillRegistry(load_settings())
        reg.build_index()
        ctx = await _ctx(llm=MockProvider())
        res = await execute_skill(reg.get("openalex-search"), {"query": "rag"}, ctx)
        chunks = await ResearchStore(ctx.db).chunk_count()
        return res, chunks

    res, chunks = _run(_go())
    assert res["status"] == "ok" and res["indexed"] == 1 and chunks >= 1


@pytest.mark.asyncio
async def test_chunk_target_words_setting_is_honored():
    """research.chunk_target_words must actually change ingest chunking."""
    from omni.research.engine_util import index_results

    long_summary = " ".join(f"word{i}" for i in range(60))
    ctx = await _ctx(llm=None)
    try:
        ctx.settings.research.chunk_target_words = 40
        small = await index_results(ctx, [{"title": "small-target", "summary": long_summary}],
                                    origin="openalex")
        ctx.settings.research.chunk_target_words = 1000
        big = await index_results(ctx, [{"title": "big-target", "summary": long_summary}],
                                  origin="openalex")
        assert small["sources"][0]["chunks"] > 1   # small target → multiple chunks
        assert big["sources"][0]["chunks"] == 1     # large target → single chunk
    finally:
        from omni.storage.db import reset_databases

        await reset_databases()


def test_connector_registry_enablement_semantics():
    from omni.research.registry import ConnectorRegistry

    s = load_settings()
    s.research.connectors = []  # empty allow-list → all curated connectors enabled
    reg = ConnectorRegistry(s)
    assert {c.name for c in reg.enabled()} == {
        "arxiv", "openalex", "crossref", "unpaywall", "pubmed", "semanticscholar",
        "biorxiv", "clinicaltrials",
    }
    assert reg.is_enabled("openalex") is True
    assert reg.is_enabled("not-a-connector") is False

    s.research.connectors = ["arxiv", "openalex"]  # explicit allow-list
    reg = ConnectorRegistry(s)
    assert reg.is_enabled("openalex") is True
    assert reg.is_enabled("crossref") is False
    assert {c.name for c in reg.enabled()} == {"arxiv", "openalex"}


def test_connector_registry_secret_scope_is_isolated():
    from omni.research.registry import ConnectorRegistry

    s = load_settings()
    s.research.contact_email = "me@example.com"
    reg = ConnectorRegistry(s)

    # A connector that declares contact_email in its scope receives it…
    unpaywall = reg.resolve("unpaywall")
    assert unpaywall is not None
    assert unpaywall.secrets == {"contact_email": "me@example.com"}

    # …but a connector with no secret scope receives nothing, even though the
    # contact email is configured (secret-scope isolation).
    arxiv = reg.resolve("arxiv")
    assert arxiv is not None
    assert arxiv.secrets == {}


def test_connector_killswitch_disables_engine(monkeypatch):
    async def _fake_search(query, *, rows=8, email=""):
        return [{"title": "x", "summary": "y", "origin": "openalex"}]

    monkeypatch.setattr(connectors, "openalex_search", _fake_search)

    async def _go():
        reg = SkillRegistry(load_settings())
        reg.build_index()
        ctx = await _ctx(llm=None)
        ctx.settings.research.connectors = ["arxiv"]  # openalex disabled
        return await execute_skill(reg.get("openalex-search"), {"query": "x"}, ctx)

    res = _run(_go())
    assert res["status"] == "error" and "disabled" in res["error"]
