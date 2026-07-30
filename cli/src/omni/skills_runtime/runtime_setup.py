"""Explicit setup for the owner-managed Node runtime used by research-pptx.

Task execution is deliberately side-effect free: engines may read the installed
runtime but never invoke a package manager. Installation, ``omni init``, and
``omni update`` call this module from an owner-controlled CLI boundary.

The renderer installs into a single fixed cache directory. ``npm ci`` rebuilds
``node_modules`` from the pinned lockfile on every install/init/update, so there
is no need to version the cache by lockfile hash or track a readiness marker.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from omni.data import BUILTIN_SKILLS_DIR

RESEARCH_PPTX_SETUP_COMMAND = "omni skills setup research-pptx"
_RENDERER_PACKAGES = ("pptxgenjs", "sharp")

# The pinned renderer bundles ``sharp`` (its ``engines`` requires ``>=20.9.0``).
# Older Node has no matching prebuilt binary; npm only warns (EBADENGINE) and
# then ``require("sharp")`` fails at runtime. Gate on this before npm runs.
_MIN_NODE: tuple[int, int, int] = (20, 9, 0)
_MIN_NODE_LABEL = f"{_MIN_NODE[0]}.{_MIN_NODE[1]}"


class SkillRuntimeSetupError(RuntimeError):
    """A bundled Skill runtime could not be prepared by the owner CLI."""


def _parse_node_version(raw: str) -> tuple[int, int, int] | None:
    """Parse ``node --version`` output (e.g. ``v20.9.0``) into a version tuple.

    Returns ``None`` for empty/unparseable input so the caller can fall back to
    letting npm decide rather than blocking a valid-but-unusual toolchain.
    """
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", (raw or "").strip())
    if match is None:
        return None
    major, minor, patch = (int(group) for group in match.groups())
    return (major, minor, patch)


def _node_version(node: str) -> tuple[int, int, int] | None:
    """Best-effort ``(major, minor, patch)`` of the given node binary."""
    try:
        completed = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_node_version(getattr(completed, "stdout", "") or "")


def _require_supported_node(node: str) -> None:
    """Fail fast when Node is too old for the pinned sharp renderer.

    An undeterminable version is intentionally *not* blocked — npm remains the
    backstop — so this only rejects a Node we can positively identify as stale.
    """
    version = _node_version(node)
    if version is not None and version < _MIN_NODE:
        found = ".".join(str(part) for part in version)
        raise SkillRuntimeSetupError(
            f"research-pptx setup requires Node.js >= {_MIN_NODE_LABEL} (found {found}); "
            "the pinned sharp renderer ships no prebuilt binary for older Node. "
            f"Upgrade Node, then run `{RESEARCH_PPTX_SETUP_COMMAND}`."
        )


def research_pptx_runtime_dir(paths: Any) -> Path:
    """Return the fixed cache path that holds the installed Node renderer."""
    return Path(paths.cache_dir) / "skill-runtimes" / "research-pptx"


def _packages_installed(runtime_dir: Path) -> bool:
    node_modules = runtime_dir / "node_modules"
    return all((node_modules / package).is_dir() for package in _RENDERER_PACKAGES)


def research_pptx_runtime_ready(paths: Any) -> bool:
    """Fast, side-effect-free check that the renderer cache is already installed.

    Mirrors the readiness contract :func:`setup_research_pptx_runtime` uses to
    short-circuit (``force=False``), so callers can cheaply skip re-running setup.
    """
    return _packages_installed(research_pptx_runtime_dir(paths))


def setup_research_pptx_runtime(
    paths: Any,
    *,
    skill_dir: Path | None = None,
    force: bool = False,
) -> bool:
    """Install the lockfile-pinned renderer into Omni's user cache.

    Returns ``True`` when packages were (re)installed and ``False`` when the
    cache was already ready. Raises :class:`SkillRuntimeSetupError` on failure.
    """
    scripts = Path(skill_dir or (BUILTIN_SKILLS_DIR / "research-pptx")) / "scripts"
    package_json = scripts / "package.json"
    lockfile = scripts / "package-lock.json"
    if not package_json.is_file() or not lockfile.is_file():
        raise SkillRuntimeSetupError(
            f"research-pptx renderer manifest is unavailable under {scripts}."
        )

    runtime_dir = research_pptx_runtime_dir(paths)
    if not force and _packages_installed(runtime_dir):
        return False

    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        raise SkillRuntimeSetupError(
            f"research-pptx setup requires Node.js >= {_MIN_NODE_LABEL}. Install it, then run "
            f"`{RESEARCH_PPTX_SETUP_COMMAND}`."
        )
    _require_supported_node(node)
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise SkillRuntimeSetupError(
            f"research-pptx setup requires npm. Install Node.js >= {_MIN_NODE_LABEL}, then run "
            f"`{RESEARCH_PPTX_SETUP_COMMAND}`."
        )

    runtime_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(package_json, runtime_dir / package_json.name)
    shutil.copy2(lockfile, runtime_dir / lockfile.name)
    try:
        completed = subprocess.run(
            [npm, "ci", "--omit=dev"], cwd=runtime_dir, check=False
        )
    except OSError as exc:
        raise SkillRuntimeSetupError(
            f"research-pptx renderer setup could not start npm: {exc}"
        ) from exc
    if int(getattr(completed, "returncode", 1)) != 0:
        raise SkillRuntimeSetupError(
            "research-pptx renderer setup failed while running `npm ci` "
            f"(exit={getattr(completed, 'returncode', 'unknown')}). Retry with "
            f"`{RESEARCH_PPTX_SETUP_COMMAND}`."
        )
    if not _packages_installed(runtime_dir):
        raise SkillRuntimeSetupError(
            "research-pptx renderer setup completed without the required packages "
            + ", ".join(_RENDERER_PACKAGES)
            + f". Retry with `{RESEARCH_PPTX_SETUP_COMMAND}`."
        )
    return True
