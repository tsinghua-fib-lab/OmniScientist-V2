"""Exact user turns that must keep working after today's closer change.

These strings are live requests, not paraphrases. A survey closer that
swallows a figure, or a lookup fuse that stops a review, is a regression.
"""

from __future__ import annotations

import pytest

from omni.agent.intent_plan import IntentType, SkillSelection, VerificationPlan
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.plan_factory import ASSISTANT_BLOCKED_TOOLS
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.scientific_progress import lookup_pressure
from omni.runtime.remaining import (
    infer_figure_and_paper_outputs,
    infer_slide_outputs,
    plan_owes_scientific_outputs,
    survey_closer_eligible,
)
from omni.skills_runtime.registry import SkillRegistry
from tests.conftest import ScriptedLLM

RAG = (
    "为 RAG 系统综述准备材料：获取 Attention Is All You Need 摘要，"
    "并生成包含 query 、retriever、reranker、LLM 的科研架构图。并输出一篇论文"
)
SURVEY = "帮我调研如何利用隐空间干预的方式提升LLM的Agentic能力"
REVIEW = (
    "仔细review 今天 push 到master\n"
    "    上的代码，对应变动都是符合预期的，没有遗漏，没有功能退化，都是符合omni\n"
    "    设计的优化，且实现是对标了 codex、openclaw、opencode 源码设计的。\n"
    "      不做代码改动"
)
LOOP = (
    "为 智能体 loop engineering 系统综述准备材料：获取 Attention Is All You Need 摘要，"
    "并生成包含 query、retriever、reranker、LLM的科研架构图。并输出一篇论文"
)
PPT1 = "为 智能体 loop engineering 系统综述准备材料，并输出一份详细的介绍ppt"
PPT2 = "为 智能体 loop engineering 系统综述，出一份详细的PPT 介绍材料"

_FIGURE_PAPER_PROPOSAL = ModelPlanProposal(
    intent_type="workflow",
    required_capabilities=["paper.fetch.arxiv", "artifact.figure", "synthesis.final"],
    outputs=["answer", "artifact.figure", "draft.manuscript"],
    workflow_steps=[
        {"id": "fetch", "capability": "paper.fetch.arxiv"},
        {"id": "fig", "capability": "artifact.figure"},
        {"id": "paper", "capability": "synthesis.final"},
    ],
    confidence=0.9,
    rationale="abstract, architecture figure, and paper",
)
_SURVEY_PROPOSAL = ModelPlanProposal(
    intent_type="workflow",
    required_capabilities=["literature.search", "synthesis.final"],
    outputs=["sources", "draft.section"],
    confidence=0.88,
    rationale="written survey",
)
_CONFUSED_SURVEY_PROPOSAL = ModelPlanProposal(
    intent_type="workflow",
    required_capabilities=["literature.search", "synthesis.final"],
    outputs=["sources", "draft.section"],
    confidence=0.7,
    rationale="planner forgot the figure",
)
_REVIEW_PROPOSAL = ModelPlanProposal(
    intent_type="react_fallback",
    required_capabilities=[],
    outputs=["answer"],
    confidence=0.8,
    rationale="review today's master commits in the working directory",
)
_TASK_REVIEW_PROPOSAL = ModelPlanProposal(
    intent_type="react_fallback",
    required_capabilities=["task.review"],
    outputs=["answer"],
    confidence=0.8,
    rationale="review recent tasks",
)


def _planner() -> IntentPlanner:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return IntentPlanner(registry)


def _lookup_tools() -> list[ToolSpec]:
    query = {"type": "object", "properties": {"query": {"type": "string"}}}
    return [
        ToolSpec("memory_search", "lookup", query),
        ToolSpec("search_tasks", "tasks", query),
        ToolSpec("search_literature", "search", query),
        ToolSpec("run_skill", "skill", {"type": "object", "properties": {"skill": {"type": "string"}}}),
    ]


async def _ok_invoker(name: str, args: dict) -> dict:
    if name == "search_literature":
        return {"matches": [{"title": "paper"}]}
    if name == "run_skill":
        return {"status": "succeeded", "skill": args.get("skill") or "ok"}
    return {"matches": [{"id": "m1"}]}


# ── wording binds the contract ───────────────────────────────────────────────
@pytest.mark.parametrize("message", [RAG, LOOP])
def test_figure_plus_paper_wording_binds_both_deliverables(message: str) -> None:
    assert infer_figure_and_paper_outputs(message) == ["artifact.figure", "draft.manuscript"]


