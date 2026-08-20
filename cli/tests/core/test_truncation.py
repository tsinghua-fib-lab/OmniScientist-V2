"""Codex-style head/tail truncation keeps both ends and names the original size."""

from __future__ import annotations

from omni.core.truncation import (
    approx_token_count,
    formatted_truncate_text,
    truncate_middle_chars,
)


def test_approx_token_count_matches_codex_four_bytes_per_token() -> None:
    assert approx_token_count("abcd") == 1
    assert approx_token_count("abcde") == 2
    assert approx_token_count("😀") == 1  # four UTF-8 bytes


def test_truncate_middle_chars_keeps_head_and_tail() -> None:
    text = "HEAD" + ("M" * 200) + "TAIL"
    out = truncate_middle_chars(text, 48)
    assert len(out) <= 48
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    removed = _removed(out)
    marker = f"…{removed} chars truncated…"
    head, tail = out.split(marker)
    assert len(head) + removed + len(tail) == len(text)


def test_truncate_middle_chars_is_a_no_op_when_under_budget() -> None:
    assert truncate_middle_chars("short output", 100) == "short output"


def test_truncate_middle_chars_does_not_split_a_code_point() -> None:
    text = "😀" * 30
    out = truncate_middle_chars(text, 24)
    assert len(out) <= 24
    assert "\ufffd" not in out
    assert out.startswith("😀")
    assert out.endswith("😀")
    assert "chars truncated" in out


def test_formatted_truncate_text_stamps_original_token_count_and_line_count() -> None:
    text = "alpha\n" + ("body " * 80) + "\nomega"
    out = formatted_truncate_text(text, 160)
    assert len(out) <= 160
    assert f"original token count: {approx_token_count(text)}" in out
    assert f"Total output lines: {len(text.splitlines())}" in out
    assert out.startswith("Warning: truncated output")
    assert "alpha" in out
    assert "omega" in out
    assert "chars truncated" in out


def test_formatted_truncate_text_budgets_the_footer_first() -> None:
    text = "HEAD" + ("M" * 400) + "TAIL"
    footer = "\n\nFull source_ids saved to: /tmp/source_ids-deadbeef.txt"
    out = formatted_truncate_text(text, 180, footer=footer)
    assert len(out) <= 180
    assert out.endswith(footer)
    assert "HEAD" in out
    assert "TAIL" in out
    assert "original token count" in out


def test_formatted_truncate_text_returns_original_when_it_fits() -> None:
    assert formatted_truncate_text("already short", 80) == "already short"


def _removed(text: str) -> int:
    start = text.index("…") + 1
    end = text.index(" chars truncated…")
    return int(text[start:end])
