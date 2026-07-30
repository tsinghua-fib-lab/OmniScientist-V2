"""Exact provider-quality obligations for non-workflow selected skills."""

from __future__ import annotations

import copy
import json
import sys

import pytest

from omni.agent.intent_plan import (
    IntentPlan,
    IntentType,
    SkillSelection,
)
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.plan_revision import (
    canonical_plan_hash,
    create_execution_authority,
    provider_authority_for_consumer,
)
from omni.agent.plan_validator import PlanValidator
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.runtime.subtask_runtime import SubtaskRuntime
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.manifest import (
    DeliveryMode,
    ExecSpec,
    SkillEntry,
    SkillKind,
)
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database


def _registry() -> SkillRegistry:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return registry


def _quality_obligation(plan: IntentPlan) -> dict:
    exact = [
        item
        for item in plan.task_contract["deliverables"]
        if item.get("consumer_step_id") == "selected_skill:0"
    ]
    assert len(exact) == 1
    return exact[0]


def _assert_exact_quality_binding(
    plan: IntentPlan,
    *,
    deliverable_id: str,
    check: str,
) -> None:
    binding = plan.provider_bindings[0]
    obligation = _quality_obligation(plan)

    assert plan.task_contract["schema_version"] == 2
    assert plan.task_contract["autonomy"] == "selected_skill_execution"
    assert obligation["id"] == deliverable_id
    assert obligation["provider_binding_id"] == binding["provider_binding_id"]
    assert obligation["provider_contract_hash"] == binding["contract_hash"]
    assert obligation["provider_name"] == binding["provider_name"]
    assert obligation["provider_source"] == binding["provider_source"]
    assert obligation["required_checks"] == [check]
    assert plan.verification_plan.deliverable_checks == [check]


def test_single_capability_scientific_figure_gets_exact_quality_obligation() -> None:
    registry = _registry()
    plan = IntentPlanner(registry).plan_from_proposal(
        "Draw a RAG architecture.",
        ModelPlanProposal(
            intent_type="single_skill_task",
            required_capabilities=["artifact.figure"],
            outputs=["artifact"],
            confidence=0.94,
            rationale="one figure provider",
        ),
        task_id="single-figure",
    )

    validation = PlanValidator(registry).validate(plan)

    assert validation.ok
    assert plan.intent_type == IntentType.SINGLE_SKILL_TASK
    assert plan.selected_skills[0].skill == "scientific-figure"
    _assert_exact_quality_binding(
        plan,
        deliverable_id="artifact.figure",
        check="figure_matches_instruction",
    )


def test_explicit_paper_review_gets_exact_quality_obligation() -> None:
    registry = _registry()
    plan = IntentPlanner(registry).plan(
        "$paper-review Review this paper for submission.",
        task_id="explicit-review",
    )

    validation = PlanValidator(registry).validate(plan)

    assert validation.ok
    assert plan.intent_type == IntentType.SINGLE_SKILL_TASK
    assert plan.selected_skills[0].selection_source == "explicit"
    _assert_exact_quality_binding(
        plan,
        deliverable_id="review",
        check="review_complete_and_evidence_grounded",
    )


def test_qa_plus_artifact_gets_figure_provider_quality_obligation() -> None:
    registry = _registry()
    plan = IntentPlanner(registry).plan_from_proposal(
        "Explain RAG and draw its architecture.",
        ModelPlanProposal(
            intent_type="qa_plus_artifact",
            required_capabilities=["qa.grounded", "artifact.figure"],
            outputs=["answer", "artifact"],
            confidence=0.93,
            rationale="answer plus figure",
        ),
        task_id="qa-figure",
    )

    validation = PlanValidator(registry).validate(plan)

    assert validation.ok
    assert plan.intent_type == IntentType.QA_PLUS_ARTIFACT
    assert {
        item["id"]
        for item in plan.task_contract["deliverables"]
    }.issuperset({"answer", "artifact", "artifact.figure"})
    _assert_exact_quality_binding(
        plan,
        deliverable_id="artifact.figure",
        check="figure_matches_instruction",
    )


def test_accepted_v1_selected_skill_plan_keeps_its_content_hash() -> None:
    registry = _registry()
    source = IntentPlanner(registry).plan(
        "$paper-review Review this paper.",
        task_id="legacy-review",
    )
    source.inputs_compiled = True
    payload = source.to_dict()
    payload.pop("plan_schema_version")
    payload.pop("provider_bindings")
    payload.pop("resolver_evidence")
    legacy = IntentPlan.from_dict(payload)
    legacy.revision_hash = canonical_plan_hash(legacy)
    accepted_payload = legacy.to_dict()
    accepted_hash = canonical_plan_hash(legacy)

    validation = PlanValidator(registry).validate(legacy)

    assert validation.ok
    assert legacy.to_dict() == accepted_payload
    assert canonical_plan_hash(legacy) == accepted_hash
    assert "provider_bindings" not in accepted_payload
    assert accepted_payload["task_contract"] == {}


