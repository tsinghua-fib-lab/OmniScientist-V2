"""Safe command routing for IM channels.

IM channels should expose Omni product actions, not a remote shell. This module
implements a small allowlist of command handlers that can run from Feishu,
WeChat, or DingTalk after channel pairing/allowlist authorization.
"""

from __future__ import annotations

import shlex
from datetime import datetime
from typing import Any

from omni.cli.commands.tasks_cmd import (
    _collapsed_events,
    attach_result_to_session,
    resolve_subtask,
    resolve_workflow_step,
    subtasks_for_task,
    task_detail_payload,
)
from omni.core.identifiers import short_id, shortest_unique_prefixes
from omni.runtime.notifications import InboxNotifier
from omni.runtime.presentation import (
    TurnPresentation,
    task_presentation_from_result,
    turn_presentation_from_result,
)
from omni.runtime.task_object_resolver import TaskObjectResolution, resolve_task_object
from omni.storage.models import SubtaskORM


async def handle_channel_command(agent: Any, text: str, session_id: str) -> TurnPresentation | None:
    """Handle an inbound IM command, or return ``None`` for normal agent chat."""
    command = _normalize_command_text(text)
    if command is None:
        return None
    if command == "/task" or command.startswith("/task "):
        return await _handle_tasks(agent, command.removeprefix("/task").strip(), session_id)
    if command == "/stop":
        return await _handle_active_stop(agent, session_id)
    if command == "/steer" or command.startswith("/steer "):
        return await _handle_active_steer(
            agent,
            command.removeprefix("/steer").strip(),
            session_id,
        )
    if command == "/inbox" or command.startswith("/inbox "):
        return _handle_inbox(agent, session_id)
    if command == "/verify" or command.startswith("/verify "):
        return await _handle_verify(agent, command.removeprefix("/verify").strip(), session_id)
    if command == "/help" or command.startswith("/help "):
        return _help()
    if command.startswith("/"):
        return TurnPresentation(
            assistant_text=(
                f"IM channels do not execute `{command.split()[0]}`.\n\n"
                "Available safe commands: `/stop`, `/steer <instruction>`, `/task`, "
                "`/task show <id>`, `/task watch`, "
                "`/task attach <id>`、`/task retry <id>`、`/task resume <id>`、"
                "`/task approve <task>`、`/task cancel <task>`、`/task steer <task> <instruction>`、"
                "`/plan <request>`、`/verify --session`、`/inbox`。\n"
                "Run shell, configuration, and sensitive operations from the local CLI."
            ),
            session_id=session_id,
        )
    return None


async def _handle_active_stop(agent: Any, session_id: str) -> TurnPresentation:
    task = await agent.tasks.active_task_for_session(session_id)
    if task is None:
        return TurnPresentation(
            assistant_text="No active task is running in this conversation.",
            session_id=session_id,
        )
    return await _tasks_cancel(agent, task.id, session_id)


async def _handle_active_steer(
    agent: Any,
    instruction: str,
    session_id: str,
) -> TurnPresentation:
    task = await agent.tasks.active_task_for_session(session_id)
    if task is None:
        return TurnPresentation(
            assistant_text="No active task is running in this conversation.",
            session_id=session_id,
        )
    return await _tasks_steer(agent, task.id, instruction, session_id)


def _normalize_command_text(text: str) -> str | None:
    """Map a plain IM message to a safe command, or ``None`` for normal chat.

    Natural-language messages are never rewritten. This parser only recognizes
    slash commands, leaving language interpretation to the semantic planner.
    """
    value = text.strip()
    if not value:
        return None
    if value.startswith("/"):
        return value
    return None


