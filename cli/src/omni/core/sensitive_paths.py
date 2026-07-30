"""Which filesystem paths are invisible to every read surface.

This policy has two consumers that must never drift apart: the fs tools (a
mentioned or model-supplied path is refused) and the ``@`` mention picker (such
a path is never even *offered*). Keeping the globs in one low-level module makes
"add a pattern" a single edit; duplicating them would mean a newly protected
pattern is still suggested by the picker, which both invites a confusing denial
and discloses that the secret file exists.

Name-based matching (:func:`is_sensitive_path`) is cheap enough to filter a
whole file index. :func:`is_sensitive_target` additionally re-checks the resolved
target, closing the symlink bypass where a benign name points at a secret.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from pathlib import Path

# Matched case-insensitively against the file name.
SENSITIVE_GLOBS: tuple[str, ...] = (
    "secrets.toml",
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.pfx",
    "*.p12",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "*.credentials",
    "credentials.json",
    "*.secret",
    "*_secret",
    ".netrc",
    ".pgpass",
)

# Directory names that hold credentials wholesale.
SENSITIVE_DIRS: frozenset[str] = frozenset({".ssh", ".gnupg", ".aws", ".gpg"})

# Version-control metadata. Writing it converts file access into code execution
# — a ``.git/hooks`` script runs on the owner's next commit — so nothing may
# exempt it.
VCS_PROTECTED_DIRS: frozenset[str] = frozenset({".git", ".hg", ".svn"})

# Omni's own tree. It carries the session and memory stores, the config, the
# trust record and the installed skills: what runs, and what later turns
# believe. A turn may not rewrite the machinery it is running on.
STATE_PROTECTED_DIRS: frozenset[str] = frozenset({".omni", ".agents", ".codex"})

# Host-owned metadata under the user source cwd. The OS sandbox denies writes
# to these names even when they do not exist yet (Codex's carve-out set).
PROTECTED_METADATA_NAMES: frozenset[str] = (
    VCS_PROTECTED_DIRS | STATE_PROTECTED_DIRS
)

# Directories that stay read-only even inside a root the turn may otherwise
# write. This is integrity, not confidentiality.
WRITE_PROTECTED_DIRS: frozenset[str] = VCS_PROTECTED_DIRS | STATE_PROTECTED_DIRS


def is_write_protected_path(path: Path, output_roots: Sequence[Path] = ()) -> bool:
    """Whether ``path`` lies inside a directory no turn may write into.

    ``output_roots`` names the directories omni generates *into*. It exists
    because the store and the output area are the same tree: an installed omni
    keeps its workspace at ``~/.omni/workspaces/<slug>/``, so the artifacts
    directory a paper is supposed to land in sits inside the directory this
    guard protects. Refusing the whole tree made the default destination for a
    generated document unwritable, and the model — denied — dropped the paper in
    the user's source root instead.

    The exemption is stated as part of the rule rather than bolted on at the one
    call site that hurt, but it is deliberately an allow-list: everything under
    ``.omni`` stays refused unless a caller names it as output. A deny-list of
    omni's internals would have to enumerate every store, lock and manifest, and
    would silently admit each new one, which is the wrong way round for a guard
    whose purpose is integrity.

    Version control is not subject to it. ``output_roots`` can only relax
    ``STATE_PROTECTED_DIRS``; a ``.git`` path is refused however it is declared.
    """
    parts = {part.lower() for part in path.parts}
    if parts & VCS_PROTECTED_DIRS:
        return True
    if not parts & STATE_PROTECTED_DIRS:
        return False
    return not any(_is_within(path, root) for root in output_roots)


def _is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` sits at or beneath ``root``, symlinks resolved.

    Resolving both sides is what stops a link inside the output area from
    pointing the write back at the store it is carved out of.
    """
    try:
        path.resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def is_sensitive_path(path: Path) -> bool:
    """Name/directory sensitivity, without touching the filesystem."""
    name = path.name.lower()
    if any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_GLOBS):
        return True
    return bool({part.lower() for part in path.parts} & SENSITIVE_DIRS)


def is_sensitive_target(path: Path) -> bool:
    """Sensitivity of both the *named* path and its *resolved* target.

    Checking only the name lets a benign-looking symlink (``notes.txt`` →
    ``.env`` / ``~/.ssh/id_rsa``) smuggle a secret past the name-glob guard once
    a root check has cleared the *resolved* location — a TOCTOU on the symbolic
    name. A path that cannot be resolved at all (broken link, resolution loop)
    is treated as unsafe.
    """
    if is_sensitive_path(path):
        return True
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return True
    return resolved != path and is_sensitive_path(resolved)
