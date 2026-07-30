"""Separate model-facing observations, domain data, and invocation outcomes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal

COMMAND_RESULT_SCHEMA = "omni.command-result.v1"
COMMAND_RESULT_STATUSES = frozenset(
    {"succeeded", "failed", "timed_out", "blocked", "invalid"}
)


ToolLifecycle = Literal["completed", "failed", "blocked", "aborted", "timed_out"]


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    """Host-owned lifecycle result for one tool invocation.

    Tool output is untrusted domain data.  A returned dictionary containing
    ``status=cancelled`` may describe an old task, a remote job, or any other
    object; it must never cancel the invocation that transported it.  Only this
    internal carrier has lifecycle authority.
    """

    lifecycle: ToolLifecycle = "completed"
    result_success: bool | None = True
    error: str = ""

    @classmethod
    def completed(
        cls,
        *,
        success: bool = True,
        error: str = "",
    ) -> ToolCallOutcome:
        return cls(
            lifecycle="completed",
            result_success=success,
            error=error,
        )

    @classmethod
    def failed(cls, error: str) -> ToolCallOutcome:
        return cls(
            lifecycle="failed",
            result_success=None,
            error=error,
        )

    @classmethod
    def blocked(
        cls,
        error: str,
    ) -> ToolCallOutcome:
        return cls(
            lifecycle="blocked",
            result_success=None,
            error=error,
        )

    @classmethod
    def aborted(cls, error: str) -> ToolCallOutcome:
        return cls(
            lifecycle="aborted",
            result_success=None,
            error=error,
        )

    @classmethod
    def timed_out(cls, error: str) -> ToolCallOutcome:
        return cls(
            lifecycle="timed_out",
            result_success=None,
            error=error,
        )


@dataclass(frozen=True, slots=True)
class ToolResultEnvelope:
    """Two data projections plus the host-owned invocation outcome.

    ``observation`` is the text returned to the model. ``event_output`` is the
    structured, machine-readable payload persisted in task lifecycle events.
    The envelope itself is an internal carrier and must not cross either
    boundary.
    """

    observation: str
    event_output: Any
    outcome: ToolCallOutcome = field(default_factory=ToolCallOutcome.completed)


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


def tool_call_outcome(value: Any) -> ToolCallOutcome:
    """Return the trusted invocation outcome; ordinary values completed safely."""
    if isinstance(value, ToolResultEnvelope):
        return value.outcome
    if is_tool_rejection(value):
        return ToolCallOutcome.blocked(tool_rejection_error(value))
    return ToolCallOutcome.completed()


def owned_result_outcome(value: Any) -> ToolCallOutcome | None:
    """Resolve Omni-owned result schemas at their trusted adapter boundary.

    This resolver is deliberately opt-in on first-party tools.  It must not be
    applied to arbitrary provider/MCP JSON.  A domain failure means the handler
    completed but its requested operation did not. Declared action cancellation,
    timeout, and rejection remain distinct typed outcomes.
    """
    output = tool_event_output(value)
    if not isinstance(output, dict) or command_result_status(output) is not None:
        return None
    raw_status = str(output.get("status") or "").strip().lower()
    error = str(
        output.get("error")
        or output.get("warning")
        or output.get("summary")
        or raw_status
    )
    if raw_status == "cancelled":
        return ToolCallOutcome.aborted(error or "cancelled")
    if raw_status == "timed_out":
        return ToolCallOutcome.timed_out(error or "timed out")
    if raw_status in {"blocked", "invalid", "rejected"}:
        return ToolCallOutcome.blocked(error or raw_status)
    failed = raw_status in {"error", "failed"}
    if failed or (not raw_status and bool(output.get("error"))):
        return ToolCallOutcome.completed(success=False, error=error or "tool result failed")
    return None


def recall_result_outcome(value: Any) -> ToolCallOutcome | None:
    """Resolve Recall errors without confusing them with historical object state."""
    output = tool_event_output(value)
    if not isinstance(output, dict):
        return None
    if (
        (output.get("task_id") and output.get("task_status"))
        or (output.get("subtask_id") and output.get("subtask_status"))
    ):
        # The error belongs to the historical object being inspected.  The
        # read itself completed successfully.
        return ToolCallOutcome.completed()
    if not output.get("error"):
        return ToolCallOutcome.completed()
    return ToolCallOutcome.completed(
        success=False,
        error=str(output["error"]),
    )


def attach_tool_outcome(value: Any, outcome: ToolCallOutcome | None) -> Any:
    """Attach an adapter-resolved outcome without leaking the carrier downstream."""
    if outcome is None:
        return value
    if isinstance(value, ToolResultEnvelope):
        return replace(value, outcome=outcome)
    observation = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    return ToolResultEnvelope(
        observation=observation,
        event_output=value,
        outcome=outcome,
    )


def command_result_status(value: Any) -> str | None:
    """Return a recognized command outcome, ignoring foreign result schemas."""
    output = tool_event_output(value)
    if not isinstance(output, dict) or output.get("result_schema") != COMMAND_RESULT_SCHEMA:
        return None
    status = str(output.get("command_status") or "").strip()
    return status if status in COMMAND_RESULT_STATUSES else None


_COMMAND_HINT_LIMIT = 140
_HEAD_TAIL_MARK = "\n…\n"
_EXIT_GLOSS = {
    126: "cannot execute",
    127: "command not found",
}
_ERROR_LINE_MARKERS = (
    "permission denied",
    "not a git repository",
    "no such file",
    "command not found",
    "cannot execute",
    "is a directory",
    "exec format error",
    "fatal:",
    "fatal error",
    "\u81f4\u547d\u9519\u8bef",
    "error:",
    "traceback",
    "exception",
)
_PROGRESS_LINE = re.compile(r"^[.sFEx]*\s*\[[ \d]+%\]\s*$")
_ENDS_WITH_PERCENT = re.compile(r"\[[ \d]+%\]\s*$")


def _clip_command_line(line: str, limit: int) -> str:
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _collapsed_command_lines(output: str) -> list[str]:
    lines: list[str] = []
    for raw in str(output or "").splitlines():
        line = " ".join(raw.split())
        if line:
            lines.append(line)
    return lines


def first_command_output_line(output: str, *, limit: int = _COMMAND_HINT_LIMIT) -> str:
    """First non-empty process line, collapsed, for human-facing failure text."""
    lines = _collapsed_command_lines(output)
    return _clip_command_line(lines[0], limit) if lines else ""


def last_command_output_line(output: str, *, limit: int = _COMMAND_HINT_LIMIT) -> str:
    """Last non-empty process line, collapsed."""
    lines = _collapsed_command_lines(output)
    return _clip_command_line(lines[-1], limit) if lines else ""


def _has_error_marker(line: str) -> bool:
    folded = line.casefold()
    return any(marker in folded for marker in _ERROR_LINE_MARKERS)


def _is_progress_line(line: str) -> bool:
    """Pytest-style progress, or an agent poll that only restates a percent."""
    if _has_error_marker(line):
        return False
    return bool(_PROGRESS_LINE.fullmatch(line) or _ENDS_WITH_PERCENT.search(line))


def _best_error_line(output: str, *, limit: int = _COMMAND_HINT_LIMIT) -> str:
    """Prefer an error-shaped line, then the last non-progress line."""
    lines = _collapsed_command_lines(output)
    if not lines:
        return ""
    for line in reversed(lines):
        if _has_error_marker(line):
            return _clip_command_line(line, limit)
    for line in reversed(lines):
        if not _is_progress_line(line):
            return _clip_command_line(line, limit)
    return ""


def command_failure_hint(
    exit_code: int,
    output: str = "",
    stderr: str = "",
    *,
    limit: int = _COMMAND_HINT_LIMIT,
) -> str:
    """One-line cause for a failed process.

    Stderr wins over the merged stream. Progress lines such as ``[ 36%]`` are
    not a cause. Exit 126/127 get a host gloss when the process said nothing
    usable — ``Permission denied`` is often missing after a poll that printed
    pytest progress and then could not exec.
    """
    for blob in (stderr, output):
        hint = _best_error_line(blob, limit=limit)
        if hint:
            return hint
    return _EXIT_GLOSS.get(int(exit_code), "") if not isinstance(exit_code, bool) else ""


def command_exit_summary(exit_code: int, output: str = "", stderr: str = "") -> str:
    """Numeric exit plus the best process line or a 126/127 gloss.

    A bare ``Command exited with code 128`` hides Git's ``not a git repository``.
    Taking the first line hid 126's ``Permission denied`` behind pytest
    progress. Prefer stderr, then an error-shaped or last useful line.
    """
    hint = command_failure_hint(exit_code, output, stderr)
    if hint:
        return f"Command exited with code {exit_code}: {hint}"
    return f"Command exited with code {exit_code}"


def command_output_window(text: str, limit: int, *, tail_weight: int = 2) -> str:
    """Fit ``text`` into ``limit`` code points, keeping head and tail.

    ``tail_weight=2`` is one-third head and two-thirds tail, matching Codex's
    bias toward the diagnostic that usually arrives last.
    """
    text = str(text or "")
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    available = limit - len(_HEAD_TAIL_MARK)
    if available < 2:
        return text[:limit]
    head = max(1, available // (tail_weight + 1))
    tail = available - head
    return text[:head] + _HEAD_TAIL_MARK + text[-tail:]


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
    """Project a trusted outcome onto the legacy runtime failure tuple."""
    outcome = tool_call_outcome(value)
    if outcome.lifecycle == "completed" and outcome.result_success is not False:
        return None
    status = {
        "completed": "failed",
        "failed": "failed",
        "blocked": "rejected",
        "aborted": "cancelled",
        "timed_out": "timed_out",
    }[outcome.lifecycle]
    return status, outcome.error or status


def tool_outcome_event_fields(value: Any) -> dict[str, Any]:
    """Canonical durable fields for a trusted invocation result."""
    outcome = tool_call_outcome(value)
    return {
        "lifecycle_status": outcome.lifecycle,
        "result_success": outcome.result_success,
    }


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


# Tool identity travels under two keys depending on how far the event has been
# relayed: the loop and gateway emit ``name``, while progress relays that wrap a
# nested event flatten it as ``tool``. Renderers must read both, or a correctly
# named call renders as an anonymous placeholder purely because of the hop it
# arrived through.
TOOL_EVENT_NAME_KEYS = ("name", "tool")


def tool_event_name(data: Any) -> str:
    """Return the tool name carried by a tool/progress event payload."""
    if not isinstance(data, dict):
        return ""
    for key in TOOL_EVENT_NAME_KEYS:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


__all__ = [
    "COMMAND_RESULT_SCHEMA",
    "COMMAND_RESULT_STATUSES",
    "TOOL_EVENT_NAME_KEYS",
    "HostToolRejection",
    "ToolCallOutcome",
    "ToolLifecycle",
    "ToolResultEnvelope",
    "attach_tool_outcome",
    "command_exit_summary",
    "command_failure_hint",
    "command_output_window",
    "command_result_status",
    "first_command_output_line",
    "last_command_output_line",
    "is_tool_rejection",
    "tool_event_suffix",
    "tool_event_name",
    "tool_event_output",
    "tool_call_outcome",
    "tool_observation",
    "tool_outcome_event_fields",
    "owned_result_outcome",
    "recall_result_outcome",
    "tool_rejection_error",
    "tool_result_failure",
    "tool_transport_status",
]
