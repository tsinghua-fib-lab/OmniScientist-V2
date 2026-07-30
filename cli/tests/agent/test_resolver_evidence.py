"""Objective resolver evidence is independent from semantic binding policy."""

from __future__ import annotations

from copy import deepcopy

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.plan_pipeline import PlanPipeline
from omni.agent.plan_revision import canonical_plan_hash, create_revision
from omni.agent.resolver_evidence import (
    seal_resolver_evidence,
    validate_resolver_evidence,
)
from omni.config import load_settings
from omni.skills_runtime.manifest import DeliveryMode, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry


def _registry() -> SkillRegistry:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    registry.register(
        SkillEntry(
            name="doi-fetch",
            description="fetch a DOI",
            source="builtin",
            kind=SkillKind.PYTHON_ENGINE,
            delivery_mode=DeliveryMode.ASYNC_TASK,
            role="support",
            capabilities=["doc.fetch.doi"],
            input_schema={
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "format": "doi"}
                },
                "required": ["identifier"],
            },
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        )
    )
    registry.register(
        SkillEntry(
            name="path-reader",
            description="read a local path",
            source="builtin",
            kind=SkillKind.PYTHON_ENGINE,
            delivery_mode=DeliveryMode.ASYNC_TASK,
            role="support",
            capabilities=["file.read"],
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "format": "file_path"}
                },
                "required": ["path"],
            },
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        )
    )
    return registry


def _workflow_plan(
    *,
    message: str,
    capability: str,
    skill: str,
    field: str,
    value: str,
) -> IntentPlan:
    return IntentPlan(
        task_id="resolver-evidence-task",
        user_message=message,
        intent_type=IntentType.WORKFLOW,
        outputs=["result"],
        workflow_steps=[
            {
                "id": "resolve",
                "capability": capability,
                "skill_name": skill,
                "skill_source": "builtin",
                "input": {field: value},
            }
        ],
    )


def _fact_codes(validation: object) -> set[str]:
    return {
        finding.code
        for finding in getattr(validation, "findings", [])
        if finding.owner == "resolver"
    }


def test_resolver_fact_gate_is_enforced_for_non_canonical_free_text() -> None:
    # The gate still fails closed for a value that is *not* a locally-provable
    # identifier — a paper title sitting in an ``arxiv_id`` field. Free text cannot
    # self-verify; it must be grounded (search) or handed to the ReAct floor.
    settings = load_settings()
    registry = _registry()
    pipeline = PlanPipeline(
        settings=settings,
        registry=registry,
        tasks=None,
        hooks=None,
    )
    plan = _workflow_plan(
        message="Fetch Attention Is All You Need.",
        capability="paper.fetch.arxiv",
        skill="arxiv-fetch",
        field="identifier",
        value="Attention Is All You Need",
    )

    validation = pipeline._validate(plan)  # noqa: SLF001

    assert "grounded_binding_unverified" in _fact_codes(validation)
    assert not validation.ok


def test_syntactically_valid_identifier_is_admitted_without_network_gate() -> None:
    # Regression for the live task failure: a model-bound, syntactically valid
    # arXiv id (not quoted in the message) must be admitted at plan time as a
    # locally-provable fact — ``syntactic`` — and NOT be forced through a slow,
    # low-precision network title search that fails closed. The provider proves
    # the id exists when it fetches it; a wrong-but-valid id is caught at
    # execution by verify-by-fetch, not by blocking the plan.
    registry = _registry()
    plan = _workflow_plan(
        message="Fetch Attention Is All You Need.",
        capability="paper.fetch.arxiv",
        skill="arxiv-fetch",
        field="identifier",
        value="2401.99999",
    )

    validation = PlanPipeline(
        settings=load_settings(),
        registry=registry,
        tasks=None,
        hooks=None,
    )._validate_structural(plan)  # noqa: SLF001

    assert validation.ok
    assert plan.resolver_evidence[0]["verified"] is True
    assert plan.resolver_evidence[0]["verification_mode"] == "syntactic"


