"""Regression-net completeness for user-centered automation.

Two orthogonal guarantees a merge must not break:

1. The persona suites (``scientist`` / ``general`` / ``red_team``) — scenarios that drive the
   agent the way a real user types — stay fully green offline.
2. The scenario corpus keeps covering **every** capability the codebase exposes,
   every capability dimension the scoreboard tracks, and all target personas. A new
   capability with no scenario raises this gate immediately, so coverage can
   never silently rot.
"""

from __future__ import annotations

import pytest

from omni.eval import audit_coverage, load_scenarios, run_benchmark
from omni.eval.coverage import TARGET_PERSONAS


@pytest.mark.parametrize("persona", ["scientist", "general", "red_team"])
@pytest.mark.asyncio
async def test_persona_suite_is_green_offline(persona: str) -> None:
    scenarios = load_scenarios(persona=persona)
    assert scenarios, f"no scenarios for persona {persona!r}"
    report = await run_benchmark(scenarios)
    failed = [r.scenario_id for r in report.results if not r.passed]
    assert not failed, f"{persona} scenarios regressed: {failed}"
    assert report.score == 1.0


def test_corpus_coverage_is_complete() -> None:
    cov = audit_coverage(load_scenarios())
    # Any hole here means a real capability / dimension / persona has no
    # scenario exercising it — fix by adding a scenario, not by lowering the bar.
    assert not cov.missing_capabilities, f"uncovered capabilities: {sorted(cov.missing_capabilities)}"
    assert not cov.missing_dimensions, f"uncovered dimensions: {sorted(cov.missing_dimensions)}"
    assert not cov.missing_personas, f"uncovered personas: {sorted(cov.missing_personas)}"
    assert cov.complete


def test_every_persona_has_at_least_one_scenario() -> None:
    for persona in TARGET_PERSONAS:
        assert load_scenarios(persona=persona), f"persona {persona!r} has no scenarios"


def test_coverage_report_serializes_for_ci() -> None:
    import json

    payload = audit_coverage(load_scenarios()).to_dict()
    assert set(payload) >= {"complete", "capabilities", "dimensions", "personas"}
    assert payload["personas"]["rate"] == 1.0
    assert payload["personas"]["covered"] == sorted(TARGET_PERSONAS)
    assert json.loads(json.dumps(payload))["complete"] is True


def test_eval_command_is_wired_into_cli() -> None:
    from omni.cli.main import app

    names = {c.name for c in app.registered_commands if c.name}
    assert "eval" in names, "`omni eval` is not registered on the CLI app"