async def _handle_tasks(agent: Any, arg: str, session_id: str) -> TurnPresentation:
    parsed = _parse_task_args(arg, session_id)
    if parsed["help"]:
        return _tasks_help(session_id)
    if parsed["action"] == "show":
        return await _tasks_show(agent, str(parsed["subtask_id"]), session_id)
    if parsed["action"] == "subtask":
        return await _tasks_subtasks(agent, str(parsed["subtask_id"]), session_id)
    if parsed["action"] == "attach":
        return await _tasks_attach(agent, str(parsed["subtask_id"]), session_id)
    if parsed["action"] == "retry":
        return await _tasks_retry(agent, str(parsed["subtask_id"]), session_id)
    if parsed["action"] == "resume":
        return await _tasks_resume(agent, str(parsed["subtask_id"]), session_id)
    if parsed["action"] == "approve":
        return await _tasks_approve(agent, str(parsed["subtask_id"]), session_id)
    if parsed["action"] == "cancel":
        return await _tasks_cancel(agent, str(parsed["subtask_id"]), session_id)
    if parsed["action"] == "steer":
        return await _tasks_steer(
            agent,
            str(parsed["subtask_id"]),
            str(parsed["instruction"]),
            session_id,
        )
    return await _tasks_list(
        agent,
        session_id=session_id,
        action=str(parsed["action"]),
        status=str(parsed["status"]),
        limit=int(parsed["limit"]),
        session=str(parsed["session"]),
    )


async def _channel_object_resolution(
    agent: Any,
    object_id: str,
) -> TaskObjectResolution | None:
    """Resolve production channel commands with the shared global ID rules.

    Lightweight test/channel adapters without settings retain the local
    compatibility path below; a real agent always resolves ambiguity before
    touching any object namespace.
    """
    settings = getattr(agent, "settings", None)
    if settings is None:
        return None
    try:
        return await resolve_task_object(settings, object_id)
    except Exception:  # noqa: BLE001 - command lookup must fail closed
        return TaskObjectResolution(status="ambiguous")


def _channel_resolution_is_local(
    agent: Any,
    resolution: TaskObjectResolution,
) -> bool:
    owner_paths = resolution.settings.paths if resolution.settings is not None else None
    agent_paths = getattr(agent, "paths", None)
    if owner_paths is None or agent_paths is None:
        return False
    return owner_paths.project_dir == agent_paths.project_dir


def _ambiguous_object(object_id: str, session_id: str) -> TurnPresentation:
    return TurnPresentation(
        assistant_text=(
            f"Task object prefix `{object_id}` is ambiguous across tasks, workflows, "
            "workflow steps, or skill executions. Provide a longer or full ID."
        ),
        session_id=session_id,
    )


def _object_not_found(object_id: str, session_id: str) -> TurnPresentation:
    return TurnPresentation(
        assistant_text=(
            f"Task object `{object_id}` was not found. Use `/task` or `/task watch` "
            "to inspect recent tasks."
        ),
        session_id=session_id,
    )


