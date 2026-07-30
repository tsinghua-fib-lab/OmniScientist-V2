"""Automated capability benchmark for the OmniScientist agent.

Where ``omni bench`` scores the *retrieval* core (recall@k / MRR), this package
scores the *agent* itself: given real research/dialogue scenarios, does it route
to the right intent, select the right capabilities, keep its guardrails, and
stay honest about provenance? It is offline and deterministic by design (Tier A)
so it runs in CI: each scenario supplies the semantic-planner JSON the model
*should* emit, and the scorer checks the resulting :class:`TurnResult`, run
graph, and channel presentation against declared expectations.

Public surface:

* :func:`omni.eval.scenarios.load_scenarios` — read a YAML corpus.
* :func:`omni.eval.runner.run_benchmark` — score a corpus offline.
* :class:`omni.eval.report.BenchmarkReport` — aggregate + render results.
"""

from omni.eval.blackbox import (
    BlackBoxAttempt,
    BlackBoxReport,
    BlackBoxScenario,
    BlackBoxTurn,
    bundled_blackbox_scenarios_dir,
    load_blackbox_scenarios,
    run_blackbox_benchmark,
)
from omni.eval.coverage import CoverageReport, audit_coverage
from omni.eval.dashboard_html import render_trend_html
from omni.eval.external_benchmarks import (
    BioMysteryCase,
    build_biomystery_prompt,
    extract_biomystery_data,
    load_biomystery_cases,
    run_biomystery_cases,
    write_benchmark_answers,
)
from omni.eval.history import (
    append_snapshot,
    default_history_path,
    detect_regressions,
    last_snapshot,
    load_history,
    report_snapshot,
)
from omni.eval.memory_bench import (
    CITATION_HIT,
    INJECTION_HIT,
    ZERO_LEAKAGE,
    run_memory_benchmark,
)
from omni.eval.report import BenchmarkReport, DimensionScore, ScenarioResult
from omni.eval.runner import run_benchmark
from omni.eval.scenarios import Scenario, ScenarioTurn, bundled_scenarios_dir, load_scenarios
from omni.eval.trend import (
    DimensionTrend,
    TrendSummary,
    format_delta,
    sparkline,
    summarize_history,
)
from omni.research.quality import (
    QualityCheck,
    QualityDimension,
    ResearchQualityReport,
    evaluate_citation_fidelity,
    evaluate_reproducibility,
    evaluate_research_quality,
    evaluate_statistical_correctness,
    load_quality_payload,
)

__all__ = [
    "Scenario",
    "ScenarioTurn",
    "BlackBoxAttempt",
    "BlackBoxReport",
    "BlackBoxScenario",
    "BlackBoxTurn",
    "load_blackbox_scenarios",
    "bundled_blackbox_scenarios_dir",
    "run_blackbox_benchmark",
    "BioMysteryCase",
    "build_biomystery_prompt",
    "extract_biomystery_data",
    "load_biomystery_cases",
    "run_biomystery_cases",
    "write_benchmark_answers",
    "load_scenarios",
    "bundled_scenarios_dir",
    "run_benchmark",
    "run_memory_benchmark",
    "INJECTION_HIT",
    "CITATION_HIT",
    "ZERO_LEAKAGE",
    "BenchmarkReport",
    "QualityCheck",
    "QualityDimension",
    "ResearchQualityReport",
    "evaluate_citation_fidelity",
    "evaluate_reproducibility",
    "evaluate_research_quality",
    "evaluate_statistical_correctness",
    "load_quality_payload",
    "ScenarioResult",
    "DimensionScore",
    "CoverageReport",
    "audit_coverage",
    "report_snapshot",
    "append_snapshot",
    "load_history",
    "last_snapshot",
    "detect_regressions",
    "default_history_path",
    "summarize_history",
    "sparkline",
    "format_delta",
    "TrendSummary",
    "DimensionTrend",
    "render_trend_html",
]
