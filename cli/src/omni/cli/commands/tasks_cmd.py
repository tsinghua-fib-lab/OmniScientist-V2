"""`omni task` — inspect and run background research tasks."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import typer
from sqlalchemy import or_, select

from omni.cli import theme
from omni.cli.render import (
    artifact_line,
    artifact_preview,
    console,
    data_table,
    error,
    info,
    kv_table,
    one_line,
    success,
    warn,
)
from omni.cli.state import (
    AppState,
    make_agent,
    make_agent_for_object,
    make_agent_for_task,
    make_agent_from_settings,
    run_async,
)
from omni.cli.timefmt import format_local_iso, format_local_time
from omni.config.paths import OmniPaths
from omni.config.workspaces import registry_path
from omni.core.identifiers import short_id, shortest_unique_prefixes
from omni.core.tool_result import command_failure_hint, command_result_status
from omni.runtime.aggregate import AggTaskRow, list_tasks_all_workspaces
from omni.runtime.notifications import collect_inbox_notes, latest_delivery_status
from omni.runtime.presentation import task_presentation_from_result
from omni.runtime.task_object_resolver import TaskObjectResolution
from omni.runtime.task_recorder import _TASK_BLOCKED_STATUSES
from omni.runtime.task_results import is_dot_artifact
from omni.runtime.task_status import resolve_task_status
from omni.storage.models import (
    ArtifactORM,
    SubtaskORM,
    TaskEventORM,
    TaskORM,
    WorkflowRunORM,
    WorkflowStepORM,
)

if TYPE_CHECKING:  # imported lazily at call time to keep the CLI start-up light
    from omni.runtime.task_recovery import RecoveryOutcome

app = typer.Typer(help="Inspect and run background research tasks.", no_args_is_help=True)
_TASK_SUBCOMMANDS = (
    "list", "session", "all", "show", "subtask", "step", "watch", "attach", "drain", "inbox",
    "approve", "steer", "cancel", "retry", "resume", "requeue", "archive", "unarchive", "rm",
    "delete", "clear", "prune", "help",
)


class WatchKeyListener:
    """Small terminal helper so ``task watch`` can quit on ``q``.

    It activates only for interactive TTY input. In scripts, tests, or piped
    stdin it quietly falls back to normal sleeping, so watch remains automation
    friendly.
    """

    def __init__(self, stream=None) -> None:  # noqa: ANN001
        self.stream = stream or sys.stdin
        self._active = False
        self._fd: int | None = None
        self._old_attrs = None

    def __enter__(self) -> WatchKeyListener:
        if os.name != "posix" or not hasattr(self.stream, "isatty") or not self.stream.isatty():
            return self
        try:
            import termios
            import tty

            self._fd = self.stream.fileno()
            self._old_attrs = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self._active = True
        except Exception:  # noqa: BLE001 - terminal capabilities vary.
            self._active = False
        return self

    def __exit__(self, *_exc) -> None:  # noqa: ANN002
        if self._active and self._fd is not None and self._old_attrs is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_attrs)
            except Exception:  # noqa: BLE001
                pass

    def should_quit(self, timeout: float = 0.0) -> bool:
        if not self._active:
            return False
        try:
            import select

            ready, _, _ = select.select([self.stream], [], [], max(0.0, timeout))
            if not ready:
                return False
            ch = self.stream.read(1)
        except Exception:  # noqa: BLE001
            return False
        return str(ch).lower() == "q"

    def wait(self, seconds: float) -> bool:
        seconds = max(0.0, seconds)
        if not self._active:
            time.sleep(seconds)
            return False
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.should_quit(min(0.1, remaining)):
                return True

    async def wait_async(self, seconds: float) -> bool:
        seconds = max(0.0, seconds)
        if not self._active:
            await asyncio.sleep(seconds)
            return False
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.should_quit(0.0):
                return True
            await asyncio.sleep(min(0.1, remaining))


def _short(value: str | None, n: int = 8) -> str:
    return short_id(value, n) if value else "-"


def _ts(value: Any) -> str:
    return format_local_time(value)


def _status_text(row: Any) -> str:
    """Status label for a task, subtask, or aggregate row, marking archived rows."""
    status = str(getattr(row, "status", "") or "")
    return f"{status} (archived)" if getattr(row, "archived_at", None) is not None else status


def _squash_summary(text: str, limit: int) -> str:
    """Collapse whitespace and truncate to ``limit`` chars with an ellipsis.

    R5: widen the old hard 70/160-char cuts and never break mid-line in a table
    cell (newlines become spaces), so summaries/rationales stay readable.
    """
    return one_line(text, limit)


def _session_matches(session_id: str | None, prefix: str) -> bool:
    return not prefix or (session_id or "").startswith(prefix)


def _result_artifacts(result: Any) -> list[tuple[str, str, str]]:
    """Collect ``(title, path, uri)`` triples from a skill/step result payload."""
    if not isinstance(result, dict):
        return []
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(value: Any, *, title: str = "", format_hint: str = "") -> None:
        if is_dot_artifact(value, format_hint=format_hint):
            return
        if isinstance(value, dict):
            label = str(value.get("title") or value.get("format") or title or "artifact")
            uri = str(value.get("uri") or value.get("artifact_uri") or "")
            path = str(value.get("path") or value.get("file") or "")
            key = uri or path
            if not key or key in seen:
                return
            seen.add(key)
            out.append((label, path, uri))
        elif isinstance(value, str) and value:
            if value in seen:
                return
            seen.add(value)
            if value.startswith("artifact://"):
                out.append((title or "artifact", "", value))
            else:
                out.append((title or "artifact", value, ""))

    for key, value in result.items():
        if isinstance(value, str) and (value.startswith("artifact://") or key.endswith("_uri")):
            if not is_dot_artifact(value, format_hint=key.removesuffix("_uri")):
                add(value, title=key, format_hint=key.removesuffix("_uri"))
        elif key in {"artifacts", "output_uris", "files"} and isinstance(value, list):
            for item in value:
                add(item, title=key, format_hint=key)
    return out


def _artifact_abs_path(row: ArtifactORM, paths: OmniPaths | None) -> str:
    """Best-effort absolute path for a stored artifact row."""
    rel = str(row.rel_path or "").strip()
    if not rel:
        return ""
    candidate = Path(rel)
    if candidate.is_absolute():
        return str(candidate)
    if paths is None:
        return rel
    return str((paths.project_dir / rel).resolve())


async def _resolve_task_artifacts(
    *,
    task_id: str,
    subtasks: Sequence[SubtaskORM],
    steps: Sequence[WorkflowStepORM],
    db: Any,
    paths: OmniPaths | None,
) -> list[tuple[str, str, str]]:
    """Join canonically owned artifacts + child result payloads for display.

    Prefer rich ``title``/``path``/``uri`` from skill results; fall back to the
    durable ``ArtifactORM`` row. A stale task cache cannot override a different
    canonical producer.
    """
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def push(title: str, path: str, uri: str) -> None:
        key = uri or path
        if not key or key in seen:
            return
        seen.add(key)
        out.append((title or "artifact", path, uri))

    result_artifacts = [
        item
        for payload in [
            *(sub.result_json for sub in subtasks),
            *(step.result_json for step in steps),
        ]
        for item in _result_artifacts(payload)
    ]
    subtask_ids = {sub.id for sub in subtasks if sub.id}
    workflow_ids = {
        value
        for value in [
            *(sub.workflow_run_id for sub in subtasks),
            *(step.workflow_run_id for step in steps),
        ]
        if value
    }
    owned: dict[str, ArtifactORM] = {}
    if db is not None:
        ownership = [ArtifactORM.task_id == task_id]
        if subtask_ids:
            ownership.append(ArtifactORM.subtask_id.in_(subtask_ids))
        if workflow_ids:
            ownership.append(ArtifactORM.workflow_run_id.in_(workflow_ids))
        async with db.session() as s:
            rows = list(
                (
                    await s.execute(select(ArtifactORM).where(or_(*ownership)))
                ).scalars().all()
            )
        for row in rows:
            if row.task_id:
                belongs = row.task_id == task_id
            else:
                belongs = bool(
                    (row.subtask_id and row.subtask_id in subtask_ids)
                    or (row.workflow_run_id and row.workflow_run_id in workflow_ids)
                )
            if belongs:
                owned[row.id] = row

    for title, path, uri in result_artifacts:
        row: ArtifactORM | None = None
        if uri.startswith("artifact://"):
            artifact_id = uri.removeprefix("artifact://")
            if db is not None and artifact_id not in owned:
                continue
            row = owned.get(artifact_id)
        # A result that names only a URI is not the richer description this
        # prefers it for, and it still claims the key the stored row would be
        # pushed under — which is the side that knows the file. research-ideation
        # reports its report as ``report_uri`` alone, so `/task show` printed an
        # identifier for a store instead of a path anyone could open.
        if not path and row is not None:
            path = _artifact_abs_path(row, paths)
        push(title, path, uri)
    for row in owned.values():
        push(
            row.title or row.kind or "artifact",
            _artifact_abs_path(row, paths),
            row.uri or f"artifact://{row.id}",
        )
    return out


def _print_artifacts(rows: Sequence[tuple[str, str, str]], *, limit: int = 24) -> None:
    """Render artifacts as a bold **title** plus its path and dim ``artifact://``."""
    if not rows:
        return
    console.print(f"\n[{theme.STRONG} {theme.ACCENT}]artifacts[/]")
    for title, path, uri in list(rows)[:limit]:
        artifact_line(title or "artifact", path, uri)


def _subtask_summary(task: SubtaskORM) -> str:
    if task.error:
        return task.error
    result = task.result_json or {}
    if isinstance(result, dict):
        for key in ("summary", "text", "abstract", "message", "title", "result"):
            if result.get(key):
                return str(result[key])[:1200]
    return json.dumps(result, ensure_ascii=False, default=str)[:1200] if result else "(no result)"


def _result_summary_text(result: Any, error_text: str = "") -> str:
    if error_text:
        return error_text
    if isinstance(result, dict):
        for key in ("summary", "text", "abstract", "message", "title", "result"):
            if result.get(key):
                return str(result[key])
    return json.dumps(result, ensure_ascii=False, default=str) if result else "-"


def _subtask_json_payload(task: SubtaskORM) -> dict[str, Any]:
    return {
        "object_kind": "skill_execution",
        "object_id": task.id,
        "subtask_id": task.id,
        "skill": task.skill_name,
        "status": task.status,
        "session": task.session_id or "",
        "task_id": getattr(task, "task_id", "") or "",
        "workflow_run_id": getattr(task, "workflow_run_id", "") or "",
        "workflow_step_id": getattr(task, "workflow_step_id", "") or "",
        "parent_event_id": getattr(task, "parent_event_id", "") or "",
        "created_at": format_local_iso(task.created_at),
        "started_at": format_local_iso(task.started_at),
        "finished_at": format_local_iso(task.finished_at),
        "attempt": task.attempt,
        "step_attempt": getattr(task, "step_attempt", 1) or 1,
        "notify_channel": getattr(task, "notify_channel", "") or "",
        "retry_of": getattr(task, "retry_of", "") or "",
        "resume_of": getattr(task, "resume_of", "") or "",
        "original_error": getattr(task, "original_error", "") or "",
        "recovery_attempt": getattr(task, "recovery_attempt", 0) or 0,
        "recovery_policy": getattr(task, "recovery_policy", "") or "",
        "archived_at": format_local_iso(task.archived_at),
        "archived_reason": task.archived_reason or "",
        "error": task.error or "",
        "input_json": task.input_json or {},
        "result_json": task.result_json or {},
        "trace_log": task.trace_log or [],
    }


def _event_json_payload(event: TaskEventORM) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "task_id": event.task_id,
        "seq": event.seq,
        "event_type": event.event_type,
        "status": event.status,
        "lifecycle_status": getattr(event, "lifecycle_status", "") or "",
        "result_success": getattr(event, "result_success", None),
        "name": event.name,
        "tool_name": event.tool_name,
        "skill_name": event.skill_name,
        "workflow_run_id": event.workflow_run_id,
        "workflow_step_id": event.workflow_step_id,
        "subtask_id": event.subtask_id,
        "step_id": event.step_id,
        "input_json": event.input_json or {},
        "output_json": event.output_json or {},
        "error": event.error or "",
        "summary": event.summary or "",
        "pct": event.pct,
        "duration_ms": event.duration_ms,
        "created_at": format_local_iso(event.created_at),
    }


def _workflow_json_payload(
    workflow: WorkflowRunORM,
    steps: Sequence[WorkflowStepORM],
    executions: Sequence[SubtaskORM],
) -> dict[str, Any]:
    execution_by_id = {execution.id: execution for execution in executions}
    return {
        "object_kind": "workflow_run",
        "object_id": workflow.id,
        "workflow_run_id": workflow.id,
        "task_id": workflow.task_id,
        "status": workflow.status,
        "goal": workflow.goal,
        "current_step_id": workflow.current_step_id,
        "attempt": workflow.attempt,
        "plan_json": workflow.plan_json or {},
        "task_contract_json": workflow.task_contract_json or {},
        "workflow_dag_json": workflow.workflow_dag_json or {},
        "result_json": workflow.result_json or {},
        "error": workflow.error or "",
        "created_at": format_local_iso(workflow.created_at),
        "started_at": format_local_iso(workflow.started_at),
        "finished_at": format_local_iso(workflow.finished_at),
        "steps": [
            _workflow_step_json_payload(row, execution_by_id)
            for row in steps
            if row.workflow_run_id == workflow.id
        ],
        "trace_log": workflow.trace_log or [],
    }


