"""scientific-figure skill: structured artifacts + research provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omni.config import load_settings
from omni.research.store import ResearchStore
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.executor import execute_skill
from omni.skills_runtime.manifest import SkillKind
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database


@pytest.mark.asyncio
async def test_scientific_figure_records_research_artifacts_and_run():
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    registry = SkillRegistry(settings)
    registry.build_index()
    entry = registry.get("scientific-figure")
    assert entry is not None
    assert entry.kind == SkillKind.PYTHON_ENGINE

    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        project=settings.paths.project_name,
        session_id="sess-figure",
        channel="cli",
        db=db,
        artifacts=ArtifactStore(settings.paths, db),
        registry=registry,
    )
    events: list[str] = []

    async def progress(stage: str, pct: float = 0.0, **_data) -> None:
        events.append(stage)

    out = await execute_skill(
        entry,
        {
            "input": "帮我实现产出一个 transformer 的架构图。",
            "title": "Transformer 架构图",
            "figure_kind": "transformer",
        },
        ctx,
        progress_callback=progress,
    )

    assert out["status"] == "ok"
    assert out["title"] == "Transformer 架构图"
    assert out["run_id"]
    assert any(a["format"] == "dot" for a in out["artifacts"])
    assert any(a["format"] == "svg" for a in out["artifacts"])
    assert any(a["format"] == "json" for a in out["artifacts"])
    assert all(a.get("uri", "").startswith("artifact://") for a in out["artifacts"])
    assert out["figure_bundle"]["status"] == "passed"
    assert out["figure_bundle"]["manifest_uri"].startswith("artifact://")
    assert out["figure_bundle"]["verification"]["passed"] is True
    assert "record provenance" in events

    # A self-contained provenance.json is emitted beside the figure, binding it
    # to its inputs, the research run, and the verified bundle manifest.
    prov = next((a for a in out["artifacts"] if a["path"].endswith(".provenance.json")), None)
    assert prov is not None
    doc = json.loads(Path(prov["path"]).read_text(encoding="utf-8"))
    assert doc["schema"] == "omni.figure_provenance/v1"
    assert doc["figure_bundle"]["status"] == "passed"
    assert doc["research"]["run_id"] == out["run_id"]
    assert doc["files"]  # references the rendered artifacts

    store = ResearchStore(db)
    counts = await store.counts()
    assert counts["sources"] >= 1
    assert counts["claims"] >= 5
    assert counts["evidence"] >= 5
    assert counts["runs"] >= 1

    sources = await store.list_sources()
    assert any(s.arxiv_id == "1706.03762" for s in sources)
    runs = await store.list_runs(session_id="sess-figure")
    assert out["run_id"] == runs[0].id
    # The run records the rendered outputs; the provenance.json is a meta-record
    # written afterward (it references the run), so it is intentionally excluded.
    render_uris = {a["uri"] for a in out["artifacts"] if a["format"] in {"dot", "svg", "png"}}
    assert set(runs[0].output_uris) >= render_uris


@pytest.mark.asyncio
async def test_scientific_figure_rag_request_uses_rag_nodes_not_transformer_template():
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    registry = SkillRegistry(settings)
    registry.build_index()
    entry = registry.get("scientific-figure")
    assert entry is not None
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        project=settings.paths.project_name,
        session_id="sess-rag-figure",
        channel="cli",
        db=db,
        artifacts=ArtifactStore(settings.paths, db),
        registry=registry,
    )

    out = await execute_skill(
        entry,
        {
            "input": "RAG architecture with query, embedding index, vector database, retriever, reranker, LLM, citations",
            "title": "RAG Architecture",
            "figure_kind": "rag",
        },
        ctx,
    )

    assert out["status"] == "ok"
    assert out["title"] == "RAG Architecture"
    assert "RAG" in out["caption"]
    dot_path = next(a["path"] for a in out["artifacts"] if a["format"] == "dot")
    dot = Path(dot_path).read_text(encoding="utf-8")
    assert "Vector DB" in dot
    assert "Retriever" in dot
    assert "Reranker" in dot
    assert "Citation Verification" in dot
    assert "Encoder stack" not in dot
    assert "Decoder stack" not in dot
    sources = await ResearchStore(db).list_sources()
    assert any(s.arxiv_id == "2005.11401" for s in sources)


def _figure_ctx(settings, db, registry, session_id: str) -> ExecContext:
    return ExecContext(
        settings=settings,
        paths=settings.paths,
        project=settings.paths.project_name,
        session_id=session_id,
        channel="cli",
        db=db,
        artifacts=ArtifactStore(settings.paths, db),
        registry=registry,
    )


@pytest.mark.asyncio
async def test_scientific_figure_creation_gate_upgrades_misplanned_generic_to_rag():
    """Regression for task 0058c605: the planner mis-set figure_kind=generic under
    multi-deliverable load although the instruction plainly named RAG components."""
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    registry = SkillRegistry(settings)
    registry.build_index()
    entry = registry.get("scientific-figure")
    assert entry is not None

    out = await execute_skill(
        entry,
        {
            "input": (
                "为 RAG 系统综述准备材料：获取 Attention Is All You Need 摘要，"
                "并生成包含 query、retriever、reranker、LLM 的科研架构图。并输出一篇论文"
            ),
            "title": "RAG系统架构图",
            "figure_kind": "generic",
        },
        _figure_ctx(settings, db, registry, "sess-gate-upgrade"),
    )

    assert out["status"] == "ok"
    assert out["outcome"]["code"] == "template_upgraded"
    assert out["outcome"]["requested_kind"] == "generic"
    assert out["outcome"]["figure_kind"] == "rag"
    dot_path = next(a["path"] for a in out["artifacts"] if a["format"] == "dot")
    dot = Path(dot_path).read_text(encoding="utf-8")
    assert "Retriever" in dot
    assert "Reranker" in dot
    assert "Vector DB" in dot
    assert "input -> method -> validation -> output" not in dot


@pytest.mark.asyncio
async def test_scientific_figure_creation_gate_corrects_wrong_explicit_template():
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    registry = SkillRegistry(settings)
    registry.build_index()
    entry = registry.get("scientific-figure")
    assert entry is not None

    out = await execute_skill(
        entry,
        {
            "input": "生成包含 query、retriever、reranker、LLM 的 RAG 科研架构图",
            "title": "RAG系统架构图",
            # A planner choice is advisory. The provider sees the effective
            # instruction and must not knowingly ship the wrong built-in template.
            "figure_kind": "transformer",
        },
        _figure_ctx(settings, db, registry, "sess-gate-explicit-mismatch"),
    )

    assert out["status"] == "ok"
    assert out["figure_kind"] == "rag"
    assert out["outcome"]["code"] == "template_corrected"
    assessment = out["deliverable_assessment"]
    assert assessment["status"] == "passed"
    assert assessment["effective_inputs"]["figure_kind"] == "rag"
    assert assessment["effective_inputs"]["requested_figure_kind"] == "transformer"
    assert assessment["criteria"][0]["criterion_id"] == "figure_matches_instruction"
    assert assessment["criteria"][0]["status"] == "passed"


@pytest.mark.asyncio
async def test_scientific_figure_creation_gate_keeps_clean_generic_requests():
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    registry = SkillRegistry(settings)
    registry.build_index()
    entry = registry.get("scientific-figure")
    assert entry is not None

    out = await execute_skill(
        entry,
        {
            "input": "画一个通用的科研方法流程示意图",
            "title": "科研方法流程",
            "figure_kind": "generic",
        },
        _figure_ctx(settings, db, registry, "sess-gate-clean"),
    )

    assert out["status"] == "ok"
    assert "outcome" not in out
    assert "warning" not in out
    dot_path = next(a["path"] for a in out["artifacts"] if a["format"] == "dot")
    dot = Path(dot_path).read_text(encoding="utf-8")
    assert "Inputs" in dot


@pytest.mark.asyncio
async def test_scientific_figure_creation_gate_degrades_ambiguous_domain_requests():
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    registry = SkillRegistry(settings)
    registry.build_index()
    entry = registry.get("scientific-figure")
    assert entry is not None

    out = await execute_skill(
        entry,
        {
            # Both template signatures match equally: transformer (transformer,
            # encoder) vs rag (rag, retriever) — no clear winner, so the skill
            # keeps generic but reports it as degraded instead of "ok".
            "input": "结合 transformer encoder 与 RAG retriever 的混合系统示意图",
            "title": "混合系统",
            "figure_kind": "generic",
        },
        _figure_ctx(settings, db, registry, "sess-gate-ambiguous"),
    )

    assert out["status"] == "partial"
    assert out["outcome"]["code"] == "generic_despite_domain_terms"
    assert out["recoverable"] is True
    assert "degraded placeholder" in out["warning"]


@pytest.mark.asyncio
async def test_scientific_figure_revision_preserves_source_and_adds_engineering_detail(tmp_path):
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    registry = SkillRegistry(settings)
    registry.build_index()
    entry = registry.get("scientific-figure")
    assert entry is not None
    source = tmp_path / "rag.dot"
    source.write_text(
        """digraph RAG {
  graph [rankdir=LR, label="RAG Architecture"];
  user [label="User Query"];
  vectordb [label="Vector DB"];
  retriever [label="Retriever"];
  reranker [label="Reranker"];
  llm [label="LLM Generator"];
  answer [label="Grounded Answer"];
  user -> vectordb -> retriever -> reranker -> llm -> answer;
}
""",
        encoding="utf-8",
    )
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        project=settings.paths.project_name,
        session_id="sess-rag-revision",
        channel="cli",
        db=db,
        artifacts=ArtifactStore(settings.paths, db),
        registry=registry,
    )

    out = await execute_skill(
        entry,
        {
            "input": "生成的这个架构图过于简单，内容不够，请结合实际工程实践做优化",
            "figure_kind": "rag",
            "revision_mode": "major",
            "source_artifact_path": str(source),
            "source_task_id": "task-rag",
            "revision_constraints": {
                "preserve_source_structure": True,
                "reject_generic_template": True,
                "min_nodes": 6,
                "min_edges": 5,
            },
        },
        ctx,
    )

    assert out["status"] == "ok"
    assert out["revision"]["mode"] == "major"
    assert out["revision"]["quality"]["passed"] is True
    assert out["deliverable_assessment"]["deliverable_id"]
    assert out["deliverable_assessment"]["effective_inputs"]["revision_mode"] == "major"
    dot_path = next(a["path"] for a in out["artifacts"] if a["format"] == "dot")
    dot = Path(dot_path).read_text(encoding="utf-8")
    assert "Vector DB" in dot
    assert "Retriever" in dot
    assert "Reranker" in dot
    assert "Hybrid Retrieval" in dot
    assert "Observability" in dot
    assert "input -> method -> validation -> output" not in dot
