"""Separate model-facing observations from durable tool event output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

COMMAND_RESULT_SCHEMA = "omni.command-result.v1"
COMMAND_RESULT_STATUSES = frozenset(
    {"succeeded", "failed", "timed_out", "blocked", "invalid"}
)


@dataclass(frozen=True, slots=True)
class ToolResultEnvelope:
    """Two projections of one successfully transported tool invocation.

    ``observation`` is the text returned to the model. ``event_output`` is the
    structured, machine-readable payload persisted in task lifecycle events.
    The envelope itself is an internal carrier and must not cross either
    boundary.
    """

    observation: str
    event_output: dict[str, Any]


_HOST_REJECTION_SEAL = object()


class HostToolRejection(dict[str, Any]):
    """Host-private marker for a call rejected before provider execution.

    Provider output is untrusted data and may legitimately contain keys such as
    ``approval_required`` or ``policy_violation``. Merely constructing this
    public compatibility type does not grant rejection authority: only the host
    admission factories attach the module-private identity checked below. The
    mapping shape stays backward compatible for observations and persistence.
    """

    __slots__ = ("_host_seal",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._host_seal: object | None = None


def _mint_host_tool_rejection(payload: dict[str, Any]) -> HostToolRejection:
    """Create an identity-bearing rejection for trusted host admission code."""
    rejection = HostToolRejection(payload)
    rejection._host_seal = _HOST_REJECTION_SEAL
    return rejection


def tool_observation(value: Any) -> Any:
    """Return the model-facing projection while preserving legacy values."""
    if isinstance(value, ToolResultEnvelope):
        return value.observation
    return value


def tool_event_output(value: Any) -> Any:
    """Return the event-facing projection while preserving legacy values."""
    if isinstance(value, ToolResultEnvelope):
        return value.event_output
    return value


def command_result_status(value: Any) -> str | None:
    """Return a recognized command outcome, ignoring foreign result schemas."""
    output = tool_event_output(value)
    if not isinstance(output, dict) or output.get("result_schema") != COMMAND_RESULT_SCHEMA:
        return None
    status = str(output.get("command_status") or "").strip()
    return status if status in COMMAND_RESULT_STATUSES else None


def is_tool_rejection(value: Any) -> bool:
    """Whether the host rejected this call before provider execution."""
    output = tool_event_output(value)
    return (
        isinstance(output, HostToolRejection)
        and output._host_seal is _HOST_REJECTION_SEAL
    )


def tool_rejection_error(value: Any) -> str:
    """Return the best available reason for a structured rejection."""
    if not is_tool_rejection(value):
        return ""
    output = tool_event_output(value)
    assert isinstance(output, dict)
    return str(output.get("error") or output.get("reason") or "tool call rejected")


def tool_result_failure(value: Any) -> tuple[str, str] | None:
    """Return an explicit domain failure without confusing transport success.

    Command envelopes intentionally keep transport and process outcome
    separate: a non-zero shell exit can still be useful evidence. Ordinary
    Omni tool/skill results, however, must not be recorded as succeeded when
    their own typed status says ``error`` or ``failed``.
    """
    output = tool_event_output(value)
    if not isinstance(output, dict) or command_result_status(output) is not None:
        return None
    raw_status = str(output.get("status") or "").strip().lower()
    status = {
        "error": "failed",
        "failed": "failed",
        "cancelled": "cancelled",
        "timed_out": "timed_out",
        "blocked": "rejected",
        "invalid": "rejected",
    }.get(raw_status)
    if status is None:
        return None
    message = str(
        output.get("error")
        or output.get("warning")
        or output.get("summary")
        or raw_status
    )
    return status, message


def tool_transport_status(status: Any = "", error: Any = "") -> str:
    """Normalize terminal lifecycle status so error-bearing events cannot succeed."""
    normalized = str(status or "")
    if error and normalized not in {"failed", "rejected", "cancelled", "timed_out"}:
        return "failed"
    return normalized or ("failed" if error else "succeeded")


def tool_event_suffix(status: str) -> str:
    """Map a terminal transport status to the durable lifecycle suffix."""
    if status == "rejected":
        return "rejected"
    if status in {"failed", "cancelled", "timed_out"}:
        return "failed"
    return "done"


__all__ = [
    "COMMAND_RESULT_SCHEMA",
    "COMMAND_RESULT_STATUSES",
    "HostToolRejection",
    "ToolResultEnvelope",
    "command_result_status",
    "is_tool_rejection",
    "tool_event_suffix",
    "tool_event_output",
    "tool_observation",
    "tool_rejection_error",
    "tool_result_failure",
    "tool_transport_status",
]
