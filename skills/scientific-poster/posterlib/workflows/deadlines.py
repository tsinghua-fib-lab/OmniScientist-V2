"""Runtime deadlines and model-work budgets for scientific-poster workflows."""

from __future__ import annotations

import math
from typing import Any

from posterlib.generation import model_runtime

WORKFLOW_RUNTIME_BUDGET_SECONDS = 3590.0
REFERENCE_MAX_SECONDS = 300.0
POST_PLANNING_RESERVE_SECONDS = 180.0
POST_REFERENCE_RESERVE_SECONDS = 100.0
PPTX_EXPORT_RESERVE_SECONDS = 90.0
HOST_EXECUTION_RESERVE_SECONDS = 10.0
VISUAL_LOOP_TIMEOUT_WARNING = (
    "The bounded automatic visual loop reached its runtime budget; the latest "
    "checkpointed candidate remains pending review."
)


def reference_deadline(workflow_deadline: float, now: float) -> float:
    """Bound reference-pixel interpretation to its share of remaining time."""

    return now + reference_step_budget_seconds(workflow_deadline, now)


def draft_planning_deadline(
    workflow_deadline: float,
    now: float,
    *,
    reserve_reference_phase: bool = True,
) -> float:
    """Stop semantic planning early enough to render and review one exact candidate."""

    reference_reserve = (
        REFERENCE_MAX_SECONDS if reserve_reference_phase else 0.0
    )
    return max(
        now,
        workflow_deadline
        - reference_reserve
        - POST_PLANNING_RESERVE_SECONDS,
    )


def reference_step_budget_seconds(workflow_deadline: float, now: float) -> float:
    """Give reference interpretation its available window after delivery reserve."""

    available = max(
        0.0,
        workflow_deadline - now - POST_REFERENCE_RESERVE_SECONDS,
    )
    return min(
        REFERENCE_MAX_SECONDS,
        available,
    )


def workflow_timeout_seconds(input_data: dict[str, Any]) -> float:
    """Validate the requested shared workflow envelope before starting work."""

    value = input_data.get("workflow_timeout_seconds", WORKFLOW_RUNTIME_BUDGET_SECONDS)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= 3600
    ):
        raise model_runtime.ModelBoundaryError(
            "invalid_payload",
            "workflow_timeout_seconds must be greater than 0 and at most 3600",
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


def editing_deadline(workflow_deadline: float, now: float) -> float:
    """Reserve delivery time once, before any planning, editing or visual review."""

    remaining = max(0.0, workflow_deadline - now)
    return workflow_deadline - min(PPTX_EXPORT_RESERVE_SECONDS, remaining * 0.2)


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
        raw_remaining = remaining()
        if not isinstance(raw_remaining, bool) and isinstance(
            raw_remaining, (int, float)
        ):
            remaining_seconds = float(raw_remaining)
            if math.isfinite(remaining_seconds):
                if remaining_seconds <= 0:
                    return now
                return max(
                    now,
                    min(
                        deadline,
                        now
                        + remaining_seconds
                        - HOST_EXECUTION_RESERVE_SECONDS,
                    ),
                )
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
