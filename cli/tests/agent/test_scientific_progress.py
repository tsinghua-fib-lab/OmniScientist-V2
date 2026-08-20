"""Lookup is not progress toward owed scientific outputs.

Historical user strings come from already-executed turns (reports/ manifests
and live routing fixtures). The change must not reroute those requests: advice
stays answer-only, a lone literature search stays on the host runner, and a
prior-work figure request may still open the earlier task once.
"""

from __future__ import annotations

import pytest

from omni.agent.intent_plan import IntentType
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolInvocationRecord, ToolSpec
from omni.core.scientific_progress import (
    lookup_pressure,
    this_turn_research_evidence,
)
from omni.runtime.remaining import plan_owes_scientific_outputs
from omni.skills_runtime.registry import SkillRegistry
from tests.conftest import ScriptedLLM

# Executed-turn user inputs (manifest titles / live fixtures).
SURVEY = "帮我调研如何利用隐空间干预的方式提升LLM的Agentic能力"
ADVICE_PAPER = "该如何才能写好顶会论文？"
ADVICE_RESEARCH = "请告诉我如何做好科研"
FIGURE_AND_DRAFT = (
    "为 智能体 系统综述准备材料：获取 Attention Is All You Need 摘要，"
    "并生成包含 query 、retriever、reranker、LLM 的科研架构图。并输出一篇论文 Draft"
)
SLIDES = "请做一组会PPT：Transformer注意力机制"
LIT_ONLY = "帮我做联邦学习的文献调研"
PRIOR_FIGURE = "你最近给我生成的架构图是讲的什么啊，给我重新生成一份吧"


def _planner() -> IntentPlanner:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return IntentPlanner(registry)


def _lookup_tools() -> list[ToolSpec]:
    query = {"type": "object", "properties": {"query": {"type": "string"}}}
    return [
        ToolSpec("memory_search", "lookup", query),
        ToolSpec("memory_get", "get", {"type": "object", "properties": {"id": {"type": "string"}}}),
        ToolSpec("search_tasks", "tasks", query),
        ToolSpec("search_literature", "search", query),
        ToolSpec("bash", "shell", {"type": "object", "properties": {"command": {"type": "string"}}}),
        ToolSpec("get_task", "task", {"type": "object", "properties": {"task_id": {"type": "string"}}}),
    ]


async def _ok_invoker(name: str, args: dict) -> dict:
    if name == "search_literature":
        return {"matches": [{"title": "paper"}]}
    if name == "bash":
        return {"exit_code": 0, "stdout": "ok"}
    return {"matches": [{"id": "m1", "summary": args.get("query") or "hit"}]}


# ── unit: pressure / evidence ────────────────────────────────────────────────
def test_lookup_pressure_ignores_answer_only_turns() -> None:
    trace = [
        ToolInvocationRecord(name="memory_search", arguments={"query": "a"}, status="succeeded"),
        ToolInvocationRecord(name="memory_search", arguments={"query": "b"}, status="succeeded"),
        ToolInvocationRecord(name="get_task", arguments={"task_id": "x"}, status="succeeded"),
    ]
    assert lookup_pressure(trace, owed=False) == 0
    assert lookup_pressure(trace, owed=True) == 3


def test_a_produce_call_clears_lookup_pressure() -> None:
    trace = [
        ToolInvocationRecord(name="memory_search", arguments={"query": "a"}, status="succeeded"),
        ToolInvocationRecord(
            name="search_literature", arguments={"query": SURVEY}, status="succeeded"
        ),
    ]
    assert lookup_pressure(trace, owed=True) == 0
    assert this_turn_research_evidence(trace) is True


def test_bash_between_lookups_does_not_reset_the_streak() -> None:
    trace = [
        ToolInvocationRecord(name="memory_search", arguments={"query": "a"}, status="succeeded"),
        ToolInvocationRecord(name="bash", arguments={"command": "ls"}, status="succeeded"),
        ToolInvocationRecord(name="memory_search", arguments={"query": "b"}, status="succeeded"),
    ]
    assert lookup_pressure(trace, owed=True) == 2
    assert this_turn_research_evidence(trace) is False


def test_drained_skill_counts_as_this_turn_research() -> None:
    assert this_turn_research_evidence([], [{"subtask_id": "s1", "result": {"status": "ok"}}])


def test_empty_funnel_is_not_this_turn_research() -> None:
    empty = {
        "status": "partial",
        "warning": "Literature search returned zero relevant papers for the generated queries.",
        "steps": {"search": {"queries": ["latent space"], "paper_count": 0, "papers": []}},
    }
    trace = [
        ToolInvocationRecord(
            name="run_skill",
            arguments={"skill_name": "research-ideation"},
            result={"status": "degraded", "n_kept": 0, "queries": ["latent space"], "result": empty},
            status="succeeded",
        )
    ]
    assert this_turn_research_evidence(trace) is False
    assert this_turn_research_evidence([], [{"subtask_id": "s1", "result": empty}]) is False
    assert lookup_pressure(
        [
            ToolInvocationRecord(name="memory_search", arguments={"query": "a"}, status="succeeded"),
            *trace,
            ToolInvocationRecord(name="memory_search", arguments={"query": "b"}, status="succeeded"),
        ],
        owed=True,
    ) == 1


