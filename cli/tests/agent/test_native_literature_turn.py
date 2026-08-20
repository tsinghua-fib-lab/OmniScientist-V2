"""Literature retrieval stays on the native ReAct tool, not a compiled skill.

P1-01: a named ``search_literature`` token, or a lone ``literature.search``
capability, must not become ``SINGLE_SKILL_TASK`` / ``run_skill``. ``$skill``
protocol still wins. Survey pairs stay on capable ReAct with ``write_file``.
"""

from __future__ import annotations

from omni.agent.boundary_router import BoundaryRouter, explicit_native_tool
from omni.agent.intent_plan import IntentType
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.skills_runtime.registry import SkillRegistry


def _planner() -> IntentPlanner:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return IntentPlanner(registry)


def test_named_search_literature_is_an_explicit_native_tool() -> None:
    text = (
        "调用 search_literature，query='large language model retrieval', rows=8。"
        "禁止 write_file、run_skill、spawn_subagents。"
    )
    assert explicit_native_tool(text) == "search_literature"

    registry = SkillRegistry(load_settings())
    registry.build_index()
    decision = BoundaryRouter(registry).route(text)
    assert decision is not None
    assert decision.kind == "explicit_tool"
    assert decision.tool == "search_literature"

    plan = _planner().boundary_plan(text, task_id="named-lit")
    assert plan is not None
    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert plan.selected_skills == []
    assert plan.tool_policy.allows("search_literature")
    assert not plan.tool_policy.allows("run_skill")
    assert not plan.tool_policy.allows("write_file")
    assert not plan.tool_policy.allows("spawn_subagents")
    assert "sources" in plan.verification_plan.required_outputs


def test_dollar_openalex_still_compiles_as_an_explicit_skill() -> None:
    plan = _planner().boundary_plan(
        "$openalex-search federated learning",
        task_id="named-skill",
    )
    assert plan is not None
    assert plan.intent_type is IntentType.SINGLE_SKILL_TASK
    assert [selection.skill for selection in plan.selected_skills] == ["openalex-search"]


def test_skill_protocol_wins_over_a_native_tool_token() -> None:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    decision = BoundaryRouter(registry).route(
        "$openalex-search then search_literature for more"
    )
    assert decision is not None
    assert decision.kind == "explicit_skill"
    assert decision.skill == "openalex-search"


def test_lone_literature_search_proposal_does_not_select_a_skill() -> None:
    plan = _planner().plan_from_proposal(
        "检索 RAG 文献，只列出 source_id。",
        ModelPlanProposal(
            intent_type="single_skill_task",
            required_capabilities=["literature.search"],
            outputs=["sources"],
            capability_inputs={
                "literature.search": {
                    "query": "large language model retrieval augmented generation"
                }
            },
            confidence=0.9,
            rationale="literature only",
        ),
        task_id="lone-lit",
    )
    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert plan.selected_skills == []
    assert plan.tool_policy.allows("search_literature")
    assert not plan.tool_policy.allows("run_skill")
    assert (
        plan.capability_inputs["literature.search"]["query"]
        == "large language model retrieval augmented generation"
    )
    from omni.agent.plan_runner_utils import apply_retrieve_only_projection, is_retrieve_only_plan

    assert is_retrieve_only_plan(plan)
    source_ids = [f"src-{index:02d}" for index in range(22)]
    projected = apply_retrieve_only_projection(
        plan,
        source_ids=source_ids,
        model_text="listed 17 of 22 ids; preview truncated",
    )
    assert projected.splitlines() == source_ids
    assert "truncated" not in projected
