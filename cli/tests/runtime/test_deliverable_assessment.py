"""Provider-owned deliverable assessment parsing and aggregation."""

from __future__ import annotations

import json

from omni.runtime.deliverable_assessment import (
    apply_prompt_assessment_transport,
    bind_deliverable_assessment_identity,
    collect_deliverable_assessments,
    evaluate_deliverable_assessments,
    make_provider_binding_id,
    parse_deliverable_assessment,
    quality_retry_decision,
)


def _assessment(
    *,
    deliverable_id: str = "figure",
    provider_binding_id: str = "skill:scientific-figure:figure",
    check: str = "figure_matches_instruction",
    status: str = "passed",
) -> dict[str, object]:
    return {
        "schema": "omni.deliverable-assessment/v1",
        "deliverable_id": deliverable_id,
        "provider_binding_id": provider_binding_id,
        "provider": "scientific-figure",
        "contract_hash": "contract-hash-figure-v1",
        "step_id": deliverable_id,
        "feedback": "provider checked the effective output",
        "status": status,
        "retryable": False,
        "effective_inputs": {"figure_kind": "rag"},
        "criteria": [
            {
                "criterion_id": check,
                "status": status,
                "summary": "provider checked the effective output",
            }
        ],
        "evidence_refs": ["artifact://figure"],
    }


def test_parse_deliverable_assessment_preserves_effective_inputs_and_identity() -> None:
    parsed = parse_deliverable_assessment(_assessment())

    assert parsed is not None
    assert parsed.deliverable_id == "figure"
    assert parsed.provider_binding_id == "skill:scientific-figure:figure"
    assert parsed.effective_inputs == {"figure_kind": "rag"}
    assert parsed.criteria[0].criterion_id == "figure_matches_instruction"


def test_parse_deliverable_assessment_rejects_incomplete_or_unknown_status() -> None:
    missing_binding = _assessment()
    missing_binding["provider_binding_id"] = ""
    missing_contract = _assessment()
    missing_contract["contract_hash"] = ""
    unknown = _assessment(status="maybe")

    assert parse_deliverable_assessment(missing_binding) is None
    assert parse_deliverable_assessment(missing_contract) is None
    assert parse_deliverable_assessment(unknown) is None


def test_collect_deliverable_assessments_reads_only_explicit_result_contract() -> None:
    valid = _assessment()
    records = collect_deliverable_assessments(
        [
            {"deliverable_assessment": valid},
            {"deliverable_assessments": [_assessment(deliverable_id="draft")]},
            {"metadata": {"deliverable_assessment": _assessment(deliverable_id="incidental")}},
        ]
    )

    assert [item.deliverable_id for item in records] == ["figure", "draft"]


def test_collection_preserves_assessments_with_distinct_exact_identity() -> None:
    first = _assessment(provider_binding_id="shared-binding")
    first["step_id"] = "figure-a"
    first["contract_hash"] = "hash-a"
    second = _assessment(provider_binding_id="shared-binding")
    second["step_id"] = "figure-b"
    second["contract_hash"] = "hash-b"

    records = collect_deliverable_assessments(
        [
            {"deliverable_assessment": first},
            {"deliverable_assessment": second},
        ]
    )

    assert [(item.step_id, item.contract_hash) for item in records] == [
        ("figure-a", "hash-a"),
        ("figure-b", "hash-b"),
    ]


def test_prompt_assessment_transport_extracts_explicit_fenced_envelope() -> None:
    assessment = _assessment(status="degraded")
    text = (
        "# Review\n\nUsable with limitations.\n\n"
        "```json\n"
        + json.dumps(
            {"deliverable_assessment": assessment},
            ensure_ascii=False,
        )
        + "\n```"
    )

    result = apply_prompt_assessment_transport(
        {"status": "ok", "text": text},
        final_text=text,
        quality_contract={
            "assessment_required": True,
            "checks": ["figure_matches_instruction"],
        },
        provider="prompt-review",
        fallback_identity={},
    )

    assert result["deliverable_assessment"] == assessment
    parsed = parse_deliverable_assessment(result["deliverable_assessment"])
    assert parsed is not None
    assert parsed.status == "degraded"
    assert parsed.evidence_refs == ("artifact://figure",)


def test_prompt_assessment_transport_is_strict_without_missing_opt_in() -> None:
    result = apply_prompt_assessment_transport(
        {"status": "ok", "text": "A review without an assessment."},
        final_text="A review without an assessment.",
        quality_contract={
            "assessment_required": True,
            "checks": ["review_grounded"],
        },
        provider="prompt-review",
        fallback_identity={},
    )

    assert "deliverable_assessment" not in result


