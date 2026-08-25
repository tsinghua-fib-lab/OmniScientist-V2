"""Classify skill wall-clock expiry without growing the subtask runtime.

A declared skill budget that produced a durable artifact is degraded.
A workflow envelope that expired before the skill started, or a skill
that timed out with nothing on disk, is failed so the turn can replan.
"""

from __future__ import annotations

from omni.skills_runtime.executor import SkillExecutionTimeout

_WORKFLOW_ENVELOPE = "workflow_envelope"


def skill_timeout_kind(exc: BaseException) -> str | None:
    """Return the timeout bound, or None when ``exc`` is not a skill timeout."""
    if not isinstance(exc, SkillExecutionTimeout):
        return None
    kind = str(getattr(exc, "kind", "") or "").strip()
    return kind or "skill_budget"


def skill_exception_status(exc: BaseException, *, has_durable_output: bool = False) -> str:
    """Map a skill exception to the durable execution status.

    Workflow-envelope expiry is always failed: the child never had its own
    budget. A skill-owned budget or stall is degraded only when a deliverable
    already exists; zero output stays failed so SINGLE_SKILL can fall through.
    """
    kind = skill_timeout_kind(exc)
    if kind is None:
        return "failed"
    if kind == _WORKFLOW_ENVELOPE:
        return "failed"
    return "degraded" if has_durable_output else "failed"


def timeout_failure_result(
    *,
    status: str,
    err: str,
    subtask_id: str,
    task_id: str,
) -> dict[str, object] | None:
    """Persist a recoverable timeout payload only when the run is degraded."""
    if status != "degraded":
        return None
    return {
        "status": status,
        "summary": err,
        "recoverable": True,
        "subtask_id": subtask_id,
        "task_id": task_id,
        "error": err,
    }


__all__ = [
    "skill_exception_status",
    "skill_timeout_kind",
    "timeout_failure_result",
]