def _workflow_step_json_payload(
    step: WorkflowStepORM,
    execution_by_id: dict[str, SubtaskORM] | None = None,
) -> dict[str, Any]:
    executions = execution_by_id or {}
    return {
        "object_kind": "workflow_step",
        "object_id": step.id,
        "workflow_step_id": step.id,
        "workflow_run_id": step.workflow_run_id,
        "task_id": step.task_id,
        "step_id": step.step_key,
        "position": step.position,
        "skill_name": step.skill_name,
        "capability": step.capability,
        "provider_type": step.provider_type,
        "deliverable": step.deliverable,
        "status": step.status,
        "required": step.required,
        "depends_on": step.depends_on or [],
        "optional_depends_on": step.optional_depends_on or [],
        "allow_failed_dependencies": step.allow_failed_dependencies,
        "failure_policy": step.failure_policy,
        "input_json": step.input_json or {},
        "result_json": step.result_json or {},
        "error": step.error or "",
        "warning": step.warning or "",
        "recoverable": step.recoverable,
        "current_execution_id": step.current_execution_id,
        "execution_ids": step.execution_ids or [],
        "child_task_id": step.child_task_id,
        "child_task_ids": step.child_task_ids or [],
        "current_execution": (
            _subtask_json_payload(executions[step.current_execution_id])
            if step.current_execution_id in executions
            else None
        ),
        "created_at": format_local_iso(step.created_at),
        "started_at": format_local_iso(step.started_at),
        "finished_at": format_local_iso(step.finished_at),
    }


def _task_json_payload(
    task: TaskORM,
    events: Sequence[TaskEventORM],
    workflows: Sequence[WorkflowRunORM],
    steps: Sequence[WorkflowStepORM],
    subtasks: Sequence[SubtaskORM],
    child_tasks: Sequence[TaskORM],
    *,
    artifact_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "object_kind": "task",
        "object_id": task.id,
        "task_id": task.id,
        "kind": task.kind,
        "depth": task.depth,
        "attempt": getattr(task, "attempt", 1) or 1,
        "retry_of_task_id": getattr(task, "retry_of_task_id", "") or "",
        "root_task_id": getattr(task, "root_task_id", "") or "",
        "recommended": _task_recommended_action(
            task, resumable=_awaits_a_checkpoint(events)
        ),
        "parent_task_id": task.parent_task_id or "",
        "origin_workflow_run_id": task.origin_workflow_run_id or "",
        "origin_workflow_step_id": task.origin_workflow_step_id or "",
        "status": task.status,
        "session": task.session_id or "",
        "project": task.project,
        "channel": task.channel,
        "title": task.title,
        "user_input": task.user_input,
        "summary": task.summary or "",
        "error": task.error or "",
        "current_stage": task.current_stage,
        "current_tool": task.current_tool,
        "current_workflow_id": task.current_workflow_id,
        "current_subtask_id": task.current_subtask_id,
        "intent_type": getattr(task, "intent_type", "") or "",
        "plan_status": getattr(task, "plan_status", "") or "",
        "provenance_mode": getattr(task, "provenance_mode", "") or "",
        "plan_json": getattr(task, "plan_json", {}) or {},
        "tool_policy_json": getattr(task, "tool_policy_json", {}) or {},
        "submitted_workflow_ids": task.submitted_workflow_ids or [],
        "submitted_subtask_ids": task.submitted_subtask_ids or [],
        "artifact_ids": list(artifact_ids) if artifact_ids is not None else task.artifact_ids or [],
        "source_ids": task.source_ids or [],
        "claim_ids": task.claim_ids or [],
        "evidence_ids": task.evidence_ids or [],
        "created_at": format_local_iso(task.created_at),
        "started_at": format_local_iso(task.started_at),
        "finished_at": format_local_iso(task.finished_at),
        "archived_at": format_local_iso(task.archived_at),
        "archived_reason": task.archived_reason or "",
        "cost": _task_cost_summary(events),
        "events": [_event_json_payload(e) for e in events],
        "workflows": [
            _workflow_json_payload(workflow, steps, subtasks)
            for workflow in workflows
        ],
        "subtasks": [_subtask_json_payload(t) for t in subtasks],
        "child_tasks": [
            {
                "task_id": child.id,
                "kind": child.kind,
                "status": child.status,
                "parent_task_id": child.parent_task_id or "",
                "origin_workflow_run_id": child.origin_workflow_run_id or "",
                "origin_workflow_step_id": child.origin_workflow_step_id or "",
                "title": child.title,
                "summary": child.summary,
                "error": child.error,
            }
            for child in child_tasks
        ],
    }


def _plan_contract_summary(plan_json: dict[str, Any]) -> str:
    selected = plan_json.get("selected_skills") if isinstance(plan_json.get("selected_skills"), list) else []
    parts: list[str] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("skill") or "")
        level = str(item.get("contract_level") or "none")
        if skill:
            parts.append(f"{skill}:{level}")
    return ", ".join(parts) or "-"


def _plan_settlement_summary(plan_json: dict[str, Any]) -> str:
    declared = plan_json.get("verification_plan") if isinstance(plan_json.get("verification_plan"), dict) else {}
    required = declared.get("required_outputs") if isinstance(declared.get("required_outputs"), list) else []
    events = declared.get("required_events") if isinstance(declared.get("required_events"), list) else []
    bits: list[str] = []
    if required:
        bits.append("outputs=" + ",".join(str(x) for x in required[:4]))
    if events:
        bits.append("events=" + ",".join(str(x) for x in events[:4]))
    return "; ".join(bits) or "-"


def _host_remaining_summary(plan_json: dict[str, Any], artifact_rows: Sequence[tuple[str, str, str]] | None) -> str:
    from omni.runtime.remaining import remaining_deliverables

    declared = plan_json.get("verification_plan") if isinstance(plan_json.get("verification_plan"), dict) else {}
    required = list(declared.get("required_outputs") or plan_json.get("outputs") or [])
    artifacts = []
    for kind, title, target in artifact_rows or []:
        artifacts.append(
            SimpleNamespace(kind=kind, title=title, path=target, rel_path=target, uri=target, mime="")
        )
    remaining = remaining_deliverables([str(x) for x in required], artifacts)
    if not remaining:
        return "all named deliverables present" if required else "-"
    return "missing " + ", ".join(remaining)


def _recovery_summary(task: SubtaskORM) -> str:
    attempt = int(getattr(task, "recovery_attempt", 0) or 0)
    policy = str(getattr(task, "recovery_policy", "") or "")
    if not attempt and not policy:
        return "-"
    base = f"attempt={attempt}" if attempt else ""
    return " ".join(part for part in (base, policy) if part)


