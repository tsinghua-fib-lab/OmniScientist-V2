"""Versioned, validated checkpoints for staged poster drafting."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from posterlib.runtime.runtime_io import write_json_atomic
from posterlib.visual import reference_analysis, reference_catalog

_FILENAME = "draft-checkpoint.json"
_VERSION = 8
_REFERENCE_STAGES = frozenset(
    {"reference-ready", "design-ready", "author-ready"}
)
_DESIGN_STAGES = frozenset({"design-ready", "author-ready"})
_STAGES = frozenset({"plan-ready", *_REFERENCE_STAGES})
_PLAN_FIELDS = {
    "source_text": str,
    "asset_inputs": list,
    "asset_sha256s": list,
    "source_figure_sha256s": list,
    "warnings": list,
    "paper_source": dict,
    "content_budget": dict,
    "page_plan": dict,
    "visual_preferences": dict,
    "visual_iteration": int,
    "authoring_request": str,
    "author_model_role": str,
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


def restore(workspace: Path, source_path: Path) -> bool:
    """Replace the task checkpoint with one validated candidate sidecar."""

    state = load_path(source_path)
    if state is None:
        return False
    try:
        save(workspace, stage=str(state["stage"]), state=state)
    except (OSError, ValueError):
        return False
    return True


def _validated_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("version") != _VERSION:
        return None
    stage = value.get("stage")
    if stage not in _STAGES:
        return None
    optional_fields = {
        "source_figure_observations",
        "reference_image",
        "editing_issue",
    }
    allowed_fields = {
        "version",
        "stage",
        *_PLAN_FIELDS,
        *optional_fields,
    }
    if stage in _REFERENCE_STAGES:
        allowed_fields.add("design_reference")
    if stage in _DESIGN_STAGES:
        allowed_fields.add("visual_design")
    if stage == "author-ready":
        allowed_fields.add("html_template")
    fields = set(value)
    if not (allowed_fields - optional_fields <= fields <= allowed_fields):
        return None
    if any(
        not isinstance(value.get(name), expected)
        for name, expected in _PLAN_FIELDS.items()
    ):
        return None
    if any(not isinstance(item, str) for item in value["warnings"]):
        return None
    if value["author_model_role"] not in {"main", "vision"}:
        return None
    if "source_figure_observations" in value and not isinstance(
        value["source_figure_observations"], dict
    ):
        return None
    if "editing_issue" in value and not isinstance(value["editing_issue"], dict):
        return None
    for field in ("asset_sha256s", "source_figure_sha256s"):
        if any(not isinstance(item, str) for item in value[field]):
            return None
    iteration = value["visual_iteration"]
    if isinstance(iteration, bool) or iteration < 0:
        return None
    raw_reference = value.get("design_reference")
    raw_reference_image = value.get("reference_image")
    if raw_reference_image is not None and (
        not isinstance(raw_reference_image, str) or not raw_reference_image.strip()
    ):
        return None
    reference: reference_catalog.ReferenceBundle | None = None
    if raw_reference is not None:
        if not isinstance(raw_reference, Mapping):
            return None
        try:
            reference = reference_catalog.ReferenceBundle.from_dict(dict(raw_reference))
        except reference_catalog.ReferenceSeedError:
            return None
    if stage in _REFERENCE_STAGES and raw_reference is None:
        return None
    raw_design = value.get("visual_design")
    design: reference_analysis.ReferenceAnalysis | None = None
    if raw_design is not None:
        if not isinstance(raw_design, Mapping):
            return None
        try:
            design = reference_analysis.ReferenceAnalysis.from_dict(dict(raw_design))
        except reference_analysis.ReferenceAnalysisError:
            return None
    if raw_design is not None and raw_reference is None:
        return None
    if (
        design is not None
        and reference is not None
        and design.reference_image_sha256 != reference.image_sha256
    ):
        return None
    if stage in _DESIGN_STAGES and raw_design is None:
        return None
    if stage == "author-ready" and not isinstance(value.get("html_template"), str):
        return None
    normalized = {key: item for key, item in value.items() if key != "version"}
    if reference is not None:
        normalized["design_reference"] = reference.to_dict()
    if design is not None:
        normalized["visual_design"] = design.to_dict()
    return normalized


__all__ = ["load", "load_path", "restore", "save", "save_path"]
