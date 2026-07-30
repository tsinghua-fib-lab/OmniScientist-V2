"""M3 reproducibility: run ledger (tool → CLI) and as-of date-pinned retrieval."""

from __future__ import annotations

import asyncio
import re

from typer.testing import CliRunner

from omni.cli.main import app
from omni.config import load_settings
from omni.research.corpus import ingest_source
from omni.research.store import ResearchStore
from omni.research.tools import build_research_tools
from omni.skills_runtime.context import ExecContext
from omni.storage.db import get_database

runner = CliRunner()
_HEX8 = re.compile(r"[0-9a-f]{8}")


def _run(coro):
    async def _wrap():
        from omni.storage.db import reset_databases

        try:
            return await coro
        finally:
            await reset_databases()

    return asyncio.run(_wrap())


async def _ctx(*, as_of: str = "", llm=None) -> ExecContext:
    s = load_settings()
    s.research.as_of = as_of
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ExecContext(settings=s, paths=s.paths, project=s.paths.project_name,
                       session_id="s1", channel="cli", db=db, llm=llm)


def test_log_run_then_visible_via_cli():
    async def _seed():
        ctx = await _ctx()
        tools = {t.spec.name: t for t in build_research_tools(ctx)}
        return await tools["log_run"].handler(
            {"title": "ablation-A", "cmd": "python e.py", "seed": 13,
             "metrics": {"accuracy": 0.873}})

    res = _run(_seed())
    assert res["status"] == "ok"

    listed = runner.invoke(app, ["run", "list"])
    assert listed.exit_code == 0
    assert "ablation-A" in listed.stdout
    rid = _HEX8.search(listed.stdout).group(0)

    shown = runner.invoke(app, ["run", "show", rid])
    assert shown.exit_code == 0
    assert "13" in shown.stdout  # seed
    assert "accuracy" in shown.stdout
    assert "sha256:" in shown.stdout  # env lock captured


def test_as_of_pin_filters_search_corpus_tool():
    async def _go():
        ctx = await _ctx(as_of="2000-01-01")  # everything we add now is "after"
        await ingest_source(ResearchStore(ctx.db), None,
                            meta={"title": "fresh paper"},
                            full_text="novel widget achieves state of the art results")
        tools = {t.spec.name: t for t in build_research_tools(ctx)}
        pinned = await tools["search_corpus"].handler({"query": "widget"})

        ctx2 = await _ctx(as_of="")  # no pin → visible
        tools2 = {t.spec.name: t for t in build_research_tools(ctx2)}
        open_ = await tools2["search_corpus"].handler({"query": "widget"})
        return pinned, open_

    pinned, open_ = _run(_go())
    assert pinned["status"] == "empty"
    assert open_["status"] == "ok" and open_["matches"]
