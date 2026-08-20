from __future__ import annotations

from omni.core.approval import denied_result
from omni.core.tool_policy import policy_violation
from omni.core.tool_result import (
    COMMAND_RESULT_SCHEMA,
    HostToolRejection,
    ToolCallOutcome,
    ToolResultEnvelope,
    _mint_host_tool_rejection,
    attach_tool_outcome,
    command_exit_summary,
    command_failure_hint,
    command_output_window,
    command_result_status,
    first_command_output_line,
    fs_result_outcome,
    is_tool_rejection,
    owned_result_outcome,
    tool_call_outcome,
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


def test_untrusted_result_status_is_domain_data_not_invocation_authority() -> None:
    for status in ("error", "failed", "cancelled", "timed_out", "blocked", "invalid"):
        assert tool_result_failure(
            {"status": status, "summary": "historical object state"}
        ) is None


def test_typed_unsuccessful_result_retains_invocation_authority() -> None:
    output = {"status": "error", "error": "source unavailable"}
    result = ToolResultEnvelope(
        observation='{"status":"error","error":"source unavailable"}',
        event_output=output,
        outcome=ToolCallOutcome.completed(
            success=False,
            error="source unavailable",
        ),
    )

    assert tool_result_failure(result) == ("failed", "source unavailable")
    assert tool_event_output(result) is output


def test_fs_policy_denial_is_blocked_not_succeeded() -> None:
    denied = (
        "ERROR: read denied because the path is outside the accessible roots: "
        "/Users/antonio/.omni/cache/spillover/source_ids-deadbeef.txt. "
        "Accessible roots: /tmp/project."
    )
    wrapped = attach_tool_outcome(denied, fs_result_outcome(denied))
    outcome = tool_call_outcome(wrapped)

    assert outcome.lifecycle == "blocked"
    assert outcome.result_success is not True
    assert tool_result_failure(wrapped) == ("rejected", denied)


def test_fs_missing_path_is_completed_failure() -> None:
    missing = "ERROR: path does not exist: /tmp/project/nope.md"
    outcome = fs_result_outcome(missing)

    assert outcome is not None
    assert outcome.lifecycle == "completed"
    assert outcome.result_success is False
    assert tool_result_failure(attach_tool_outcome(missing, outcome)) == (
        "failed",
        missing,
    )


def test_owned_result_resolver_preserves_declared_cancellation() -> None:
    outcome = owned_result_outcome(
        {"status": "cancelled", "summary": "child job was cancelled"}
    )

    assert outcome is not None
    assert outcome.lifecycle == "aborted"
    assert outcome.result_success is None
    assert outcome.error == "child job was cancelled"


def test_command_exit_summary_keeps_the_process_error_line() -> None:
    assert command_exit_summary(128) == "Command exited with code 128"
    assert first_command_output_line("\n  \n致命错误：不是 Git 仓库（或者任何父目录）：.git\n") == (
        "致命错误：不是 Git 仓库（或者任何父目录）：.git"
    )
    assert command_exit_summary(
        128, "致命错误：不是 Git 仓库（或者任何父目录）：.git\n"
    ) == "Command exited with code 128: 致命错误：不是 Git 仓库（或者任何父目录）：.git"


def test_command_failure_hint_prefers_stderr_and_skips_progress() -> None:
    assert command_failure_hint(
        126,
        "[ 36%]\n./nope: Permission denied\n",
        "./nope: Permission denied\n",
    ) == "./nope: Permission denied"
    assert command_failure_hint(126, "[ 36%]\n") == "cannot execute"
    assert command_failure_hint(127, "") == "command not found"
    assert command_exit_summary(126, "[ 36%]\n") == (
        "Command exited with code 126: cannot execute"
    )


def test_command_output_window_keeps_head_and_tail() -> None:
    text = "HEAD" + ("." * 40) + "TAIL"
    window = command_output_window(text, 16, tail_weight=2)
    assert window.startswith("HEAD")
    assert window.endswith("TAIL")
    assert "…" in window
    assert len(window) <= 16


def test_command_outcome_remains_separate_from_transport_status() -> None:
    assert tool_result_failure(
        {
            "result_schema": COMMAND_RESULT_SCHEMA,
            "command_status": "failed",
            "status": "error",
            "exit_code": 1,
        }
    ) is None
