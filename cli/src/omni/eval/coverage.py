"""Coverage audit for the scenario corpus — "is the regression net complete?".

The capability benchmark (``harness``/``runner``) tells you *how well* the agent
does on the scenarios that exist. This module answers the orthogonal question the
user actually cares about for regression safety: *which capabilities, capability
dimensions, and user personas are not exercised by any scenario at all?* A
capability with zero scenarios is an invisible regression waiting to happen, so
``omni eval --coverage`` (and a CI gate) can fail when the net has holes.

Targets are derived from the source of truth in the codebase (the capability
registry + the check-dimension map), so new capabilities automatically raise the
coverage bar until someone writes a scenario for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from omni.agent.capabilities import (
    DELIVERABLE_DRAFT_MANUSCRIPT,
    DELIVERABLE_DRAFT_SECTION,
    WORKFLOW_CAPABILITIES,
)
from omni.eval.report import CHECK_DIMENSION
from omni.eval.scenarios import Scenario

# Personas the corpus is expected to cover in real-usage regressions:
# a researcher, an everyday user, and an adversary probing the safety envelope.
TARGET_PERSONAS: tuple[str, ...] = ("scientist", "general", "red_team")

# ``draft.section`` / ``draft.manuscript`` are *deliverables* (produced via a
# ``synthesis.final`` step's ``deliverable`` field), not standalone capabilities,
# so they are not part of the capability coverage target.
_DELIVERABLES = {DELIVERABLE_DRAFT_SECTION, DELIVERABLE_DRAFT_MANUSCRIPT}
# Executed natively (no backing skill) but still worth a regression scenario.
_NATIVE_CAPABILITIES = {"synthesis.final"}


def target_capabilities() -> set[str]:
    """The capability set every regression corpus should exercise."""
    return (set(WORKFLOW_CAPABILITIES) - _DELIVERABLES) | _NATIVE_CAPABILITIES


def target_dimensions() -> set[str]:
    """The capability dimensions the scoreboard tracks."""
    return set(CHECK_DIMENSION.values())


@dataclass(slots=True)
class CoverageReport:
    """What the corpus covers vs. what it should cover."""

    covered_capabilities: set[str] = field(default_factory=set)
    covered_dimensions: set[str] = field(default_factory=set)
    covered_personas: set[str] = field(default_factory=set)
    scenarios_total: int = 0

    @property
    def missing_capabilities(self) -> set[str]:
        return target_capabilities() - self.covered_capabilities

    @property
    def missing_dimensions(self) -> set[str]:
        return target_dimensions() - self.covered_dimensions

    @property
    def missing_personas(self) -> set[str]:
        return set(TARGET_PERSONAS) - self.covered_personas

    @property
    def complete(self) -> bool:
        """True when there are no capability/dimension/persona holes."""
        return not (self.missing_capabilities or self.missing_dimensions or self.missing_personas)

    def _rate(self, covered: set[str], target: set[str]) -> float:
        return (len(covered & target) / len(target)) if target else 1.0

    def to_dict(self) -> dict[str, object]:
        caps, dims = target_capabilities(), target_dimensions()
        return {
            "complete": self.complete,
            "scenarios_total": self.scenarios_total,
            "capabilities": {
                "rate": round(self._rate(self.covered_capabilities, caps), 4),
                "covered": sorted(self.covered_capabilities & caps),
                "missing": sorted(self.missing_capabilities),
            },
            "dimensions": {
                "rate": round(self._rate(self.covered_dimensions, dims), 4),
                "covered": sorted(self.covered_dimensions & dims),
                "missing": sorted(self.missing_dimensions),
            },
            "personas": {
                "rate": round(self._rate(self.covered_personas, set(TARGET_PERSONAS)), 4),
                "covered": sorted(self.covered_personas & set(TARGET_PERSONAS)),
                "missing": sorted(self.missing_personas),
            },
        }


def audit_coverage(scenarios: list[Scenario]) -> CoverageReport:
    """Compute what the scenario corpus covers vs. the codebase targets.

    A *dimension* counts as covered only when some scenario actually **asserts a
    check** in it (derived from the ``expect`` keys via ``CHECK_DIMENSION``), not
    merely tags itself — so the audit reflects real assertions, not intent.
    """
    report = CoverageReport(scenarios_total=len(scenarios))
    for scenario in scenarios:
        report.covered_capabilities |= scenario.capabilities()
        if scenario.type == "retrieval":
            report.covered_dimensions.add("retrieval")
        for turn in scenario.turns:
            for key in turn.expect:
                dim = CHECK_DIMENSION.get(key)
                if dim:
                    report.covered_dimensions.add(dim)
        if scenario.persona:
            report.covered_personas.add(scenario.persona)
    return report


__all__ = [
    "CoverageReport",
    "audit_coverage",
    "target_capabilities",
    "target_dimensions",
    "TARGET_PERSONAS",
]
