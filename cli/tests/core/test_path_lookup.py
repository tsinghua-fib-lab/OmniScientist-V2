"""Quote-equivalent path lookup (BUG-20) and fail-closed misses."""

from __future__ import annotations

from pathlib import Path

from omni.core.path_lookup import (
    fold_quote_marks,
    missing_path_message,
    resolve_existing_path,
    unwrap_matching_quotes,
)


def test_fold_equates_curly_and_ascii_quotes() -> None:
    assert fold_quote_marks("报告“初稿”.md") == fold_quote_marks('报告"初稿".md')
    assert fold_quote_marks("it’s") == fold_quote_marks("it's")


def test_unwrap_matching_curly_and_ascii_wrappers() -> None:
    assert unwrap_matching_quotes('"C:/work/p.pdf"') == "C:/work/p.pdf"
    assert unwrap_matching_quotes("“C:/work/p.pdf”") == "C:/work/p.pdf"
    assert unwrap_matching_quotes("bare") == "bare"


def test_resolve_finds_curly_quoted_name_from_ascii_spelling(tmp_path: Path) -> None:
    real = tmp_path / "报告“初稿”.md"
    real.write_text("ok", encoding="utf-8")
    asked = tmp_path / '报告"初稿".md'

    found = resolve_existing_path(asked)

    assert found == real


def test_resolve_finds_ascii_name_from_curly_spelling(tmp_path: Path) -> None:
    real = tmp_path / 'note "draft".txt'
    try:
        real.write_text("ok", encoding="utf-8")
    except OSError:
        return
    asked = tmp_path / "note “draft”.txt"

    found = resolve_existing_path(str(asked))

    assert found == real


def test_resolve_walks_quoted_directory_components(tmp_path: Path) -> None:
    folder = tmp_path / "项目“A”"
    folder.mkdir()
    real = folder / "notes.md"
    real.write_text("ok", encoding="utf-8")
    asked = tmp_path / '项目"A"' / "notes.md"

    found = resolve_existing_path(asked)

    assert found == real


def test_ambiguous_quote_equivalents_fail_closed(tmp_path: Path) -> None:
    curly = tmp_path / "报告“初稿”.md"
    curly.write_text("curly", encoding="utf-8")
    ascii_name = tmp_path / '报告"初稿".md'
    try:
        ascii_name.write_text("ascii", encoding="utf-8")
    except OSError:
        return
    if not ascii_name.exists() or ascii_name == curly:
        return
    # A third spelling that folds to the same name must not pick a winner.
    assert resolve_existing_path(tmp_path / "报告‘初稿’.md") is None


def test_missing_path_message_forbids_quote_retries(tmp_path: Path) -> None:
    path = tmp_path / "missing.md"
    message = missing_path_message(path, next_step="Use list_dir.")
    assert str(path) in message
    assert "do not rewrite quotation marks" in message
    assert "Do not retry" in message
    assert "Use list_dir." in message