def _json_preview(value: Any, *, limit: int = 3000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... (truncated; use --json for full data)"


def _list_text(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return ", ".join(str(x) for x in value if x) or "-"
    return str(value)


def _step_provider_label(step: WorkflowStepORM) -> str:
    if step.skill_name:
        return step.skill_name
    if step.provider_type == "child_task":
        return "Child Task"
    if step.provider_type == "native_executor":
        return step.capability or step.deliverable or "Native synthesis"
    return step.provider_type or step.capability or "-"


def _subtask_attachment_context(task: SubtaskORM) -> str:
    """Build concise, actionable context for continuing from a task result."""
    result = task.result_json if isinstance(task.result_json, dict) else {}
    presentation = task_presentation_from_result(
        subtask_id=task.id,
        task_id=task.task_id or "",
        object_kind="skill_execution",
        object_id=task.id,
        skill=task.skill_name,
        status=task.status,
        result=result,
        error=task.error or "",
        trace=task.trace_log if isinstance(task.trace_log, list) else [],
    ).to_plain_text()
    if not presentation.strip():
        presentation = _subtask_summary(task)
    task_input = ""
    if task.input_json:
        task_input = json.dumps(task.input_json, ensure_ascii=False, default=str)[:1200]
    owner = f" (owning Task {task.task_id})" if task.task_id else ""
    lines = [
        f"Skill execution {task.id}{owner} is attached to the current session.",
        "",
        "Continuation rules:",
        "- Attachment adds the task result to context; it does not require direct source-file editing.",
        "- For follow-up questions or explanations, answer from the result below.",
        "- For a small artifact revision, use the controlled revision path and re-render and validate all derived artifacts.",
        "- For a structural revision, use source_task_id and artifact_uri to submit a revision or a new task to the original capability provider.",
        "- Do not search the whole workspace unless an artifact is missing or cannot be read.",
        "",
        presentation,
    ]
    if task_input:
        lines += ["", "Original task input:", task_input]
    return "\n".join(lines).strip()


def _workflow_attachment_context(
    workflow: WorkflowRunORM,
    steps: list[WorkflowStepORM],
) -> str:
    lines = [
        f"Workflow run {workflow.id} is attached to the current session.",
        "",
        "Continuation rules:",
        "- Treat the stable workflow steps below as completed context; do not repeat them unless asked.",
        "- Answer follow-up questions from the recorded step outputs.",
        "- Revise an artifact from its concrete artifact-producing step and skill execution.",
        "- Preserve the original source structure for revisions unless the user explicitly asks to simplify.",
        "",
        f"Goal: {workflow.goal}",
        f"Status: {workflow.status}",
    ]
    summary = _result_summary_text(workflow.result_json, workflow.error)
    if summary:
        lines.append(f"Summary: {summary}")
    if steps:
        lines += ["", "Workflow steps:"]
        for step in steps:
            note = _result_summary_text(step.result_json, step.error or step.warning)
            lines.append(
                f"- {step.step_key}: provider={_step_provider_label(step)}; "
                f"status={step.status}; execution={step.current_execution_id or '-'}; "
                f"result={note or '-'}"
            )
    return "\n".join(lines).strip()


@dataclass(slots=True)
class AttachedResult:
    id: str
    object_kind: str
    task_id: str
    session_id: str
    skill_name: str = ""


async def attach_result_to_session(
    source_agent,
    session_id: str,
    object_id: str,
    *,
    require_same_session: bool = False,
    resolution: TaskObjectResolution | None = None,
    target_agent=None,
) -> AttachedResult | None:  # noqa: ANN001
    """Read a task object from its workspace and attach it to a target session."""
    if resolution is not None and resolution.status != "ok":
        return None
    target_agent = target_agent or source_agent
    resolved_kind = resolution.object_kind if resolution is not None else None
    resolved_id = resolution.object_id if resolution is not None else object_id
    task = (
        await source_agent.tasks.get_task(resolved_id)
        if resolved_kind in {None, "task"}
        else None
    )
    if task is not None:
        if require_same_session and task.session_id != session_id:
            return None
        workflows = await source_agent.runtime.list_workflow_runs(
            task_id=task.id, limit=100
        )
        workflow = workflows[-1] if workflows else None
        if workflow is not None:
            steps = await source_agent.runtime.list_workflow_steps(workflow.id)
            content = _workflow_attachment_context(workflow, steps)
            await target_agent.focus.record_workflow_attachment(
                workflow, steps, session_id=session_id
            )
            skill_name = _step_provider_label(steps[-1]) if steps else ""
        else:
            executions = await source_agent.tasks.list_subtasks_by_ids(
                [str(value) for value in (task.submitted_subtask_ids or []) if value]
            )
            execution = executions[-1] if executions else None
            content = (
                _subtask_attachment_context(execution)
                if execution is not None
                else f"Task {task.id} is attached.\nStatus: {task.status}\nSummary: {task.summary or task.error or '-'}"
            )
            if execution is not None:
                await target_agent.focus.record_skill_execution_attachment(
                    execution, session_id=session_id
                )
            skill_name = execution.skill_name if execution is not None else ""
        attached = AttachedResult(task.id, "task", task.id, task.session_id, skill_name)
    else:
        workflow = (
            await source_agent.runtime.get_workflow_run(resolved_id)
            if resolved_kind in {None, "workflow_run"}
            else None
        )
        if workflow is not None:
            if require_same_session and workflow.session_id != session_id:
                return None
            steps = await source_agent.runtime.list_workflow_steps(workflow.id)
            content = _workflow_attachment_context(workflow, steps)
            await target_agent.focus.record_workflow_attachment(
                workflow, steps, session_id=session_id
            )
            attached = AttachedResult(
                workflow.id,
                "workflow_run",
                workflow.task_id,
                workflow.session_id,
                _step_provider_label(steps[-1]) if steps else "",
            )
        else:
            if resolved_kind == "skill_execution":
                execution = await source_agent.runtime.get_subtask(resolved_id)
                execution_status = "ok" if execution is not None else "not_found"
            elif resolved_kind is None:
                execution, execution_status = await resolve_subtask_strict(
                    source_agent.runtime, resolved_id
                )
            else:
                execution, execution_status = None, "not_found"
            if execution_status == "ambiguous":
                return None
            if execution is not None:
                if require_same_session and execution.session_id != session_id:
                    return None
                content = _subtask_attachment_context(execution)
                await target_agent.focus.record_skill_execution_attachment(
                    execution, session_id=session_id
                )
                attached = AttachedResult(
                    execution.id,
                    "skill_execution",
                    execution.task_id,
                    execution.session_id,
                    execution.skill_name,
                )
            else:
                if resolved_kind == "workflow_step":
                    async with source_agent.db.session() as db_session:
                        step = await db_session.get(WorkflowStepORM, resolved_id)
                    step_workflow = (
                        await source_agent.runtime.get_workflow_run(
                            step.workflow_run_id
                        )
                        if step is not None
                        else None
                    )
                    step_status = (
                        "ok"
                        if step is not None and step_workflow is not None
                        else "not_found"
                    )
                elif resolved_kind is None:
                    step_workflow, step, step_status = await resolve_workflow_step(
                        source_agent.runtime, resolved_id
                    )
                else:
                    step_workflow, step, step_status = None, None, "not_found"
                if step_status != "ok" or step_workflow is None or step is None:
                    return None
                if require_same_session and step_workflow.session_id != session_id:
                    return None
                content = _workflow_attachment_context(step_workflow, [step])
                await target_agent.focus.record_workflow_attachment(
                    step_workflow, [step], session_id=session_id
                )
                attached = AttachedResult(
                    step.id,
                    "workflow_step",
                    step_workflow.task_id,
                    step_workflow.session_id,
                    _step_provider_label(step),
                )
    await target_agent._persist_message(  # noqa: SLF001 - command reuses session persistence.
        session_id,
        "user",
        content,
        kind="task_attachment",
        attached_object_id=attached.id,
        attached_object_kind=attached.object_kind,
        attached_task_id=attached.task_id,
        skill_name=attached.skill_name,
    )
    return attached


def render_tasks_usage_help() -> None:
    """Render task command details for both shell and REPL users."""
    info("Use `/task ...` in the REPL and `omni task ...` in the shell.")
    info(f"Available subcommands: {', '.join(_TASK_SUBCOMMANDS)}.")
    info("Delete task trees in the current workspace; cross-workspace deletion is not implicit.")
    data_table(
        "Task subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "List tasks (user requests) in the current workspace; `/task` is an alias", "/task"],
            ["session", "List tasks for one session; pass an id or prefix in the shell", "/task session"],
            ["all", "List tasks across catalog workspaces (incl. IM channel anchor)", "/task all"],
            ["show <id>", "Show a task, workflow run, workflow step, or skill execution; use --json for full data", "/task show c5b6859f"],
            ["subtask <task>", "Show the skill-execution attempts owned by a task", "/task subtask c5b6859f"],
            ["step <workflow> <step>", "Show stable workflow-step input, output, attempts, and recovery data", "/task step flow1234 diagram"],
            ["watch", "Refresh the task list until q or Ctrl+C", "/task watch"],
            ["attach <id>", "Attach a task result to the current session for follow-up", "/task attach c5b6859f"],
            ["approve <task>", "Execute a validated plan-mode task", "/task approve c5b6859f"],
            ["steer <task> <instruction>", "Adjust a running task at its next execution boundary", "/task steer c5b6859f finish the figure before the summary"],
            ["cancel <task>", "Cancel at the next boundary and preserve partial results", "/task cancel c5b6859f"],
            ["retry <object>", "Run a fresh attempt of a task or execution; use --step for a stable workflow step", "/task retry c5b6859f"],
            ["resume <object>", "Continue where it stopped, keeping finished work; --input answers a waiting task", "/task resume c5b6859f --input am"],
            ["requeue <execution>", "Return one standalone skill execution to the queue, in place", "/task requeue exec1234"],
            ["drain", "Execute pending workflow runs and standalone skill executions now", "omni task drain"],
            ["inbox", "Show completions for this workspace + IM channel anchor", "/inbox or /task inbox"],
            ["archive <id>", "Archive a task while retaining traceability", "/task archive c5b6859f"],
            ["unarchive <id>", "Return an archived task to default listings", "/task unarchive c5b6859f"],
            ["rm/delete <id...>", "Delete task trees in the current workspace; multiple ids preview until --yes", "/task rm c5b6859f d4e5f678 --yes"],
            ["clear", "Delete matching tasks in bulk; requires --yes and --force for succeeded tasks", "/task clear --status failed --yes"],
            ["prune", "Remove failed and stale pending history with full-tree protection", "/task prune --yes"],
        ],
    )
    data_table(
        "Important task options",
        ["option", "commands", "example"],
        [
            ["--session <id>", "list / watch / all", "/task list --session 1a2b3c"],
            ["--all", "list / watch", "/task list --all"],
            ["--status <status>", "list / watch / clear", "/task list --status failed"],
            ["--kind turn|subagent|maintenance|chat|all", "list / watch / all", "/task list --kind chat"],
            ["--limit N", "list / watch", "/task list --limit 50"],
            ["--archived", "list / watch / all", "/task list --archived"],
            ["--json", "show / step", "/task show c5b6859f --json"],
            ["--interval N / --once", "watch", "/task watch --interval 1 --once"],
            ["--before <N>d", "clear", "/task clear --before 30d --yes"],
            ["--force / --yes", "rm / delete / clear / prune", "/task rm c5b6859f d4e5f678 --force --yes"],
        ],
    )
    info("Typical flow: each request creates a task; use `/task watch` for progress, `/task show <task>` for the execution chain, and `/task subtask <task>` for its skill executions.")
    info("Lists show user requests (kind=turn) by default; conversational/inspection answers are filed under `--kind chat`; other system records need `--kind maintenance`, `--kind subagent`, or `--kind all`.")
    info("Workflow recovery: inspect `/task step <workflow-run> <step-id>`, then use `/task retry|resume <workflow-run> --step <step-id>`.")
    info("Recovery verbs take any object id: `retry` starts a new attempt, `resume` continues from a checkpoint, `requeue` re-queues one skill execution unchanged.")
    info("History cleanup: prefer `/task archive <id>` to retain provenance; use `/task prune --yes` for failed and stale tasks; deleting succeeded tasks requires --force.")


@app.command("help")
def help_cmd() -> None:
    """Show task subcommands and common examples (`/task help` in the REPL)."""
    render_tasks_usage_help()


async def resolve_subtask(runtime, subtask_id: str) -> SubtaskORM | None:  # noqa: ANN001
    """Resolve a skill execution by exact id or unique prefix."""
    task = await runtime.get_subtask(subtask_id)
    if task is not None and task.task_id:
        return task
    if not subtask_id:
        return None
    rows = [
        task for task in await runtime.list_subtasks(limit=500, include_archived=True)
        if task.task_id
    ]
    matches = [t for t in rows if t.id.startswith(subtask_id)]
    if len(matches) == 1:
        return matches[0]
    return matches[0] if len(matches) == 1 else None


async def resolve_subtask_strict(runtime, subtask_id: str) -> tuple[SubtaskORM | None, str]:  # noqa: ANN001
    """Resolve a subtask by exact id or unique prefix, without workflow-step fallback."""
    task = await runtime.get_subtask(subtask_id)
    if task is not None and task.task_id:
        return task, "ok"
    if not subtask_id:
        return None, "not_found"
    rows = [
        task for task in await runtime.list_subtasks(limit=1000, include_archived=True)
        if task.task_id
    ]
    matches = [t for t in rows if t.id.startswith(subtask_id)]
    if len(matches) == 1:
        return matches[0], "ok"
    if matches:
        return None, "ambiguous"
    return None, "not_found"


async def resolve_workflow_step(
    runtime,  # noqa: ANN001
    step_id: str,
) -> tuple[WorkflowRunORM | None, WorkflowStepORM | None, str]:
    """Resolve a stable workflow step globally by row/key/execution id."""
    if not step_id:
        return None, None, "not_found"
    runs = await runtime.list_workflow_runs(limit=1000)
    exact: list[tuple[WorkflowRunORM, WorkflowStepORM]] = []
    prefix: list[tuple[WorkflowRunORM, WorkflowStepORM]] = []
    for run in runs:
        for step in await runtime.list_workflow_steps(run.id):
            values = {step.id, step.step_key, step.current_execution_id}
            if step_id in values:
                exact.append((run, step))
            elif any(value and value.startswith(step_id) for value in values):
                prefix.append((run, step))
    matches = exact or prefix
    if len(matches) == 1:
        run, step = matches[0]
        return run, step, "ok"
    if matches:
        return None, None, "ambiguous"
    return None, None, "not_found"


async def resolve_workflow_step_in_task(
    runtime,  # noqa: ANN001
    workflow_or_task_id: str,
    step_id: str,
) -> tuple[WorkflowRunORM | None, WorkflowStepORM | None, str]:
    """Resolve a stable workflow step within one workflow run or owning task."""
    run = await runtime.get_workflow_run(workflow_or_task_id)
    if run is None:
        runs = await runtime.list_workflow_runs(task_id=workflow_or_task_id, limit=100)
        if len(runs) != 1:
            return None, None, "ambiguous" if runs else "not_found"
        run = runs[0]
    rows = await runtime.list_workflow_steps(run.id)
    exact = [
        row
        for row in rows
        if step_id in {row.id, row.step_key, row.current_execution_id}
    ]
    prefix = [
        row
        for row in rows
        if row.id.startswith(step_id)
        or row.step_key.startswith(step_id)
        or bool(row.current_execution_id and row.current_execution_id.startswith(step_id))
    ]
    matches = exact or prefix
    if len(matches) == 1:
        return run, matches[0], "ok"
    if matches:
        return run, None, "ambiguous"
    return run, None, "not_found"


async def subtasks_for_task(agent, task_id: str) -> list[SubtaskORM]:  # noqa: ANN001
    if not hasattr(agent, "tasks") or getattr(agent, "db", None) is None:
        return []
    task = await agent.tasks.get_task(task_id)
    if task is None:
        return []
    async with agent.db.session() as s:
        rows = (
            await s.execute(
                select(SubtaskORM)
                .where(SubtaskORM.task_id == task.id)
                .order_by(SubtaskORM.created_at.asc())
            )
        ).scalars().all()
    return list(rows)


async def task_detail_payload(
    agent,  # noqa: ANN001
    task_id: str,
) -> tuple[
    TaskORM,
    list[TaskEventORM],
    list[WorkflowRunORM],
    list[WorkflowStepORM],
    list[SubtaskORM],
    list[TaskORM],
] | None:
    if not hasattr(agent, "tasks"):
        return None
    task = await agent.tasks.get_task(task_id)
    if task is None:
        return None
    workflows = await agent.runtime.list_workflow_runs(task_id=task.id)
    steps: list[WorkflowStepORM] = []
    for workflow in workflows:
        steps.extend(await agent.runtime.list_workflow_steps(workflow.id))
    return (
        task,
        await agent.tasks.list_events(task.id),
        workflows,
        steps,
        await subtasks_for_task(agent, task.id),
        await agent.tasks.list_child_tasks(task.id),
    )


async def workflow_detail_payload(
    agent,  # noqa: ANN001
    workflow_run_id: str,
) -> tuple[WorkflowRunORM, list[WorkflowStepORM], list[SubtaskORM]] | None:
    workflow = await agent.runtime.get_workflow_run(workflow_run_id)
    if workflow is None:
        return None
    steps = await agent.runtime.list_workflow_steps(workflow.id)
    async with agent.db.session() as session:
        rows = await session.execute(
            select(SubtaskORM)
            .where(SubtaskORM.workflow_run_id == workflow.id)
            .order_by(SubtaskORM.created_at.asc())
        )
        executions = list(rows.scalars().all())
    return workflow, steps, executions


def render_subtask_list(
    paths: OmniPaths,
    rows: Sequence[SubtaskORM],
    *,
    session: str = "",
    status: str = "",
) -> None:
    """Render current-workspace tasks with enough path context to debug windows."""
    filters = " ".join(x for x in (f"status={status}" if status else "", f"session={session}" if session else "") if x)
    info(f"Current workspace: {paths.project_name} · {paths.project_db}")
    if not rows:
        suffix = f" ({filters})" if filters else ""
        info(
            f"No skill executions{suffix}. Find a task with `/task`, then inspect it with "
            "`/task subtask <task>`."
        )
        return
    data_table(
        "Skill executions (current workspace)",
        ["execution_id", "skill", "status", "session", "created"],
        [[_short(r.id), r.skill_name, _status_text(r), _short(r.session_id), _ts(r.created_at)] for r in rows],
    )


def render_task_list(
    paths: OmniPaths,
    rows: Sequence[TaskORM],
    *,
    session: str = "",
    status: str = "",
) -> None:
    """Render task-level (user-request) rows for the current workspace."""
    filters = " ".join(x for x in (f"status={status}" if status else "", f"session={session}" if session else "") if x)
    info(f"Current workspace: {paths.project_name} · {paths.project_db}")
    if not rows:
        suffix = f" ({filters})" if filters else ""
        info(
            f"No tasks{suffix}. A new user request creates a task immediately; "
            "inspect its subtasks with `/task subtask <task>`. Other workspaces "
            "(incl. IM channel anchor): `/task all`."
        )
        return
    task_prefixes = shortest_unique_prefixes([row.id for row in rows])
    data_table(
        "Tasks (current workspace)",
        ["task_id", "status", "source", "current", "session", "created", "title"],
        [
            [
                task_prefixes[r.id],
                _status_text(r),
                r.channel or "-",
                (r.current_subtask_id[:8] if r.current_subtask_id else r.current_tool or r.current_stage or "-"),
                _short(r.session_id),
                _ts(r.created_at),
                (r.title or r.user_input or "")[:60],
            ]
            for r in rows
        ],
    )


def render_all_task_list(
    rows: Sequence[AggTaskRow],
    *,
    limit: int = 30,
    session: str = "",
    status: str = "",
    home=None,  # noqa: ANN001
) -> None:
    """Render global task rows from the workspace catalog."""
    filters = " ".join(x for x in (f"status={status}" if status else "", f"session={session}" if session else "") if x)
    info(
        "Global tasks are read from the workspace catalog "
        f"(registry + on-disk path workspaces + channel anchor + named projects): "
        f"{registry_path(home)}"
    )
    shown = list(rows)[:limit]
    if not shown:
        suffix = f" ({filters})" if filters else ""
        info(
            f"No tasks{suffix}. Catalog covers ~/.omni/workspaces/* stores on disk, "
            "the IM channel anchor, and named projects. A different clone path "
            "keys a different workspace — `omni status` shows the active store."
        )
        return
    task_prefixes = shortest_unique_prefixes([row.id for row in shown])
    data_table(
        "Tasks (all workspaces)",
        ["workspace", "task_id", "status", "created", "title"],
        [[r.workspace[:16], task_prefixes[r.id], _status_text(r), _ts(r.created_at), (r.title or "")[:48]]
         for r in shown],
    )


def render_subtask_json(task: SubtaskORM) -> None:
    """Render the complete task payload as JSON for scripts and debugging."""
    console.print_json(json.dumps(_subtask_json_payload(task), ensure_ascii=False, default=str))


def render_workflow_step_json(
    step: WorkflowStepORM,
    execution: SubtaskORM | None,
) -> None:
    """Render a focused workflow-step payload as JSON."""
    by_id = {execution.id: execution} if execution is not None else {}
    console.print_json(
        json.dumps(_workflow_step_json_payload(step, by_id), ensure_ascii=False, default=str)
    )


def render_workflow_json(
    workflow: WorkflowRunORM,
    steps: Sequence[WorkflowStepORM],
    executions: Sequence[SubtaskORM],
) -> None:
    console.print_json(
        json.dumps(
            _workflow_json_payload(workflow, steps, executions),
            ensure_ascii=False,
            default=str,
        )
    )


def render_task_json(
    task: TaskORM,
    events: Sequence[TaskEventORM],
    workflows: Sequence[WorkflowRunORM],
    steps: Sequence[WorkflowStepORM],
    subtasks: Sequence[SubtaskORM],
    child_tasks: Sequence[TaskORM],
    *,
    artifact_ids: Sequence[str] | None = None,
) -> None:
    """Render the complete task payload as JSON for scripts and debugging."""
    console.print_json(
        json.dumps(
            _task_json_payload(
                task,
                events,
                workflows,
                steps,
                subtasks,
                child_tasks,
                artifact_ids=artifact_ids,
            ),
            ensure_ascii=False,
            default=str,
        )
    )


def _event_command_result(event: TaskEventORM) -> dict[str, Any] | None:
    """Return a structured command result; legacy string outputs stay opaque."""
    output = event.output_json
    if not isinstance(output, dict) or command_result_status(output) is None:
        return None
    return output


def _event_status(event: TaskEventORM) -> str:
    """Show transport lifecycle and command outcome as two distinct statuses."""
    transport = str(event.status or "")
    command = _event_command_result(event)
    if command is None:
        return transport
    return f"{transport} · command={command['command_status']}"


def _event_note(event: TaskEventORM) -> str:
    if event.error:
        return _squash_summary(event.error, 240)
    command = _event_command_result(event)
    summary = str(event.summary or (command or {}).get("summary") or "")
    if command is not None:
        parts: list[str] = []
        exit_code = command.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            parts.append(f"exit={exit_code}")
        output = str(command.get("output") or "").strip()
        if command.get("command_status") == "succeeded" and output:
            parts.append(output)
        elif command.get("command_status") == "failed":
            stderr = str(command.get("stderr") or "")
            hint = ""
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                hint = command_failure_hint(exit_code, output, stderr)
            if not hint and ": " in summary:
                hint = summary.split(": ", 1)[1]
            parts.append(hint or summary or str(command.get("reason") or ""))
        elif summary:
            parts.append(summary)
        elif command.get("reason"):
            parts.append(str(command["reason"]))
        if parts:
            return _squash_summary(" · ".join(parts), 240)
    if summary:
        return _squash_summary(summary, 240)
    if event.subtask_id:
        return event.subtask_id[:8]
    return ""


def _task_cost_summary(events: Sequence[TaskEventORM]) -> dict[str, Any]:
    from omni.agent.cost import summarize_cost_events

    return summarize_cost_events(events)


def _collapsed_events(
    events: Sequence[TaskEventORM],
) -> list[tuple[TaskEventORM, int]]:
    """Fold consecutive duplicate progress records for the human view only."""
    collapsed: list[tuple[TaskEventORM, int]] = []
    for event in events:
        key = (
            event.event_type,
            event.name,
            event.status,
            event.skill_name,
            event.workflow_run_id,
            event.workflow_step_id,
            event.subtask_id,
            event.step_id,
            event.pct,
            event.summary,
            event.error,
        )
        if collapsed:
            previous, count = collapsed[-1]
            previous_key = (
                previous.event_type,
                previous.name,
                previous.status,
                previous.skill_name,
                previous.workflow_run_id,
                previous.workflow_step_id,
                previous.subtask_id,
                previous.step_id,
                previous.pct,
                previous.summary,
                previous.error,
            )
            if key == previous_key:
                collapsed[-1] = (event, count + 1)
                continue
        collapsed.append((event, 1))
    return collapsed


_ACTIVITY_ROWS = 20
# Rows the tail always keeps once a run is over, leaving the rest of the window
# for the records that explain the outcome.
_ACTIVITY_TAIL_ROWS = 12
_UNHAPPY_STATUSES = frozenset(
    {"cancelled", "degraded", "error", "failed", "interrupted", "rejected", "timeout"}
)


def _explains_outcome(event: TaskEventORM) -> bool:
    """Whether this record is a candidate answer to "why did it end like that"."""
    return bool(event.error) or str(event.status or "") in _UNHAPPY_STATUSES


def _activity_window(
    collapsed: list[tuple[TaskEventORM, int]], *, terminal: bool
) -> tuple[list[tuple[TaskEventORM, int]], bool]:
    """Rows worth showing, plus whether they are a contiguous tail.

    A live run is followed for its tail. A finished one is opened to find out
    how it ended, and the record that answers that is often not among the last
    twenty: the ``tool_limit_exceeded`` refusal that shaped run 0792bf0a had
    long scrolled out by the time the task settled, leaving a view full of
    unremarkable ticks. Once a run is over, failures keep their place in the
    window and the uninteresting middle is what gets dropped.
    """
    if len(collapsed) <= _ACTIVITY_ROWS:
        return collapsed, True
    if not terminal:
        return collapsed[-_ACTIVITY_ROWS:], True
    head, tail = collapsed[:-_ACTIVITY_TAIL_ROWS], collapsed[-_ACTIVITY_TAIL_ROWS:]
    explanatory = [pair for pair in head if _explains_outcome(pair[0])]
    kept = explanatory[-(_ACTIVITY_ROWS - len(tail)) :]
    if not kept:
        return collapsed[-_ACTIVITY_ROWS:], True
    return [*kept, *tail], False


def _step_locators(
    steps: Sequence[WorkflowStepORM],
    subtasks: Sequence[SubtaskORM],
) -> dict[str, str]:
    """Map every id an event can carry onto a ``position/total`` plan locator.

    An event points at its step by any of three handles depending on who wrote
    it — the step row id, the plan's own ``step_key``, or the skill execution
    running under it — so all three are indexed to the same locator.
    """
    totals: dict[str, int] = {}
    for step in steps:
        run = step.workflow_run_id or ""
        totals[run] = totals.get(run, 0) + 1
    by_step_id: dict[str, str] = {}
    out: dict[str, str] = {}
    for step in steps:
        total = totals.get(step.workflow_run_id or "", 0)
        locator = f"{step.position}/{total}" if total > 1 else str(step.position)
        if step.id:
            by_step_id[step.id] = locator
            out[step.id] = locator
        if step.step_key:
            out.setdefault(step.step_key, locator)
    for execution in subtasks:
        locator = by_step_id.get(execution.workflow_step_id or "")
        if locator and execution.id:
            out.setdefault(execution.id, locator)
    return out


def _event_step(event: TaskEventORM, locators: dict[str, str]) -> str:
    """Where in the plan a record happened; ``·`` for task-level records.

    The column used to carry ``TaskEventORM.seq``, the per-task event ordinal.
    It is a faithful number and an unhelpful one: it counts *records*, so a
    four-step workflow reported step one at ``#43``, and because the view keeps
    only the last twenty rows the first one visible never started at 1. Codex,
    OpenCode and OpenClaw all decline to surface an event ordinal at all and
    locate work in the plan instead (``✔``/``□`` checklists, ``n/m`` progress).
    The raw ``seq`` is still in ``--json`` for anyone reading the stream itself.
    """
    for key in (event.workflow_step_id, event.step_id, event.subtask_id):
        if key and key in locators:
            return locators[key]
    return "·"


def _awaits_a_checkpoint(events: Sequence[TaskEventORM]) -> bool:
    """Whether a clarification checkpoint on this task is still unanswered.

    A tool that suspends mid-run opens a checkpoint and records it here; a later
    ``resolved``/``expired`` event closes it. Only the newest one decides, so an
    answered question stops advertising a resume that would now be refused.
    """
    for event in reversed(list(events or [])):
        kind = str(getattr(event, "event_type", "") or "")
        if kind.startswith("action.checkpoint."):
            return kind.endswith(".created")
    return False


def _task_recommended_action(task: TaskORM, *, resumable: bool = False) -> str:
    """Suggest the next recovery verb for a task (mirrors the coordinator).

    Every waiting task used to be told to run ``omni task resume <id> --input
    <choice>`` — a flag the command did not have, on a command that resolved
    only skill executions and so answered ``Subtask <id> was not found``. All
    thirty waiting turns here were given it and none was ever answered.

    Only a task that suspended *inside* a tool has a checkpoint to resume, and
    for it the choice is a word like ``am`` that ``--input`` now takes. A
    planner question suspends before any tool runs and leaves nothing to
    resolve, so the way to answer it is to reply in the session.
    """
    status = getattr(task, "status", "") or ""
    short = (getattr(task, "id", "") or "")[:8]
    if status == "needs_input":
        if resumable:
            return f"omni task resume {short} --input <choice>"
        return "answer in session"
    if status == "awaiting_approval":
        return f"omni task approve {short}"
    if status in {"failed", "cancelled", "interrupted", "degraded"}:
        return f"omni task retry {short}"
    return "-"


def render_task_detail(
    task: TaskORM,
    events: Sequence[TaskEventORM],
    workflows: Sequence[WorkflowRunORM],
    steps: Sequence[WorkflowStepORM],
    subtasks: Sequence[SubtaskORM],
    child_tasks: Sequence[TaskORM],
    *,
    artifact_rows: Sequence[tuple[str, str, str]] | None = None,
) -> None:
    """Render one user request and its four explicit execution object types."""
    kv_table(
        f"Task {task.id[:8]}",
        [
            ("object_kind", "task"),
            ("object_id", task.id),
            ("task_id", task.id),
            ("kind", task.kind or "turn"),
            ("depth", task.depth),
            ("attempt", getattr(task, "attempt", 1) or 1),
            ("retry_of", (getattr(task, "retry_of_task_id", "") or "-")[:8]),
            ("root_task", (getattr(task, "root_task_id", "") or "-")[:8]),
            (
                "recommended",
                _task_recommended_action(task, resumable=_awaits_a_checkpoint(events)),
            ),
            ("parent_task", (task.parent_task_id or "-")[:8]),
            ("origin_workflow", (task.origin_workflow_run_id or "-")[:8]),
            ("origin_step", task.origin_workflow_step_id or "-"),
            ("status", task.status),
            ("archived", f"{_ts(task.archived_at)} {task.archived_reason or ''}".strip() if task.archived_at else "-"),
            ("channel", task.channel or "-"),
            ("session", task.session_id or "-"),
            ("created", _ts(task.created_at)),
            ("started", _ts(task.started_at)),
            ("finished", _ts(task.finished_at)),
            (
                "current",
                task.current_workflow_id[:8]
                if task.current_workflow_id
                else task.current_subtask_id[:8]
                if task.current_subtask_id
                else task.current_tool or task.current_stage or "-",
            ),
            ("error", task.error or "-"),
        ],
    )
    cost = _task_cost_summary(events)
    if cost.get("calls") or cost.get("total_tokens"):
        kv_table(
            "cost",
            [
                ("tokens", f"{int(cost.get('total_tokens') or 0):,}"),
                ("prompt", f"{int(cost.get('prompt_tokens') or 0):,}"),
                ("completion", f"{int(cost.get('completion_tokens') or 0):,}"),
                ("estimated_usd", f"${float(cost.get('cost_usd') or 0.0):.4f}"),
                ("calls", str(int(cost.get("calls") or 0))),
                (
                    "components",
                    ", ".join(
                        f"{name}={int(bucket.get('total_tokens') or 0):,}"
                        for name, bucket in (cost.get("components") or {}).items()
                    ) or "-",
                ),
            ],
        )
    console.print(f"\n[bold cyan]user input[/bold cyan]\n{task.user_input or '-'}")
    if task.summary:
        console.print(f"\n[bold cyan]summary[/bold cyan]\n{task.summary}")
    plan_json = getattr(task, "plan_json", {}) or {}
    if isinstance(plan_json, dict) and plan_json:
        selected = plan_json.get("selected_skills") if isinstance(plan_json.get("selected_skills"), list) else []
        skills = ", ".join(str(item.get("skill")) for item in selected if isinstance(item, dict) and item.get("skill"))
        kv_table(
            "plan",
            [
                ("intent", str(plan_json.get("intent_type") or "-")),
                ("confidence", str(plan_json.get("confidence") or "-")),
                ("mode", str(plan_json.get("execution_mode") or "-")),
                ("provenance", str(plan_json.get("provenance_mode") or "-")),
                ("skills", skills or "-"),
                ("contract", _plan_contract_summary(plan_json)),
                ("settlement", _plan_settlement_summary(plan_json)),
                ("remaining", _host_remaining_summary(plan_json, artifact_rows)),
                ("rationale", _squash_summary(str(plan_json.get("rationale") or "-"), 600)),
            ],
        )
    if workflows:
        data_table(
            "workflow runs",
            ["workflow_id", "status", "steps", "current step", "summary/error"],
            [
                [
                    _short(workflow.id),
                    workflow.status,
                    len([step for step in steps if step.workflow_run_id == workflow.id]),
                    workflow.current_step_id or "-",
                    _squash_summary(_result_summary_text(workflow.result_json, workflow.error), 240),
                ]
                for workflow in workflows
            ],
        )
    if steps:
        data_table(
            "workflow steps",
            ["#", "step", "skill/provider", "status", "result"],
            [
                [
                    step.position,
                    step.step_key,
                    _step_provider_label(step),
                    step.status,
                    _squash_summary(_result_summary_text(step.result_json, step.error or step.warning), 240),
                ]
                for step in steps
            ],
        )
    if subtasks:
        data_table(
            "skill executions",
            ["execution_id", "step", "skill", "attempt", "status", "result/error"],
            [
                [
                    _short(execution.id),
                    next(
                        (
                            step.step_key
                            for step in steps
                            if step.id == execution.workflow_step_id
                        ),
                        "-",
                    ),
                    execution.skill_name,
                    execution.step_attempt,
                    _status_text(execution),
                    _squash_summary(_subtask_summary(execution), 240),
                ]
                for execution in subtasks
            ],
        )
    if child_tasks:
        data_table(
            "child tasks",
            ["task_id", "kind", "workflow step", "status", "title", "summary/error"],
            [
                [
                    _short(child.id),
                    child.kind,
                    _short(child.origin_workflow_step_id),
                    child.status,
                    child.title[:80],
                    _squash_summary(child.error or child.summary, 240),
                ]
                for child in child_tasks
            ],
        )
    collapsed = _collapsed_events(events)
    progress_events = [
        pair
        for pair in collapsed
        if pair[0].event_type in {"subtask.progress", "workflow.progress"}
    ]
    if progress_events:
        console.print("\n[bold cyan]recent progress[/bold cyan]")
        for event, count in progress_events[-5:]:
            execution_label = _short(event.subtask_id) if event.subtask_id else "-"
            pct = f" {event.pct:.0%}" if event.pct is not None else ""
            label = event.skill_name or event.name or event.summary or "progress"
            repeat = f" ×{count}" if count > 1 else ""
            step = f" step={event.step_id}" if event.step_id else ""
            console.print(f"  • {execution_label} {label}{step}{pct}{repeat}")
    if collapsed:
        activity, contiguous = _activity_window(
            collapsed, terminal=task.status in _TERMINAL_TASK_STATUSES
        )
        locators = _step_locators(steps, subtasks)
        columns = ["type", "actor", "status", "workflow", "execution", "pct", "note"]
        rows = [
            [
                event.event_type,
                event.skill_name or event.step_id or event.name or event.tool_name,
                _event_status(event),
                _short(event.workflow_run_id) if event.workflow_run_id else "",
                _short(event.subtask_id) if event.subtask_id else "",
                "" if event.pct is None else event.pct,
                _event_note(event) + (f" (×{count})" if count > 1 else ""),
            ]
            for event, count in activity
        ]
        # The column earns its width only when it can tell two positions apart.
        # A single delegated execution has no plan at all (dc787efa); a one-step
        # plan has a plan and still nothing to distinguish, so every located row
        # reads ``1`` and the rest read ``·`` (138c7b6e). Both are the same
        # column saying nothing, so both suppress it.
        if len(set(locators.values())) > 1:
            columns.insert(0, "step")
            for row, (event, _count) in zip(rows, activity, strict=True):
                row.insert(0, _event_step(event, locators))
        data_table("activity", columns, rows, layout="activity")
        if len(collapsed) > len(activity):
            # Without this the table looks complete while silently dropping the
            # beginning of the run — the same gap that made the old ordinal
            # column read as if the task had started at record 43. When the rows
            # are not a contiguous tail, say so: an unexplained jump in the
            # middle of a table is worse than no table.
            scope = (
                f"the last {len(activity)}"
                if contiguous
                else f"{len(activity)} (every failure, plus the last {_ACTIVITY_TAIL_ROWS})"
            )
            console.print(
                f"[{theme.MUTED}]showing {scope} of {len(collapsed)} records[/]"
            )
    rows = list(artifact_rows) if artifact_rows is not None else [
        ("artifact", "", f"artifact://{aid}") for aid in (task.artifact_ids or [])[:12]
    ]
    _print_artifacts(rows)
    # R3: surface the deliverable itself (e.g. the generated paper/report), not
    # just a bare ``artifact://`` id, by inlining a bounded preview of text bodies.
    _render_task_artifact_previews(rows)
    info(f"Full JSON: /task show {task.id[:8]} --json")


# Text-like artifact bodies worth inlining under a task view.
_PREVIEWABLE_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst"})
# Of those, the ones that are documents rather than transcripts. A ``.txt`` or
# ``.rst`` body is quoted as written; markdown is rendered (see ``markdown_body``).
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})


