"""Shared host-side read model for SoulAgent scientist personas.

The portable SoulAgent Skill owns activation and lifecycle writes. This module
only projects the small on-disk discovery contract used by product surfaces so
the CLI and Web UI cannot drift into separate persona catalogs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omni.agent.persona_stoma import load_persona_identity, load_persona_overlay
from omni.personas.roots import persona_kg_root, persona_state_root

_MAX_CATALOG_JSON_BYTES = 256 * 1024


@dataclass(frozen=True)
class PersonaPaths:
    """Resolved project and scanner roots for one Omni invocation."""

    project_root: Path
    kg_root: Path


def resolve_persona_paths(paths: Any) -> PersonaPaths:
    """Resolve the folder-exact state root and the KG scanner for one invocation."""
    return PersonaPaths(
        project_root=persona_state_root(paths),
        kg_root=persona_kg_root(paths),
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_CATALOG_JSON_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def persona_inventory(kg_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Read the lightweight KG identity contract without importing Skill code."""
    scientists: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    if not kg_root.is_dir():
        return scientists, invalid
    try:
        candidates = sorted(path for path in kg_root.iterdir() if path.is_dir())
    except OSError:
        return scientists, [
            {
                "directory": kg_root.name or "scientist-kg",
                "error": "persona scanner could not be read",
            }
        ]
    for candidate in candidates:
        identity = _read_json_object(candidate / "identity.json")
        manifest = _read_json_object(candidate / "manifest.json")
        scientist_id = str(
            identity.get("scientist_id") or manifest.get("scientist_id") or candidate.name
        ).strip()
        scientist_name = str(identity.get("scientist_name") or "").strip()
        if not scientist_name or scientist_id != candidate.name:
            invalid.append(
                {
                    "directory": candidate.name,
                    "error": "missing identity or scientist_id does not match the directory",
                }
            )
            continue
        raw_aliases = identity.get("aliases") or []
        if not isinstance(raw_aliases, list) or any(
            not isinstance(value, str) for value in raw_aliases
        ):
            invalid.append(
                {
                    "directory": candidate.name,
                    "error": "aliases must be a list of strings",
                }
            )
            continue
        aliases = [value.strip() for value in raw_aliases if value.strip()]
        scientists.append(
            {
                "scientist_id": scientist_id,
                "scientist_name": scientist_name,
                "aliases": aliases,
                "path": str(candidate),
            }
        )
    return scientists, invalid


def persona_snapshot(
    paths: Any,
    *,
    repair_incomplete_unload: bool = True,
    metadata_only: bool = False,
) -> dict[str, Any]:
    """Return the common CLI/Web persona status and scanner inventory."""
    resolved = resolve_persona_paths(paths)
    overlay = (
        load_persona_identity(
            resolved.project_root,
            repair_incomplete_unload=repair_incomplete_unload,
        )
        if metadata_only
        else load_persona_overlay(
            resolved.project_root,
            repair_incomplete_unload=repair_incomplete_unload,
        )
    )
    scientists, invalid = persona_inventory(resolved.kg_root)
    return {
        "active": overlay.active,
        "scientist_id": overlay.scientist_id if overlay.active else "",
        "scientist_name": overlay.scientist_name if overlay.active else "",
        "project_root": str(resolved.project_root),
        "kg_root": str(resolved.kg_root),
        "available": scientists,
        "invalid": invalid,
    }
