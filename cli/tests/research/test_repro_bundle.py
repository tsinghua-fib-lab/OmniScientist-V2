"""Reproducible artifact packaging (P1-H): bundle contents, manifest, tool."""

from __future__ import annotations

import json
import zipfile

import pytest

from omni.config import load_settings
from omni.research.repro import build_repro_bundle, git_commit, sha256_bytes
from omni.research.store import ResearchStore
from omni.research.tools import build_research_tools
from omni.skills_runtime.context import ExecContext
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database

_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'


async def _ctx() -> ExecContext:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ExecContext(
        settings=s, paths=s.paths, project=s.paths.project_name,
        session_id="sess-repro", channel="cli", db=db,
        artifacts=ArtifactStore(s.paths, db), llm=None,
    )


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_sha256_bytes_stable():
    assert sha256_bytes(b"abc") == sha256_bytes(b"abc")
    assert len(sha256_bytes(b"abc")) == 64
    assert sha256_bytes(b"abc") != sha256_bytes(b"abd")


def test_git_commit_is_best_effort(tmp_path):
    # A non-repo dir must yield "" rather than raise.
    assert git_commit(tmp_path) == ""


# ── bundle build ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_build_bundle_contents_and_manifest():
    ctx = await _ctx()
    art = await ctx.artifacts.put_bytes(
        _SVG, kind="figure", title="fig", ext="svg", mime="image/svg+xml",
        session_id=ctx.session_id,
    )

    result = await build_repro_bundle(
        artifacts=ctx.artifacts, store=ResearchStore(ctx.db), paths=ctx.paths,
        artifact_uri=art.uri, title="my figure", command="python make_fig.py",
        seed=7, metrics={"acc": 0.9}, inputs={"n": 3}, session_id=ctx.session_id,
    )

    assert result["status"] == "ok"
    assert result["bundle_uri"].startswith("artifact://")
    assert result["artifact_sha256"] == sha256_bytes(_SVG)
    assert result["run_id"]  # ledger mirror recorded

    zip_path = await ctx.artifacts.resolve_path(result["bundle_uri"])
    assert zip_path is not None and zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "MANIFEST.json" in names
        assert "README.md" in names
        assert "command.sh" in names
        assert any(n.startswith("artifact/") and n.endswith(".svg") for n in names)
        manifest = json.loads(zf.read("MANIFEST.json"))

    assert manifest["schema"] == "omni.repro_bundle/v1"
    assert manifest["artifact"]["sha256"] == sha256_bytes(_SVG)
    assert manifest["creation"]["command"] == "python make_fig.py"
    assert manifest["creation"]["seed"] == 7
    assert manifest["metrics"] == {"acc": 0.9}
    assert manifest["environment"]["env_lock"].startswith("sha256:")
    assert "python" in manifest["environment"]


@pytest.mark.asyncio
async def test_build_bundle_with_code_writes_code_file():
    ctx = await _ctx()
    art = await ctx.artifacts.put_bytes(b"data,1\n", kind="data", ext="csv", session_id=ctx.session_id)
    result = await build_repro_bundle(
        artifacts=ctx.artifacts, store=None, paths=ctx.paths,
        artifact_uri=art.uri, code="print('hi')", code_filename="gen.py",
        session_id=ctx.session_id,
    )
    assert result["status"] == "ok"
    assert result["run_id"] == ""  # no store → no ledger row, still succeeds
    zip_path = await ctx.artifacts.resolve_path(result["bundle_uri"])
    with zipfile.ZipFile(zip_path) as zf:
        assert "gen.py" in zf.namelist()
        assert zf.read("gen.py").decode() == "print('hi')"


@pytest.mark.asyncio
async def test_build_bundle_missing_artifact_errors():
    ctx = await _ctx()
    result = await build_repro_bundle(
        artifacts=ctx.artifacts, store=None, paths=ctx.paths,
        artifact_uri="artifact://does-not-exist",
    )
    assert result["status"] == "error"


# ── tool surface ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_package_artifact_tool_roundtrip():
    ctx = await _ctx()
    art = await ctx.artifacts.put_bytes(_SVG, kind="figure", ext="svg", session_id=ctx.session_id)
    tools = {t.spec.name: t for t in build_research_tools(ctx)}
    assert "package_artifact" in tools

    out = await tools["package_artifact"].handler({
        "artifact_uri": art.uri, "title": "t", "command": "echo hi",
    })
    assert out["status"] == "ok"
    assert out["bundle_uri"].startswith("artifact://")


@pytest.mark.asyncio
async def test_package_artifact_tool_requires_uri():
    ctx = await _ctx()
    tools = {t.spec.name: t for t in build_research_tools(ctx)}
    out = await tools["package_artifact"].handler({})
    assert "error" in out
