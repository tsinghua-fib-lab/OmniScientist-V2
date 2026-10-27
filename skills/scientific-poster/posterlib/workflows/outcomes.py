"""Public outcome construction and warning helpers for poster workflows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import poster_contract

from posterlib.sources import PaperSourceError

from . import deadlines, decision_request


def workflow_issues(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Read phase-owned failures, including saved single-issue checkpoints."""

    raw = value.get("workflow_issues")
    issues = {str(phase): dict(issue) for phase, issue in raw.items()
              if isinstance(issue, Mapping)} if isinstance(raw, Mapping) else {}
    legacy = value.get("editing_issue")
    if isinstance(legacy, Mapping):
        phase = str(legacy.get("phase") or (
            "planning" if legacy.get("code") == "content_audit_unavailable" else "editing"
        ))
        issues.setdefault(phase, dict(legacy))
    return issues


def _store_workflow_issues(state: dict[str, Any], issues: dict[str, dict[str, Any]]) -> None:
    state.pop("editing_issue", None)
    if issues:
        state["workflow_issues"] = issues
    else:
        state.pop("workflow_issues", None)


def update_workflow_issue(
    state: dict[str, Any], phase: str, issue: Mapping[str, Any] | None,
) -> None:
    """Replace or resolve one stage while retaining every other unfinished stage."""

    issues = workflow_issues(state)
    if issue is None:
        issues.pop(phase, None)
    else:
        issues[phase] = dict(issue)
    _store_workflow_issues(state, issues)


def merge_workflow_issues(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """Carry unfinished stages and an editor boundary failure into a retained result."""

    issues = {**workflow_issues(target), **workflow_issues(source)}
    error = source.get("error_info")
    if isinstance(error, Mapping):
        issues[str(error.get("phase") or "editing")] = dict(error)
    _store_workflow_issues(target, issues)
    history = list(dict.fromkeys([
        *target.get("warning_history", []), *source.get("warning_history", []),
    ]))
    if history:
        target["warning_history"] = history


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
    issues = workflow_issues(details)
    _store_workflow_issues(details, issues)
    details["requires_approval"] = False
    if decision_reason:
        try:
            details["decision_request"] = decision_request.build_request(
                details,
                reason=decision_reason,
            )
        except ValueError:
            pass
    outcome = poster_contract.outcome_result(code, summary=summary, **details)
    if outcome["status"] == "ok" and issues:
        outcome["status"] = "partial"
        outcome["summary"] = summary + " Unresolved workflow stages: " + "; ".join(
            f"{phase}: {issue.get('summary') or issue.get('code') or phase}"
            for phase, issue in issues.items()
        )
    return outcome


def revision_model_timed_out(result: dict[str, Any]) -> bool:
    """Recognize a bounded model timeout without masking other model failures."""

    issue = workflow_issues(result).get("editing") or result.get("error_info")
    return isinstance(issue, Mapping) and issue.get("reason") in {
        "model_timeout", "deadline_exhausted",
    }


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
    merge_workflow_issues(result, failure)
    diagnostic = failure.get("diagnostic_candidate")
    if isinstance(diagnostic, Mapping):
        result["diagnostic_candidate"] = dict(diagnostic)
    result.update(
        {
            "workspace": str(best.get("workspace") or ""),
            "visual_quality_state": "revision-required",
            "retry_from_checkpoint": bool(
                isinstance(diagnostic, Mapping)
                and diagnostic.get("revision_state_persisted") is True
            ),
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
        deadlines.VISUAL_LOOP_TIMEOUT_WARNING,
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
            "visual_quality_state": (
                "revision-required"
                if details.get("visual_quality_state") == "revision-required"
                else "awaiting-review"
            ),
            "warnings": warnings,
        }
    )
    if review_mode == "vlm":
        try:
            details["decision_request"] = decision_request.build_request(
                details,
                reason="reviewer-unavailable",
            )
        except ValueError:
            pass
    return poster_contract.outcome_result(
        "visual_review_unavailable",
        summary="The exact rendered candidate is awaiting image-capable review.",
        **details,
    )


def inspection_deadline_result() -> dict[str, Any]:
    """Report a bounded render timeout without claiming the candidate passed."""

    return poster_contract.outcome_result(
        "inspection_unavailable",
        summary=(
            "Poster work reached the shared workflow deadline; resume from the "
            "returned checkpoint when one is available."
        ),
        requires_approval=False,
        visual_quality_state="not-reviewable",
        error_info={
            "code": "inspection_unavailable", "reason": "deadline_exhausted",
            "phase": "editing",
            "summary": "Poster work reached the shared workflow deadline.",
        },
        warnings=[deadlines.VISUAL_LOOP_TIMEOUT_WARNING],
    )


def error_result(
    code: str, message: str, *, error_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one stable scientific-poster error outcome."""

    result = poster_contract.outcome_result(
        code,
        summary=f"scientific-poster did not complete: {message}",
    )
    if result["status"] == "error":
        result["error"] = message
    if error_info is not None:
        result["error_info"] = dict(error_info)
    return result


def paper_source_failure(exc: PaperSourceError) -> dict[str, Any]:
    """Translate a paper-source failure into the public workflow outcome contract."""

    if exc.code == "missing_capability":
        from posterlib.runtime.dependencies import missing_result

        return missing_result(
            "pdf-reading",
            dependency="pymupdf",
            stage="poster.prepare-source",
            error=exc,
        )
    return error_result(exc.code, str(exc))
