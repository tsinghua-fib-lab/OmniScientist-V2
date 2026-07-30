"""Pure DAG selection and durable workflow state projections."""

from __future__ import annotations

from typing import Any

from omni.runtime.task_results import _result_summary
from omni.runtime.workflow_plan import _is_child_task_step, _is_native_workflow_step
from omni.skills_runtime.registry import SkillRegistry, resolve_step_entry


def all_dependencies(step: dict[str, Any]) -> list[str]:
    """Return required and optional dependency ids in declaration order."""
    return [
        *[str(item) for item in step.get("depends_on") or [] if item],
        *[str(item) for item in step.get("optional_depends_on") or [] if item],
    ]


def step_and_descendants(steps: list[Any], step_id: str) -> set[str]:
    """Return the retry invalidation set rooted at ``step_id``."""
    if not step_id:
        return set()
    children: dict[str, set[str]] = {}
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        child = str(raw.get("id") or "")
        for dependency in all_dependencies(raw):
            children.setdefault(dependency, set()).add(child)
    descendants = {step_id}
    frontier = [step_id]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, set()):
            if child and child not in descendants:
                descendants.add(child)
                frontier.append(child)
    return descendants


def dependencies_terminal(step: dict[str, Any], terminal_ids: set[str]) -> bool:
    """Return whether every dependency has reached a terminal state."""
    return all(dependency in terminal_ids for dependency in all_dependencies(step))


def step_concurrent_safe(step: dict[str, Any], entry: Any) -> bool:
    """Fail closed: parallelize only an explicit step or skill declaration."""
    if _is_native_workflow_step(step) or _is_child_task_step(step):
        return False
    if "concurrent_safe" in step:
        return step.get("concurrent_safe") is True
    execution = getattr(entry, "execution", {}) if entry is not None else {}
    return isinstance(execution, dict) and execution.get("concurrent_safe") is True


def select_execution_wave(
    ready: list[dict[str, Any]],
    registry: SkillRegistry,
    *,
    concurrency: int,
) -> list[dict[str, Any]]:
    """Return one deterministic DAG wave, respecting unsafe barriers."""
    wave: list[dict[str, Any]] = []
    for step in ready:
        entry = resolve_step_entry(registry, step)
        if not step_concurrent_safe(step, entry):
            return wave or [step]
        wave.append(step)
        if len(wave) >= max(1, concurrency):
            break
    return wave


def workflow_step_record(
    step: dict[str, Any],
    *,
    status: str,
    result: Any = None,
    error: str = "",
    warning: str = "",
    skip_reason: str = "",
    recoverable: bool = False,
) -> dict[str, Any]:
    """Project one execution outcome into the stable workflow step contract."""
    record: dict[str, Any] = {
        "id": step.get("id", ""),
        "skill_name": step.get("skill_name", ""),
        "capability": step.get("capability", ""),
        "provider_type": step.get("provider_type", ""),
        "deliverable": step.get("deliverable", ""),
        "status": status,
        "input": dict(step.get("input") or {}),
        "depends_on": list(step.get("depends_on") or []),
        "required": step.get("required", True) is not False,
    }
    optional_fields = (
        "optional_depends_on",
        "failure_policy",
        "planned_skill_name",
        "normalization_reason",
        "capability_resolution",
        "fallback_skill",
        "provider_binding_id",
        "provider_contract_hash",
        "provider_name",
        "provider_source",
        "provider_version",
        "deliverable_id",
        "quality_contract",
        "execution_id",
        "subtask_id",
        "child_task_id",
        "workflow_step_id",
    )
    for field in optional_fields:
        if step.get(field):
            value = step[field]
            if field == "optional_depends_on":
                record[field] = list(value)
            elif field == "quality_contract":
                record[field] = dict(value)
            else:
                record[field] = value
    if step.get("allow_failed_dependencies"):
        record["allow_failed_dependencies"] = True
    if result is not None:
        record["result"] = result
        summary = _result_summary(result)
        if summary:
            record["summary"] = summary
    for field, value in (("error", error), ("warning", warning), ("skip_reason", skip_reason)):
        if value:
            record[field] = value
    if recoverable:
        record["recoverable"] = True
    return record


