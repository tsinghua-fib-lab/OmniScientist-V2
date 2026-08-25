"""Bounded, read-only git history (Codex ``recent_commits``).

Changelog questions must start from ``git log``, not a repository-wide grep.
This helper is host-owned: it never invents SHAs, and a missing git binary
or a non-repo working directory is reported as unavailable.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_GIT_TIMEOUT_S = 5.0
_DEFAULT_LIMIT = 20
_DEFAULT_SINCE_DAYS = 7

# Conservative: only fire when the user is asking about this repo's history.
_CHANGELOG_HINTS = (
    "git commit",
    "commits",
    "changelog",
    "what changed",
    "last few days",
    "last four days",
    "recent commit",
    "latest commit",
    "optimization points",
)


def utterance_asks_repo_changelog(text: str) -> bool:
    blob = str(text or "").casefold()
    return any(hint in blob for hint in _CHANGELOG_HINTS)


def recent_commits(
    cwd: str | Path | None,
    *,
    limit: int = _DEFAULT_LIMIT,
    since_days: int = _DEFAULT_SINCE_DAYS,
) -> list[dict[str, Any]] | None:
    """Return recent commits, or ``None`` when git is unavailable.

    An empty list means git worked and the window is empty. Never invent SHAs.
    """
    root = Path(cwd) if cwd else Path.cwd()
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0:
        return None
    bound = max(1, min(int(limit), 50))
    window = max(1, min(int(since_days), 31))
    try:
        logged = subprocess.run(
            [
                "git",
                "log",
                f"-n{bound}",
                f"--since={window} days ago",
                "--pretty=format:%H%x1f%ct%x1f%s",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if logged.returncode != 0:
        return None
    entries: list[dict[str, Any]] = []
    for line in logged.stdout.splitlines():
        sha, _, rest = line.partition("\x1f")
        ts_s, _, subject = rest.partition("\x1f")
        sha = sha.strip()
        if not sha:
            continue
        try:
            stamp = datetime.fromtimestamp(int(ts_s.strip() or "0"), tz=UTC)
            date = stamp.strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            date = ""
        entries.append({"sha": sha, "date": date, "subject": subject.strip()})
    return entries


def repository_history_block(cwd: str | Path | None, user_message: str) -> str:
    """Inject bounded ``git log`` when the turn is a changelog question."""
    if not utterance_asks_repo_changelog(user_message):
        return ""
    entries = recent_commits(cwd)
    if entries is None:
        return (
            "[Repository history]\n"
            "Git is unavailable in this working directory. Say so honestly. "
            "Do not invent commit SHAs. Do not start with a repository-wide grep."
        )
    if not entries:
        return (
            "[Repository history]\n"
            "No commits in the last 7 days. Do not invent commit SHAs. "
            "Do not start with a repository-wide grep."
        )
    lines = [f"- {item['sha'][:8]} {item['date']} {item['subject']}".rstrip() for item in entries]
    return (
        "[Repository history]\n"
        "Host-injected `git log` (bounded). Summarize these commits. "
        "Do not start with a repository-wide grep; use `git show` / `git diff` "
        "only when you need a file from one of these SHAs.\n"
        + "\n".join(lines)
    )


__all__ = [
    "recent_commits",
    "repository_history_block",
    "utterance_asks_repo_changelog",
]
