"""P3 — the persistent-memory benchmark must run offline and stay green in CI.

Metrics are "injection hit + citation hit + zero leakage" measured across the
cross-session / cross-workspace / cross-channel / isolation / concurrency /
offline dimensions. A regression here means durable memory stopped following the
owner, stopped preferring grounded facts, or started leaking across principals.
"""

from __future__ import annotations

import json

import pytest

from omni.eval import run_memory_benchmark
from omni.eval.memory_bench import CITATION_HIT, INJECTION_HIT, ZERO_LEAKAGE
from omni.eval.report import BenchmarkReport

_DIMENSIONS = {
    "cross_session", "cross_workspace", "cross_channel",
    "isolation", "concurrency", "offline",
}


@pytest.mark.asyncio
async def test_persistent_memory_benchmark_passes_offline() -> None:
    report = await run_memory_benchmark()
    assert isinstance(report, BenchmarkReport)
    assert report.results, "benchmark produced no results"

    failed = [r.scenario_id for r in report.results if not r.passed]
    assert not failed, f"memory scenarios regressed: {failed}"
    assert report.score == 1.0

    # Every persistent-memory dimension is exercised …
    assert {r.scenario_id for r in report.results} == _DIMENSIONS
    # … and each of the three P3 metrics is measured and fully green.
    dims = {d.name: d for d in report.dimensions()}
    assert set(dims) == {INJECTION_HIT, CITATION_HIT, ZERO_LEAKAGE}
    for name, dim in dims.items():
        assert dim.total > 0 and dim.rate == 1.0, name


@pytest.mark.asyncio
async def test_memory_benchmark_report_serializes_for_ci_trend() -> None:
    report = await run_memory_benchmark()
    payload = report.to_dict()
    assert set(payload) >= {"score", "dimensions", "scenarios", "checks_passed", "checks_total"}
    # round-trips through JSON without custom encoders (CI trend snapshotting)
    assert json.loads(json.dumps(payload))["score"] == 1.0
