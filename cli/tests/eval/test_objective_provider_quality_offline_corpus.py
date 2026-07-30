"""Offline acceptance corpus for objective gates and exact provider binding."""

from __future__ import annotations

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.plan_validator import PlanValidator
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
    ("input_data", "expected_code", "executable"),
    [
        (
            {"input": "query retriever reranker LLM", "figure_kind": "rag"},
            "",
            True,
        ),
        # A declared field carrying a value the provider does not accept. Only
        # the model can choose a legal one, so the plan waits until it does.
        (
            {
                "input": "query retriever reranker LLM",
                "figure_kind": "unknown",
            },
            "provider_schema_invalid",
            False,
        ),
        # A field the provider never declared. The compiler has already dropped
        # it by the time validation runs, so there is no remedy left to apply
        # and nothing for a stop to achieve — the run continues and reports what
        # it discarded. Blocking here is what once handed a researcher a schema
        # diagnostic phrased as a question they could not answer.
        (
            {
                "input": "query retriever reranker LLM",
                "figuer_kind": "rag",
            },
            "provider_schema_invalid",
            True,
        ),
    ],
)
def test_objective_provider_schema_cases(
    input_data: dict[str, object],
    expected_code: str,
    executable: bool,
) -> None:
    validation = PlanValidator(_registry()).validate(_figure_plan(input_data))
    codes = {finding.code for finding in validation.findings}

    if expected_code:
        assert expected_code in codes
    assert validation.ok is executable
    # Executable is not the same as clean: a violation that still runs has to
    # remain visible as a violation rather than pass for a healthy plan.
    if expected_code and executable:
        assert validation.status == "degraded"
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


def test_complex_research_steps_stay_provider_bound_offline() -> None:
    """Every step of a real multi-provider plan is sealed to an exact provider.

    The model now names the provider for each step instead of the planner
    resolving it from a capability, so this corpus case starts from a
    model-shaped step list. Each of those names must still be resolved offline
    to one concrete provider and frozen — including the native synthesis step,
    which names no skill at all. An unsealed step is one a later run could
    satisfy with a different provider than the plan was validated against.
    """
    registry = SkillRegistry(load_settings())
    registry.build_index()
    plan = IntentPlan(
        task_id="offline-complex-task",
        user_message=(
            "Fetch arXiv:1706.03762, draw a RAG architecture with query, "
            "retriever, reranker and LLM, then write a paper draft."
        ),
        intent_type=IntentType.WORKFLOW,
        outputs=["artifact", "draft.section"],
        workflow_steps=[
            {
                "id": "paper",
                "skill_name": "arxiv-fetch",
                "capability": "paper.fetch.arxiv",
                "input": {"identifier": "1706.03762"},
            },
            {
                "id": "figure",
                "skill_name": "scientific-figure",
                "capability": "artifact.figure",
                "depends_on": ["paper"],
                "input": {
                    "input": "RAG: query, retriever, reranker, LLM",
                    "figure_kind": "generic",
                },
            },
            {
                "id": "draft",
                "skill_name": "",
                "provider_type": "native_executor",
                "capability": "synthesis.final",
                "depends_on": ["paper", "figure"],
                "input": {"deliverable": "draft.section"},
            },
        ],
    )

    validation = PlanValidator(registry).validate(plan)
    codes = {finding.code for finding in validation.findings}
    assert validation.ok
    assert [
        (
            step["provider_name"],
            step["provider_source"],
            bool(step["provider_contract_hash"]),
        )
        for step in plan.workflow_steps
    ] == [
        ("arxiv-fetch", "builtin", True),
        ("scientific-figure", "builtin", True),
        ("synthesis.final", "native", True),
    ]
    assert [
        binding["provider_binding_id"] for binding in plan.provider_bindings
    ] == [step["provider_binding_id"] for step in plan.workflow_steps]
    assert not _RETIRED_FINDINGS.intersection(codes)
