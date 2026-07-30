"""Fuzzy file candidates for the ``@`` mention picker.

Mirrors what the reference agents do, with no new dependency. Codex walks the
tree with ripgrep's ``ignore`` crate and ranks via ``nucleo``; Claude Code builds
its index from ``git ls-files`` (falling back to ripgrep). Both therefore respect
``.gitignore`` — the single most important quality signal, because an index full
of ``.venv`` and ``node_modules`` makes the picker useless.

Omni gets the same candidate set from one subprocess:
``git ls-files --cached --others --exclude-standard`` lists tracked *and*
untracked-but-not-ignored paths, so a file created moments ago is offered while
ignored build output never is. Outside a repository (or without git) a bounded
:func:`os.walk` with the same noise-directory skip list takes over.

Ranking is a compact subsequence scorer rather than a vendored fuzzy library:
the candidate count is bounded and the work happens off the UI thread, so the
simpler thing that stays dependency-free wins.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from omni.core.sensitive_paths import is_sensitive_path

# Kept in step with the fs tools' grep/glob skip list: directories that are pure
# noise in a picker even when a repository does not ignore them.
_SKIP_DIRS = frozenset(
    {
        ".git", "__pycache__", ".venv", "venv", "node_modules",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".tox",
        ".DS_Store", "dist", "build", ".eggs",
    }
)

# Codex returns 20 search hits and shows 8 rows; matching the result cap keeps
# the menu scrollable but never unbounded.
DEFAULT_LIMIT = 20
# Upper bound on the index. A repository larger than this still works — ranking
# just sees a prefix of the walk — and typing stays responsive.
_MAX_INDEXED = 20_000
# Re-index after this many seconds so a newly written file becomes mentionable
# without restarting the REPL.
_CACHE_TTL_S = 4.0

_BOUNDARY_CHARS = "/\\_-. "


@dataclass(frozen=True)
class FileCandidate:
    """One ranked pick: ``relative`` is inserted, ``is_dir`` drives the label."""

    relative: str
    is_dir: bool
    score: int


def fuzzy_score(query: str, candidate: str) -> int | None:
    """Score ``candidate`` against a subsequence ``query``; ``None`` if no match.

    Rewards the signals that make a pick feel obvious — matches at path/word
    boundaries, consecutive runs, and hits inside the file name rather than some
    ancestor directory — and mildly penalises gaps and very long paths.
    """
    if not query:
        return 0
    lowered_query = query.lower()
    lowered = candidate.lower()
    score = 0
    cursor = 0
    previous = -2
    for char in lowered_query:
        found = lowered.find(char, cursor)
        if found == -1:
            return None
        if found == 0 or lowered[found - 1] in _BOUNDARY_CHARS:
            score += 8
        if found == previous + 1:
            score += 6
        else:
            score -= min(4, found - previous - 1)
        previous = found
        cursor = found + 1
    separator = lowered.rfind("/")
    if previous > separator:  # the match reached the basename
        score += 5
    basename = lowered[separator + 1 :]
    if basename.startswith(lowered_query):
        score += 20
    elif lowered.startswith(lowered_query):
        score += 10
    return score - len(lowered) // 24


class FileSearcher:
    """Cached, gitignore-aware file index for one root directory."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        always_visible: Sequence[Path] = (),
    ) -> None:
        self._root = (root or Path.cwd()).resolve()
        # Directories indexed regardless of gitignore. Gitignore is a good proxy
        # for "not part of this project" when the ignored thing is build output;
        # it is a bad one for omni's own deliverables, which a repository often
        # ignores precisely because they are generated.
        self._always_visible = tuple(Path(path) for path in always_visible)
        self._entries: list[tuple[str, bool]] = []
        self._loaded_at = 0.0

    @property
    def root(self) -> Path:
        return self._root

    def search(self, query: str, *, limit: int = DEFAULT_LIMIT) -> list[FileCandidate]:
        """Top ``limit`` candidates for ``query``, best first.

        An empty query lists the shallowest entries, which is what makes a bare
        ``@`` useful as "show me around" rather than an empty popup.
        """
        entries = self._index()
        if not query:
            # Shallowest first, and files ahead of directories at equal depth: a
            # bare ``@`` is usually "which file", while directories matter once
            # the user starts navigating with a prefix.
            shallow = sorted(
                entries,
                key=lambda item: (item[0].count("/"), item[1], item[0].lower()),
            )
            return [FileCandidate(rel, is_dir, 0) for rel, is_dir in shallow[:limit]]
        scored: list[FileCandidate] = []
        for relative, is_dir in entries:
            score = fuzzy_score(query, relative)
            if score is None:
                continue
            scored.append(FileCandidate(relative, is_dir, score))
        scored.sort(key=lambda item: (-item.score, len(item.relative), item.relative.lower()))
        return scored[:limit]

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    def _index(self) -> list[tuple[str, bool]]:
        now = time.monotonic()
        if self._entries and (now - self._loaded_at) < _CACHE_TTL_S:
            return self._entries
        files = _git_files(self._root)
        if files is None:
            files = _walk_files(self._root)
        for extra in self._always_visible:
            files.extend(_walk_files(self._root, extra))
        self._entries = _with_directories(list(dict.fromkeys(files)))
        self._loaded_at = now
        return self._entries