def _render_task_artifact_previews(
    rows: Sequence[tuple[str, str, str]], *, limit: int = 2, max_chars: int = 1400
) -> None:
    """Inline a bounded preview of text/report artifacts (the paper) in a task view."""
    shown = 0
    for title, path, uri in rows:
        if shown >= limit:
            break
        body = _read_text_artifact(path)
        if not body:
            continue
        artifact_preview(
            title or "artifact",
            body[:max_chars].rstrip(),
            markdown=Path(path).suffix.lower() in _MARKDOWN_SUFFIXES,
            hint=(
                f"Preview truncated; open the full artifact with open_artifact {uri or path}"
                if len(body) > max_chars
                else ""
            ),
        )
        shown += 1


def _read_text_artifact(path: str, *, max_bytes: int = 200_000) -> str:
    """Return a text artifact's body, or ``""`` if it is not a small text file."""
    if not path:
        return ""
    p = Path(path)
    if p.suffix.lower() not in _PREVIEWABLE_SUFFIXES or not p.is_file():
        return ""
    try:
        if p.stat().st_size > max_bytes:
            return ""
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _render_artifact_previews(result: dict[str, Any], paths: OmniPaths | None) -> None:
    """Inline small text/report artifact bodies beneath the artifact links."""
    if paths is None:
        return
    from omni.runtime.artifact_preview import inline_text_artifacts
    from omni.runtime.presentation import task_presentation_from_result

    presentation = inline_text_artifacts(
        task_presentation_from_result(subtask_id="", skill="", status="succeeded", result=result),
        paths.artifacts_dir,
    )
    for art in presentation.artifacts:
        if not art.preview:
            continue
        artifact_preview(
            art.title,
            art.preview,
            markdown=art.is_markdown,
            hint=(
                f"Preview truncated; open the full artifact with open_artifact {art.uri or art.target}"
                if art.preview_truncated
                else ""
            ),
        )


