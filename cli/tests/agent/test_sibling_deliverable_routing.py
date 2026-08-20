"""A single task skill must not swallow independent deliverables.

BUG-01 / Xu: the planner named research.ideation then said react_fallback
would do code, experiment, and figures — but the sealed plan was one
ideation step. Literature failure then made the whole turn 0/1. Workflow
proposals already become a capable turn; this pins the remaining hole:
``single_skill_task`` that drops every capability after the first, or that
routes ideation-plus-figure through qa_plus_artifact.
"""

from __future__ import annotations

from omni.agent.intent_plan import IntentType
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.skills_runtime.registry import SkillRegistry


def _planner() -> IntentPlanner:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return IntentPlanner(registry)


def test_ideation_alone_still_binds_the_task_skill() -> None:
    plan = _planner().plan_from_proposal(
        "调研隐空间干预如何提升 LLM 的 agent 能力，给出研究设想。",
        ModelPlanProposal(
            intent_type="single_skill_task",
            required_capabilities=["research.ideation"],
            outputs=["answer"],
            confidence=0.9,
            rationale="one task provider spans the request",
        ),
        task_id="ideation-only",
    )

    assert plan.intent_type is IntentType.SINGLE_SKILL_TASK
    assert [sel.skill for sel in plan.selected_skills] == ["research-ideation"]


def test_ideation_plus_figure_is_sequenced_live_not_swallowed() -> None:
    """Dropping artifact.figure on the floor is how a 1-step workflow is born."""
    plan = _planner().plan_from_proposal(
        "先做研究设想，再画一张方法架构图。",
        ModelPlanProposal(
            intent_type="single_skill_task",
            required_capabilities=["research.ideation", "artifact.figure"],
            outputs=["answer", "artifact"],
            confidence=0.88,
            rationale="ideation then react_fallback for the figure",
        ),
        task_id="ideation-plus-figure",
    )

    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert "answer" in plan.outputs
    assert "artifact" in plan.outputs or "artifact.figure" in plan.outputs


def test_ideation_plus_manuscript_is_sequenced_live() -> None:
    plan = _planner().plan_from_proposal(
        "调研并写成一篇短稿。",
        ModelPlanProposal(
            intent_type="single_skill_task",
            required_capabilities=["research.ideation"],
            outputs=["answer", "draft.manuscript"],
            confidence=0.86,
            rationale="ideation covers the survey; writing is separate",
        ),
        task_id="ideation-plus-manuscript",
    )

    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert "draft.manuscript" in plan.outputs


def test_qa_plus_figure_pair_is_unchanged() -> None:
    plan = _planner().plan_from_proposal(
        "用一段话解释 transformer，并画架构图。",
        ModelPlanProposal(
            intent_type="qa_plus_artifact",
            required_capabilities=["qa.grounded", "artifact.figure"],
            outputs=["answer", "artifact"],
            confidence=0.91,
            rationale="short answer plus a figure",
        ),
        task_id="qa-plus-figure",
    )

    assert plan.intent_type is IntentType.QA_PLUS_ARTIFACT


def test_literature_search_alone_stays_on_native_react() -> None:
    plan = _planner().plan_from_proposal(
        "检索 RAG factuality 的文献。",
        ModelPlanProposal(
            intent_type="single_skill_task",
            required_capabilities=["literature.search"],
            outputs=["sources"],
            confidence=0.84,
            rationale="just search",
        ),
        task_id="lit-only",
    )

    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert plan.selected_skills == []
    assert plan.tool_policy.allows("search_literature")
    assert not plan.tool_policy.allows("run_skill")


def test_literature_search_plus_synthesis_stays_on_capable_react() -> None:
    plan = _planner().plan_from_proposal(
        "帮我调研如何利用隐空间干预的方式提升LLM的Agentic能力",
        ModelPlanProposal(
            intent_type="workflow",
            required_capabilities=["literature.search", "synthesis.final"],
            outputs=["sources", "draft.section"],
            confidence=0.88,
            rationale="written survey",
        ),
        task_id="survey-live",
    )

    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert plan.selected_skills == []
    assert plan.tool_policy.allows("search_literature")
    assert plan.tool_policy.allows("write_file")
    assert not plan.tool_policy.allows("bash")
    assert "draft.section" in plan.verification_plan.required_outputs


def test_literature_search_plus_ideation_is_sequenced_live() -> None:
    plan = _planner().plan_from_proposal(
        "先检索再做研究设想。",
        ModelPlanProposal(
            intent_type="single_skill_task",
            required_capabilities=["literature.search", "research.ideation"],
            outputs=["sources", "answer"],
            confidence=0.85,
            rationale="search then ideation",
        ),
        task_id="lit-plus-ideation",
    )

    assert plan.intent_type is IntentType.REACT_FALLBACK