@pytest.mark.parametrize(
    ("message", "capability", "skill", "field", "value", "mode"),
    [
        (
            "Fetch arXiv 1706.03762.",
            "paper.fetch.arxiv",
            "arxiv-fetch",
            "identifier",
            "1706.03762",
            "user_exact",
        ),
        (
            "Fetch DOI 10.1234/example.",
            "doc.fetch.doi",
            "doi-fetch",
            "identifier",
            "10.1234/example",
            "user_exact",
        ),
    ],
)
def test_explicit_user_identifier_is_verified_locally(
    message: str,
    capability: str,
    skill: str,
    field: str,
    value: str,
    mode: str,
) -> None:
    registry = _registry()
    plan = _workflow_plan(
        message=message,
        capability=capability,
        skill=skill,
        field=field,
        value=value,
    )

    validation = PlanPipeline(
        settings=load_settings(),
        registry=registry,
        tasks=None,
        hooks=None,
    )._validate_structural(plan)  # noqa: SLF001

    assert validation.ok
    assert plan.resolver_evidence[0]["verified"] is True
    assert plan.resolver_evidence[0]["verification_mode"] == mode


def test_existing_path_uses_local_exists_evidence(tmp_path) -> None:  # noqa: ANN001
    source = tmp_path / "paper.pdf"
    source.write_text("offline fixture", encoding="utf-8")
    registry = _registry()
    plan = _workflow_plan(
        message="Read the attached local paper.",
        capability="file.read",
        skill="path-reader",
        field="path",
        value=str(source),
    )

    validation = PlanPipeline(
        settings=load_settings(),
        registry=registry,
        tasks=None,
        hooks=None,
    )._validate_structural(plan)  # noqa: SLF001

    assert validation.ok
    assert plan.resolver_evidence[0]["verified"] is True
    assert plan.resolver_evidence[0]["verification_mode"] == "local_exists"


def test_missing_path_is_not_accepted_as_local_evidence(tmp_path) -> None:  # noqa: ANN001
    source = tmp_path / "missing.pdf"
    registry = _registry()
    plan = _workflow_plan(
        message=f"Read {source}.",
        capability="file.read",
        skill="path-reader",
        field="path",
        value=str(source),
    )

    validation = PlanPipeline(
        settings=load_settings(),
        registry=registry,
        tasks=None,
        hooks=None,
    )._validate_structural(plan)  # noqa: SLF001

    assert "grounded_binding_unverified" in _fact_codes(validation)
    assert plan.resolver_evidence[0]["required_mode"] == "local_exists"
    assert plan.resolver_evidence[0]["verified"] is False


def test_free_text_in_doi_field_requires_grounded_evidence() -> None:
    # A free-text title in a DOI field is not locally provable, so the fact gate
    # still requires grounding and fails closed until it is sealed.
    registry = _registry()
    plan = _workflow_plan(
        message="Fetch the paper named Example Systems.",
        capability="doc.fetch.doi",
        skill="doi-fetch",
        field="identifier",
        value="Example Systems",
    )

    validation = PlanPipeline(
        settings=load_settings(),
        registry=registry,
        tasks=None,
        hooks=None,
    )._validate_structural(plan)  # noqa: SLF001

    finding = next(
        item
        for item in validation.findings
        if item.code == "grounded_binding_unverified"
    )
    evidence = plan.resolver_evidence[0]
    assert finding.provider_binding_id == evidence["provider_binding_id"]
    assert finding.provider_source == evidence["provider_source"]
    assert finding.provider_contract_hash == evidence["contract_hash"]
    assert plan.resolver_evidence[0]["verified"] is False