def render_subtask_detail(task: SubtaskORM, paths: OmniPaths | None = None) -> None:
    """Render a Skill Execution as a human view; use --json for raw data."""
    deleted = None
    if paths is not None:
        try:
            from omni.skills_runtime.install import deleted_skill_record

            deleted = deleted_skill_record(task.skill_name, paths)
        except Exception:  # noqa: BLE001
            deleted = None
    kv_table(
        f"Skill execution {task.id[:8]}",
        [
            ("object_kind", "skill_execution"),
            ("object_id", task.id),
            ("execution_id", task.id),
            ("skill", task.skill_name),
            ("task_id", getattr(task, "task_id", "") or "-"),
            ("workflow", (getattr(task, "workflow_run_id", "") or "")[:8] or "-"),
            ("workflow step", (getattr(task, "workflow_step_id", "") or "")[:8] or "-"),
            ("skill deleted", f"{deleted.get('action', '')} at {deleted.get('deleted_at', '')}" if deleted else "-"),
            ("status", task.status),
            ("archived", f"{_ts(task.archived_at)} {task.archived_reason or ''}".strip() if task.archived_at else "-"),
            ("session", task.session_id or "-"),
            ("notify", getattr(task, "notify_channel", "") or "-"),
            ("retry_of", (getattr(task, "retry_of", "") or "")[:8] or "-"),
            ("resume_of", (getattr(task, "resume_of", "") or "")[:8] or "-"),
            ("recovery", _recovery_summary(task)),
            ("created", _ts(task.created_at)),
            ("started", _ts(task.started_at)),
            ("finished", _ts(task.finished_at)),
            ("attempt", task.attempt),
            ("step attempt", getattr(task, "step_attempt", 1) or 1),
            ("error", task.error or "-"),
        ],
    )
    if paths is not None:
        delivery = latest_delivery_status(paths.project_dir, task.id)
        if delivery is not None:
            status = str(delivery.get("delivery_status") or "-")
            retry = "yes" if status == "failed" else "-"
            kv_table(
                "Notification delivery",
                [
                    ("channel", delivery.get("channel") or "-"),
                    ("delivery", status),
                    ("retry queued", retry),
                    ("message", delivery.get("message") or "-"),
                    ("time", _ts(delivery.get("created_at", ""))),
                ],
            )
    result = task.result_json if isinstance(task.result_json, dict) else {}
    summary = _subtask_summary(task)
    if summary:
        console.print(f"\n[bold cyan]summary[/bold cyan]\n{summary}")

    artifacts = _result_artifacts(task.result_json)
    if artifacts:
        _print_artifacts(artifacts)
        _render_artifact_previews(result, paths)
    trace = task.trace_log if isinstance(task.trace_log, list) else []
    if trace:
        data_table(
            "recent trace",
            ["stage", "step", "skill", "pct"],
            [
                [
                    event.get("stage", ""),
                    event.get("step_id", ""),
                    event.get("skill", ""),
                    event.get("pct", ""),
                ]
                for event in trace[-8:]
                if isinstance(event, dict)
            ],
        )
    info(f"Full JSON: /task show {task.id[:8]} --json")
    if task.task_id:
        info(f"Full task: /task show {task.task_id[:8]}")


def render_workflow_detail(
    workflow: WorkflowRunORM,
    steps: Sequence[WorkflowStepORM],
    executions: Sequence[SubtaskORM],
) -> None:
    execution_by_id = {execution.id: execution for execution in executions}
    kv_table(
        f"Workflow {workflow.id[:8]}",
        [
            ("object_kind", "workflow_run"),
            ("object_id", workflow.id),
            ("workflow_run_id", workflow.id),
            ("task_id", workflow.task_id),
            ("status", workflow.status),
            ("current step", workflow.current_step_id or "-"),
            ("attempt", workflow.attempt),
            ("created", _ts(workflow.created_at)),
            ("started", _ts(workflow.started_at)),
            ("finished", _ts(workflow.finished_at)),
            ("error", workflow.error or "-"),
        ],
    )
    console.print(f"\n[bold cyan]goal[/bold cyan]\n{workflow.goal or '-'}")
    if workflow.result_json:
        console.print(
            f"\n[bold cyan]summary[/bold cyan]\n"
            f"{_result_summary_text(workflow.result_json, workflow.error)}"
        )
    if steps:
        data_table(
            "workflow steps",
            ["#", "step", "skill/provider", "status", "result"],
            [
                [
                    step.position,
                    step.step_key,
                    _step_provider_label(step),
                    step.status,
                    _squash_summary(_result_summary_text(step.result_json, step.error or step.warning), 240),
                ]
                for step in steps
            ],
        )
    attempts = [
        execution_by_id[execution_id]
        for step in steps
        for execution_id in step.execution_ids or []
        if execution_id in execution_by_id
    ]
    if attempts:
        data_table(
            "skill executions",
            ["execution_id", "step", "skill", "attempt", "status", "result/error"],
            [
                [
                    _short(execution.id),
                    next(
                        (step.step_key for step in steps if step.id == execution.workflow_step_id),
                        "-",
                    ),
                    execution.skill_name,
                    execution.step_attempt,
                    execution.status,
                    _squash_summary(_subtask_summary(execution), 240),
                ]
                for execution in attempts
            ],
        )
    info(f"Full JSON: /task show {workflow.id[:8]} --json")
    if workflow.task_id:
        info(f"Full task: /task show {workflow.task_id[:8]}")


def render_workflow_step_detail(
    workflow: WorkflowRunORM,
    step: WorkflowStepORM,
    execution: SubtaskORM | None,
) -> None:
    """Render one stable workflow step and its current skill attempt."""
    result = step.result_json if isinstance(step.result_json, dict) else {}
    kv_table(
        f"Workflow step {step.step_key}",
        [
            ("object_kind", "workflow_step"),
            ("object_id", step.id),
            ("workflow", workflow.id),
            ("task_id", workflow.task_id),
            ("workflow_step_id", step.id),
            ("step_id", step.step_key),
            ("skill/provider", _step_provider_label(step)),
            ("status", step.status),
            ("required", step.required),
            ("depends_on", _list_text(step.depends_on)),
            ("optional_depends_on", _list_text(step.optional_depends_on)),
            ("failure_policy", step.failure_policy or "-"),
            ("recoverable", step.recoverable),
            ("current execution", step.current_execution_id or "-"),
            ("execution attempts", len(step.execution_ids or [])),
            ("child task", step.child_task_id or "-"),
            ("child task attempts", len(step.child_task_ids or [])),
            ("error", step.error or "-"),
        ],
    )
    note = _result_summary_text(result, step.error or step.warning)
    if note:
        console.print(f"\n[bold cyan]summary[/bold cyan]\n{note}")
    if step.input_json:
        console.print(f"\n[bold cyan]input[/bold cyan]\n{_json_preview(step.input_json)}")
    if result:
        console.print(f"\n[bold cyan]result[/bold cyan]\n{_json_preview(result)}")
    if execution is not None:
        kv_table(
            "current skill execution",
            [
                ("execution_id", execution.id),
                ("skill", execution.skill_name),
                ("attempt", execution.step_attempt),
                ("status", execution.status),
                ("retry_of", execution.retry_of or "-"),
                ("resume_of", execution.resume_of or "-"),
                ("error", execution.error or "-"),
            ],
        )
    artifacts = _result_artifacts(result)
    _print_artifacts(artifacts)
    trace = [
        event
        for event in (workflow.trace_log if isinstance(workflow.trace_log, list) else [])
        if isinstance(event, dict) and str(event.get("step_id") or "") == step.step_key
    ]
    if trace:
        data_table(
            "step trace",
            ["stage", "step", "skill", "pct"],
            [
                [
                    event.get("stage", ""),
                    event.get("step_id", ""),
                    event.get("skill", ""),
                    event.get("pct", ""),
                ]
                for event in trace[-12:]
            ],
        )
    actions = [
        f"Retry this step: /task retry {workflow.id[:8]} --step {step.step_key}",
        f"resume in place: /task resume {workflow.id[:8]} --step {step.step_key}",
        f"full workflow: /task show {workflow.id[:8]}",
    ]
    if workflow.task_id:
        actions.append(f"full task: /task show {workflow.task_id[:8]}")
    info("; ".join(actions))


_TASK_KINDS = ("turn", "subagent", "maintenance", "chat")


