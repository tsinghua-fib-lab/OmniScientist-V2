"""Canonical tool transcripts stay provider-valid across interruptions."""

from __future__ import annotations

from omni.core.tool_transcript import normalize_tool_transcript


def test_missing_tool_results_are_closed_with_structured_aborted_outputs() -> None:
    normalized = normalize_tool_transcript(
        [
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-a",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                    {
                        "id": "call-b",
                        "type": "function",
                        "function": {"name": "glob", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-a",
                "name": "read_file",
                "content": "ok",
            },
            {"role": "user", "content": "continue"},
        ]
    )

    result_ids = [
        message["tool_call_id"]
        for message in normalized.messages
        if message.get("role") == "tool"
    ]
    assert result_ids == ["call-a", "call-b"]
    repaired = next(
        message for message in normalized.messages if message.get("tool_call_id") == "call-b"
    )
    assert '"status": "aborted"' in repaired["content"]
    assert normalized.valid is True
    assert normalized.repairs == ["missing_tool_result:call-b"]


def test_orphan_and_duplicate_tool_results_are_removed() -> None:
    normalized = normalize_tool_transcript(
        [
            {"role": "user", "content": "hello"},
            {"role": "tool", "tool_call_id": "orphan", "content": "bad"},
        ]
    )

    assert normalized.messages == [{"role": "user", "content": "hello"}]
    assert normalized.repairs == ["orphan_tool_result:orphan"]
    assert normalized.valid is True


def test_reused_call_id_is_renamed_without_losing_its_result() -> None:
    normalized = normalize_tool_transcript(
        [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call-a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call-a", "content": "first"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call-a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call-a", "content": "second"},
        ]
    )

    results = [message for message in normalized.messages if message.get("role") == "tool"]
    assert [message["content"] for message in results] == ["first", "second"]
    assert results[0]["tool_call_id"] == "call-a"
    assert results[1]["tool_call_id"].startswith("omni_call_")
    assert normalized.valid is True


def test_duplicate_ids_in_one_batch_preserve_result_order_and_names() -> None:
    normalized = normalize_tool_transcript(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "same",
                        "type": "function",
                        "function": {"name": "first_tool", "arguments": "{}"},
                    },
                    {
                        "id": "same",
                        "type": "function",
                        "function": {"name": "second_tool", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "same", "name": "first_tool", "content": "first"},
            {"role": "tool", "tool_call_id": "same", "name": "second_tool", "content": "second"},
        ]
    )

    calls = normalized.messages[0]["tool_calls"]
    results = [message for message in normalized.messages if message.get("role") == "tool"]
    assert [message["tool_call_id"] for message in results] == [call["id"] for call in calls]
    assert [message["name"] for message in results] == ["first_tool", "second_tool"]
    assert [message["content"] for message in results] == ["first", "second"]
    assert not any(repair.startswith("orphan_tool_result") for repair in normalized.repairs)
    assert not any(repair.startswith("missing_tool_result") for repair in normalized.repairs)
    assert normalized.valid is True
