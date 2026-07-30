"""Offline release thresholds for reactive binding and steer races."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from omni.eval.reactive_binding_gate import (
    BindingCaseEvidence,
    ModelRepairBudget,
    ReactiveBindingCriteria,
    ReactiveBindingMetrics,
    ReactiveBindingProvenance,
    aggregate_reactive_binding_metrics,
    evaluate_reactive_binding_release,
    initialize_fail_closed_report,
    write_release_gate_report,
)

_BUDGET = ModelRepairBudget(
    max_call_rate=0.05,
    max_cost_per_case_usd=0.001,
)
_REPO_ROOT = Path(__file__).resolve().parents[3]


_PROVENANCE = ReactiveBindingProvenance(
    candidate_ref="candidate-sha",
    baseline_ref="previous-release",
    benchmark_id="production-plan-pipeline-v1",
    corpus_sha256="a" * 64,
    proposal_sha256="b" * 64,
    prompt_sha256="c" * 64,
    catalog_sha256="d" * 64,
    contract_sha256="e" * 64,
    platform="test-platform",
    python="3.12.test",
    benchmark_samples=100,
    benchmark_warmups=5,
)


def _passing_metrics() -> ReactiveBindingMetrics:
    return ReactiveBindingMetrics(
        total_cases=134,
        accuracy_cases=100,
        first_pass_valid_cases=95,
        post_repair_valid_cases=99,
        silent_critical_mismatches=0,
        model_repair_calls=5,
        model_repair_cost_usd=0.10,
        repair_limit_violations=0,
        rejected_patches=2,
        recorded_rejected_patches=2,
        steer_trials=10_000,
        steer_losses=0,
        detector_negative_cases=20,
        detector_false_positives=0,
        healthy_model_repair_calls=0,
        resolver_owned_unverified_executions=0,
        planning_latency_p95_ms=105.0,
        baseline_planning_latency_p95_ms=100.0,
        authoritative_plan_checks=1,
        non_replay_safe_execution_cases=1,
        normal_mode_self_heal_cases=1,
        resolver_owned_cases=1,
        resolver_constraint_omission_cases=1,
        resolver_constraint_omission_escapes=0,
        forged_constraint_shadowing_cases=1,
        forged_constraint_shadowing_escapes=0,
        multi_consumer_binding_cases=1,
        multi_consumer_binding_isolation_violations=0,
        model_repair_prompt_disclosure_cases=1,
        model_repair_prompt_disclosure_violations=0,
        malformed_output_schema_preflight_cases=1,
        malformed_output_schema_execution_escapes=0,
        constraint_evidence_integrity_cases=2,
        constraint_evidence_integrity_errors=0,
        provider_authority_integrity_cases=4,
        provider_authority_integrity_errors=0,
        provider_runtime_closure_cases=5,
        provider_runtime_closure_errors=0,
        provider_dispatch_authority_cases=2,
        provider_dispatch_authority_errors=0,
        recovery_authority_continuity_cases=3,
        recovery_authority_continuity_errors=0,
        durable_retry_idempotency_cases=3,
        durable_retry_idempotency_errors=0,
        generic_sensitive_approval_cases=2,
        generic_sensitive_approval_bypasses=0,
        run_skill_output_projection_cases=2,
        run_skill_output_false_successes=0,
        native_synthesis_output_projection_cases=1,
        native_synthesis_output_false_successes=0,
        forged_provider_rejection_cases=2,
        forged_provider_rejection_false_rejections=0,
        durable_subtask_domain_failure_cases=1,
        durable_subtask_domain_failure_false_successes=0,
        failing_turn_fallback_cases=2,
        failing_turn_fallback_losses=0,
        failing_turn_fallback_duplicates=0,
        deterministic_workflow_steer_cases=1,
        deterministic_workflow_false_applied=0,
        deterministic_detached_steer_cases=1,
        deterministic_detached_steer_rejection_failures=0,
        deterministic_detached_steer_orphan_controls=0,
        steering_seal_cases=1,
        steering_seal_reopen_violations=0,
        steering_seal_orphan_controls=0,
        gateway_frame_revocation_cases=1,
        gateway_frame_revocation_escapes=0,
        gateway_delegation_lease_cases=1,
        gateway_delegation_lease_violations=0,
        foreground_ack_fallback_cases=1,
        foreground_ack_retry_violations=0,
        foreground_ack_losses=0,
        foreground_ack_duplicates=0,
        durable_control_requeue_cases=1,
        durable_control_requeue_violations=0,
        durable_control_live_owner_cases=1,
        durable_control_live_owner_violations=0,
        durable_control_crash_recovery_cases=1,
        durable_control_crash_recovery_losses=0,
        durable_control_crash_recovery_duplicates=0,
        durable_control_legacy_stale_cases=1,
        durable_control_legacy_stale_violations=0,
    )


def test_release_gate_accepts_all_inclusive_quality_boundaries() -> None:
    report = evaluate_reactive_binding_release(
        _passing_metrics(),
        repair_budget=_BUDGET,
        provenance=_PROVENANCE,
    )

    assert report.passed
    assert report.failed_checks == ()
    assert report.metrics.first_pass_validity == 0.95
    assert report.metrics.post_repair_validity == 0.99
    assert report.metrics.model_repair_call_rate == 0.05
    assert report.metrics.model_repair_cost_per_case_usd == 0.001


def test_release_report_is_stable_json_for_ci_artifacts() -> None:
    report = evaluate_reactive_binding_release(
        _passing_metrics(),
        repair_budget=_BUDGET,
        provenance=_PROVENANCE,
    )

    payload = json.loads(json.dumps(report.to_dict()))

    assert payload["schema"] == "omni.reactive-binding-release-gate.v1"
    assert payload["passed"] is True
    assert payload["metrics"]["steer_trials"] == 10_000
    assert payload["repair_budget"]["max_call_rate"] == 0.05
    assert payload["provenance"]["benchmark_id"] == "production-plan-pipeline-v1"
    assert payload["provenance"]["corpus_sha256"] == "a" * 64
    assert {check["name"] for check in payload["checks"]} >= {
        "first_pass_validity",
        "post_repair_validity",
        "model_repair_call_rate",
        "model_repair_cost_per_case_usd",
        "rejected_patch_recording_rate",
        "steer_losses",
        "resolver_constraint_omission_escapes",
        "forged_constraint_shadowing_escapes",
        "multi_consumer_binding_isolation_violations",
        "model_repair_prompt_disclosure_violations",
        "malformed_output_schema_execution_escapes",
        "constraint_evidence_integrity_errors",
        "provider_authority_integrity_errors",
        "provider_runtime_closure_errors",
        "provider_dispatch_authority_errors",
        "recovery_authority_continuity_errors",
        "durable_retry_idempotency_errors",
        "generic_sensitive_approval_bypasses",
        "run_skill_output_false_successes",
        "native_synthesis_output_false_successes",
        "forged_provider_rejection_false_rejections",
        "durable_subtask_domain_failure_false_successes",
        "failing_turn_fallback_losses",
        "deterministic_workflow_false_applied",
        "deterministic_detached_steer_rejection_failures",
        "deterministic_detached_steer_orphan_controls",
        "steering_seal_reopen_violations",
        "steering_seal_orphan_controls",
        "gateway_frame_revocation_escapes",
        "gateway_delegation_lease_violations",
        "foreground_ack_retry_violations",
        "foreground_ack_losses",
        "foreground_ack_duplicates",
        "durable_control_requeue_violations",
        "durable_control_live_owner_violations",
        "durable_control_crash_recovery_losses",
        "durable_control_crash_recovery_duplicates",
        "durable_control_legacy_stale_violations",
    }


def test_gate_reports_every_release_invariant_that_regressed() -> None:
    metrics = ReactiveBindingMetrics(
        total_cases=134,
        accuracy_cases=100,
        first_pass_valid_cases=94,
        post_repair_valid_cases=98,
        silent_critical_mismatches=1,
        model_repair_calls=11,
        model_repair_cost_usd=2.0,
        repair_limit_violations=1,
        rejected_patches=2,
        recorded_rejected_patches=1,
        steer_trials=9_999,
        steer_losses=1,
        steer_duplicates=1,
        authoritative_plan_mismatches=1,
        non_replay_safe_duplicate_executions=1,
        normal_mode_self_heal_warnings=1,
        detector_negative_cases=20,
        detector_false_positives=1,
        healthy_model_repair_calls=1,
        resolver_owned_unverified_executions=1,
        planning_latency_p95_ms=111.0,
        baseline_planning_latency_p95_ms=100.0,
        authoritative_plan_checks=1,
        non_replay_safe_execution_cases=1,
        normal_mode_self_heal_cases=1,
        resolver_owned_cases=1,
        resolver_constraint_omission_cases=1,
        resolver_constraint_omission_escapes=1,
        forged_constraint_shadowing_cases=1,
        forged_constraint_shadowing_escapes=1,
        multi_consumer_binding_cases=1,
        multi_consumer_binding_isolation_violations=1,
        model_repair_prompt_disclosure_cases=1,
        model_repair_prompt_disclosure_violations=1,
        malformed_output_schema_preflight_cases=1,
        malformed_output_schema_execution_escapes=1,
        constraint_evidence_integrity_cases=2,
        constraint_evidence_integrity_errors=1,
        provider_authority_integrity_cases=4,
        provider_authority_integrity_errors=1,
        provider_runtime_closure_cases=5,
        provider_runtime_closure_errors=1,
        provider_dispatch_authority_cases=2,
        provider_dispatch_authority_errors=1,
        recovery_authority_continuity_cases=3,
        recovery_authority_continuity_errors=1,
        durable_retry_idempotency_cases=3,
        durable_retry_idempotency_errors=1,
        generic_sensitive_approval_cases=2,
        generic_sensitive_approval_bypasses=1,
        run_skill_output_projection_cases=2,
        run_skill_output_false_successes=1,
        native_synthesis_output_projection_cases=1,
        native_synthesis_output_false_successes=1,
        forged_provider_rejection_cases=2,
        forged_provider_rejection_false_rejections=1,
        durable_subtask_domain_failure_cases=1,
        durable_subtask_domain_failure_false_successes=1,
        failing_turn_fallback_cases=2,
        failing_turn_fallback_losses=1,
        failing_turn_fallback_duplicates=1,
        deterministic_workflow_steer_cases=1,
        deterministic_workflow_false_applied=1,
        deterministic_detached_steer_cases=1,
        deterministic_detached_steer_rejection_failures=1,
        deterministic_detached_steer_orphan_controls=1,
        steering_seal_cases=1,
        steering_seal_reopen_violations=1,
        steering_seal_orphan_controls=1,
        gateway_frame_revocation_cases=1,
        gateway_frame_revocation_escapes=1,
        gateway_delegation_lease_cases=1,
        gateway_delegation_lease_violations=1,
        foreground_ack_fallback_cases=1,
        foreground_ack_retry_violations=1,
        foreground_ack_losses=1,
        foreground_ack_duplicates=1,
        durable_control_requeue_cases=1,
        durable_control_requeue_violations=1,
        durable_control_live_owner_cases=1,
        durable_control_live_owner_violations=1,
        durable_control_crash_recovery_cases=1,
        durable_control_crash_recovery_losses=1,
        durable_control_crash_recovery_duplicates=1,
        durable_control_legacy_stale_cases=1,
        durable_control_legacy_stale_violations=1,
    )

    report = evaluate_reactive_binding_release(
        metrics,
        repair_budget=ModelRepairBudget(
            max_call_rate=0.10,
            max_cost_per_case_usd=0.01,
        ),
        provenance=_PROVENANCE,
    )

    assert not report.passed
    assert set(report.failed_checks) == {
        "first_pass_validity",
        "post_repair_validity",
        "silent_critical_mismatches",
        "repair_limit_violations",
        "model_repair_call_rate",
        "model_repair_cost_per_case_usd",
        "rejected_patch_recording_rate",
        "steer_trials",
        "steer_losses",
        "steer_duplicates",
        "authoritative_plan_mismatches",
        "non_replay_safe_duplicate_executions",
        "normal_mode_self_heal_warnings",
        "detector_false_positive_rate",
        "healthy_model_repair_calls",
        "resolver_owned_unverified_executions",
        "resolver_constraint_omission_escapes",
        "forged_constraint_shadowing_escapes",
        "multi_consumer_binding_isolation_violations",
        "model_repair_prompt_disclosure_violations",
        "malformed_output_schema_execution_escapes",
        "constraint_evidence_integrity_errors",
        "provider_authority_integrity_errors",
        "provider_runtime_closure_errors",
        "provider_dispatch_authority_errors",
        "recovery_authority_continuity_errors",
        "durable_retry_idempotency_errors",
        "generic_sensitive_approval_bypasses",
        "run_skill_output_false_successes",
        "native_synthesis_output_false_successes",
        "forged_provider_rejection_false_rejections",
        "durable_subtask_domain_failure_false_successes",
        "failing_turn_fallback_losses",
        "failing_turn_fallback_duplicates",
        "deterministic_workflow_false_applied",
        "deterministic_detached_steer_rejection_failures",
        "deterministic_detached_steer_orphan_controls",
        "steering_seal_reopen_violations",
        "steering_seal_orphan_controls",
        "gateway_frame_revocation_escapes",
        "gateway_delegation_lease_violations",
        "foreground_ack_retry_violations",
        "foreground_ack_losses",
        "foreground_ack_duplicates",
        "durable_control_requeue_violations",
        "durable_control_live_owner_violations",
        "durable_control_crash_recovery_losses",
        "durable_control_crash_recovery_duplicates",
        "durable_control_legacy_stale_violations",
        "planning_latency_regression",
    }


def test_rejected_patch_audit_must_be_exercised_not_vacuously_green() -> None:
    metrics = replace(
        _passing_metrics(),
        rejected_patches=0,
        recorded_rejected_patches=0,
    )

    report = evaluate_reactive_binding_release(
        metrics,
        repair_budget=_BUDGET,
        provenance=_PROVENANCE,
    )

    assert report.failed_checks == ("rejected_patch_cases",)


def test_zero_error_counts_require_non_vacuous_execution_evidence() -> None:
    metrics = replace(
        _passing_metrics(),
        authoritative_plan_checks=0,
        non_replay_safe_execution_cases=0,
        normal_mode_self_heal_cases=0,
        resolver_owned_cases=0,
        resolver_constraint_omission_cases=0,
        forged_constraint_shadowing_cases=0,
        multi_consumer_binding_cases=0,
        model_repair_prompt_disclosure_cases=0,
        malformed_output_schema_preflight_cases=0,
        constraint_evidence_integrity_cases=0,
        provider_authority_integrity_cases=0,
        provider_runtime_closure_cases=0,
        provider_dispatch_authority_cases=0,
        recovery_authority_continuity_cases=0,
        durable_retry_idempotency_cases=0,
        generic_sensitive_approval_cases=0,
        run_skill_output_projection_cases=0,
        native_synthesis_output_projection_cases=0,
        forged_provider_rejection_cases=0,
        durable_subtask_domain_failure_cases=0,
        failing_turn_fallback_cases=0,
        deterministic_workflow_steer_cases=0,
        deterministic_detached_steer_cases=0,
        steering_seal_cases=0,
        gateway_frame_revocation_cases=0,
        gateway_delegation_lease_cases=0,
        foreground_ack_fallback_cases=0,
        durable_control_requeue_cases=0,
        durable_control_live_owner_cases=0,
        durable_control_crash_recovery_cases=0,
        durable_control_legacy_stale_cases=0,
    )

    report = evaluate_reactive_binding_release(
        metrics,
        repair_budget=_BUDGET,
        provenance=_PROVENANCE,
    )

    assert set(report.failed_checks) == {
        "authoritative_plan_checks",
        "non_replay_safe_execution_cases",
        "normal_mode_self_heal_cases",
        "resolver_owned_cases",
        "resolver_constraint_omission_cases",
        "forged_constraint_shadowing_cases",
        "multi_consumer_binding_cases",
        "model_repair_prompt_disclosure_cases",
        "malformed_output_schema_preflight_cases",
        "constraint_evidence_integrity_cases",
        "provider_authority_integrity_cases",
        "provider_runtime_closure_cases",
        "provider_dispatch_authority_cases",
        "recovery_authority_continuity_cases",
        "durable_retry_idempotency_cases",
        "generic_sensitive_approval_cases",
        "run_skill_output_projection_cases",
        "native_synthesis_output_projection_cases",
        "forged_provider_rejection_cases",
        "durable_subtask_domain_failure_cases",
        "failing_turn_fallback_cases",
        "deterministic_workflow_steer_cases",
        "deterministic_detached_steer_cases",
        "steering_seal_cases",
        "gateway_frame_revocation_cases",
        "gateway_delegation_lease_cases",
        "foreground_ack_fallback_cases",
        "durable_control_requeue_cases",
        "durable_control_live_owner_cases",
        "durable_control_crash_recovery_cases",
        "durable_control_legacy_stale_cases",
    }


def test_repair_call_and_cost_budgets_are_independently_enforced() -> None:
    report = evaluate_reactive_binding_release(
        _passing_metrics(),
        repair_budget=ModelRepairBudget(
            max_call_rate=0.049,
            max_cost_per_case_usd=0.0009,
        ),
        provenance=_PROVENANCE,
    )

    assert report.failed_checks == (
        "model_repair_call_rate",
        "model_repair_cost_per_case_usd",
    )


def test_aggregate_counts_case_cost_audit_and_10k_race_evidence() -> None:
    cases = [
        BindingCaseEvidence(
            first_pass_valid=index < 95,
            post_repair_valid=index < 99,
            model_repair_calls=1 if 95 <= index < 99 else 0,
            model_repair_cost_usd=0.0005 if 95 <= index < 99 else 0.0,
            rejected_patches=1 if index == 99 else 0,
            recorded_rejected_patches=1 if index == 99 else 0,
            detector_negative=index < 20,
            include_in_accuracy=True,
            authoritative_plan_checks=int(index == 0),
            non_replay_safe_execution_cases=int(index == 0),
            normal_mode_self_heal_cases=int(index == 0),
            resolver_owned_cases=int(index == 0),
            resolver_constraint_omission_cases=int(index == 0),
            forged_constraint_shadowing_cases=int(index == 0),
            multi_consumer_binding_cases=int(index == 0),
            model_repair_prompt_disclosure_cases=int(index == 0),
            malformed_output_schema_preflight_cases=int(index == 0),
            constraint_evidence_integrity_cases=2 * int(index == 0),
            provider_authority_integrity_cases=4 * int(index == 0),
            provider_runtime_closure_cases=5 * int(index == 0),
            provider_dispatch_authority_cases=2 * int(index == 0),
            recovery_authority_continuity_cases=3 * int(index == 0),
            durable_retry_idempotency_cases=3 * int(index == 0),
            generic_sensitive_approval_cases=2 * int(index == 0),
            run_skill_output_projection_cases=2 * int(index == 0),
            native_synthesis_output_projection_cases=int(index == 0),
            forged_provider_rejection_cases=2 * int(index == 0),
            durable_subtask_domain_failure_cases=int(index == 0),
            failing_turn_fallback_cases=2 * int(index == 0),
            deterministic_workflow_steer_cases=int(index == 0),
            deterministic_detached_steer_cases=int(index == 0),
            steering_seal_cases=int(index == 0),
            gateway_frame_revocation_cases=int(index == 0),
            gateway_delegation_lease_cases=int(index == 0),
            foreground_ack_fallback_cases=int(index == 0),
            durable_control_requeue_cases=int(index == 0),
            durable_control_live_owner_cases=int(index == 0),
            durable_control_crash_recovery_cases=int(index == 0),
            durable_control_legacy_stale_cases=int(index == 0),
        )
        for index in range(100)
    ]

    metrics = aggregate_reactive_binding_metrics(
        cases,
        steer_trials=10_000,
        steer_losses=0,
        baseline_planning_latency_p95_ms=100.0,
        planning_latency_p95_ms=105.0,
    )
    report = evaluate_reactive_binding_release(
        metrics,
        repair_budget=_BUDGET,
        provenance=_PROVENANCE,
    )

    assert metrics.accuracy_cases == 100
    assert metrics.first_pass_valid_cases == 95
    assert metrics.post_repair_valid_cases == 99
    assert metrics.model_repair_calls == 4
    assert metrics.model_repair_cost_usd == pytest.approx(0.002)
    assert metrics.rejected_patch_recording_rate == 1.0
    assert report.passed


def test_per_case_double_repair_is_detected_even_when_average_rate_is_low() -> None:
    cases = [
        BindingCaseEvidence(
            first_pass_valid=index != 0,
            post_repair_valid=True,
            model_repair_calls=2 if index == 0 else 0,
            detector_negative=index < 20,
        )
        for index in range(100)
    ]
    cases[-1] = BindingCaseEvidence(
        True,
        True,
        rejected_patches=1,
        recorded_rejected_patches=1,
        authoritative_plan_checks=1,
        non_replay_safe_execution_cases=1,
        normal_mode_self_heal_cases=1,
        resolver_owned_cases=1,
        resolver_constraint_omission_cases=1,
        forged_constraint_shadowing_cases=1,
        multi_consumer_binding_cases=1,
        model_repair_prompt_disclosure_cases=1,
        malformed_output_schema_preflight_cases=1,
        constraint_evidence_integrity_cases=2,
        provider_authority_integrity_cases=4,
        provider_runtime_closure_cases=5,
        provider_dispatch_authority_cases=2,
        recovery_authority_continuity_cases=3,
        durable_retry_idempotency_cases=3,
        generic_sensitive_approval_cases=2,
        run_skill_output_projection_cases=2,
        native_synthesis_output_projection_cases=1,
        forged_provider_rejection_cases=2,
        durable_subtask_domain_failure_cases=1,
        failing_turn_fallback_cases=2,
        deterministic_workflow_steer_cases=1,
        deterministic_detached_steer_cases=1,
        steering_seal_cases=1,
        gateway_frame_revocation_cases=1,
        gateway_delegation_lease_cases=1,
        foreground_ack_fallback_cases=1,
        durable_control_requeue_cases=1,
        durable_control_live_owner_cases=1,
        durable_control_crash_recovery_cases=1,
        durable_control_legacy_stale_cases=1,
    )
    metrics = aggregate_reactive_binding_metrics(
        cases,
        steer_trials=10_000,
        steer_losses=0,
        baseline_planning_latency_p95_ms=100.0,
        planning_latency_p95_ms=100.0,
    )

    report = evaluate_reactive_binding_release(
        metrics,
        repair_budget=ModelRepairBudget(
            max_call_rate=0.05,
            max_cost_per_case_usd=0.001,
        ),
        provenance=_PROVENANCE,
    )

    assert metrics.model_repair_call_rate == 0.02
    assert report.failed_checks == ("repair_limit_violations",)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"total_cases": -1},
        {"first_pass_valid_cases": 101},
        {"post_repair_valid_cases": 101},
        {"model_repair_cost_usd": float("inf")},
        {"steer_losses": 10_001},
        {
            "generic_sensitive_approval_cases": 0,
            "generic_sensitive_approval_bypasses": 1,
        },
        {
            "native_synthesis_output_projection_cases": 0,
            "native_synthesis_output_false_successes": 1,
        },
        {
            "forged_constraint_shadowing_cases": 0,
            "forged_constraint_shadowing_escapes": 1,
        },
        {
            "multi_consumer_binding_cases": 0,
            "multi_consumer_binding_isolation_violations": 1,
        },
        {
            "model_repair_prompt_disclosure_cases": 0,
            "model_repair_prompt_disclosure_violations": 1,
        },
        {
            "malformed_output_schema_preflight_cases": 0,
            "malformed_output_schema_execution_escapes": 1,
        },
        {
            "constraint_evidence_integrity_cases": 0,
            "constraint_evidence_integrity_errors": 1,
        },
        {
            "provider_authority_integrity_cases": 0,
            "provider_authority_integrity_errors": 1,
        },
        {
            "provider_runtime_closure_cases": 0,
            "provider_runtime_closure_errors": 1,
        },
        {
            "provider_dispatch_authority_cases": 0,
            "provider_dispatch_authority_errors": 1,
        },
        {
            "recovery_authority_continuity_cases": 0,
            "recovery_authority_continuity_errors": 1,
        },
        {
            "durable_retry_idempotency_cases": 0,
            "durable_retry_idempotency_errors": 1,
        },
        {
            "forged_provider_rejection_cases": 0,
            "forged_provider_rejection_false_rejections": 1,
        },
        {
            "durable_subtask_domain_failure_cases": 0,
            "durable_subtask_domain_failure_false_successes": 1,
        },
        {
            "deterministic_detached_steer_cases": 0,
            "deterministic_detached_steer_rejection_failures": 1,
        },
        {
            "steering_seal_cases": 0,
            "steering_seal_reopen_violations": 1,
        },
        {
            "gateway_delegation_lease_cases": 0,
            "gateway_delegation_lease_violations": 1,
        },
        {
            "durable_control_requeue_cases": 0,
            "durable_control_requeue_violations": 1,
        },
        {
            "durable_control_live_owner_cases": 0,
            "durable_control_live_owner_violations": 1,
        },
        {
            "durable_control_crash_recovery_cases": 0,
            "durable_control_crash_recovery_losses": 1,
        },
        {
            "durable_control_legacy_stale_cases": 0,
            "durable_control_legacy_stale_violations": 1,
        },
    ],
)
def test_impossible_or_nonfinite_evidence_fails_closed(kwargs: dict[str, object]) -> None:
    values = {
        "total_cases": 100,
        "accuracy_cases": 100,
        "first_pass_valid_cases": 95,
        "post_repair_valid_cases": 99,
        "silent_critical_mismatches": 0,
        "model_repair_calls": 4,
        "model_repair_cost_usd": 0.01,
        "repair_limit_violations": 0,
        "rejected_patches": 1,
        "recorded_rejected_patches": 1,
        "steer_trials": 10_000,
        "steer_losses": 0,
        "detector_negative_cases": 20,
        "planning_latency_p95_ms": 100.0,
        "baseline_planning_latency_p95_ms": 100.0,
        "authoritative_plan_checks": 1,
        "non_replay_safe_execution_cases": 1,
        "normal_mode_self_heal_cases": 1,
        "resolver_owned_cases": 1,
        "resolver_constraint_omission_cases": 1,
        "forged_constraint_shadowing_cases": 1,
        "multi_consumer_binding_cases": 1,
        "model_repair_prompt_disclosure_cases": 1,
        "malformed_output_schema_preflight_cases": 1,
        "constraint_evidence_integrity_cases": 2,
        "provider_authority_integrity_cases": 4,
        "provider_runtime_closure_cases": 5,
        "provider_dispatch_authority_cases": 2,
        "recovery_authority_continuity_cases": 3,
        "durable_retry_idempotency_cases": 3,
        "generic_sensitive_approval_cases": 2,
        "run_skill_output_projection_cases": 2,
        "native_synthesis_output_projection_cases": 1,
        "forged_provider_rejection_cases": 2,
        "durable_subtask_domain_failure_cases": 1,
        "failing_turn_fallback_cases": 2,
        "deterministic_workflow_steer_cases": 1,
        "deterministic_detached_steer_cases": 1,
        "steering_seal_cases": 1,
        "gateway_frame_revocation_cases": 1,
        "gateway_delegation_lease_cases": 1,
        "foreground_ack_fallback_cases": 1,
        "durable_control_requeue_cases": 1,
        "durable_control_live_owner_cases": 1,
        "durable_control_crash_recovery_cases": 1,
        "durable_control_legacy_stale_cases": 1,
    }
    values.update(kwargs)

    with pytest.raises(ValueError):
        ReactiveBindingMetrics(**values)


def test_system_evidence_does_not_pad_binding_accuracy_denominator() -> None:
    cases = [
        BindingCaseEvidence(
            first_pass_valid=index < 95,
            post_repair_valid=index < 99,
            detector_negative=index < 20,
        )
        for index in range(100)
    ]
    cases.extend(
        BindingCaseEvidence(
            first_pass_valid=True,
            post_repair_valid=True,
            include_in_accuracy=False,
            authoritative_plan_checks=int(index == 0),
            non_replay_safe_execution_cases=int(index == 1),
            normal_mode_self_heal_cases=int(index == 2),
            resolver_owned_cases=int(index == 3),
            resolver_constraint_omission_cases=int(index == 3),
            forged_constraint_shadowing_cases=int(index == 4),
            multi_consumer_binding_cases=int(index == 17),
            model_repair_prompt_disclosure_cases=int(index == 18),
            malformed_output_schema_preflight_cases=int(index == 19),
            constraint_evidence_integrity_cases=2 * int(index == 19),
            provider_authority_integrity_cases=4 * int(index == 19),
            provider_runtime_closure_cases=5 * int(index == 19),
            provider_dispatch_authority_cases=2 * int(index == 19),
            recovery_authority_continuity_cases=3 * int(index == 19),
            durable_retry_idempotency_cases=3 * int(index == 19),
            generic_sensitive_approval_cases=int(index in {5, 6}),
            run_skill_output_projection_cases=2 * int(index == 7),
            native_synthesis_output_projection_cases=int(index == 8),
            forged_provider_rejection_cases=2 * int(index == 9),
            durable_subtask_domain_failure_cases=int(index == 10),
            durable_control_requeue_cases=int(index == 11),
            durable_control_live_owner_cases=int(index == 11),
            durable_control_crash_recovery_cases=int(index == 12),
            durable_control_legacy_stale_cases=int(index == 12),
            failing_turn_fallback_cases=int(index in {13, 14}),
            deterministic_workflow_steer_cases=int(index == 15),
            deterministic_detached_steer_cases=int(index == 15),
            steering_seal_cases=int(index == 15),
            gateway_frame_revocation_cases=int(index == 16),
            gateway_delegation_lease_cases=int(index == 16),
            foreground_ack_fallback_cases=int(index == 16),
            rejected_patches=int(index == 0),
            recorded_rejected_patches=int(index == 0),
        )
        for index in range(20)
    )

    metrics = aggregate_reactive_binding_metrics(
        cases,
        steer_trials=10_000,
        steer_losses=0,
        baseline_planning_latency_p95_ms=100.0,
        planning_latency_p95_ms=100.0,
    )

    assert metrics.total_cases == 120
    assert metrics.accuracy_cases == 100
    assert metrics.first_pass_validity == 0.95
    assert metrics.post_repair_validity == 0.99


def test_aggregate_requires_an_explicit_measured_candidate_latency() -> None:
    with pytest.raises(TypeError):
        aggregate_reactive_binding_metrics(  # type: ignore[call-arg]
            [],
            steer_trials=10_000,
            steer_losses=0,
            baseline_planning_latency_p95_ms=100.0,
        )


def test_release_report_fails_closed_until_complete_report_atomically_replaces_it(
    tmp_path,
) -> None:
    destination = tmp_path / "reactive-binding-release-gate.json"

    initialize_fail_closed_report(
        destination,
        failure_stage="gate_not_started",
    )
    initial = json.loads(destination.read_text(encoding="utf-8"))

    assert initial["passed"] is False
    assert initial["failure_stage"] == "gate_not_started"

    report = evaluate_reactive_binding_release(
        _passing_metrics(),
        repair_budget=_BUDGET,
        provenance=_PROVENANCE,
    )
    write_release_gate_report(destination, report)
    completed = json.loads(destination.read_text(encoding="utf-8"))

    assert completed["passed"] is True
    assert "failure_stage" not in completed


@pytest.mark.parametrize("workflow_name", ["ci.yml", "release.yml"])
def test_workflow_runs_release_gate_with_objective_corpus(
    workflow_name: str,
) -> None:
    workflow = (
        _REPO_ROOT / ".github" / "workflows" / workflow_name
    ).read_text(encoding="utf-8")
    evaluator_tests_at = workflow.index(
        "cli/tests/eval/test_reactive_binding_release_gate.py"
    )
    corpus_at = workflow.index(
        "cli/tests/eval/test_objective_provider_quality_offline_corpus.py"
    )

    # The gate job runs the gate-logic unit tests alongside the objective
    # provider-quality corpus that replaced the retired reactive-binding one.
    assert evaluator_tests_at < corpus_at
    # The deleted reactive-binding corpus and its orphaned report/artifact
    # dance must not be referenced any more (the sole report producer was
    # removed with the semantic-constraint architecture).
    assert "test_reactive_binding_offline_corpus.py" not in workflow
    assert "OMNI_REACTIVE_BINDING_REPORT" not in workflow
    assert "name: reactive-binding-release-gate" not in workflow


def test_windows_pid_probe_stays_in_the_regular_ci_matrix() -> None:
    workflow = (
        _REPO_ROOT / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    control_tests = (
        _REPO_ROOT
        / "cli"
        / "tests"
        / "cli"
        / "test_codex_turn_input_contract.py"
    ).read_text(encoding="utf-8")

    assert "windows-latest" in workflow
    assert 'pytest -q -m "not release_gate"' in workflow
    assert (
        "test_current_process_liveness_probe_is_non_destructive"
        in control_tests
    )


@pytest.mark.parametrize(
    ("call_rate", "cost"),
    [(-0.1, 0.0), (1.1, 0.0), (0.1, -0.01), (0.1, float("inf"))],
)
def test_repair_budget_must_be_explicit_and_finite(
    call_rate: float,
    cost: float,
) -> None:
    with pytest.raises(ValueError):
        ModelRepairBudget(
            max_call_rate=call_rate,
            max_cost_per_case_usd=cost,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minimum_cases": 99},
        {"minimum_accuracy_cases": 99},
        {"minimum_first_pass_validity": 0.949},
        {"minimum_post_repair_validity": 0.989},
        {"maximum_silent_critical_mismatches": 1},
        {"minimum_rejected_patches": 0},
        {"minimum_rejected_patch_recording_rate": 0.99},
        {"minimum_steer_trials": 9_999},
        {"maximum_steer_losses": 1},
        {"maximum_steer_duplicates": 1},
        {"minimum_detector_negative_cases": 19},
        {"minimum_planning_benchmark_samples": 49},
        {"maximum_detector_false_positive_rate": 0.011},
        {"maximum_planning_latency_regression": 0.11},
        {"minimum_authoritative_plan_checks": 0},
        {"minimum_non_replay_safe_execution_cases": 0},
        {"minimum_normal_mode_self_heal_cases": 0},
        {"minimum_resolver_owned_cases": 0},
        {"minimum_resolver_constraint_omission_cases": 0},
        {"minimum_forged_constraint_shadowing_cases": 0},
        {"minimum_multi_consumer_binding_cases": 0},
        {"minimum_model_repair_prompt_disclosure_cases": 0},
        {"minimum_malformed_output_schema_preflight_cases": 0},
        {"minimum_constraint_evidence_integrity_cases": 1},
        {"minimum_provider_authority_integrity_cases": 3},
        {"minimum_provider_runtime_closure_cases": 4},
        {"minimum_provider_dispatch_authority_cases": 1},
        {"minimum_recovery_authority_continuity_cases": 2},
        {"minimum_durable_retry_idempotency_cases": 2},
        {"minimum_generic_sensitive_approval_cases": 1},
        {"minimum_run_skill_output_projection_cases": 1},
        {"minimum_native_synthesis_output_projection_cases": 0},
        {"minimum_forged_provider_rejection_cases": 1},
        {"minimum_durable_subtask_domain_failure_cases": 0},
        {"minimum_failing_turn_fallback_cases": 1},
        {"minimum_deterministic_workflow_steer_cases": 0},
        {"minimum_deterministic_detached_steer_cases": 0},
        {"minimum_steering_seal_cases": 0},
        {"minimum_gateway_frame_revocation_cases": 0},
        {"minimum_gateway_delegation_lease_cases": 0},
        {"minimum_foreground_ack_fallback_cases": 0},
        {"minimum_durable_control_requeue_cases": 0},
        {"minimum_durable_control_live_owner_cases": 0},
        {"minimum_durable_control_crash_recovery_cases": 0},
        {"minimum_durable_control_legacy_stale_cases": 0},
    ],
)
def test_release_criteria_cannot_be_configured_below_product_floors(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        ReactiveBindingCriteria(**kwargs)
