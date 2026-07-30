"""Shared execution-outcome semantics across the agent runtime."""

from __future__ import annotations

from omni.core.termination import (
    aggregate_outcome_status,
    execution_outcome_status,
    is_bounded_termination,
)


def test_synthesized_bounded_termination_is_degraded() -> None:
    assert execution_outcome_status("text", "synthesized_max_tool_calls") == "degraded"
    assert execution_outcome_status("text", "synthesized_max_iterations") == "degraded"
    assert execution_outcome_status("text", "synthesized_no_progress") == "degraded"


def test_wall_clock_stops_are_bounded_degraded_not_failed() -> None:
    # A time layer (overall ceiling or the progress watchdog) is a *bounded*
    # stop: the loop synthesizes over what it has and settles degraded. Only a
    # genuine provider hang with zero results stays a hard failure.
    assert is_bounded_termination("timeout")
    assert is_bounded_termination("stalled")
    assert is_bounded_termination("synthesized_timeout")
    assert is_bounded_termination("synthesized_stalled")
    assert execution_outcome_status("partial", "timeout") == "degraded"
    assert execution_outcome_status("text", "synthesized_stalled") == "degraded"
    assert execution_outcome_status("error", "llm_timeout") == "failed"


def test_normal_completion_at_exact_budget_is_succeeded() -> None:
    assert execution_outcome_status("text", "done") == "succeeded"


def test_user_cancellation_is_not_misclassified_as_degraded() -> None:
    assert execution_outcome_status("partial", "cancelled") == "cancelled"


def test_outcome_aggregation_uses_failed_degraded_succeeded_precedence() -> None:
    assert aggregate_outcome_status("succeeded", "degraded") == "degraded"
    assert aggregate_outcome_status("degraded", "failed", "succeeded") == "failed"
    assert aggregate_outcome_status("succeeded", "succeeded") == "succeeded"
