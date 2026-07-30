"""Native local-corpus Q&A after retirement of corpus-index/lit-qa skills."""

from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from omni.cli.main import app
from omni.config import load_settings
from omni.research.store import ResearchStore
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database

runner = CliRunner()


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
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ExecContext(settings=s, paths=s.paths, project=s.paths.project_name,
                       session_id="s1", channel="cli", db=db, llm=llm)


def test_active_literature_skills_are_registered():
    reg = SkillRegistry(load_settings())
    reg.build_index()
    for name in ("arxiv-fetch", "openalex-search"):
        e = reg.get(name)
        assert e is not None, f"{name} not indexed"
    assert reg.get("lit-qa") is None
    assert reg.get("corpus-index") is None


def test_omni_lit_empty_then_grounded():
    empty = runner.invoke(app, ["lit", "what is retrieval augmented generation"])
    assert empty.exit_code == 0
    assert "local corpus is empty" in empty.stdout
    assert "(empty response)" not in empty.stdout
    assert "openalex-search" in empty.stdout

    # Seed the corpus directly (offline), then ask again.
    async def _seed():
        from omni.research.corpus import ingest_source

        ctx = await _ctx(llm=None)
        await ingest_source(
            ResearchStore(ctx.db), None,
            meta={"arxiv_id": "2005.11401", "title": "Retrieval-Augmented Generation"},
            full_text="RAG combines a retriever and a generator for knowledge intensive tasks.",
        )

    _run(_seed())
    grounded = runner.invoke(app, ["lit", "retrieval augmented generation retriever"])
    assert grounded.exit_code == 0
    assert "S1" in grounded.stdout  # a citable passage was surfaced
    assert "Native grounded synthesis" in grounded.stdout
    assert "lit-qa skill was not found" not in grounded.stdout


def test_omni_lit_requires_question():
    res = runner.invoke(app, ["lit"])
    assert res.exit_code == 0
    assert "Usage" in res.stdout