def _normalize_kind(kind: str) -> str | None:
    """Validate ``--kind`` and map it to a storage filter (None = no filter)."""
    value = (kind or "turn").strip().lower()
    if value == "all":
        return None
    if value not in _TASK_KINDS:
        error(f"--kind expects one of: {', '.join(_TASK_KINDS)}, all; received: {kind}")
        raise typer.Exit(1)
    return value


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    status: str = typer.Option("", help="Filter by status"),
    kind: str = typer.Option("turn", "--kind", "-k", help="Task kind: turn (default), subagent, maintenance, chat, or all"),
    limit: int = typer.Option(30, help="Maximum rows to show"),
    show_all: bool = typer.Option(False, "--all", "-a", help="Show all workspaces (default: current workspace)"),
    session: str = typer.Option("", "--session", "-s", help="Filter by session id or prefix"),
    archived: bool = typer.Option(False, "--archived", help="Include archived tasks"),
) -> None:
    """List tasks (user requests) in the current workspace or across workspaces."""
    state: AppState = ctx.obj
    kind_filter = _normalize_kind(kind)

    if show_all:
        rows = run_async(list_tasks_all_workspaces(
            limit_per=limit,
            status=status or None,
            include_archived=archived,
            kind=kind_filter,
        ))
        rows = [r for r in rows if _session_matches(r.session_id, session)]
        render_all_task_list(rows, limit=limit, session=session, status=status, home=state.settings().paths.home)
        return

    async def _run():
        agent = await make_agent(state)
        try:
            rows = await agent.tasks.list_tasks(
                limit=limit,
                status=status or None,
                include_archived=archived,
                kind=kind_filter,
            )
            return agent.paths, rows
        finally:
            await agent.aclose()

    paths, rows = run_async(_run())
    rows = [r for r in rows if _session_matches(r.session_id, session)]
    render_task_list(paths, rows, session=session, status=status)


@app.command("session")
def session_cmd(
    ctx: typer.Context,
    session: str = typer.Argument("", help="Session id or prefix; `/task session` defaults to the current REPL session"),
    status: str = typer.Option("", help="Filter by status"),
    kind: str = typer.Option("turn", "--kind", "-k", help="Task kind: turn (default), subagent, maintenance, chat, or all"),
    limit: int = typer.Option(30, help="Maximum rows to show"),
    archived: bool = typer.Option(False, "--archived", help="Include archived tasks"),
) -> None:
    """List tasks submitted by one session; the REPL defaults to its current session."""
    if not session:
        error("Specify a session id in the shell: omni task session <session-id>. In the REPL, use `/task session`.")
        raise typer.Exit(1)
    list_cmd(ctx, status=status, kind=kind, limit=limit, show_all=False, session=session, archived=archived)


@app.command("all")
def all_cmd(
    ctx: typer.Context,
    status: str = typer.Option("", help="Filter by status"),
    kind: str = typer.Option("turn", "--kind", "-k", help="Task kind: turn (default), subagent, maintenance, chat, or all"),
    limit: int = typer.Option(30, help="Maximum rows per workspace"),
    session: str = typer.Option("", "--session", "-s", help="Filter by session id or prefix"),
    archived: bool = typer.Option(False, "--archived", help="Include archived tasks"),
) -> None:
    """List tasks across all catalog workspaces (registry ∪ channel anchor ∪ named projects)."""
    list_cmd(ctx, status=status, kind=kind, limit=limit, show_all=True, session=session, archived=archived)


@app.command("show")
def show_cmd(
    ctx: typer.Context,
    object_id: str = typer.Argument(
        ..., help="Task, workflow, workflow-step, or skill-execution id/prefix"
    ),
    view: bool = typer.Option(False, "--view", help="Show the human-readable view (default)"),
    json_output: bool = typer.Option(False, "--json", help="Output complete JSON"),
) -> None:
    """Show task details, input, errors, results, and artifacts."""
    state: AppState = ctx.obj

    async def _run():
        agent, resolution, _ = await make_agent_for_object(state, object_id)
        try:
            if resolution.status == "ambiguous":
                return resolution.status, None, None, agent.paths
            if resolution.status == "not_found":
                step_task, step, step_status = await resolve_workflow_step(
                    agent.runtime, object_id
                )
                if step_status == "ambiguous":
                    return "ambiguous", None, None, agent.paths
                if step_task is None or step is None:
                    return "missing", None, None, agent.paths
                execution = (
                    await agent.runtime.get_subtask(step.current_execution_id)
                    if step.current_execution_id
                    else None
                )
                return "step", (step_task, step, execution), None, agent.paths
            if not resolution.task_id:
                return "missing", None, None, agent.paths
            resolved_id = resolution.object_id
            if resolution.object_kind == "task":
                task_payload = await task_detail_payload(agent, resolved_id)
                if task_payload is None:
                    return "missing", None, None, agent.paths
                task, _events, _workflows, steps, subtasks, _children = task_payload
                rows = await _resolve_task_artifacts(
                    task_id=task.id,
                    subtasks=subtasks,
                    steps=steps,
                    db=agent.db,
                    paths=agent.paths,
                )
                owned_ids = list(
                    dict.fromkeys(
                        uri.removeprefix("artifact://")
                        for _title, _path, uri in rows
                        if uri.startswith("artifact://")
                    )
                )
                return "task", task_payload, (rows, owned_ids), agent.paths
            if resolution.object_kind == "workflow_run":
                workflow_payload = await workflow_detail_payload(agent, resolved_id)
                if workflow_payload is None:
                    return "missing", None, None, agent.paths
                return "workflow", workflow_payload, None, agent.paths
            if resolution.object_kind == "skill_execution":
                sub = await agent.runtime.get_subtask(resolved_id)
                if sub is None:
                    return "missing", None, None, agent.paths
                return "subtask", sub, None, agent.paths
            if resolution.object_kind == "workflow_step":
                async with agent.db.session() as db_session:
                    step = await db_session.get(WorkflowStepORM, resolved_id)
                if step is None:
                    return "missing", None, None, agent.paths
                step_task = await agent.runtime.get_workflow_run(step.workflow_run_id)
                if step_task is None:
                    return "missing", None, None, agent.paths
                execution = (
                    await agent.runtime.get_subtask(step.current_execution_id)
                    if step.current_execution_id
                    else None
                )
                return "step", (step_task, step, execution), None, agent.paths
            return "missing", None, None, agent.paths
        finally:
            await agent.aclose()

    kind, payload, artifact_data, owner_paths = run_async(_run())
    artifact_rows, owned_artifact_ids = artifact_data or (None, None)
    if kind == "ambiguous":
        error(
            f"Task object prefix {object_id} is ambiguous across tasks, workflows, "
            "workflow steps, or skill executions; provide a longer or full id."
        )
        raise typer.Exit(1)
    if not payload:
        error(f"Task object {object_id} was not found")
        raise typer.Exit(1)
    if json_output:
        if kind == "task":
            task, events, workflows, steps, subtasks, child_tasks = payload
            render_task_json(
                task,
                events,
                workflows,
                steps,
                subtasks,
                child_tasks,
                artifact_ids=owned_artifact_ids,
            )
        elif kind == "workflow":
            workflow, steps, executions = payload
            render_workflow_json(workflow, steps, executions)
        elif kind == "step":
            _, step, execution = payload
            render_workflow_step_json(step, execution)
        else:
            render_subtask_json(payload)
    else:
        _ = view
        if kind == "task":
            task, events, workflows, steps, subtasks, child_tasks = payload
            render_task_detail(
                task, events, workflows, steps, subtasks, child_tasks,
                artifact_rows=artifact_rows,
            )
        elif kind == "workflow":
            workflow, steps, executions = payload
            render_workflow_detail(workflow, steps, executions)
        elif kind == "step":
            workflow, step, execution = payload
            render_workflow_step_detail(workflow, step, execution)
        else:
            render_subtask_detail(payload, owner_paths)