# ── ReAct loop ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_distinct_memory_queries_are_no_progress_when_a_draft_is_owed() -> None:
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("1", "memory_search", {"query": "latent space"})]
            ),
            ChatWithToolsResult(
                tool_calls=[ToolCall("2", "memory_search", {"query": "agentic intervention"})]
            ),
            ChatWithToolsResult(content="I will increment the v3 report."),
        ]
    )
    result = await ReActLoopAgent(
        llm,
        _ok_invoker,
        max_iterations=8,
        no_progress_threshold=2,
        owes_scientific_outputs=True,
    ).run(system_prompt="s", user_message=SURVEY, tools=_lookup_tools())

    assert [record.name for record in result.tool_trace] == ["memory_search", "memory_search"]
    assert "no_progress" in result.terminated_reason
    assert "search_literature" not in result.tool_names()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_opening_parallel_lookups_are_steered_not_stopped() -> None:
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
            ChatWithToolsResult(content="Searching new papers since the earlier survey."),
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
    assert llm.calls == 3


async def test_one_lookup_then_literature_search_is_progress() -> None:
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("1", "memory_search", {"query": SURVEY})]
            ),
            ChatWithToolsResult(
                tool_calls=[ToolCall("2", "search_literature", {"query": SURVEY})]
            ),
            ChatWithToolsResult(content="Searching new papers since the earlier survey."),
        ]
    )
    result = await ReActLoopAgent(
        llm,
        _ok_invoker,
        max_iterations=6,
        no_progress_threshold=2,
        owes_scientific_outputs=True,
    ).run(system_prompt="s", user_message=SURVEY, tools=_lookup_tools())

    assert result.tool_names() == ["memory_search", "search_literature"]
    assert result.terminated_reason == "done"


@pytest.mark.asyncio
async def test_advice_turns_may_keep_looking_up() -> None:
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(tool_calls=[ToolCall("1", "memory_search", {"query": "papers"})]),
            ChatWithToolsResult(tool_calls=[ToolCall("2", "memory_search", {"query": "writing"})]),
            ChatWithToolsResult(content="Here is how to write a paper."),
        ]
    )
    result = await ReActLoopAgent(
        llm,
        _ok_invoker,
        max_iterations=6,
        no_progress_threshold=2,
        owes_scientific_outputs=False,
    ).run(system_prompt="s", user_message=ADVICE_PAPER, tools=_lookup_tools())

    assert result.tool_names() == ["memory_search", "memory_search"]
    assert result.terminated_reason == "done"
    assert result.content == "Here is how to write a paper."


# ── historical user inputs: routing must not change ──────────────────────────
def test_survey_pair_is_literature_plus_writing_only() -> None:
    from omni.agent.capabilities import is_survey_pair

    assert is_survey_pair(["literature.search", "synthesis.final"])
    assert is_survey_pair(["literature.search"], ["draft.section"])
    assert not is_survey_pair(["literature.search"])
    assert not is_survey_pair(["literature.search", "research.ideation"])
    assert not is_survey_pair(["literature.search", "synthesis.final", "artifact.figure"])


def test_survey_with_search_and_write_still_owes_a_draft() -> None:
    plan = _planner().plan_from_proposal(
        SURVEY,
        ModelPlanProposal(
            intent_type="single_skill_task",
            required_capabilities=["literature.search", "synthesis.final"],
            outputs=["sources", "draft.section"],
            confidence=0.88,
            rationale="written survey",
        ),
        task_id="hist-survey",
    )
    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert plan.selected_skills == []
    assert plan.tool_policy.allows("search_literature")
    assert plan.tool_policy.allows("write_file")
    assert plan_owes_scientific_outputs(plan)
    assert "draft.section" in plan.verification_plan.required_outputs


def test_offline_survey_does_not_invent_a_draft_debt() -> None:
    """No survey-specific binding: a model-less plan stays answer-only."""
    plan = _planner().plan(SURVEY, task_id="hist-survey-offline")
    assert plan_owes_scientific_outputs(plan) is False


def test_advice_requests_do_not_owe_a_manuscript() -> None:
    for message, task_id in (
        (ADVICE_PAPER, "hist-advice-paper"),
        (ADVICE_RESEARCH, "hist-advice-research"),
    ):
        plan = _planner().plan(message, task_id=task_id)
        assert plan_owes_scientific_outputs(plan) is False


def test_literature_only_stays_on_the_native_search_tool() -> None:
    plan = _planner().plan_from_proposal(
        LIT_ONLY,
        ModelPlanProposal(
            intent_type="single_skill_task",
            required_capabilities=["literature.search"],
            outputs=["sources"],
            confidence=0.9,
            rationale="literature survey",
        ),
        task_id="hist-lit",
    )
    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert plan.selected_skills == []
    assert plan.tool_policy.allows("search_literature")
    assert not plan.tool_policy.allows("run_skill")
    assert plan_owes_scientific_outputs(plan) is False


def test_figure_and_draft_request_still_owes_both() -> None:
    plan = _planner().plan_from_proposal(
        FIGURE_AND_DRAFT,
        ModelPlanProposal(
            intent_type="qa_plus_artifact",
            required_capabilities=["qa.grounded", "artifact.figure", "synthesis.final"],
            outputs=["answer", "artifact.figure", "draft.section"],
            confidence=0.86,
            rationale="abstract, figure, draft",
        ),
        task_id="hist-figure-draft",
    )
    assert plan_owes_scientific_outputs(plan)
    assert "artifact.figure" in plan.verification_plan.required_outputs
    assert "draft.section" in plan.verification_plan.required_outputs


def test_slides_request_still_owes_a_deck() -> None:
    plan = _planner().plan(SLIDES, task_id="hist-slides")
    assert "artifact.slides" in plan.verification_plan.required_outputs
    assert plan_owes_scientific_outputs(plan)


def test_prior_figure_request_may_look_up_once() -> None:
    """Opening the earlier figure is the work's first step, not a loop."""
    trace = [
        ToolInvocationRecord(
            name="get_task", arguments={"task_id": "212f50b5"}, status="succeeded"
        )
    ]
    assert lookup_pressure(trace, owed=True) == 1
    assert this_turn_research_evidence(trace) is False
