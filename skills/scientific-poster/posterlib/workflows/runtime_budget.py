"""Runtime deadlines and model-work budgets for scientific-poster workflows."""

from __future__ import annotations

import math
from typing import Any

from posterlib.generation import model_runtime

WORKFLOW_RUNTIME_BUDGET_SECONDS = 1790.0
REFERENCE_PREFLIGHT_MAX_SECONDS = 150.0
# Preserve one normal VLM-review window plus deterministic authoring, inspection,
# persistence, and the host reserve. The former 140-second value belonged to the
# removed model-authored HTML/repair loop and could starve reference interpretation.
REFERENCE_DOWNSTREAM_RESERVE_SECONDS = 100.0
HOST_EXECUTION_RESERVE_SECONDS = 10.0
VISUAL_LOOP_TIMEOUT_WARNING = (
    "The bounded automatic visual loop reached its runtime budget; the latest "
    "checkpointed candidate remains pending review."
)


def reference_preflight_deadline(workflow_deadline: float, now: float) -> float:
    """Bound reference-pixel interpretation to its share of remaining time."""

    return now + reference_step_budget_seconds(workflow_deadline, now)


def reference_step_budget_seconds(workflow_deadline: float, now: float) -> float:
    """Give reference interpretation its available window after delivery reserve."""

    available = max(
        0.0,
        workflow_deadline - now - REFERENCE_DOWNSTREAM_RESERVE_SECONDS,
    )
    return min(
        REFERENCE_PREFLIGHT_MAX_SECONDS,
        available,
    )


def workflow_timeout_seconds(input_data: dict[str, Any]) -> float:
    """Validate the requested shared workflow envelope before starting work."""

    value = input_data.get("workflow_timeout_seconds", WORKFLOW_RUNTIME_BUDGET_SECONDS)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= 1800
    ):
        raise model_runtime.ModelBoundaryError(
            "invalid_payload",
            "workflow_timeout_seconds must be greater than 0 and at most 1800",
        )
    return float(value)


def workflow_deadline(
    ctx: Any, now: float, input_data: dict[str, Any] | None = None
) -> float:
    """Bound one model-backed workflow below the common outer execution timeout."""

    return bounded_host_deadline(
        ctx,
        now=now,
        local_budget_seconds=workflow_timeout_seconds(input_data or {}),
    )


def visual_loop_deadline(
    ctx: Any, now: float, input_data: dict[str, Any] | None = None
) -> float:
    """Leave a host-provided execution envelope enough time to persist a result."""

    return workflow_deadline(ctx, now, input_data)


def bounded_host_deadline(
    ctx: Any,
    *,
    now: float,
    local_budget_seconds: float,
) -> float:
    """Intersect a local budget with a positive host deadline."""

    deadline = now + local_budget_seconds
    clock = getattr(ctx, "execution_clock", None)
    remaining = getattr(clock, "remaining", None)
    if callable(remaining):
        seconds = remaining()
        if (
            isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
            and math.isfinite(seconds)
        ):
            return max(now, min(deadline, now + seconds - HOST_EXECUTION_RESERVE_SECONDS))
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