async def _tasks_show(agent: Any, subtask_id: str, session_id: str) -> TurnPresentation:
    if not subtask_id:
        return TurnPresentation(assistant_text="Usage: `/task show <task-id>`", session_id=session_id)
    resolution = await _channel_object_resolution(agent, subtask_id)
    if resolution is not None and resolution.status == "ambiguous":
        return _ambiguous_object(subtask_id, session_id)
    if resolution is not None and resolution.status == "ok":
        if not _channel_resolution_is_local(agent, resolution):
            return _not_owned(subtask_id, session_id)
        if not resolution.task_id:
            return _object_not_found(subtask_id, session_id)
        object_kind = resolution.object_kind
        resolved_id = resolution.object_id
    else:
        object_kind = None
        resolved_id = subtask_id

    task_payload = (
        await task_detail_payload(agent, resolved_id)
        if object_kind in {None, "task"}
        else None
    )
    if task_payload is not None:
        task, events, workflows, steps, executions, child_tasks = task_payload
        if task.session_id != session_id:
            return _not_owned(subtask_id, session_id)
        lines = [
            f"## Task `{task.id[:8]}` ({task.status})",
            "",
            "Object kind: `task`",
            f"Object ID: `{task.id}`",
            f"Task ID: `{task.id}`",
            f"User input: {task.user_input[:500] or '-'}",
            f"Current stage: {task.current_subtask_id[:8] if task.current_subtask_id else task.current_tool or task.current_stage or '-'}",
        ]
        if workflows:
            lines += ["", "### Workflows", "", "| workflow | status | goal |", "|---|---|---|"]
            lines.extend(
                f"| `{workflow.id[:8]}` | {workflow.status} | {workflow.goal[:120] or '-'} |"
                for workflow in workflows[:8]
            )
        if steps:
            lines += [
                "",
                "### Workflow steps",
                "",
                "| step | provider | execution | status | result |",
                "|---|---|---|---|---|",
            ]
            for step in steps[:12]:
                provider = step.skill_name or step.capability or step.deliverable or step.provider_type
                result = step.error or step.warning or str((step.result_json or {}).get("summary") or "-")
                lines.append(
                    f"| `{step.step_key}` | {provider} | "
                    f"`{(step.current_execution_id or '')[:8] or '-'}` | {step.status} | {result[:120]} |"
                )
        if executions:
            step_by_id = {step.id: step.step_key for step in steps}
            lines += [
                "",
                "### Skill executions",
                "",
                "| execution | step | skill | attempt | status |",
                "|---|---|---|---|---|",
            ]
            lines.extend(
                f"| `{execution.id[:8]}` | {step_by_id.get(execution.workflow_step_id or '', '-')} | "
                f"{execution.skill_name} | {execution.step_attempt} | {execution.status} |"
                for execution in executions[:12]
            )
        if child_tasks:
            lines += ["", "### Child tasks", "", "| task | kind | status | title |", "|---|---|---|---|"]
            lines.extend(
                f"| `{child.id[:8]}` | {child.kind} | {child.status} | {child.title[:120] or '-'} |"
                for child in child_tasks[:8]
            )
        if events:
            lines += ["", "Recent activity:"]
            for event, count in _collapsed_events(events)[-8:]:
                execution_ref = f" execution `{event.subtask_id[:8]}`" if event.subtask_id else ""
                repeat = f" (x{count})" if count > 1 else ""
                note = event.summary or event.error or ""
                label = event.skill_name or event.step_id or event.name
                lines.append(
                    f"- {event.event_type} · {label}{execution_ref} · "
                    f"{event.status} {note[:120]}{repeat}"
                )
        lines.append(f"\nFull data: `/task show {task.id[:8]} --json` (local CLI)")
        return TurnPresentation(assistant_text="\n".join(lines), session_id=session_id)
    task = (
        await agent.runtime.get_subtask(resolved_id)
        if object_kind == "skill_execution"
        else await resolve_subtask(agent.runtime, resolved_id)
        if object_kind is None
        else None
    )
    if task is not None:
        if task.session_id != session_id:
            return _not_owned(subtask_id, session_id)
        return TurnPresentation(
            assistant_text="",
            session_id=session_id,
            tasks=[_presentation_for_task(task)],
        )
    workflow = (
        await agent.runtime.get_workflow_run(resolved_id)
        if object_kind in {None, "workflow_run"}
        else None
    )
    if workflow is not None:
        if workflow.session_id != session_id:
            return _not_owned(subtask_id, session_id)
        steps = await agent.runtime.list_workflow_steps(workflow.id)
        lines = [
            f"## Workflow `{workflow.id[:8]}` ({workflow.status})",
            "",
            "Object kind: `workflow_run`",
            f"Object ID: `{workflow.id}`",
            f"Task ID: `{workflow.task_id}`",
            f"Parent task: `{workflow.task_id[:8]}`",
        ]
        if workflow.task_id:
            lines.append(f"Full task: `/task show {workflow.task_id[:8]}`")
        lines.extend([
            f"Goal: {workflow.goal[:500] or '-'}",
            "",
            "| step | provider | execution | status | result |",
            "|---|---|---|---|---|",
        ])
        for step in steps[:16]:
            provider = step.skill_name or step.capability or step.deliverable or step.provider_type
            result = step.error or step.warning or str((step.result_json or {}).get("summary") or "-")
            lines.append(
                f"| `{step.step_key}` | {provider} | `{(step.current_execution_id or '')[:8] or '-'}` | "
                f"{step.status} | {result[:120]} |"
            )
        lines.append(f"\nFull data: `/task show {workflow.id[:8]} --json` (local CLI)")
        return TurnPresentation(assistant_text="\n".join(lines), session_id=session_id)
    step_workflow, step, step_status = (
        await resolve_workflow_step(agent.runtime, resolved_id)
        if object_kind in {None, "workflow_step"}
        else (None, None, "not_found")
    )
    if step_status == "ok" and step_workflow is not None and step is not None:
        if step_workflow.session_id != session_id:
            return _not_owned(subtask_id, session_id)
        provider = step.skill_name or step.capability or step.deliverable or step.provider_type
        result = step.error or step.warning or str((step.result_json or {}).get("summary") or "-")
        lines = [
            f"## Workflow step `{step.step_key}` ({step.status})",
            "",
            "Object kind: `workflow_step`",
            f"Object ID: `{step.id}`",
            f"Task ID: `{step_workflow.task_id}`",
        ]
        if step_workflow.task_id:
            lines.append(f"Full task: `/task show {step_workflow.task_id[:8]}`")
        lines.extend([
            f"Workflow: `{step_workflow.id[:8]}`",
            f"Provider: {provider}",
            f"Current execution: `{(step.current_execution_id or '')[:8] or '-'}`",
            f"Result: {result[:1000]}",
        ])
        return TurnPresentation(
            assistant_text="\n".join(lines),
            session_id=session_id,
        )
    if step_status == "ambiguous":
        return _ambiguous_object(subtask_id, session_id)
    return _object_not_found(subtask_id, session_id)


