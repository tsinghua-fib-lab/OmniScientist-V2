"""Checked candidates and checkpoint selection for one poster editing session."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from posterlib.runtime.model_response import ModelBoundaryError

_UNAVAILABLE_INSPECTION_OUTCOMES = frozenset({
    "chromium_inspection_failed", "dom_evaluation_failed", "html_not_found",
    "inspection_failed", "inspection_output_failed", "inspection_unavailable",
    "invalid_inspection_options", "missing_capability", "screenshot_failed",
})


@dataclass(frozen=True)
class EditorCandidate:
    """Keep one exact HTML state with its own validation and browser evidence."""

    html: str
    validation: dict[str, Any]
    inspection: dict[str, Any] | None
    feedback: dict[str, Any]


@dataclass(frozen=True)
class EditorResult:
    """The selected candidate and the cause of an unfinished editing session."""

    candidate: EditorCandidate
    issue: dict[str, Any] | None = None


async def check_candidate(
    html: str,
    *,
    validate: Callable[[str], dict[str, Any]],
    inspect: Callable[[str], Awaitable[dict[str, Any]]] | None,
    supplied_validation: dict[str, Any] | None = None,
    supplied_inspection: dict[str, Any] | None = None,
) -> EditorCandidate:
    """Bind static validation and optional rendered measurements to exact bytes."""

    report = supplied_validation if supplied_validation is not None else validate(html)
    if report.get("status") != "ok":
        return EditorCandidate(html, report, None, {"status": "error", "diagnostics": report})
    warnings = {"validation_warnings": report["warnings"]} if report.get("warnings") else {}
    if inspect is None:
        return EditorCandidate(html, report, None, {"status": "ok", **warnings})
    rendered = supplied_inspection if supplied_inspection is not None else await inspect(html)
    feedback = {
        **{key: value for key, value in rendered.items() if not key.startswith("_")},
        **warnings, "static_valid": True,
    }
    measured = rendered.get("_rendered_inspection")
    return EditorCandidate(
        html, report, measured if isinstance(measured, dict) else rendered, feedback,
    )


class EditorCandidates:
    """Advance working and delivery checkpoints together with their evidence."""

    def __init__(
        self,
        source: EditorCandidate,
        *,
        validate: Callable[[str], dict[str, Any]],
        inspect: Callable[[str], Awaitable[dict[str, Any]]] | None,
        initial_composition: bool,
    ) -> None:
        self.source = self.current = source
        self.best: EditorCandidate | None = None
        self.last_failed_check: dict[str, Any] | None = None
        self.has_working_edit = False
        self.finishable = False
        self.validate = validate
        self.inspect = inspect
        self.initial_composition = initial_composition

    async def stage(self, html: str) -> dict[str, Any]:
        """Retain measured progress while preserving the best deliverable version."""

        if html == self.current.html:
            return {
                "status": "unchanged", "code": "revision_noop",
                "check": self.current.feedback,
                "message": "This batch leaves the HTML unchanged. Choose a different edit.",
            }
        proposal = await check_candidate(html, validate=self.validate, inspect=self.inspect)
        checked = proposal.feedback
        if not self.initial_composition and inspection_unavailable(checked):
            self.last_failed_check = checked
            raise ModelBoundaryError(
                "inspection_unavailable",
                "The revised candidate could not be measured; the checked version remains selected.",
            )
        incumbent_digest = _render_digest(self.current.inspection)
        proposal_digest = _render_digest(proposal.inspection)
        if (
            incumbent_digest is not None and proposal_digest == incumbent_digest
            and not _proposal_advances(self.current.feedback, checked, require_blocker_repair=True)
        ):
            before = self.current.feedback.get("report") or {}
            after = checked.get("report") or {}
            unmatched = sorted(set(after.get("unmatched_style_selectors") or [])
                               - set(before.get("unmatched_style_selectors") or []))
            return {
                "status": "diagnostic", "code": "revision_no_visible_change",
                "message": "The rendered pixels are unchanged. Check the matching CSS and wrapper arrangement.",
                **({"new_unmatched_selectors": unmatched} if unmatched else {}),
                "check": self.current.feedback,
            }
        if proposal.validation.get("status") != "ok" or (
            self.has_working_edit and not _proposal_advances(self.current.feedback, checked)
        ):
            self.last_failed_check = checked
            return {
                "status": "diagnostic", "code": "candidate_not_improved",
                "message": "The working draft is retained. Use the proposal's measured blockers to choose another edit.",
                "proposal_check": checked, "check": self.current.feedback,
            }
        if self.best is not None:
            improved = _proposal_advances(self.best.feedback, checked)
        elif self.initial_composition:
            # The first statically valid authored composition establishes the
            # delivery checkpoint, with any remaining layout defects attached.
            improved = True
        else:
            improved = _proposal_advances(self.source.feedback, checked)
        self.current = proposal
        self.has_working_edit = self.finishable = True
        if improved:
            self.best = proposal
        return {"status": "applied", "check": checked, "delivery_checkpoint_updated": improved}

    def ready(self) -> bool:
        """Hand off only a changed candidate with usable delivery measurements."""

        check = self.current.feedback
        return self.finishable and (
            check.get("status") == "ok"
            or (self.initial_composition and inspection_unavailable(check))
        )

    def restore(self, checkpoint: str) -> dict[str, Any]:
        """Restore HTML and its bound evidence as one checkpoint."""

        if checkpoint == "source":
            self.current = self.source
            self.finishable = False
        elif checkpoint == "last_candidate":
            if self.best is None:
                return {
                    "status": "error", "code": "candidate_checkpoint_unavailable",
                    "message": "No checked edited candidate has been recorded in this session.",
                }
            self.current = self.best
            self.finishable = True
        else:
            return {"status": "error", "code": "invalid_checkpoint",
                    "message": "checkpoint must be source or last_candidate"}
        return {"status": "restored", "checkpoint": checkpoint}

    def stopped(self, error: ModelBoundaryError) -> EditorResult:
        """Return a valid checkpoint and the precise unfinished-session evidence."""

        selected = self.best or self.source
        if selected.validation.get("status") != "ok":
            raise error
        return EditorResult(selected, {
            **error.as_issue(),
            "phase": "editing",
            "last_check": self.last_failed_check or self.current.feedback,
        })


def _blocking_issues(check: Mapping[str, Any] | None) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Key deterministic blockers by measured owner so progress stays comparable."""

    if not isinstance(check, Mapping):
        return {}
    issues: list[Any] = []
    diagnostics = check.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        issues.extend(diagnostics.get("issues") or [])
    report = check.get("report")
    if isinstance(report, Mapping):
        issues.extend(item for item in report.get("warnings") or []
                      if isinstance(item, Mapping) and item.get("severity") == "error")
    blockers = {}
    for item in issues:
        if not isinstance(item, Mapping):
            continue
        observed = item.get("observed")
        observed = observed if isinstance(observed, Mapping) else {}
        owner = str(item.get("poster_id") or item.get("module_id")
                    or observed.get("module_a") or item.get("sha256") or "poster")
        peer = str(observed.get("module_b") or item.get("role") or "")
        blockers[(str(item.get("code") or "unknown"), owner, peer)] = dict(item)
    return blockers


