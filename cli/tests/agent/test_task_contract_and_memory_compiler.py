from __future__ import annotations

import pytest

from omni.agent.intent_plan import ContextPolicy, IntentPlan, IntentType, ToolPolicy
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.plan_revision import canonical_plan_hash
from omni.agent.planner import IntentPlanner
from omni.agent.task_contract import (
    TASK_CONTRACT_SCHEMA_VERSION,
    bind_task_contract_providers,
    provider_quality_checks,
    task_contract_deliverables,
)
from omni.config import load_settings
from omni.memory.compiler import MemoryCompiler
from omni.memory.service import MemoryLayer, MemoryService
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database
from tests.conftest import ScriptedLLM


def _contracted_skill(name: str, capability: str) -> SkillEntry:
    return SkillEntry(
        name=name,
        description=f"core skill for {capability}",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command="python", args=["-c", "print('{}')"], stdout_format="json"),
        source="builtin",
        capabilities=[capability],
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        output_schema={"type": "object", "properties": {"status": {"type": "string"}}},
    )


def test_workflow_model_proposal_materializes_task_contract_and_dag():
    settings = load_settings()
    registry = SkillRegistry(settings)
    registry.register(_contracted_skill("literature-search", "literature.search"))
    registry.register(_contracted_skill("lit-qa", "qa.grounded"))
    registry.register(_contracted_skill("scientific-figure", "artifact.figure"))

    proposal = ModelPlanProposal(
        intent_type="workflow",
        confidence=0.91,
        required_capabilities=["literature.search", "qa.grounded", "artifact.figure"],
        workflow_steps=[
            {"id": "lit", "capability": "literature.search", "input": {"input": "RAG hallucination"}, "reason": "find sources"},
            {"id": "qa", "capability": "qa.grounded", "depends_on": ["lit"], "input": {"input": "answer with evidence"}},
            {"id": "fig", "capability": "artifact.figure", "depends_on": ["qa"], "input": {"input": "draw architecture"}},
        ],
        outputs=["answer", "artifact"],
        rationale="longer research workflow",
    )

    plan = IntentPlanner(registry).plan_from_proposal(
        "RAG 如何降低幻觉，并生成一份工程架构图",
        proposal,
        task_id="run-contract",
    )

    assert plan.intent_type == IntentType.WORKFLOW
    assert plan.task_contract["schema_version"] == TASK_CONTRACT_SCHEMA_VERSION
    assert plan.task_contract["objective"] == "RAG 如何降低幻觉，并生成一份工程架构图"
    assert plan.task_contract["deliverables"] == [
        {
            "id": "answer",
            "kind": "answer",
            "required": True,
            "acceptance": ["answer_or_partial_answer_present"],
        },
        {
            "id": "artifact",
            "kind": "artifact",
            "required": True,
            "acceptance": [
                "artifact_uri_present",
                "artifact_generation_trace_present",
            ],
        },
    ]
    assert plan.workflow_dag["is_dag"] is True
    assert [node["id"] for node in plan.workflow_dag["nodes"]] == ["lit", "qa", "fig"]
    assert plan.workflow_dag["edges"] == [
        {"from": "lit", "to": "qa"},
        {"from": "qa", "to": "fig"},
    ]


def test_non_workflow_plan_does_not_carry_heavy_contract_or_dag():
    settings = load_settings()
    registry = SkillRegistry(settings)
    # A non-gate product question now routes to the capable assistant (react),
    # which must stay lightweight — no task_contract / workflow_dag baggage.
    plan = IntentPlanner(registry).plan("你的存储架构是如何实现的", task_id="run-direct")

    assert plan.intent_type == IntentType.REACT_FALLBACK
    assert plan.task_contract == {}
    assert plan.workflow_dag == {}


def test_task_contract_deliverables_accept_legacy_string_lists_without_mutation():
    legacy = {
        "objective": "legacy persisted task",
        "deliverables": ["answer", "artifact"],
    }

    assert task_contract_deliverables(legacy) == [
        {
            "id": "answer",
            "kind": "answer",
            "required": True,
            "acceptance": ["answer_or_partial_answer_present"],
        },
        {
            "id": "artifact",
            "kind": "artifact",
            "required": True,
            "acceptance": [
                "artifact_uri_present",
                "artifact_generation_trace_present",
            ],
        },
    ]
    assert legacy == {
        "objective": "legacy persisted task",
        "deliverables": ["answer", "artifact"],
    }


