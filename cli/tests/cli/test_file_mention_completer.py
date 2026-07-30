"""The ``@`` completion surface: when it fires, and what it inserts.

Two behaviours carry the design. The completer must fire on a mention written
*mid-sentence* (unlike the slash surface, which only owns a line prefix), and it
must replace only the typed token so the ``@`` marker survives into the
submitted text — that marker is the explicit attachment grant.
"""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from omni.cli.file_search import FileSearcher
from omni.cli.repl_input import FileMentionCompleter, build_repl_completer


def _completer(root: Path) -> FileMentionCompleter:
    return FileMentionCompleter(searcher=FileSearcher(root))


def _complete(completer: FileMentionCompleter, text: str) -> list:
    document = Document(text, cursor_position=len(text))
    return list(completer.get_completions(document, CompleteEvent()))


def test_mention_completes_mid_sentence(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    results = _complete(_completer(tmp_path), "review @READ")
    assert [c.text for c in results] == ["README.md"]
    # Only the typed token is replaced, so the leading ``@`` stays in the buffer.
    assert results[0].start_position == -len("READ")


def test_no_at_token_yields_nothing(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    assert _complete(_completer(tmp_path), "review the readme") == []


def test_email_never_triggers_completion(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    assert _complete(_completer(tmp_path), "mail me at user@example.com") == []


def test_bare_at_offers_an_overview(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("x", encoding="utf-8")
    assert {c.text for c in _complete(_completer(tmp_path), "look at @")} == {"a.md", "b.md"}


def test_finished_mention_stops_completing(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    # Whitespace ends an unquoted mention: the cursor is past it now.
    assert _complete(_completer(tmp_path), "@a.md and then") == []


def test_directory_completes_with_slash_for_navigation(tmp_path: Path) -> None:
    nested = tmp_path / "corpus"
    nested.mkdir()
    (nested / "paper.md").write_text("x", encoding="utf-8")
    inserted = {c.text for c in _complete(_completer(tmp_path), "@corpus")}
    assert "corpus/" in inserted


def test_whitespace_paths_are_quoted(tmp_path: Path) -> None:
    folder = tmp_path / "my docs"
    folder.mkdir()
    (folder / "note.md").write_text("x", encoding="utf-8")
    inserted = {c.text for c in _complete(_completer(tmp_path), "@my")}
    assert any(text.startswith('"') for text in inserted)


def test_quoted_mention_is_closed_not_doubled(tmp_path: Path) -> None:
    folder = tmp_path / "my docs"
    folder.mkdir()
    (folder / "note.md").write_text("x", encoding="utf-8")
    results = _complete(_completer(tmp_path), '@"my docs/no')
    assert [c.text for c in results] == ['my docs/note.md"']


def test_sensitive_files_are_not_completable(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    assert _complete(_completer(tmp_path), "@.en") == []
    assert _complete(_completer(tmp_path), "@") == []


def test_merged_completer_keeps_both_surfaces(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    completer = build_repl_completer(["help", "task"], root=tmp_path)
    # Threaded completers expose the async API; the sync one delegates too.
    slash = Document("/hel", cursor_position=4)
    assert [c.text for c in completer.get_completions(slash, CompleteEvent())] == ["help"]
    mention = Document("review @READ", cursor_position=12)
    assert [c.text for c in completer.get_completions(mention, CompleteEvent())] == ["README.md"]
