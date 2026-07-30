"""Objective provider-schema gates and bounded repair metadata."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.plan_validator import SEVERITY_DEGRADED, PlanValidator
from omni.agent.provider_binding import provider_contract_hash
from omni.config import load_settings
from omni.core.tool_contracts import (
    ProviderInputCompiler,
    provider_schema_definition_errors,
    skill_input_contract_error,
)
from omni.runtime.workflow_plan import WorkflowNeedsInput, prepare_workflow_plan
from omni.skills_runtime.manifest import SkillEntry, parse_skill_text
from omni.skills_runtime.registry import SkillRegistry


def _full_entry(schema: dict) -> SimpleNamespace:
    return SimpleNamespace(input_schema=schema, contract_level="full")


def test_full_provider_contract_rejects_unconsumed_unknown_input() -> None:
    compiler = ProviderInputCompiler()
    compiled = compiler.compile_entry(
        _full_entry(
            {
                "type": "object",
                "properties": {
                    "input": {"type": "string"},
                    "figure_kind": {
                        "type": "string",
                        "enum": ["generic", "rag"],
                    },
                },
                "required": ["input"],
            }
        ),
        semantic_input={
            "input": "RAG architecture",
            "figuer_kind": "rag",
        },
        raw_message="",
    )

    assert not compiled.ok
    assert compiled.arguments == {"input": "RAG architecture"}
    assert compiled.errors == (
        {
            "code": "unknown_provider_field",
            "field": "figuer_kind",
            "path": "figuer_kind",
            "keyword": "additionalProperties",
            "missing": [],
            "label": "",
            "reason": "field 'figuer_kind' is not declared by the selected provider",
            "message": "field 'figuer_kind' is not declared by the selected provider",
            "allowed_values": [],
        },
    )


def test_full_provider_contract_keeps_provider_neutral_single_scalar_binding() -> None:
    compiler = ProviderInputCompiler()
    compiled = compiler.compile_entry(
        _full_entry(
            {
                "type": "object",
                "properties": {"provider_query": {"type": "string"}},
                "required": ["provider_query"],
            }
        ),
        semantic_input={"input": "memory retrieval"},
        raw_message="",
    )

    assert compiled.ok
    assert compiled.arguments == {"provider_query": "memory retrieval"}


def test_provider_compiler_reports_nested_schema_pointer_and_enum() -> None:
    compiler = ProviderInputCompiler()
    compiled = compiler.compile_entry(
        _full_entry(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "options": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["brief", "full"],
                            }
                        },
                        "required": ["mode"],
                    }
                },
                "required": ["options"],
            }
        ),
        semantic_input={"options": {"mode": "verbose"}},
        raw_message="",
    )

    assert not compiled.ok
    error = compiled.errors[0]
    assert error["field"] == "options"
    assert error["path"] == "options.mode"
    assert error["keyword"] == "enum"
    assert error["allowed_values"] == ["brief", "full"]


def test_absent_schema_and_explicit_empty_object_contract_are_distinct() -> None:
    compiler = ProviderInputCompiler()

    absent = compiler.compile_schema(
        None,
        semantic_input={"undeclared": ""},
        raw_message="",
    )
    explicit = compiler.compile_schema(
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        semantic_input={"undeclared": ""},
        raw_message="",
    )

    assert absent.ok
    assert absent.arguments == {"undeclared": ""}
    assert explicit.arguments == {}
    assert explicit.errors[0]["path"] == "undeclared"
    assert explicit.errors[0]["keyword"] == "additionalProperties"


def test_required_empty_values_are_governed_by_json_schema_constraints() -> None:
    compiler = ProviderInputCompiler()
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "items": {"type": "array"},
            "options": {"type": "object"},
        },
        "required": ["text", "items", "options"],
    }

    compiled = compiler.compile_schema(
        schema,
        semantic_input={"text": "", "items": [], "options": {}},
        raw_message="fallback text must not replace an explicit empty string",
    )

    assert compiled.ok
    assert compiled.arguments == {"text": "", "items": [], "options": {}}


@pytest.mark.parametrize(
    ("field_schema", "value", "keyword"),
    [
        ({"type": "string", "minLength": 1}, "", "minLength"),
        ({"type": "array", "minItems": 1}, [], "minItems"),
        ({"type": "object", "minProperties": 1}, {}, "minProperties"),
    ],
)
def test_empty_value_rejection_comes_from_declared_json_schema(
    field_schema: dict,
    value: object,
    keyword: str,
) -> None:
    compiler = ProviderInputCompiler()

    compiled = compiler.compile_schema(
        {
            "type": "object",
            "properties": {"value": field_schema},
            "required": ["value"],
        },
        semantic_input={"value": value},
        raw_message="",
    )

    assert not compiled.ok
    assert compiled.arguments == {"value": value}
    assert compiled.errors[0]["path"] == "value"
    assert compiled.errors[0]["keyword"] == keyword


@pytest.mark.parametrize(
    ("schema", "semantic"),
    [
        (
            {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "rag"},
                            "nodes": {"type": "array"},
                        },
                        "required": ["kind", "nodes"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {"kind": {"const": "generic"}},
                        "required": ["kind"],
                        "additionalProperties": False,
                    },
                ]
            },
            {"kind": "rag", "nodes": []},
        ),
        (
            {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                    {
                        "type": "object",
                        "properties": {"identifier": {"type": "string"}},
                        "required": ["identifier"],
                    },
                ]
            },
            {"identifier": ""},
        ),
        (
            {
                "allOf": [
                    {
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                        "required": ["title"],
                    },
                    {
                        "type": "object",
                        "properties": {"tags": {"type": "array"}},
                        "required": ["tags"],
                    },
                ]
            },
            {"title": "", "tags": []},
        ),
    ],
)
def test_root_composed_provider_schema_validates_the_complete_instance(
    schema: dict,
    semantic: dict,
) -> None:
    compiled = ProviderInputCompiler().compile_entry(
        _full_entry(schema),
        semantic_input=semantic,
        raw_message="",
    )

    assert compiled.ok
    assert compiled.arguments == semantic


def test_root_composed_provider_schema_rejects_an_invalid_complete_instance() -> None:
    compiled = ProviderInputCompiler().compile_entry(
        _full_entry(
            {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"kind": {"const": "rag"}},
                        "required": ["kind"],
                    },
                    {
                        "type": "object",
                        "properties": {"kind": {"const": "generic"}},
                        "required": ["kind"],
                    },
                ]
            }
        ),
        semantic_input={"kind": "other"},
        raw_message="",
    )

    assert not compiled.ok
    assert compiled.arguments == {"kind": "other"}
    assert compiled.errors[0]["path"] == "$"
    assert compiled.errors[0]["keyword"] == "oneOf"


def test_precompiled_provider_arguments_are_revalidated_with_full_schema() -> None:
    entry = _full_entry(
        {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["brief", "full"],
                        }
                    },
                    "required": ["mode"],
                }
            },
            "required": ["options"],
        }
    )

    nested = skill_input_contract_error(
        entry,
        {"options": {"mode": "invented"}},
    )
    unknown = skill_input_contract_error(
        entry,
        {"options": {"mode": "full"}, "forged": True},
    )

    assert nested["path"] == "options.mode"
    assert nested["keyword"] == "enum"
    assert nested["allowed_values"] == ["brief", "full"]
    assert unknown["path"] == "forged"
    assert unknown["keyword"] == "additionalProperties"


def test_final_plan_gate_revalidates_precompiled_nested_arguments() -> None:
    registry = SkillRegistry(load_settings(), sources=())
    registry.register(
        SkillEntry(
            name="nested-report",
            description="nested objective schema fixture",
            source="project_omni",
            trusted=True,
            capabilities=["report.nested"],
            input_schema={
                "type": "object",
                "properties": {
                    "options": {
                        "type": "object",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["brief", "full"],
                            }
                        },
                        "required": ["mode"],
                        "additionalProperties": False,
                    }
                },
                "required": ["options"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        )
    )
    plan = IntentPlan(
        task_id="task-precompiled-nested",
        user_message="write the nested report",
        intent_type=IntentType.WORKFLOW,
        inputs_compiled=True,
        outputs=["report"],
        workflow_steps=[
            {
                "id": "report",
                "capability": "report.nested",
                "skill_name": "nested-report",
                "skill_source": "project_omni",
                "input": {"options": {"mode": "invented"}},
                "input_compiled": True,
            }
        ],
    )

    validation = PlanValidator(registry).validate(plan)
    finding = next(item for item in validation.findings if item.code == "provider_schema_invalid")

    assert not validation.ok
    assert finding.field_path == "/workflow_steps/0/input/options/mode"
    assert finding.schema_keyword == "enum"
    assert finding.allowed_values == ["brief", "full"]
    assert finding.provider_binding_id.startswith("provider-binding-")
    assert finding.provider_contract_hash


def test_plan_validator_emits_patchable_objective_json_pointer() -> None:
    registry = SkillRegistry(load_settings(), sources=())
    registry.register(
        SkillEntry(
            name="objective-report",
            description="objective schema fixture",
            source="project_omni",
            trusted=True,
            capabilities=["report.write"],
            input_schema={
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["en", "zh"],
                        "x-omni": {"binding_owner": "model"},
                    }
                },
                "required": ["language"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        )
    )
    plan = IntentPlan(
        task_id="task-objective-schema",
        user_message="write a report",
        intent_type=IntentType.WORKFLOW,
        outputs=["artifact"],
        workflow_steps=[
            {
                "id": "report",
                "capability": "report.write",
                "skill_name": "objective-report",
                "skill_source": "project_omni",
                "input": {"language": "cn"},
            }
        ],
    )

    result = PlanValidator(registry).validate(plan)

    finding = next(item for item in result.findings if item.code == "provider_schema_invalid")
    assert finding.field_path == "/workflow_steps/0/input/language"
    assert finding.owner == "model"
    assert finding.repairable is True
    assert finding.actual == "cn"
    assert finding.expected == ["en", "zh"]
    assert finding.repair_strategy == "schema_model_patch"


def test_schema_keyword_outside_model_repair_allowlist_is_not_patchable() -> None:
    registry = SkillRegistry(load_settings(), sources=())
    registry.register(
        SkillEntry(
            name="closed-report",
            description="closed provider schema fixture",
            source="project_omni",
            trusted=True,
            capabilities=["report.closed"],
            input_schema={
                "type": "object",
                "properties": {"language": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        )
    )
    plan = IntentPlan(
        task_id="task-closed-schema",
        user_message="write a report",
        intent_type=IntentType.WORKFLOW,
        outputs=["artifact"],
        workflow_steps=[
            {
                "id": "report",
                "capability": "report.closed",
                "skill_name": "closed-report",
                "skill_source": "project_omni",
                "input": {"language": "en", "invented": True},
            }
        ],
    )

    validation = PlanValidator(registry).validate(plan)
    finding = next(
        item
        for item in validation.findings
        if item.code == "provider_schema_invalid" and item.schema_keyword == "additionalProperties"
    )

    assert finding.owner == "model"
    assert finding.repairable is False
    # Not patchable, but not a question either. The compiler already left
    # ``invented`` out of the provider arguments, so the remedy is spent before
    # validation runs and the finding only has to report it. Routing this to
    # ``needs_input`` is what let run 0db3d740 ask the user to answer a schema
    # diagnostic; a field the provider never declared is nobody's to supply.
    assert finding.repair_strategy == "drop_undeclared_input"
    assert finding.severity == SEVERITY_DEGRADED
    assert finding.message.endswith("the value was dropped and the run continued without it")


@pytest.mark.parametrize(
    ("schema", "keyword"),
    [
        ({"type": 7}, "invalid_schema"),
        (["not", "a", "schema"], "invalid_schema"),
        ({"$ref": "#/$defs/missing"}, "unresolved_ref"),
        (
            {"$ref": "https://schemas.example.invalid/provider.json"},
            "external_ref",
        ),
    ],
)
def test_provider_schema_definition_errors_are_never_model_repairable(
    schema: object,
    keyword: str,
) -> None:
    registry = SkillRegistry(load_settings(), sources=())
    registry.register(
        SkillEntry(
            name="broken-contract-provider",
            description="provider with a broken contract definition",
            source="project_omni",
            trusted=True,
            capabilities=["report.broken"],
            input_schema=schema,
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        )
    )
    plan = IntentPlan(
        task_id=f"task-{keyword}",
        user_message="write a report",
        intent_type=IntentType.WORKFLOW,
        outputs=["artifact"],
        workflow_steps=[
            {
                "id": "report",
                "capability": "report.broken",
                "skill_name": "broken-contract-provider",
                "skill_source": "project_omni",
                "input": {"language": "en"},
            }
        ],
    )

    validation = PlanValidator(registry).validate(plan)
    finding = next(
        item
        for item in validation.findings
        if item.code == "provider_schema_invalid" and item.schema_keyword == keyword
    )

    assert not validation.ok
    assert finding.owner == "provider"
    assert finding.repairable is False
    assert finding.repair_strategy == "provider_contract_fix"
    assert finding.actual is None


def test_manifest_schema_presence_preserves_composed_and_boolean_contracts() -> None:
    composed = parse_skill_text(
        """---
