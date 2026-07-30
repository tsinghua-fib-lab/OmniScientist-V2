"""Evidence-table and notebook artifacts are generated from the research ledger."""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.config.paths import OmniPaths
from omni.research.artifacts import build_evidence_table, build_research_notebook
from omni.research.store import ResearchStore
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import Database


async def _stores(tmp_path: Path):  # noqa: ANN202
    paths = OmniPaths(
        home=tmp_path / "home",
        project_name="quality",
        project_dir=tmp_path / "home" / "projects" / "quality",
        workspace_root=tmp_path,
    )
    paths.ensure_dirs()
    db = Database(paths.project_db)
    await db.init()
    return db, ResearchStore(db), ArtifactStore(paths, db)


@pytest.mark.asyncio
async def test_evidence_table_exports_csv_and_markdown(tmp_path: Path):
    db, store, artifacts = await _stores(tmp_path)
    try:
        source = await store.add_source(
            {"title": "RAG paper", "doi": "10.1/rag"}, origin="test"
        )
        claim = await store.add_claim("Retrieval grounds generation", session_id="s1")
        evidence = await store.add_evidence(
            claim.id, source_id=source.id, quote="retrieved passages", locator="p. 3"
        )

        result = await build_evidence_table(
            store=store, artifacts=artifacts, session_id="s1", title="RAG evidence"
        )

        assert result["status"] == "ok"
        assert result["claim_ids"] == [claim.id]
        assert result["evidence_ids"] == [evidence.id]
        assert {item["format"] for item in result["artifacts"]} == {"csv", "markdown"}
        contents = [Path(item["path"]).read_text(encoding="utf-8") for item in result["artifacts"]]
        assert all("Retrieval grounds generation" in content for content in contents)
        assert any("RAG paper" in content for content in contents)
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_research_notebook_snapshots_full_ledger(tmp_path: Path):
    db, store, artifacts = await _stores(tmp_path)
    try:
        await store.add_hypothesis("Reranking improves factuality", session_id="s1")
        await store.add_source({"title": "Evidence source", "url": "https://example.test"})
        await store.add_claim("A tracked claim", session_id="s1")
        await store.add_run(
            title="Ablation", session_id="s1", cmd="python run.py", seed=7,
            metrics={"accuracy": 0.9}, status="succeeded",
        )

        result = await build_research_notebook(
            store=store, artifacts=artifacts, session_id="s1", title="Project snapshot"
        )
        path = Path(result["artifacts"][0]["path"])
        text = path.read_text(encoding="utf-8")

        assert result["status"] == "ok"
        assert "Reranking improves factuality" in text
        assert "Evidence source" in text
        assert "A tracked claim" in text
        assert "accuracy=0.9" in text
        assert "seed=7" in text
    finally:
        await db.dispose()