def test_model_supplied_canonical_doi_is_admitted_syntactically() -> None:
    # A canonical DOI the model bound (described by title, not quoted verbatim) is
    # a locally-provable fact, exactly like an arXiv id: admitted as ``syntactic``
    # without a plan-time network gate. Existence is proven when the doc is fetched.
    registry = _registry()
    plan = _workflow_plan(
        message="Fetch the paper named Example Systems.",
        capability="doc.fetch.doi",
        skill="doi-fetch",
        field="identifier",
        value="10.1234/example",
    )

    validation = PlanPipeline(
        settings=load_settings(),
        registry=registry,
        tasks=None,
        hooks=None,
    )._validate_structural(plan)  # noqa: SLF001

    assert validation.ok
    assert plan.resolver_evidence[0]["verified"] is True
    assert plan.resolver_evidence[0]["verification_mode"] == "syntactic"


def test_grounded_search_evidence_is_sealed_for_the_exact_binding() -> None:
    registry = _registry()
    plan = _workflow_plan(
        message="Fetch Attention Is All You Need.",
        capability="paper.fetch.arxiv",
        skill="arxiv-fetch",
        field="identifier",
        value="Attention Is All You Need",
    )
    seal_resolver_evidence(
        plan,
        registry,
        field_path="/workflow_steps/0/input/identifier",
        value="Attention Is All You Need",
        verification_mode="grounded_search",
        source="arxiv_id.search",
    )

    validation = PlanPipeline(
        settings=load_settings(),
        registry=registry,
        tasks=None,
        hooks=None,
    )._validate_structural(plan)  # noqa: SLF001

    assert validation.ok
    assert plan.resolver_evidence[0]["verified"] is True
    assert plan.resolver_evidence[0]["verification_mode"] == "grounded_search"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("provider_source", "user_omni"),
        ("field_path", "/workflow_steps/0/input/other_identifier"),
        ("value", "2401.99999"),
        ("contract_hash", "forged-contract-hash"),
    ],
)
def test_grounded_evidence_drift_fails_closed(
    field: str,
    replacement: str,
) -> None:
    registry = _registry()
    plan = _workflow_plan(
        message="Fetch Attention Is All You Need.",
        capability="paper.fetch.arxiv",
        skill="arxiv-fetch",
        field="identifier",
        value="Attention Is All You Need",
    )
    seal_resolver_evidence(
        plan,
        registry,
        field_path="/workflow_steps/0/input/identifier",
        value="Attention Is All You Need",
        verification_mode="grounded_search",
        source="arxiv_id.search",
    )
    plan.resolver_evidence[0][field] = replacement

    validation = PlanPipeline(
        settings=load_settings(),
        registry=registry,
        tasks=None,
        hooks=None,
    )._validate_structural(plan)  # noqa: SLF001

    assert "grounded_binding_unverified" in _fact_codes(validation)
    assert not validation.ok


def test_current_value_drift_invalidates_grounded_evidence() -> None:
    registry = _registry()
    plan = _workflow_plan(
        message="Fetch Attention Is All You Need.",
        capability="paper.fetch.arxiv",
        skill="arxiv-fetch",
        field="identifier",
        value="Attention Is All You Need",
    )
    assert seal_resolver_evidence(
        plan,
        registry,
        field_path="/workflow_steps/0/input/identifier",
        value="Attention Is All You Need",
        verification_mode="grounded_search",
        source="arxiv_id.search",
    )
    # Drift to a *different* non-canonical title: the sealed grounded_search
    # evidence was bound to the original title, so the new value is unproved. (A
    # canonical id would instead be admitted syntactically — that path is covered
    # by ``test_syntactically_valid_identifier_is_admitted_without_network_gate``.)
    plan.workflow_steps[0]["input"]["identifier"] = "A Completely Different Paper"

    findings = validate_resolver_evidence(plan, registry)

    assert [finding.code for finding in findings] == [
        "grounded_binding_unverified"
    ]
    assert findings[0].actual == "A Completely Different Paper"


