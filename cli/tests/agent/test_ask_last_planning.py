"""Ask-last planning: a value the agent can discover is never turned into a question.

Regression guard for run dfcb92bb. In one planning pass the model bound the real
identifier into the step *and* listed a stale ``missing_inputs`` gap for it. A
pre-existing veto (``missing_inputs`` non-empty => ask) then discarded that
fully-bound, executable plan and asked the user for an id it already had.

These tests pin the repaired contract across the four quadrants:

    bound plan            -> verify resolver fact, execute, never ask (incident replay)
    resolvable-id gap     -> ReAct floor look-up (search -> id -> fetch), never ask
    un-groundable gap     -> recovery Rung 3 asks (repair failed -> ask)
    explicit needs_input  -> ask (the model chose it)

plus the deterministic units the routing rests on: placeholder cleaning and
``missing_inputs`` reconciliation. Planning is single-pass (Codex-aligned): the
model binds step inputs itself and reconciliation is a pure, no-LLM check that
drops stale gaps once the plan proves executable.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omni.agent.input_resolution import resolve_identifier_fields
from omni.agent.intent_plan import IntentType
from omni.agent.model_planner import ModelIntentPlanner, ModelPlanProposal
from omni.agent.plan_recovery import (
    ACTION_EXECUTE,
    ACTION_NEEDS_INPUT,
    ACTION_REACT,
    recover,
)
from omni.agent.plan_validator import PlanValidator
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.core.llm.client import LLMClient
from omni.skills_runtime.manifest import DeliveryMode, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry

_INCIDENT_GOAL = (
    "为 RAG 系统综述准备材料：获取 Attention Is All You Need 摘要，"
    "并生成包含 query、retriever、reranker、LLM 的科研架构图。并输出一篇论文"
)


class _ScriptedLLM(LLMClient):
    """Single-pass planner double: returns one scripted proposal, no binder call."""

    def __init__(self, plan: dict) -> None:
        self.model = "scripted"
        self._plan = plan

    async def chat(self, system: str, user: str, **kwargs: Any) -> str:
        assert "semantic intent planner" in system.lower()
        return json.dumps(self._plan, ensure_ascii=False)

    async def chat_with_tools(self, messages, tools, **kwargs: Any):  # noqa: ANN001, ANN201 # pragma: no cover
        raise AssertionError("scripted planning uses chat only")

    async def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        return [[0.0] for _ in texts]


def _registry() -> SkillRegistry:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return registry


def _custom_skill(
    name: str, capability: str, *, required: list[str], fmt: str | None = None, role: str = "task"
) -> SkillEntry:
    props: dict[str, dict] = {}
    for idx, key in enumerate(required):
        props[key] = {"type": "string", "format": fmt} if (fmt and idx == 0) else {"type": "string"}
    return SkillEntry(
        name=name,
        description=f"{name} handles {capability}",
        source="builtin",
        kind=SkillKind.PYTHON_ENGINE,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role=role,
        capabilities=[capability],
        priority=50,
        input_schema={"type": "object", "properties": props, "required": list(required)},
        output_schema={"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]},
    )


def _contradictory_incident_plan() -> dict:
    """Run dfcb92bb shape: the model binds the real id in the step but still
    lists a stale ``missing_inputs`` gap for the same field (single-pass)."""
    return {
        "intent_type": "workflow",
        "confidence": 0.9,
        "workflow_steps": [
            {"id": "paper", "capability": "paper.fetch.arxiv", "input": {"identifier": "1706.03762"}},
            {
                "id": "figure",
                "capability": "artifact.figure",
                "depends_on": ["paper"],
                "input": {"figure_kind": "rag"},
            },
            {
                "id": "writing",
                "capability": "synthesis.final",
                "depends_on": ["figure"],
                "input": {"deliverable": "draft.section"},
            },
        ],
        "outputs": ["artifact", "draft.section"],
        # Exact provider fields only. ``template`` was a retired host alias;
        # keeping it here would test an unrelated schema violation instead of
        # the stale-missing-input regression.
        "capability_inputs": {"artifact.figure": {"figure_kind": "rag", "title": "RAG 架构图"}},
        "missing_inputs": [{"field": "arxiv_id", "reason": "需要 arXiv ID（例如 1706.03762）"}],
        "execution_mode": "background",
        "provenance_mode": "light",
        "rationale": "multi-deliverable research prep",
    }


# ── Quadrant 1: a fully-bound plan executes and never asks ──


@pytest.mark.asyncio
async def test_incident_dfcb92bb_bound_plan_executes_and_never_asks():
    registry = _registry()

    proposal = await ModelIntentPlanner(_ScriptedLLM(_contradictory_incident_plan()), registry).propose(
        _INCIDENT_GOAL
    )

    assert proposal is not None
    # The concrete id is bound and the stale gap the model contradicted itself
    # with was reconciled away deterministically (with an audit trail).
    paper = next(step for step in proposal.workflow_steps if step["id"] == "paper")
    assert paper["input"]["identifier"] == "1706.03762"
    assert proposal.missing_inputs == []
    assert proposal.binding_audit["dropped_missing_inputs"][0]["field"] == "arxiv_id"

    # Ask-last routing builds the workflow, not a needs_input question. Because
    # the user named only the paper title, the model-supplied id still needs
    # resolver-owned grounding before execution. The normal in-lane resolver
    # verifies that exact value rather than trusting model knowledge.
    planner = IntentPlanner(registry)
    plan = planner.plan_from_proposal(_INCIDENT_GOAL, proposal, task_id="run-incident")
    assert plan.intent_type == IntentType.WORKFLOW

    async def _grounded_search(
        field_format: str,
        query: str,
    ) -> list[tuple[str, str]]:
        assert field_format == "arxiv_id"
        assert query == "Attention Is All You Need"
        return [("1706.03762", "Attention Is All You Need")]

    plan, validation, records = await resolve_identifier_fields(
        plan,
        PlanValidator(registry).validate(plan),
        registry=registry,
        searcher=_grounded_search,
    )
    assert [record.via for record in records] == ["arxiv_id.verify"]
    outcome = recover(plan, validation, registry)
    assert outcome.action == ACTION_EXECUTE
    fetch = next(s for s in outcome.plan.workflow_steps if s.get("capability") == "paper.fetch.arxiv")
    assert fetch["input"]["identifier"] == "1706.03762"
    figure = next(s for s in outcome.plan.workflow_steps if s.get("capability") == "artifact.figure")
    assert figure["input"].get("figure_kind") == "rag"


# ── Quadrant 2: a resolvable-id gap looks it up on the floor, never asks ──


@pytest.mark.asyncio
async def test_resolvable_id_gap_hands_off_to_react_lookup_not_a_question():
    plan = {
        "intent_type": "workflow",
        "confidence": 0.85,
        "workflow_steps": [
            {"id": "paper", "capability": "paper.fetch.arxiv", "input": {"identifier": "<user_provided>"}},
            {"id": "figure", "capability": "artifact.figure", "depends_on": ["paper"], "input": {}},
        ],
        "outputs": ["artifact"],
        "missing_inputs": [{"field": "identifier", "reason": "title-only paper needs a concrete arXiv id"}],
        "rationale": "title-only paper plus figure",
    }
    registry = _registry()
    goal = "获取 Attention Is All You Need 摘要，并生成 RAG 架构图。"

    proposal = await ModelIntentPlanner(_ScriptedLLM(plan), registry).propose(goal)

    assert proposal is not None
    # The placeholder id was stripped and nothing bound it, so the gap is real
    # and preserved for the recovery ladder.
    assert proposal.missing_inputs

    planner = IntentPlanner(registry)
    plan_obj = planner.plan_from_proposal(goal, proposal, task_id="run-groundable")
    # Ask-last: a workflow with steps is built, not short-circuited to a question.
    assert plan_obj.intent_type == IntentType.WORKFLOW

    # Look-up before ask/error: an arXiv id the in-lane resolver could not bind
    # (offline in this unit) goes to the ReAct floor to act-and-look-up — never a
    # lossy whole-sentence search and never a question to the user.
    outcome = recover(plan_obj, PlanValidator(registry).validate(plan_obj), registry)
    assert outcome.action == ACTION_REACT
    assert outcome.rung == "4_react_lookup"
    assert any("Attention Is All You Need" in note for note in outcome.notes)
    assert not any(s.get("capability") == "literature.search" for s in outcome.plan.workflow_steps)


# ── Quadrant 3: an un-groundable gap asks (repair failed → ask) ──


@pytest.mark.asyncio
async def test_ungroundable_gap_asks_after_repair_is_impossible():
    registry = _registry()
    # A required task whose only field is strict-typed, not a resolvable identifier
    # (a DOI we don't search), and has no producer to reroute to: the value
    # genuinely cannot be discovered, so ask.
    registry.register(_custom_skill("title-fetch", "custom.fetch", required=["identifier"], fmt="doi"))
    plan = {
        "intent_type": "workflow",
        "confidence": 0.8,
        "workflow_steps": [
            {"id": "only", "capability": "custom.fetch", "input": {"identifier": "Some Paper Title"}},
        ],
        "outputs": ["data"],
        "missing_inputs": [{"field": "identifier", "reason": "no concrete id"}],
        "rationale": "fetch a paper the user only named",
    }
    goal = "获取那篇论文的数据"

    proposal = await ModelIntentPlanner(_ScriptedLLM(plan), registry).propose(goal)

    assert proposal is not None
    planner = IntentPlanner(registry)
    plan_obj = planner.plan_from_proposal(goal, proposal, task_id="run-ungroundable")
    # Ask-last still builds first; the ask is the ladder's verdict, not a veto.
    assert plan_obj.intent_type == IntentType.WORKFLOW

    outcome = recover(plan_obj, PlanValidator(registry).validate(plan_obj), registry)
    assert outcome.action == ACTION_NEEDS_INPUT
    assert outcome.rung == "3_needs_input"
    assert any(item.get("field") == "identifier" for item in outcome.missing_inputs)


# ── Quadrant 4: an explicit needs_input intent is always a question ──


def test_explicit_needs_input_intent_is_always_a_question():
    registry = _registry()
    proposal = ModelPlanProposal(
        intent_type="needs_input",
        missing_inputs=[{"field": "research_topic", "reason": "no concrete topic was given"}],
        rationale="request is too vague to plan",
    )
    plan = IntentPlanner(registry).plan_from_proposal("帮我做点研究", proposal, task_id="run-explicit")

    assert plan.intent_type == IntentType.NEEDS_INPUT
    assert any(item.get("field") == "research_topic" for item in plan.missing_inputs)


def test_missing_inputs_without_any_steps_still_asks():
    """No workflow to execute ⇒ the gap is un-actionable ⇒ ask (ask-last floor)."""
    registry = _registry()
    proposal = ModelPlanProposal(
        intent_type="single_skill_task",
        missing_inputs=[{"field": "dataset", "reason": "no dataset was provided"}],
        rationale="single task with nothing to run",
    )
    plan = IntentPlanner(registry).plan_from_proposal("分析我的数据", proposal, task_id="run-nosteps")

    assert plan.intent_type == IntentType.NEEDS_INPUT
    assert any(item.get("field") == "dataset" for item in plan.missing_inputs)


# ── Deterministic units the routing rests on ──


def test_placeholder_values_are_stripped_from_steps_and_capability_inputs():
    payload = {
        "intent_type": "workflow",
        "workflow_steps": [
            {
                "id": "s1",
                "capability": "paper.fetch.arxiv",
                "input": {"identifier": "<user_provided>", "note": "{{fill me}}", "keep": "1706.03762"},
            },
        ],
        "capability_inputs": {"artifact.figure": {"title": "<title>", "template": "rag"}},
        "missing_inputs": [{"field": "identifier", "reason": "x"}],
    }
    proposal = ModelPlanProposal.from_payload(payload)

    # Both ``<...>`` and ``{{...}}`` placeholders are removed; real values kept.
    assert proposal.workflow_steps[0]["input"] == {"keep": "1706.03762"}
    assert proposal.capability_inputs["artifact.figure"] == {"template": "rag"}


@pytest.mark.asyncio
async def test_reconcile_drops_stale_gap_when_required_fields_are_bound():
    plan = {
        "intent_type": "workflow",
        "confidence": 0.9,
        "workflow_steps": [
            {"id": "paper", "capability": "paper.fetch.arxiv", "input": {"identifier": "1706.03762"}},
        ],
        "outputs": ["workflow"],
        "missing_inputs": [{"field": "arxiv_id", "reason": "stale gap the model listed anyway"}],
        "rationale": "single fetch, id already known",
    }
    proposal = await ModelIntentPlanner(_ScriptedLLM(plan), _registry()).propose("获取 arXiv 1706.03762 摘要")

    assert proposal is not None
    assert proposal.missing_inputs == []
    assert proposal.binding_audit["dropped_missing_inputs"][0]["field"] == "arxiv_id"


@pytest.mark.asyncio
async def test_reconcile_keeps_genuine_gap_when_required_field_unbound():
    plan = {
        "intent_type": "workflow",
        "confidence": 0.9,
        "workflow_steps": [
            {"id": "paper", "capability": "paper.fetch.arxiv", "input": {}},
        ],
        "outputs": ["workflow"],
        "missing_inputs": [{"field": "identifier", "reason": "genuine gap"}],
        "rationale": "single fetch, no id",
    }
    proposal = await ModelIntentPlanner(_ScriptedLLM(plan), _registry()).propose("获取 Attention Is All You Need 摘要")

    assert proposal is not None
    assert proposal.missing_inputs == [{"field": "identifier", "reason": "genuine gap"}]
    assert "dropped_missing_inputs" not in proposal.binding_audit