name: composed-provider
description: composed provider
metadata:
  helixforge:
    input_schema:
      oneOf:
        - type: object
          properties: {query: {type: string}}
          required: [query]
    output_schema: false
---
instructions
""",
        default_name="composed-provider",
        source="project_omni",
    )
    absent = parse_skill_text(
        """---
name: absent-provider
description: absent provider
metadata:
  helixforge: {}
---
instructions
""",
        default_name="absent-provider",
        source="project_omni",
    )
    explicit_empty = parse_skill_text(
        """---
name: explicit-empty-provider
description: explicitly unconstrained provider
metadata:
  helixforge:
    input_schema: {}
    output_schema: {}
---
instructions
""",
        default_name="explicit-empty-provider",
        source="project_omni",
    )

    assert composed.contract_level == "full"
    assert composed.input_schema_declared is True
    assert composed.output_schema_declared is True
    assert composed.output_schema is False
    assert absent.contract_level == "none"
    assert absent.input_schema_declared is False
    assert absent.output_schema_declared is False
    assert explicit_empty.contract_level == "full"
    assert explicit_empty.input_schema == {}
    assert explicit_empty.output_schema == {}


def test_absent_and_explicit_empty_manifest_schemas_compile_differently() -> None:
    absent = parse_skill_text(
        """---