def _git_files(root: Path) -> list[str] | None:
    """Tracked + untracked-but-not-ignored paths, or ``None`` outside a repo.

    ``-z`` is not a micro-optimisation: without it git applies ``core.quotePath``
    and returns a non-ASCII path as a quoted, backslash-escaped literal
    (``"figures/\\345\\255\\230....png"``). Those bytes are not a path, so every
    CJK-named file became an unselectable candidate that resolved to nothing.
    NUL separation also removes the (legal) newline-in-filename ambiguity.
    """
    try:
        completed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            # git reports path bytes as UTF-8. Decoding with the locale default
            # instead — the ANSI code page on Windows — turns every CJK filename
            # into mojibake that names no file, which is the same unselectable
            # candidate ``-z`` was added to prevent, arriving by another route.
            encoding="utf-8",
            errors="surrogateescape",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    out: list[str] = []
    for entry in completed.stdout.split("\0"):
        relative = entry.strip()
        if relative and not _is_noise(relative):
            out.append(relative)
        if len(out) >= _MAX_INDEXED:
            break
    return out


def deliverable_roots(base: Path) -> list[Path]:
    """omni's own output subfolders under ``base`` that currently exist.

    Kept in step with the artifact store's own mapping rather than hardcoded
    here: a new deliverable kind must not silently become unmentionable.
    """
    from omni.storage.artifacts import deliverable_subdirs

    resolved = base.expanduser()
    return [
        candidate
        for candidate in (resolved / name for name in deliverable_subdirs())
        if candidate.is_dir()
    ]


def _walk_files(root: Path, start: Path | None = None) -> list[str]:
    """Walk ``start`` (default ``root``), naming results relative to ``root``."""
    out: list[str] = []
    for current, dirs, names in os.walk(start or root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        base = Path(current)
        for name in names:
            path = base / name
            if is_sensitive_path(path):
                continue
            try:
                # POSIX separators, because ``git ls-files`` reports "/" on every
                # platform and both sources feed one index. Left native, a
                # Windows walk yields "pkg\\main.py", which counts zero "/" and
                # so reads as depth zero — every entry ties for shallowest and
                # the bare-``@`` ordering collapses. It would also spell the same
                # file differently inside a repository and outside one.
                out.append(path.relative_to(root).as_posix())
            except ValueError:
                # Configured outside the picker root: only an absolute path can
                # name it, and an absolute mention resolves just as well.
                out.append(path.as_posix())
            if len(out) >= _MAX_INDEXED:
                return out
    return out


def _with_directories(files: list[str]) -> list[tuple[str, bool]]:
    """Pair each file with its ancestor directories so path navigation works."""
    entries: list[tuple[str, bool]] = [(rel, False) for rel in files]
    seen: set[str] = set()
    for relative in files:
        parent = os.path.dirname(relative)
        while parent and parent not in seen:
            seen.add(parent)
            entries.append((parent, True))
            parent = os.path.dirname(parent)
    return entries


def _is_noise(relative: str) -> bool:
    parts = Path(relative).parts
    if any(part in _SKIP_DIRS for part in parts):
        return True
    # Sensitive files are never offered: suggesting one only earns the user a
    # denial from the fs tools, and the suggestion itself discloses that the
    # secret exists.
    return is_sensitive_path(Path(relative))
