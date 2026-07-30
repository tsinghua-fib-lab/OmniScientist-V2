"""Generic Action-admission vocabulary: the domain-agnostic contract types."""

from __future__ import annotations

from omni.core.action_contracts import (
    ActionDecision,
    ActionDecisionStatus,
    EffectKind,
    ProvenanceKind,
    ResolutionResult,
    ResolutionStatus,
    provenance_trusted_for_critical,
)


def test_bare_model_assumption_cannot_back_a_critical_field():
    # Every enumerated trusted provenance is accepted…
    for kind in (
        ProvenanceKind.USER_EVIDENCE,
        ProvenanceKind.HOST_CONTEXT,
        ProvenanceKind.POLICY_DEFAULT,
        ProvenanceKind.USER_CONFIRMED,
        ProvenanceKind.COMPUTED,
    ):
        assert provenance_trusted_for_critical(kind)
    # …and a bare model guess (or any unknown token) is fail-closed.
    assert not provenance_trusted_for_critical("model_assumption")
    assert not provenance_trusted_for_critical("nonsense")


def test_effect_kind_is_composed_as_a_set():
    schedule_effects = frozenset({EffectKind.STATE_CHANGE, EffectKind.DEFERRED, EffectKind.PERSISTENT})
    assert EffectKind.DEFERRED in schedule_effects
    assert EffectKind.DESTRUCTIVE not in schedule_effects


def test_action_decision_ready_carries_canonical_arguments():
    decision = ActionDecision.ready_with({"trigger": {"kind": "once"}})
    assert decision.ready
    assert decision.status is ActionDecisionStatus.READY
    assert decision.canonical_arguments == {"trigger": {"kind": "once"}}


def test_action_decision_needs_input_carries_resolution_not_arguments():
    resolution = ResolutionResult(status=ResolutionStatus.AMBIGUOUS, reason="AM vs PM")
    decision = ActionDecision.needs_input_with(resolution)
    assert decision.needs_input
    assert decision.canonical_arguments is None
    assert decision.resolution is resolution
    assert decision.reason == "AM vs PM"


def test_action_decision_rejected():
    decision = ActionDecision.rejected_with("untrusted provenance")
    assert decision.status is ActionDecisionStatus.REJECTED
    assert decision.canonical_arguments is None
