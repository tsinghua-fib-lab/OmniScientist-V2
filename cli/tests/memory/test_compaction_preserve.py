from __future__ import annotations

from omni.memory.compaction import (
    format_rollover_checkpoint,
    microcompact_tool_results,
    parse_rollover_checkpoint,
)


def test_microcompact_keeps_failed_observation_verbatim() -> None:
    failed = (
        '{"status":"failed","error_class":"invalid_args","error":"old_string must occur once"}'
        + ("x" * 800)
    )
    messages = [
        {"role": "tool", "content": "ok source_id=abc12345 [S1] " + ("y" * 800)},
        {"role": "tool", "content": failed},
        {"role": "tool", "content": "recent"},
        {"role": "tool", "content": "latest"},
    ]
    trimmed = microcompact_tool_results(messages, keep_last=2, max_chars=80)
    assert trimmed == 1
    assert messages[1]["content"] == failed
    assert "source_id=abc12345" in messages[0]["content"]
    assert "[S1]" in messages[0]["content"]
    assert "preserved:" in messages[0]["content"]


def test_rollover_schema_roundtrip() -> None:
    parsed = parse_rollover_checkpoint(
        """
        {
          "objective": "write the RAG survey",
          "verified_findings": [{"tool": "search_literature", "source_id": "srcABC", "text": "Transformer"}],
          "unpaid_deliverables": ["draft.manuscript"],
          "next_action": "cite_source then write_file"
        }
        """
    )
    assert parsed is not None
    assert parsed["objective"] == "write the RAG survey"
    assert parsed["verified_findings"][0]["source_id"] == "srcABC"
    text = format_rollover_checkpoint(parsed)
    assert "source_id=srcABC" in text
    assert "draft.manuscript" in text


def test_invalid_rollover_does_not_parse() -> None:
    assert parse_rollover_checkpoint("just a paragraph of notes") is None
    assert parse_rollover_checkpoint('{"objective":"x"}') is None
