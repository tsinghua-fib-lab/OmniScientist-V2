"""Durable revision checkpoints, visual progress, and selection validation."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import poster_contract

from posterlib.content import html_contract, planning, scientific_snapshot
from posterlib.visual import review_contract

from . import draft_checkpoint


class SelectionStateError(ValueError):
    """A live-preview selection does not identify the source HTML."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def visual_iteration(value: Any) -> int:
    """Normalize the screenshot-review sequence number for publication."""

    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise review_contract.ReviewContractError(
            "visual_review_invalid", "visual_iteration must be an integer"
        )
    if value < 0:
        raise review_contract.ReviewContractError(
            "visual_review_invalid",
            "visual_iteration must be non-negative",
        )
    return value


def revision_checkpoint_state(
    source_path: Path,
    *,
    source_html: str,
    html_sha256: str,
) -> dict[str, Any] | None:
    """Restore content-structure and adaptive-page state beside a durable artifact."""

    template, assets = html_contract.tokenize_embedded_images(source_html)
    bound_template = bind_asset_tokens(template, assets)
    sidecar = draft_checkpoint.load_path(revision_sidecar_path(source_path))
    if sidecar is not None and sidecar.get("stage") == "author-ready":
        checkpoint_template = str(sidecar.get("html_template") or "")
        if checkpoint_template == bound_template:
            return {
                **sidecar,
                "html_sha256": html_sha256,
                "artifact_path": str(source_path),
            }
    return None


def persist_revision_sidecar(
    *,
    artifact_path: Path,
    html_text: str,
    checkpoint_state: dict[str, Any],
) -> bool:
    """Carry the grounded checkpoint beside an immutable published HTML artifact."""

    checkpoint = dict(checkpoint_state)
    template = str(checkpoint.get("html_template") or "")
    published_template, published_assets = html_contract.tokenize_embedded_images(
        html_text
    )
    if checkpoint_template_signature(template) != checkpoint_template_signature(
        published_template
    ):
        return False
    checkpoint["html_template"] = bind_asset_tokens(
        published_template, published_assets
    )
    try:
        draft_checkpoint.save_path(
            revision_sidecar_path(artifact_path),
            stage="author-ready",
            state=checkpoint,
        )
    except (OSError, ValueError):
        return False
    return True


def revision_sidecar_path(source_path: Path) -> Path:
    """Return the durable revision-state path beside a published poster."""

    return source_path.with_name(f"{source_path.name}.poster-state.json")


def revision_checkpoint_payload(
    state: dict[str, Any],
    *,
    html_template: str,
    page_plan: dict[str, Any],
    visual_iteration: int,
) -> dict[str, Any]:
    """Carry durable grounding and content-structure state into each revision workspace."""

    required = (
        "source_text",
        "asset_inputs",
        "asset_sha256s",
        "source_figure_sha256s",
        "warnings",
        "paper_source",
        "content_budget",
        "visual_preferences",
        "design_reference",
        "visual_design",
        "authoring_request",
        "author_model_role",
    )
    payload = {
        **{name: state[name] for name in required},
        "source_figure_sha256s": list(state["source_figure_sha256s"]),
        "page_plan": page_plan,
        "html_template": html_template,
        "visual_iteration": visual_iteration,
    }
    for name in ("source_figure_observations", "reference_image", "editing_issue"):
        if name in state:
            payload[name] = state[name]
    return payload


