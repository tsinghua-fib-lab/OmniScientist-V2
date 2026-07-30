"""Public outcome construction and warning helpers for poster workflows."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import poster_core

from posterlib.sources import paper_source

from . import decision_checkpoint, draft_checkpoint, runtime_budget


def visual_outcome(
    result: dict[str, Any],
    code: str,
    summary: str,
    *,
    decision_reason: str = "",
) -> dict[str, Any]:
    """Replace only stable outcome fields while retaining published artifacts."""

    details = {
        key: value
        for key, value in result.items()
        if key not in {"status", "outcome", "summary", "blocking", "recoverable"}
    }
    details["requires_approval"] = code == "visual_review_passed"
    if decision_reason:
        try:
            details["decision_request"] = decision_checkpoint.build_request(
                details,
                reason=decision_reason,
            )
        except ValueError:
            pass
    return poster_core.outcome_result(code, summary=summary, **details)


def revision_model_timed_out(result: dict[str, Any]) -> bool:
    """Recognize a bounded model timeout without masking other model failures."""

    outcome = result.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("code") != "llm_error":
        return False
    detail = " ".join(
        str(result.get(field) or "") for field in ("error", "summary")
    ).casefold()
    return "timed out" in detail or "runtime budget was exhausted" in detail


def revision_timeout_error(
    best: dict[str, Any],
    failure: dict[str, Any],
) -> dict[str, Any]:
    """Preserve the exact candidate and expose a normal bounded-revision checkpoint."""

    detail = str(failure.get("error") or failure.get("summary") or "").strip()
    message = "Visual revision timed out before a reviewable candidate was published."
    if detail:
        message = f"{message} {detail}"
    result = dict(best)
    workspace = str(best.get("workspace") or "")
    retry_from_checkpoint = False
    if workspace:
        checkpoint = draft_checkpoint.load(Path(workspace))
        retry_from_checkpoint = isinstance(
            (checkpoint or {}).get("pending_visual_revision"), dict
        )
    result.update(
        {
            "workspace": workspace,
            "visual_quality_state": "revision-required",
            "retry_from_checkpoint": retry_from_checkpoint,
        }
    )
    append_warning_once(result, message)
    return visual_outcome(
        result,
        "visual_revision_required",
        message,
        decision_reason="workflow-budget-exhausted",
    )


def append_warning_once(result: dict[str, Any], warning: str) -> None:
    """Append one warning while preserving stable insertion order."""

    warnings = list(result.get("warnings") or [])
    if warning not in warnings:
        warnings.append(warning)
    result["warnings"] = warnings


def visual_loop_pending(result: dict[str, Any]) -> dict[str, Any]:
    """Return the current fully published candidate after exhausting loop budget."""

    return visual_review_unavailable(
        result,
        runtime_budget.VISUAL_LOOP_TIMEOUT_WARNING,
    )


def visual_review_unavailable(
    result: dict[str, Any],
    warning: str,
) -> dict[str, Any]:
    """Preserve candidate bytes while clearing every stale visual-pass field."""

    details = {
        key: value
        for key, value in result.items()
        if key
        not in {
            "status",
            "outcome",
            "summary",
            "blocking",
            "recoverable",
            "requires_approval",
            "visual_review",
            "visual_review_path",
            "visual_review_sha256",
        }
    }
    warnings = list(details.get("warnings") or [])
    if warning and warning not in warnings:
        warnings.append(warning)
    review_mode = (
        "vlm" if details.get("visual_review_mode") == "vlm" else "deterministic-only"
    )
    details.update(
        {
            "requires_approval": False,
            "visual_review_mode": review_mode,
            "visual_quality_state": "awaiting-review",
            "warnings": warnings,
        }
    )
    if review_mode == "vlm":
        try:
            details["decision_request"] = decision_checkpoint.build_request(
                details,
                reason="reviewer-unavailable",
            )
        except ValueError:
            pass
    return poster_core.outcome_result(
        "visual_review_unavailable",
        summary="The exact rendered candidate is awaiting image-capable review.",
        **details,
    )


def rendered_inspection_blocked(
    result: dict[str, Any],
    warning: str = "",
    *,
    decision_reason: str = "",
) -> dict[str, Any]:
    """Expose the actual failed inspection without relabeling capabilities as geometry."""

    if warning:
        append_warning_once(result, warning)
    result["visual_review_mode"] = "not-run"
    result["visual_quality_state"] = "not-reviewable"
    inspection = result.get("inspection")
    outcome = (
        inspection.get("outcome")
        if isinstance(inspection, Mapping)
        else result.get("outcome")
    )
    code = str(outcome.get("code") or "") if isinstance(outcome, Mapping) else ""
    if code not in poster_core.OUTCOME_CONTRACTS:
        code = "inspection_unavailable"
    inspection_summary = (
        str(inspection.get("summary") or "").strip()
        if isinstance(inspection, Mapping)
        else str(result.get("summary") or "").strip()
    )
    if not inspection_summary:
        inspection_summary = (
            "The rendered poster still exceeds its physical delivery geometry."
            if code == "inspection_blocked"
            else "Rendered inspection did not produce a reviewable poster."
        )
    return visual_outcome(
        result,
        "visual_revision_required" if decision_reason else code,
        inspection_summary,
        decision_reason=decision_reason,
    )


def inspection_deadline_result() -> dict[str, Any]:
    """Report a bounded render timeout without claiming the candidate passed."""

    return poster_core.outcome_result(
        "inspection_unavailable",
        summary=(
            "Poster rendering reached the shared workflow deadline; the durable "
            "authoring checkpoint can be resumed."
        ),
        requires_approval=False,
        visual_quality_state="not-reviewable",
        warnings=[runtime_budget.VISUAL_LOOP_TIMEOUT_WARNING],
    )


def error_result(code: str, message: str) -> dict[str, Any]:
    """Build one stable scientific-poster error outcome."""

    result = poster_core.outcome_result(
        code,
        summary=f"scientific-poster did not complete: {message}",
    )
    if result["status"] == "error":
        result["error"] = message
    return result


def paper_source_failure(exc: paper_source.PaperSourceError) -> dict[str, Any]:
    """Translate a paper-source failure into the public workflow outcome contract."""

    if exc.code == "missing_capability":
        from posterlib.runtime.capability import missing_result

        return missing_result(
            "pdf-reading",
            dependency="pymupdf",
            stage="poster.prepare-source",
            error=exc,
        )
    return error_result(exc.code, str(exc))
