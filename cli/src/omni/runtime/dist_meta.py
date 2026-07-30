"""PyPI / installer distribution identity.

The import package and console script remain ``omni``; only the distribution
name published to indexes and used by ``uv tool`` / ``pip`` / ``pipx`` is
``OmniScientist-V2`` (PEP 503 normalized: ``omniscientist-v2``).
"""

from __future__ import annotations

DIST_NAME = "OmniScientist-V2"
DIST_NORMALIZED = "omniscientist-v2"
# Pre-rename installs and path heuristics (if any remain on a machine).
DIST_LEGACY = "omniscientist"

# Tool/venv directory names used by uv and pipx (normalized).
DIST_TOOL_NAMES: tuple[str, ...] = (DIST_NORMALIZED, DIST_LEGACY)
