"""Bounded automatic VLM review for one rendered poster candidate."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import poster_core

from posterlib.runtime import runtime_io
from posterlib.visual import candidate_control, inspection_policy, visual_review

from . import revision_state, runtime_budget, workflow_outcomes

_VLM_TIMEOUT_SECONDS = 60.0
_VISUAL_CONTINUATION_SCHEMA = "scientific-poster.visual-continuation.v1"

ReviewState = Literal["pending", "passed", "revision-required", "failed"]
ReviewMode = Literal["vlm", "deterministic-only"]
Reviewer = Callable[..., Awaitable[dict[str, Any]]]
RevisionCallback = Callable[..., Awaitable[Any]]
CommitCallback = Callable[[Any], Awaitable[dict[str, Any]]]
PreparedInspectionCallback = Callable[[Any], dict[str, Any] | None]
PreparedReviewCallback = Callable[
    [Any],
    tuple[dict[str, Any], str] | None,
]

_MISSING_CONFIG_WARNING = (
    "Omni VLM visual review is not configured; the rendered poster is pending an "
    "image-capable review."
)
_PROVIDER_WARNING = (
    "Omni VLM visual review was unavailable after bounded attempts; the rendered poster "
    "remains pending review."
)
_REQUEST_ARTIFACT_WARNING = (
    "The bound visual-review request artifact is unavailable; the rendered poster "
    "remains pending review."
)
RENDERED_REVISION_REJECTED_WARNING = (
    "An automatic revision was rejected because its bound visual or delivery evidence "
    "regressed the active candidate; the best published version was preserved."
)
NOOP_REVISION_WARNING = "The automatic revision returned identical HTML; the active candidate was preserved."
INVALID_REVISION_WARNING = (
    "The automatic revision did not satisfy the grounded HTML contract; the active "
    "candidate and bound review checkpoint were preserved."
)


@dataclass(frozen=True)
class VisualLoopRuntime:
    """Injected provider objects for one offline-testable visual review."""

    client: Any


@dataclass(frozen=True)
class VisualReviewOutcome:
    """One persisted review result or an explicit pending state."""

    state: ReviewState
    review_mode: ReviewMode
    receipt_path: str | None = None
    receipt: dict[str, Any] | None = None
    warning: str = ""


@dataclass(frozen=True)
class VisualLoopCallbacks:
    """Engine-owned revision operations needed by the portable visual loop."""

    revise: RevisionCallback
    commit: CommitCallback
    prepared_inspection: PreparedInspectionCallback
    prepared_review: PreparedReviewCallback
    defer_revision_commit: object


def runtime_from_env(
    environ: Mapping[str, str] | None = None,
) -> VisualLoopRuntime | None:
    """Return a VLM runtime only for complete explicit environment config."""

    try:
        from posterlib.visual import vlm_client
    except ImportError:
        return None
    try:
        config = vlm_client.config_from_env(
            environ,
            timeout_s=_VLM_TIMEOUT_SECONDS,
        )
    except (ValueError, vlm_client.VlmError):
        return None
    if config is None:
        return None
    client = vlm_client.VlmClient(config)
    return VisualLoopRuntime(client=client)


def runtime_from_context(
    ctx: Any,
    environ: Mapping[str, str] | None = None,
) -> VisualLoopRuntime | None:
    """Prefer a usable host VLM, then use complete environment configuration."""

    try:
        from posterlib.visual import vlm_client
    except ImportError:
        return runtime_from_env(environ)
    client = vlm_client.client_from_context(ctx)
    if client is not None:
        return VisualLoopRuntime(client=client)
    return runtime_from_env(environ)


def _review_configuration_present(
    ctx: Any,
    environ: Mapping[str, str] | None,
    runtime: VisualLoopRuntime | None,
) -> bool:
    """Keep an unavailable configured reviewer distinct from no configuration."""

    if runtime is not None:
        return True
    try:
        from posterlib.visual import vlm_client
    except ImportError:
        return False
    return vlm_client.configuration_present(ctx, environ)


async def review_and_persist(
    request: Mapping[str, Any],
    *,
    output_dir: str | Path,
    ctx: Any = None,
    environ: Mapping[str, str] | None = None,
    runtime: VisualLoopRuntime | None = None,
    reviewer: Reviewer | None = None,
) -> VisualReviewOutcome:
    """Review one bound request, atomically persist it, and reuse valid receipts."""

    bound = visual_review.validate_request(request)
    destination = Path(output_dir).expanduser().resolve()
    receipt_path = destination / "visual-review.json"
    result_path = destination / "model-result.json"

    reused = _reusable_receipt(receipt_path, bound)
    if reused is not None:
        return VisualReviewOutcome(
            state=_outcome_state(reused),
            review_mode="vlm",
            receipt_path=str(receipt_path),
            receipt=reused,
        )

    active_runtime = runtime or runtime_from_context(ctx, environ)
    review_configured = _review_configuration_present(
        ctx,
        environ,
        active_runtime,
    )
    if active_runtime is None:
        return VisualReviewOutcome(
            state="pending",
            review_mode="vlm" if review_configured else "deterministic-only",
            warning=(
                _PROVIDER_WARNING if review_configured else _MISSING_CONFIG_WARNING
            ),
        )

    try:
        if reviewer is None:
            from posterlib.visual.vlm_review import review_request

            reviewer = review_request
        async with asyncio.timeout(_VLM_TIMEOUT_SECONDS):
            result = await reviewer(
                bound,
                client=active_runtime.client,
            )
        receipt = visual_review.validate_result(bound, result)
        runtime_io.write_json_atomic(
            result_path,
            dict(result),
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        runtime_io.write_json_atomic(
            receipt_path,
            dict(receipt),
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        visual_review.VisualReviewError,
    ) as exc:
        return VisualReviewOutcome(
            state="pending",
            review_mode="vlm",
            warning=f"{_PROVIDER_WARNING} ({type(exc).__name__}: {exc})",
        )

    return VisualReviewOutcome(
        state=_outcome_state(receipt),
        review_mode="vlm",
        receipt_path=str(receipt_path),
        receipt=receipt,
    )


async def complete(
    initial: dict[str, Any],
    *,
    input_data: dict[str, Any],
    progress_callback: Any,
    callbacks: VisualLoopCallbacks,
    ctx: Any = None,
    deadline: float | None = None,
    environ: Mapping[str, str] | None = None,
    runtime: VisualLoopRuntime | None = None,
    reviewer: Reviewer | None = None,
) -> dict[str, Any]:
    """Review and revise published candidates without recursive engine calls."""

    result = initial
    loop = asyncio.get_running_loop()
    if deadline is None:
        deadline = runtime_budget.visual_loop_deadline(ctx, loop.time())
    active_runtime = runtime or runtime_from_context(ctx, environ)
    review_configured = _review_configuration_present(
        ctx,
        environ,
        active_runtime,
    )
    regression_retry_used = False
    while True:
        pending_operations: list[str] | None = None
        regressed_inspection_feedback: list[str] = []
        try:
            resumed = revision_state.resume_pending_visual_revision(result)
        except visual_review.VisualReviewError as exc:
            return workflow_outcomes.error_result(exc.code, str(exc))
        request_value = result.get("visual_review_request")
        if (
            resumed is None
            and _inspection_is_blocked(result)
            and not isinstance(request_value, dict)
        ):
            result, improved = await _repair_blocked_inspection(
                result,
                input_data=input_data,
                progress_callback=progress_callback,
                deadline=(
                    runtime_budget.automatic_revision_deadline(deadline)
                    if active_runtime is not None
                    else deadline
                ),
                callbacks=callbacks,
            )
            if improved:
                continue
            return result
        if resumed is None and not isinstance(request_value, dict):
            break
        remaining = deadline - loop.time()
        if remaining <= 0:
            if resumed is not None:
                return workflow_outcomes.revision_timeout_error(
                    result,
                    {"error": "Visual revision timed out before model execution."},
                )
            _mark_pending_review_mode(result, review_configured)
            return workflow_outcomes.visual_loop_pending(result)
        if resumed is not None:
            pending, receipt = resumed
            pending_operations = list(pending["operations"])
            regressed_inspection_feedback = (
                revision_state.pending_visual_regression_feedback(pending)
            )
            review = VisualReviewOutcome(
                state="revision-required",
                review_mode="vlm",
                receipt_path=str(pending["visual_review_path"]),
                receipt=receipt,
            )
        else:
            assert isinstance(request_value, dict)
            request = dict(request_value)
            request_path = Path(str(result.get("visual_review_request_path") or ""))
            if not request_path.is_file():
                _mark_pending_review_mode(result, review_configured)
                return workflow_outcomes.visual_review_unavailable(
                    result,
                    _REQUEST_ARTIFACT_WARNING,
                )
            await runtime_io.progress(
                progress_callback,
                "poster.visual-review",
                0.82,
                iteration=request.get("iteration"),
            )
            review_kwargs: dict[str, Any] = {
                "output_dir": request_path.parent,
                "ctx": ctx,
                "environ": environ,
            }
            if active_runtime is not None:
                review_kwargs["runtime"] = active_runtime
            if reviewer is not None:
                review_kwargs["reviewer"] = reviewer
            remaining = deadline - loop.time()
            if remaining <= 0:
                _mark_pending_review_mode(result, review_configured)
                return workflow_outcomes.visual_loop_pending(result)
            try:
                async with asyncio.timeout(remaining):
                    review = await review_and_persist(request, **review_kwargs)
            except TimeoutError:
                _mark_pending_review_mode(result, review_configured)
                return workflow_outcomes.visual_loop_pending(result)
        if review.warning:
            workflow_outcomes.append_warning_once(result, review.warning)
        result["visual_review_mode"] = review.review_mode
        if review.state == "pending" or review.receipt is None:
            if _inspection_is_blocked(result):
                result, improved = await _repair_blocked_inspection(
                    result,
                    input_data=input_data,
                    progress_callback=progress_callback,
                    deadline=(
                        runtime_budget.automatic_revision_deadline(deadline)
                        if active_runtime is not None
                        else deadline
                    ),
                    callbacks=callbacks,
                )
                if improved:
                    continue
                return result
            return workflow_outcomes.visual_review_unavailable(result, review.warning)

        result["visual_review_path"] = str(review.receipt_path or "")
        result["visual_review"] = review.receipt
        result["visual_quality_state"] = review.state
        inspection = result.get("inspection")
        inspection_passed = (
            isinstance(inspection, dict) and inspection.get("status") == "ok"
        )
        feedback_items = result.get("inspection_feedback")
        inspection_feedback = (
            [str(item).strip() for item in feedback_items if str(item).strip()]
            if isinstance(feedback_items, list)
            else []
        )
        if review.state == "passed" and inspection_passed:
            revision_state.clear_pending_visual_revision(result)
            return workflow_outcomes.visual_outcome(
                result,
                "visual_review_passed",
                "The rendered inspection and reference-aware image review passed for "
                "these exact poster bytes.",
            )
        if review.state == "failed":
            return _visual_revision_exhausted(
                result,
                "The poster did not pass before the bounded visual revision limit.",
            )

        next_iteration = int(review.receipt["iteration"]) + 1
        if next_iteration > int(review.receipt["max_iterations"]):
            return _visual_revision_exhausted(
                result,
                "The visual reviewer passed, but the rendered inspection still has "
                "blocking geometry after the bounded revision limit.",
            )
        deterministic_revision = review.state == "passed"
        operations = (
            pending_operations
            if pending_operations is not None
            else sorted(
                {str(issue["operation"]) for issue in review.receipt["critical_issues"]}
            )
        )
        revision_mode = (
            "full-layout"
            if deterministic_revision
            else revision_state.vlm_revision_mode(operations)
        )
        if regressed_inspection_feedback and revision_mode == "style-only":
            revision_mode = "full-layout"
        if review.state == "revision-required" and resumed is None:
            try:
                revision_state.persist_pending_visual_revision(
                    result,
                    receipt_path=str(review.receipt_path or ""),
                )
            except visual_review.VisualReviewError as exc:
                return workflow_outcomes.error_result(exc.code, str(exc))
        await runtime_io.progress(
            progress_callback,
            "poster.visual-revise",
            0.86,
            iteration=next_iteration,
            revision_mode=revision_mode,
            operations=operations,
        )
        if loop.time() >= deadline:
            return workflow_outcomes.revision_timeout_error(
                result,
                {"error": "Visual revision timed out before model execution."},
            )
        revision_input = {
            **input_data,
            "action": poster_core.ACTION_REVISE,
            "source_html_uri": str(result["html_uri"]),
            "source_html_sha256": str(result["html_sha256"]),
            "feedback": "\n".join(
                [
                    *inspection_feedback,
                    *(
                        [
                            "The previous uncommitted revision was rejected by rendered "
                            "inspection. Repair its measured geometry failures without "
                            "repeating that composition or weakening the accepted VLM "
                            "strengths:",
                            *regressed_inspection_feedback,
                        ]
                        if regressed_inspection_feedback
                        else []
                    ),
                ]
            ),
            "visual_iteration": next_iteration,
            "inspection_repair_attempt": (
                revision_state.inspection_repair_attempt(result) + 1
                if not inspection_passed
                else revision_state.inspection_repair_attempt(result)
            ),
        }
        revision_input["revision_mode"] = revision_mode
        if deterministic_revision:
            revision_input["feedback"] = revision_input["feedback"] or (
                "Resolve every blocking rendered-inspection issue without removing "
                "grounded content."
            )
        else:
            revision_input["visual_review_path"] = str(review.receipt_path)
        revision_deadline = runtime_budget.automatic_revision_deadline(deadline)
        revision_remaining = revision_deadline - loop.time()
        if not runtime_budget.bound_automatic_revision(
            revision_input,
            remaining_seconds=revision_remaining,
        ):
            return workflow_outcomes.revision_timeout_error(
                result,
                {
                    "error": (
                        "The remaining workflow time is too short for a useful "
                        "model revision; the bound review checkpoint was preserved."
                    )
                },
            )
        revision_input.pop("selection_state", None)
        revision_input["_defer_revision_commit"] = callbacks.defer_revision_commit
        best = result
        if regressed_inspection_feedback:
            regression_retry_used = True
        remaining = revision_deadline - loop.time()
        if remaining <= 0:
            return workflow_outcomes.revision_timeout_error(
                best,
                {"error": "Visual revision timed out before model execution."},
            )
        try:
            async with asyncio.timeout(remaining):
                candidate = await callbacks.revise(
                    revision_input,
                    progress_callback,
                    deadline=revision_deadline,
                )
        except TimeoutError:
            return workflow_outcomes.revision_timeout_error(
                best,
                {"error": "Visual revision timed out during model execution."},
            )
        candidate_outcome = (
            candidate.get("outcome") if isinstance(candidate, dict) else None
        )
        if (
            isinstance(candidate_outcome, Mapping)
            and candidate_outcome.get("code") == "candidate_validation_failed"
        ):
            workflow_outcomes.append_warning_once(best, INVALID_REVISION_WARNING)
            validation_detail = str(
                candidate.get("error") or candidate.get("summary") or ""
            ).strip()
            if validation_detail:
                workflow_outcomes.append_warning_once(best, validation_detail)
            best["visual_quality_state"] = "revision-required"
            return workflow_outcomes.visual_outcome(
                best,
                "visual_revision_required",
                "The automatic revision was invalid; the exact reviewed candidate "
                "and its retry checkpoint were preserved.",
                decision_reason="automatic-revision-invalid",
            )
        candidate_inspection = callbacks.prepared_inspection(candidate)
        if candidate_inspection is not None:
            candidate_review = await _review_prepared_candidate(
                candidate,
                callbacks=callbacks,
                ctx=ctx,
                environ=environ,
                runtime=active_runtime,
                reviewer=reviewer,
                deadline=deadline,
            )
            rendered_noop = _rendered_revision_noop(best, candidate_review)
            if not rendered_noop and _accept_prepared_candidate(
                best,
                candidate,
                candidate_inspection=candidate_inspection,
                candidate_review=candidate_review,
                review_configured=review_configured,
            ):
                result = await callbacks.commit(candidate)
                revision_state.clear_pending_visual_revision(best)
                if (
                    candidate_review is not None
                    and candidate_review.receipt is not None
                ):
                    result["visual_review"] = candidate_review.receipt
                    result["visual_review_path"] = str(
                        candidate_review.receipt_path or ""
                    )
                    result["visual_quality_state"] = candidate_review.state
                    result["visual_review_mode"] = candidate_review.review_mode
                    if (
                        candidate_review.state == "passed"
                        and candidate_inspection.get("status") == "ok"
                    ):
                        return workflow_outcomes.visual_outcome(
                            result,
                            "visual_review_passed",
                            "The exact pre-commit candidate passed rendered inspection "
                            "and evidence-bound image review.",
                        )
            else:
                result = best
                regressed_feedback = inspection_policy.inspection_feedback(
                    candidate_inspection
                )
                if rendered_noop:
                    regressed_feedback.append(
                        "The prepared revision rendered pixel-identically to the "
                        "candidate that the bound reviewer required revising. Make a "
                        "materially different whole-page layout or spacing change; do "
                        "not seek a new verdict for unchanged pixels."
                    )
                if (
                    candidate_review is not None
                    and candidate_review.receipt is not None
                ):
                    if candidate_review.state == "failed":
                        continuation = _staged_visual_continuation(
                            candidate,
                            candidate_review=candidate_review,
                            callbacks=callbacks,
                        )
                        if continuation is not None:
                            result["visual_continuation"] = continuation
                        failed_summary = str(
                            candidate_review.receipt.get("summary") or ""
                        ).strip()
                        if failed_summary:
                            workflow_outcomes.append_warning_once(
                                result,
                                "The final staged candidate was not accepted: "
                                + failed_summary,
                            )
                        workflow_outcomes.append_warning_once(
                            result, RENDERED_REVISION_REJECTED_WARNING
                        )
                        return _visual_revision_exhausted(
                            result,
                            "The bounded visual revision limit was reached; the best "
                            "published candidate was preserved for a later decision.",
                        )
                    if candidate_review.receipt.get("verdict") == "revise":
                        visual_feedback = visual_review.revision_feedback(
                            candidate_review.receipt
                        ).strip()
                        if visual_feedback:
                            regressed_feedback.append(
                                "The uncommitted candidate regressed the visual "
                                "composition: " + visual_feedback
                            )
                if review.state == "revision-required" and regressed_feedback:
                    try:
                        revision_state.persist_pending_visual_regression(
                            result,
                            inspection_feedback=regressed_feedback,
                        )
                    except visual_review.VisualReviewError as exc:
                        return workflow_outcomes.error_result(exc.code, str(exc))
                workflow_outcomes.append_warning_once(
                    result, RENDERED_REVISION_REJECTED_WARNING
                )
                result["visual_quality_state"] = "revision-required"
                result["retry_from_checkpoint"] = review.state == "revision-required"
                if (
                    review.state == "revision-required"
                    and regressed_feedback
                    and not regression_retry_used
                ):
                    regression_retry_used = True
                    continue
                return workflow_outcomes.visual_outcome(
                    result,
                    "visual_revision_required",
                    "Automatic visual revision regressed the bound composition or "
                    "delivery evidence; the best published candidate and repair "
                    "feedback were preserved.",
                    decision_reason="automatic-revision-regressed",
                )
        else:
            if workflow_outcomes.revision_model_timed_out(candidate):
                return workflow_outcomes.revision_timeout_error(best, candidate)
            outcome = candidate.get("outcome")
            if isinstance(outcome, Mapping) and (
                outcome.get("code") == "revision_noop"
                or candidate.get("revision_noop") is True
            ):
                workflow_outcomes.append_warning_once(best, NOOP_REVISION_WARNING)
                best["visual_quality_state"] = "revision-required"
                return workflow_outcomes.visual_outcome(
                    best,
                    "visual_revision_required",
                    "The visual reviewer requested changes, but the authoring model "
                    "returned identical HTML; the bound candidate was preserved.",
                    decision_reason="automatic-revision-noop",
                )
            result = candidate
        if result.get("status") == "error" and not _inspection_is_blocked(result):
            return result
        if loop.time() >= deadline:
            _mark_pending_review_mode(result, review_configured)
            return workflow_outcomes.visual_loop_pending(result)
    return result


def _visual_revision_exhausted(
    result: dict[str, Any],
    summary: str,
) -> dict[str, Any]:
    """Return a recoverable checkpoint instead of failing at the review limit."""

    result["visual_quality_state"] = "failed"
    result["retry_from_checkpoint"] = isinstance(
        result.get("visual_continuation"), Mapping
    )
    return workflow_outcomes.visual_outcome(
        result,
        "visual_revision_required",
        summary,
        decision_reason="automatic-revision-exhausted",
    )


def _staged_visual_continuation(
    candidate: Any,
    *,
    candidate_review: VisualReviewOutcome,
    callbacks: VisualLoopCallbacks,
) -> dict[str, Any] | None:
    """Bind a rejected staged candidate to a fresh bounded revision cycle."""

    if (
        candidate_review.state != "failed"
        or candidate_review.receipt is None
        or not candidate_review.receipt_path
    ):
        return None
    prepared = callbacks.prepared_review(candidate)
    if prepared is None:
        return None
    request, _request_path = prepared
    try:
        bound_request = visual_review.validate_request(request)
        candidate_path = Path(str(bound_request["candidate_html_path"]))
        candidate_sha256 = str(bound_request["candidate_html_sha256"])
        if visual_review.sha256_file(candidate_path) != candidate_sha256:
            return None
        source_html = candidate_path.read_text(encoding="utf-8")
        if (
            revision_state.revision_checkpoint_state(
                candidate_path,
                source_html=source_html,
                html_sha256=candidate_sha256,
            )
            is None
        ):
            return None
        receipt = visual_review.load_receipt(
            candidate_review.receipt_path,
            expected_html_sha256=candidate_sha256,
            expected_reference_image_sha256=str(
                bound_request["reference_image_sha256"]
            ),
        )
        if (
            receipt["receipt_sha256"] != candidate_review.receipt["receipt_sha256"]
            or receipt["request_sha256"] != bound_request["request_sha256"]
            or receipt["screenshot_sha256"] != bound_request["screenshot_sha256"]
            or receipt["visual_evidence_sha256"]
            != bound_request["visual_evidence_sha256"]
        ):
            return None
        feedback = visual_review.continuation_feedback(receipt)
    except (OSError, UnicodeError, visual_review.VisualReviewError):
        return None
    return {
        "schema": _VISUAL_CONTINUATION_SCHEMA,
        "action": poster_core.ACTION_REVISE,
        "source_html_uri": candidate_path.resolve().as_uri(),
        "source_html_sha256": candidate_sha256,
        "feedback": feedback,
        "exhausted_visual_review_path": str(candidate_review.receipt_path),
        "exhausted_visual_review_sha256": str(receipt["receipt_sha256"]),
        "reference_image_sha256": str(receipt["reference_image_sha256"]),
        "starts_new_bounded_cycle": True,
    }


async def _review_prepared_candidate(
    candidate: Any,
    *,
    callbacks: VisualLoopCallbacks,
    ctx: Any,
    environ: Mapping[str, str] | None,
    runtime: VisualLoopRuntime | None,
    reviewer: Reviewer | None,
    deadline: float,
) -> VisualReviewOutcome | None:
    """Review exact deferred bytes before they can replace the active poster."""

    prepared = callbacks.prepared_review(candidate)
    if prepared is None:
        return None
    request, request_path_text = prepared
    request_path = Path(request_path_text)
    if not request_path.is_file():
        return None
    kwargs: dict[str, Any] = {
        "output_dir": request_path.parent,
        "ctx": ctx,
        "environ": environ,
    }
    if runtime is not None:
        kwargs["runtime"] = runtime
    if reviewer is not None:
        kwargs["reviewer"] = reviewer
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return None
    try:
        async with asyncio.timeout(remaining):
            return await review_and_persist(request, **kwargs)
    except TimeoutError:
        return None


def _accept_prepared_candidate(
    incumbent: dict[str, Any],
    candidate: Any,
    *,
    candidate_inspection: dict[str, Any],
    candidate_review: VisualReviewOutcome | None,
    review_configured: bool,
) -> bool:
    """Accept only a physical or perceptual Pareto improvement."""

    if candidate_review is not None and candidate_review.state == "failed":
        return False
    candidate_receipt = (
        candidate_review.receipt if candidate_review is not None else None
    )
    candidate_evidence = candidate_control.CandidateEvidence(
        html_sha256=str(getattr(candidate, "html_sha256", "") or ""),
        inspection=candidate_inspection,
        review_receipt=candidate_receipt,
    )
    incumbent_evidence = candidate_control.CandidateEvidence(
        html_sha256=str(incumbent.get("html_sha256") or ""),
        inspection=(
            incumbent.get("inspection")
            if isinstance(incumbent.get("inspection"), Mapping)
            else {}
        ),
        review_receipt=(
            incumbent.get("visual_review")
            if isinstance(incumbent.get("visual_review"), Mapping)
            else None
        ),
    )
    delivery = candidate_control.delivery_relation(
        incumbent_evidence, candidate_evidence
    )
    if candidate_review is None or candidate_review.receipt is None:
        return not review_configured and delivery == "dominates"
    if candidate_control.ready_for_delivery(candidate_evidence):
        return True
    state = candidate_control.ControllerState(
        composition_anchor=(
            incumbent_evidence
            if incumbent_evidence.review_receipt is not None
            else None
        ),
        delivery_candidate=incumbent_evidence,
    )
    updated = candidate_control.observe(state, candidate_evidence)
    return (
        updated.composition_anchor is candidate_evidence
        and updated.delivery_candidate is candidate_evidence
    )


def _rendered_revision_noop(
    incumbent: Mapping[str, Any],
    candidate_review: VisualReviewOutcome | None,
) -> bool:
    """Reject a pass flip when a requested visual revision changed no pixels."""

    previous = incumbent.get("visual_review")
    current = candidate_review.receipt if candidate_review is not None else None
    return (
        isinstance(previous, Mapping)
        and isinstance(current, Mapping)
        and previous.get("verdict") == "revise"
        and current.get("verdict") == "pass"
        and bool(previous.get("screenshot_sha256"))
        and previous.get("screenshot_sha256") == current.get("screenshot_sha256")
    )


def _mark_pending_review_mode(
    result: dict[str, Any],
    review_configured: bool,
) -> None:
    """Distinguish a configured VLM awaiting review from a no-VLM fallback."""

    result["visual_review_mode"] = "vlm" if review_configured else "deterministic-only"


async def _repair_blocked_inspection(
    best: dict[str, Any],
    *,
    input_data: dict[str, Any],
    progress_callback: Any,
    deadline: float,
    callbacks: VisualLoopCallbacks,
) -> tuple[dict[str, Any], bool]:
    """Repair one blocked render and report whether a better candidate was committed."""

    inspection = best.get("inspection")
    outcome = inspection.get("outcome") if isinstance(inspection, dict) else None
    inspection_blocked = (
        isinstance(outcome, Mapping) and outcome.get("code") == "inspection_blocked"
    )
    feedback_items = best.get("inspection_feedback")
    feedback = (
        [str(item).strip() for item in feedback_items if str(item).strip()]
        if isinstance(feedback_items, list)
        else []
    )
    attempt = revision_state.inspection_repair_attempt(best)
    if not inspection_blocked or not feedback:
        return workflow_outcomes.rendered_inspection_blocked(
            best,
            decision_reason="automatic-revision-exhausted",
        ), False
    if attempt >= revision_state.MAX_INSPECTION_REPAIR_ATTEMPTS:
        return workflow_outcomes.rendered_inspection_blocked(
            best,
            decision_reason="automatic-revision-exhausted",
        ), False

    loop = asyncio.get_running_loop()
    remaining = deadline - loop.time()
    if remaining <= 0:
        return workflow_outcomes.revision_timeout_error(
            best,
            {"error": "Inspection repair timed out before model execution."},
        ), False
    await runtime_io.progress(
        progress_callback,
        "poster.visual-revise",
        0.86,
        iteration=attempt + 1,
        revision_mode="full-layout",
        operations=["reflow"],
    )
    repair_feedback = list(feedback)
    if attempt:
        repair_feedback.append(
            "A previous full-layout revision was rejected because it did not reduce "
            "measured overflow. Use a materially different whole-page composition; "
            "do not repeat that placement strategy or shrink scientific type."
        )
    revision_input = {
        **input_data,
        "action": poster_core.ACTION_REVISE,
        "source_html_uri": str(best.get("html_uri") or ""),
        "source_html_sha256": str(best.get("html_sha256") or ""),
        "feedback": "\n".join(repair_feedback),
        "revision_mode": "full-layout",
        "visual_iteration": revision_state.visual_iteration(
            best.get("visual_iteration")
        ),
        "inspection_repair_attempt": attempt + 1,
        "_baseline_inspection": inspection,
    }
    revision_input.pop("visual_review_path", None)
    revision_input.pop("selection_state", None)
    if not runtime_budget.bound_automatic_revision(
        revision_input,
        remaining_seconds=remaining,
    ):
        return workflow_outcomes.revision_timeout_error(
            best,
            {
                "error": (
                    "The remaining workflow time is too short for a useful model "
                    "revision; the best rendered checkpoint was preserved."
                )
            },
        ), False
    revision_input["_defer_revision_commit"] = callbacks.defer_revision_commit
    revision_state.advance_best_checkpoint(
        best,
        inspection_repair_attempt=attempt + 1,
    )
    try:
        async with asyncio.timeout(remaining):
            candidate = await callbacks.revise(
                revision_input,
                progress_callback,
                deadline=deadline,
            )
    except TimeoutError:
        return workflow_outcomes.revision_timeout_error(
            best,
            {"error": "Inspection repair timed out during model execution."},
        ), False

    retry_from_best = False
    candidate_inspection = callbacks.prepared_inspection(candidate)
    if candidate_inspection is not None:
        incumbent_evidence = candidate_control.CandidateEvidence(
            html_sha256=str(best.get("html_sha256") or ""),
            inspection=inspection if isinstance(inspection, Mapping) else {},
        )
        candidate_evidence = candidate_control.CandidateEvidence(
            html_sha256=str(getattr(candidate, "html_sha256", "") or ""),
            inspection=candidate_inspection,
        )
        if (
            candidate_control.delivery_relation(
                incumbent_evidence,
                candidate_evidence,
            )
            == "dominates"
        ):
            committed = await callbacks.commit(candidate)
            revision_state.advance_best_checkpoint(
                committed,
                inspection_repair_attempt=attempt + 1,
            )
            return committed, True
        workflow_outcomes.append_warning_once(
            best,
            RENDERED_REVISION_REJECTED_WARNING,
        )
        retry_from_best = True
    else:
        if workflow_outcomes.revision_model_timed_out(candidate):
            return workflow_outcomes.revision_timeout_error(best, candidate), False
        outcome = candidate.get("outcome")
        outcome_code = (
            str(outcome.get("code") or "") if isinstance(outcome, Mapping) else ""
        )
        if outcome_code == "revision_noop" or candidate.get("revision_noop") is True:
            workflow_outcomes.append_warning_once(best, NOOP_REVISION_WARNING)
            retry_from_best = True
        else:
            warning = str(candidate.get("error") or candidate.get("summary") or "")
            if warning:
                workflow_outcomes.append_warning_once(best, warning)
    if retry_from_best and attempt + 1 < revision_state.MAX_INSPECTION_REPAIR_ATTEMPTS:
        return best, True
    return workflow_outcomes.rendered_inspection_blocked(
        best,
        decision_reason="automatic-revision-exhausted",
    ), False


def _inspection_is_blocked(result: Mapping[str, Any]) -> bool:
    """Return whether the published candidate has blocking rendered geometry."""

    inspection = result.get("inspection")
    outcome = inspection.get("outcome") if isinstance(inspection, Mapping) else None
    return isinstance(outcome, Mapping) and outcome.get("code") == "inspection_blocked"


def _reusable_receipt(
    path: Path,
    request: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        receipt = visual_review.load_receipt(
            path,
            expected_html_sha256=str(request["candidate_html_sha256"]),
            expected_reference_image_sha256=str(request["reference_image_sha256"]),
        )
    except visual_review.VisualReviewError:
        return None
    if (
        receipt["request_sha256"] != request["request_sha256"]
        or receipt["screenshot_sha256"] != request["screenshot_sha256"]
        or receipt["visual_evidence_sha256"] != request["visual_evidence_sha256"]
    ):
        return None
    return receipt


def _outcome_state(receipt: Mapping[str, Any]) -> ReviewState:
    state = str(receipt.get("quality_state") or "")
    if state not in {"passed", "revision-required", "failed"}:
        raise visual_review.VisualReviewError(
            "visual_review_invalid", "visual review receipt quality state is invalid"
        )
    return state  # type: ignore[return-value]


__all__ = [
    "NOOP_REVISION_WARNING",
    "RENDERED_REVISION_REJECTED_WARNING",
    "VisualLoopCallbacks",
    "VisualLoopRuntime",
    "VisualReviewOutcome",
    "complete",
    "review_and_persist",
    "runtime_from_context",
    "runtime_from_env",
]