def workflow_state(
    workflow_run_id: str,
    goal: str,
    step_records: list[dict[str, Any]],
    total: int,
    *,
    final: bool = False,
) -> dict[str, Any]:
    """Build the user-facing and machine-readable aggregate workflow state."""
    succeeded = sum(1 for step in step_records if step.get("status") == "succeeded")
    degraded = sum(1 for step in step_records if step.get("status") == "degraded")
    failed = sum(1 for step in step_records if step.get("status") == "failed")
    skipped = sum(1 for step in step_records if step.get("status") == "skipped")
    recoverable_failed = sum(
        1
        for step in step_records
        if step.get("status") == "failed" and step.get("recoverable") is True
    )
    blocking = any(
        step.get("status") in {"failed", "skipped"}
        and step.get("required", True) is not False
        and step.get("recoverable") is not True
        for step in step_records
    )
    if final and len(step_records) == total:
        if blocking or (succeeded == 0 and (failed or skipped)):
            status = "failed"
        elif failed or skipped or degraded:
            status = "degraded"
        else:
            status = "succeeded"
    else:
        status = "running"
    skills_used = [
        str(step.get("skill_name"))
        for step in step_records
        if step.get("status") != "skipped"
        and step.get("skill_name")
        and step.get("provider_type") not in {"native_executor", "child_task"}
    ]
    if status == "succeeded":
        summary = f"Workflow completed: {succeeded}/{total} step(s) succeeded."
    elif status == "degraded":
        summary = (
            f"Workflow completed with warnings: {succeeded}/{total} step(s) succeeded, "
            f"{degraded} degraded, {failed} failed, and {skipped} skipped."
        )
    elif status == "failed":
        summary = (
            f"Workflow partially completed: {succeeded}/{total} step(s) succeeded, "
            f"{failed} failed, and {skipped} skipped."
        )
    else:
        summary = f"Workflow running: {len(step_records)}/{total} steps have results."
    payload: dict[str, Any] = {
        "status": status,
        "summary": summary,
        "goal": goal,
        "skills_used": skills_used,
        "steps": list(step_records),
        "workflow": {
            "workflow_run_id": workflow_run_id,
            "goal": goal,
            "step_count": total,
            "completed_steps": len(step_records),
            "succeeded": succeeded,
            "degraded": degraded,
            "failed": failed,
            "skipped": skipped,
            "recoverable_failed": recoverable_failed,
            "skills_used": skills_used,
        },
        "recoverable": bool(step_records),
    }
    first_error = next((str(step.get("error")) for step in step_records if step.get("error")), "")
    first_warning = next(
        (str(step.get("warning")) for step in step_records if step.get("warning")), ""
    )
    if first_error:
        payload["error"] = first_error
    if first_warning:
        payload["warning"] = first_warning
    return payload


def workflow_checkpoint_summary(
    steps: list[dict[str, Any]],
    step_records: list[dict[str, Any]],
    *,
    status: str,
    current_step_id: str = "",
) -> dict[str, Any]:
    """Project current state into the durable checkpoint contract."""
    completed_ids = [
        str(record.get("id"))
        for record in step_records
        if str(record.get("status") or "") in {"succeeded", "degraded"} and record.get("id")
    ]
    failed_ids = [
        str(record.get("id"))
        for record in step_records
        if str(record.get("status") or "") in {"failed", "skipped"} and record.get("id")
    ]
    completed = set(completed_ids)
    terminal = completed | set(failed_ids)
    pending = [
        {
            "step_id": str(step.get("id") or ""),
            "skill_name": str(step.get("skill_name") or ""),
            "capability": str(step.get("capability") or ""),
            "provider_type": str(step.get("provider_type") or ""),
            "depends_on": list(step.get("depends_on") or []),
        }
        for step in steps
        if str(step.get("id") or "") not in terminal
    ]
    return {
        "status": status,
        "current_step_id": current_step_id,
        "last_completed_step_id": completed_ids[-1] if completed_ids else "",
        "completed_step_ids": completed_ids,
        "failed_step_ids": failed_ids,
        "pending_steps": pending,
    }


def workflow_failure_message(step_records: list[dict[str, Any]]) -> str:
    """Return the first actionable terminal failure message."""
    for status, field, fallback in (
        ("failed", "error", "unknown error"),
        ("skipped", "skip_reason", "dependency failed"),
    ):
        for step in step_records:
            if step.get("status") == status:
                return (
                    f"workflow step {step.get('id')} ({step.get('skill_name')}) {status}: "
                    f"{step.get(field) or fallback}"
                )
    return "workflow failed"


__all__ = [
    "all_dependencies",
    "dependencies_terminal",
    "select_execution_wave",
    "step_and_descendants",
    "step_concurrent_safe",
    "workflow_checkpoint_summary",
    "workflow_failure_message",
    "workflow_state",
    "workflow_step_record",
]