@app.command("step")
def step_cmd(
    ctx: typer.Context,
    workflow_run_id: str = typer.Argument(..., help="Workflow run id or unique prefix"),
    step_id: str = typer.Argument(..., help="Stable step id, execution id, or unique prefix"),
    json_output: bool = typer.Option(False, "--json", help="Output complete JSON"),
) -> None:
    """Show a workflow step's input, output, and recovery entry points."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            resolved = await _resolve_workflow_for_recovery(agent, workflow_run_id)
            if resolved is None:
                return None, None, None, "not_found"
            workflow, step, status = await resolve_workflow_step_in_task(
                agent.runtime, resolved.id, step_id
            )
            execution = (
                await agent.runtime.get_subtask(step.current_execution_id)
                if step is not None and step.current_execution_id
                else None
            )
            return workflow, step, execution, status
        finally:
            await agent.aclose()

    workflow, step, execution, status = run_async(_run())
    if status == "ambiguous":
        error(f"{workflow_run_id}/{step_id} matches multiple records; provide longer ids.")
        raise typer.Exit(1)
    if workflow is None:
        error(f"Workflow run {workflow_run_id} was not found")
        raise typer.Exit(1)
    if step is None:
        error(f"Workflow step {step_id} was not found in workflow {workflow.id[:8]}")
        raise typer.Exit(1)
    if json_output:
        render_workflow_step_json(step, execution)
    else:
        render_workflow_step_detail(workflow, step, execution)


@app.command("subtask")
def subtasks_cmd(
    ctx: typer.Context,
    task_id: str = typer.Argument("", help="Task id or unique prefix; omit to list recent subtasks"),
    status: str = typer.Option("", help="Filter by subtask status"),
    limit: int = typer.Option(30, help="Maximum rows to show"),
    archived: bool = typer.Option(False, "--archived", help="Include archived subtasks"),
) -> None:
    """Show the subtasks (skill executions) of a task, or recent subtasks when no task is given."""
    state: AppState = ctx.obj

    async def _run():
        agent, _ = await make_agent_for_task(state, task_id)
        try:
            if task_id:
                run = await agent.tasks.get_task(task_id)
                if run is None:
                    return agent.paths, []
                rows = await subtasks_for_task(agent, run.id)
                if status:
                    rows = [r for r in rows if r.status == status]
                if not archived:
                    rows = [r for r in rows if getattr(r, "archived_at", None) is None]
                return agent.paths, rows[:limit]
            rows = await agent.runtime.list_subtasks(
                limit=limit,
                status=status or None,
                include_archived=archived,
            )
            rows = [r for r in rows if r.task_id]
            return agent.paths, rows[:limit]
        finally:
            await agent.aclose()

    paths, rows = run_async(_run())
    render_subtask_list(paths, rows, status=status)


# A single-task watch has nothing left to follow once the task settles, so it
# returns on its own; paused states (needs_input / awaiting_approval) may still
# resume, so they keep refreshing.
_TERMINAL_TASK_STATUSES = frozenset(
    {"succeeded", "degraded", "failed", "cancelled", "interrupted"}
)


def _watch_single_task(state: AppState, task_id: str, *, interval: float, once: bool) -> None:
    """Follow one task's detail until it settles or the user presses q / Ctrl+C.

    Resolves the owning workspace through the global task index (so a task from
    another workspace still follows) and re-renders the same view as ``task show``
    each tick. Auto-returns once the task reaches a terminal status.
    """

    async def _detail():  # noqa: ANN202 - local payload shuttle
        agent, _ = await make_agent_for_task(state, task_id)
        try:
            payload = await task_detail_payload(agent, task_id)
            if payload is None:
                return None
            task, _events, _workflows, steps, subtasks, _children = payload
            rows = await _resolve_task_artifacts(
                task_id=task.id,
                subtasks=subtasks,
                steps=steps,
                db=agent.db,
                paths=agent.paths,
            )
            return payload, rows
        finally:
            await agent.aclose()

    try:
        with WatchKeyListener() as keys:
            while True:
                detail = run_async(_detail())
                if detail is None:
                    error(f"Task {task_id} was not found")
                    raise typer.Exit(1)
                payload, artifact_rows = detail
                task, events, workflows, steps, subtasks, child_tasks = payload
                if not once:
                    console.clear()
                render_task_detail(
                    task, events, workflows, steps, subtasks, child_tasks,
                    artifact_rows=artifact_rows,
                )
                if once:
                    return
                if task.status in _TERMINAL_TASK_STATUSES:
                    info(f"Task {task.id[:8]} is {task.status}; nothing more to watch.")
                    return
                info("Watching: press q to return to the CLI, or Ctrl+C to stop.")
                if keys.wait(max(0.5, interval)):
                    info("Stopped watching.")
                    return
    except KeyboardInterrupt:
        info("Stopped watching.")


@app.command("watch")
def watch_cmd(
    ctx: typer.Context,
    task_id: str = typer.Argument(
        "", help="Task id/prefix to follow live; omit to watch the task list"
    ),
    status: str = typer.Option("", help="Filter by status"),
    kind: str = typer.Option("turn", "--kind", "-k", help="Task kind: turn (default), subagent, maintenance, chat, or all"),
    limit: int = typer.Option(20, help="Maximum rows to show"),
    show_all: bool = typer.Option(False, "--all", "-a", help="Show all workspaces"),
    session: str = typer.Option("", "--session", "-s", help="Filter by session id or prefix"),
    archived: bool = typer.Option(False, "--archived", help="Include archived tasks"),
    interval: float = typer.Option(2.0, "--interval", "-i", help="Refresh interval in seconds"),
    once: bool = typer.Option(False, "--once", help="Refresh once for scripts or tests"),
) -> None:
    """Follow a single task's live detail (with a task id) or the task list.

    With ``<task-id>`` this follows one task — its status, subtasks, and workflow
    steps — resolving across workspaces via the global task index, and returns on
    its own once the task settles. Without an id it refreshes the task list
    (optionally ``--all`` workspaces). Quit with q or Ctrl+C.
    """
    state: AppState = ctx.obj
    kind_filter = _normalize_kind(kind)
    if task_id:
        _watch_single_task(state, task_id, interval=interval, once=once)
        return
    try:
        with WatchKeyListener() as keys:
            while True:
                if not once:
                    console.clear()
                if show_all:
                    rows = run_async(list_tasks_all_workspaces(
                        limit_per=limit,
                        status=status or None,
                        include_archived=archived,
                        kind=kind_filter,
                    ))
                    rows = [r for r in rows if _session_matches(r.session_id, session)]
                    render_all_task_list(
                        rows,
                        limit=limit,
                        session=session,
                        status=status,
                        home=state.settings().paths.home,
                    )
                else:
                    async def _run():
                        agent = await make_agent(state)
                        try:
                            rows = await agent.tasks.list_tasks(
                                limit=limit,
                                status=status or None,
                                include_archived=archived,
                                kind=kind_filter,
                            )
                            return agent.paths, rows
                        finally:
                            await agent.aclose()

                    paths, rows = run_async(_run())
                    rows = [r for r in rows if _session_matches(r.session_id, session)]
                    render_task_list(paths, rows, session=session, status=status)
                if once:
                    return
                info("Watching: press q to return to the CLI, or Ctrl+C to stop.")
                if keys.wait(max(0.5, interval)):
                    info("Stopped watching.")
                    return
    except KeyboardInterrupt:
        info("Stopped watching.")


@app.command("attach")
def attach_cmd(
    ctx: typer.Context,
    object_id: str = typer.Argument(..., help="Task, workflow, step, or skill-execution id/prefix"),
    session: str = typer.Option("", "--session", "-s", help="Target session id or prefix; required in the shell"),
) -> None:
    """Attach a finished result to a session for follow-up questions."""
    if not session:
        error("Specify a target session: omni task attach <id> --session <session-id>")
        raise typer.Exit(1)
    state: AppState = ctx.obj

    async def _run():
        source_agent, resolution, remote = await make_agent_for_object(
            state, object_id
        )
        target_agent = source_agent
        try:
            if remote:
                target_agent = await make_agent(state)
            if resolution.status != "ok":
                return resolution.status, None
            if not resolution.task_id:
                return "not_found", None
            sess = await target_agent.get_session(session)
            if sess is None:
                return "no_session", None
            attached = await attach_result_to_session(
                source_agent,
                sess.id,
                resolution.object_id,
                resolution=resolution,
                target_agent=target_agent,
            )
            return ("ok", attached) if attached else ("not_found", None)
        finally:
            if target_agent is not source_agent:
                await target_agent.aclose()
            await source_agent.aclose()

    status, attached = run_async(_run())
    if status == "ambiguous":
        error(
            f"Task object prefix {object_id} is ambiguous across tasks, workflows, "
            "workflow steps, or skill executions; provide a longer or full id."
        )
        raise typer.Exit(1)
    if status == "no_session":
        error(f"Session {session} was not found")
        raise typer.Exit(1)
    if status == "not_found" or attached is None:
        error(f"Task result {object_id} was not found or was ambiguous")
        raise typer.Exit(1)
    success(
        f"Attached {attached.object_kind} {attached.id[:8]} to session {session[:8]}."
    )


@app.command("approve")
def approve_cmd(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Awaiting-approval task id or unique prefix"),
) -> None:
    """Approve and execute a saved plan-mode task, preserving its task id."""
    state: AppState = ctx.obj

    async def _run():
        from omni.runtime.daemon import is_daemon_running

        agent, _ = await make_agent_for_task(state, task_id)
        try:
            turn = await agent.approve_task(
                task_id,
                drain_tasks=not is_daemon_running(agent.paths),
            )
            return turn
        finally:
            await agent.aclose()

    try:
        turn = run_async(_run())
    except (LookupError, ValueError) as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    success(f"Approved and executed task {turn.task_id[:8]}.")
    if turn.text:
        console.print(turn.text)


@app.command("steer")
def steer_cmd(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Running task id or unique prefix"),
    instruction: list[str] = typer.Argument(..., help="Instruction to apply at the next execution boundary"),
) -> None:
    """Send a persistent steering instruction to a running task."""
    state: AppState = ctx.obj
    text = " ".join(instruction).strip()

    async def _run():
        agent, _ = await make_agent_for_task(state, task_id)
        try:
            row = await agent.tasks.get_task(task_id)
            if row is None:
                raise LookupError(f"task not found: {task_id}")
            rejection = await agent.tasks.steer_rejection_reason(row.id)
            if rejection:
                raise ValueError(rejection)
            return await agent.tasks.request_control(row.id, action="steer", instruction=text)
        finally:
            await agent.aclose()

    try:
        control = run_async(_run())
    except (LookupError, ValueError) as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    success(f"Submitted a steering instruction to task {task_id[:8]} (control {control.id[:8]}).")


@app.command("cancel")
def cancel_cmd(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Running task id or unique prefix"),
) -> None:
    """Request cancellation at the next boundary while preserving completed results."""
    state: AppState = ctx.obj

    async def _run():
        agent, _ = await make_agent_for_task(state, task_id)
        try:
            row = await agent.tasks.get_task(task_id)
            if row is None:
                raise LookupError(f"task not found: {task_id}")
            control = await agent.tasks.request_control(row.id, action="cancel")
            await agent.runtime.reconcile_lost_executors(
                task_id=row.id, explicit=True
            )
            status = await agent.tasks.control_status(control.id)
            return control, status
        finally:
            await agent.aclose()

    try:
        control, status = run_async(_run())
    except (LookupError, ValueError) as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    if status == "applied":
        warn(
            f"Cancelled task {task_id[:8]} (control {control.id[:8]}); "
            "the owning executor was already gone."
        )
        return
    warn(f"Requested cancellation of task {task_id[:8]} (control {control.id[:8]}).")


def _report_recovery(outcome: RecoveryOutcome, *, object_id: str) -> None:
    """Render one coordinator outcome, or exit non-zero explaining why not.

    Every verb reports through here so the same state reads the same way
    whichever one the user reached for, and so an id the resolver could not
    place is described as the id it is rather than as a missing subtask.
    """
    if outcome.ok:
        success(outcome.message or f"Recovered {object_id}.")
        return
    if outcome.status == "ambiguous":
        error(
            outcome.message
            or f"Prefix {object_id} matches multiple objects; provide a longer id."
        )
        raise typer.Exit(1)
    if outcome.status == "not_found":
        error(
            outcome.message
            or (
                f"No task, workflow run, workflow step, or skill execution "
                f"matched {object_id}."
            )
        )
        raise typer.Exit(1)
    # A resolved object in the wrong state is not a failure of the command; say
    # what the state is and which verb fits it.
    report = warn if outcome.status == "wrong_state" else error
    report(outcome.message or f"Could not recover {object_id}.")
    if outcome.suggested_command:
        info(f"Try: {outcome.suggested_command}")
    if outcome.status != "wrong_state":
        raise typer.Exit(1)


async def _recover(
    state: AppState, object_id: str, verb: str, **kwargs: Any
) -> RecoveryOutcome:
    """Resolve ``object_id`` to its owning workspace and dispatch one verb."""
    from omni.runtime.task_recovery import TaskRecoveryCoordinator

    agent, resolution, _remote = await make_agent_for_object(state, object_id)
    try:
        coordinator = TaskRecoveryCoordinator(agent)
        method = getattr(coordinator, verb)
        return await method(resolution, object_id=object_id, **kwargs)
    finally:
        await agent.aclose()


@app.command("retry")
def retry_cmd(
    ctx: typer.Context,
    object_id: str = typer.Argument(
        ..., help="Task, workflow run, workflow step, or skill execution id/prefix"
    ),
    notify: str = typer.Option("", "--notify", help="Override the notification channel"),
    step: str = typer.Option("", "--step", help="Retry this workflow step and its downstream steps"),
) -> None:
    """Run a fresh attempt of a task or execution from its original input."""
    state: AppState = ctx.obj
    outcome = run_async(
        _recover(state, object_id, "retry", notify_channel=notify, step=step)
    )
    _report_recovery(outcome, object_id=object_id)


@app.command("resume")
def resume_cmd(
    ctx: typer.Context,
    object_id: str = typer.Argument(
        ..., help="Task, workflow run, workflow step, or skill execution id/prefix"
    ),
    step: str = typer.Option("", "--step", help="Resume the workflow in place from this step"),
    input_choice: str = typer.Option(
        "", "--input", help="Answer a waiting task's clarification with this choice"
    ),
) -> None:
    """Continue an object from where it stopped, keeping the work already done."""
    state: AppState = ctx.obj
    outcome = run_async(
        _recover(state, object_id, "resume", step=step, input_choice=input_choice)
    )
    _report_recovery(outcome, object_id=object_id)


@app.command("requeue")
def requeue_cmd(
    ctx: typer.Context,
    object_id: str = typer.Argument(..., help="Skill execution id/prefix"),
) -> None:
    """Return one standalone skill execution to the queue, in place."""
    state: AppState = ctx.obj
    outcome = run_async(_recover(state, object_id, "requeue"))
    _report_recovery(outcome, object_id=object_id)


async def _resolve_workflow_for_recovery(agent, value: str) -> WorkflowRunORM | None:  # noqa: ANN001
    workflow = await agent.runtime.get_workflow_run(value)
    if workflow is not None:
        return workflow
    execution, status = await resolve_subtask_strict(agent.runtime, value)
    if execution is not None and status == "ok" and execution.workflow_run_id:
        return await agent.runtime.get_workflow_run(execution.workflow_run_id)
    task = await agent.tasks.get_task(value)
    if task is None:
        return None
    workflows = await agent.runtime.list_workflow_runs(task_id=task.id)
    return workflows[0] if len(workflows) == 1 else None


def _parse_before_days(value: str) -> int | None:
    """Parse ``--before`` values like ``30`` / ``30d`` → days (None = invalid)."""
    v = value.strip().lower().removesuffix("d")
    if not v.isdigit():
        return None
    return int(v)


async def _resolve_task_for_mutation(agent, task_id: str) -> tuple[TaskORM | None, str]:  # noqa: ANN001
    """Resolve a task (user request) for destructive ops: exact id or unique prefix."""
    if not hasattr(agent, "tasks") or not task_id:
        return None, "not_found"
    task = await agent.tasks.get_task(task_id)
    return (task, "ok") if task is not None else (None, "not_found")


@dataclass
class _TaskReferenceResolution:
    """Exact Task ids resolved from user-supplied ids or unique prefixes."""

    task_ids: list[str]
    missing: list[str]
    ambiguous: dict[str, list[str]]


async def _resolve_task_references(agent, references: Sequence[str]) -> _TaskReferenceResolution:  # noqa: ANN001
    """Resolve a batch in one workspace without letting one bad prefix delete anything."""
    refs = list(dict.fromkeys(str(ref).strip() for ref in references if str(ref).strip()))
    async with agent.db.session() as session:
        rows = list((await session.execute(select(TaskORM))).scalars().all())
    by_id = {row.id: row for row in rows}
    resolved: list[str] = []
    missing: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    for ref in refs:
        if ref in by_id:
            matches = [ref]
        else:
            matches = [task_id for task_id in by_id if task_id.startswith(ref)]
        if len(matches) == 1:
            if matches[0] not in resolved:
                resolved.append(matches[0])
        elif not matches:
            missing.append(ref)
        else:
            ambiguous[ref] = sorted(matches)
    return _TaskReferenceResolution(
        task_ids=resolved,
        missing=missing,
        ambiguous=ambiguous,
    )


def _task_barrier_details(
    tasks: dict[str, str],
    prefixes: dict[str, str],
) -> str:
    return ", ".join(
        f"{prefixes.get(task_id, task_id)} ({status})"
        for task_id, status in tasks.items()
    )


def _execution_barrier_details(
    barriers: Sequence[Any],
    prefixes: dict[str, str],
) -> str:
    return ", ".join(
        f"{prefixes.get(item.task_id, item.task_id)} owns "
        f"{item.object_kind} {item.object_id} ({item.status})"
        for item in barriers
    )


def _clear_preview_text(outcome) -> str:  # noqa: ANN001
    """Explain a clear/prune outcome per status so ``0 deletable`` is never opaque."""
    parts: list[str] = [f"{outcome.deleted_total} deletable"]
    if outcome.protected:
        detail = ", ".join(f"{n} {s}" for s, n in sorted(outcome.protected.items()))
        parts.append(f"{outcome.protected_total} protected ({detail}; --force to include)")
    if outcome.blocked:
        detail = ", ".join(f"{n} {s}" for s, n in sorted(outcome.blocked.items()))
        parts.append(f"{outcome.blocked_total} active ({detail}; wait or reconcile first)")
    if outcome.retained:
        detail = ", ".join(f"{n} {s}" for s, n in sorted(outcome.retained.items()))
        parts.append(f"{outcome.retained_total} retained ({detail})")
    if outcome.concurrent_write:
        parts.append("workspace changed concurrently; nothing was deleted")
    return "; ".join(parts)


@app.command("archive")
def archive_cmd(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Task id or unique prefix"),
    reason: str = typer.Option("", "--reason", "-r", help="Optional archive reason"),
) -> None:
    """Archive a task while retaining show, attach, and artifact traceability."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            task, status = await _resolve_task_for_mutation(agent, task_id)
            if task is None or status != "ok":
                return status, None
            if task.status in _TASK_BLOCKED_STATUSES:
                return "active", task
            ok = await agent.tasks.archive_task(task.id, reason=reason)
            return ("ok" if ok else "not_found"), task
        finally:
            await agent.aclose()

    status, task = run_async(_run())
    if status == "active" and task is not None:
        warn(f"Task {task.id[:8]} is {task.status} and cannot be archived; wait for completion or stop the worker.")
        raise typer.Exit(1)
    if status == "not_found" or task is None:
        error(f"Task {task_id} was not found in the current workspace.")
        raise typer.Exit(1)
    success(f"Archived task {task.id[:8]}; it remains available through `/task show {task.id[:8]}`.")


