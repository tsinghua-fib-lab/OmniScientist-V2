"""Natural-language black-box evaluation must not inject planner answers."""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.agent import OmniAgent
from omni.agent.capabilities import CAPABILITY_GROUNDED_QA
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.eval.blackbox import (
    BlackBoxScenario,
    BlackBoxTurn,
    load_blackbox_scenarios,
    run_blackbox_benchmark,
)
from omni.eval.coverage import target_capabilities
from tests.conftest import ScriptedLLM


def test_blackbox_loader_rejects_scripted_plan(tmp_path: Path) -> None:
    corpus = tmp_path / "bad.yaml"
    corpus.write_text(
        """
id: not-black-box
title: bad
turns:
  - user: hello
    plan: {intent_type: direct_answer}
    expect: {task_status: succeeded}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="plan"):
        load_blackbox_scenarios(corpus)


def test_blackbox_loader_rejects_root_level_fixture(tmp_path: Path) -> None:
    corpus = tmp_path / "bad-root.yaml"
    corpus.write_text(
        """
id: not-black-box-root
fixtures: [scientific-figure]
turns:
  - user: hello
    expect: {task_status: succeeded}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="fixtures"):
        load_blackbox_scenarios(corpus)


@pytest.mark.asyncio
async def test_blackbox_runs_real_agent_boundary_without_plan_injection(settings) -> None:  # noqa: ANN001
    scenario = BlackBoxScenario(
        id="self-knowledge-storage",
        title="Storage architecture from product docs",
        turns=(
            BlackBoxTurn(
                user="你的存储架构是如何实现的？",
                expect={
                    "task_status": "succeeded",
                    "output_contains_any": ["SQLite", "文件系统"],
                    "tools_include": ["docs_search"],
                    "terminated_reason_exclude": ["max_iterations"],
                },
            ),
        ),
    )

    async def agent_factory(attempt_settings):  # noqa: ANN001, ANN202
        agent = await OmniAgent.create(attempt_settings)
        agent.llm = ScriptedLLM(
            [
                ChatWithToolsResult(
                    tool_calls=[ToolCall("docs", "docs_search", {"query": "存储 架构 SQLite"})]
                ),
                ChatWithToolsResult(content="Omni 使用 SQLite 与文件系统组成的本地优先存储。"),
            ]
        )
        return agent

    report = await run_blackbox_benchmark(
        [scenario],
        settings=settings,
        repeats=2,
        concurrency=2,
        agent_factory=agent_factory,
    )

    assert report.attempted == 2
    assert report.skipped == 0
    assert report.success_rate == 1.0
    assert report.provenance_accuracy == 1.0
    assert report.mean_manual_rework == 0.0
    assert report.total_tokens > 0
    assert len({attempt.task_ids[0] for attempt in report.attempts}) == 2
    assert all(attempt.passed for attempt in report.attempts)
    assert all(attempt.cost["calls"] > 0 for attempt in report.attempts)
    scenario_metrics = report.to_dict()["scenarios"]
    assert scenario_metrics[0]["attempts"] == 2
    assert scenario_metrics[0]["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_blackbox_rejects_macos_private_absolute_path(settings) -> None:  # noqa: ANN001
    scenario = BlackBoxScenario(
        id="path-leak",
        title="No local path disclosure",
        turns=(
            BlackBoxTurn(
                user="Where is the result?",
                expect={"task_status": "succeeded", "no_absolute_paths": True},
            ),
        ),
    )

    async def agent_factory(attempt_settings):  # noqa: ANN001, ANN202
        agent = await OmniAgent.create(attempt_settings)
        agent.llm = ScriptedLLM(
            [ChatWithToolsResult(content="Result: /private/var/folders/local/secret.txt")]
        )
        return agent

    report = await run_blackbox_benchmark(
        [scenario],
        settings=settings,
        agent_factory=agent_factory,
    )

    assert report.attempted == 1
    assert report.success_rate == 0.0
    assert any(
        check["name"] == "no_absolute_paths" and not check["passed"]
        for check in report.attempts[0].checks
    )


def test_bundled_blackbox_corpus_contains_only_natural_language() -> None:
    scenarios = load_blackbox_scenarios()
    assert scenarios
    assert any(scenario.requires_model for scenario in scenarios)
    assert any(not scenario.requires_model for scenario in scenarios)
    assert all(turn.user and turn.expect for scenario in scenarios for turn in scenario.turns)


def test_bundled_product_scenarios_only_require_active_or_native_capabilities() -> None:
    expected = {
        str(capability)
        for scenario in load_blackbox_scenarios()
        for turn in scenario.turns
        for capability in turn.expect.get("capabilities_include", [])
    }

    assert expected <= target_capabilities() | {CAPABILITY_GROUNDED_QA}