def checkpoint_template_signature(html_template: str) -> str:
    """Ignore only deterministic asset annotations added during publication."""

    without_figure_hashes = re.sub(
        r"\s+data-source-figure-sha256=(?:\"[0-9a-f]{64}\"|'[0-9a-f]{64}')",
        "",
        html_template,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(?:asset-sha256://[0-9a-f]{64}|asset://\d+)",
        "asset://*",
        without_figure_hashes,
    )


def bind_asset_tokens(
    html_template: str,
    assets: list[dict[str, str]],
) -> str:
    """Bind each durable image position to the bytes published at that position."""

    bound = html_template
    for asset in sorted(assets, key=lambda item: len(item["token"]), reverse=True):
        bound = bound.replace(
            asset["token"], f"asset-sha256://{asset['content_sha256']}"
        )
    return bound


def visual_content_brief(
    page_plan: dict[str, Any],
    *,
    displayed_html: str,
    user_direction: str = "",
) -> dict[str, Any]:
    """Expose current direction, page geometry and the displayed module bindings."""

    snapshot = scientific_snapshot.scientific_content_snapshot(displayed_html)
    width = page_plan.get("width_mm")
    readability_reference = (
        planning.typography_metrics(float(width))
        if isinstance(width, (int, float))
        and not isinstance(width, bool)
        and math.isfinite(float(width))
        and float(width) > 0
        else {}
    )
    brief = {
        "grounded_authority": {
            "content_modules": [
                {
                    "id": module_id,
                    "semantic_roles": entry["semantic_roles"],
                    "priority": entry["priority"],
                    "source_label": "; ".join(entry["source_labels"]),
                    "figure_sha256s": list(entry["source_figure_sha256s"]),
                }
                for module_id, entries in snapshot["modules"].items()
                for entry in entries
            ],
        },
        "page": {
            "width_mm": page_plan.get("width_mm"),
            "height_mm": page_plan.get("height_mm"),
        },
        "readability_reference": readability_reference,
    }
    if user_direction.strip():
        brief["user_direction"] = user_direction
    return brief


def visual_revision_feedback(
    receipt_path: str,
    *,
    parent_html_sha256: str,
    content_budget: dict[str, Any] | None,
) -> tuple[
    str,
    int,
    frozenset[str],
    frozenset[str],
    dict[str, Any],
]:
    """Return bound feedback, review iteration and permitted edit targets."""

    receipt = review_contract.load_receipt(
        receipt_path,
        expected_html_sha256=parent_html_sha256,
    )
    review_contract.validate_issue_targets(
        receipt.get("critical_issues"),
        grounded_module_ids=_content_module_ids(content_budget),
    )
    feedback = review_contract.format_revision_feedback(receipt)
    next_iteration = int(receipt["iteration"]) + 1
    operations = frozenset(
        str(issue["operation"]) for issue in receipt["critical_issues"]
    )
    content_replan_targets = frozenset(
        str(target).strip()
        for issue in receipt["critical_issues"]
        if issue["operation"] == "content-replan"
        for target in issue["targets"]
    )
    return (
        feedback,
        next_iteration,
        operations,
        content_replan_targets,
        receipt,
    )


def validate_content_replan_targets(
    value: Any,
    *,
    content_budget: dict[str, Any] | None,
) -> frozenset[str]:
    """Bind explicit copy-edit authority to existing grounded module ids."""

    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("content_replan_targets must be a non-empty array")
    if any(not isinstance(target, str) or not target.strip() for target in value):
        raise ValueError("content_replan_targets must contain non-empty strings")
    valid_ids = set(_content_module_ids(content_budget))
    targets = {target.strip() for target in value}
    unknown = sorted(targets - valid_ids)
    if unknown:
        raise ValueError(
            "content-replan targets must be grounded module ids: " + ", ".join(unknown),
        )
    return frozenset(targets)


def _content_module_ids(
    content_budget: dict[str, Any] | None,
) -> frozenset[str]:
    """Return the current grounded module owners for revision target validation."""

    modules = (content_budget or {}).get("content_modules")
    if not isinstance(modules, list):
        return frozenset()
    return frozenset(
        str(module.get("id") or "").strip()
        for module in modules
        if isinstance(module, dict) and str(module.get("id") or "").strip()
    )


def vlm_revision_mode(
    operations: frozenset[str] | set[str] | list[str],
) -> str:
    """Route a receipt-directed revision through the narrowest safe edit boundary."""

    normalized = {str(operation) for operation in operations}
    if "content-replan" in normalized:
        return "content-replan"
    if "reflow" in normalized:
        # Reflow needs layout-shell authority because wrapper topology may change
        # while the scientific snapshot remains frozen.
        return "full-layout"
    return "full-layout"


def manual_revision_mode(value: object) -> str:
    """Validate the public revision authority requested by the caller."""

    requested = str(value or "").strip()
    if not requested:
        return "full-layout"
    if requested in {"full-layout", "content-replan"}:
        return requested
    raise ValueError("revision_mode must be full-layout or content-replan")


def revision_page_policy(
    *,
    stored_page_plan: Any,
    page: dict[str, Any],
) -> dict[str, Any]:
    """Freeze the rendered draft canvas for every later visual revision."""

    if isinstance(stored_page_plan, dict):
        plan = dict(stored_page_plan)
    else:
        plan = {
            "strategy": "fixed",
            "width_mm": page.get("width_mm"),
            "height_mm": page.get("height_mm"),
        }
    height = page.get("height_mm")
    source_strategy = plan.get("source_page_strategy", plan.get("strategy"))
    source_min_height = plan.get(
        "source_min_height_mm", plan.get("min_height_mm", height)
    )
    source_max_height = plan.get(
        "source_max_height_mm", plan.get("max_height_mm", height)
    )
    plan.update(
        {
            "strategy": "fixed",
            "source_page_strategy": source_strategy,
            "source_min_height_mm": source_min_height,
            "source_max_height_mm": source_max_height,
            "width_mm": page.get("width_mm"),
            "height_mm": height,
            "min_height_mm": height,
            "max_height_mm": height,
        }
    )
    return plan


def validate_revision_selection(
    value: object,
    *,
    parent_sha256: str,
    source_html: str,
) -> dict[str, Any]:
    """Validate that a live selection still matches the exact source HTML."""

    if not isinstance(value, dict):
        raise SelectionStateError(
            "candidate_validation_failed", "selection_state must be an object"
        )
    selection = dict(value)
    if selection.get("source_html_sha256") != parent_sha256:
        raise SelectionStateError(
            "stale_selection", "Selection belongs to different HTML bytes."
        )
    poster_id = str(selection.get("poster_id") or "").strip()
    identities = poster_contract.poster_identity_map(source_html)
    if not poster_id or poster_id not in identities:
        raise SelectionStateError(
            "invalid_selection",
            "Selection does not identify a stable data-poster-id in the source HTML.",
        )
    expected = identities[poster_id]
    for name in ("poster_module", "semantic_roles", "module_priority"):
        if str(selection.get(name) or "") != expected.get(name, ""):
            raise SelectionStateError(
                "invalid_selection",
                f"Selection {name} does not match the source HTML.",
            )
    return selection
