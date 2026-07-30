"""Ask-last planning: a value the agent can discover is never turned into a question.

Regression guard for run dfcb92bb. In one planning pass the model bound the real
identifier into the step *and* listed a stale ``missing_inputs`` gap for it. A
pre-existing veto (``missing_inputs`` non-empty => ask) then discarded that
fully-bound, executable plan and asked the user for an id it already had.

These tests pin the repaired contract across the four quadrants:

    bound plan            -> execute, never ask (incident replay)
    resolvable-id gap     -> capable tool-enabled turn looks it up, never asks
    un-groundable gap     -> the runtime refuses to invent it and asks
    explicit needs_input  -> ask (the model chose it)

plus the deterministic units the routing rests on: placeholder cleaning and
``missing_inputs`` reconciliation. Planning is single-pass (Codex-aligned): the
model binds step inputs itself and reconciliation is a pure, no-LLM check that
drops stale gaps once the plan proves executable.

Quadrants 1-3 used to be settled while the planner compiled a DAG, so each was
asserted against an ``IntentType.WORKFLOW`` plan and the rung the recovery
ladder picked for it. The planner no longer compiles multi-step work; it hands
the turn to the capable assistant and the model sequences the steps by calling
``run_workflow``. The ask-last *decision* therefore moved but did not weaken:
the planner still refuses to short-circuit a gap it might be able to close, and
the ask now happens where the value is finally proven undiscoverable — at the
workflow gate, which refuses to persist an under-specified step.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from omni.agent import OmniAgent
from omni.agent.intent_plan import IntentType
from omni.agent.model_planner import ModelIntentPlanner, ModelPlanProposal
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, LLMClient, ToolCall
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry
from tests.conftest import PlanningLLM

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
    script = (
        "import json,sys;"
        "json.load(sys.stdin);"
        "print(json.dumps({'status':'ok','summary':'ran " + name + "'}))"
    )
    return SkillEntry(
        name=name,
        description=f"{name} handles {capability}",
        source="builtin",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role=role,
        capabilities=[capability],
        priority=500,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
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

    # Ask-last routing hands the turn to the capable assistant that can run the
    # work, not to a needs_input question. The reconciled gap is gone from the
    # plan too, so nothing downstream can resurrect it as an ask.
    plan = IntentPlanner(registry).plan_from_proposal(
        _INCIDENT_GOAL, proposal, task_id="run-incident"
    )
    assert plan.intent_type is not IntentType.NEEDS_INPUT
    assert plan.missing_inputs == []
    # The turn keeps the tools that execute the work; the model sequences it.
    assert "run_workflow" not in plan.tool_policy.blocked_tools

    # Planning carries no second copy of the model's provider inputs. The model
    # passes the id it already resolved in its own tool call, so there is nothing
    # here to go stale against it. (A valid-but-wrong id is caught at execution by
    # verify-by-fetch, not by a slow, low-precision plan-time title search — the
    # very gate that dead-ended good ids.)
    assert plan.capability_inputs == {}


# ── Quadrant 2: a resolvable-id gap is looked up, never asked ──


@pytest.mark.asyncio
async def test_resolvable_id_gap_becomes_a_lookup_turn_not_a_question():
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
    # The placeholder id was stripped and nothing bound it, so the gap is real.
    assert proposal.missing_inputs

    plan_obj = IntentPlanner(registry).plan_from_proposal(goal, proposal, task_id="run-groundable")

    # Ask-last: a gap the agent may still be able to close does *not*
    # short-circuit the turn into a question. The request goes to the capable,
    # tool-enabled turn that can search for the id and then fetch it.
    assert plan_obj.intent_type is not IntentType.NEEDS_INPUT
    assert plan_obj.missing_inputs == []
    blocked = set(plan_obj.tool_policy.blocked_tools)
    assert not {"search_literature", "run_skill", "run_workflow"} & blocked
    # And the host does not "helpfully" substitute a lossy whole-sentence search
    # for the fetch the user actually asked for.
    assert plan_obj.workflow_steps == []


# ── Quadrant 3: an un-groundable gap asks (execution refuses to invent it) ──


@pytest.mark.asyncio
async def test_ungroundable_gap_asks_instead_of_executing_on_a_guess():
    """The last gate before a task exists refuses an unbindable required input.

    The value here is a paper *title* in a strict-typed identifier field with no
    lookup adapter behind it: genuinely undiscoverable. The model still tries to
    run it, and the workflow gate declines to persist the run and tells the
    model to ask the user — the ask-last floor, now enforced where the guess
    would otherwise have become a task.
    """
    goal = "获取那篇论文的数据"
    agent = await OmniAgent.create(load_settings())
    try:
        agent.registry.register(
            _custom_skill("title-fetch", "custom.fetch", required=["identifier"], fmt="doi")
        )
        agent.llm = PlanningLLM(
            [
                {
                    "intent_type": "workflow",
                    "confidence": 0.8,
                    "outputs": ["data"],
                    "rationale": "fetch a paper the user only named",
                }
            ],
            script=[
                ChatWithToolsResult(
                    tool_calls=[
                        ToolCall(
                            id="call_workflow",
                            name="run_workflow",
                            arguments={
                                "goal": goal,
                                "mode": "background",
                                "steps": [
                                    {
                                        "id": "only",
                                        "skill": "title-fetch",
                                        "input": {"paper": "Some Paper Title"},
                                    }
                                ],
                            },
                        )
                    ]
                ),
                ChatWithToolsResult(content="Which paper do you mean? I need its DOI."),
            ],
        )
        result = await agent.handle_turn(goal, channel="cli", drain_tasks=False)

        # Nothing was created from the guess …
        assert not result.submitted_workflow_ids
        assert await agent.runtime.list_subtasks(limit=10) == []
        # … and the tool told the model to ask rather than inventing a value.
        submission = next(
            record for record in result.tool_trace if record.name == "run_workflow"
        )
        assert submission.result["status"] == "needs_input"
        assert "Ask the user" in submission.result["message"]
        assert submission.result["missing"][0]["skill_name"] == "title-fetch"
        assert submission.result["missing"][0]["missing"] == ["identifier"]
    finally:
        await agent.aclose()


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
