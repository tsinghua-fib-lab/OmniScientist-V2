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

MAX_INSPECTION_REPAIR_ATTEMPTS = 2


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


def inspection_repair_attempt(value: Any) -> int:
    """Read resumable deterministic-repair progress without trusting host input."""

    raw = value.get("inspection_repair_attempt", 0) if isinstance(value, dict) else 0
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if 0 <= raw <= MAX_INSPECTION_REPAIR_ATTEMPTS else 0


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
    inspection_repair_attempt: int = 0,
    preserve_pending_visual_revision: bool = False,
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
        "inspection_repair_attempt": inspection_repair_attempt,
    }
    pending = state.get("pending_visual_revision")
    if preserve_pending_visual_revision and isinstance(pending, dict):
        payload["pending_visual_revision"] = dict(pending)
    return payload


def persist_pending_visual_revision(
    result: dict[str, Any],
    *,
    receipt_path: str,
) -> dict[str, Any]:
    """Bind one revision-required receipt to the active author checkpoint."""

    workspace = _result_workspace(result)
    checkpoint = draft_checkpoint.load(workspace)
    if checkpoint is None or checkpoint.get("stage") != "author-ready":
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "A validated author checkpoint is required before visual revision.",
        )
    parent_html_sha256 = _result_html_sha256(result)
    reference = checkpoint.get("design_reference")
    reference_image_sha256 = (
        str(reference.get("image_sha256") or "").strip()
        if isinstance(reference, dict)
        else ""
    )
    path = _workspace_receipt_path(workspace, receipt_path)
    receipt = visual_review.load_receipt(
        path,
        expected_html_sha256=parent_html_sha256,
        expected_reference_image_sha256=reference_image_sha256,
    )
    if receipt.get("quality_state") != "revision-required":
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Only a revision-required receipt can be checkpointed for retry.",
        )
    next_iteration = int(receipt["iteration"]) + 1
    if int(checkpoint["visual_iteration"]) != int(receipt["iteration"]):
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Visual review iteration does not match the active author checkpoint.",
        )
    if next_iteration > visual_review.MAX_VISUAL_REVISIONS:
        raise visual_review.VisualReviewError(
            "visual_review_failed", "visual revision limit has been reached"
        )
    pending = {
        "parent_html_sha256": parent_html_sha256,
        "visual_review_path": str(path),
        "receipt_sha256": str(receipt["receipt_sha256"]),
        "reference_image_sha256": str(receipt["reference_image_sha256"]),
        "screenshot_sha256": str(receipt["screenshot_sha256"]),
        "visual_evidence_sha256": str(receipt["visual_evidence_sha256"]),
        "next_iteration": next_iteration,
        "operations": sorted(
            {str(issue["operation"]) for issue in receipt["critical_issues"]}
        ),
    }
    draft_checkpoint.save(
        workspace,
        stage="author-ready",
        state={**checkpoint, "pending_visual_revision": pending},
    )
    return pending


