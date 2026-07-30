"""Portable deterministic action service for the scientific-poster Skill."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import poster_core

from posterlib.paths import SKILL_ROOT

SKILL = "scientific-poster"
HOST_ACTIONS = {"draft", "revise"}
HOST_METADATA_FIELDS = frozenset(
    {"channel", "file_uri", "project", "run_id", "tenant_id", "user_id"}
)


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one deterministic action without pretending to be an authoring model."""

    if payload.get("self_test"):
        return {"status": "ok", "skill": SKILL, "portable_runner": True}
    payload = {
        key: value for key, value in payload.items() if key not in HOST_METADATA_FIELDS
    }
    try:
        action = poster_core.normalize_action(payload.get("action"))
    except ValueError as exc:
        return _error("invalid_action", str(exc))
    if action in HOST_ACTIONS:
        return _error(
            "host_agent_required",
            "draft and revise require a host model that can author complete HTML/CSS.",
        )
    if action == "estimate":
        return _estimate(payload)
    if action == "validate":
        return _validate(payload)
    if action == "preview":
        return _preview(payload)
    if action == "inspect":
        return _inspect(payload)
    if action == "prepare-visual-review":
        return _prepare_visual_review(payload)
    if action == "submit-visual-review":
        return _submit_visual_review(payload)
    if action == "export-pptx":
        return _export_pptx(payload)
    if action == "approve":
        return _approve(payload)
    return _error("invalid_action", f"Unsupported portable action: {action}")


def _estimate(payload: dict[str, Any]) -> dict[str, Any]:
    """Estimate a page deterministically from a caller-supplied grounded budget."""

    allowed = {
        "action",
        "content_budget",
        "orientation",
        "page",
        "source",
        "source_text",
        "source_figure_sha256s",
    }
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        return _error(
            "candidate_validation_failed",
            "estimate received unexpected field(s): " + ", ".join(unexpected),
        )
    if "content_budget" not in payload:
        return _error(
            "host_agent_required",
            "Source-only estimate requires a host model to select grounded poster evidence.",
        )
    source_text = payload.get("source_text")
    if not isinstance(source_text, str) or not source_text.strip():
        source_path = str(payload.get("source") or "").strip()
        if not source_path:
            return _error(
                "missing_input",
                "Portable estimate requires source_text or a local UTF-8 source file.",
            )
        try:
            source_text = Path(source_path).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return _error("source_read_failed", str(exc))
    raw_hashes = payload.get("source_figure_sha256s") or []
    if not isinstance(raw_hashes, list) or not all(
        isinstance(item, str) for item in raw_hashes
    ):
        return _error(
            "invalid_content_budget",
            "source_figure_sha256s must be a string array.",
        )
    try:
        from posterlib.content.planning import (
            PlanningError,
            estimate_page,
            normalize_content_budget,
        )

        budget = normalize_content_budget(
            payload.get("content_budget"),
            source_text=source_text,
            source_figure_sha256s=set(raw_hashes),
        )
        page_plan = estimate_page(
            budget,
            page=payload.get("page"),
            orientation=str(payload.get("orientation") or "auto"),
        )
    except PlanningError as exc:
        code = (
            exc.code
            if exc.code in poster_core.OUTCOME_CONTRACTS
            else "invalid_content_budget"
        )
        return _error(code, str(exc))
    return {
        **_outcome(
            "estimate_complete",
            "Grounded content budget and physical page recommendation are ready.",
        ),
        "skill": SKILL,
        "content_budget": budget,
        "page_plan": page_plan.to_dict(),
        "expected_source_figure_sha256s": sorted(set(raw_hashes)),
        "paper_source": {"kind": "text", "character_count": len(source_text)},
        "warnings": [],
    }


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    checked = _validated_html(
        payload, allowed={"action", "html", "source", "source_text"}
    )
    if isinstance(checked, dict) and checked.get("status") == "error":
        return checked
    path, source_bytes, report = checked
    digest = hashlib.sha256(source_bytes).hexdigest()
    valid = report.get("status") == "ok"
    return {
        **_outcome(
            "poster_valid" if valid else "poster_invalid",
            "Poster HTML satisfies the static contract."
            if valid
            else "Poster HTML needs revision before preview or approval.",
        ),
        "html": str(path),
        "html_sha256": digest,
        "source_html_uri": f"portable://sha256/{digest}",
        "validation": report,
        "skill": SKILL,
    }


