"""Host-owned git log for changelog turns (A-CHG-01)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omni.core.react_agent import ToolSpec
from omni.core.system_prompt import build_system_prompt
from omni.runtime.git_info import (
    recent_commits,
    repository_history_block,
    utterance_asks_repo_changelog,
)

_A_CHG_01 = (
    "Review the last four days of git commits in this repository. "
    "Analyze new features, optimizations, problems solved, and anything "
    "that looks unreasonable. What were the optimization points?"
)


def _local_tools() -> list[ToolSpec]:
    schema = {"type": "object"}
    return [
        ToolSpec("bash", "shell", schema),
        ToolSpec("grep", "search", schema),
        ToolSpec("read_file", "read", schema),
    ]


def test_changelog_utterance_is_detected() -> None:
    assert utterance_asks_repo_changelog(_A_CHG_01)
    assert utterance_asks_repo_changelog("What changed in Omni for researchers in the latest commits?")
    assert not utterance_asks_repo_changelog("Review arXiv 1706.03762 as a NeurIPS reviewer.")


def test_recent_commits_and_history_block_in_temp_repo(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "Walkthrough",
        "GIT_AUTHOR_EMAIL": "wt@example.com",
        "GIT_COMMITTER_NAME": "Walkthrough",
        "GIT_COMMITTER_EMAIL": "wt@example.com",
    }
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)
    (tmp_path / "note.md").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "note.md"], cwd=tmp_path, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "walkthrough changelog probe"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )
    entries = recent_commits(tmp_path)
    assert entries is not None
    assert entries
    assert "walkthrough changelog probe" in entries[0]["subject"]
    sha = entries[0]["sha"]
    assert len(sha) >= 8
    block = repository_history_block(tmp_path, _A_CHG_01)
    assert "[Repository history]" in block
    assert sha[:8] in block
    assert "repository-wide grep" in block
    prompt = build_system_prompt(
        role="R",
        tools=_local_tools(),
        project_name="proj",
        working_dir=tmp_path,
        repo_history=block,
    )
    assert sha[:8] in prompt
    assert "git log" in prompt
    assert prompt.index("[Repository history]") < prompt.index("[Session context]")


def test_unavailable_git_does_not_invent_shas(tmp_path: Path) -> None:
    block = repository_history_block(tmp_path, _A_CHG_01)
    assert "Git is unavailable" in block
    assert "do not invent" in block.lower()
    assert not utterance_asks_repo_changelog("list files in this folder")
    assert repository_history_block(tmp_path, "list files in this folder") == ""