def _spoofing_quality_skill() -> SkillEntry:
    assessment = {
        "schema": "omni.deliverable-assessment/v1",
        "deliverable_id": "spoofed-deliverable",
        "provider_binding_id": "spoofed-binding",
        "provider": "spoofed-provider",
        "contract_hash": "spoofed-contract",
        "step_id": "spoofed-step",
        "feedback": "provider judged its output",
        "status": "passed",
        "retryable": False,
        "effective_inputs": {"input": "go"},
        "criteria": [
            {
                "criterion_id": "fixture_quality",
                "status": "passed",
                "summary": "provider checked the fixture",
            }
        ],
    }
    script = (
        "import json;"
        "print(json.dumps("
        + repr(
            {
                "status": "ok",
                "summary": "fixture complete",
                "deliverable_assessment": assessment,
            }
        )
        + "))"
    )
    return SkillEntry(
        name="spoofing-quality-provider",
        description="quality identity boundary fixture",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role="task",
        capabilities=["artifact.fixture"],
        deliverables=["artifact.fixture"],
        quality_contract={
            "checks": ["fixture_quality"],
            "assessment_required": True,
            "assessment_schema": "omni.deliverable-assessment/v1",
        },
        input_schema={
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "summary": {"type": "string"},
                "deliverable_assessment": {"type": "object"},
            },
            "required": ["status", "deliverable_assessment"],
        },
        exec_spec=ExecSpec(
            command=sys.executable,
            args=["-c", script],
            stdout_format="json",
        ),
    )


@pytest.mark.asyncio
async def test_subtask_boundary_overwrites_spoofed_assessment_identity() -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    registry = SkillRegistry(settings, sources=())
    entry = _spoofing_quality_skill()
    registry.register(entry)
    plan = IntentPlan(
        task_id="identity-boundary",
        user_message="run the quality fixture",
        intent_type=IntentType.SINGLE_SKILL_TASK,
        outputs=["artifact"],
        selected_skills=[
            SkillSelection(
                skill=entry.name,
                reason="explicit fixture",
                matched_capabilities=["artifact.fixture"],
                selection_source="explicit",
                contract_level="full",
            )
        ],
        provider_inputs={entry.name: {"input": "go"}},
        inputs_compiled=True,
    )
    validation = PlanValidator(registry).validate(plan)
    assert validation.ok
    authority = create_execution_authority(plan, registry=registry)
    provider_authority = provider_authority_for_consumer(
        authority,
        consumer_kind="selected_skill",
        consumer_id="0",
    )

    def ctx_factory(session_id: str, channel: str) -> ExecContext:
        return ExecContext(
            settings=settings,
            paths=settings.paths,
            session_id=session_id,
            channel=channel,
        )

    runtime = SubtaskRuntime(db, settings, registry, ctx_factory)
    execution_id = await runtime.enqueue(
        entry.name,
        {"input": "go"},
        "",
        provider_authority=provider_authority,
    )
    await runtime.process(execution_id)

    execution = await runtime.get_subtask(execution_id)
    assert execution is not None
    assert execution.status == "succeeded"
    assessment = execution.result_json["deliverable_assessment"]
    identity = provider_authority["assessment_identity"]
    assert {
        key: assessment[key]
        for key in (
            "deliverable_id",
            "provider_binding_id",
            "contract_hash",
            "step_id",
            "provider",
        )
    } == {
        "deliverable_id": identity["deliverable_id"],
        "provider_binding_id": identity["provider_binding_id"],
        "contract_hash": identity["provider_contract_hash"],
        "step_id": identity["id"],
        "provider": identity["provider_name"],
    }
    assert assessment["criteria"][0]["criterion_id"] == "fixture_quality"
    assert assessment["status"] == "passed"
    assert json.loads(json.dumps(execution.result_json)) == execution.result_json

    tampered_authority = copy.deepcopy(provider_authority)
    tampered_authority["assessment_identity"]["provider_binding_id"] = (
        "tampered-binding"
    )
    rejected_id = await runtime.enqueue(
        entry.name,
        {"input": "go"},
        "",
        provider_authority=tampered_authority,
    )
    await runtime.process(rejected_id)

    rejected = await runtime.get_subtask(rejected_id)
    assert rejected is not None
    assert rejected.status == "failed"
    assert "provider assessment identity does not match" in rejected.error
    assert rejected.result_json == {}
