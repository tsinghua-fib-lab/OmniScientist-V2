from __future__ import annotations

from omni.core.approval import denied_result
from omni.core.tool_policy import policy_violation
from omni.core.tool_result import (
    COMMAND_RESULT_SCHEMA,
    HostToolRejection,
    ToolResultEnvelope,
    _mint_host_tool_rejection,
    command_result_status,
    is_tool_rejection,
    tool_event_output,
    tool_event_suffix,
    tool_observation,
    tool_rejection_error,
    tool_result_failure,
    tool_transport_status,
)


def test_tool_result_envelope_projects_model_and_event_views() -> None:
    event_output = {
        "result_schema": "omni.command-result.v1",
        "command_status": "failed",
        "exit_code": 1,
    }
    result = ToolResultEnvelope(
        observation="[exit=1]\n",
        event_output=event_output,
    )

    assert tool_observation(result) == "[exit=1]\n"
    assert tool_event_output(result) is event_output


def test_tool_result_projections_preserve_legacy_results() -> None:
    legacy_dict = {"status": "ok"}
    legacy_text = "done"

    assert tool_observation(legacy_dict) is legacy_dict
    assert tool_event_output(legacy_dict) is legacy_dict
    assert tool_observation(legacy_text) is legacy_text
    assert tool_event_output(legacy_text) is legacy_text


def test_command_result_status_only_accepts_the_owned_versioned_schema() -> None:
    assert command_result_status(
        {"result_schema": COMMAND_RESULT_SCHEMA, "command_status": "failed"}
    ) == "failed"
    assert command_result_status(
        {"result_schema": "external.command-result.v1", "command_status": "failed"}
    ) is None
    assert command_result_status(
        {"result_schema": COMMAND_RESULT_SCHEMA, "command_status": "future_status"}
    ) is None


def test_tool_rejection_requires_the_host_private_marker() -> None:
    assert is_tool_rejection(denied_result("bash", "owner denied")) is True
    assert is_tool_rejection(policy_violation("bash", "blocked")) is True
    assert is_tool_rejection(
        HostToolRejection({"approval_required": True})
    ) is False
    assert is_tool_rejection(
        HostToolRejection({"policy_violation": True})
    ) is False
    assert is_tool_rejection({"approval_required": True}) is False
    assert is_tool_rejection({"policy_violation": True}) is False
    assert is_tool_rejection({"command_status": "blocked"}) is False


def test_tool_event_suffix_preserves_transport_lifecycle_meaning() -> None:
    assert tool_event_suffix("succeeded") == "done"
    assert tool_event_suffix("rejected") == "rejected"
    assert tool_event_suffix("failed") == "failed"
    assert tool_event_suffix("cancelled") == "failed"
    assert tool_event_suffix("timed_out") == "failed"


def test_tool_transport_status_rejects_success_with_an_error() -> None:
    assert tool_transport_status("succeeded", "boom") == "failed"
    assert tool_transport_status("", "boom") == "failed"
    assert tool_transport_status("rejected", "owner denied") == "rejected"
    assert tool_transport_status("cancelled", "cancelled by user") == "cancelled"
    assert tool_transport_status("timed_out", "deadline") == "timed_out"
    assert tool_transport_status("", "") == "succeeded"


def test_tool_rejection_error_preserves_reason_only_payloads() -> None:
    assert tool_rejection_error(
        _mint_host_tool_rejection(
            {"approval_required": True, "reason": "owner denied"}
        )
    ) == "owner denied"
    assert tool_rejection_error(
        _mint_host_tool_rejection(
            {"policy_violation": True, "reason": "blocked by plan"}
        )
    ) == "blocked by plan"
    assert tool_rejection_error(
        HostToolRejection({"approval_required": True})
    ) == ""
    assert tool_rejection_error({"approval_required": True}) == ""
    assert tool_rejection_error({"reason": "not a rejection"}) == ""


def test_explicit_domain_failures_are_not_mistaken_for_transport_success() -> None:
    assert tool_result_failure(
        {"status": "error", "error": "source unavailable"}
    ) == ("failed", "source unavailable")
    assert tool_result_failure(
        {"status": "cancelled", "summary": "stopped by user"}
    ) == ("cancelled", "stopped by user")
    assert tool_result_failure({"status": "ok"}) is None


def test_command_outcome_remains_separate_from_transport_status() -> None:
    assert tool_result_failure(
        {
            "result_schema": COMMAND_RESULT_SCHEMA,
            "command_status": "failed",
            "status": "error",
            "exit_code": 1,
        }
    ) is None