def test_live_provider_contract_drift_invalidates_grounded_evidence() -> None:
    registry = _registry()
    plan = _workflow_plan(
        message="Fetch Attention Is All You Need.",
        capability="paper.fetch.arxiv",
        skill="arxiv-fetch",
        field="identifier",
        value="Attention Is All You Need",
    )
    assert seal_resolver_evidence(
        plan,
        registry,
        field_path="/workflow_steps/0/input/identifier",
        value="Attention Is All You Need",
        verification_mode="grounded_search",
        source="arxiv_id.search",
    )
    sealed_hash = plan.resolver_evidence[0]["contract_hash"]
    entry = registry.get_scoped("builtin", "arxiv-fetch")
    assert entry is not None
    original_schema = deepcopy(entry.input_schema)
    try:
        entry.input_schema = {
            **deepcopy(entry.input_schema),
            "x-contract-revision": 2,
        }
        findings = validate_resolver_evidence(plan, registry)
    finally:
        entry.input_schema = original_schema

    assert [finding.code for finding in findings] == [
        "grounded_binding_unverified"
    ]
    assert findings[0].provider_contract_hash != sealed_hash


def test_selected_provider_drift_invalidates_grounded_evidence() -> None:
    registry = _registry()
    registry.register(
        SkillEntry(
            name="arxiv-fetch",
            description="shadow arXiv provider",
            source="user_omni",
            kind=SkillKind.PYTHON_ENGINE,
            delivery_mode=DeliveryMode.ASYNC_TASK,
            role="support",
            capabilities=["paper.fetch.arxiv"],
            input_schema={
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "format": "arxiv_id",
                    }
                },
                "required": ["identifier"],
            },
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        )
    )
    plan = _workflow_plan(
        message="Fetch Attention Is All You Need.",
        capability="paper.fetch.arxiv",
        skill="arxiv-fetch",
        field="identifier",
        value="Attention Is All You Need",
    )
    assert seal_resolver_evidence(
        plan,
        registry,
        field_path="/workflow_steps/0/input/identifier",
        value="Attention Is All You Need",
        verification_mode="grounded_search",
        source="arxiv_id.search",
    )
    plan.workflow_steps[0]["skill_source"] = "user_omni"

    findings = validate_resolver_evidence(plan, registry)

    assert [finding.code for finding in findings] == [
        "grounded_binding_unverified"
    ]
    assert findings[0].provider_source == "user_omni"


