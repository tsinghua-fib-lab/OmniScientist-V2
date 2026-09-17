"""Durable revision checkpoints, visual progress, and selection validation."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import poster_core

from posterlib.content import html_contract, planning, scientific_snapshot
from posterlib.visual import visual_review

from . import draft_checkpoint


class SelectionStateError(ValueError):
    """A live-preview selection does not identify the source HTML."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def visual_iteration(value: Any) -> int:
    """Normalize the bounded screenshot-review iteration for publication."""

    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise visual_review.VisualReviewError(
            "visual_review_invalid", "visual_iteration must be an integer"
        )
    if not 0 <= value <= visual_review.MAX_VISUAL_REVISIONS:
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            f"visual_iteration must be between 0 and {visual_review.MAX_VISUAL_REVISIONS}",
        )
    return value


def revision_checkpoint_state(
    source_path: Path,
    *,
    source_html: str,
    html_sha256: str,
) -> dict[str, Any] | None:
    """Restore content-structure and adaptive-page state beside a durable artifact."""

    template, _assets = html_contract.tokenize_embedded_images(source_html)
    source_signature = checkpoint_template_signature(template)
    sidecar = draft_checkpoint.load_path(revision_sidecar_path(source_path))
    if sidecar is not None and sidecar.get("stage") == "author-ready":
        checkpoint_template = str(sidecar.get("html_template") or "")
        if checkpoint_template_signature(checkpoint_template) == source_signature:
            return {
                **sidecar,
                "html_sha256": html_sha256,
                "artifact_path": str(source_path),
            }
    for workspace in (source_path.parent, source_path.parent.parent):
        checkpoint = draft_checkpoint.load(workspace)
        if checkpoint is None or checkpoint.get("stage") != "author-ready":
            continue
        checkpoint_template = str(checkpoint.get("html_template") or "")
        if checkpoint_template_signature(checkpoint_template) != source_signature:
            continue
        return {
            **checkpoint,
            "html_sha256": html_sha256,
            "artifact_path": str(source_path),
        }
    return None


def persist_revision_sidecar(
    workspace: Path,
    *,
    artifact_path: Path,
    html_text: str,
    checkpoint_state: dict[str, Any] | None = None,
) -> bool | None:
    """Carry the grounded checkpoint beside an immutable published HTML artifact."""

    if checkpoint_state is None:
        checkpoint = draft_checkpoint.load(workspace)
        if checkpoint is None or checkpoint.get("stage") != "author-ready":
            return None
    else:
        checkpoint = dict(checkpoint_state)
    template = str(checkpoint.get("html_template") or "")
    published_template, _assets = html_contract.tokenize_embedded_images(html_text)
    if checkpoint_template_signature(template) != checkpoint_template_signature(
        published_template
    ):
        return None
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
    state: dict[str, Any] | None,
    *,
    html_template: str,
    page_plan: dict[str, Any],
    visual_iteration: int,
) -> dict[str, Any] | None:
    """Carry durable grounding and content-structure state into each revision workspace."""

    required = (
        "source_text",
        "authoring_request",
        "asset_inputs",
        "asset_sha256s",
        "source_figure_sha256s",
        "warnings",
        "paper_source",
        "content_budget",
        "visual_preferences",
        "design_reference",
        "visual_design",
    )
    if state is None or any(name not in state for name in required):
        return None
    source_figure_sha256s = state["source_figure_sha256s"]
    if (
        not isinstance(source_figure_sha256s, (list, tuple))
        or not isinstance(state["visual_preferences"], dict)
        or not isinstance(state["design_reference"], dict)
        or not isinstance(state["visual_design"], dict)
    ):
        return None
    payload = {
        **{name: state[name] for name in required},
        "source_figure_sha256s": list(source_figure_sha256s),
        "page_plan": page_plan,
        "html_template": html_template,
        "visual_iteration": visual_iteration,
    }
    return payload


