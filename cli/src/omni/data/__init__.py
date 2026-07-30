"""Bundled data: default role prompt + built-in skill packages.

The built-in skills are a *separate* top-level collection living at the
repository root (``<repo>/skills``), a sibling of the Python project
(``<repo>/cli``) so CLI code and skill content stay independent. At wheel-build
time the sibling is force-included under ``omni/data/skills`` (see
``cli/hatch_build.py``) so an installed copy is self-contained; during
editable/source checkouts we locate the sibling ``skills`` directory at runtime.
"""

from pathlib import Path

from omni.skills_runtime.discovery import is_skill_collection

DATA_DIR = Path(__file__).resolve().parent
ROLE_FILE = DATA_DIR / "role.md"


def _resolve_builtin_skills_dir() -> Path:
    """Locate the bundled skills dir for both installed and source layouts."""
    # 1. Installed wheel (force-included) or any copy placed beside this module.
    packaged = DATA_DIR / "skills"
    if packaged.is_dir():
        return packaged
    # 2. Editable/source checkout: walk up from ``…/cli/src/omni/data`` looking
    #    for the sibling ``<repo>/skills`` collection (layout-independent).
    for parent in DATA_DIR.parents:
        candidate = parent / "skills"
        if candidate != packaged and is_skill_collection(candidate):
            return candidate
        if (parent / ".git").exists():  # stop at the repository root
            break
    # 3. Fallback to the packaged path (may be created later / empty).
    return packaged


def _resolve_docs_dir() -> Path:
    """Locate omni's own documentation dir (self-knowledge corpus).

    The docs live at ``<repo>/cli/docs`` — outside the ``src/omni`` package —
    so the wheel force-includes them under ``omni/data/docs`` (see
    ``cli/hatch_build.py``). Installed copies read that packaged dir; source
    checkouts resolve the in-repo ``cli/docs`` sibling at runtime. These docs
    are the *only* part of omni's own tree exposed to the agent (read-only);
    source and secrets are never reachable through the docs tools.
    """
    packaged = DATA_DIR / "docs"
    if packaged.is_dir():
        return packaged
    # Editable/source checkout: DATA_DIR is ``…/cli/src/omni/data``; the docs
    # dir is ``…/cli/docs``. Walk up to the ``cli`` project root and look for it.
    for parent in DATA_DIR.parents:
        candidate = parent / "docs"
        if candidate != packaged and (candidate / "README.md").is_file():
            return candidate
        if (parent / ".git").exists():  # stop at the repository root
            break
    return packaged


BUILTIN_SKILLS_DIR = _resolve_builtin_skills_dir()
DOCS_DIR = _resolve_docs_dir()

# System skills are omni's internal, non-product machinery (e.g. the schedulable
# ``agent-goal`` sub-agent). They live *inside* the package (shipped via the
# ``src/omni/data/**/*.md`` wheel artifact) rather than in the curated product
# ``skills`` inventory, and are registered separately so they are resolvable by
# name / ``$name`` but never enter the product catalog or automatic selection.
SYSTEM_SKILLS_DIR = DATA_DIR / "system_skills"
