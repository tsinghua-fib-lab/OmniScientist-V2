"""Deterministic research-quality evaluator and CLI integration."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from omni.cli.main import app
from omni.eval import (
    evaluate_citation_fidelity,
    evaluate_reproducibility,
    evaluate_research_quality,
    evaluate_statistical_correctness,
    load_quality_payload,
)


def test_citation_fidelity_detects_dangling_and_wrong_evidence():
    result = evaluate_citation_fidelity({
        "sources": {"S1": {"title": "RAG paper"}},
        "claims": [
            {"text": "supported", "citations": ["S1"], "supported_by": ["S1"]},
            {"text": "wrong", "citations": ["S1"], "supported_by": ["S2"]},
            {"text": "dangling", "citations": ["S9"], "supported_by": ["S9"]},
        ],
    })

    assert not result.passed
    assert result.metrics["precision"] == 1 / 3
    assert result.metrics["resolution"] == 2 / 3
    assert result.metrics["claim_coverage"] == 1 / 3


def test_statistical_correctness_supports_tolerance_and_invariants():
    result = evaluate_statistical_correctness({"assertions": [
        {"name": "rounded", "reported": 0.3333, "expected": 1 / 3, "atol": 0.001},
        {"name": "p", "kind": "probability", "reported": 1.2},
        {"name": "ci", "kind": "interval", "lower": 0.1, "estimate": 0.2, "upper": 0.3},
        {"name": "n", "kind": "sample_size", "reported": 10.5},
    ]})

    assert [check.passed for check in result.checks] == [True, False, True, False]
    assert result.score == 0.5


def test_reproducibility_verifies_manifest_and_artifact_hash(tmp_path: Path):
    artifact = tmp_path / "result.csv"
    artifact.write_text("x,y\n1,2\n", encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    result = evaluate_reproducibility({
        "artifact_path": str(artifact),
        "stochastic": False,
        "manifest": {
            "schema": "omni.repro_bundle/v1",
            "artifact": {"sha256": digest},
            "creation": {"command": "python analysis.py", "inputs": {}},
            "environment": {"env_lock": "numpy==2", "python": "3.12"},
        },
    })

    assert result.passed
    assert result.score == 1.0


def test_bundled_quality_baseline_passes_all_dimensions():
    report = evaluate_research_quality(load_quality_payload())

    assert report.passed
    assert report.score == 1.0
    assert {item.name for item in report.dimensions} == {
        "citation_fidelity", "statistical_correctness", "reproducibility",
    }


def test_eval_research_quality_json_cli(tmp_path: Path):
    source = tmp_path / "quality.json"
    source.write_text(json.dumps(load_quality_payload()), encoding="utf-8")

    result = CliRunner().invoke(
        app, ["eval", "--research-quality", "--quality-input", str(source), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["passed"] is True
    assert payload["dimensions"]["citation_fidelity"]["score"] == 1.0
