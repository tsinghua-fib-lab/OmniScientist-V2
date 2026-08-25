"""Candidate quality for the ``@`` picker: ranking, gitignore, and secrets.

The picker is a read surface, so the security-relevant assertion is that a
sensitive file is never *offered* — not merely refused later by the fs tools.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from omni.cli.file_search import FileSearcher, deliverable_roots, fuzzy_score


def test_fuzzy_score_requires_a_subsequence() -> None:
    assert fuzzy_score("rme", "README.md") is not None
    assert fuzzy_score("zzz", "README.md") is None
    assert fuzzy_score("", "anything") == 0


def test_basename_prefix_outranks_an_incidental_match() -> None:
    direct = fuzzy_score("read", "README.md")
    buried = fuzzy_score("read", "docs/threads/already-done.md")
    assert direct is not None and buried is not None
    assert direct > buried


def test_path_navigation_query_matches_full_relative_path() -> None:
    assert fuzzy_score("cli/set", "cli/src/omni/config/settings.py") is not None


def _searcher(root: Path) -> FileSearcher:
    return FileSearcher(root)


def test_walk_fallback_finds_files_outside_a_repository(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "main.py").write_text("x", encoding="utf-8")

    hits = {c.relative for c in _searcher(tmp_path).search("README")}
    assert "README.md" in hits
    nested = {c.relative for c in _searcher(tmp_path).search("main")}
    assert "pkg/main.py" in nested


def test_sensitive_files_are_never_offered(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("key", encoding="utf-8")
    (tmp_path / "server.pem").write_text("key", encoding="utf-8")
    (tmp_path / "notes.md").write_text("fine", encoding="utf-8")

    searcher = _searcher(tmp_path)
    every = {c.relative for c in searcher.search("")}
    assert "notes.md" in every
    assert ".env" not in every
    assert "id_rsa" not in every
    assert "server.pem" not in every
    # Also unreachable when asked for by name.
    assert [c.relative for c in searcher.search("env")] == []


def test_noise_directories_are_skipped(tmp_path: Path) -> None:
    junk = tmp_path / "node_modules" / "left-pad"
    junk.mkdir(parents=True)
    (junk / "index.js").write_text("x", encoding="utf-8")
    (tmp_path / "index.js").write_text("x", encoding="utf-8")

    hits = [c.relative for c in _searcher(tmp_path).search("index.js")]
    assert hits == ["index.js"]


def test_empty_query_lists_shallowest_entries_first(tmp_path: Path) -> None:
    (tmp_path / "top.md").write_text("x", encoding="utf-8")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "deep.md").write_text("x", encoding="utf-8")

    listed = [c.relative for c in _searcher(tmp_path).search("")]
    assert listed[0] == "top.md"


def test_directories_are_offered_for_navigation(tmp_path: Path) -> None:
    nested = tmp_path / "corpus" / "papers"
    nested.mkdir(parents=True)
    (nested / "a.md").write_text("x", encoding="utf-8")

    dirs = {c.relative: c.is_dir for c in _searcher(tmp_path).search("corpus")}
    assert dirs.get("corpus") is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_non_ascii_paths_are_not_git_quoted(tmp_path: Path) -> None:
    """Regression: ``core.quotePath`` turned CJK names into unusable candidates.

    Without ``-z`` git returns ``"figures/\\345\\255\\230....png"`` — a quoted,
    backslash-escaped literal that is not a path. Every CJK-named file therefore
    showed up in the picker as garbage that resolved to nothing.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    target = tmp_path / "存储架构图.md"
    target.write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    listed = {c.relative for c in FileSearcher(tmp_path).search("")}

    assert "存储架构图.md" in listed
    assert not any(name.startswith('"') for name in listed)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_gitignored_deliverables_stay_mentionable(tmp_path: Path) -> None:
    """omni's own outputs survive gitignore; ordinary ignored files do not.

    Projects routinely gitignore ``figures/`` *because* it is generated — but for
    a research turn the figure omni just produced is the most likely next
    reference, the opposite of the build noise gitignore protects the picker from.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("figures/\nscratch/\n", encoding="utf-8")
    figures = tmp_path / "figures"
    figures.mkdir()
    (figures / "Fig-1234abcd.provenance.json").write_text("{}", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "junk.txt").write_text("x", encoding="utf-8")

    searcher = FileSearcher(tmp_path, always_visible=deliverable_roots(tmp_path))
    listed = {c.relative for c in searcher.search("")}

    assert "figures/Fig-1234abcd.provenance.json" in listed
    assert "scratch/junk.txt" not in listed


def test_deliverable_roots_only_reports_existing_directories(tmp_path: Path) -> None:
    assert deliverable_roots(tmp_path) == []
    (tmp_path / "figures").mkdir()
    assert [d.name for d in deliverable_roots(tmp_path)] == ["figures"]


def test_deliverable_roots_include_outputs_and_leftover_siblings(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    reports = tmp_path / "reports"
    outputs.mkdir()
    reports.mkdir()
    found = {path.name: path for path in deliverable_roots(outputs)}
    assert found["outputs"] == outputs
    assert found["reports"] == reports


def test_deliverables_are_still_secret_free(tmp_path: Path) -> None:
    """Re-including a directory must not re-include secrets inside it."""
    figures = tmp_path / "figures"
    figures.mkdir()
    (figures / "plot.png").write_text("x", encoding="utf-8")
    (figures / ".env").write_text("TOKEN=1", encoding="utf-8")

    searcher = FileSearcher(tmp_path, always_visible=deliverable_roots(tmp_path))
    listed = {c.relative for c in searcher.search("")}

    assert "figures/plot.png" in listed
    assert "figures/.env" not in listed


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_gitignored_paths_are_excluded_but_untracked_are_kept(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("x", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("x", encoding="utf-8")
    (tmp_path / "fresh.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)

    listed = {c.relative for c in _searcher(tmp_path).search("txt")}
    assert "tracked.txt" in listed
    assert "fresh.txt" in listed  # untracked but not ignored
    assert "ignored.txt" not in listed