def _prepare_visual_review(payload: dict[str, Any]) -> dict[str, Any]:
    """Materialize a provider-neutral VLM request for exact rendered bytes."""

    allowed = {
        "action",
        "html",
        "screenshot",
        "reference",
        "content_brief",
        "visual_evidence",
        "iteration",
        "output_dir",
    }
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        return _error(
            "visual_review_invalid",
            "prepare-visual-review received unexpected field(s): "
            + ", ".join(unexpected),
        )
    try:
        from posterlib.runtime import runtime_io
        from posterlib.visual import visual_review

        request = visual_review.build_request(
            html_path=str(payload.get("html") or ""),
            screenshot_path=str(payload.get("screenshot") or ""),
            reference=payload.get("reference"),
            content_brief=payload.get("content_brief")
            if isinstance(payload.get("content_brief"), dict)
            else {},
            visual_evidence=payload.get("visual_evidence")
            if isinstance(payload.get("visual_evidence"), dict)
            else {},
            iteration=payload.get("iteration", 0),
        )
        output_dir = (
            Path(payload.get("output_dir") or Path.cwd() / "poster-visual-review")
            .expanduser()
            .resolve()
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        request_path = output_dir / "visual-review-request.json"
        runtime_io.write_json_atomic(
            request_path,
            request,
            indent=2,
            sort_keys=True,
        )
    except (OSError, visual_review.VisualReviewError) as exc:
        code = str(getattr(exc, "code", "visual_review_invalid"))
        return _error(code, str(exc))
    return {
        **_outcome(
            "visual_review_unavailable",
            "The candidate is rendered, but an image-capable reviewer must inspect it.",
        ),
        "skill": SKILL,
        "visual_review_request_path": str(request_path),
        "visual_review_request": request,
        "artifacts": [
            {
                "title": "Poster visual review request",
                "format": "json",
                "path": str(request_path),
                "mime": "application/json",
            }
        ],
    }


def _submit_visual_review(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a harness-supplied VLM result and persist its exact receipt."""

    allowed = {
        "action",
        "visual_review_request",
        "visual_review_result",
        "output_dir",
    }
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        return _error(
            "visual_review_invalid",
            "submit-visual-review received unexpected field(s): "
            + ", ".join(unexpected),
        )
    try:
        from posterlib.runtime import runtime_io
        from posterlib.visual import visual_review

        request_value = payload.get("visual_review_request")
        request = (
            visual_review.load_request(request_value)
            if isinstance(request_value, (str, Path))
            else dict(request_value)
            if isinstance(request_value, dict)
            else None
        )
        if request is None:
            raise visual_review.VisualReviewError(
                "visual_review_invalid",
                "visual_review_request must be a request object or JSON path",
            )
        result_value = payload.get("visual_review_result")
        if isinstance(result_value, (str, Path)):
            try:
                loaded_result = json.loads(
                    Path(result_value).expanduser().read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise visual_review.VisualReviewError(
                    "visual_review_invalid", f"cannot read visual review result: {exc}"
                ) from exc
        else:
            loaded_result = result_value
        if not isinstance(loaded_result, dict):
            raise visual_review.VisualReviewError(
                "visual_review_invalid",
                "visual_review_result must be a result object or JSON path",
            )
        receipt = visual_review.validate_result(request, loaded_result)
        output_dir = (
            Path(payload.get("output_dir") or Path.cwd() / "poster-visual-review")
            .expanduser()
            .resolve()
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = output_dir / "visual-review.json"
        runtime_io.write_json_atomic(
            receipt_path,
            receipt,
            indent=2,
            sort_keys=True,
        )
    except (OSError, visual_review.VisualReviewError) as exc:
        code = str(getattr(exc, "code", "visual_review_invalid"))
        return _error(code, str(exc))

    quality_state = str(receipt["quality_state"])
    if quality_state == "passed":
        code = "visual_review_passed"
        summary = "The image-capable visual review passed for these exact poster bytes."
    elif quality_state == "revision-required":
        code = "visual_revision_required"
        operations = sorted(
            {str(issue["operation"]) for issue in receipt["critical_issues"]}
        )
        summary = (
            "The image-capable reviewer requested a grounded content replan."
            if "content-replan" in operations
            else "The image-capable reviewer requested a complete poster revision."
        )
    else:
        code = "visual_review_failed"
        summary = "The poster did not pass before the visual revision limit."
    result = {
        **_outcome(code, summary),
        "skill": SKILL,
        "visual_review_path": str(receipt_path),
        "visual_review": receipt,
        "artifacts": [
            {
                "title": "Poster visual review",
                "format": "json",
                "path": str(receipt_path),
                "mime": "application/json",
            }
        ],
    }
    if quality_state == "revision-required":
        result["revision_feedback"] = visual_review.revision_feedback(receipt)
    return result


def _preview(payload: dict[str, Any]) -> dict[str, Any]:
    checked = _validated_html(
        payload, allowed={"action", "html", "source", "source_text"}
    )
    if isinstance(checked, dict) and checked.get("status") == "error":
        return checked
    path, source_bytes, report = checked
    if report.get("status") != "ok":
        return {
            **_error(
                "source_html_invalid",
                "Poster HTML must pass validation before preview.",
            ),
            "validation": report,
        }
    if path.name != "poster.html":
        return _error(
            "poster_filename_required",
            "Live preview requires a file named poster.html.",
        )
    digest = hashlib.sha256(source_bytes).hexdigest()
    return {
        **_outcome("preview_ready", "Poster live-preview metadata is ready."),
        "skill": SKILL,
        "html": str(path),
        "html_sha256": digest,
        "source_html_uri": f"portable://sha256/{digest}",
        "preview_argv": poster_core.build_preview_argv(
            path,
            skill_dir=SKILL_ROOT,
            python_executable=sys.executable,
        ),
        "selection_state_path": str(path.parent / "selection-state.json"),
        "requires_approval": False,
        "warnings": ["Run Chromium inspection before requesting approval."],
    }


def _approve(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "action",
        "source_html_path",
        "source_html_sha256",
        "source_html_uri",
        "output_dir",
        "approved",
        "operator_confirmation",
        "session_id",
        "host_event_id",
        "paper_path",
        "source",
        "source_text",
        "source_figure_sha256s",
        "visual_review_path",
    }
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        return _error(
            "candidate_validation_failed",
            "approve received unexpected field(s): " + ", ".join(unexpected),
        )
    source_path = payload.get("source_html_path")
    if not isinstance(source_path, str) or not source_path.strip():
        return _error("approval_source_mismatch", "source_html_path is required.")
    grounding = _approval_grounding(payload)
    if isinstance(grounding, dict):
        return grounding
    grounding_source, source_figure_sha256s = grounding
    try:
        from posterlib.delivery.approval import ApprovalError, create_poster_approval

        bundle = create_poster_approval(
            source_html_path=source_path,
            source_html_sha256=str(payload.get("source_html_sha256") or ""),
            source_html_uri=(
                str(payload["source_html_uri"])
                if payload.get("source_html_uri") is not None
                else None
            ),
            output_dir=payload.get("output_dir") or Path.cwd() / "poster-approval",
            approved=payload.get("approved"),
            operator_confirmation=payload.get("operator_confirmation"),
            session_id=payload.get("session_id"),
            source_text=grounding_source,
            source_figure_sha256s=source_figure_sha256s,
            visual_review_path=payload.get("visual_review_path"),
            host_event_id=(
                str(payload["host_event_id"])
                if payload.get("host_event_id") is not None
                else None
            ),
        )
    except ApprovalError as exc:
        return {**_error(exc.code, str(exc)), **exc.details}
    except (OSError, ValueError) as exc:
        return _error("approval_receipt_untrusted", str(exc))
    return {
        **_outcome(
            "poster_approval_recorded", "Stored exact approved HTML and its receipt."
        ),
        "skill": SKILL,
        "approval_path": str(bundle.approval_path),
        "approved_html_path": str(bundle.html_path),
        "visual_review_path": str(bundle.visual_review_path),
        "source_html_sha256": bundle.source_html_sha256,
        "visual_review_sha256": bundle.receipt["visual_review_sha256"],
        "approval_sha256": bundle.approval_sha256,
        "bundle_sha256": bundle.bundle_sha256,
    }


def _approval_grounding(
    payload: dict[str, Any],
) -> tuple[str, object] | dict[str, Any]:
    """Resolve one explicit approval source without relying on engine memory."""

    paper_path = str(payload.get("paper_path") or "").strip()
    grounding_path = str(payload.get("source") or "").strip()
    grounding_text = payload.get("source_text")
    has_text = isinstance(grounding_text, str) and bool(grounding_text.strip())
    if sum((bool(paper_path), bool(grounding_path), has_text)) != 1:
        return _error(
            "missing_input",
            "approve requires exactly one of paper_path, source, or source_text.",
        )
    if paper_path:
        if payload.get("source_figure_sha256s"):
            return _error(
                "candidate_validation_failed",
                "source_figure_sha256s is derived from paper_path and must not be supplied.",
            )
        try:
            from posterlib.sources.paper_source import PaperSourceError, prepare_pdf

            with tempfile.TemporaryDirectory(
                prefix="scientific-poster-approval-source-"
            ) as directory:
                paper = prepare_pdf(paper_path, directory)
        except PaperSourceError as exc:
            if exc.code == "missing_capability":
                from posterlib.runtime.capability import missing_result

                return missing_result(
                    "pdf-reading",
                    dependency="pymupdf",
                    stage="approval-source",
                    error=exc,
                )
            return _error(exc.code, str(exc))
        return paper.text, [str(figure["sha256"]) for figure in paper.figures]
    if grounding_path:
        try:
            grounding_text = (
                Path(grounding_path).expanduser().read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError) as exc:
            return _error("source_read_failed", str(exc))
    assert isinstance(grounding_text, str)
    return grounding_text, payload.get("source_figure_sha256s") or ()


def _inspect(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "action",
        "html",
        "source",
        "source_text",
        "output_dir",
        "scale",
        "paper_path",
        "source_figure_sha256s",
    }
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        return _error(
            "candidate_validation_failed",
            "inspect received unexpected field(s): " + ", ".join(unexpected),
        )
    paper_path = str(payload.get("paper_path") or "").strip()
    if paper_path and payload.get("source_figure_sha256s"):
        return _error(
            "candidate_validation_failed",
            "source_figure_sha256s is derived from paper_path and must not be supplied.",
        )
    if paper_path and (payload.get("source") or payload.get("source_text")):
        return _error(
            "candidate_validation_failed",
            "paper_path cannot be combined with source or source_text during inspection.",
        )

    static_payload = {
        key: value
        for key, value in payload.items()
        if key in {"action", "html", "source", "source_text"}
    }
    expected_hashes: set[str] | None = None
    if paper_path:
        try:
            from posterlib.sources.paper_source import PaperSourceError, prepare_pdf

            with tempfile.TemporaryDirectory(
                prefix="scientific-poster-inspection-source-"
            ) as directory:
                paper = prepare_pdf(
                    Path(paper_path.removeprefix("file://")),
                    directory,
                )
        except PaperSourceError as exc:
            if exc.code == "missing_capability":
                from posterlib.runtime.capability import missing_result

                return missing_result(
                    "pdf-reading",
                    dependency="pymupdf",
                    stage="inspection-source",
                    error=exc,
                )
            return _error(exc.code, str(exc))
        static_payload["source_text"] = paper.text
        expected_hashes = {str(figure["sha256"]) for figure in paper.figures}
    elif "source_figure_sha256s" in payload:
        raw_hashes = payload.get("source_figure_sha256s")
        if not isinstance(raw_hashes, list) or not all(
            isinstance(item, str) for item in raw_hashes
        ):
            return _error(
                "candidate_validation_failed",
                "source_figure_sha256s must be a string array.",
            )
        try:
            poster_core.source_figure_manifest_sha256(raw_hashes)
        except ValueError as exc:
            return _error("candidate_validation_failed", str(exc))
        expected_hashes = set(raw_hashes)

    checked = _validated_html(
        static_payload,
        allowed={"action", "html", "source", "source_text"},
    )
    if isinstance(checked, dict) and checked.get("status") == "error":
        return checked
    path, source_bytes, report = checked
    if report.get("status") != "ok":
        return {
            **_error("source_html_invalid", "Poster HTML must pass static validation."),
            "validation": report,
        }
    output = Path(
        str(payload.get("output_dir") or path.parent / "poster-inspection")
    ).expanduser()
    command = [
        sys.executable,
        "-m",
        "posterlib.runtime.browser_inspection",
        "--html",
        str(path),
        "--out",
        str(output),
    ]
    if payload.get("scale") is not None:
        command.extend(["--scale", str(payload["scale"])])
    if expected_hashes is not None:
        command.append("--source-figure-manifest-known")
        for digest in sorted(expected_hashes):
            command.extend(["--expected-source-figure-sha256", digest])
    result = _run_json_process(
        command,
        timeout=180,
        code="inspection_unavailable",
        cwd=SKILL_ROOT,
    )
    result["skill"] = SKILL
    result["source_html_sha256"] = hashlib.sha256(source_bytes).hexdigest()
    return result


def _export_pptx(payload: dict[str, Any]) -> dict[str, Any]:
    """Export validated poster HTML as a one-slide native PowerPoint deck."""

    allowed = {"action", "html", "source", "source_text", "output_dir"}
    checked = _validated_html(payload, allowed=allowed)
    if isinstance(checked, dict) and checked.get("status") == "error":
        return checked
    path, source_bytes, report = checked
    if report.get("status") != "ok":
        return {
            **_error(
                "source_html_invalid",
                "Poster HTML must pass static validation before PPTX export.",
            ),
            "validation": report,
        }
    output = (
        Path(str(payload.get("output_dir") or path.parent / "editable-poster"))
        .expanduser()
        .resolve()
    )
    try:
        from posterlib.delivery.pptx_export import ExportError, export_editable_pptx

        bundle = export_editable_pptx(path, output)
    except ExportError as exc:
        code = (
            exc.code
            if exc.code in poster_core.OUTCOME_CONTRACTS
            else "pptx_export_failed"
        )
        return {**_error(code, str(exc)), **exc.details}
    return {
        **_outcome(
            "pptx_export_complete",
            "Exported one-slide native PowerPoint poster with object-bound rubric results.",
        ),
        "skill": SKILL,
        "source_html_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "pptx_path": bundle["pptx_path"],
        "scene_path": bundle["scene_path"],
        "rubric_path": bundle["rubric_path"],
        "rubric": bundle["rubric"],
        "openxml": bundle.get("openxml", {}),
        "editable_object_count": len(bundle["scene"]["objects"]),
    }


def _validated_html(
    payload: dict[str, Any],
    *,
    allowed: set[str],
) -> tuple[Path, bytes, dict[str, Any]] | dict[str, Any]:
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        return _error(
            "candidate_validation_failed",
            "unexpected field(s): " + ", ".join(unexpected),
        )
    raw_path = str(payload.get("html") or "").strip()
    if not raw_path:
        return _error("missing_html", "html is required.")
    path = Path(raw_path).expanduser().resolve()
    try:
        source_bytes = path.read_bytes()
        html_text = source_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return _error("source_read_failed", str(exc))
    source_path = str(payload.get("source") or "").strip()
    if source_path:
        try:
            source_text = Path(source_path).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return _error("source_read_failed", str(exc))
    else:
        source_text = payload.get("source_text")
        if not isinstance(source_text, str):
            source_text = ""
    report = poster_core.validate_poster_html(html_text, source_text=source_text)
    return path, source_bytes, report


def _run_json_process(
    command: list[str],
    *,
    timeout: int,
    code: str,
    cwd: Path | None = None,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
        )
        value = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return _error(code, str(exc))
    if not isinstance(value, dict):
        return _error(code, "Child process returned a non-object result.")
    value = poster_core.normalize_outcome_result(
        value,
        fallback_code=code,
        fallback_summary="Child process failed to return a registered outcome.",
    )
    if completed.stderr:
        value.setdefault("stderr", completed.stderr)
    return value


def _outcome(code: str, summary: str, **details: Any) -> dict[str, Any]:
    return poster_core.outcome_result(code, summary=summary, **details)


def _error(code: str, message: str) -> dict[str, Any]:
    result = poster_core.outcome_result(code, summary=message, error=message)
    result["skill"] = SKILL
    return result