name: absent-schema
description: absent schema
metadata:
  helixforge: {}
---
instructions
""",
        default_name="absent-schema",
        source="project_omni",
    )
    explicit = parse_skill_text(
        """---
name: explicit-schema
description: explicit schema
metadata:
  helixforge:
    input_schema: {}
    output_schema: {}
---
instructions
""",
        default_name="explicit-schema",
        source="project_omni",
    )
    compiler = ProviderInputCompiler()

    absent_result = compiler.compile_entry(
        absent,
        semantic_input={"undeclared": ""},
        raw_message="",
    )
    explicit_result = compiler.compile_entry(
        explicit,
        semantic_input={"undeclared": ""},
        raw_message="",
    )

    assert absent_result.arguments == {"undeclared": ""}
    assert explicit_result.arguments == {"undeclared": ""}
    assert absent.input_schema_declared is False
    assert explicit.input_schema_declared is True
    assert provider_contract_hash(absent) != provider_contract_hash(explicit)


def test_boolean_schema_values_have_distinct_provider_contract_hashes() -> None:
    allowed = SkillEntry(
        name="boolean-provider",
        description="boolean input contract",
        source="project_omni",
        input_schema=True,
        input_schema_declared=True,
    )
    denied = SkillEntry(
        name="boolean-provider",
        description="boolean input contract",
        source="project_omni",
        input_schema=False,
        input_schema_declared=True,
    )

    assert provider_contract_hash(allowed) != provider_contract_hash(denied)


def test_explicit_null_schema_is_invalid_but_absent_schema_is_unconstrained() -> None:
    explicit_null = SkillEntry(
        name="null-contract",
        description="explicit null input schema",
        source="project_omni",
        input_schema=None,
        input_schema_declared=True,
        output_schema={"type": "object"},
        output_schema_declared=True,
    )
    absent = SkillEntry(
        name="absent-contract",
        description="absent input schema",
        source="project_omni",
        input_schema=None,
        input_schema_declared=False,
    )

    compiled = ProviderInputCompiler().compile_entry(
        explicit_null,
        semantic_input={"input": "report"},
        raw_message="",
    )
    direct_error = skill_input_contract_error(explicit_null, {"input": "report"})

    assert not compiled.ok
    assert compiled.errors[0]["keyword"] == "invalid_schema"
    assert direct_error["code"] == "provider_schema_invalid"
    assert direct_error["keyword"] == "invalid_schema"
    assert provider_schema_definition_errors(explicit_null)[0]["schema_field"] == "input_schema"
    assert ProviderInputCompiler().compile_entry(
        absent,
        semantic_input={"input": "report"},
        raw_message="",
    ).ok
    assert skill_input_contract_error(absent, {"input": "report"}) == {}
    assert provider_schema_definition_errors(absent) == ()


@pytest.mark.parametrize("null_field", ["input_schema", "output_schema"])
def test_plan_validator_rejects_explicit_null_provider_schema(null_field: str) -> None:
    registry = SkillRegistry(load_settings(), sources=())
    schemas: dict[str, object] = {
        "input_schema": {
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
    }
    schemas[null_field] = None
    registry.register(
        SkillEntry(
            name=f"null-{null_field}",
            description="provider with an explicit null schema",
            source="project_omni",
            trusted=True,
            capabilities=["report.null"],
            input_schema=schemas["input_schema"],
            input_schema_declared=True,
            output_schema=schemas["output_schema"],
            output_schema_declared=True,
        )
    )
    plan = IntentPlan(
        task_id=f"task-null-{null_field}",
        user_message="write a report",
        intent_type=IntentType.WORKFLOW,
        outputs=["artifact"],
        workflow_steps=[
            {
                "id": "report",
                "capability": "report.null",
                "skill_name": f"null-{null_field}",
                "skill_source": "project_omni",
                "input": {"input": "report"},
            }
        ],
    )

    validation = PlanValidator(registry).validate(plan)
    finding = next(
        item
        for item in validation.findings
        if item.code == "provider_schema_invalid"
        and item.schema_keyword == "invalid_schema"
    )

    assert not validation.ok
    assert finding.owner == "provider"
    assert finding.repairable is False
    assert finding.repair_strategy == "provider_contract_fix"


@pytest.mark.parametrize("null_field", ["input_schema", "output_schema"])
def test_direct_workflow_ingress_rejects_explicit_null_provider_schema(
    null_field: str,
) -> None:
    registry = SkillRegistry(load_settings(), sources=())
    schemas: dict[str, object] = {
        "input_schema": {
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
    }
    schemas[null_field] = None
    registry.register(
        SkillEntry(
            name=f"workflow-null-{null_field}",
            description="provider with an explicit null schema",
            source="project_omni",
            trusted=True,
            input_schema=schemas["input_schema"],
            input_schema_declared=True,
            output_schema=schemas["output_schema"],
            output_schema_declared=True,
        )
    )

    with pytest.raises(WorkflowNeedsInput) as exc_info:
        prepare_workflow_plan(
            "write report",
            [
                {
                    "id": "report",
                    "skill_name": f"workflow-null-{null_field}",
                    "skill_source": "project_omni",
                    "input": {"input": "report"},
                }
            ],
            registry,
        )

    assert exc_info.value.missing[0]["missing"] == ["provider_schema"]
    assert exc_info.value.missing[0]["label"] == null_field
    assert "valid JSON schema" in exc_info.value.missing[0]["reason"]


