"""Filesystem discovery of skill packages — the single source of truth.

Every part of the CLI that needs to answer "where are the skills on disk?"
goes through here so the on-disk contract (``<dir>/SKILL.md`` packages, flat
``<name>.md`` files, nested ecosystem layouts) is defined in exactly one place:

* :func:`skill_dirs_in` — immediate child skill packages (used by install/export
  and the bundled-skills locator).
* :func:`indexed_skill_dirs` — fail-closed product inventory for bundled skills.
* :func:`is_skill_collection` — whether a directory *holds* skill packages.
* :func:`iter_skill_paths` — recursive catalog scan used by the registry.

The module is intentionally stdlib-only (no ``manifest`` / ``paths`` imports) so
it can be imported from foundational modules (``omni.data``) without cycles.
"""

from __future__ import annotations

import os
from pathlib import Path

from omni.skills_runtime.index_format import (  # noqa: F401 - public discovery API
    SKILL_INDEX_FILENAME,
    SkillIndexError,
    active_skill_names,
    indexed_skill_dirs,
)

# Directories never traversed when discovering skills recursively.
NOISE_DIRS: frozenset[str] = frozenset(
    {
        "node_modules", "__pycache__", ".git", ".hg", ".venv", "venv",
        "dist", "build", ".cache", ".tmp", ".pytest_cache", ".mypy_cache",
        "references", "scripts", "assets",  # skill-internal resource dirs
    }
)

def skill_dirs_in(root: Path) -> list[Path]:
    """Immediate child directories of ``root`` that are skill packages.

    A skill package is a directory containing a ``SKILL.md``. This is the
    shallow, one-level scan used when copying/importing/exporting whole
    packages (as opposed to the recursive catalog scan).
    """
    if not root.is_dir():
        return []
    try:
        return sorted(d for d in root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())
    except OSError:
        return []


def is_skill_collection(root: Path) -> bool:
    """True if ``root`` holds at least one ``<child>/SKILL.md`` skill package."""
    return bool(skill_dirs_in(root))


def iter_skill_paths(root: Path) -> list[Path]:
    """Find skill entry points under ``root`` for catalog indexing.

    Supports the flat single-file layout (``<root>/<name>.md``) and recursive
    directory layouts (``<root>/**/SKILL.md``), pruning hidden and resource
    directories so external ecosystems' nested skills are discovered cheaply.
    Returns directory paths (for ``<dir>/SKILL.md`` skills) and file paths (for
    flat ``.md`` skills).
    """
    out: list[Path] = []
    if not root.is_dir():
        return out
    # Flat single-file skills at the top level (OmniScientist / Claude convention).
    for entry in sorted(root.iterdir()):
        if (
            entry.is_file()
            and entry.suffix.lower() == ".md"
            and entry.name != "SKILL.md"
            and entry.name.lower() != "readme.md"
        ):
            out.append(entry)
    # Recursive directory skills (each dir holding a SKILL.md).
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in NOISE_DIRS)
        if "SKILL.md" in filenames:
            out.append(Path(dirpath))
            dirnames[:] = []  # don't descend into a skill's own subtree
    return out
