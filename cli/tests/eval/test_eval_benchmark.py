"""The capability benchmark must run offline and stay green in CI.

This guards the ``omni eval`` harness itself (scenario loading, scoring, JSON
report shape) and asserts the bundled seed corpus passes deterministically.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omni.eval import BenchmarkReport, load_scenarios, run_benchmark
from omni.eval.harness import (
    ScenarioLLM,
    _collect_execution_steps,
    _collect_routing_steps,
    _evaluate_turn,
)
from omni.eval.report import CheckOutcome, DimensionScore, ScenarioResult
from omni.eval.scenarios import Scenario, ScenarioTurn
from omni.skills_runtime.discovery import active_skill_names

ROOT = Path(__file__).resolve().parents[3]


def test_bundled_corpus_loads_and_is_well_formed():
    scenarios = load_scenarios()
    ids = {s.id for s in scenarios}
    # a few anchors from the seed corpus
    assert {"research_workflow_plan", "paper_title_needs_input", "memory_write_gate", "retrieval_recall"} <= ids
    # every turns-scenario has at least one expectation to check (except retrieval)
    for s in scenarios:
        if s.type == "turns":
            assert s.turns and any(t.expect for t in s.turns), s.id


def test_tag_filter_selects_subset():
    routing = load_scenarios(tag="routing")
    assert routing
    assert all("routing" in s.tags for s in routing)


def test_routing_corpus_covers_exact_active_skill_manifest():
    routed: set[str] = set()
    for scenario in load_scenarios():
        for turn in scenario.turns:
            selected = turn.expect.get("skill_selected")
            if selected and not str(selected).startswith("!"):
                routed.add(str(selected))
            # Unnamed figure.editable.pptx no longer binds livefigure without
            # VLM; the corpus still names that provider on the exclude list.
            for key in (
                "skills_include",
                "skills_executed_include",
                "skills_executed_exclude",
            ):
                routed.update(str(name) for name in (turn.expect.get(key) or []))

    assert routed == set(active_skill_names(ROOT / "skills"))


@pytest.mark.asyncio
async def test_bundled_capability_benchmark_passes_offline():
    report = await run_benchmark()
    assert isinstance(report, BenchmarkReport)
    assert report.results, "benchmark produced no results"
    # Deterministic seed corpus must be fully green — a regression here means a
    # real capability (routing / capability selection / guardrails / retrieval)
    # changed behaviour.
    failed = [r.scenario_id for r in report.results if not r.passed]
    assert not failed, f"scenarios regressed: {failed}"
    assert report.score == 1.0

    dims = {d.name for d in report.dimensions()}
    assert {"routing", "capability_selection", "guardrails", "retrieval"} <= dims


@pytest.mark.asyncio
async def test_report_to_dict_is_serializable_for_ci_trend():
    report = await run_benchmark(load_scenarios(tag="routing"))
    payload = report.to_dict()
    assert set(payload) >= {"score", "dimensions", "scenarios", "checks_passed", "checks_total"}
    assert payload["scenarios"]
    # round-trips through JSON without custom encoders
    import json

    assert json.loads(json.dumps(payload))["score"] == payload["score"]


def test_score_math_and_dimension_aggregation_are_correct():
    # A pure unit check of the scoring model, independent of the agent.
    checks = [
        CheckOutcome("s1", "routing", "kind", True),
        CheckOutcome("s1", "guardrails", "skills_exclude", False, "boom"),
    ]
    report = BenchmarkReport(results=[ScenarioResult("s1", "t", ("routing",), checks)])
    assert report.total_checks == 2
    assert report.passed_checks == 1
    assert report.score == 0.5
    assert report.scenarios_passed == 0  # one check failed → scenario failed
    by_name = {d.name: d for d in report.dimensions()}
    assert by_name["routing"] == DimensionScore("routing", 1, 1)
    assert by_name["guardrails"].rate == 0.0


def test_scenario_dataclasses_default_sensibly():
    turn = ScenarioTurn(user="hi")
    assert turn.plan is None and turn.drain is False and turn.expect == {}
    sc = Scenario(id="x", title="X")
    assert sc.type == "turns" and sc.channel == "cli" and sc.dimensions == ("uncategorized",)


@pytest.mark.asyncio
async def test_routing_plan_is_not_counted_as_execution_evidence():
    plan = {
        "selected_skills": [
            {
                "skill": "livefigure",
                "matched_capabilities": ["figure.editable.pptx"],
            }
        ]
    }

    class Tasks:
        async def get_task(self, _task_id: str):
            return SimpleNamespace(plan_json=plan)

    agent = SimpleNamespace(tasks=Tasks())
    result = SimpleNamespace(
        task_id="task-1",
        submitted_workflow_ids=[],
        submitted_subtask_ids=[],
        drained_results=[],
    )

    routing = await _collect_routing_steps(agent, result)
    executed = await _collect_execution_steps(agent, result)

    assert routing == [
        {"skill_name": "livefigure", "capability": "figure.editable.pptx"}
    ]
    assert executed == []


@pytest.mark.asyncio
async def test_submitted_pending_work_is_not_counted_as_execution_evidence():
    class Runtime:
        async def get_workflow_run(self, _workflow_id: str):
            return SimpleNamespace(
                status="pending",
                plan_json={
                    "steps": [
                        {
                            "id": "figure",
                            "skill_name": "livefigure",
                            "capability": "figure.editable.pptx",
                        }
                    ]
                },
            )

        async def list_workflow_steps(self, _workflow_id: str):
            return [
                SimpleNamespace(
                    step_key="figure",
                    skill_name="livefigure",
                    capability="figure.editable.pptx",
                    status="pending",
                )
            ]

        async def get_subtask(self, _subtask_id: str):
            return SimpleNamespace(
                skill_name="livefigure",
                status="pending",
                input_json={"input": "editable figure"},
            )

    agent = SimpleNamespace(runtime=Runtime(), registry=SimpleNamespace(get=lambda _name: None))
    result = SimpleNamespace(
        submitted_workflow_ids=["workflow-1"],
        submitted_subtask_ids=["subtask-1"],
        drained_results=[],
    )

    executed = await _collect_execution_steps(agent, result)

    assert executed == []


@pytest.mark.asyncio
async def test_gateway_admission_rejection_is_not_counted_as_skill_execution():
    class Runtime:
        async def list_workflow_steps(self, _workflow_id: str):
            return [
                SimpleNamespace(
                    step_key="figure",
                    skill_name="livefigure",
                    capability="figure.editable.pptx",
                    status="failed",
                    started_at=object(),
                    result_json={
                        "status": "error",
                        "action_required": {"kind": "configure"},
                    },
                )
            ]

        async def get_subtask(self, _subtask_id: str):
            return None

    agent = SimpleNamespace(runtime=Runtime(), registry=SimpleNamespace(get=lambda _name: None))
    result = SimpleNamespace(
        submitted_workflow_ids=["workflow-1"],
        submitted_subtask_ids=[],
        drained_results=[],
    )

    assert await _collect_execution_steps(agent, result) == []


@pytest.mark.asyncio
async def test_engine_failure_is_still_counted_as_skill_execution():
    class Runtime:
        async def list_workflow_steps(self, _workflow_id: str):
            return [
                SimpleNamespace(
                    step_key="figure",
                    skill_name="livefigure",
                    capability="figure.editable.pptx",
                    status="failed",
                    started_at=object(),
                    result_json={
                        "status": "error",
                        "error_info": {"code": "render_failed"},
                    },
                )
            ]

        async def get_subtask(self, _subtask_id: str):
            return None

    agent = SimpleNamespace(runtime=Runtime(), registry=SimpleNamespace(get=lambda _name: None))
    result = SimpleNamespace(
        submitted_workflow_ids=["workflow-1"],
        submitted_subtask_ids=[],
        drained_results=[],
    )

    assert await _collect_execution_steps(agent, result) == [
        {
            "id": "figure",
            "skill_name": "livefigure",
            "capability": "figure.editable.pptx",
            "status": "failed",
        }
    ]


def test_routing_and_execution_expectations_use_different_evidence():
    turn = ScenarioTurn(
        user="make an editable figure",
        expect={
            "capabilities_include": ["figure.editable.pptx"],
            "capabilities_executed": ["artifact.figure"],
            "skill_selected": "livefigure",
            "skills_executed_exclude": ["livefigure"],
            "action_required": {
                "kind": "configure",
                "command": "omni config vlm",
            },
        },
    )
    result = SimpleNamespace(
        kind="needs_input",
        terminated_reason="",
        settlement_status="",
        text="Configure the VLM first.",
        drained_results=[
            {
                "status": "failed",
                "result": {
                    "status": "error",
                    "action_required": {
                        "kind": "configure",
                        "command": "omni config vlm",
                    },
                },
            }
        ],
        tool_trace=[],
    )

    checks = _evaluate_turn(
        "missing-vlm",
        turn,
        result,
        routing_steps=[
            {"skill_name": "livefigure", "capability": "figure.editable.pptx"}
        ],
        execution_steps=[
            {"skill_name": "scientific-figure", "capability": "artifact.figure"}
        ],
        llm=ScenarioLLM(),
        channel="cli",
        approval_events=[],
    )

    assert {(check.name, check.passed) for check in checks} == {
        ("capabilities_include", True),
        ("capabilities_executed", True),
        ("skill_selected", True),
        ("skills_executed_exclude", True),
        ("action_required", True),
    }


def test_skills_exclude_remains_a_routing_guardrail() -> None:
    turn = ScenarioTurn(
        user="make an editable figure",
        expect={"skills_exclude": ["livefigure"]},
    )
    result = SimpleNamespace(
        kind="needs_input",
        terminated_reason="vlm_not_configured",
        settlement_status="needs_input",
        text="Configure the VLM first.",
    )

    checks = _evaluate_turn(
        "missing-vlm",
        turn,
        result,
        routing_steps=[
            {"skill_name": "livefigure", "capability": "figure.editable.pptx"}
        ],
        execution_steps=[],
        llm=ScenarioLLM(),
        channel="cli",
        approval_events=[],
    )

    assert len(checks) == 1
    assert checks[0].name == "skills_exclude"
    assert checks[0].passed is False
