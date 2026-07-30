"""Offline acceptance corpus for objective gates and provider-owned quality."""

from __future__ import annotations

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.plan_validator import PlanValidator
from omni.agent.skill_arbitrator import SkillArbitrator
from omni.agent.workflow_plan_builder import WorkflowPlanBuilder
from omni.config import load_settings
from omni.skills_runtime.manifest import SkillEntry
from omni.skills_runtime.registry import SkillRegistry

_RETIRED_FINDINGS = {
    "constraint_target_unverified",
    "semantic_binding_mismatch",
    "unconsumed_constraint",
}


def _registry() -> SkillRegistry:
    registry = SkillRegistry(load_settings(), sources=())
    registry.register(
        SkillEntry(
            name="figure-provider",
            description="offline figure fixture",
            source="builtin",
            trusted=True,
            role="task",
            capabilities=["artifact.figure"],
            quality_contract={
                "checks": ["figure_matches_instruction"],
                "assessment_required": True,
                "assessment_schema": "omni.deliverable-assessment/v1",
            },
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "input": {
                        "type": "string",
                        "x-omni": {"semantic_role": "instruction"},
                    },
                    "figure_kind": {
                        "type": "string",
                        "enum": ["generic", "rag", "transformer"],
                    },
                },
                "required": ["input"],
            },
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        )
    )
    return registry


def _figure_plan(input_data: dict[str, object]) -> IntentPlan:
    return IntentPlan(
        task_id="offline-objective-case",
        user_message=(
            "Draw a RAG system with query, retriever, reranker, and LLM."
        ),
        intent_type=IntentType.WORKFLOW,
        outputs=["artifact"],
        workflow_steps=[
            {
                "id": "figure",
                "skill_name": "figure-provider",
                "skill_source": "builtin",
                "capability": "artifact.figure",
                "input": input_data,
            }
        ],
    )


@pytest.mark.parametrize(
    ("input_data", "expected_code"),
    [
        (
            {"input": "query retriever reranker LLM", "figure_kind": "rag"},
            "",
        ),
        (
            {
                "input": "query retriever reranker LLM",
                "figure_kind": "unknown",
            },
            "provider_schema_invalid",
        ),
        (
            {
                "input": "query retriever reranker LLM",
                "figuer_kind": "rag",
            },
            "provider_schema_invalid",
        ),
    ],
)
def test_objective_provider_schema_cases(
    input_data: dict[str, object],
    expected_code: str,
) -> None:
    validation = PlanValidator(_registry()).validate(_figure_plan(input_data))
    codes = {finding.code for finding in validation.findings}

    if expected_code:
        assert expected_code in codes
        assert not validation.ok
    else:
        assert validation.ok
    assert not _RETIRED_FINDINGS.intersection(codes)


def test_semantic_hint_is_not_a_host_execution_blocker() -> None:
    validation = PlanValidator(_registry()).validate(
        _figure_plan(
            {
                "input": "query retriever reranker vector store and LLM",
                # The provider will resolve its effective kind at execution.
                "figure_kind": "generic",
            }
        )
    )

    assert validation.ok
    assert not _RETIRED_FINDINGS.intersection(
        finding.code for finding in validation.findings
    )


def test_complex_research_plan_stays_typed_and_quality_bound_offline() -> None:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    builder = WorkflowPlanBuilder(SkillArbitrator(registry))
    plan = builder.from_specs(
        (
            "Fetch arXiv:1706.03762, draw a RAG architecture with query, "
            "retriever, reranker and LLM, then write a paper draft."
        ),
        [
            {
                "id": "paper",
                "capability": "paper.fetch.arxiv",
                "input": {"identifier": "1706.03762"},
            },
            {
                "id": "figure",
                "capability": "artifact.figure",
                "depends_on": ["paper"],
                "input": {
                    "input": "RAG: query, retriever, reranker, LLM",
                    "figure_kind": "generic",
                },
            },
            {
                "id": "draft",
                "capability": "synthesis.final",
                "depends_on": ["paper", "figure"],
            },
        ],
        task_id="offline-complex-task",
        rationale="offline acceptance corpus",
        confidence=0.9,
        provenance_mode="full",
    )

    validation = PlanValidator(registry).validate(plan)
    codes = {finding.code for finding in validation.findings}
    assert plan.intent_type is IntentType.WORKFLOW
    assert validation.ok
    assert [step["id"] for step in plan.workflow_steps] == [
        "paper",
        "figure",
        "draft",
    ]
    assert {
        "figure_matches_instruction",
        "draft_content_present",
    }.issubset(plan.verification_plan.deliverable_checks)
    assert all(
        step.get("provider_binding_id")
        for step in plan.workflow_steps
    )
    assert not _RETIRED_FINDINGS.intersection(codes)