def test_prompt_assessment_transport_materializes_only_declared_unknown() -> None:
    result = apply_prompt_assessment_transport(
        {"status": "ok", "text": "A review without an assessment."},
        final_text="A review without an assessment.",
        quality_contract={
            "assessment_required": True,
            "assessment_schema": "omni.deliverable-assessment/v1",
            "checks": ["review_grounded", "review_complete"],
            "missing_assessment_status": "unknown",
        },
        provider="prompt-review",
        fallback_identity={
            "deliverable_id": "review",
            "provider_binding_id": "skill:prompt-review:review",
            "contract_hash": "contract-hash",
            "step_id": "review-step",
        },
    )

    assessment = result["deliverable_assessment"]
    parsed = parse_deliverable_assessment(assessment)
    assert parsed is not None
    assert parsed.status == "unknown"
    assert parsed.retryable is False
    assert assessment["assessment_origin"] == "host_missing_provider_assessment"
    assert assessment["evidence_refs"] == []
    assert [item["criterion_id"] for item in assessment["criteria"]] == [
        "review_grounded",
        "review_complete",
    ]
    assert {item["status"] for item in assessment["criteria"]} == {"unknown"}
    assert all(item["evidence_refs"] == [] for item in assessment["criteria"])
    assert "did not emit a parseable" in assessment["feedback"]


def test_missing_assessment_for_requested_check_fails_closed() -> None:
    outcome = evaluate_deliverable_assessments(
        ["figure_matches_instruction"],
        [],
        task_contract={},
    )

    assert outcome.failures == ("figure_matches_instruction",)
    assert outcome.details[0]["reason"] == "missing_assessment"


def test_structured_contract_matches_deliverable_and_provider_binding() -> None:
    expected_binding = make_provider_binding_id(
        provider_type="skill",
        provider_name="scientific-figure",
        deliverable_id="figure",
    )
    task_contract = {
        "deliverables": [
            {
                "deliverable_id": "figure",
                "provider_binding_id": expected_binding,
                "required_checks": ["figure_matches_instruction"],
            }
        ]
    }

    wrong_provider = evaluate_deliverable_assessments(
        ["figure_matches_instruction"],
        [
            parse_deliverable_assessment(
                _assessment(provider_binding_id="skill:other-provider:figure")
            )
        ],
        task_contract=task_contract,
    )
    matching = evaluate_deliverable_assessments(
        ["figure_matches_instruction"],
        [
            parse_deliverable_assessment(
                _assessment(provider_binding_id=expected_binding)
            )
        ],
        task_contract=task_contract,
    )

    assert wrong_provider.failures == ("figure_matches_instruction",)
    assert wrong_provider.details[0]["reason"] == "missing_assessment"
    assert matching.failures == ()
    assert matching.degraded == ()


def test_structured_contract_rejects_contract_hash_or_step_drift() -> None:
    task_contract = {
        "deliverables": [
            {
                "id": "figure-step",
                "consumer_step_id": "figure-step",
                "provider_binding_id": "provider-binding-exact",
                "provider_contract_hash": "contract-hash-exact",
                "required_checks": ["figure_matches_instruction"],
            }
        ]
    }
    payload = _assessment(
        deliverable_id="figure-step",
        provider_binding_id="provider-binding-exact",
    )
    payload["contract_hash"] = "contract-hash-drifted"
    payload["step_id"] = "figure-step"

    outcome = evaluate_deliverable_assessments(
        ["figure_matches_instruction"],
        [parse_deliverable_assessment(payload)],
        task_contract=task_contract,
    )

    assert outcome.failures == ("figure_matches_instruction",)
    assert outcome.details[0]["reason"] == "missing_assessment"


def test_host_binds_provider_judgement_to_exact_execution_identity() -> None:
    result = {"deliverable_assessment": _assessment()}
    bind_deliverable_assessment_identity(
        result,
        {
            "id": "figure-step",
            "deliverable_id": "figure-step",
            "skill_name": "scientific-figure",
            "provider_binding_id": "provider-binding-exact",
            "provider_contract_hash": "contract-hash-exact",
        },
    )

    parsed = parse_deliverable_assessment(result["deliverable_assessment"])
    assert parsed is not None
    assert parsed.deliverable_id == "figure-step"
    assert parsed.step_id == "figure-step"
    assert parsed.provider_binding_id == "provider-binding-exact"
    assert parsed.contract_hash == "contract-hash-exact"


def test_v2_contract_does_not_fall_back_to_same_named_criterion() -> None:
    task_contract = {
        "schema_version": 2,
        "deliverables": [
            {
                "id": "draft.section",
                "kind": "draft.section",
                "required": True,
                "acceptance": ["draft_content_present"],
            }
        ],
    }
    matching = parse_deliverable_assessment(
        _assessment(
            deliverable_id="draft.section",
            provider_binding_id="native_executor:synthesis.final:draft.section",
            check="draft_content_present",
        )
    )
    wrong_deliverable = parse_deliverable_assessment(
        _assessment(
            deliverable_id="draft.manuscript",
            provider_binding_id="native_executor:synthesis.final:draft.manuscript",
            check="draft_content_present",
        )
    )

    matching_name_only = evaluate_deliverable_assessments(
        ["draft_content_present"],
        [matching],
        task_contract=task_contract,
    )
    failed = evaluate_deliverable_assessments(
        ["draft_content_present"],
        [wrong_deliverable],
        task_contract=task_contract,
    )

    assert matching_name_only.failures == ("draft_content_present",)
    assert matching_name_only.details[0]["reason"] == "missing_exact_obligation"
    assert failed.failures == ("draft_content_present",)
    assert failed.details[0]["reason"] == "missing_exact_obligation"


