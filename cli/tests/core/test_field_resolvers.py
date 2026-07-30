"""Fact resolution and grounded lookup share one resolver registry."""

import os

import pytest

from omni.core.field_resolvers import (
    has_resolver,
    has_searcher,
    resolve_field,
    search_field_candidates,
)


def test_fact_resolver_registry_distinguishes_parse_only_and_lookup_adapters() -> None:
    assert has_resolver("arxiv-id")
    assert has_searcher("arxiv-id")
    assert has_resolver("doi")
    assert not has_searcher("doi")
    assert resolve_field("doi", {"identifier": "doi:10.1000/example"}).value == (
        "10.1000/example"
    )


def test_at_attachment_with_spaces_resolves_longest_existing_path(tmp_path) -> None:
    paper = tmp_path / "Worldlines in a Real Town.pdf"
    paper.write_bytes(b"%PDF-test")

    resolved = resolve_field(
        "local-file-or-text",
        {"input": f"@{paper} 请审稿"},
    )

    assert resolved.resolved is True
    assert resolved.value == str(paper.resolve())


def test_a_drive_letter_is_read_as_a_path_not_a_url_scheme(tmp_path, monkeypatch) -> None:
    """``urlparse`` calls the "C" in "C:\\work\\p.pdf" a scheme.

    The resolver rejects anything with a scheme, because that is how an http
    URL is kept out — so on Windows every absolute path was discarded before it
    was ever looked for: an attached paper resolved to nothing, and a path the
    user had just typed could not be proved to exist.

    Windows hands us the real article: ``tmp_path`` is already on a drive. POSIX
    has no drives but does permit a colon in a filename, so a directory actually
    named "C:" reproduces the same parse there — which is the only way this was
    covered before, and it cannot run on Windows at all, because pathlib reads
    the "C:" being appended as the drive it is already on and hands back the
    parent unchanged.
    """
    if os.name == "nt":
        paper = tmp_path / "paper.pdf"
        paper.write_bytes(b"%PDF-test")
        typed = str(paper)
    else:
        drive = tmp_path / "C:"
        drive.mkdir()
        paper = drive / "paper.pdf"
        paper.write_bytes(b"%PDF-test")
        monkeypatch.chdir(tmp_path)
        typed = "C:/paper.pdf"

    resolved = resolve_field("file-path", {"input": typed})

    assert resolved.resolved is True
    assert resolved.value == str(paper.resolve())


def test_curly_quotes_in_filename_resolve_from_ascii_spelling(tmp_path) -> None:
    paper = tmp_path / "论文“终稿”.pdf"
    paper.write_bytes(b"%PDF-test")
    asked = tmp_path / '论文"终稿".pdf'

    resolved = resolve_field("file-path", {"input": str(asked)})

    assert resolved.resolved is True
    assert resolved.value == str(paper.resolve())


def test_curly_quoted_attachment_resolves(tmp_path) -> None:
    paper = tmp_path / "Worldlines in a Real Town.pdf"
    paper.write_bytes(b"%PDF-test")

    resolved = resolve_field(
        "local-file-or-text",
        {"input": f"@“{paper}” 请审稿"},
    )

    assert resolved.resolved is True
    assert resolved.value == str(paper.resolve())


def test_a_real_url_is_still_not_a_local_path() -> None:
    assert resolve_field("file-path", {"input": "https://example.com/p.pdf"}).resolved is False


def test_local_file_or_text_keeps_substantial_inline_manuscript() -> None:
    manuscript = "Abstract\n" + ("Complete manuscript evidence. " * 20)

    resolved = resolve_field("local_file_or_text", {"input": manuscript})

    assert resolved.resolved is True
    assert resolved.value == manuscript.strip()


@pytest.mark.asyncio
async def test_unknown_or_parse_only_resolver_has_no_lookup_candidates() -> None:
    assert await search_field_candidates("doi", "A paper title") == []
    assert await search_field_candidates("not-registered", "A paper title") == []