@app.command("unarchive")
def unarchive_cmd(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Task id or unique prefix"),
) -> None:
    """Return an archived task to default task listings."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            task, status = await _resolve_task_for_mutation(agent, task_id)
            if task is None or status != "ok":
                return status, None
            ok = await agent.tasks.unarchive_task(task.id)
            return ("ok" if ok else "not_found"), task
        finally:
            await agent.aclose()

    status, task = run_async(_run())
    if status == "not_found" or task is None:
        error(f"Task {task_id} was not found in the current workspace.")
        raise typer.Exit(1)
    success(f"Returned task {task.id[:8]} to default listings.")


@app.command("rm")
def rm_cmd(
    ctx: typer.Context,
    task_ids: list[str] = typer.Argument(..., help="One or more Task ids or unique prefixes"),
    force: bool = typer.Option(False, "--force", "-f", help="Also delete protected completed tasks; active tasks are never deleted"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm a multi-Task deletion; otherwise preview it"),
) -> None:
    """Delete Task trees; multiple ids preview first and require --yes."""
    if any(not str(task_id).strip() for task_id in task_ids):
        error("Task ids cannot be empty; provide an exact id or unique prefix.")
        raise typer.Exit(1)
    state: AppState = ctx.obj
    batch_requested = len(task_ids) > 1

    async def _run(dry_run: bool):
        agent = await make_agent(state)
        try:
            resolution = await _resolve_task_references(agent, task_ids)
            if resolution.missing or resolution.ambiguous:
                return resolution, None
            outcome = await agent.tasks.delete_tasks(
                resolution.task_ids,
                force=force,
                dry_run=dry_run,
            )
            return resolution, outcome
        finally:
            await agent.aclose()

    resolution, outcome = run_async(_run(batch_requested and not yes))
    if resolution.missing:
        error(
            "Task reference(s) not found in the current workspace: "
            + ", ".join(resolution.missing)
        )
        raise typer.Exit(1)
    if resolution.ambiguous:
        detail = "; ".join(
            f"{ref} matches {', '.join(matches)}"
            for ref, matches in resolution.ambiguous.items()
        )
        error(f"Ambiguous Task reference(s): {detail}. Use a longer prefix.")
        raise typer.Exit(1)
    if outcome is None:
        error("No Task ids were provided.")
        raise typer.Exit(1)
    prefixes = shortest_unique_prefixes(outcome.known_task_ids)
    if outcome.concurrent_write:
        warn(
            "Task deletion could not reserve the current workspace because another "
            "writer is active; nothing was deleted. Retry after it settles."
        )
        raise typer.Exit(1)
    if outcome.missing_ids:
        error("Task(s) disappeared before deletion: " + ", ".join(outcome.missing_ids))
        raise typer.Exit(1)
    if outcome.blocked_tasks or outcome.blocked_executions:
        details = [
            _task_barrier_details(outcome.blocked_tasks, prefixes)
            if outcome.blocked_tasks else "",
            _execution_barrier_details(outcome.blocked_executions, prefixes)
            if outcome.blocked_executions else "",
        ]
        warn(
            "Active work blocks deletion: "
            f"{'; '.join(detail for detail in details if detail)}; wait for it to settle."
        )
        raise typer.Exit(1)
    if outcome.protected_tasks:
        warn(
            "Task deletion includes protected history: "
            f"{_task_barrier_details(outcome.protected_tasks, prefixes)}; "
            "add --force to confirm. "
            "Artifact records and files are preserved."
        )
        raise typer.Exit(1)
    if outcome.retained_tasks:
        warn(
            "Task deletion was retained by a tree-integrity boundary: "
            f"{_task_barrier_details(outcome.retained_tasks, prefixes)}; nothing was deleted."
        )
        raise typer.Exit(1)
    if batch_requested and not yes:
        data_table(
            "Task deletion preview",
            ["task_id", "status", "title"],
            [
                [prefixes.get(task.id, task.id), task.status, one_line(task.title, 80)]
                for task in outcome.deleted_tasks
            ],
        )
        info(
            f"Would delete {outcome.deleted_total} tasks across "
            f"{len(resolution.task_ids)} selected Task reference(s). Add --yes to confirm."
        )
        return
    if len(resolution.task_ids) == 1:
        selected_id = resolution.task_ids[0]
        task = next(item for item in outcome.deleted_tasks if item.id == selected_id)
        descendants = max(0, outcome.deleted_total - 1)
        descendant_text = (
            f" and {descendants} descendant Task(s)" if descendants else ""
        )
        success(
            f"Deleted task {prefixes.get(task.id, task.id)} ({task.status}) and its subtasks"
            f"{descendant_text}."
        )
        return
    success(
        f"Deleted {outcome.deleted_total} tasks across "
        f"{len(resolution.task_ids)} selected Task reference(s)."
    )


@app.command("delete")
def delete_cmd(
    ctx: typer.Context,
    task_ids: list[str] = typer.Argument(..., help="One or more Task ids or unique prefixes"),
    force: bool = typer.Option(False, "--force", "-f", help="Also delete protected completed tasks; active tasks are never deleted"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm a multi-Task deletion; otherwise preview it"),
) -> None:
    """Alias for ``task rm``."""
    rm_cmd(ctx, task_ids, force=force, yes=yes)


@app.command("clear")
def clear_cmd(
    ctx: typer.Context,
    status: str = typer.Option("", "--status", help="Delete matching statuses, such as failed or cancelled"),
    before: str = typer.Option("", "--before", help="Delete tasks older than N days, such as 30 or 30d"),
    kind: str = typer.Option("turn", "--kind", "-k", help="Task kind: turn (default), subagent, maintenance, chat, or all"),
    clear_all: bool = typer.Option(False, "--all", help="Do not filter by status or age; protections still apply"),
    include_archived: bool = typer.Option(False, "--include-archived", help="Also delete already-archived tasks"),
    force: bool = typer.Option(False, "--force", "-f", help="Also delete succeeded/provenance tasks; active tasks are never deleted"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm deletion; otherwise show a preview"),
) -> None:
    """Delete tasks in bulk (cascading to subtasks), protecting active and provenance tasks by default."""
    state: AppState = ctx.obj
    kind_filter = _normalize_kind(kind)
    if not (status or before or clear_all):
        error("Specify at least one filter: --status <s>, --before <N>d, or --all.")
        raise typer.Exit(1)
    cutoff = None
    if before:
        days = _parse_before_days(before)
        if days is None:
            error(f"--before expects a number of days such as 30 or 30d; received: {before}")
            raise typer.Exit(1)
        from datetime import UTC, datetime, timedelta

        cutoff = datetime.now(UTC) - timedelta(days=days)
    if status in _TASK_BLOCKED_STATUSES:
        warn(f"{status} tasks are held by workers and cannot be deleted in bulk; stop the service or wait for completion.")
        raise typer.Exit(1)

    async def _run(dry: bool):
        agent = await make_agent(state)
        try:
            return await agent.tasks.clear_tasks(
                status=status or None, before=cutoff, kind=kind_filter,
                include_archived=include_archived, force=force, dry_run=dry,
            )
        finally:
            await agent.aclose()

    if not yes:
        outcome = run_async(_run(True))
        info(f"Would delete {_clear_preview_text(outcome)}. Add --yes to confirm.")
        raise typer.Exit(0)
    outcome = run_async(_run(False))
    if outcome.concurrent_write:
        warn(
            "Task cleanup could not reserve the current workspace because another "
            "writer is active; nothing was deleted. Retry after it settles."
        )
        raise typer.Exit(1)
    if outcome.deleted_total == 0:
        warn(f"Deleted 0 tasks — {_clear_preview_text(outcome)}.")
        return
    success(f"Deleted {outcome.deleted_total} tasks (and their subtasks); {_clear_preview_text(outcome)}.")


@app.command("prune")
def prune_cmd(
    ctx: typer.Context,
    stale_days: int = typer.Option(7, "--stale-days", help="Also remove pending subtasks older than N days"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm deletion; otherwise show a preview"),
) -> None:
    """Remove failed/cancelled/interrupted tasks and stale pending subtasks; keep succeeded and active work."""
    state: AppState = ctx.obj
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(days=max(0, stale_days))

    async def _run(dry: bool):
        agent = await make_agent(state)
        try:
            outcome = await agent.tasks.clear_tasks(
                kind=None, prunable_only=True, dry_run=dry,
            )
            if outcome.concurrent_write:
                return outcome, 0
            stale = await agent.runtime.clear_subtasks(
                status="pending", before=cutoff, protect=("running", "succeeded"), dry_run=dry,
            )
            return outcome, stale
        finally:
            await agent.aclose()

    if not yes:
        outcome, stale = run_async(_run(True))
        info(
            f"Would remove {outcome.deleted_total} failed/cancelled/interrupted tasks "
            f"and {stale} pending subtasks older than {stale_days} days. Add --yes to confirm."
        )
        raise typer.Exit(1)
    outcome, stale = run_async(_run(False))
    if outcome.concurrent_write:
        warn(
            "Task pruning could not reserve the current workspace because another "
            "writer is active; nothing was deleted. Retry after it settles."
        )
        raise typer.Exit(1)
    success(
        f"Removed {outcome.deleted_total} failed/cancelled/interrupted tasks (with subtasks) "
        f"and {stale} stale pending subtasks; succeeded and active work was preserved."
    )


@app.command("drain")
def drain_cmd(ctx: typer.Context) -> None:
    """Execute all pending subtasks immediately (no daemon needed)."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        processed = await agent.runtime.drain()
        await agent.aclose()
        return processed

    processed = run_async(_run())
    success(f"Processed {len(processed)} subtasks") if processed else info("No executable subtasks")


@app.command("inbox")
def inbox_cmd(ctx: typer.Context) -> None:
    """Show task-completion notifications for this workspace and the IM channel anchor."""
    state: AppState = ctx.obj
    paths = state.settings().paths
    notes = collect_inbox_notes(paths)
    # R2: re-resolve each note's status from the settled task row so the inbox
    # never disagrees with ``/task show`` / ``/schedule``. A delivery-time
    # snapshot can be stale; the durable row is the single source of truth.
    # Status lookup routes via TaskIndex so IM-anchor task ids resolve correctly.
    statuses = run_async(_resolve_inbox_statuses(state, notes[-30:]))
    render_inbox(paths, notes=notes, statuses=statuses)


async def _resolve_inbox_statuses(
    state: AppState, notes: Sequence[dict[str, Any]]
) -> dict[str, str]:
    """Map each note's canonical Task id → authoritative status (best-effort).

    Groups ids by owning workspace (TaskIndex / catalog) so a WeChat completion
    noted on the channel anchor is not looked up only in the CWD store.
    """
    from omni.runtime.task_index import resolve_task_workspace

    ids = {
        str(
            note.get("task_id")
            or note.get("object_id")
            or note.get("subtask_id")
            or ""
        )
        for note in notes
    }
    ids.discard("")
    if not ids:
        return {}
    resolved: dict[str, str] = {}
    try:
        local_settings = state.settings()
    except Exception:  # noqa: BLE001 — inbox must render even without settings
        return {}

    by_dir: dict[str, tuple[Any, list[str]]] = {}
    for ref in ids:
        target = None
        try:
            target = await resolve_task_workspace(local_settings, ref)
        except Exception:  # noqa: BLE001 — routing is best-effort
            target = None
        settings = target or local_settings
        key = str(settings.paths.project_dir)
        bucket = by_dir.get(key)
        if bucket is None:
            by_dir[key] = (settings, [ref])
        else:
            bucket[1].append(ref)

    for settings, refs in by_dir.values():
        try:
            agent = await make_agent_from_settings(settings)
        except Exception:  # noqa: BLE001
            continue
        try:
            for ref in refs:
                try:
                    task = await agent.tasks.get_task(ref)
                except Exception:  # noqa: BLE001
                    task = None
                status = resolve_task_status(task)
                if status:
                    resolved[ref] = status
        finally:
            await agent.aclose()
    return resolved


def render_inbox(
    paths: OmniPaths,
    *,
    limit: int = 30,
    notes: Sequence[dict[str, Any]] | None = None,
    statuses: dict[str, str] | None = None,
) -> None:
    """Render recent task notifications with channel delivery outcome.

    When ``notes`` is omitted, merges this workspace's inbox with the IM channel
    anchor's (tagged ``workspace``). Delivery status is read from each note's
    owning ``_project_dir`` when present.
    """
    if notes is None:
        notes = collect_inbox_notes(paths)
    if not notes:
        warn(
            "No task notifications. Completions from this workspace and the IM "
            "channel anchor (e.g. default) appear here; use `/task all` for live tasks."
        )
        return
    statuses = statuses or {}
    rows = []
    show_workspace = any(str(n.get("workspace") or "") for n in notes[-limit:])
    for note in notes[-limit:]:
        subtask_id = str(note.get("subtask_id") or "")
        ref = str(note.get("object_id") or subtask_id or "")
        task_id = str(note.get("task_id") or "")
        object_kind = str(note.get("object_kind") or "skill_execution")
        object_label = {
            "workflow_run": "workflow",
            "workflow_step": "step",
            "skill_execution": "execution",
        }.get(object_kind, object_kind)
        note_project = Path(str(note.get("_project_dir") or paths.project_dir))
        delivery = latest_delivery_status(note_project, ref) if ref else None
        delivery_status = "-"
        if delivery is not None:
            delivery_status = str(delivery.get("delivery_status") or "-")
            if delivery_status == "failed":
                delivery_status += " (retry queued)"
        elif note.get("channel"):
            delivery_status = str(note.get("channel"))
        status = statuses.get(task_id) or statuses.get(ref) or note.get("status", "")
        row = [
            _ts(note.get("created_at", "")),
            task_id[:8] or "-",
            f"{object_label}:{ref[:8]}" if ref else object_label,
            note.get("skill_name", ""),
            status,
            delivery_status,
            _squash_summary(note.get("summary", "") or note.get("title", ""), 200),
        ]
        if show_workspace:
            row.insert(1, str(note.get("workspace") or "-")[:16])
        rows.append(row)
    headers = ["time", "task", "object", "skill", "status", "delivery", "summary"]
    if show_workspace:
        headers.insert(1, "workspace")
    data_table("Notification inbox", headers, rows)
