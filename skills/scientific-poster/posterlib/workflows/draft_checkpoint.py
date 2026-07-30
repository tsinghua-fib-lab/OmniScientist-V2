"""Versioned, validated checkpoints for staged poster drafting."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from posterlib.runtime.runtime_io import write_json_atomic
from posterlib.visual import reference_seeds, visual_design, visual_review

_FILENAME = "draft-checkpoint.json"
_VERSION = 5
_REFERENCE_STAGES = frozenset(
    {"reference-ready", "design-ready", "author-repair-ready", "author-ready"}
)
_DESIGN_STAGES = frozenset({"design-ready", "author-repair-ready", "author-ready"})
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
_PENDING_VISUAL_REVISION_FIELDS = frozenset(
    {
        "parent_html_sha256",
        "visual_review_path",
        "receipt_sha256",
        "reference_image_sha256",
        "screenshot_sha256",
        "visual_evidence_sha256",
        "next_iteration",
        "operations",
    }
)
_PENDING_VISUAL_REVISION_OPTIONAL_FIELDS = frozenset({"regressed_inspection_feedback"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
        payload,
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
    pending_revision = value.get("pending_visual_revision")
    if pending_revision is not None and (
        stage != "author-ready"
        or not _valid_pending_visual_revision(
            pending_revision,
            visual_iteration=iteration,
        )
    ):
        return None
    inspection_attempt = value.get("inspection_repair_attempt")
    if inspection_attempt is not None and (
        isinstance(inspection_attempt, bool)
        or not isinstance(inspection_attempt, int)
        or not 0 <= inspection_attempt <= 2
    ):
        return None
    raw_reference = value.get("design_reference")
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
    if stage == "author-repair-ready":
        attempt = value.get("author_repair_attempt")
        if (
            not isinstance(value.get("html_template"), str)
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 0 <= attempt <= 2
        ):
            return None
    if stage == "author-ready" and not isinstance(value.get("html_template"), str):
        return None
    return {key: item for key, item in value.items() if key != "version"}


def _valid_reference(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    raw = dict(value)
    try:
        return reference_seeds.ReferenceBundle.from_dict(raw).to_dict() == raw
    except reference_seeds.ReferenceSeedError:
        return False


def _valid_visual_design(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    raw = dict(value)
    try:
        return visual_design.VisualDesignPlan.from_dict(raw).to_dict() == raw
    except visual_design.VisualDesignError:
        return False


def _valid_pending_visual_revision(
    value: Any,
    *,
    visual_iteration: int,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    pending = dict(value)
    pending_fields = set(pending)
    if (
        not _PENDING_VISUAL_REVISION_FIELDS.issubset(pending_fields)
        or pending_fields
        - _PENDING_VISUAL_REVISION_FIELDS
        - _PENDING_VISUAL_REVISION_OPTIONAL_FIELDS
    ):
        return False
    if any(
        not isinstance(pending.get(field), str)
        or _SHA256_RE.fullmatch(str(pending[field])) is None
        for field in (
            "parent_html_sha256",
            "receipt_sha256",
            "reference_image_sha256",
            "screenshot_sha256",
            "visual_evidence_sha256",
        )
    ):
        return False
    review_path = pending.get("visual_review_path")
    if not isinstance(review_path, str) or not Path(review_path).is_absolute():
        return False
    next_iteration = pending.get("next_iteration")
    if (
        isinstance(next_iteration, bool)
        or not isinstance(next_iteration, int)
        or next_iteration != visual_iteration + 1
        or next_iteration > visual_review.MAX_VISUAL_REVISIONS
    ):
        return False
    operations = pending.get("operations")
    if not isinstance(operations, list) or not operations:
        return False
    regression_feedback = pending.get("regressed_inspection_feedback")
    if regression_feedback is not None and (
        not isinstance(regression_feedback, list)
        or not regression_feedback
        or any(
            not isinstance(item, str) or not item.strip()
            for item in regression_feedback
        )
    ):
        return False
    allowed = {"restyle", "reflow", "content-replan"}
    return all(
        isinstance(operation, str) and operation in allowed for operation in operations
    ) and operations == sorted(set(operations))


__all__ = ["load", "load_path", "save", "save_path"]