def test_current_field_drift_invalidates_grounded_evidence() -> None:
    registry = _registry()
    registry.register(
        SkillEntry(
            name="dual-id-fetch",
            description="provider with two objective identifier slots",
            source="builtin",
            kind=SkillKind.PYTHON_ENGINE,
            delivery_mode=DeliveryMode.ASYNC_TASK,
            role="support",
            capabilities=["paper.fetch.dual"],
            input_schema={
                "type": "object",
                "properties": {
                    "primary": {"type": "string", "format": "arxiv_id"},
                    "secondary": {"type": "string", "format": "arxiv_id"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
            },
        )
    )
    plan = _workflow_plan(
        message="Fetch Attention Is All You Need.",
        capability="paper.fetch.dual",
        skill="dual-id-fetch",
        field="primary",
        value="Attention Is All You Need",
    )
    assert seal_resolver_evidence(
        plan,
        registry,
        field_path="/workflow_steps/0/input/primary",
        value="Attention Is All You Need",
        verification_mode="grounded_search",
        source="arxiv_id.search",
    )
    # Move the (non-canonical) title to a different objective slot: the sealed
    # evidence for ``primary`` cannot authorize ``secondary``, which is unproved.
    plan.workflow_steps[0]["input"] = {"secondary": "Attention Is All You Need"}

    findings = validate_resolver_evidence(plan, registry)

    assert [finding.field_path for finding in findings] == [
        "/workflow_steps/0/input/secondary"
    ]


def test_legacy_grounded_binding_is_opaque_and_cannot_authorize_execution() -> None:
    registry = _registry()
    plan = _workflow_plan(
        message="Fetch Attention Is All You Need.",
        capability="paper.fetch.arxiv",
        skill="arxiv-fetch",
        field="identifier",
        value="Attention Is All You Need",
    )
    plan.requested_constraints = [
        {
            "constraint_id": "legacy-resolver-fact",
            "semantic_key": "paper_id",
            "requested_value": "1706.03762",
            "source": "host_contract",
            "critical": True,
            "step_id": "resolve",
            "capability_instance": "paper.fetch.arxiv",
            "target_field": "identifier",
            "owner": "resolver",
            "verification_mode": "grounded_search",
            "evidence_verified": True,
        }
    ]
    plan.binding_records = [
        {
            "constraint_id": "legacy-resolver-fact",
            "field_path": "/workflow_steps/0/input/identifier",
            "value": "1706.03762",
            "owner": "resolver",
            "source": "arxiv_id.search",
            "verified": True,
            "verification_mode": "grounded_search",
            "step_id": "resolve",
            "capability_instance": "paper.fetch.arxiv",
        }
    ]
    legacy_payload = plan.to_dict()
    legacy_payload.pop("plan_schema_version")
    legacy_payload.pop("provider_bindings")
    legacy_payload.pop("resolver_evidence")
    legacy_plan = IntentPlan.from_dict(legacy_payload)
    accepted = create_revision(
        legacy_plan,
        revision=1,
        stage="accepted",
    ).plan
    accepted_hash = canonical_plan_hash(accepted)

    findings = validate_resolver_evidence(accepted, registry)

    assert [finding.code for finding in findings] == [
        "grounded_binding_unverified"
    ]
    assert accepted.resolver_evidence == []
    assert canonical_plan_hash(accepted) == accepted_hash


def test_v2_revision_cannot_authorize_fact_with_legacy_binding_arrays() -> None:
    registry = _registry()
    plan = _workflow_plan(
        message="Fetch Attention Is All You Need.",
        capability="paper.fetch.arxiv",
        skill="arxiv-fetch",
        field="identifier",
        value="Attention Is All You Need",
    )
    plan.requested_constraints = [
        {
            "constraint_id": "forged-legacy-fact",
            "owner": "resolver",
            "target_field": "identifier",
        }
    ]
    plan.binding_records = [
        {
            "constraint_id": "forged-legacy-fact",
            "owner": "resolver",
            "verified": True,
            "verification_mode": "grounded_search",
            "source": "arxiv_id.search",
            "field_path": "/workflow_steps/0/input/identifier",
            "value": "1706.03762",
            "step_id": "resolve",
            "capability_instance": "paper.fetch.arxiv",
        }
    ]
    # The title did not explicitly contain this id. A v2 revision that omits
    # ResolverEvidence must fail closed instead of treating deprecated arrays
    # as execution authority.
    accepted = create_revision(
        plan,
        revision=1,
        stage="accepted",
    ).plan
    accepted._resolver_evidence_present = False  # noqa: SLF001
    accepted.resolver_evidence = []

    findings = validate_resolver_evidence(accepted, registry)

    assert [finding.code for finding in findings] == [
        "grounded_binding_unverified"
    ]
    assert accepted.resolver_evidence


def test_resolver_evidence_round_trips_and_only_changes_new_plan_hashes() -> None:
    registry = _registry()
    plan = _workflow_plan(
        message="Fetch Attention Is All You Need.",
        capability="paper.fetch.arxiv",
        skill="arxiv-fetch",
        field="identifier",
        value="Attention Is All You Need",
    )
    legacy_payload = deepcopy(plan.to_dict())
    legacy_payload.pop("resolver_evidence")
    assert "resolver_evidence" not in legacy_payload
    legacy_hash = canonical_plan_hash(legacy_payload)

    seal_resolver_evidence(
        plan,
        registry,
        field_path="/workflow_steps/0/input/identifier",
        value="Attention Is All You Need",
        verification_mode="grounded_search",
        source="arxiv_id.search",
    )
    restored = IntentPlan.from_dict(plan.to_dict())

    assert restored.resolver_evidence == plan.resolver_evidence
    assert canonical_plan_hash(restored) != legacy_hash