def _blocker_magnitudes(issue: Mapping[str, Any]) -> tuple[float, ...]:
    observed = issue.get("observed")
    if not isinstance(observed, Mapping):
        return ()
    return tuple(max(0.0, float(value)) for key, value in sorted(observed.items())
                 if any(token in str(key) for token in ("overflow_", "overlap_"))
                 and not isinstance(value, bool) and isinstance(value, (int, float)))


def _layout_relations(check: Mapping[str, Any]) -> str:
    report = check.get("report")
    relations = report.get("layout_relations") if isinstance(report, Mapping) else None
    return json.dumps(relations or [], ensure_ascii=True, sort_keys=True, default=str)


def _proposal_advances(
    incumbent: Mapping[str, Any], proposal: Mapping[str, Any], *, require_blocker_repair: bool = False,
) -> bool:
    if require_blocker_repair and not _blocking_issues(incumbent):
        return False
    if proposal.get("status") == "ok":
        return True
    if proposal.get("static_valid") is True and not _blocking_issues(proposal):
        return True
    if incumbent.get("status") == "ok":
        return False
    before = _blocking_issues(incumbent)
    after = _blocking_issues(proposal)
    if not before or not after or after.keys() - before.keys():
        return False
    improved = after.keys() < before.keys()
    for key in after:
        old, new = _blocker_magnitudes(before[key]), _blocker_magnitudes(after[key])
        if not old or len(old) != len(new):
            continue
        if any(b > a for a, b in zip(old, new, strict=True)):
            return False
        improved |= any(b < a for a, b in zip(old, new, strict=True))
    return improved or (
        not require_blocker_repair and _layout_relations(incumbent) != _layout_relations(proposal)
    )


def _render_digest(inspection: Mapping[str, Any] | None) -> bytes | None:
    if not isinstance(inspection, Mapping):
        return None
    path = Path(str(inspection.get("screenshot_path") or ""))
    try:
        return hashlib.sha256(path.read_bytes()).digest() if path.is_file() else None
    except OSError:
        return None


def inspection_unavailable(check: Mapping[str, Any]) -> bool:
    """Distinguish unavailable rendering from a measurable layout defect."""

    if check.get("static_valid") is not True or _blocking_issues(check):
        return False
    outcome = check.get("outcome")
    code = outcome.get("code") if isinstance(outcome, Mapping) else ""
    return check.get("status") != "ok" and code in _UNAVAILABLE_INSPECTION_OUTCOMES