def test_v2_duplicate_deliverable_obligations_require_each_exact_provider() -> None:
    task_contract = {
        "schema_version": 2,
        "deliverables": [
            {
                "id": "shared-figure",
                "consumer_step_id": "figure-a",
                "provider_binding_id": "binding-a",
                "provider_contract_hash": "hash-a",
                "required": True,
                "required_checks": ["figure_quality"],
            },
            {
                "id": "shared-figure",
                "consumer_step_id": "figure-b",
                "provider_binding_id": "binding-b",
                "provider_contract_hash": "hash-b",
                "required": True,
                "required_checks": ["figure_quality"],
            },
        ],
    }
    provider_a = _assessment(
        deliverable_id="shared-figure",
        provider_binding_id="binding-a",
        check="figure_quality",
    )
    provider_a["contract_hash"] = "hash-a"
    provider_a["step_id"] = "figure-a"
    wrong_provider_for_b = _assessment(
        deliverable_id="shared-figure",
        provider_binding_id="binding-a",
        check="figure_quality",
    )
    wrong_provider_for_b["contract_hash"] = "hash-b"
    wrong_provider_for_b["step_id"] = "figure-b"
    provider_b = _assessment(
        deliverable_id="shared-figure",
        provider_binding_id="binding-b",
        check="figure_quality",
    )
    provider_b["contract_hash"] = "hash-b"
    provider_b["step_id"] = "figure-b"

    outcome = evaluate_deliverable_assessments(
        ["figure_quality"],
        [
            parse_deliverable_assessment(provider_a),
            parse_deliverable_assessment(wrong_provider_for_b),
        ],
        task_contract=task_contract,
    )
    complete = evaluate_deliverable_assessments(
        ["figure_quality"],
        [
            parse_deliverable_assessment(provider_a),
            parse_deliverable_assessment(provider_b),
        ],
        task_contract=task_contract,
    )

    assert outcome.failures == ("figure_quality",)
    assert [
        (item["step_id"], item["provider_binding_id"], item["reason"])
        for item in outcome.details
    ] == [
        ("figure-a", "binding-a", "provider_assessment"),
        ("figure-b", "binding-b", "missing_assessment"),
    ]
    assert complete.failures == ()


def test_criterion_status_is_aggregated_without_trusting_top_level_success() -> None:
    payload = _assessment()
    payload["status"] = "passed"
    payload["criteria"] = [
        {
            "criterion_id": "figure_matches_instruction",
            "status": "degraded",
            "summary": "ambiguous domain template",
        }
    ]
    parsed = parse_deliverable_assessment(payload)

    outcome = evaluate_deliverable_assessments(
        ["figure_matches_instruction"],
        [parsed],
        task_contract={},
    )

    assert outcome.failures == ()
    assert outcome.degraded == ("figure_matches_instruction",)


def test_quality_retry_requires_provider_authority_and_retryable_assessment() -> None:
    payload = _assessment(status="degraded")
    payload["retryable"] = True
    parsed = parse_deliverable_assessment(payload)
    assert parsed is not None

    unsafe = quality_retry_decision(
        parsed,
        provider_replay_safe=False,
        prior_quality_retries=0,
    )
    safe = quality_retry_decision(
        parsed,
        provider_replay_safe=True,
        prior_quality_retries=0,
    )

    assert unsafe.allowed is False
    assert unsafe.reason == "provider_not_replay_safe"
    assert safe.allowed is True
    assert safe.reason == "quality_retry_admitted"


def test_quality_retry_is_single_attempt_and_rejects_unprotected_side_effects() -> None:
    payload = _assessment(status="failed")
    payload["retryable"] = True
    parsed = parse_deliverable_assessment(payload)
    assert parsed is not None

    exhausted = quality_retry_decision(
        parsed,
        provider_replay_safe=True,
        prior_quality_retries=1,
    )
    side_effecting = quality_retry_decision(
        parsed,
        provider_replay_safe=True,
        prior_quality_retries=0,
        committed_side_effects=("artifact://first-attempt",),
    )
    idempotent = quality_retry_decision(
        parsed,
        provider_replay_safe=True,
        prior_quality_retries=0,
        committed_side_effects=("artifact://first-attempt",),
        idempotency_key="quality-retry:workflow:figure",
    )
    declared_side_effects_without_key = quality_retry_decision(
        parsed,
        provider_replay_safe=True,
        prior_quality_retries=0,
        idempotency_required=True,
    )
    declared_side_effects_with_key = quality_retry_decision(
        parsed,
        provider_replay_safe=True,
        prior_quality_retries=0,
        idempotency_required=True,
        idempotency_key="quality-retry:workflow:figure",
    )

    assert exhausted.allowed is False
    assert exhausted.reason == "quality_retry_budget_exhausted"
    assert side_effecting.allowed is False
    assert side_effecting.reason == "unprotected_side_effects"
    assert idempotent.allowed is True
    assert declared_side_effects_without_key.allowed is False
    assert declared_side_effects_without_key.reason == "idempotency_key_required"
    assert declared_side_effects_with_key.allowed is True