def resume_pending_visual_revision(
    result: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Load one exact pending receipt without invoking the visual reviewer again."""

    workspace_value = str(result.get("workspace") or "").strip()
    if not workspace_value:
        return None
    workspace = Path(workspace_value)
    checkpoint = draft_checkpoint.load(workspace)
    if checkpoint is None or checkpoint.get("stage") != "author-ready":
        return None
    raw_pending = checkpoint.get("pending_visual_revision")
    if not isinstance(raw_pending, dict):
        return None
    pending = dict(raw_pending)
    parent_html_sha256 = _result_html_sha256(result)
    if pending["parent_html_sha256"] != parent_html_sha256:
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Pending visual revision belongs to different HTML bytes.",
        )
    path = _workspace_receipt_path(workspace, str(pending["visual_review_path"]))
    reference = checkpoint.get("design_reference")
    reference_image_sha256 = (
        str(reference.get("image_sha256") or "").strip()
        if isinstance(reference, dict)
        else ""
    )
    if not reference_image_sha256:
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Pending visual revision has no bound design reference.",
        )
    receipt = visual_review.load_receipt(
        path,
        expected_html_sha256=parent_html_sha256,
        expected_reference_image_sha256=reference_image_sha256,
    )
    expected = {
        "receipt_sha256": receipt["receipt_sha256"],
        "reference_image_sha256": receipt["reference_image_sha256"],
        "screenshot_sha256": receipt["screenshot_sha256"],
        "visual_evidence_sha256": receipt["visual_evidence_sha256"],
        "next_iteration": int(receipt["iteration"]) + 1,
        "operations": sorted(
            {str(issue["operation"]) for issue in receipt["critical_issues"]}
        ),
    }
    if receipt.get("quality_state") != "revision-required" or any(
        pending[field] != value for field, value in expected.items()
    ):
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Pending visual revision no longer matches its bound receipt.",
        )
    return pending, receipt


def pending_visual_regression_feedback(pending: dict[str, Any]) -> list[str]:
    """Return measured feedback captured from the latest rejected revision."""

    raw_feedback = pending.get("regressed_inspection_feedback")
    if not isinstance(raw_feedback, list):
        return []
    return [
        str(item).strip()
        for item in raw_feedback
        if isinstance(item, str) and item.strip()
    ]


def persist_pending_visual_regression(
    result: dict[str, Any],
    *,
    inspection_feedback: list[str],
) -> None:
    """Keep rejected-candidate geometry beside its still-pending VLM receipt."""

    feedback = [
        str(item).strip()
        for item in inspection_feedback
        if isinstance(item, str) and item.strip()
    ]
    if not feedback:
        return
    workspace = _result_workspace(result)
    checkpoint = draft_checkpoint.load(workspace)
    if checkpoint is None or checkpoint.get("stage") != "author-ready":
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "A validated author checkpoint is required to preserve revision feedback.",
        )
    pending = checkpoint.get("pending_visual_revision")
    parent_html_sha256 = _result_html_sha256(result)
    if (
        not isinstance(pending, dict)
        or pending.get("parent_html_sha256") != parent_html_sha256
    ):
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Rejected revision feedback has no matching pending visual receipt.",
        )
    updated_pending = {
        **pending,
        "regressed_inspection_feedback": feedback,
    }
    draft_checkpoint.save(
        workspace,
        stage="author-ready",
        state={**checkpoint, "pending_visual_revision": updated_pending},
    )


def clear_pending_visual_revision(result: dict[str, Any]) -> None:
    """Clear a pending receipt only after the same candidate explicitly passes."""

    workspace_value = str(result.get("workspace") or "").strip()
    if not workspace_value:
        return
    workspace = Path(workspace_value)
    checkpoint = draft_checkpoint.load(workspace)
    if checkpoint is None or checkpoint.get("stage") != "author-ready":
        return
    pending = checkpoint.get("pending_visual_revision")
    if not isinstance(pending, dict):
        return
    if pending.get("parent_html_sha256") != result.get("html_sha256"):
        return
    cleared = dict(checkpoint)
    cleared.pop("pending_visual_revision", None)
    draft_checkpoint.save(workspace, stage="author-ready", state=cleared)


def _result_workspace(result: dict[str, Any]) -> Path:
    workspace_value = str(result.get("workspace") or "").strip()
    if not workspace_value:
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Visual revision requires a durable task workspace.",
        )
    raw_workspace = Path(workspace_value).expanduser()
    if raw_workspace.is_symlink():
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Visual revision workspace is not a regular directory.",
        )
    workspace = raw_workspace.resolve()
    if not workspace.is_dir():
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Visual revision workspace is not a regular directory.",
        )
    return workspace


def _result_html_sha256(result: dict[str, Any]) -> str:
    expected = str(result.get("html_sha256") or "").strip()
    html_path = str(result.get("html_path") or "").strip()
    if not html_path:
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Visual revision requires the exact published HTML path.",
        )
    try:
        actual = visual_review.sha256_file(html_path)
    except visual_review.VisualReviewError as exc:
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Visual revision HTML is unavailable for checkpoint binding.",
        ) from exc
    if not expected or actual != expected:
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Visual revision HTML bytes do not match the published candidate.",
        )
    return expected


def _workspace_receipt_path(workspace: Path, value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_relative_to(workspace.resolve()):
        raise visual_review.VisualReviewError(
            "visual_review_invalid",
            "Visual review receipt must remain inside the task workspace.",
        )
    return path


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
    *,
    requested_mode: str = "",
) -> str:
    """Choose the narrowest model boundary for one validated VLM repair."""

    normalized = {str(operation) for operation in operations}
    if "content-replan" in normalized:
        return "content-replan"
    if "reflow" in normalized or requested_mode == "full-layout":
        return "full-layout"
    # A pure restyle can preserve the editable DOM. Reflow cannot: modules may
    # need to move across independently flowing groups, and a prior stylesheet
    # attempt may explicitly escalate to full-layout.
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


def advance_best_checkpoint(
    result: dict[str, Any],
    *,
    inspection_repair_attempt: int | None = None,
    visual_iteration: int | None = None,
) -> None:
    """Advance loop progress while keeping the checkpoint bound to active HTML."""

    updates: dict[str, int] = {}
    if inspection_repair_attempt is not None:
        updates["inspection_repair_attempt"] = inspection_repair_attempt
    if visual_iteration is not None:
        updates["visual_iteration"] = visual_iteration
    result.update(updates)
    workspace_value = str(result.get("workspace") or "").strip()
    if not workspace_value or not updates:
        return
    workspace = Path(workspace_value)
    checkpoint = draft_checkpoint.load(workspace)
    if checkpoint is None or checkpoint.get("stage") != "author-ready":
        return
    draft_checkpoint.save(
        workspace,
        stage="author-ready",
        state={**checkpoint, **updates},
    )
