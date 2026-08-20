"""``@path`` mention parsing: word boundaries, quoting, and grant hygiene.

The parser is the authorization surface for attachments, so the cases that
matter most are the *negative* ones: prose that merely contains ``@`` must never
become a read grant, and a mention that resolves to nothing must never be
reported as attachable.
"""

from __future__ import annotations

from pathlib import Path

from omni.core.file_mentions import (
    format_mention,
    iter_mention_tokens,
    mention_file_uris,
    parse_mentions,
    resolve_turn_attachments,
    strip_mention_marker,
)


def test_relative_mention_resolves_against_cwd(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    mentions = parse_mentions("review @README.md please", cwd=tmp_path)
    assert len(mentions) == 1
    assert mentions[0].path == (tmp_path / "README.md").resolve()
    assert mentions[0].exists is True


def test_absolute_mention_is_kept(tmp_path: Path) -> None:
    target = tmp_path / "paper.md"
    target.write_text("body", encoding="utf-8")
    mentions = parse_mentions(f"review @{target}", cwd=Path.cwd())
    assert [m.path for m in mentions] == [target.resolve()]


def test_email_and_git_revision_are_not_mentions(tmp_path: Path) -> None:
    text = "mail user@example.com about HEAD@{0} and a@b"
    assert list(iter_mention_tokens(text)) == []
    assert parse_mentions(text, cwd=tmp_path) == []


def test_curly_quoted_mention_supports_whitespace(tmp_path: Path) -> None:
    folder = tmp_path / "my docs"
    folder.mkdir()
    target = folder / "a b.md"
    target.write_text("x", encoding="utf-8")
    mentions = parse_mentions("summarize @“my docs/a b.md” now", cwd=tmp_path)
    assert [m.path for m in mentions] == [target.resolve()]


def test_mention_resolves_curly_filename_from_ascii_spelling(tmp_path: Path) -> None:
    target = tmp_path / "报告“初稿”.md"
    target.write_text("x", encoding="utf-8")
    asked = tmp_path / '报告"初稿".md'
    mentions = parse_mentions(f"read @{asked}", cwd=tmp_path)
    assert [m.path for m in mentions] == [target.resolve()]


def test_quoted_mention_supports_whitespace(tmp_path: Path) -> None:
    folder = tmp_path / "my docs"
    folder.mkdir()
    target = folder / "a b.md"
    target.write_text("x", encoding="utf-8")
    mentions = parse_mentions('summarize @"my docs/a b.md" now', cwd=tmp_path)
    assert [m.path for m in mentions] == [target.resolve()]


def test_trailing_punctuation_is_trimmed_only_when_it_helps(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    weird = tmp_path / "odd,.md"
    weird.write_text("x", encoding="utf-8")

    trimmed = parse_mentions("read @notes.md, then plot", cwd=tmp_path)
    assert [m.path for m in trimmed] == [(tmp_path / "notes.md").resolve()]

    # A filename that genuinely contains the punctuation still resolves literally.
    literal = parse_mentions("read @odd,.md", cwd=tmp_path)
    assert [m.path for m in literal] == [weird.resolve()]


def test_sentence_final_period_is_recovered(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("x", encoding="utf-8")
    mentions = parse_mentions("start from @plan.md.", cwd=tmp_path)
    assert [m.path for m in mentions] == [(tmp_path / "plan.md").resolve()]


def test_multiple_mentions_preserve_order_and_dedupe(tmp_path: Path) -> None:
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    mentions = parse_mentions("@a.md then @b.md and @a.md again", cwd=tmp_path)
    assert [m.raw for m in mentions] == ["a.md", "b.md"]


def test_missing_mention_is_reported_but_never_granted(tmp_path: Path) -> None:
    mentions = parse_mentions("review @nope.md", cwd=tmp_path)
    assert len(mentions) == 1
    assert mentions[0].exists is False
    # The grant list only carries paths that are really there.
    assert mention_file_uris("review @nope.md", cwd=tmp_path) == []


def test_parent_traversal_is_normalised(tmp_path: Path) -> None:
    nested = tmp_path / "sub"
    nested.mkdir()
    target = tmp_path / "top.md"
    target.write_text("x", encoding="utf-8")
    mentions = parse_mentions("@sub/../top.md", cwd=tmp_path)
    assert [m.path for m in mentions] == [target.resolve()]


def test_bare_at_is_ignored(tmp_path: Path) -> None:
    assert parse_mentions("what about @ this", cwd=tmp_path) == []


def test_directory_mention_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "corpus").mkdir()
    mentions = parse_mentions("@corpus", cwd=tmp_path)
    assert mentions[0].is_dir is True


def test_attachments_merge_retry_snapshot_with_new_mentions(tmp_path: Path) -> None:
    (tmp_path / "new.md").write_text("x", encoding="utf-8")
    resolved = resolve_turn_attachments(
        "also read @new.md", cwd=tmp_path, extra=["file:///kept/from/retry.md"]
    )
    assert resolved.file_uris == [
        "file:///kept/from/retry.md",
        str((tmp_path / "new.md").resolve()),
    ]
    assert resolved.missing == []


def test_attachments_are_none_when_nothing_is_mentioned(tmp_path: Path) -> None:
    # Callers keep passing their existing default through untouched.
    assert resolve_turn_attachments("just a question", cwd=tmp_path).file_uris is None


def test_attachments_report_typos_instead_of_attaching_nothing(tmp_path: Path) -> None:
    (tmp_path / "real.md").write_text("x", encoding="utf-8")
    resolved = resolve_turn_attachments("@real.md and @typo.md", cwd=tmp_path)
    assert resolved.file_uris == [str((tmp_path / "real.md").resolve())]
    assert resolved.missing == ["typo.md"]


def test_format_mention_quotes_whitespace() -> None:
    assert format_mention("/tmp/notes.md") == "@/tmp/notes.md"
    assert format_mention("/tmp/OmniScientist Cli.pdf") == '@"/tmp/OmniScientist Cli.pdf"'


def test_strip_mention_marker_tolerates_model_copy_paste() -> None:
    assert strip_mention_marker("@notes.md") == "notes.md"
    assert strip_mention_marker("notes.md") == "notes.md"
    assert strip_mention_marker("  @a/b.md  ") == "a/b.md"
    assert strip_mention_marker("") == ""
