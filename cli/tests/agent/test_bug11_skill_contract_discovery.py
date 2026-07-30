"""BUG-11: the coordinator must call research-pptx, not explore its schema.

Fang Yi: find_skill / docs_search / glob / search_tasks burned the turn and
returned prose. Codex injects SKILL.md on select; Omni keeps typed contracts
and run_skill. The lookup has to return those fields, a one-step slides
workflow must stay on the host runner, and a deck request owes artifact.slides.
"""

from __future__ import annotations

import pytest

from omni.agent.intent_plan import IntentType
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.planner import IntentPlanner
from omni.agent.skill_lookup import FIND_SKILL_NEXT_ACTION
from omni.agent.tool_surface import ToolSurfaceBuilder
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import (
    ReActLoopAgent,
    ToolInvocationRecord,
    ToolSpec,
    _contract_hunt_pressure,
)
from omni.runtime.remaining import infer_slide_outputs, remaining_deliverables
from omni.skills_runtime.context import ExecContext, Tool
from omni.skills_runtime.registry import SkillRegistry
from tests.conftest import ScriptedLLM


def _planner() -> IntentPlanner:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return IntentPlanner(registry)


@pytest.mark.asyncio
async def test_find_skill_returns_research_pptx_contract_first() -> None:
    settings = load_settings()
    registry = SkillRegistry(settings)
    registry.build_index()

    async def _no_mcp(_ctx: ExecContext) -> list[Tool]:
        return []

    builder = ToolSurfaceBuilder(
        runtime=None, tasks=None, registry=registry, mcp_loader=_no_mcp
    )
    ctx = ExecContext(settings=settings, paths=settings.paths, channel="cli", db=None)
    tools = await builder.build(ctx, wait_for_tasks=True)
    find_skill = {tool.spec.name: tool for tool in tools}["find_skill"]

    found = await find_skill.handler({"query": "research-pptx"})
    names = [item["name"] for item in found["matches"]]
    assert names[0] == "research-pptx"
    card = found["matches"][0]
    assert "topic" in card["input_schema"]["properties"]
    assert card["instruction_field"] == "topic"
    assert card["call"]["skill_name"] == "research-pptx"
    assert "topic" in card["call"]["input"]
    assert found["next_action"] == FIND_SKILL_NEXT_ACTION


def test_one_step_slides_workflow_stays_on_the_host_runner() -> None:
    plan = _planner().plan_from_proposal(
        "请根据这篇论文做一组会PPT：Transformer注意力机制",
        ModelPlanProposal(
            intent_type="workflow",
            required_capabilities=["slides.generate"],
            workflow_steps=[
                {
                    "id": "deck",
                    "capability": "slides.generate",
                    "input": {"topic": "Transformer注意力机制", "talk_type": "group_meeting"},
                }
            ],
            outputs=["artifact.slides"],
            rationale="model labelled a lone deck as a workflow",
        ),
        task_id="bug11-one-step-slides",
    )

    assert plan.intent_type is IntentType.SINGLE_SKILL_TASK
    assert [selection.skill for selection in plan.selected_skills] == ["research-pptx"]
    assert plan.capability_inputs["slides.generate"]["topic"] == "Transformer注意力机制"
    assert "artifact.slides" in plan.verification_plan.required_outputs


def test_explicit_research_pptx_owes_a_deck() -> None:
    plan = _planner().boundary_plan(
        "$research-pptx 做一组会PPT：Transformer",
        task_id="bug11-explicit-slides",
    )
    assert plan is not None
    assert plan.intent_type is IntentType.SINGLE_SKILL_TASK
    assert "artifact.slides" in plan.verification_plan.required_outputs


def test_offline_slides_request_binds_the_deck_debt() -> None:
    plan = _planner().plan("请做一组会PPT：Transformer注意力机制", task_id="bug11-offline")
    assert "artifact.slides" in plan.verification_plan.required_outputs


