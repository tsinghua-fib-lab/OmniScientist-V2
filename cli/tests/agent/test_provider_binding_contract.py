"""Exact provider identity is shared by planning, facts, and verification."""

from __future__ import annotations

from omni.agent.intent_plan import IntentPlan, IntentType, VerificationPlan
from omni.agent.plan_pipeline import PlanPipeline
from omni.agent.plan_revision import canonical_plan_hash
from omni.agent.plan_validator import PlanValidator
from omni.agent.provider_binding import provider_contract_hash
from omni.agent.task_contract import build_task_contract
from omni.config import load_settings
from omni.runtime.workflow_plan import prepare_workflow_plan
from omni.skills_runtime.manifest import SkillEntry
from omni.skills_runtime.registry import SkillRegistry


def _entry(
    *,
    source: str,
    enum: list[str] | None = None,
) -> SkillEntry:
    return SkillEntry(
        name="same-name-provider",
        description="provider binding fixture",
        source=source,
        trusted=True,
        capabilities=["artifact.fixture"],
        quality_contract={
            "checks": ["fixture_quality"],
            "assessment_required": True,
            "assessment_schema": "omni.deliverable-assessment/v1",
        },
        input_schema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    **({"enum": list(enum)} if enum else {}),
                }
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
    )


def _plan(*, source: str) -> IntentPlan:
    steps = [
        {
            "id": "render",
            "skill_name": "same-name-provider",
            "skill_source": source,
            "capability": "artifact.fixture",
            "input": {"mode": "full"},
            "depends_on": [],
        }
    ]
    return IntentPlan(
        task_id="provider-binding-task",
        user_message="render the fixture",
        intent_type=IntentType.WORKFLOW,
        outputs=["artifact"],
        workflow_steps=steps,
        task_contract=build_task_contract(
            objective="render the fixture",
            deliverables=["artifact"],
            capabilities=["artifact.fixture"],
            workflow_steps=steps,
        ),
        verification_plan=VerificationPlan(
            required_outputs=["artifact"],
            required_events=["workflow.submitted"],
        ),
    )


def test_same_name_different_source_has_different_exact_binding() -> None:
    registry = SkillRegistry(load_settings(), sources=())
    builtin = _entry(source="builtin", enum=["full"])
    project = _entry(source="project_omni", enum=["full"])
    registry.register(builtin)
    registry.register(project)

    builtin_plan = _plan(source="builtin")
    project_plan = _plan(source="project_omni")
    assert PlanValidator(registry).validate(builtin_plan).ok
    assert PlanValidator(registry).validate(project_plan).ok

    left = builtin_plan.provider_bindings[0]
    right = project_plan.provider_bindings[0]
    assert left["provider_source"] == "builtin"
    assert right["provider_source"] == "project_omni"
    assert left["provider_binding_id"] != right["provider_binding_id"]
    assert left["contract_hash"] != right["contract_hash"]


def test_contract_change_changes_binding_and_is_resealed() -> None:
    registry = SkillRegistry(load_settings(), sources=())
    entry = _entry(source="builtin", enum=["full"])
    registry.register(entry)
    plan = _plan(source="builtin")
    assert PlanValidator(registry).validate(plan).ok
    first_binding = plan.provider_bindings[0]["provider_binding_id"]
    first_hash = plan.provider_bindings[0]["contract_hash"]

    entry.input_schema["properties"]["mode"]["enum"] = ["full", "brief"]
    plan.inputs_compiled = False
    assert PlanValidator(registry).validate(plan).ok

    assert plan.provider_bindings[0]["provider_binding_id"] != first_binding
    assert plan.provider_bindings[0]["contract_hash"] != first_hash
    assert plan.provider_bindings[0]["contract_hash"] == provider_contract_hash(
        entry
    )


def test_task_contract_quality_check_is_bound_to_exact_consumer() -> None:
    registry = SkillRegistry(load_settings(), sources=())
    registry.register(_entry(source="builtin", enum=["full"]))
    plan = _plan(source="builtin")

    validation = PlanValidator(registry).validate(plan)

    assert validation.ok
    step = plan.workflow_steps[0]
    requirement = next(
        item
        for item in plan.task_contract["deliverables"]
        if item["id"] == "render"
    )
    assert requirement["consumer_step_id"] == "render"
    assert requirement["provider_binding_id"] == step["provider_binding_id"]
    assert (
        requirement["provider_contract_hash"]
        == step["provider_contract_hash"]
    )
    assert requirement["required_checks"] == ["fixture_quality"]
    assert plan.verification_plan.deliverable_checks == ["fixture_quality"]


def test_accepted_v1_workflow_is_validated_without_rewriting_its_hash() -> None:
    registry = SkillRegistry(load_settings(), sources=())
    registry.register(_entry(source="builtin", enum=["full"]))
    source = _plan(source="builtin")
    source.workflow_steps = prepare_workflow_plan(
        source.user_message,
        source.workflow_steps,
        registry,
        seal_provider_bindings=False,
    )
    source.inputs_compiled = True
    legacy_payload = source.to_dict()
    legacy_payload.pop("plan_schema_version")
    legacy_payload.pop("provider_bindings")
    legacy_payload.pop("resolver_evidence")
    legacy = IntentPlan.from_dict(legacy_payload)
    legacy.revision_hash = canonical_plan_hash(legacy)
    accepted_payload = legacy.to_dict()
    accepted_hash = canonical_plan_hash(legacy)

    validation = PlanValidator(registry).validate(legacy)
    pipeline = PlanPipeline(
        settings=load_settings(),
        registry=registry,
        tasks=None,
        hooks=None,
    )
    candidate, final_validation = pipeline._materialize_workflow(  # noqa: SLF001
        legacy,
        validation,
    )

    assert validation.ok
    assert final_validation.ok
    assert legacy.to_dict() == accepted_payload
    assert candidate.to_dict() == accepted_payload
    assert canonical_plan_hash(legacy) == accepted_hash
    assert canonical_plan_hash(candidate) == accepted_hash
    assert "provider_bindings" not in accepted_payload
    assert "provider_binding_id" not in accepted_payload["workflow_steps"][0]


def test_runtime_derivative_of_accepted_v1_workflow_seals_exact_binding() -> None:
    registry = SkillRegistry(load_settings(), sources=())
    registry.register(_entry(source="builtin", enum=["full"]))
    source = _plan(source="builtin")
    source.workflow_steps = prepare_workflow_plan(
        source.user_message,
        source.workflow_steps,
        registry,
        seal_provider_bindings=False,
    )
    source.inputs_compiled = True
    legacy_payload = source.to_dict()
    legacy_payload.pop("plan_schema_version")
    legacy_payload.pop("provider_bindings")
    legacy_payload.pop("resolver_evidence")
    legacy = IntentPlan.from_dict(legacy_payload)
    legacy.revision_hash = canonical_plan_hash(legacy)
    accepted_hash = canonical_plan_hash(legacy)

    runtime_steps = prepare_workflow_plan(
        legacy.user_message,
        legacy.workflow_steps,
        registry,
    )

    assert runtime_steps[0]["provider_binding_id"].startswith(
        "provider-binding-"
    )
    assert runtime_steps[0]["provider_contract_hash"] == provider_contract_hash(
        registry.resolve_ref("same-name-provider", "builtin")
    )
    assert canonical_plan_hash(legacy) == accepted_hash
    assert "provider_binding_id" not in legacy.workflow_steps[0]
