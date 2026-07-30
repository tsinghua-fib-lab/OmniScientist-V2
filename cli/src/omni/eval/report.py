"""Scoring model + aggregation for the capability benchmark.

Every check a scenario declares is attributed to a canonical *capability
dimension* (routing, capability selection, guardrails, verification, grounding,
presentation, retrieval) so the scoreboard reads as "how good is the agent at
X", not "which test file passed". The report is pure data + rendering so it can
be asserted on in pytest and serialized to JSON for CI trend tracking.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

# Maps an ``expect:`` key to the capability dimension it measures.
CHECK_DIMENSION: dict[str, str] = {
    "kind": "routing",
    "planner_calls": "routing",
    "terminated_reason": "routing",
    "capabilities_include": "capability_selection",
    "capabilities_executed": "execution",
    "skills_include": "skill_routing",
    "skills_exclude": "guardrails",
    "skills_executed_include": "execution",
    "skills_executed_exclude": "execution",
    "action_required": "guardrails",
    "verification": "verification",
    "text_contains": "grounding",
    "im_hides": "presentation",
    "retrieval": "retrieval",
    "safety": "safety",
    "provenance_capsule": "provenance",
    "self_review": "self_review",
    "literature": "literature",
    "skill_selected": "skill_routing",
    "compute": "compute",
    "recall": "recall",
    "cost": "cost",
    "retry": "retry",
    "fork": "fork",
    "schedule": "schedule",
    "streaming": "streaming",
    "memory_graph": "memory_graph",
}


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    scenario_id: str
    dimension: str
    name: str
    passed: bool
    detail: str = ""


@dataclass(slots=True)
class ScenarioResult:
    scenario_id: str
    title: str
    tags: tuple[str, ...]
    checks: list[CheckOutcome] = field(default_factory=list)
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.error and all(c.passed for c in self.checks)

    @property
    def n_passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)


@dataclass(frozen=True, slots=True)
class DimensionScore:
    name: str
    passed: int
    total: int

    @property
    def rate(self) -> float:
        return (self.passed / self.total) if self.total else 1.0


@dataclass(slots=True)
class BenchmarkReport:
    results: list[ScenarioResult] = field(default_factory=list)

    def dimensions(self) -> list[DimensionScore]:
        agg: OrderedDict[str, list[int]] = OrderedDict()
        for res in self.results:
            for check in res.checks:
                bucket = agg.setdefault(check.dimension, [0, 0])
                bucket[1] += 1
                if check.passed:
                    bucket[0] += 1
        return [DimensionScore(name, p, t) for name, (p, t) in agg.items()]

    @property
    def total_checks(self) -> int:
        return sum(len(r.checks) for r in self.results)

    @property
    def passed_checks(self) -> int:
        return sum(r.n_passed for r in self.results)

    @property
    def scenarios_passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def score(self) -> float:
        """Overall check pass-rate in [0, 1]."""
        return (self.passed_checks / self.total_checks) if self.total_checks else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "scenarios_passed": self.scenarios_passed,
            "scenarios_total": len(self.results),
            "checks_passed": self.passed_checks,
            "checks_total": self.total_checks,
            "dimensions": {
                d.name: {"passed": d.passed, "total": d.total, "rate": round(d.rate, 4)}
                for d in self.dimensions()
            },
            "scenarios": [
                {
                    "id": r.scenario_id,
                    "title": r.title,
                    "tags": list(r.tags),
                    "passed": r.passed,
                    "error": r.error,
                    "checks": [
                        {"dimension": c.dimension, "name": c.name, "passed": c.passed, "detail": c.detail}
                        for c in r.checks
                    ],
                }
                for r in self.results
            ],
        }


__all__ = ["CheckOutcome", "ScenarioResult", "DimensionScore", "BenchmarkReport", "CHECK_DIMENSION"]
