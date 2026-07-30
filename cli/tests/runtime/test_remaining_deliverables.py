"""Named scientific outputs are a settlement contract, not a caption."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omni.agent.capabilities import contract_outputs
from omni.agent.intent_plan import IntentType
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.runtime.remaining import (
    remaining_deliverables,
    remaining_figure,
    remaining_slides,
    remaining_writing,
)
from omni.skills_runtime.registry import SkillRegistry


def test_figure_does_not_satisfy_a_manuscript_debt() -> None:
    figure = SimpleNamespace(
        kind="figure",
        title="RAG architecture",
        rel_path="figures/rag.png",
        mime="image/png",
        uri="artifact://fig",
    )
    remaining = remaining_deliverables(
        ["answer", "artifact.figure", "draft.manuscript"],
        [figure],
    )
    assert remaining == ["draft.manuscript"]
    assert remaining_writing(remaining) == ["draft.manuscript"]
    assert remaining_figure(remaining) == []


def test_dot_sidecar_does_not_count_as_the_figure() -> None:
    dot = SimpleNamespace(
        kind="figure",
        title="RAG",
        rel_path="figures/rag.dot",
        mime="text/vnd.graphviz",
        uri="artifact://dot",
    )
    remaining = remaining_deliverables(["artifact.figure"], [dot])
    assert remaining == ["artifact.figure"]
    assert remaining_figure(remaining) == ["artifact.figure"]


def test_markdown_report_satisfies_manuscript() -> None:
    report = SimpleNamespace(
        kind="report",
        title="Survey",
        rel_path="reports/survey.md",
        mime="text/markdown",
        uri="artifact://paper",
    )
    assert remaining_deliverables(["draft.manuscript", "draft.section"], [report]) == []


def test_answer_alone_is_not_an_artifact_debt() -> None:
    assert contract_outputs(["answer"]) == []
    assert remaining_deliverables(["answer"], []) == []


def test_figure_debt_is_host_fillable() -> None:
    remaining = remaining_deliverables(["artifact.figure", "draft.manuscript"], [])
    assert remaining_figure(remaining) == ["artifact.figure"]
    assert remaining_writing(remaining) == ["draft.manuscript"]


def test_pptx_plus_manuscript_is_not_writing_only() -> None:
    remaining = remaining_deliverables(["artifact.pptx", "draft.manuscript"], [])
    assert remaining == ["artifact.pptx", "draft.manuscript"]
    writing = remaining_writing(remaining)
    assert writing == ["draft.manuscript"]
    assert set(writing) != set(remaining)


def test_slide_debt_is_host_fillable_and_editable_pptx_is_not() -> None:
    remaining = remaining_deliverables(
        ["artifact.slides", "artifact.pptx", "draft.manuscript"], []
    )
    assert remaining_slides(remaining) == ["artifact.slides"]
    assert remaining_writing(remaining) == ["draft.manuscript"]


def test_react_fallback_copies_named_outputs_onto_verification() -> None:
    planner = IntentPlanner(SkillRegistry(load_settings()))
    proposal = ModelPlanProposal.from_payload(
        {
            "intent_type": "workflow",
            "required_capabilities": [
                "paper.fetch.arxiv",
                "artifact.figure",
                "draft.manuscript",
            ],
            "outputs": ["artifact.figure", "draft.manuscript", "answer"],
            "workflow_steps": [
                {"id": "fetch", "capability": "paper.fetch.arxiv"},
                {"id": "fig", "capability": "artifact.figure"},
                {"id": "paper", "capability": "draft.manuscript"},
            ],
            "confidence": 0.8,
            "rationale": "survey materials sequenced live",
        }
    )
    plan = planner.plan_from_proposal(
        "Attention abstract, RAG figure, and a survey paper",
        proposal,
        task_id="task-829bfee2",
    )
    assert plan.intent_type == IntentType.REACT_FALLBACK
    assert "artifact.figure" in plan.outputs
    assert "draft.manuscript" in plan.outputs
    required = plan.verification_plan.required_outputs
    assert "artifact.figure" in required
    assert "draft.manuscript" in required
    assert "react.finished" in plan.verification_plan.required_events


def test_answer_only_proposal_still_binds_named_capabilities() -> None:
    """Incident 6978342b: ReAct floor with outputs=answer dropped the paper debt."""
    planner = IntentPlanner(SkillRegistry(load_settings()))
    proposal = ModelPlanProposal.from_payload(
        {
            "intent_type": "workflow",
            "required_capabilities": ["artifact.figure", "draft.manuscript"],
            "outputs": ["answer"],
            "workflow_steps": [
                {"id": "fig", "capability": "artifact.figure"},
                {"id": "paper", "capability": "draft.manuscript"},
            ],
            "confidence": 0.8,
            "rationale": "summary, figure, and paper sequenced live",
        }
    )
    plan = planner.plan_from_proposal(
        "Attention abstract, RAG figure, and a survey paper",
        proposal,
        task_id="task-6978342b",
    )
    assert plan.intent_type == IntentType.REACT_FALLBACK
    assert "artifact.figure" in plan.outputs
    assert "draft.manuscript" in plan.outputs
    required = plan.verification_plan.required_outputs
    assert "artifact.figure" in required
    assert "draft.manuscript" in required


def test_figure_and_paper_wording_binds_the_contract_without_capabilities() -> None:
    from omni.runtime.remaining import infer_figure_and_paper_outputs, infer_slide_outputs

    assert infer_figure_and_paper_outputs(
        "请总结 Attention，画一张 RAG 架构图，并输出一篇论文"
    ) == ["artifact.figure", "draft.manuscript"]
    assert infer_figure_and_paper_outputs("请画一张架构图") == []
    assert infer_figure_and_paper_outputs("写一篇综述") == []
    assert infer_figure_and_paper_outputs(
        "我想了解我前面问的 RAG 系统综述 & Transformer 架构图，看看这个任务的产出是什么"
    ) == []
    assert infer_slide_outputs("请做一组会PPT") == ["artifact.slides"]
    assert infer_slide_outputs("生成 PPT 和综述材料") == ["artifact.slides"]
    assert infer_slide_outputs("Make one editable single-slide figure") == []
    assert infer_figure_and_paper_outputs("生成 PPT 和综述材料") == []
    assert infer_figure_and_paper_outputs(
        "为 loop engineering 系统综述准备材料，并生成架构图，并输出一份详细的介绍ppt"
    ) == ["artifact.figure"]
    assert infer_figure_and_paper_outputs(
        "为 RAG 系统综述准备材料，生成架构图，并输出一篇论文和一份PPT"
    ) == ["artifact.figure", "draft.manuscript"]


def test_skip_completed_skills_note_names_the_figure_and_the_paper_debt() -> None:
    from omni.runtime.remaining import skip_completed_skills_note

    figure = SimpleNamespace(
        kind="figure", title="RAG architecture", rel_path="figures/rag.png", mime="image/png",
    )
    note = skip_completed_skills_note(["draft.manuscript"], [figure])
    assert "RAG architecture" in note
    assert "draft.manuscript" in note
    assert "do not rerun" in note.lower()
    assert "this task" in note.lower()
    assert "another task" in note.lower()


def test_host_remaining_summary_reports_missing_manuscript() -> None:
    from omni.cli.commands.tasks_cmd import _host_remaining_summary

    plan = {
        "outputs": ["artifact.figure", "draft.manuscript"],
        "verification_plan": {"required_outputs": ["artifact.figure", "draft.manuscript"]},
    }
    missing = _host_remaining_summary(plan, [("figure", "RAG architecture", "figures/rag.png")])
    assert missing == "missing draft.manuscript"
    filled = _host_remaining_summary(
        plan,
        [
            ("figure", "RAG architecture", "figures/rag.png"),
            ("report", "Survey", "reports/survey.md"),
        ],
    )
    assert filled == "all named deliverables present"


@pytest.mark.asyncio
async def test_remaining_retry_context_says_other_tasks_do_not_count() -> None:
    from omni.runtime.remaining import remaining_retry_context

    original = SimpleNamespace(
        plan_json={
            "outputs": ["artifact.figure"],
            "verification_plan": {"required_outputs": ["artifact.figure"]},
        }
    )
    current = SimpleNamespace(retry_of_task_id="orig-1")
    tasks = SimpleNamespace(
        get_task=AsyncMock(side_effect=lambda tid: current if tid == "retry-1" else original)
    )
    artifacts = SimpleNamespace(list_by_task=AsyncMock(return_value=[]))

    note = await remaining_retry_context(tasks, artifacts, "retry-1")
    assert "another task" in note.lower()
    assert "this task" in note.lower()
    assert "artifact.figure" in note
