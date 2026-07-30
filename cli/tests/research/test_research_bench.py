"""M-F eval: offline retrieval benchmark + `omni bench` CLI (keyword path)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from omni.cli.main import app
from omni.config import load_settings
from omni.research.bench import DEFAULT_QUERIES, run_retrieval_bench
from omni.research.store import ResearchStore
from omni.storage.db import get_database

runner = CliRunner()


async def _store() -> ResearchStore:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ResearchStore(db)


@pytest.mark.asyncio
async def test_retrieval_bench_keyword_is_strong():
    store = await _store()
    try:
        result = await run_retrieval_bench(store, None, k=3)
        assert result.n == len(DEFAULT_QUERIES)
        # Keyword overlap should retrieve the right doc for most queries.
        assert result.recall_at_k >= 0.8
        assert 0.0 < result.mrr <= 1.0
        assert all("rank" in q for q in result.per_query)
    finally:
        from omni.storage.db import reset_databases

        await reset_databases()


def test_bench_cli_prints_scorecard():
    res = runner.invoke(app, ["bench", "--k", "3"])
    assert res.exit_code == 0
    assert "recall@3" in res.stdout
    assert "MRR" in res.stdout
