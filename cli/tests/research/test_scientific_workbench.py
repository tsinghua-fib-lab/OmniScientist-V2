"""End-to-end workbench contracts for compute and figure provenance."""

from __future__ import annotations

import asyncio
import json

import pytest

from omni.config import load_settings
from omni.research.artifacts import build_figure_bundle, verify_figure_bundle
from omni.research.tools import build_research_tools
from omni.runtime.compute_jobs import ComputeJobStore
from omni.skills_runtime.builtin_tools.compute import build_compute_tools
from omni.skills_runtime.context import ExecContext
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database


async def _ctx() -> ExecContext:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    return ExecContext(
        settings=settings,
        paths=settings.paths,
        project=settings.paths.project_name,
        session_id="workbench-session",
        task_id="workbench-run",
        channel="cli",
        db=db,
        artifacts=ArtifactStore(settings.paths, db),
    )


@pytest.mark.asyncio
async def test_compute_tool_persists_queryable_job_lifecycle() -> None:
    ctx = await _ctx()
    tools = {tool.spec.name: tool for tool in build_compute_tools(ctx)}

    result = await tools["run_compute"].handler({"command": "printf managed-compute"})

    assert result["status"] == "ok"
    assert result["compute_job_id"]
    job = await ComputeJobStore(ctx.db).get(result["compute_job_id"])
    assert job is not None
    assert job.status == "succeeded"
    assert job.task_id == ctx.task_id
    status = await tools["get_compute_job"].handler({"job_id": job.id})
    assert status["job"]["status"] == "succeeded"
    assert status["job"]["command"] == "printf managed-compute"


@pytest.mark.asyncio
async def test_running_local_compute_honors_durable_cancel_request() -> None:
    ctx = await _ctx()
    tools = {tool.spec.name: tool for tool in build_compute_tools(ctx)}
    # Long enough that a busy CI host still observes ``running`` before the
    # process exits on its own (``sleep 5`` raced to ``succeeded`` under load).
    running = asyncio.create_task(
        tools["run_compute"].handler({"command": "sleep 30", "timeout": 60})
    )
    store = ComputeJobStore(ctx.db)
    job = None
    for _ in range(200):
        jobs = await store.list(session_id=ctx.session_id)
        job = jobs[0] if jobs else None
        if job is not None and job.status == "running":
            break
        await asyncio.sleep(0.05)
    assert job is not None and job.status == "running"

    cancelled = await tools["cancel_compute"].handler({"job_id": job.id})
    result = await asyncio.wait_for(running, timeout=10)
    settled = await store.get(job.id)

    assert cancelled["status"] == "cancel_requested"
    assert result["status"] == "cancelled"
    assert settled is not None and settled.status == "cancelled"


@pytest.mark.asyncio
async def test_statistical_reviewer_is_available_to_every_research_skill() -> None:
    ctx = await _ctx()
    tools = {tool.spec.name: tool for tool in build_research_tools(ctx)}

    result = await tools["review_statistics"].handler({
        "assertions": [
            {"name": "confidence_interval", "kind": "interval", "lower": 0.1, "estimate": 0.2, "upper": 0.3},
            {"name": "p_value", "kind": "probability", "reported": 1.4},
        ]
    })

    assert result["status"] == "failed"
    assert result["score"] == 0.5
    assert [item["passed"] for item in result["checks"]] == [True, False]


@pytest.mark.asyncio
async def test_figure_code_data_bundle_detects_posthoc_data_change() -> None:
    ctx = await _ctx()
    figure = await ctx.artifacts.put_bytes(
        b"<svg><text>accuracy</text></svg>", kind="figure", ext="svg", mime="image/svg+xml"
    )
    code = await ctx.artifacts.put_bytes(
        b"print('plot')\n", kind="code", ext="py", mime="text/x-python"
    )
    data = await ctx.artifacts.put_bytes(
        b"epoch,accuracy\n1,0.9\n", kind="data", ext="csv", mime="text/csv"
    )

    built = await build_figure_bundle(
        artifacts=ctx.artifacts,
        figure_uri=figure.uri,
        code_uri=code.uri,
        data_uris=[data.uri],
        run_ids=["experiment-run-1"],
        session_id=ctx.session_id,
        title="Accuracy figure",
    )
    assert built["status"] == "ok"
    manifest_path = await ctx.artifacts.resolve_path(built["manifest_uri"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert (await verify_figure_bundle(artifacts=ctx.artifacts, manifest=manifest))["passed"]

    data_path = await ctx.artifacts.resolve_path(data.uri)
    data_path.write_text("epoch,accuracy\n1,0.1\n", encoding="utf-8")
    verified = await verify_figure_bundle(artifacts=ctx.artifacts, manifest=manifest)
    assert not verified["passed"]
    assert any(
        item["name"].startswith("data_hash:") and not item["passed"]
        for item in verified["checks"]
    )
