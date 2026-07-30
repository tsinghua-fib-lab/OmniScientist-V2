"""Trend gate: record capability scores over time and fail on regression."""

from __future__ import annotations

from omni.eval import (
    append_snapshot,
    detect_regressions,
    last_snapshot,
    load_history,
    report_snapshot,
)
from omni.eval.report import BenchmarkReport, CheckOutcome, ScenarioResult


def _report(*outcomes: tuple[str, bool]) -> BenchmarkReport:
    checks = [CheckOutcome("s1", dim, dim, ok) for dim, ok in outcomes]
    return BenchmarkReport(results=[ScenarioResult("s1", "t", tuple(d for d, _ in outcomes), checks)])


def test_snapshot_captures_score_and_dimension_rates():
    snap = report_snapshot(_report(("routing", True), ("safety", True)), coverage_complete=True, label="x")
    assert snap["score"] == 1.0
    assert snap["dimensions"] == {"routing": 1.0, "safety": 1.0}
    assert snap["coverage_complete"] is True
    assert snap["label"] == "x"


def test_append_and_load_history_roundtrip(tmp_path):
    path = tmp_path / "eval_history.jsonl"
    append_snapshot(report_snapshot(_report(("routing", True))), path=path)
    append_snapshot(report_snapshot(_report(("routing", False))), path=path)
    history = load_history(path)
    assert len(history) == 2
    assert last_snapshot(path)["dimensions"]["routing"] == 0.0


def test_no_baseline_never_regresses():
    assert detect_regressions(report_snapshot(_report(("routing", True))), None) == []


def test_detects_overall_and_dimension_drops():
    baseline = report_snapshot(_report(("routing", True), ("safety", True)))
    current = report_snapshot(_report(("routing", True), ("safety", False)))
    regressions = detect_regressions(current, baseline)
    assert any("safety" in r for r in regressions)
    assert any("overall score" in r for r in regressions)


def test_equal_or_better_is_not_a_regression():
    baseline = report_snapshot(_report(("routing", False)))
    current = report_snapshot(_report(("routing", True)))
    assert detect_regressions(current, baseline) == []


def test_coverage_completeness_regression_is_flagged():
    baseline = report_snapshot(_report(("routing", True)), coverage_complete=True)
    current = report_snapshot(_report(("routing", True)), coverage_complete=False)
    assert any("coverage" in r for r in detect_regressions(current, baseline))
