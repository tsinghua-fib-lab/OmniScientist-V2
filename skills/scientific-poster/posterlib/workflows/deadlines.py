"""Host execution envelopes and explicit poster timeout requests."""

from __future__ import annotations

import math
from typing import Any

from posterlib.generation.model_runtime import ModelBoundaryError

VISUAL_LOOP_TIMEOUT_WARNING = (
    "The visual loop reached its execution deadline; the selected candidate "
    "and its current review state were retained."
)


def optional_timeout_seconds(input_data: dict[str, Any], name: str) -> float | None:
    """Read a positive duration when the caller explicitly supplies one."""

    value = input_data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ModelBoundaryError("invalid_payload", f"{name} must be a positive finite number")
    return float(value)


def workflow_timeout_seconds(input_data: dict[str, Any]) -> float | None:
    """Return the caller's optional timeout for the complete workflow."""

    return optional_timeout_seconds(input_data, "workflow_timeout_seconds")


def workflow_deadline(
    ctx: Any, now: float, input_data: dict[str, Any] | None = None,
) -> float | None:
    """Share the exposed host envelope and any explicit caller timeout."""

    seconds = workflow_timeout_seconds(input_data or {})
    bounds = [now + seconds] if seconds is not None else []
    clock = getattr(ctx, "execution_clock", None)
    remaining = getattr(clock, "remaining", None)
    if callable(remaining):
        seconds = remaining()
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and math.isfinite(seconds):
            bounds.append(now + max(0.0, seconds))
    else:
        host_deadline = getattr(ctx, "execution_deadline", None)
        if (
            isinstance(host_deadline, (int, float)) and not isinstance(host_deadline, bool)
            and math.isfinite(host_deadline) and host_deadline > 0
        ):
            bounds.append(host_deadline)
    return min(bounds) if bounds else None