def test_slide_wording_binds_a_deck_not_a_single_slide_figure() -> None:
    assert infer_slide_outputs("Turn this study into a complete thesis-defense deck.") == [
        "artifact.slides"
    ]
    assert infer_slide_outputs("请做一组会PPT：Transformer") == ["artifact.slides"]
    assert infer_slide_outputs("Make one editable single-slide scientific figure") == []
    assert remaining_deliverables(["artifact.slides"], []) == ["artifact.slides"]


@pytest.mark.asyncio
async def test_contract_hunting_after_find_skill_is_no_progress() -> None:
    contract = {
        "matches": [
            {
                "name": "research-pptx",
                "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}},
            }
        ]
    }

    async def invoker(name: str, args: dict) -> dict:  # noqa: ARG001
        if name == "find_skill":
            return contract
        return {"status": "ok", "matches": [{"doc": "commands.md"}]}

    llm = ScriptedLLM(
        [
            ChatWithToolsResult(tool_calls=[ToolCall("1", "find_skill", {"query": "research-pptx"})]),
            ChatWithToolsResult(tool_calls=[ToolCall("2", "docs_search", {"query": "pptx params"})]),
            ChatWithToolsResult(content="文字大纲"),
        ]
    )
    tools = [
        ToolSpec("find_skill", "lookup", {"type": "object", "properties": {"query": {"type": "string"}}}),
        ToolSpec("docs_search", "docs", {"type": "object", "properties": {"query": {"type": "string"}}}),
        ToolSpec("run_skill", "run", {"type": "object", "properties": {"skill_name": {"type": "string"}}}),
    ]
    result = await ReActLoopAgent(llm, invoker, max_iterations=8, no_progress_threshold=2).run(
        system_prompt="s",
        user_message="请做一组会PPT",
        tools=tools,
    )
    assert "run_skill" not in [record.name for record in result.tool_trace]
    assert "no_progress" in result.terminated_reason


@pytest.mark.asyncio
async def test_find_skill_then_run_skill_is_not_a_hunt() -> None:
    async def invoker(name: str, args: dict) -> dict:
        if name == "find_skill":
            return {
                "matches": [
                    {
                        "name": "research-pptx",
                        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}},
                    }
                ]
            }
        return {"status": "succeeded", "skill_name": args.get("skill_name")}

    llm = ScriptedLLM(
        [
            ChatWithToolsResult(tool_calls=[ToolCall("1", "find_skill", {"query": "research-pptx"})]),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "2",
                        "run_skill",
                        {"skill_name": "research-pptx", "input": {"topic": "Transformer"}},
                    )
                ]
            ),
            ChatWithToolsResult(content="Deck submitted."),
        ]
    )
    tools = [
        ToolSpec("find_skill", "lookup", {"type": "object", "properties": {"query": {"type": "string"}}}),
        ToolSpec("run_skill", "run", {"type": "object", "properties": {"skill_name": {"type": "string"}}}),
    ]
    result = await ReActLoopAgent(llm, invoker, max_iterations=6, no_progress_threshold=2).run(
        system_prompt="s",
        user_message="请做一组会PPT",
        tools=tools,
    )
    assert [record.name for record in result.tool_trace] == ["find_skill", "run_skill"]
    assert result.terminated_reason == "done"
    assert result.content == "Deck submitted."


def _skill_card(*names: str) -> dict:
    return {
        "matches": [
            {
                "name": name,
                "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}},
            }
            for name in names
        ]
    }


def _find_record(*names: str, query: str = "") -> ToolInvocationRecord:
    return ToolInvocationRecord(
        name="find_skill",
        arguments={"query": query or (names[0] if names else "")},
        result=_skill_card(*names),
    )


def _docs_record(query: str = "params") -> ToolInvocationRecord:
    return ToolInvocationRecord(
        name="docs_search",
        arguments={"query": query},
        result={"status": "ok", "matches": [{"doc": "commands.md"}]},
    )


def test_two_disjoint_find_skill_cards_are_not_a_hunt() -> None:
    assert (
        _contract_hunt_pressure(
            [_find_record("livefigure"), _find_record("research-pptx")]
        )
        == 1
    )