async def _tasks_attach(agent: Any, object_id: str, session_id: str) -> TurnPresentation:
    if not object_id:
        return TurnPresentation(assistant_text="Usage: `/task attach <id>`", session_id=session_id)
    resolution = await _channel_object_resolution(agent, object_id)
    if resolution is not None and resolution.status == "ambiguous":
        return _ambiguous_object(object_id, session_id)
    if resolution is not None and resolution.status == "ok":
        if (
            not resolution.task_id
            or not _channel_resolution_is_local(agent, resolution)
        ):
            return _not_owned(object_id, session_id)
    attached = await attach_result_to_session(
        agent,
        session_id,
        resolution.object_id
        if resolution is not None and resolution.status == "ok"
        else object_id,
        require_same_session=True,
        resolution=(
            resolution
            if resolution is not None and resolution.status == "ok"
            else None
        ),
    )
    if attached is None:
        return TurnPresentation(
            assistant_text=f"Task result `{object_id}` was not found in this session or was ambiguous.",
            session_id=session_id,
        )
    return TurnPresentation(
        assistant_text=(
            f"Attached {attached.object_kind} `{attached.id[:8]}` to this IM session. "
            "You can continue the discussion or request an artifact revision."
        ),
        session_id=session_id,
    )


async def _tasks_retry(agent: Any, subtask_id: str, session_id: str) -> TurnPresentation:
    if not subtask_id:
        return TurnPresentation(assistant_text="Usage: `/task retry <subtask-id>`", session_id=session_id)
    task = await resolve_subtask(agent.runtime, subtask_id)
    if task is None:
        return TurnPresentation(assistant_text=f"Subtask `{subtask_id}` was not found.", session_id=session_id)
    if task.session_id != session_id:
        return _not_owned(subtask_id, session_id)
    new_id = await agent.runtime.retry_subtask(task.id)
    if not new_id:
        return TurnPresentation(assistant_text=f"Subtask `{task.id[:8]}` cannot be retried.", session_id=session_id)
    return TurnPresentation(
        assistant_text=f"Created retry subtask `{new_id[:8]}` from `{task.id[:8]}`.",
        session_id=session_id,
    )


async def _tasks_resume(agent: Any, subtask_id: str, session_id: str) -> TurnPresentation:
    if not subtask_id:
        return TurnPresentation(assistant_text="Usage: `/task resume <subtask-id>`", session_id=session_id)
    task = await resolve_subtask(agent.runtime, subtask_id)
    if task is None:
        return TurnPresentation(assistant_text=f"Subtask `{subtask_id}` was not found.", session_id=session_id)
    if task.session_id != session_id:
        return _not_owned(subtask_id, session_id)
    ok = await agent.runtime.resume_subtask(task.id)
    if not ok:
        return TurnPresentation(
            assistant_text=f"Subtask `{task.id[:8]}` is `{task.status}` and does not need or support resume.",
            session_id=session_id,
        )
    return TurnPresentation(assistant_text=f"Returned subtask `{task.id[:8]}` to the recovery queue.", session_id=session_id)


async def _tasks_subtasks(agent: Any, task_id: str, session_id: str) -> TurnPresentation:
    if not task_id:
        return TurnPresentation(assistant_text="Usage: `/task subtask <task-id>`", session_id=session_id)
    task = await agent.tasks.get_task(task_id)
    if task is not None and task.session_id != session_id:
        return _not_owned(task_id, session_id)
    rows = await subtasks_for_task(agent, task_id)
    if not rows:
        return TurnPresentation(assistant_text=f"Task `{task_id}` has no subtasks.", session_id=session_id)
    lines = ["## Subtasks", "", "| subtask | skill | status |", "|---|---|---|"]
    lines.extend(f"| `{task.id[:8]}` | {task.skill_name} | {task.status} |" for task in rows[:12])
    lines.append(f"\nDetails: `/task show {rows[0].id[:8]}`")
    return TurnPresentation(assistant_text="\n".join(lines), session_id=session_id)