@pytest.mark.parametrize("message", [SURVEY, REVIEW, PPT1, PPT2])
def test_survey_and_review_wording_do_not_bind_a_figure(message: str) -> None:
    assert infer_figure_and_paper_outputs(message) == []


@pytest.mark.parametrize("message", [PPT1, PPT2])
def test_ppt_survey_wording_binds_slides_not_a_manuscript(message: str) -> None:
    assert infer_slide_outputs(message) == ["artifact.slides"]
    assert infer_figure_and_paper_outputs(message) == []


# ── latent-space survey: capable ReAct with write_file ───────────────────────
def test_latent_survey_uses_capable_react_with_write_file() -> None:
    plan = _planner().plan_from_proposal(SURVEY, _SURVEY_PROPOSAL, task_id="live-survey")
    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert plan.selected_skills == []
    assert plan.tool_policy.allows("search_literature")
    assert plan.tool_policy.allows("write_file")
    assert not plan.tool_policy.allows("bash")
    assert "draft.section" in plan.verification_plan.required_outputs
    assert "artifact.figure" not in plan.verification_plan.required_outputs
    assert plan_owes_scientific_outputs(plan)
    assert survey_closer_eligible(plan)


# ── RAG / loop-engineering: live sequence, both debts ────────────────────────
@pytest.mark.parametrize("message,task_id", [(RAG, "live-rag"), (LOOP, "live-loop")])
def test_figure_and_paper_requests_stay_live_and_owe_both(
    message: str, task_id: str
) -> None:
    plan = _planner().plan_from_proposal(message, _FIGURE_PAPER_PROPOSAL, task_id=task_id)
    assert plan.intent_type is IntentType.REACT_FALLBACK
    required = plan.verification_plan.required_outputs
    assert "artifact.figure" in required
    assert "draft.manuscript" in required or "draft.section" in required
    assert plan_owes_scientific_outputs(plan)
    assert survey_closer_eligible(plan) is False
    assert plan.tool_policy.allows("write_file")
    assert plan.tool_policy.allows("edit_file")
    assert not plan.tool_policy.allows("bash")
    assert not plan.tool_policy.allows("run_compute")


@pytest.mark.parametrize("message,task_id", [(RAG, "live-rag-confused"), (LOOP, "live-loop-confused")])
def test_figure_and_paper_are_not_swallowed_by_a_survey_proposal(
    message: str, task_id: str
) -> None:
    """A planner that only names literature+write must not drop the figure."""
    plan = _planner().plan_from_proposal(message, _CONFUSED_SURVEY_PROPOSAL, task_id=task_id)
    assert plan.intent_type is IntentType.REACT_FALLBACK
    required = plan.verification_plan.required_outputs
    assert "artifact.figure" in required
    assert "draft.manuscript" in required or "draft.section" in required
    assert survey_closer_eligible(plan) is False


# ── review today's master: no scientific debt, tools stay on ─────────────────
def test_master_review_is_a_capable_read_turn() -> None:
    plan = _planner().plan_from_proposal(REVIEW, _REVIEW_PROPOSAL, task_id="live-review")
    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert plan_owes_scientific_outputs(plan) is False
    assert survey_closer_eligible(plan) is False
    assert "draft.section" not in plan.verification_plan.required_outputs
    assert "artifact.figure" not in plan.verification_plan.required_outputs
    for blocked in ASSISTANT_BLOCKED_TOOLS:
        assert plan.tool_policy.allows(blocked) is False


def test_master_review_task_review_cap_does_not_owe_a_paper() -> None:
    plan = _planner().plan_from_proposal(REVIEW, _TASK_REVIEW_PROPOSAL, task_id="live-task-review")
    assert plan_owes_scientific_outputs(plan) is False
    assert survey_closer_eligible(plan) is False
    assert lookup_pressure([], owed=plan_owes_scientific_outputs(plan)) == 0


def test_offline_review_does_not_invent_scientific_debts() -> None:
    plan = _planner().plan(REVIEW, task_id="live-review-offline")
    assert plan_owes_scientific_outputs(plan) is False
    assert infer_figure_and_paper_outputs(REVIEW) == []


