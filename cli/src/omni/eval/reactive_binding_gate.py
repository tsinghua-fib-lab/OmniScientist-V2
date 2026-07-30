"""Offline release gates for reactive binding and busy-turn steering.

The gate consumes deterministic aggregate evidence; it never calls a model,
opens a database, or reaches the network. CI can therefore build evidence from
unit/scenario runs and make the release decision with explicit cost budgets.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class BindingCaseEvidence:
    """One offline binding scenario's release-relevant outcomes."""

    first_pass_valid: bool
    post_repair_valid: bool
    silent_critical_mismatches: int = 0
    model_repair_calls: int = 0
    model_repair_cost_usd: float = 0.0
    rejected_patches: int = 0
    recorded_rejected_patches: int = 0
    authoritative_plan_mismatches: int = 0
    non_replay_safe_duplicate_executions: int = 0
    normal_mode_self_heal_warnings: int = 0
    detector_negative: bool = False
    detector_false_positives: int = 0
    resolver_owned_unverified_executions: int = 0
    authoritative_plan_checks: int = 0
    non_replay_safe_execution_cases: int = 0
    normal_mode_self_heal_cases: int = 0
    resolver_owned_cases: int = 0
    resolver_constraint_omission_cases: int = 0
    resolver_constraint_omission_escapes: int = 0
    forged_constraint_shadowing_cases: int = 0
    forged_constraint_shadowing_escapes: int = 0
    multi_consumer_binding_cases: int = 0
    multi_consumer_binding_isolation_violations: int = 0
    model_repair_prompt_disclosure_cases: int = 0
    model_repair_prompt_disclosure_violations: int = 0
    malformed_output_schema_preflight_cases: int = 0
    malformed_output_schema_execution_escapes: int = 0
    constraint_evidence_integrity_cases: int = 0
    constraint_evidence_integrity_errors: int = 0
    provider_authority_integrity_cases: int = 0
    provider_authority_integrity_errors: int = 0
    provider_runtime_closure_cases: int = 0
    provider_runtime_closure_errors: int = 0
    provider_dispatch_authority_cases: int = 0
    provider_dispatch_authority_errors: int = 0
    recovery_authority_continuity_cases: int = 0
    recovery_authority_continuity_errors: int = 0
    durable_retry_idempotency_cases: int = 0
    durable_retry_idempotency_errors: int = 0
    generic_sensitive_approval_cases: int = 0
    generic_sensitive_approval_bypasses: int = 0
    run_skill_output_projection_cases: int = 0
    run_skill_output_false_successes: int = 0
    native_synthesis_output_projection_cases: int = 0
    native_synthesis_output_false_successes: int = 0
    forged_provider_rejection_cases: int = 0
    forged_provider_rejection_false_rejections: int = 0
    durable_subtask_domain_failure_cases: int = 0
    durable_subtask_domain_failure_false_successes: int = 0
    failing_turn_fallback_cases: int = 0
    failing_turn_fallback_losses: int = 0
    failing_turn_fallback_duplicates: int = 0
    deterministic_workflow_steer_cases: int = 0
    deterministic_workflow_false_applied: int = 0
    deterministic_detached_steer_cases: int = 0
    deterministic_detached_steer_rejection_failures: int = 0
    deterministic_detached_steer_orphan_controls: int = 0
    steering_seal_cases: int = 0
    steering_seal_reopen_violations: int = 0
    steering_seal_orphan_controls: int = 0
    gateway_frame_revocation_cases: int = 0
    gateway_frame_revocation_escapes: int = 0
    gateway_delegation_lease_cases: int = 0
    gateway_delegation_lease_violations: int = 0
    foreground_ack_fallback_cases: int = 0
    foreground_ack_retry_violations: int = 0
    foreground_ack_losses: int = 0
    foreground_ack_duplicates: int = 0
    durable_control_requeue_cases: int = 0
    durable_control_requeue_violations: int = 0
    durable_control_live_owner_cases: int = 0
    durable_control_live_owner_violations: int = 0
    durable_control_crash_recovery_cases: int = 0
    durable_control_crash_recovery_losses: int = 0
    durable_control_crash_recovery_duplicates: int = 0
    durable_control_legacy_stale_cases: int = 0
    durable_control_legacy_stale_violations: int = 0
    include_in_accuracy: bool = True

    def __post_init__(self) -> None:
        for name in (
            "silent_critical_mismatches",
            "model_repair_calls",
            "rejected_patches",
            "recorded_rejected_patches",
            "authoritative_plan_mismatches",
            "non_replay_safe_duplicate_executions",
            "normal_mode_self_heal_warnings",
            "detector_false_positives",
            "resolver_owned_unverified_executions",
            "authoritative_plan_checks",
            "non_replay_safe_execution_cases",
            "normal_mode_self_heal_cases",
            "resolver_owned_cases",
            "resolver_constraint_omission_cases",
            "resolver_constraint_omission_escapes",
            "forged_constraint_shadowing_cases",
            "forged_constraint_shadowing_escapes",
            "multi_consumer_binding_cases",
            "multi_consumer_binding_isolation_violations",
            "model_repair_prompt_disclosure_cases",
            "model_repair_prompt_disclosure_violations",
            "malformed_output_schema_preflight_cases",
            "malformed_output_schema_execution_escapes",
            "constraint_evidence_integrity_cases",
            "constraint_evidence_integrity_errors",
            "provider_authority_integrity_cases",
            "provider_authority_integrity_errors",
            "provider_runtime_closure_cases",
            "provider_runtime_closure_errors",
            "provider_dispatch_authority_cases",
            "provider_dispatch_authority_errors",
            "recovery_authority_continuity_cases",
            "recovery_authority_continuity_errors",
            "durable_retry_idempotency_cases",
            "durable_retry_idempotency_errors",
            "generic_sensitive_approval_cases",
            "generic_sensitive_approval_bypasses",
            "run_skill_output_projection_cases",
            "run_skill_output_false_successes",
            "native_synthesis_output_projection_cases",
            "native_synthesis_output_false_successes",
            "forged_provider_rejection_cases",
            "forged_provider_rejection_false_rejections",
            "durable_subtask_domain_failure_cases",
            "durable_subtask_domain_failure_false_successes",
            "failing_turn_fallback_cases",
            "failing_turn_fallback_losses",
            "failing_turn_fallback_duplicates",
            "deterministic_workflow_steer_cases",
            "deterministic_workflow_false_applied",
            "deterministic_detached_steer_cases",
            "deterministic_detached_steer_rejection_failures",
            "deterministic_detached_steer_orphan_controls",
            "steering_seal_cases",
            "steering_seal_reopen_violations",
            "steering_seal_orphan_controls",
            "gateway_frame_revocation_cases",
            "gateway_frame_revocation_escapes",
            "gateway_delegation_lease_cases",
            "gateway_delegation_lease_violations",
            "foreground_ack_fallback_cases",
            "foreground_ack_retry_violations",
            "foreground_ack_losses",
            "foreground_ack_duplicates",
            "durable_control_requeue_cases",
            "durable_control_requeue_violations",
            "durable_control_live_owner_cases",
            "durable_control_live_owner_violations",
            "durable_control_crash_recovery_cases",
            "durable_control_crash_recovery_losses",
            "durable_control_crash_recovery_duplicates",
            "durable_control_legacy_stale_cases",
            "durable_control_legacy_stale_violations",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        for failures, cases in (
            (
                "resolver_constraint_omission_escapes",
                "resolver_constraint_omission_cases",
            ),
            (
                "forged_constraint_shadowing_escapes",
                "forged_constraint_shadowing_cases",
            ),
            (
                "multi_consumer_binding_isolation_violations",
                "multi_consumer_binding_cases",
            ),
            (
                "model_repair_prompt_disclosure_violations",
                "model_repair_prompt_disclosure_cases",
            ),
            (
                "malformed_output_schema_execution_escapes",
                "malformed_output_schema_preflight_cases",
            ),
            (
                "constraint_evidence_integrity_errors",
                "constraint_evidence_integrity_cases",
            ),
            (
                "provider_authority_integrity_errors",
                "provider_authority_integrity_cases",
            ),
            (
                "provider_runtime_closure_errors",
                "provider_runtime_closure_cases",
            ),
            (
                "provider_dispatch_authority_errors",
                "provider_dispatch_authority_cases",
            ),
            (
                "recovery_authority_continuity_errors",
                "recovery_authority_continuity_cases",
            ),
            (
                "durable_retry_idempotency_errors",
                "durable_retry_idempotency_cases",
            ),
            (
                "generic_sensitive_approval_bypasses",
                "generic_sensitive_approval_cases",
            ),
            (
                "run_skill_output_false_successes",
                "run_skill_output_projection_cases",
            ),
            (
                "native_synthesis_output_false_successes",
                "native_synthesis_output_projection_cases",
            ),
            (
                "forged_provider_rejection_false_rejections",
                "forged_provider_rejection_cases",
            ),
            (
                "durable_subtask_domain_failure_false_successes",
                "durable_subtask_domain_failure_cases",
            ),
            ("failing_turn_fallback_losses", "failing_turn_fallback_cases"),
            (
                "failing_turn_fallback_duplicates",
                "failing_turn_fallback_cases",
            ),
            (
                "deterministic_workflow_false_applied",
                "deterministic_workflow_steer_cases",
            ),
            (
                "deterministic_detached_steer_rejection_failures",
                "deterministic_detached_steer_cases",
            ),
            ("steering_seal_reopen_violations", "steering_seal_cases"),
            (
                "gateway_delegation_lease_violations",
                "gateway_delegation_lease_cases",
            ),
            ("foreground_ack_retry_violations", "foreground_ack_fallback_cases"),
            ("foreground_ack_losses", "foreground_ack_fallback_cases"),
            ("foreground_ack_duplicates", "foreground_ack_fallback_cases"),
            ("durable_control_requeue_violations", "durable_control_requeue_cases"),
            (
                "durable_control_live_owner_violations",
                "durable_control_live_owner_cases",
            ),
            (
                "durable_control_crash_recovery_losses",
                "durable_control_crash_recovery_cases",
            ),
            (
                "durable_control_crash_recovery_duplicates",
                "durable_control_crash_recovery_cases",
            ),
            (
                "durable_control_legacy_stale_violations",
                "durable_control_legacy_stale_cases",
            ),
        ):
            if getattr(self, failures) > getattr(self, cases):
                raise ValueError(f"{failures} cannot exceed {cases}")
        if (
            not math.isfinite(self.model_repair_cost_usd)
            or self.model_repair_cost_usd < 0
        ):
            raise ValueError("model_repair_cost_usd must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ReactiveBindingMetrics:
    """Aggregate evidence supplied to the release gate."""

    total_cases: int
    accuracy_cases: int
    first_pass_valid_cases: int
    post_repair_valid_cases: int
    silent_critical_mismatches: int
    model_repair_calls: int
    model_repair_cost_usd: float
    repair_limit_violations: int
    rejected_patches: int
    recorded_rejected_patches: int
    steer_trials: int
    steer_losses: int
    steer_duplicates: int = 0
    authoritative_plan_mismatches: int = 0
    non_replay_safe_duplicate_executions: int = 0
    normal_mode_self_heal_warnings: int = 0
    detector_negative_cases: int = 0
    detector_false_positives: int = 0
    healthy_model_repair_calls: int = 0
    resolver_owned_unverified_executions: int = 0
    planning_latency_p95_ms: float = 0.0
    baseline_planning_latency_p95_ms: float = 0.0
    authoritative_plan_checks: int = 0
    non_replay_safe_execution_cases: int = 0
    normal_mode_self_heal_cases: int = 0
    resolver_owned_cases: int = 0
    resolver_constraint_omission_cases: int = 0
    resolver_constraint_omission_escapes: int = 0
    forged_constraint_shadowing_cases: int = 0
    forged_constraint_shadowing_escapes: int = 0
    multi_consumer_binding_cases: int = 0
    multi_consumer_binding_isolation_violations: int = 0
    model_repair_prompt_disclosure_cases: int = 0
    model_repair_prompt_disclosure_violations: int = 0
    malformed_output_schema_preflight_cases: int = 0
    malformed_output_schema_execution_escapes: int = 0
    constraint_evidence_integrity_cases: int = 0
    constraint_evidence_integrity_errors: int = 0
    provider_authority_integrity_cases: int = 0
    provider_authority_integrity_errors: int = 0
    provider_runtime_closure_cases: int = 0
    provider_runtime_closure_errors: int = 0
    provider_dispatch_authority_cases: int = 0
    provider_dispatch_authority_errors: int = 0
    recovery_authority_continuity_cases: int = 0
    recovery_authority_continuity_errors: int = 0
    durable_retry_idempotency_cases: int = 0
    durable_retry_idempotency_errors: int = 0
    generic_sensitive_approval_cases: int = 0
    generic_sensitive_approval_bypasses: int = 0
    run_skill_output_projection_cases: int = 0
    run_skill_output_false_successes: int = 0
    native_synthesis_output_projection_cases: int = 0
    native_synthesis_output_false_successes: int = 0
    forged_provider_rejection_cases: int = 0
    forged_provider_rejection_false_rejections: int = 0
    durable_subtask_domain_failure_cases: int = 0
    durable_subtask_domain_failure_false_successes: int = 0
    failing_turn_fallback_cases: int = 0
    failing_turn_fallback_losses: int = 0
    failing_turn_fallback_duplicates: int = 0
    deterministic_workflow_steer_cases: int = 0
    deterministic_workflow_false_applied: int = 0
    deterministic_detached_steer_cases: int = 0
    deterministic_detached_steer_rejection_failures: int = 0
    deterministic_detached_steer_orphan_controls: int = 0
    steering_seal_cases: int = 0
    steering_seal_reopen_violations: int = 0
    steering_seal_orphan_controls: int = 0
    gateway_frame_revocation_cases: int = 0
    gateway_frame_revocation_escapes: int = 0
    gateway_delegation_lease_cases: int = 0
    gateway_delegation_lease_violations: int = 0
    foreground_ack_fallback_cases: int = 0
    foreground_ack_retry_violations: int = 0
    foreground_ack_losses: int = 0
    foreground_ack_duplicates: int = 0
    durable_control_requeue_cases: int = 0
    durable_control_requeue_violations: int = 0
    durable_control_live_owner_cases: int = 0
    durable_control_live_owner_violations: int = 0
    durable_control_crash_recovery_cases: int = 0
    durable_control_crash_recovery_losses: int = 0
    durable_control_crash_recovery_duplicates: int = 0
    durable_control_legacy_stale_cases: int = 0
    durable_control_legacy_stale_violations: int = 0

    def __post_init__(self) -> None:
        count_fields = (
            "total_cases",
            "accuracy_cases",
            "first_pass_valid_cases",
            "post_repair_valid_cases",
            "silent_critical_mismatches",
            "model_repair_calls",
            "repair_limit_violations",
            "rejected_patches",
            "recorded_rejected_patches",
            "steer_trials",
            "steer_losses",
            "steer_duplicates",
            "authoritative_plan_mismatches",
            "non_replay_safe_duplicate_executions",
            "normal_mode_self_heal_warnings",
            "detector_negative_cases",
            "detector_false_positives",
            "healthy_model_repair_calls",
            "resolver_owned_unverified_executions",
            "authoritative_plan_checks",
            "non_replay_safe_execution_cases",
            "normal_mode_self_heal_cases",
            "resolver_owned_cases",
            "resolver_constraint_omission_cases",
            "resolver_constraint_omission_escapes",
            "forged_constraint_shadowing_cases",
            "forged_constraint_shadowing_escapes",
            "multi_consumer_binding_cases",
            "multi_consumer_binding_isolation_violations",
            "model_repair_prompt_disclosure_cases",
            "model_repair_prompt_disclosure_violations",
            "malformed_output_schema_preflight_cases",
            "malformed_output_schema_execution_escapes",
            "constraint_evidence_integrity_cases",
            "constraint_evidence_integrity_errors",
            "provider_authority_integrity_cases",
            "provider_authority_integrity_errors",
            "provider_runtime_closure_cases",
            "provider_runtime_closure_errors",
            "provider_dispatch_authority_cases",
            "provider_dispatch_authority_errors",
            "recovery_authority_continuity_cases",
            "recovery_authority_continuity_errors",
            "durable_retry_idempotency_cases",
            "durable_retry_idempotency_errors",
            "generic_sensitive_approval_cases",
            "generic_sensitive_approval_bypasses",
            "run_skill_output_projection_cases",
            "run_skill_output_false_successes",
            "native_synthesis_output_projection_cases",
            "native_synthesis_output_false_successes",
            "forged_provider_rejection_cases",
            "forged_provider_rejection_false_rejections",
            "durable_subtask_domain_failure_cases",
            "durable_subtask_domain_failure_false_successes",
            "failing_turn_fallback_cases",
            "failing_turn_fallback_losses",
            "failing_turn_fallback_duplicates",
            "deterministic_workflow_steer_cases",
            "deterministic_workflow_false_applied",
            "deterministic_detached_steer_cases",
            "deterministic_detached_steer_rejection_failures",
            "deterministic_detached_steer_orphan_controls",
            "steering_seal_cases",
            "steering_seal_reopen_violations",
            "steering_seal_orphan_controls",
            "gateway_frame_revocation_cases",
            "gateway_frame_revocation_escapes",
            "gateway_delegation_lease_cases",
            "gateway_delegation_lease_violations",
            "foreground_ack_fallback_cases",
            "foreground_ack_retry_violations",
            "foreground_ack_losses",
            "foreground_ack_duplicates",
            "durable_control_requeue_cases",
            "durable_control_requeue_violations",
            "durable_control_live_owner_cases",
            "durable_control_live_owner_violations",
            "durable_control_crash_recovery_cases",
            "durable_control_crash_recovery_losses",
            "durable_control_crash_recovery_duplicates",
            "durable_control_legacy_stale_cases",
            "durable_control_legacy_stale_violations",
        )
        for name in count_fields:
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.accuracy_cases > self.total_cases:
            raise ValueError("accuracy_cases cannot exceed total_cases")
        if self.first_pass_valid_cases > self.accuracy_cases:
            raise ValueError("first_pass_valid_cases cannot exceed accuracy_cases")
        if self.post_repair_valid_cases > self.accuracy_cases:
            raise ValueError("post_repair_valid_cases cannot exceed accuracy_cases")
        if self.steer_losses > self.steer_trials:
            raise ValueError("steer_losses cannot exceed steer_trials")
        if self.steer_duplicates > self.steer_trials:
            raise ValueError("steer_duplicates cannot exceed steer_trials")
        bounded_failures = (
            (
                "resolver_constraint_omission_escapes",
                "resolver_constraint_omission_cases",
            ),
            (
                "forged_constraint_shadowing_escapes",
                "forged_constraint_shadowing_cases",
            ),
            (
                "multi_consumer_binding_isolation_violations",
                "multi_consumer_binding_cases",
            ),
            (
                "model_repair_prompt_disclosure_violations",
                "model_repair_prompt_disclosure_cases",
            ),
            (
                "malformed_output_schema_execution_escapes",
                "malformed_output_schema_preflight_cases",
            ),
            (
                "constraint_evidence_integrity_errors",
                "constraint_evidence_integrity_cases",
            ),
            (
                "provider_authority_integrity_errors",
                "provider_authority_integrity_cases",
            ),
            (
                "provider_runtime_closure_errors",
                "provider_runtime_closure_cases",
            ),
            (
                "provider_dispatch_authority_errors",
                "provider_dispatch_authority_cases",
            ),
            (
                "recovery_authority_continuity_errors",
                "recovery_authority_continuity_cases",
            ),
            (
                "durable_retry_idempotency_errors",
                "durable_retry_idempotency_cases",
            ),
            (
                "generic_sensitive_approval_bypasses",
                "generic_sensitive_approval_cases",
            ),
            (
                "run_skill_output_false_successes",
                "run_skill_output_projection_cases",
            ),
            (
                "native_synthesis_output_false_successes",
                "native_synthesis_output_projection_cases",
            ),
            (
                "forged_provider_rejection_false_rejections",
                "forged_provider_rejection_cases",
            ),
            (
                "durable_subtask_domain_failure_false_successes",
                "durable_subtask_domain_failure_cases",
            ),
            ("failing_turn_fallback_losses", "failing_turn_fallback_cases"),
            (
                "failing_turn_fallback_duplicates",
                "failing_turn_fallback_cases",
            ),
            (
                "deterministic_workflow_false_applied",
                "deterministic_workflow_steer_cases",
            ),
            (
                "deterministic_detached_steer_rejection_failures",
                "deterministic_detached_steer_cases",
            ),
            ("steering_seal_reopen_violations", "steering_seal_cases"),
            (
                "gateway_delegation_lease_violations",
                "gateway_delegation_lease_cases",
            ),
            ("foreground_ack_retry_violations", "foreground_ack_fallback_cases"),
            ("foreground_ack_losses", "foreground_ack_fallback_cases"),
            ("foreground_ack_duplicates", "foreground_ack_fallback_cases"),
            ("durable_control_requeue_violations", "durable_control_requeue_cases"),
            (
                "durable_control_live_owner_violations",
                "durable_control_live_owner_cases",
            ),
            (
                "durable_control_crash_recovery_losses",
                "durable_control_crash_recovery_cases",
            ),
            (
                "durable_control_crash_recovery_duplicates",
                "durable_control_crash_recovery_cases",
            ),
            (
                "durable_control_legacy_stale_violations",
                "durable_control_legacy_stale_cases",
            ),
        )
        for failures, cases in bounded_failures:
            if getattr(self, failures) > getattr(self, cases):
                raise ValueError(f"{failures} cannot exceed {cases}")
        if not math.isfinite(self.model_repair_cost_usd) or self.model_repair_cost_usd < 0:
            raise ValueError("model_repair_cost_usd must be finite and non-negative")
        for name in ("planning_latency_p95_ms", "baseline_planning_latency_p95_ms"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")

    @property
    def first_pass_validity(self) -> float:
        return _rate(self.first_pass_valid_cases, self.accuracy_cases)

    @property
    def post_repair_validity(self) -> float:
        return _rate(self.post_repair_valid_cases, self.accuracy_cases)

    @property
    def model_repair_call_rate(self) -> float:
        return _rate(self.model_repair_calls, self.accuracy_cases)

    @property
    def model_repair_cost_per_case_usd(self) -> float:
        return _rate(self.model_repair_cost_usd, self.accuracy_cases)

    @property
    def rejected_patch_recording_rate(self) -> float:
        return _rate(self.recorded_rejected_patches, self.rejected_patches, empty=1.0)

    @property
    def detector_false_positive_rate(self) -> float:
        return _rate(
            self.detector_false_positives,
            self.detector_negative_cases,
            empty=1.0,
        )

    @property
    def planning_latency_regression(self) -> float:
        if self.baseline_planning_latency_p95_ms <= 0:
            return math.inf
        return (
            self.planning_latency_p95_ms - self.baseline_planning_latency_p95_ms
        ) / self.baseline_planning_latency_p95_ms

    def to_dict(self) -> dict[str, int | float]:
        payload: dict[str, int | float] = asdict(self)
        payload.update({
            "model_repair_cost_usd": round(self.model_repair_cost_usd, 8),
            "first_pass_validity": round(self.first_pass_validity, 6),
            "post_repair_validity": round(self.post_repair_validity, 6),
            "model_repair_call_rate": round(self.model_repair_call_rate, 6),
            "model_repair_cost_per_case_usd": round(
                self.model_repair_cost_per_case_usd, 8
            ),
            "rejected_patch_recording_rate": round(
                self.rejected_patch_recording_rate, 6
            ),
            "detector_false_positive_rate": round(
                self.detector_false_positive_rate, 6
            ),
            "planning_latency_regression": round(
                self.planning_latency_regression, 6
            ),
        })
        return payload


@dataclass(frozen=True, slots=True)
class ModelRepairBudget:
    """Owner/CI supplied spend envelope; release code never invents one."""

    max_call_rate: float
    max_cost_per_case_usd: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.max_call_rate <= 1.0:
            raise ValueError("max_call_rate must be between 0 and 1")
        if (
            not math.isfinite(self.max_cost_per_case_usd)
            or self.max_cost_per_case_usd < 0
        ):
            raise ValueError("max_cost_per_case_usd must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ReactiveBindingProvenance:
    """Reproducibility identity for one release-gate report."""

    candidate_ref: str
    baseline_ref: str
    benchmark_id: str
    corpus_sha256: str
    proposal_sha256: str
    prompt_sha256: str
    catalog_sha256: str
    contract_sha256: str
    platform: str
    python: str
    benchmark_samples: int
    benchmark_warmups: int

    def __post_init__(self) -> None:
        for name in (
            "candidate_ref",
            "baseline_ref",
            "benchmark_id",
            "platform",
            "python",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be non-empty")
        for name in (
            "corpus_sha256",
            "proposal_sha256",
            "prompt_sha256",
            "catalog_sha256",
            "contract_sha256",
        ):
            value = str(getattr(self, name))
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self.benchmark_samples <= 0:
            raise ValueError("benchmark_samples must be positive")
        if self.benchmark_warmups < 0:
            raise ValueError("benchmark_warmups must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReactiveBindingCriteria:
    """Non-negotiable quality thresholds for a release corpus."""

    minimum_cases: int = 100
    minimum_accuracy_cases: int = 100
    minimum_first_pass_validity: float = 0.95
    minimum_post_repair_validity: float = 0.99
    maximum_silent_critical_mismatches: int = 0
    minimum_rejected_patches: int = 1
    minimum_rejected_patch_recording_rate: float = 1.0
    minimum_steer_trials: int = 10_000
    maximum_steer_losses: int = 0
    maximum_steer_duplicates: int = 0
    minimum_detector_negative_cases: int = 20
    maximum_detector_false_positive_rate: float = 0.01
    maximum_planning_latency_regression: float = 0.10
    minimum_planning_benchmark_samples: int = 50
    minimum_authoritative_plan_checks: int = 1
    minimum_non_replay_safe_execution_cases: int = 1
    minimum_normal_mode_self_heal_cases: int = 1
    minimum_resolver_owned_cases: int = 1
    minimum_resolver_constraint_omission_cases: int = 1
    minimum_forged_constraint_shadowing_cases: int = 1
    minimum_multi_consumer_binding_cases: int = 1
    minimum_model_repair_prompt_disclosure_cases: int = 1
    minimum_malformed_output_schema_preflight_cases: int = 1
    minimum_constraint_evidence_integrity_cases: int = 2
    minimum_provider_authority_integrity_cases: int = 4
    minimum_provider_runtime_closure_cases: int = 5
    minimum_provider_dispatch_authority_cases: int = 2
    minimum_recovery_authority_continuity_cases: int = 3
    minimum_durable_retry_idempotency_cases: int = 3
    minimum_generic_sensitive_approval_cases: int = 2
    minimum_run_skill_output_projection_cases: int = 2
    minimum_native_synthesis_output_projection_cases: int = 1
    minimum_forged_provider_rejection_cases: int = 2
    minimum_durable_subtask_domain_failure_cases: int = 1
    minimum_failing_turn_fallback_cases: int = 2
    minimum_deterministic_workflow_steer_cases: int = 1
    minimum_deterministic_detached_steer_cases: int = 1
    minimum_steering_seal_cases: int = 1
    minimum_gateway_frame_revocation_cases: int = 1
    minimum_gateway_delegation_lease_cases: int = 1
    minimum_foreground_ack_fallback_cases: int = 1
    minimum_durable_control_requeue_cases: int = 1
    minimum_durable_control_live_owner_cases: int = 1
    minimum_durable_control_crash_recovery_cases: int = 1
    minimum_durable_control_legacy_stale_cases: int = 1

    def __post_init__(self) -> None:
        floors = {
            "minimum_cases": 100,
            "minimum_accuracy_cases": 100,
            "minimum_first_pass_validity": 0.95,
            "minimum_post_repair_validity": 0.99,
            "minimum_rejected_patches": 1,
            "minimum_rejected_patch_recording_rate": 1.0,
            "minimum_steer_trials": 10_000,
            "minimum_detector_negative_cases": 20,
            "minimum_planning_benchmark_samples": 50,
            "minimum_authoritative_plan_checks": 1,
            "minimum_non_replay_safe_execution_cases": 1,
            "minimum_normal_mode_self_heal_cases": 1,
            "minimum_resolver_owned_cases": 1,
            "minimum_resolver_constraint_omission_cases": 1,
            "minimum_forged_constraint_shadowing_cases": 1,
            "minimum_multi_consumer_binding_cases": 1,
            "minimum_model_repair_prompt_disclosure_cases": 1,
            "minimum_malformed_output_schema_preflight_cases": 1,
            "minimum_constraint_evidence_integrity_cases": 2,
            "minimum_provider_authority_integrity_cases": 4,
            "minimum_provider_runtime_closure_cases": 5,
            "minimum_provider_dispatch_authority_cases": 2,
            "minimum_recovery_authority_continuity_cases": 3,
            "minimum_durable_retry_idempotency_cases": 3,
            "minimum_generic_sensitive_approval_cases": 2,
            "minimum_run_skill_output_projection_cases": 2,
            "minimum_native_synthesis_output_projection_cases": 1,
            "minimum_forged_provider_rejection_cases": 2,
            "minimum_durable_subtask_domain_failure_cases": 1,
            "minimum_failing_turn_fallback_cases": 2,
            "minimum_deterministic_workflow_steer_cases": 1,
            "minimum_deterministic_detached_steer_cases": 1,
            "minimum_steering_seal_cases": 1,
            "minimum_gateway_frame_revocation_cases": 1,
            "minimum_gateway_delegation_lease_cases": 1,
            "minimum_foreground_ack_fallback_cases": 1,
            "minimum_durable_control_requeue_cases": 1,
            "minimum_durable_control_live_owner_cases": 1,
            "minimum_durable_control_crash_recovery_cases": 1,
            "minimum_durable_control_legacy_stale_cases": 1,
        }
        for name, floor in floors.items():
            if getattr(self, name) < floor:
                raise ValueError(f"{name} cannot weaken the release floor {floor}")
        zero_tolerance = (
            "maximum_silent_critical_mismatches",
            "maximum_steer_losses",
            "maximum_steer_duplicates",
        )
        for name in zero_tolerance:
            if getattr(self, name) != 0:
                raise ValueError(f"{name} is a zero-tolerance release invariant")
        if self.maximum_detector_false_positive_rate > 0.01:
            raise ValueError(
                "maximum_detector_false_positive_rate cannot exceed 0.01"
            )
        if self.maximum_planning_latency_regression > 0.10:
            raise ValueError(
                "maximum_planning_latency_regression cannot exceed 0.10"
            )


@dataclass(frozen=True, slots=True)
class ReleaseGateCheck:
    name: str
    passed: bool
    actual: int | float
    comparator: str
    threshold: int | float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "actual": _rounded(self.actual),
            "comparator": self.comparator,
            "threshold": _rounded(self.threshold),
        }


@dataclass(frozen=True, slots=True)
class ReactiveBindingGateReport:
    metrics: ReactiveBindingMetrics
    repair_budget: ModelRepairBudget
    criteria: ReactiveBindingCriteria
    provenance: ReactiveBindingProvenance
    checks: tuple[ReleaseGateCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "omni.reactive-binding-release-gate.v1",
            "passed": self.passed,
            "failed_checks": list(self.failed_checks),
            "metrics": self.metrics.to_dict(),
            "repair_budget": asdict(self.repair_budget),
            "criteria": asdict(self.criteria),
            "provenance": self.provenance.to_dict(),
            "checks": [check.to_dict() for check in self.checks],
        }


def aggregate_reactive_binding_metrics(
    cases: list[BindingCaseEvidence] | tuple[BindingCaseEvidence, ...],
    *,
    steer_trials: int,
    steer_losses: int,
    steer_duplicates: int = 0,
    baseline_planning_latency_p95_ms: float,
    planning_latency_p95_ms: float,
) -> ReactiveBindingMetrics:
    """Aggregate deterministic scenario and 10k-race evidence."""
    accuracy_cases = [case for case in cases if case.include_in_accuracy]
    return ReactiveBindingMetrics(
        total_cases=len(cases),
        accuracy_cases=len(accuracy_cases),
        first_pass_valid_cases=sum(case.first_pass_valid for case in accuracy_cases),
        post_repair_valid_cases=sum(case.post_repair_valid for case in accuracy_cases),
        silent_critical_mismatches=sum(
            case.silent_critical_mismatches for case in cases
        ),
        model_repair_calls=sum(case.model_repair_calls for case in accuracy_cases),
        model_repair_cost_usd=sum(
            case.model_repair_cost_usd for case in accuracy_cases
        ),
        repair_limit_violations=sum(
            case.model_repair_calls > 1 for case in accuracy_cases
        ),
        rejected_patches=sum(case.rejected_patches for case in cases),
        recorded_rejected_patches=sum(
            case.recorded_rejected_patches for case in cases
        ),
        steer_trials=steer_trials,
        steer_losses=steer_losses,
        steer_duplicates=steer_duplicates,
        authoritative_plan_mismatches=sum(
            case.authoritative_plan_mismatches for case in cases
        ),
        non_replay_safe_duplicate_executions=sum(
            case.non_replay_safe_duplicate_executions for case in cases
        ),
        normal_mode_self_heal_warnings=sum(
            case.normal_mode_self_heal_warnings for case in cases
        ),
        detector_negative_cases=sum(case.detector_negative for case in cases),
        detector_false_positives=sum(
            case.detector_false_positives for case in cases
        ),
        healthy_model_repair_calls=sum(
            case.model_repair_calls
            for case in accuracy_cases
            if case.first_pass_valid
        ),
        resolver_owned_unverified_executions=sum(
            case.resolver_owned_unverified_executions for case in cases
        ),
        planning_latency_p95_ms=planning_latency_p95_ms,
        baseline_planning_latency_p95_ms=baseline_planning_latency_p95_ms,
        authoritative_plan_checks=sum(
            case.authoritative_plan_checks for case in cases
        ),
        non_replay_safe_execution_cases=sum(
            case.non_replay_safe_execution_cases for case in cases
        ),
        normal_mode_self_heal_cases=sum(
            case.normal_mode_self_heal_cases for case in cases
        ),
        resolver_owned_cases=sum(case.resolver_owned_cases for case in cases),
        resolver_constraint_omission_cases=sum(
            case.resolver_constraint_omission_cases for case in cases
        ),
        resolver_constraint_omission_escapes=sum(
            case.resolver_constraint_omission_escapes for case in cases
        ),
        forged_constraint_shadowing_cases=sum(
            case.forged_constraint_shadowing_cases for case in cases
        ),
        forged_constraint_shadowing_escapes=sum(
            case.forged_constraint_shadowing_escapes for case in cases
        ),
        multi_consumer_binding_cases=sum(
            case.multi_consumer_binding_cases for case in cases
        ),
        multi_consumer_binding_isolation_violations=sum(
            case.multi_consumer_binding_isolation_violations
            for case in cases
        ),
        model_repair_prompt_disclosure_cases=sum(
            case.model_repair_prompt_disclosure_cases for case in cases
        ),
        model_repair_prompt_disclosure_violations=sum(
            case.model_repair_prompt_disclosure_violations
            for case in cases
        ),
        malformed_output_schema_preflight_cases=sum(
            case.malformed_output_schema_preflight_cases
            for case in cases
        ),
        malformed_output_schema_execution_escapes=sum(
            case.malformed_output_schema_execution_escapes
            for case in cases
        ),
        constraint_evidence_integrity_cases=sum(
            case.constraint_evidence_integrity_cases for case in cases
        ),
        constraint_evidence_integrity_errors=sum(
            case.constraint_evidence_integrity_errors for case in cases
        ),
        provider_authority_integrity_cases=sum(
            case.provider_authority_integrity_cases for case in cases
        ),
        provider_authority_integrity_errors=sum(
            case.provider_authority_integrity_errors for case in cases
        ),
        provider_runtime_closure_cases=sum(
            case.provider_runtime_closure_cases for case in cases
        ),
        provider_runtime_closure_errors=sum(
            case.provider_runtime_closure_errors for case in cases
        ),
        provider_dispatch_authority_cases=sum(
            case.provider_dispatch_authority_cases for case in cases
        ),
        provider_dispatch_authority_errors=sum(
            case.provider_dispatch_authority_errors for case in cases
        ),
        recovery_authority_continuity_cases=sum(
            case.recovery_authority_continuity_cases for case in cases
        ),
        recovery_authority_continuity_errors=sum(
            case.recovery_authority_continuity_errors for case in cases
        ),
        durable_retry_idempotency_cases=sum(
            case.durable_retry_idempotency_cases for case in cases
        ),
        durable_retry_idempotency_errors=sum(
            case.durable_retry_idempotency_errors for case in cases
        ),
        generic_sensitive_approval_cases=sum(
            case.generic_sensitive_approval_cases for case in cases
        ),
        generic_sensitive_approval_bypasses=sum(
            case.generic_sensitive_approval_bypasses for case in cases
        ),
        run_skill_output_projection_cases=sum(
            case.run_skill_output_projection_cases for case in cases
        ),
        run_skill_output_false_successes=sum(
            case.run_skill_output_false_successes for case in cases
        ),
        native_synthesis_output_projection_cases=sum(
            case.native_synthesis_output_projection_cases for case in cases
        ),
        native_synthesis_output_false_successes=sum(
            case.native_synthesis_output_false_successes for case in cases
        ),
        forged_provider_rejection_cases=sum(
            case.forged_provider_rejection_cases for case in cases
        ),
        forged_provider_rejection_false_rejections=sum(
            case.forged_provider_rejection_false_rejections for case in cases
        ),
        durable_subtask_domain_failure_cases=sum(
            case.durable_subtask_domain_failure_cases for case in cases
        ),
        durable_subtask_domain_failure_false_successes=sum(
            case.durable_subtask_domain_failure_false_successes
            for case in cases
        ),
        failing_turn_fallback_cases=sum(
            case.failing_turn_fallback_cases for case in cases
        ),
        failing_turn_fallback_losses=sum(
            case.failing_turn_fallback_losses for case in cases
        ),
        failing_turn_fallback_duplicates=sum(
            case.failing_turn_fallback_duplicates for case in cases
        ),
        deterministic_workflow_steer_cases=sum(
            case.deterministic_workflow_steer_cases for case in cases
        ),
        deterministic_workflow_false_applied=sum(
            case.deterministic_workflow_false_applied for case in cases
        ),
        deterministic_detached_steer_cases=sum(
            case.deterministic_detached_steer_cases for case in cases
        ),
        deterministic_detached_steer_rejection_failures=sum(
            case.deterministic_detached_steer_rejection_failures
            for case in cases
        ),
        deterministic_detached_steer_orphan_controls=sum(
            case.deterministic_detached_steer_orphan_controls
            for case in cases
        ),
        steering_seal_cases=sum(case.steering_seal_cases for case in cases),
        steering_seal_reopen_violations=sum(
            case.steering_seal_reopen_violations for case in cases
        ),
        steering_seal_orphan_controls=sum(
            case.steering_seal_orphan_controls for case in cases
        ),
        gateway_frame_revocation_cases=sum(
            case.gateway_frame_revocation_cases for case in cases
        ),
        gateway_frame_revocation_escapes=sum(
            case.gateway_frame_revocation_escapes for case in cases
        ),
        gateway_delegation_lease_cases=sum(
            case.gateway_delegation_lease_cases for case in cases
        ),
        gateway_delegation_lease_violations=sum(
            case.gateway_delegation_lease_violations for case in cases
        ),
        foreground_ack_fallback_cases=sum(
            case.foreground_ack_fallback_cases for case in cases
        ),
        foreground_ack_retry_violations=sum(
            case.foreground_ack_retry_violations for case in cases
        ),
        foreground_ack_losses=sum(case.foreground_ack_losses for case in cases),
        foreground_ack_duplicates=sum(
            case.foreground_ack_duplicates for case in cases
        ),
        durable_control_requeue_cases=sum(
            case.durable_control_requeue_cases for case in cases
        ),
        durable_control_requeue_violations=sum(
            case.durable_control_requeue_violations for case in cases
        ),
        durable_control_live_owner_cases=sum(
            case.durable_control_live_owner_cases for case in cases
        ),
        durable_control_live_owner_violations=sum(
            case.durable_control_live_owner_violations for case in cases
        ),
        durable_control_crash_recovery_cases=sum(
            case.durable_control_crash_recovery_cases for case in cases
        ),
        durable_control_crash_recovery_losses=sum(
            case.durable_control_crash_recovery_losses for case in cases
        ),
        durable_control_crash_recovery_duplicates=sum(
            case.durable_control_crash_recovery_duplicates for case in cases
        ),
        durable_control_legacy_stale_cases=sum(
            case.durable_control_legacy_stale_cases for case in cases
        ),
        durable_control_legacy_stale_violations=sum(
            case.durable_control_legacy_stale_violations for case in cases
        ),
    )


def evaluate_reactive_binding_release(
    metrics: ReactiveBindingMetrics,
    *,
    repair_budget: ModelRepairBudget,
    provenance: ReactiveBindingProvenance,
    criteria: ReactiveBindingCriteria | None = None,
) -> ReactiveBindingGateReport:
    """Apply quality, audit, spend, and race gates to offline evidence."""
    cfg = criteria or ReactiveBindingCriteria()
    checks = (
        _minimum("corpus_cases", metrics.total_cases, cfg.minimum_cases),
        _minimum(
            "accuracy_cases",
            metrics.accuracy_cases,
            cfg.minimum_accuracy_cases,
        ),
        _minimum(
            "first_pass_validity",
            metrics.first_pass_validity,
            cfg.minimum_first_pass_validity,
        ),
        _minimum(
            "post_repair_validity",
            metrics.post_repair_validity,
            cfg.minimum_post_repair_validity,
        ),
        _maximum(
            "silent_critical_mismatches",
            metrics.silent_critical_mismatches,
            cfg.maximum_silent_critical_mismatches,
        ),
        _maximum(
            "repair_limit_violations",
            metrics.repair_limit_violations,
            0,
        ),
        _maximum(
            "model_repair_call_rate",
            metrics.model_repair_call_rate,
            repair_budget.max_call_rate,
        ),
        _maximum(
            "model_repair_cost_per_case_usd",
            metrics.model_repair_cost_per_case_usd,
            repair_budget.max_cost_per_case_usd,
        ),
        _minimum(
            "rejected_patch_cases",
            metrics.rejected_patches,
            cfg.minimum_rejected_patches,
        ),
        _minimum(
            "rejected_patch_recording_rate",
            metrics.rejected_patch_recording_rate,
            cfg.minimum_rejected_patch_recording_rate,
        ),
        _minimum("steer_trials", metrics.steer_trials, cfg.minimum_steer_trials),
        _maximum("steer_losses", metrics.steer_losses, cfg.maximum_steer_losses),
        _maximum(
            "steer_duplicates",
            metrics.steer_duplicates,
            cfg.maximum_steer_duplicates,
        ),
        _maximum(
            "authoritative_plan_mismatches",
            metrics.authoritative_plan_mismatches,
            0,
        ),
        _minimum(
            "authoritative_plan_checks",
            metrics.authoritative_plan_checks,
            cfg.minimum_authoritative_plan_checks,
        ),
        _maximum(
            "non_replay_safe_duplicate_executions",
            metrics.non_replay_safe_duplicate_executions,
            0,
        ),
        _minimum(
            "non_replay_safe_execution_cases",
            metrics.non_replay_safe_execution_cases,
            cfg.minimum_non_replay_safe_execution_cases,
        ),
        _maximum(
            "normal_mode_self_heal_warnings",
            metrics.normal_mode_self_heal_warnings,
            0,
        ),
        _minimum(
            "normal_mode_self_heal_cases",
            metrics.normal_mode_self_heal_cases,
            cfg.minimum_normal_mode_self_heal_cases,
        ),
        _minimum(
            "detector_negative_cases",
            metrics.detector_negative_cases,
            cfg.minimum_detector_negative_cases,
        ),
        _maximum(
            "detector_false_positive_rate",
            metrics.detector_false_positive_rate,
            cfg.maximum_detector_false_positive_rate,
        ),
        _maximum(
            "healthy_model_repair_calls",
            metrics.healthy_model_repair_calls,
            0,
        ),
        _maximum(
            "resolver_owned_unverified_executions",
            metrics.resolver_owned_unverified_executions,
            0,
        ),
        _minimum(
            "resolver_owned_cases",
            metrics.resolver_owned_cases,
            cfg.minimum_resolver_owned_cases,
        ),
        _maximum(
            "resolver_constraint_omission_escapes",
            metrics.resolver_constraint_omission_escapes,
            0,
        ),
        _minimum(
            "resolver_constraint_omission_cases",
            metrics.resolver_constraint_omission_cases,
            cfg.minimum_resolver_constraint_omission_cases,
        ),
        _maximum(
            "forged_constraint_shadowing_escapes",
            metrics.forged_constraint_shadowing_escapes,
            0,
        ),
        _minimum(
            "forged_constraint_shadowing_cases",
            metrics.forged_constraint_shadowing_cases,
            cfg.minimum_forged_constraint_shadowing_cases,
        ),
        _maximum(
            "multi_consumer_binding_isolation_violations",
            metrics.multi_consumer_binding_isolation_violations,
            0,
        ),
        _minimum(
            "multi_consumer_binding_cases",
            metrics.multi_consumer_binding_cases,
            cfg.minimum_multi_consumer_binding_cases,
        ),
        _maximum(
            "model_repair_prompt_disclosure_violations",
            metrics.model_repair_prompt_disclosure_violations,
            0,
        ),
        _minimum(
            "model_repair_prompt_disclosure_cases",
            metrics.model_repair_prompt_disclosure_cases,
            cfg.minimum_model_repair_prompt_disclosure_cases,
        ),
        _maximum(
            "malformed_output_schema_execution_escapes",
            metrics.malformed_output_schema_execution_escapes,
            0,
        ),
        _minimum(
            "malformed_output_schema_preflight_cases",
            metrics.malformed_output_schema_preflight_cases,
            cfg.minimum_malformed_output_schema_preflight_cases,
        ),
        _maximum(
            "constraint_evidence_integrity_errors",
            metrics.constraint_evidence_integrity_errors,
            0,
        ),
        _minimum(
            "constraint_evidence_integrity_cases",
            metrics.constraint_evidence_integrity_cases,
            cfg.minimum_constraint_evidence_integrity_cases,
        ),
        _maximum(
            "provider_authority_integrity_errors",
            metrics.provider_authority_integrity_errors,
            0,
        ),
        _minimum(
            "provider_authority_integrity_cases",
            metrics.provider_authority_integrity_cases,
            cfg.minimum_provider_authority_integrity_cases,
        ),
        _maximum(
            "provider_runtime_closure_errors",
            metrics.provider_runtime_closure_errors,
            0,
        ),
        _minimum(
            "provider_runtime_closure_cases",
            metrics.provider_runtime_closure_cases,
            cfg.minimum_provider_runtime_closure_cases,
        ),
        _maximum(
            "provider_dispatch_authority_errors",
            metrics.provider_dispatch_authority_errors,
            0,
        ),
        _minimum(
            "provider_dispatch_authority_cases",
            metrics.provider_dispatch_authority_cases,
            cfg.minimum_provider_dispatch_authority_cases,
        ),
        _maximum(
            "recovery_authority_continuity_errors",
            metrics.recovery_authority_continuity_errors,
            0,
        ),
        _minimum(
            "recovery_authority_continuity_cases",
            metrics.recovery_authority_continuity_cases,
            cfg.minimum_recovery_authority_continuity_cases,
        ),
        _maximum(
            "durable_retry_idempotency_errors",
            metrics.durable_retry_idempotency_errors,
            0,
        ),
        _minimum(
            "durable_retry_idempotency_cases",
            metrics.durable_retry_idempotency_cases,
            cfg.minimum_durable_retry_idempotency_cases,
        ),
        _maximum(
            "generic_sensitive_approval_bypasses",
            metrics.generic_sensitive_approval_bypasses,
            0,
        ),
        _minimum(
            "generic_sensitive_approval_cases",
            metrics.generic_sensitive_approval_cases,
            cfg.minimum_generic_sensitive_approval_cases,
        ),
        _maximum(
            "run_skill_output_false_successes",
            metrics.run_skill_output_false_successes,
            0,
        ),
        _minimum(
            "run_skill_output_projection_cases",
            metrics.run_skill_output_projection_cases,
            cfg.minimum_run_skill_output_projection_cases,
        ),
        _maximum(
            "native_synthesis_output_false_successes",
            metrics.native_synthesis_output_false_successes,
            0,
        ),
        _minimum(
            "native_synthesis_output_projection_cases",
            metrics.native_synthesis_output_projection_cases,
            cfg.minimum_native_synthesis_output_projection_cases,
        ),
        _maximum(
            "forged_provider_rejection_false_rejections",
            metrics.forged_provider_rejection_false_rejections,
            0,
        ),
        _minimum(
            "forged_provider_rejection_cases",
            metrics.forged_provider_rejection_cases,
            cfg.minimum_forged_provider_rejection_cases,
        ),
        _maximum(
            "durable_subtask_domain_failure_false_successes",
            metrics.durable_subtask_domain_failure_false_successes,
            0,
        ),
        _minimum(
            "durable_subtask_domain_failure_cases",
            metrics.durable_subtask_domain_failure_cases,
            cfg.minimum_durable_subtask_domain_failure_cases,
        ),
        _maximum(
            "failing_turn_fallback_losses",
            metrics.failing_turn_fallback_losses,
            0,
        ),
        _maximum(
            "failing_turn_fallback_duplicates",
            metrics.failing_turn_fallback_duplicates,
            0,
        ),
        _minimum(
            "failing_turn_fallback_cases",
            metrics.failing_turn_fallback_cases,
            cfg.minimum_failing_turn_fallback_cases,
        ),
        _maximum(
            "deterministic_workflow_false_applied",
            metrics.deterministic_workflow_false_applied,
            0,
        ),
        _minimum(
            "deterministic_workflow_steer_cases",
            metrics.deterministic_workflow_steer_cases,
            cfg.minimum_deterministic_workflow_steer_cases,
        ),
        _maximum(
            "deterministic_detached_steer_rejection_failures",
            metrics.deterministic_detached_steer_rejection_failures,
            0,
        ),
        _maximum(
            "deterministic_detached_steer_orphan_controls",
            metrics.deterministic_detached_steer_orphan_controls,
            0,
        ),
        _minimum(
            "deterministic_detached_steer_cases",
            metrics.deterministic_detached_steer_cases,
            cfg.minimum_deterministic_detached_steer_cases,
        ),
        _maximum(
            "steering_seal_reopen_violations",
            metrics.steering_seal_reopen_violations,
            0,
        ),
        _maximum(
            "steering_seal_orphan_controls",
            metrics.steering_seal_orphan_controls,
            0,
        ),
        _minimum(
            "steering_seal_cases",
            metrics.steering_seal_cases,
            cfg.minimum_steering_seal_cases,
        ),
        _maximum(
            "gateway_frame_revocation_escapes",
            metrics.gateway_frame_revocation_escapes,
            0,
        ),
        _minimum(
            "gateway_frame_revocation_cases",
            metrics.gateway_frame_revocation_cases,
            cfg.minimum_gateway_frame_revocation_cases,
        ),
        _maximum(
            "gateway_delegation_lease_violations",
            metrics.gateway_delegation_lease_violations,
            0,
        ),
        _minimum(
            "gateway_delegation_lease_cases",
            metrics.gateway_delegation_lease_cases,
            cfg.minimum_gateway_delegation_lease_cases,
        ),
        _maximum(
            "foreground_ack_retry_violations",
            metrics.foreground_ack_retry_violations,
            0,
        ),
        _maximum(
            "foreground_ack_losses",
            metrics.foreground_ack_losses,
            0,
        ),
        _maximum(
            "foreground_ack_duplicates",
            metrics.foreground_ack_duplicates,
            0,
        ),
        _minimum(
            "foreground_ack_fallback_cases",
            metrics.foreground_ack_fallback_cases,
            cfg.minimum_foreground_ack_fallback_cases,
        ),
        _maximum(
            "durable_control_requeue_violations",
            metrics.durable_control_requeue_violations,
            0,
        ),
        _minimum(
            "durable_control_requeue_cases",
            metrics.durable_control_requeue_cases,
            cfg.minimum_durable_control_requeue_cases,
        ),
        _maximum(
            "durable_control_live_owner_violations",
            metrics.durable_control_live_owner_violations,
            0,
        ),
        _minimum(
            "durable_control_live_owner_cases",
            metrics.durable_control_live_owner_cases,
            cfg.minimum_durable_control_live_owner_cases,
        ),
        _maximum(
            "durable_control_crash_recovery_losses",
            metrics.durable_control_crash_recovery_losses,
            0,
        ),
        _maximum(
            "durable_control_crash_recovery_duplicates",
            metrics.durable_control_crash_recovery_duplicates,
            0,
        ),
        _minimum(
            "durable_control_crash_recovery_cases",
            metrics.durable_control_crash_recovery_cases,
            cfg.minimum_durable_control_crash_recovery_cases,
        ),
        _maximum(
            "durable_control_legacy_stale_violations",
            metrics.durable_control_legacy_stale_violations,
            0,
        ),
        _minimum(
            "durable_control_legacy_stale_cases",
            metrics.durable_control_legacy_stale_cases,
            cfg.minimum_durable_control_legacy_stale_cases,
        ),
        _maximum(
            "planning_latency_regression",
            metrics.planning_latency_regression,
            cfg.maximum_planning_latency_regression,
        ),
        _minimum(
            "planning_benchmark_samples",
            provenance.benchmark_samples,
            cfg.minimum_planning_benchmark_samples,
        ),
    )
    return ReactiveBindingGateReport(
        metrics=metrics,
        repair_budget=repair_budget,
        criteria=cfg,
        provenance=provenance,
        checks=checks,
    )


def initialize_fail_closed_report(
    path: str | os.PathLike[str],
    *,
    failure_stage: str,
) -> None:
    """Create the report before evaluation so missing evidence never looks green."""
    _write_json_atomic(
        path,
        {
            "schema": "omni.reactive-binding-release-gate.v1",
            "passed": False,
            "failed_checks": ["release_gate_incomplete"],
            "failure_stage": str(failure_stage or "unknown"),
        },
    )


def write_release_gate_report(
    path: str | os.PathLike[str],
    report: ReactiveBindingGateReport,
) -> None:
    """Atomically replace the fail-closed placeholder with a complete report."""
    _write_json_atomic(path, report.to_dict())


def _write_json_atomic(
    path: str | os.PathLike[str],
    payload: dict[str, Any],
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _rate(numerator: int | float, denominator: int, *, empty: float = 0.0) -> float:
    return float(numerator) / denominator if denominator else empty


def _minimum(name: str, actual: int | float, threshold: int | float) -> ReleaseGateCheck:
    return ReleaseGateCheck(name, actual >= threshold, actual, ">=", threshold)


def _maximum(name: str, actual: int | float, threshold: int | float) -> ReleaseGateCheck:
    return ReleaseGateCheck(name, actual <= threshold, actual, "<=", threshold)


def _rounded(value: int | float) -> int | float:
    return round(value, 8) if isinstance(value, float) else value


__all__ = [
    "BindingCaseEvidence",
    "ModelRepairBudget",
    "ReactiveBindingCriteria",
    "ReactiveBindingGateReport",
    "ReactiveBindingMetrics",
    "ReactiveBindingProvenance",
    "ReleaseGateCheck",
    "aggregate_reactive_binding_metrics",
    "evaluate_reactive_binding_release",
    "initialize_fail_closed_report",
    "write_release_gate_report",
]
