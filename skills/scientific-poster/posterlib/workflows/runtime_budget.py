"""Runtime deadlines and model-work budgets for scientific-poster workflows."""

from __future__ import annotations

import math
from typing import Any

from posterlib.generation import model_runtime

WORKFLOW_RUNTIME_BUDGET_SECONDS = 540.0
VISUAL_LOOP_BUDGET_SECONDS = 240.0
POST_REFERENCE_RESERVE_SECONDS = 300.0
REFERENCE_PREFLIGHT_MAX_SECONDS = 60.0
DRAFT_PUBLICATION_RESERVE_SECONDS = 60.0
MIN_FULL_HTML_REPAIR_BUDGET_SECONDS = 150.0
REVISION_PUBLICATION_RESERVE_SECONDS = 10.0
FOLLOWUP_VISUAL_REVIEW_RESERVE_SECONDS = 65.0
MIN_AUTOMATIC_MODEL_BUDGET_SECONDS = 45.0
HOST_EXECUTION_RESERVE_SECONDS = 10.0
VISUAL_LOOP_TIMEOUT_WARNING = (
    "The bounded automatic visual loop reached its runtime budget; the latest "
    "checkpointed candidate remains pending review."
)


def bound_automatic_revision(
    revision_input: dict[str, Any], *, remaining_seconds: float
) -> bool:
    """Bound model work before an atomic revision publication begins."""

    requested = revision_input.get(
        "authoring_timeout_seconds",
        model_runtime.DEFAULT_AUTHORING_TIMEOUT_SECONDS,
    )
    if isinstance(requested, bool) or not isinstance(requested, (int, float)):
        requested = model_runtime.DEFAULT_AUTHORING_TIMEOUT_SECONDS
    model_budget = remaining_seconds - REVISION_PUBLICATION_RESERVE_SECONDS
    if model_budget < MIN_AUTOMATIC_MODEL_BUDGET_SECONDS:
        return False
    revision_input["authoring_timeout_seconds"] = min(float(requested), model_budget)
    revision_input["authoring_transport_retries"] = 0
    revision_input["_bounded_visual_revision"] = True
    return True


def automatic_revision_deadline(workflow_deadline: float) -> float:
    """Reserve time for the revised screenshot's bound VLM review."""

    return workflow_deadline - FOLLOWUP_VISUAL_REVIEW_RESERVE_SECONDS


def draft_authoring_deadline(workflow_deadline: float) -> float:
    """Reserve time to validate, render, and persist an authored candidate."""

    return workflow_deadline - DRAFT_PUBLICATION_RESERVE_SECONDS


def reference_preflight_deadline(workflow_deadline: float, now: float) -> float:
    """Bound reference-pixel interpretation while preserving the draft tail."""

    return max(
        now,
        min(
            now + REFERENCE_PREFLIGHT_MAX_SECONDS,
            workflow_deadline - POST_REFERENCE_RESERVE_SECONDS,
        ),
    )


def workflow_deadline(ctx: Any, now: float) -> float:
    """Bound one model-backed workflow below the common outer execution timeout."""

    return bounded_host_deadline(
        ctx,
        now=now,
        local_budget_seconds=WORKFLOW_RUNTIME_BUDGET_SECONDS,
    )


def visual_loop_deadline(ctx: Any, now: float) -> float:
    """Leave a host-provided execution envelope enough time to persist a result."""

    return bounded_host_deadline(
        ctx,
        now=now,
        local_budget_seconds=VISUAL_LOOP_BUDGET_SECONDS,
    )


def bounded_host_deadline(
    ctx: Any,
    *,
    now: float,
    local_budget_seconds: float,
) -> float:
    """Intersect a local budget with a positive host deadline."""

    deadline = now + local_budget_seconds
    raw_host_deadline = getattr(ctx, "execution_deadline", 0.0)
    if isinstance(raw_host_deadline, bool) or not isinstance(
        raw_host_deadline, (int, float)
    ):
        return deadline
    host_deadline = float(raw_host_deadline)
    if not math.isfinite(host_deadline) or host_deadline <= 0:
        return deadline
    if host_deadline <= now:
        return now
    return max(
        now,
        min(deadline, host_deadline - HOST_EXECUTION_RESERVE_SECONDS),
    )


def host_llm(ctx: Any) -> Any:
    """Return the callable host LLM or raise the stable boundary error."""

    llm = getattr(ctx, "llm", None) if ctx is not None else None
    if llm is None or not callable(getattr(llm, "chat", None)):
        raise model_runtime.ModelBoundaryError(
            "llm_unavailable",
            "This host action requires an LLM supplied by the host runtime.",
        )
    return llm
