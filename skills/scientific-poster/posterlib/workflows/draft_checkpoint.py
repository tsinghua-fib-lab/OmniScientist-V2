"""Versioned, validated checkpoints for staged poster drafting."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from posterlib.runtime.runtime_io import write_json_atomic
from posterlib.visual import reference_seeds, visual_design

_FILENAME = "draft-checkpoint.json"
_VERSION = 5
_REFERENCE_STAGES = frozenset(
    {"reference-ready", "design-ready", "author-ready"}
)
_DESIGN_STAGES = frozenset({"design-ready", "author-ready"})
_STAGES = frozenset({"plan-ready", *_REFERENCE_STAGES})
_PLAN_FIELDS = {
    "source_text": str,
    "authoring_request": str,
    "asset_inputs": list,
    "asset_sha256s": list,
    "source_figure_sha256s": list,
    "warnings": list,
    "paper_source": dict,
    "content_budget": dict,
    "page_plan": dict,
    "visual_preferences": dict,
    "visual_iteration": int,
}


def save(workspace: Path, *, stage: str, state: Mapping[str, Any]) -> None:
    """Atomically persist a validated draft stage in its task workspace."""

    save_path(workspace / _FILENAME, stage=stage, state=state)


def save_path(path: Path, *, stage: str, state: Mapping[str, Any]) -> None:
    """Atomically persist a validated checkpoint at an explicit sidecar path."""

    payload = {**dict(state), "version": _VERSION, "stage": stage}
    normalized = _validated_payload(payload)
    if normalized is None:
        raise ValueError(f"invalid {stage!r} poster draft checkpoint")
    write_json_atomic(
        path,
        {**normalized, "version": _VERSION},
        indent=None,
        sort_keys=True,
        allow_nan=False,
    )


def load(workspace: Path) -> dict[str, Any] | None:
    """Load a valid checkpoint, ignoring missing, stale, or malformed state."""

    return load_path(workspace / _FILENAME)


def load_path(path: Path) -> dict[str, Any] | None:
    """Load a valid checkpoint from an explicit sidecar path."""

    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return _validated_payload(payload)


def _validated_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("version") != _VERSION:
        return None
    stage = value.get("stage")
    if stage not in _STAGES:
        return None
    if any(
        not isinstance(value.get(name), expected)
        for name, expected in _PLAN_FIELDS.items()
    ):
        return None
    if any(not isinstance(item, str) for item in value["warnings"]):
        return None
    for field in ("asset_sha256s", "source_figure_sha256s"):
        if any(not isinstance(item, str) for item in value[field]):
            return None
    iteration = value["visual_iteration"]
    if isinstance(iteration, bool) or not 0 <= iteration <= 2:
        return None
    raw_reference = value.get("design_reference")
    raw_reference_image = value.get("reference_image")
    if raw_reference_image is not None and (
        not isinstance(raw_reference_image, str) or not raw_reference_image.strip()
    ):
        return None
    if raw_reference is not None and not _valid_reference(raw_reference):
        return None
    if stage in _REFERENCE_STAGES and raw_reference is None:
        return None
    raw_design = value.get("visual_design")
    if raw_design is not None and not _valid_visual_design(raw_design):
        return None
    if raw_design is not None and raw_reference is None:
        return None
    if (
        raw_design is not None
        and raw_reference is not None
        and (
            raw_design.get("reference_image_sha256")
            != raw_reference.get("image_sha256")
        )
    ):
        return None
    if stage in _DESIGN_STAGES and raw_design is None:
        return None
    if stage == "author-ready" and not isinstance(value.get("html_template"), str):
        return None
    normalized = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "version",
            "pending_visual_revision",
            "inspection_repair_attempt",
        }
    }
    if raw_reference is not None:
        normalized["design_reference"] = reference_seeds.ReferenceBundle.from_dict(
            dict(raw_reference)
        ).to_dict()
    if raw_design is not None:
        normalized["visual_design"] = visual_design.VisualDesignPlan.from_dict(
            dict(raw_design)
        ).to_dict()
    return normalized


def _valid_reference(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    raw = dict(value)
    try:
        reference_seeds.ReferenceBundle.from_dict(raw)
        return True
    except reference_seeds.ReferenceSeedError:
        return False


def _valid_visual_design(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    raw = dict(value)
    try:
        visual_design.VisualDesignPlan.from_dict(raw)
        return True
    except visual_design.VisualDesignError:
        return False


__all__ = ["load", "load_path", "save", "save_path"]