async def _tasks_approve(agent: Any, task_id: str, session_id: str) -> TurnPresentation:
    if not task_id:
        return TurnPresentation(assistant_text="Usage: `/task approve <task-id>`", session_id=session_id)
    task = await agent.tasks.get_task(task_id)
    if task is None:
        return TurnPresentation(assistant_text=f"Task `{task_id}` was not found.", session_id=session_id)
    if task.session_id != session_id:
        return _not_owned(task_id, session_id)
    if task.status != "awaiting_approval":
        return TurnPresentation(
            assistant_text=f"Task `{task.id[:8]}` is `{task.status}`, not awaiting approval.",
            session_id=session_id,
        )
    try:
        result = await agent.approve_task(task.id, drain_tasks=False)
    except (LookupError, ValueError) as exc:
        return TurnPresentation(assistant_text=str(exc), session_id=session_id)
    return turn_presentation_from_result(result, channel=task.channel)


async def _tasks_cancel(agent: Any, task_id: str, session_id: str) -> TurnPresentation:
    if not task_id:
        return TurnPresentation(assistant_text="Usage: `/task cancel <task-id>`", session_id=session_id)
    task = await agent.tasks.get_task(task_id)
    if task is None:
        return TurnPresentation(assistant_text=f"Task `{task_id}` was not found.", session_id=session_id)
    if task.session_id != session_id:
        return _not_owned(task_id, session_id)
    if task.status in {"awaiting_approval", "needs_input"}:
        await agent.tasks.finish_task(task.id, status="cancelled", summary="cancelled by channel user")
        return TurnPresentation(
            assistant_text=f"Cancelled task `{task.id[:8]}`.",
            session_id=session_id,
        )
    try:
        await agent.tasks.request_control(task.id, action="cancel")
    except ValueError:
        return TurnPresentation(
            assistant_text=f"Task `{task.id[:8]}` is `{task.status}` and cannot be cancelled.",
            session_id=session_id,
        )
    return TurnPresentation(
        assistant_text=f"Cancellation requested. Task `{task.id[:8]}` will stop at the next safe execution boundary.",
        session_id=session_id,
    )


async def _tasks_steer(
    agent: Any,
    task_id: str,
    instruction: str,
    session_id: str,
) -> TurnPresentation:
    if not task_id or not instruction.strip():
        return TurnPresentation(
            assistant_text="Usage: `/task steer <task-id> <instruction>`",
            session_id=session_id,
        )
    task = await agent.tasks.get_task(task_id)
    if task is None:
        return TurnPresentation(assistant_text=f"Task `{task_id}` was not found.", session_id=session_id)
    if task.session_id != session_id:
        return _not_owned(task_id, session_id)
    rejection = await agent.tasks.steer_rejection_reason(task.id)
    if rejection:
        return TurnPresentation(
            assistant_text=rejection,
            session_id=session_id,
        )
    try:
        await agent.tasks.request_control(task.id, action="steer", instruction=instruction)
    except ValueError:
        return TurnPresentation(
            assistant_text=f"Task `{task.id[:8]}` is `{task.status}` and cannot be steered.",
            session_id=session_id,
        )
    return TurnPresentation(
        assistant_text=f"Steering instruction submitted to task `{task.id[:8]}`: {_compact(instruction, 160)}",
        session_id=session_id,
    )