def test_legacy_task_contract_round_trip_preserves_its_revision_hash():
    plan = IntentPlan(
        task_id="legacy-contract",
        user_message="legacy persisted task",
        intent_type=IntentType.WORKFLOW,
        task_contract={
            "objective": "legacy persisted task",
            "deliverables": ["answer", "artifact"],
        },
    )
    original_hash = canonical_plan_hash(plan)

    restored = IntentPlan.from_dict(plan.to_dict())

    assert restored.task_contract == plan.task_contract
    assert canonical_plan_hash(restored) == original_hash
    assert [item["kind"] for item in task_contract_deliverables(restored.task_contract)] == [
        "answer",
        "artifact",
    ]


def test_provider_binding_preserves_duplicate_deliverable_consumer_obligations():
    contract = {
        "schema_version": 2,
        "objective": "render two variants",
        "deliverables": [
            {
                "id": "shared-figure",
                "kind": "artifact.figure",
                "required": True,
                "acceptance": [],
            }
        ],
    }
    steps = [
        {
            "id": "figure-a",
            "deliverable_id": "shared-figure",
            "capability": "artifact.figure",
            "provider_name": "provider-a",
            "provider_source": "builtin",
            "provider_binding_id": "binding-a",
            "provider_contract_hash": "hash-a",
            "quality_contract": {
                "assessment_required": True,
                "checks": ["figure_quality"],
            },
        },
        {
            "id": "figure-b",
            "deliverable_id": "shared-figure",
            "capability": "artifact.figure",
            "provider_name": "provider-b",
            "provider_source": "project",
            "provider_binding_id": "binding-b",
            "provider_contract_hash": "hash-b",
            "quality_contract": {
                "assessment_required": True,
                "checks": ["figure_quality"],
            },
        },
    ]

    bound = bind_task_contract_providers(contract, steps)
    rebound = bind_task_contract_providers(bound, steps)
    obligations = [
        item
        for item in bound["deliverables"]
        if item.get("required_checks") == ["figure_quality"]
    ]

    assert [
        (
            item["id"],
            item["consumer_step_id"],
            item["provider_binding_id"],
            item["provider_contract_hash"],
        )
        for item in obligations
    ] == [
        ("shared-figure", "figure-a", "binding-a", "hash-a"),
        ("shared-figure", "figure-b", "binding-b", "hash-b"),
    ]
    assert rebound == bound


def test_provider_quality_checks_rejects_non_array_metadata() -> None:
    steps = [
        {
            "id": "figure",
            "required": True,
            "quality_contract": {
                "assessment_required": True,
                "checks": "figure_quality",
            },
        }
    ]

    assert provider_quality_checks(steps) == []


@pytest.mark.asyncio
async def test_memory_compiler_filters_by_plan_and_budget():
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    memory = MemoryService(db, settings, llm=ScriptedLLM())
    await memory.record(layer=MemoryLayer.SESSION, scope="session", scope_id="s1", summary="current session RAG preference")
    await memory.record(layer=MemoryLayer.EPISODIC, scope="session", scope_id="old", summary="old RAG experiment trace")
    await memory.record(layer=MemoryLayer.SEMANTIC, scope="project", summary="RAG reduces hallucination through retrieval grounding")
    await memory.record(layer=MemoryLayer.ARTIFACT, scope="task", scope_id="t1", summary="RAG architecture artifact with reranker")

    plan = IntentPlan(
        task_id="run-memory",
        user_message="RAG 如何降低幻觉",
        intent_type=IntentType.WORKFLOW,
        outputs=["answer"],
        context_policy=ContextPolicy(include_memory=True, include_research_brief=True, include_referenced_tasks=True),
        tool_policy=ToolPolicy(max_tool_calls=0),
    )

    compiled = await MemoryCompiler(memory).compile_for_turn(
        plan,
        query="RAG hallucination",
        session_id="s1",
        token_budget=80,
    )

    assert compiled.selected_memory_ids
    assert "Compiled memory" in compiled.text
    assert "role=assistant" in compiled.text
    assert "RAG" in compiled.text
    assert len(compiled.text) <= 420

    plan.context_policy.include_memory = False
    empty = await MemoryCompiler(memory).compile_for_turn(plan, query="RAG", session_id="s1")
    assert empty.text == ""
    assert empty.selected_memory_ids == []
