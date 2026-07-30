"""Security and call-budget contract for bounded objective schema repair."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.model_plan_repair import (
    ModelPlanRepairer,
    RepairProviderContract,
    project_repair_schema,
)
from omni.agent.plan_lifecycle import model_repair_findings
from omni.agent.plan_patch import (
    PlanPatch,
    PlanPatchOp,
    allowed_patch_paths,
    apply_plan_patch,
)
from omni.agent.plan_pipeline import PlanPipeline
from omni.agent.plan_revision import create_revision
from omni.agent.plan_validator import PlanFinding
from omni.agent.provider_binding import materialize_step_provider_binding
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.skills_runtime.manifest import SkillEntry
from omni.skills_runtime.registry import SkillRegistry

_LANGUAGE_PATH = "/workflow_steps/0/input/language"


def _plan() -> IntentPlan:
    return IntentPlan(
        task_id="task-repair",
        user_message="Write the report in Chinese.",
        intent_type=IntentType.WORKFLOW,
        outputs=["report"],
        provider_inputs={"report-provider": {"language": "cn"}},
        inputs_compiled=True,
        input_compilation_errors=[{"code": "old-cache"}],
        workflow_steps=[
            {
                "id": "report",
                "capability": "report.write",
                "skill_name": "report-provider",
                "skill_source": "builtin",
                "input": {"language": "cn"},
            }
        ],
    )


def _finding(*, owner: str = "model", path: str = _LANGUAGE_PATH) -> PlanFinding:
    return PlanFinding(
        code="provider_schema_invalid",
        message="'cn' is not one of ['en', 'zh']",
        severity="blocking",
        scope="step",
        step_id="report",
        skill_name="report-provider",
        capability="report.write",
        missing_field="language",
        repairable=owner == "model",
        field_path=path,
        actual="cn",
        expected=["en", "zh"],
        owner=owner,
        repair_strategy="schema_model_patch",
        provider_binding_id="provider-binding-report",
        provider_source="builtin",
        provider_contract_hash="contract-hash-report",
        schema_keyword="enum",
        allowed_values=["en", "zh"],
    )


def _repair_contract(
    schema: dict[str, Any],
    *,
    binding_id: str = "provider-binding-report",
    name: str = "report-provider",
    source: str = "builtin",
    contract_hash: str = "contract-hash-report",
) -> RepairProviderContract:
    return RepairProviderContract(
        contract_key=f"provider-contract:{contract_hash}",
        provider_binding_id=binding_id,
        provider_name=name,
        provider_source=source,
        provider_version="1",
        provider_contract_hash=contract_hash,
        input_schema=schema,
    )


def _patch(
    revision: Any,
    findings: list[PlanFinding],
    *operations: PlanPatchOp,
) -> PlanPatch:
    return PlanPatch(
        base_revision=revision.content_hash,
        finding_ids=[finding.finding_id for finding in findings],
        operations=list(operations),
    )


def test_host_allowlist_excludes_policy_and_provider_identity_paths() -> None:
    plan = _plan()
    finding = _finding()
    revision = create_revision(plan, revision=1, source="validator")

    assert allowed_patch_paths(plan, [finding]) == frozenset({_LANGUAGE_PATH})
    for path in (
        "/tool_policy/max_tool_calls",
        "/workflow_steps/0/skill_source",
        "/workflow_steps/0/provider_binding_id",
        "/workflow_steps/0/input/_skill_source",
    ):
        with pytest.raises(ValueError, match="not allowed"):
            apply_plan_patch(
                plan,
                _patch(
                    revision,
                    [finding],
                    PlanPatchOp(op="replace", path=path, value="forged"),
                ),
                current_revision=revision,
                findings=[finding],
            )


def test_stale_base_revision_is_rejected_without_mutating_plan() -> None:
    plan = _plan()
    finding = _finding()
    revision = create_revision(plan, revision=2, source="validator")
    patch = PlanPatch(
        base_revision="stale",
        finding_ids=[finding.finding_id],
        operations=[
            PlanPatchOp(op="replace", path=_LANGUAGE_PATH, value="zh")
        ],
    )

    with pytest.raises(ValueError, match="stale"):
        apply_plan_patch(
            plan,
            patch,
            current_revision=revision,
            findings=[finding],
        )
    assert plan.workflow_steps[0]["input"]["language"] == "cn"


def test_resolver_owned_fact_is_never_model_patchable() -> None:
    plan = _plan()
    finding = _finding(
        owner="resolver",
        path="/workflow_steps/0/input/identifier",
    )
    finding.repairable = True

    assert allowed_patch_paths(plan, [finding]) == frozenset()


def test_provider_owned_schema_definition_is_never_model_patchable() -> None:
    plan = _plan()
    finding = _finding(owner="provider")
    finding.repairable = True
    settings = SimpleNamespace(
        planner=SimpleNamespace(
            model_repair="allowlist",
            model_repair_capabilities=["report.write"],
        )
    )

    assert allowed_patch_paths(plan, [finding]) == frozenset()
    assert model_repair_findings(settings, plan, [finding]) == []


def test_objective_patch_returns_clone_and_invalidates_compiler_cache() -> None:
    plan = _plan()
    finding = _finding()
    revision = create_revision(plan, revision=1, source="validator")
    repaired = apply_plan_patch(
        plan,
        _patch(
            revision,
            [finding],
            PlanPatchOp(op="replace", path=_LANGUAGE_PATH, value="zh"),
        ),
        current_revision=revision,
        findings=[finding],
    )

    assert repaired is not plan
    assert repaired.workflow_steps[0]["input"]["language"] == "zh"
    assert repaired.provider_inputs == {}
    assert repaired.inputs_compiled is False
    assert repaired.input_compilation_errors == []
    assert plan.workflow_steps[0]["input"]["language"] == "cn"


class _OneRepairLLM:
    model = "online-test"

    def __init__(
        self,
        operations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls = 0
        self.tools_seen: list[list[dict[str, Any]]] = []
        self.messages_seen: list[list[dict[str, Any]]] = []
        self.operations = operations or [
            {
                "op": "replace",
                "path": _LANGUAGE_PATH,
                "value": "zh",
            }
        ]

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> ChatWithToolsResult:
        self.calls += 1
        self.tools_seen.append(tools)
        self.messages_seen.append(messages)
        return ChatWithToolsResult(
            tool_calls=[
                ToolCall(
                    id="repair-1",
                    name="submit_plan_patch",
                    arguments={"operations": self.operations},
                )
            ]
        )


@pytest.mark.asyncio
async def test_repair_uses_one_request_and_exact_selected_provider_schema() -> None:
    plan = _plan()
    finding = _finding()
    revision = create_revision(plan, revision=1, source="validator")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "language": {
                "type": "string",
                "enum": ["en", "zh"],
                "description": "Output language selected from this exact enum.",
                "default": "secret-default-must-not-be-sent",
                "examples": ["secret-example-must-not-be-sent"],
                "x-omni": {
                    "choice_hint": "Match the user's requested language.",
                    "default": "secret-hint-default",
                },
            },
            "style": {
                "type": "object",
                "properties": {
                    "tone": {"type": "string", "enum": ["formal", "plain"]}
                },
            },
        },
        "required": ["language"],
        "allOf": [
            {
                "properties": {
                    "language": {"minLength": 2},
                    "api_key": {
                        "type": "string",
                        "default": "secret-branch-default",
                    },
                }
            }
        ],
    }
    llm = _OneRepairLLM()

    result = await ModelPlanRepairer(llm).repair(
        plan,
        [finding],
        revision=revision,
        provider_contracts={
            finding.finding_id: _repair_contract(schema),
        },
    )

    assert result is not None
    assert result.plan.workflow_steps[0]["input"]["language"] == "zh"
    assert llm.calls == 1
    assert len(llm.tools_seen[0]) == 1
    payload = json.loads(llm.messages_seen[0][1]["content"])
    assert payload["provider_contracts"] == {
        "provider-contract:contract-hash-report": {
            "provider_name": "report-provider",
            "provider_source": "builtin",
            "provider_version": "1",
            "provider_contract_hash": "contract-hash-report",
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["en", "zh"],
                        "description": (
                            "Output language selected from this exact enum."
                        ),
                        "x-omni": {
                            "choice_hint": (
                                "Match the user's requested language."
                            )
                        },
                    }
                },
                "required": ["language"],
                "allOf": [
                    {
                        "properties": {
                            "language": {"minLength": 2},
                        }
                    }
                ],
            },
        }
    }
    assert payload["current_values"] == {_LANGUAGE_PATH: "cn"}
    assert payload["findings"] == [
        {
            "code": "provider_schema_invalid",
            "consumer": {
                "step_id": "report",
                "capability": "report.write",
            },
            "expected": ["en", "zh"],
            "path": _LANGUAGE_PATH,
            "provider_contract": {
                "contract_key": "provider-contract:contract-hash-report",
                "provider_binding_id": "provider-binding-report",
                "provider_source": "builtin",
                "provider_contract_hash": "contract-hash-report",
            },
        }
    ]
    assert "secret-default" not in llm.messages_seen[0][1]["content"]
    assert "secret-example" not in llm.messages_seen[0][1]["content"]
    assert '"style"' not in llm.messages_seen[0][1]["content"]
    assert "plan" not in payload
    assert "provider_inputs" not in payload
    assert "tool_policy" not in payload


def test_schema_projection_keeps_reachable_defs_without_dangling_refs() -> None:
    schema = {
        "type": "object",
        "properties": {
            "language": {"$ref": "#/$defs/Language"},
            "api_key": {
                "type": "string",
                "default": "sk-do-not-disclose",
                "examples": ["sk-example-do-not-disclose"],
            },
        },
        "$defs": {
            "Language": {
                "type": "object",
                "properties": {
                    "locale": {"$ref": "#/$defs/Locale"},
                },
                "required": ["locale"],
            },
            "Locale": {
                "type": "string",
                "enum": ["en", "zh"],
                "default": "zh",
            },
            "Unrelated": {
                "type": "string",
                "default": "unrelated-secret",
            },
        },
    }

    projected = project_repair_schema(schema, {"language"})

    assert projected["properties"] == {
        "language": {"$ref": "#/$defs/Language"}
    }
    assert set(projected["$defs"]) == {"Language", "Locale"}
    assert projected["$defs"]["Locale"] == {
        "type": "string",
        "enum": ["en", "zh"],
    }
    encoded = json.dumps(projected, sort_keys=True)
    assert "api_key" not in encoded
    assert "do-not-disclose" not in encoded
    assert "unrelated-secret" not in encoded
    for reference in ("#/$defs/Language", "#/$defs/Locale"):
        assert reference.split("/")[-1] in projected["$defs"]


@pytest.mark.parametrize(
    "schema",
    [
        {
            "type": "object",
            "properties": {
                "language": {"$ref": "#/$defs/Missing"},
            },
        },
        {
            "type": "object",
            "properties": {
                "language": {"$ref": "https://schemas.example/language"},
            },
        },
    ],
)
def test_schema_projection_rejects_unsafe_or_dangling_refs(
    schema: dict[str, Any],
) -> None:
    assert project_repair_schema(schema, {"language"}) == {}


@pytest.mark.asyncio
async def test_batched_findings_use_exact_same_name_source_contracts() -> None:
    builtin = SkillEntry(
        name="shared-provider",
        description="builtin",
        source="builtin",
        version="1",
        capabilities=["report.write"],
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "language": {"type": "string", "enum": ["en", "zh"]},
                "api_key": {
                    "type": "string",
                    "default": "sk-builtin-secret",
                },
            },
        },
        output_schema={"type": "object"},
        input_schema_declared=True,
        output_schema_declared=True,
    )
    project = SkillEntry(
        name="shared-provider",
        description="project",
        source="project_omni",
        version="2",
        capabilities=["report.rewrite"],
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tone": {"type": "string", "enum": ["formal", "plain"]},
                "api_key": {
                    "type": "string",
                    "examples": ["sk-project-secret"],
                },
            },
        },
        output_schema={"type": "object"},
        input_schema_declared=True,
        output_schema_declared=True,
    )
    first_step = {
        "id": "first",
        "capability": "report.write",
        "skill_name": builtin.name,
        "skill_source": builtin.source,
        "input": {"language": "cn"},
    }
    second_step = {
        "id": "second",
        "capability": "report.rewrite",
        "skill_name": project.name,
        "skill_source": project.source,
        "input": {"tone": "academic"},
    }
    materialize_step_provider_binding(first_step, builtin)
    materialize_step_provider_binding(second_step, project)
    plan = IntentPlan(
        task_id="task-multi-provider",
        user_message="Write in Chinese, then rewrite formally.",
        intent_type=IntentType.WORKFLOW,
        outputs=["report"],
        workflow_steps=[first_step, second_step],
    )
    first_finding = PlanFinding(
        code="provider_schema_invalid",
        message="invalid language",
        scope="step",
        step_id="first",
        skill_name=builtin.name,
        capability="report.write",
        repairable=True,
        field_path="/workflow_steps/0/input/language",
        owner="model",
        provider_binding_id=str(first_step["provider_binding_id"]),
        provider_source=builtin.source,
        provider_contract_hash=str(first_step["provider_contract_hash"]),
        schema_keyword="enum",
        expected=["en", "zh"],
    )
    second_finding = PlanFinding(
        code="provider_schema_invalid",
        message="invalid tone",
        scope="step",
        step_id="second",
        skill_name=project.name,
        capability="report.rewrite",
        repairable=True,
        field_path="/workflow_steps/1/input/tone",
        owner="model",
        provider_binding_id=str(second_step["provider_binding_id"]),
        provider_source=project.source,
        provider_contract_hash=str(second_step["provider_contract_hash"]),
        schema_keyword="enum",
        expected=["formal", "plain"],
    )
    registry = SkillRegistry(load_settings(), sources=())
    registry.register(builtin)
    registry.register(project)
    pipeline = PlanPipeline(
        settings=load_settings(),
        registry=registry,
        tasks=None,
        hooks=None,
    )

    contracts, eligible = pipeline._repair_providers(  # noqa: SLF001
        plan,
        [first_finding, second_finding],
    )

    assert eligible == [first_finding, second_finding]
    assert set(contracts) == {
        first_finding.finding_id,
        second_finding.finding_id,
    }
    assert contracts[first_finding.finding_id].provider_source == "builtin"
    assert (
        contracts[second_finding.finding_id].provider_source
        == "project_omni"
    )
    assert (
        contracts[first_finding.finding_id].contract_key
        != contracts[second_finding.finding_id].contract_key
    )
    drifted_finding = PlanFinding(
        code="provider_schema_invalid",
        message="drifted provider identity",
        scope="step",
        step_id="first",
        skill_name=builtin.name,
        capability="report.write",
        repairable=True,
        field_path="/workflow_steps/0/input/language",
        owner="model",
        provider_binding_id=str(first_step["provider_binding_id"]),
        provider_source=project.source,
        provider_contract_hash=str(second_step["provider_contract_hash"]),
        schema_keyword="enum",
    )
    drifted_contracts, drifted_eligible = pipeline._repair_providers(  # noqa: SLF001
        plan,
        [drifted_finding],
    )
    assert drifted_contracts == {}
    assert drifted_eligible == []

    llm = _OneRepairLLM(
        [
            {
                "op": "replace",
                "path": "/workflow_steps/0/input/language",
                "value": "zh",
            },
            {
                "op": "replace",
                "path": "/workflow_steps/1/input/tone",
                "value": "formal",
            },
        ]
    )
    result = await ModelPlanRepairer(llm).repair(
        plan,
        eligible,
        revision=create_revision(plan, revision=1, source="validator"),
        provider_contracts=contracts,
    )

    assert result is not None
    payload = json.loads(llm.messages_seen[0][1]["content"])
    assert len(payload["provider_contracts"]) == 2
    assert {
        item["provider_contract"]["provider_binding_id"]
        for item in payload["findings"]
    } == {
        first_step["provider_binding_id"],
        second_step["provider_binding_id"],
    }
    assert {
        item["provider_contract"]["provider_source"]
        for item in payload["findings"]
    } == {"builtin", "project_omni"}
    for item in payload["findings"]:
        identity = item["provider_contract"]
        contract = payload["provider_contracts"][identity["contract_key"]]
        assert (
            identity["provider_contract_hash"]
            == contract["provider_contract_hash"]
        )
    encoded = llm.messages_seen[0][1]["content"]
    assert "sk-builtin-secret" not in encoded
    assert "sk-project-secret" not in encoded
    for contract in payload["provider_contracts"].values():
        assert set(contract["input_schema"]["properties"]) in (
            {"language"},
            {"tone"},
        )


@pytest.mark.asyncio
async def test_repair_skips_finding_when_exact_contract_identity_drifts() -> None:
    finding = _finding()
    contract = _repair_contract(
        {
            "type": "object",
            "properties": {
                "language": {"type": "string", "enum": ["en", "zh"]}
            },
        },
        contract_hash="different-contract-hash",
    )
    llm = _OneRepairLLM()

    result = await ModelPlanRepairer(llm).repair(
        _plan(),
        [finding],
        revision=create_revision(_plan(), revision=1, source="validator"),
        provider_contracts={finding.finding_id: contract},
    )

    assert result is None
    assert llm.calls == 0


class _ForbiddenMockLLM:
    model = "mock"

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> ChatWithToolsResult:
        self.calls += 1
        raise AssertionError("offline repair must not call a model")


@pytest.mark.asyncio
async def test_offline_provider_skips_objective_repair_without_model_call() -> None:
    llm = _ForbiddenMockLLM()
    result = await ModelPlanRepairer(llm).repair(
        _plan(),
        [_finding()],
        revision=create_revision(_plan(), revision=1, source="validator"),
        provider_contracts={
            _finding().finding_id: _repair_contract({}),
        },
    )

    assert result is None
    assert llm.calls == 0


def test_repair_policy_is_capability_scoped_and_auto_requires_full_contract() -> None:
    plan = _plan()
    finding = _finding()
    denied = SimpleNamespace(
        planner=SimpleNamespace(
            model_repair="allowlist",
            model_repair_capabilities=["slides.generate"],
        )
    )
    allowed = SimpleNamespace(
        planner=SimpleNamespace(
            model_repair="allowlist",
            model_repair_capabilities=["report.write"],
        )
    )
    assert model_repair_findings(denied, plan, [finding]) == []
    assert model_repair_findings(allowed, plan, [finding]) == [finding]

    entry = SimpleNamespace(
        name="report-provider",
        source="builtin",
        contract_level="partial",
        trusted=True,
    )

    class Registry:
        def get_scoped(self, source: str, name: str) -> Any:
            return (
                entry
                if (source, name) == ("builtin", "report-provider")
                else None
            )

    auto = SimpleNamespace(
        planner=SimpleNamespace(
            model_repair="auto",
            model_repair_capabilities=[],
        )
    )
    assert model_repair_findings(
        auto,
        plan,
        [finding],
        registry=Registry(),
    ) == []
    entry.contract_level = "full"
    assert model_repair_findings(
        auto,
        plan,
        [finding],
        registry=Registry(),
    ) == [finding]


def test_production_default_enables_bounded_objective_repair() -> None:
    settings = load_settings()

    assert not hasattr(settings.planner, "binding_validation")
    assert settings.planner.model_repair == "auto"