async def _tasks_list(
    agent: Any,
    *,
    session_id: str,
    action: str,
    status: str,
    limit: int,
    session: str,
) -> TurnPresentation:
    rows = await agent.tasks.list_tasks(limit=limit, status=status or None, kind="turn") if hasattr(agent, "tasks") else []
    rows = [row for row in rows if row.session_id == session_id]
    title = "Current tasks"
    if action == "watch":
        title = "Current task snapshot"
    lines = [f"## {title}"]
    if action == "watch":
        lines.append("`/task watch` shows the current snapshot; completion notifications are pushed automatically.")
    if not rows:
        suffix = f" (status={status})" if status else ""
        lines.append(f"No tasks found{suffix}.")
        return TurnPresentation(assistant_text="\n\n".join(lines), session_id=session_id)
    lines += [
        "",
        "| task ID | status | current stage | session | created |",
        "|---|---|---|---|---|",
    ]
    task_prefixes = shortest_unique_prefixes([row.id for row in rows])
    for row in rows[:limit]:
        lines.append(
            "| "
            f"`{task_prefixes[row.id]}` | {row.status} | {short_id(row.current_subtask_id) if row.current_subtask_id else row.current_tool or row.current_stage or '-'} | "
            f"`{short_id(row.session_id) or '-'}` | {_format_time(row.created_at)} |"
        )
    lines += [
        "",
        "Next actions:",
        f"- `/task show {task_prefixes[rows[0].id]}` inspect the latest task",
        f"- `/task subtask {task_prefixes[rows[0].id]}` inspect its subtasks",
    ]
    return TurnPresentation(assistant_text="\n".join(lines), session_id=session_id)


async def _handle_verify(agent: Any, arg: str, session_id: str) -> TurnPresentation:
    from omni.research.store import ResearchStore
    from omni.research.verify import verify_session

    scope = session_id
    report = await verify_session(ResearchStore(agent.db), session_id=scope)
    label = f"session `{scope[:8]}`" if scope else "workspace"
    lines = [
        "## Evidence audit",
        f"Scope: {label}",
    ]
    if report.total_claims == 0:
        lines.append("No verifiable claims have been recorded in this scope.")
        return TurnPresentation(assistant_text="\n".join(lines), session_id=session_id)
    lines.append(
        f"Claims: {report.total_claims}; grounding rate: {report.grounding_rate:.0%}; issues: {report.issues}."
    )
    if report.unsupported:
        lines.append(f"\nUnsupported claims ({len(report.unsupported)}):")
        lines.extend(f"- {_compact(c.text, 90)} (`{c.id[:8]}`)" for c in report.unsupported[:8])
    if report.contradicted:
        lines.append(f"\nClaims with contradicting evidence ({len(report.contradicted)}):")
        lines.extend(f"- {_compact(c.text, 80)} (`{c.id[:8]}`, contradictions {n})" for c, n in report.contradicted[:8])
    if report.overconfident:
        lines.append(f"\nOverconfident claims without evidence ({len(report.overconfident)}):")
        lines.extend(
            f"- {_compact(c.text, 80)} (`{c.id[:8]}`, confidence {c.confidence:.0%})"
            for c in report.overconfident[:8]
        )
    if report.issues == 0:
        lines.append("\nAll claims have supporting evidence and no recorded contradiction.")
    else:
        lines.append("\nRecommended action: add evidence, lower confidence, or withdraw unsupported claims.")
    return TurnPresentation(assistant_text="\n".join(lines), session_id=session_id)


def _handle_inbox(agent: Any, session_id: str) -> TurnPresentation:
    notes = InboxNotifier(agent.paths.project_dir / "inbox.jsonl").read_all()
    notes = [note for note in notes if str(note.get("session_id") or "") == session_id]
    if not notes:
        return TurnPresentation(assistant_text="No task notifications.", session_id=session_id)
    lines = [
        "## Recent task notifications",
        "",
        "| time | task | object | skill | status | summary |",
        "|---|---|---|---|---|---|",
    ]
    for note in notes[-10:]:
        task_id = str(note.get("task_id") or "")
        object_id = str(note.get("object_id") or note.get("subtask_id") or "")
        object_kind = str(note.get("object_kind") or "skill_execution")
        lines.append(
            "| "
            f"{_format_time(note.get('created_at'))} | "
            f"`{task_id[:8] or '-'}` | `{object_kind}:{object_id[:8] or '-'}` | "
            f"{note.get('skill_name', '')} | "
            f"{note.get('status', '')} | {_compact(str(note.get('summary') or note.get('title') or ''), 80)} |"
        )
    return TurnPresentation(assistant_text="\n".join(lines), session_id=session_id)