def revision_checkpoint_source(
    workspace: Path,
    runtime_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Prefer the validated workspace checkpoint over mutable runtime cache state."""

    persisted = draft_checkpoint.load(workspace)
    if persisted is not None and persisted.get("stage") == "author-ready":
        return persisted
    return runtime_state


def checkpoint_template_signature(html_template: str) -> str:
    """Ignore only deterministic asset annotations added during publication."""

    without_figure_hashes = re.sub(
        r"\s+data-source-figure-sha256=(?:\"[0-9a-f]{64}\"|'[0-9a-f]{64}')",
        "",
        html_template,
        flags=re.IGNORECASE,
    )
    return re.sub(r"asset://\d+", "asset://*", without_figure_hashes)


def visual_content_brief(
    content_budget: dict[str, Any] | None,
    page_plan: dict[str, Any],
    *,
    displayed_html: str,
) -> dict[str, Any]:
    """Expose immutable authority separately from the current displayed snapshot."""

    budget = content_budget if isinstance(content_budget, dict) else {}
    modules = budget.get("content_modules")
    content_contract = page_plan.get("content_contract")
    content_contract = (
        dict(content_contract) if isinstance(content_contract, dict) else {}
    )
    grounded_modules = (
        [
            {
                key: module.get(key)
                for key in (
                    "id",
                    "section_id",
                    "title",
                    "semantic_roles",
                    "priority",
                    "visual_kind",
                    "text",
                    "detail_points",
                    "takeaway",
                    "source_label",
                    "figure_sha256s",
                    "equations",
                )
            }
            for module in modules
            if isinstance(module, dict)
        ]
        if isinstance(modules, list)
        else []
    )
    width = page_plan.get("width_mm")
    readability_reference = (
        planning.typography_metrics(float(width))
        if isinstance(width, (int, float))
        and not isinstance(width, bool)
        and math.isfinite(float(width))
        and float(width) > 0
        else {}
    )
    return {
        "grounded_authority": {
            "organization_mode": str(budget.get("organization_mode") or ""),
            "focal_role": str(
                budget.get("focal_role") or page_plan.get("focal_role") or ""
            ),
            "sections": budget.get("sections")
            if isinstance(budget.get("sections"), list)
            else [],
            "content_modules": grounded_modules,
        },
        "displayed_content_snapshot": scientific_snapshot.scientific_content_snapshot(
            displayed_html
        ),
        "page": {
            "width_mm": page_plan.get("width_mm"),
            "height_mm": page_plan.get("height_mm"),
        },
        "readability_reference": readability_reference,
        "content_contract": content_contract,
    }


def visual_revision_feedback(
    receipt_path: str,
    *,
    parent_html_sha256: str,
    reference_image_sha256: str,
    content_budget: dict[str, Any] | None,
) -> tuple[str, int, frozenset[str], frozenset[str]]:
    """Return bound feedback, next iteration, and requested repair operations."""

    receipt = visual_review.load_receipt(
        receipt_path,
        expected_html_sha256=parent_html_sha256,
        expected_reference_image_sha256=reference_image_sha256,
    )
    feedback = visual_review.revision_feedback(receipt)
    next_iteration = int(receipt["iteration"]) + 1
    if next_iteration > visual_review.MAX_VISUAL_REVISIONS:
        raise visual_review.VisualReviewError(
            "visual_review_failed", "visual revision limit has been reached"
        )
    operations = frozenset(
        str(issue["operation"]) for issue in receipt["critical_issues"]
    )
    targets = content_replan_target_ids(receipt, content_budget=content_budget)
    return feedback, next_iteration, operations, targets


def content_replan_target_ids(
    receipt: dict[str, Any],
    *,
    content_budget: dict[str, Any] | None,
) -> frozenset[str]:
    """Validate VLM copy-edit targets against immutable grounded modules."""

    targets = {
        str(target).strip()
        for issue in receipt.get("critical_issues", [])
        if isinstance(issue, dict) and issue.get("operation") == "content-replan"
        for target in issue.get("targets", [])
    }
    if not targets:
        return frozenset()
    try:
        return validate_content_replan_targets(
            sorted(targets),
            content_budget=content_budget,
        )
    except ValueError as exc:
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            str(exc),
        ) from exc


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
    modules = (content_budget or {}).get("content_modules")
    valid_ids = (
        {
            str(module.get("id") or "").strip()
            for module in modules
            if isinstance(module, dict) and str(module.get("id") or "").strip()
        }
        if isinstance(modules, list)
        else set()
    )
    targets = {target.strip() for target in value}
    unknown = sorted(targets - valid_ids)
    if unknown:
        raise ValueError(
            "content-replan targets must be grounded module ids: " + ", ".join(unknown),
        )
    return frozenset(targets)


def vlm_revision_mode(
    operations: frozenset[str] | set[str] | list[str],
) -> str:
    """Keep automatic visual repair inside the canonical draft DOM."""

    normalized = {str(operation) for operation in operations}
    if "content-replan" in normalized:
        return "content-replan"
    # The deterministic draft body uses CSS multi-column flow, so reflow is a
    # stylesheet concern. Automatic review must not replace the verified DOM.
    return "style-only"


def manual_revision_mode(value: object) -> str:
    """Keep ordinary visual feedback compact unless the caller opts into a replan."""

    requested = str(value or "").strip()
    if requested in {"style-only", "full-layout", "content-replan"}:
        return requested
    return "style-only"


def revision_page_policy(
    *,
    stored_page_plan: Any,
    page: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Keep explicit pages fixed and retain bounded height freedom for repair."""

    if isinstance(stored_page_plan, dict):
        plan = dict(stored_page_plan)
    else:
        plan = {
            "strategy": "fixed",
            "width_mm": page.get("width_mm"),
            "height_mm": page.get("height_mm"),
        }
    adaptive = (
        plan.get("strategy") == "auto"
        and isinstance(plan.get("min_height_mm"), (int, float))
        and isinstance(plan.get("max_height_mm"), (int, float))
    )
    if adaptive:
        return plan, True
    height = page.get("height_mm")
    plan.update(
        {
            "strategy": "fixed",
            "width_mm": page.get("width_mm"),
            "height_mm": height,
            "min_height_mm": height,
            "max_height_mm": height,
        }
    )
    return plan, False


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
    identities = poster_core.poster_identity_map(source_html)
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
