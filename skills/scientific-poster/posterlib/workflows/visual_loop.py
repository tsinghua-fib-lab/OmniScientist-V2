"""Run one evidence-bound visual review without hidden model repair loops."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from posterlib.runtime import runtime_io
from posterlib.visual import visual_review

from . import runtime_budget, workflow_outcomes

# The workflow deadline is the authoritative end-to-end boundary.  Keep this
# transport guard longer than that visual-loop share so a cold local reviewer is
# not cancelled by a second, shorter timeout.
_VLM_TIMEOUT_SECONDS = 600.0

ReviewState = Literal["pending", "passed", "revision-required", "failed"]
ReviewMode = Literal["vlm", "deterministic-only"]
Reviewer = Callable[..., Awaitable[dict[str, Any]]]

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
NOOP_REVISION_WARNING = (
    "The requested revision returned identical HTML; the active poster was preserved."
)


@dataclass(frozen=True)
class VisualLoopRuntime:
    """Injected provider object for one offline-testable visual review."""

    client: Any


@dataclass(frozen=True)
class VisualReviewOutcome:
    """One persisted review result or an explicit pending state."""

    state: ReviewState
    review_mode: ReviewMode
    receipt_path: str | None = None
    receipt: dict[str, Any] | None = None
    warning: str = ""


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
    return VisualLoopRuntime(client=vlm_client.VlmClient(config))


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
    return VisualLoopRuntime(client=client) if client is not None else runtime_from_env(environ)


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
    review_configured = _review_configuration_present(ctx, environ, active_runtime)
    if active_runtime is None:
        return VisualReviewOutcome(
            state="pending",
            review_mode="vlm" if review_configured else "deterministic-only",
            warning=_PROVIDER_WARNING if review_configured else _MISSING_CONFIG_WARNING,
        )

    try:
        if reviewer is None:
            from posterlib.visual.vlm_review import review_request

            reviewer = review_request
        async with asyncio.timeout(_VLM_TIMEOUT_SECONDS):
            result = await reviewer(bound, client=active_runtime.client)
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
    except (OSError, ValueError, RuntimeError, visual_review.VisualReviewError) as exc:
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
    progress_callback: Any,
    ctx: Any = None,
    deadline: float | None = None,
    environ: Mapping[str, str] | None = None,
    runtime: VisualLoopRuntime | None = None,
    reviewer: Reviewer | None = None,
) -> dict[str, Any]:
    """Inspect once and review once; return feedback instead of rewriting HTML."""

    result = initial
    if _inspection_is_blocked(result):
        return workflow_outcomes.rendered_inspection_blocked(
            result,
            decision_reason="deterministic-layout-blocked",
        )

    request = result.get("visual_review_request")
    if not isinstance(request, dict):
        return result

    loop = asyncio.get_running_loop()
    if deadline is None:
        deadline = runtime_budget.visual_loop_deadline(ctx, loop.time())
    review_configured = _review_configuration_present(ctx, environ, runtime)
    request_path = Path(str(result.get("visual_review_request_path") or ""))
    if not request_path.is_file():
        _mark_pending_review_mode(result, review_configured)
        return workflow_outcomes.visual_review_unavailable(
            result,
            _REQUEST_ARTIFACT_WARNING,
        )

    remaining = deadline - loop.time()
    if remaining <= 0:
        _mark_pending_review_mode(result, review_configured)
        return workflow_outcomes.visual_loop_pending(result)

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
    active_runtime = runtime or runtime_from_context(ctx, environ)
    if active_runtime is not None:
        review_kwargs["runtime"] = active_runtime
    if reviewer is not None:
        review_kwargs["reviewer"] = reviewer
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
        return workflow_outcomes.visual_review_unavailable(result, review.warning)

    result["visual_review_path"] = str(review.receipt_path or "")
    result["visual_review"] = review.receipt
    result["visual_quality_state"] = review.state
    inspection = result.get("inspection")
    if not isinstance(inspection, Mapping) or inspection.get("status") != "ok":
        return workflow_outcomes.rendered_inspection_blocked(
            result,
            decision_reason="deterministic-layout-blocked",
        )
    if review.state == "passed":
        return workflow_outcomes.visual_outcome(
            result,
            "visual_review_passed",
            "The rendered inspection and image review passed for these exact "
            "poster bytes.",
        )

    summary = str(review.receipt.get("summary") or "").strip()
    result["revision_feedback"] = summary
    return workflow_outcomes.visual_outcome(
        result,
        "visual_revision_required",
        summary or "The visual reviewer requested a poster revision.",
        decision_reason="visual-review-revision-required",
    )


def _inspection_is_blocked(result: Mapping[str, Any]) -> bool:
    inspection = result.get("inspection")
    outcome = inspection.get("outcome") if isinstance(inspection, Mapping) else None
    return isinstance(outcome, Mapping) and outcome.get("code") == "inspection_blocked"


def _mark_pending_review_mode(result: dict[str, Any], review_configured: bool) -> None:
    result["visual_review_mode"] = "vlm" if review_configured else "deterministic-only"


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
            "visual_review_invalid",
            "visual review receipt quality state is invalid",
        )
    return state  # type: ignore[return-value]


__all__ = [
    "NOOP_REVISION_WARNING",
    "VisualLoopRuntime",
    "VisualReviewOutcome",
    "complete",
    "review_and_persist",
    "runtime_from_context",
    "runtime_from_env",
]