def test_same_contract_then_docs_is_a_hunt() -> None:
    assert (
        _contract_hunt_pressure([_find_record("research-pptx"), _docs_record()]) == 2
    )


def test_same_skill_via_different_query_is_a_hunt() -> None:
    assert (
        _contract_hunt_pressure(
            [
                _find_record("livefigure", query="livefigure"),
                _find_record("livefigure", "scientific-figure", query="architecture"),
            ]
        )
        == 2
    )


def test_docs_after_a_second_disjoint_card_hunts_the_newer_contract() -> None:
    assert (
        _contract_hunt_pressure(
            [
                _find_record("livefigure"),
                _find_record("research-pptx"),
                _docs_record("pptx params"),
            ]
        )
        == 2
    )


def test_a_successful_consume_clears_hunt_pressure() -> None:
    assert (
        _contract_hunt_pressure(
            [
                _find_record("research-pptx"),
                _find_record("research-pptx"),
                ToolInvocationRecord(
                    name="run_skill",
                    arguments={"skill_name": "research-pptx"},
                    result={"status": "succeeded"},
                ),
            ]
        )
        == 0
    )


@pytest.mark.asyncio
async def test_two_disjoint_contracts_can_both_run_skill() -> None:
    async def invoker(name: str, args: dict) -> dict:
        if name == "find_skill":
            query = str(args.get("query") or "")
            if "pptx" in query or "slides" in query:
                return _skill_card("research-pptx")
            return _skill_card("livefigure")
        return {"status": "succeeded", "skill_name": args.get("skill_name")}

    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("1", "find_skill", {"query": "livefigure"})]
            ),
            ChatWithToolsResult(
                tool_calls=[ToolCall("2", "find_skill", {"query": "research-pptx"})]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "3",
                        "run_skill",
                        {"skill_name": "livefigure", "input": {"title": "loop"}},
                    )
                ]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "4",
                        "run_skill",
                        {"skill_name": "research-pptx", "input": {"topic": "loop"}},
                    )
                ]
            ),
            ChatWithToolsResult(content="Figure and deck submitted."),
        ]
    )
    tools = [
        ToolSpec(
            "find_skill",
            "lookup",
            {"type": "object", "properties": {"query": {"type": "string"}}},
        ),
        ToolSpec(
            "run_skill",
            "run",
            {"type": "object", "properties": {"skill_name": {"type": "string"}}},
        ),
    ]
    result = await ReActLoopAgent(
        llm, invoker, max_iterations=8, no_progress_threshold=2
    ).run(
        system_prompt="s",
        user_message="draw the loop and make slides",
        tools=tools,
    )
    assert [record.name for record in result.tool_trace] == [
        "find_skill",
        "find_skill",
        "run_skill",
        "run_skill",
    ]
    assert result.terminated_reason == "done"
    assert result.content == "Figure and deck submitted."


@pytest.mark.asyncio
async def test_repeat_find_skill_of_the_same_contract_is_no_progress() -> None:
    async def invoker(name: str, args: dict) -> dict:  # noqa: ARG001
        return _skill_card("research-pptx")

    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("1", "find_skill", {"query": "research-pptx"})]
            ),
            ChatWithToolsResult(
                tool_calls=[ToolCall("2", "find_skill", {"query": "pptx parameters"})]
            ),
            ChatWithToolsResult(content="文字大纲"),
        ]
    )
    tools = [
        ToolSpec(
            "find_skill",
            "lookup",
            {"type": "object", "properties": {"query": {"type": "string"}}},
        ),
        ToolSpec(
            "run_skill",
            "run",
            {"type": "object", "properties": {"skill_name": {"type": "string"}}},
        ),
    ]
    result = await ReActLoopAgent(
        llm, invoker, max_iterations=8, no_progress_threshold=2
    ).run(
        system_prompt="s",
        user_message="请做一组会PPT",
        tools=tools,
    )
    assert "run_skill" not in [record.name for record in result.tool_trace]
    assert "no_progress" in result.terminated_reason