# ── ReAct: orientation is not a stall when a draft is owed ───────────────────
@pytest.mark.asyncio
async def test_survey_orientation_then_retrieve_is_progress() -> None:
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall("1", "search_tasks", {"query": SURVEY}),
                    ToolCall("2", "memory_search", {"query": SURVEY}),
                ]
            ),
            ChatWithToolsResult(
                tool_calls=[ToolCall("3", "search_literature", {"query": SURVEY})]
            ),
            ChatWithToolsResult(content="New survey underway."),
        ]
    )
    result = await ReActLoopAgent(
        llm,
        _ok_invoker,
        max_iterations=6,
        no_progress_threshold=2,
        owes_scientific_outputs=True,
    ).run(system_prompt="s", user_message=SURVEY, tools=_lookup_tools())
    assert result.tool_names() == ["search_tasks", "memory_search", "search_literature"]
    assert result.terminated_reason == "done"


@pytest.mark.asyncio
async def test_figure_and_paper_orientation_then_skill_is_progress() -> None:
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("1", "search_tasks", {"query": RAG})]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall("2", "run_skill", {"skill": "arxiv-fetch"}),
                ]
            ),
            ChatWithToolsResult(content="Fetching the paper and drawing the figure."),
        ]
    )
    result = await ReActLoopAgent(
        llm,
        _ok_invoker,
        max_iterations=6,
        no_progress_threshold=2,
        owes_scientific_outputs=True,
    ).run(system_prompt="s", user_message=RAG, tools=_lookup_tools())
    assert result.tool_names() == ["search_tasks", "run_skill"]
    assert result.terminated_reason == "done"


@pytest.mark.asyncio
async def test_review_turn_may_keep_looking_up() -> None:
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(tool_calls=[ToolCall("1", "search_tasks", {"query": "master"})]),
            ChatWithToolsResult(tool_calls=[ToolCall("2", "memory_search", {"query": "today"})]),
            ChatWithToolsResult(content="Today's master changes look sound."),
        ]
    )
    result = await ReActLoopAgent(
        llm,
        _ok_invoker,
        max_iterations=6,
        no_progress_threshold=2,
        owes_scientific_outputs=False,
    ).run(system_prompt="s", user_message=REVIEW, tools=_lookup_tools())
    assert result.tool_names() == ["search_tasks", "memory_search"]
    assert result.terminated_reason == "done"
    assert "sound" in result.content


def test_react_fallback_survey_wording_is_still_closer_eligible() -> None:
    from omni.agent.intent_plan import IntentPlan

    plan = IntentPlan(
        task_id="x",
        user_message=SURVEY,
        intent_type=IntentType.REACT_FALLBACK,
        outputs=["draft.section"],
        selected_skills=[],
        capability_inputs={},
        verification_plan=VerificationPlan(required_outputs=["draft.section"]),
    )
    assert survey_closer_eligible(plan)
    assert plan_owes_scientific_outputs(plan)


def test_ppt_survey_plan_is_not_closer_eligible() -> None:
    from omni.agent.intent_plan import IntentPlan

    plan = IntentPlan(
        task_id="x",
        user_message=PPT1,
        intent_type=IntentType.REACT_FALLBACK,
        outputs=["artifact.slides"],
        selected_skills=[],
        verification_plan=VerificationPlan(required_outputs=["artifact.slides"]),
    )
    assert infer_slide_outputs(PPT1) == ["artifact.slides"]
    assert survey_closer_eligible(plan) is False


def test_confused_survey_demote_keeps_the_retrieve_query() -> None:
    plan = _planner().plan_from_proposal(SURVEY, _CONFUSED_SURVEY_PROPOSAL, task_id="live-carry")
    # SURVEY is not figure+paper; the pair stays on capable ReAct with write_file.
    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert plan.tool_policy.allows("write_file")
    rag_plan = _planner().plan_from_proposal(
        RAG, _CONFUSED_SURVEY_PROPOSAL, task_id="live-rag-carry"
    )
    assert rag_plan.intent_type is IntentType.REACT_FALLBACK
    assert "literature.search" in rag_plan.capability_inputs
    assert rag_plan.capability_inputs["literature.search"].get("query")


def test_survey_closer_rejects_a_figure_and_paper_plan() -> None:
    from omni.agent.intent_plan import IntentPlan

    plan = IntentPlan(
        task_id="x",
        user_message=RAG,
        intent_type=IntentType.REACT_FALLBACK,
        outputs=["artifact.figure", "draft.manuscript"],
        selected_skills=[
            SkillSelection(
                skill="openalex-search",
                reason="search",
                matched_capabilities=["literature.search"],
            )
        ],
        verification_plan=VerificationPlan(
            required_outputs=["artifact.figure", "draft.manuscript"]
        ),
    )
    assert survey_closer_eligible(plan) is False
