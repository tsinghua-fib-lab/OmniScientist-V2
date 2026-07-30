"""Per-artifact provenance capsules (P1): capsule model, set_meta, attach tool."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.research.provenance import CAPSULE_SCHEMA, ProvenanceCapsule, read_capsule
from omni.research.store import ResearchStore
from omni.research.tools import build_research_tools
from omni.runtime.task_recorder import TaskRecorder
from omni.skills_runtime.context import ExecContext
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database

_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>'


async def _ctx(run_id: str = "") -> ExecContext:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ExecContext(
        settings=s, paths=s.paths, project=s.paths.project_name,
        session_id="sess-prov", channel="cli", db=db, task_id=run_id,
        artifacts=ArtifactStore(s.paths, db), llm=None,
    )


# ── capsule model ────────────────────────────────────────────────────────────
def test_capsule_grounded_needs_any_evidence():
    hollow = ProvenanceCapsule(artifact_uri="artifact://x")
    ok, reasons = hollow.completeness()
    assert ok is False
    assert not hollow.is_grounded
    assert reasons  # advisory reasons listed

    grounded = ProvenanceCapsule(artifact_uri="artifact://x", source_ids=["s1"])
    assert grounded.is_grounded
    assert grounded.completeness()[0] is True


def test_capsule_roundtrip_dict():
    cap = ProvenanceCapsule(
        artifact_uri="artifact://x", title="fig", source_ids=["s1"],
        claim_ids=["c1"], tool_calls=["make_figure"],
    )
    data = cap.to_dict()
    assert data["schema"] == CAPSULE_SCHEMA
    assert data["grounded"] is True
    back = ProvenanceCapsule.from_dict(data)
    assert back.source_ids == ["s1"]
    assert back.claim_ids == ["c1"]
    assert back.tool_calls == ["make_figure"]


def test_read_capsule_from_meta_dict_and_missing():
    meta = {"provenance": ProvenanceCapsule(artifact_uri="artifact://x", source_ids=["s1"]).to_dict()}
    cap = read_capsule(meta)
    assert cap is not None and cap.source_ids == ["s1"]
    assert read_capsule({}) is None
    assert read_capsule({"provenance": "nope"}) is None


# ── artifact store set_meta ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_set_meta_merges_and_read_capsule_roundtrips():
    ctx = await _ctx()
    art = await ctx.artifacts.put_bytes(_SVG, kind="figure", ext="svg", meta={"pre": 1})
    cap = ProvenanceCapsule(artifact_uri=art.uri, source_ids=["s1"], claim_ids=["c1"])
    assert await ctx.artifacts.set_meta(art.uri, {"provenance": cap.to_dict()}) is True

    row = await ctx.artifacts.get(art.uri)
    assert row.meta["pre"] == 1  # shallow merge preserves prior keys
    read = read_capsule(row)
    assert read is not None and read.source_ids == ["s1"]

    assert await ctx.artifacts.set_meta("artifact://missing", {"provenance": {}}) is False


# ── attach_provenance tool ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_attach_provenance_grounds_and_records_event():
    recorder_ctx = await _ctx()
    recorder = TaskRecorder(recorder_ctx.db, project=recorder_ctx.project)
    run = await recorder.create_task(session_id="sess-prov", channel="cli", user_input="bind fig")

    ctx = await _ctx(run_id=run.id)
    art = await ctx.artifacts.put_bytes(_SVG, kind="figure", ext="svg")
    tools = {t.spec.name: t for t in build_research_tools(ctx)}
    assert "attach_provenance" in tools

    out = await tools["attach_provenance"].handler({
        "artifact_uri": art.uri, "title": "RAG fig",
        "sources": [{"title": "Attention Is All You Need", "arxiv_id": "1706.03762"}],
        "claims": ["RAG has retriever + generator"],
        "tool_calls": ["make_figure"],
    })
    assert out["status"] == "ok"
    assert out["grounded"] is True
    assert out["attached"] is True
    assert out["source_ids"] and out["claim_ids"]

    # capsule persisted on the artifact
    cap = read_capsule(await ctx.artifacts.get(art.uri))
    assert cap is not None and cap.is_grounded

    # durable provenance.capsule event fired with complete=true
    events = await recorder.list_events(run.id)
    capsules = [e for e in events if e.event_type == "provenance.capsule"]
    assert capsules and (capsules[0].output_json or {}).get("complete") is True

    # sources were actually cited into the research store
    store = ResearchStore(ctx.db)
    assert await store.get_source(out["source_ids"][0]) is not None


@pytest.mark.asyncio
async def test_attach_provenance_hollow_is_incomplete():
    recorder_ctx = await _ctx()
    recorder = TaskRecorder(recorder_ctx.db, project=recorder_ctx.project)
    run = await recorder.create_task(session_id="sess-prov", channel="cli", user_input="bind")

    ctx = await _ctx(run_id=run.id)
    art = await ctx.artifacts.put_bytes(_SVG, kind="figure", ext="svg")
    tools = {t.spec.name: t for t in build_research_tools(ctx)}
    out = await tools["attach_provenance"].handler({"artifact_uri": art.uri, "title": "naked"})
    assert out["status"] == "incomplete"
    assert out["grounded"] is False

    events = await recorder.list_events(run.id)
    capsules = [e for e in events if e.event_type == "provenance.capsule"]
    assert capsules and (capsules[0].output_json or {}).get("complete") is False


@pytest.mark.asyncio
async def test_attach_provenance_requires_uri():
    ctx = await _ctx()
    tools = {t.spec.name: t for t in build_research_tools(ctx)}
    assert "error" in await tools["attach_provenance"].handler({})