def _help() -> TurnPresentation:
    return TurnPresentation(
        assistant_text=(
            "Available IM commands:\n"
            "- `/stop` cancel the active task in this conversation\n"
            "- `/steer <instruction>` redirect the active task at its next safe boundary\n"
            "- `/task` list recent tasks (user requests)\n"
            "- `/task watch` show a task snapshot; subtask completion is pushed automatically\n"
            "- `/task show <id>` inspect a task or drill into its workflow, step, or skill execution\n"
            "- `/task subtask <task-id>` list subtasks\n"
            "- `/task attach <task-id>` attach the complete task result; workflow, step, and execution IDs remain supported\n"
            "- `/task retry <subtask-id>` create a retry subtask from the input snapshot\n"
            "- `/task resume <subtask-id>` return a failed or cancelled subtask to recovery\n"
            "- `/task approve <task-id>` approve and execute a plan-mode task\n"
            "- `/task cancel <task-id>` safely cancel an active task\n"
            "- `/task steer <task-id> <instruction>` steer an active task at its next boundary\n"
            "- `/plan <request>` create a plan for approval without executing it\n"
            "- `/verify --session` audit claims and evidence in this session\n"
            "- `/inbox` list recent task notifications"
        )
    )


def _tasks_help(session_id: str) -> TurnPresentation:
    presentation = _help()
    return TurnPresentation(assistant_text=presentation.assistant_text, session_id=session_id)


def _presentation_for_task(task: SubtaskORM):
    return task_presentation_from_result(
        subtask_id=task.id,
        task_id=task.task_id or "",
        object_kind="skill_execution",
        object_id=task.id,
        skill=task.skill_name,
        status=task.status,
        result=task.result_json if isinstance(task.result_json, dict) else {},
        error=task.error or "",
        trace=task.trace_log if isinstance(task.trace_log, list) else [],
    )


def _parse_task_args(arg: str, current_session_id: str) -> dict[str, Any]:
    try:
        tokens = shlex.split(arg)
    except ValueError:
        return {"help": True, "action": "list", "subtask_id": "", "status": "", "limit": 20, "session": ""}
    parsed: dict[str, Any] = {
        "help": False,
        "action": "list",
        "subtask_id": "",
        "status": "",
        "limit": 20,
        "session": "",
        "instruction": "",
    }
    if tokens and tokens[0] in {"help", "--help", "-h"}:
        parsed["help"] = True
        return parsed
    if tokens and tokens[0] == "list":
        tokens.pop(0)
    elif tokens and tokens[0] == "session":
        parsed["action"] = "session"
        parsed["session"] = current_session_id
        tokens.pop(0)
    if tokens and tokens[0] in {
        "show", "watch", "attach", "subtask", "retry", "resume", "approve", "cancel", "steer"
    }:
        parsed["action"] = tokens.pop(0)
        if parsed["action"] in {
            "show", "attach", "subtask", "retry", "resume", "approve", "cancel", "steer"
        } and tokens and not tokens[0].startswith("-"):
            parsed["subtask_id"] = tokens.pop(0)
        if parsed["action"] == "steer":
            parsed["instruction"] = " ".join(tokens).strip()
            tokens.clear()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"--session", "-s"}:
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                parsed["session"] = tokens[i + 1]
                i += 1
            else:
                parsed["session"] = current_session_id
        elif token in {"--current-session", "--this-session"}:
            parsed["session"] = current_session_id
        elif token == "--status" and i + 1 < len(tokens):
            parsed["status"] = tokens[i + 1]
            i += 1
        elif token == "--limit" and i + 1 < len(tokens):
            try:
                parsed["limit"] = max(1, min(50, int(tokens[i + 1])))
            except ValueError:
                parsed["limit"] = 20
            i += 1
        elif not token.startswith("-") and parsed["action"] == "list":
            parsed["action"] = "show"
            parsed["subtask_id"] = token
        i += 1
    return parsed


def _parse_verify_scope(arg: str, current_session_id: str) -> str:
    try:
        tokens = shlex.split(arg)
    except ValueError:
        return ""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"--session", "-s"}:
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                return tokens[i + 1]
            return current_session_id
        i += 1
    return ""


def _format_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value or "")
    return text[:16] if text else "-"


def _compact(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _not_owned(item_id: str, session_id: str) -> TurnPresentation:
    return TurnPresentation(
        assistant_text=f"Task `{item_id}` does not belong to this session and cannot be inspected or modified.",
        session_id=session_id,
    )
