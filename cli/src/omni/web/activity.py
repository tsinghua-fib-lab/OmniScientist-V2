"""Read-only activity projection for the web surface.

Live SSE and durable ``task_events`` share this DTO. Summaries never re-enter
the agent context. Raw tool input/output is truncated and redacted here; the
full payload stays on ``task.get`` for an explicit inspector open.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from omni.storage.models import TaskEventORM
from omni.web.protocol import jsonable, utc_iso

_SENSITIVE_KEY_MARKERS = ("key", "token", "secret", "password", "credential", "authorization")
_PREVIEW_LIMIT = 160
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 500

_NEW_SESSION = "\u65b0\u4f1a\u8bdd"
_SOULAGENT_TURN_PREFIX = "$soulagent "

# Web follows states that can produce a later durable transition. This is
# intentionally wider than "a worker is executing": queued and approval-paused
# tasks must keep one SSE subscription instead of reconnecting on every catalog
# refresh. ``needs_input`` is intentionally absent because that pause is handed
# back to the composer as a completed turn until the user resumes it.
FOLLOWABLE_TASK_STATUSES = frozenset(
    {"pending", "queued", "running", "recovering", "awaiting_approval"}
)


def is_soulagent_turn_input(value: Any) -> bool:
    """True for the canonical root turn compiled by the Persona Web control."""
    return str(value or "").lstrip().startswith(_SOULAGENT_TURN_PREFIX)


def persona_turn_display_title(value: Any) -> str:
    """Render a canonical Persona turn without exposing its protocol JSON."""
    raw = str(value or "").strip()
    if not is_soulagent_turn_input(raw):
        return raw
    try:
        payload = json.loads(raw.lstrip().removeprefix(_SOULAGENT_TURN_PREFIX))
    except (TypeError, ValueError):
        return "Scientist persona"
    if not isinstance(payload, dict) or str(payload.get("action") or "") == "unload":
        return "Scientist persona"
    task = str(payload.get("input") or "").strip()
    if task.startswith("Research task:"):
        task = task.removeprefix("Research task:").strip()
        return task or "Scientist persona"
    if task.casefold().startswith("think like "):
        return task
    return "Scientist persona"


def display_title(
    *,
    title: str = "",
    task_input: str = "",
    user_message: str = "",
) -> str:
    """Owner title, else first root-task input, else first user line."""
    title_candidate = title
    if is_soulagent_turn_input(task_input) and (
        not title_candidate or is_soulagent_turn_input(title_candidate)
    ):
        title_candidate = task_input
    for candidate in (title_candidate, task_input, user_message):
        candidate = persona_turn_display_title(candidate)
        text = " ".join(str(candidate or "").split())
        if text:
            return text
    return _NEW_SESSION


def classify_event(event_type: str) -> str:
    event = str(event_type or "")
    if event.startswith("plan.") or event in {"context.assembled", "input.resolved"}:
        return "plan"
    if "tool" in event or event.startswith("react."):
        return "tool"
    if event.startswith("workflow.") or event.startswith("step."):
        return "workflow"
    if "progress" in event or event.endswith(".pct"):
        return "progress"
    if event.startswith("task.") or event.endswith(".stage"):
        return "stage"
    return "event"


def replace_key_for(event_type: str) -> str:
    event = str(event_type or "")
    if event.startswith("plan.") and event not in {
        "plan.model.failed",
        "plan.executed",
        "plan.recovery",
    }:
        return "plan.summary"
    return ""


def _mask_value(key: str, value: Any) -> Any:
    lowered = str(key).lower()
    if any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS):
        return "***"
    return value


def public_skill_payload(
    skill_name: str,
    value: Any,
    *,
    result: bool,
) -> Any:
    """Return the browser-safe subset of one Skill payload.

    Most skills keep the existing shallow secret masking. SoulAgent is more
    restrictive because its durable result contains generated persona prose
    and absolute host paths that the Web UI never needs.
    """
    if skill_name.casefold() != "soulagent":
        return jsonable(value or {})
    raw = value if isinstance(value, dict) else {}
    if not result:
        return {
            key: raw[key]
            for key in ("action", "scientist_id", "force")
            if key in raw and isinstance(raw[key], (str, bool))
        }
    outcome = raw.get("outcome")
    code = outcome.get("code") if isinstance(outcome, dict) else ""
    public: dict[str, Any] = {
        "redacted": True,
        "outcome": {"code": code if isinstance(code, str) else ""},
    }
    for key in ("status", "active_scientist_id"):
        if isinstance(raw.get(key), str):
            public[key] = raw[key]
    for key in ("active", "loaded", "refreshed", "needs_input", "recoverable", "blocking"):
        if isinstance(raw.get(key), bool):
            public[key] = raw[key]
    return public


def public_skill_text(skill_name: str, value: Any, *, error: bool = False) -> str:
    """Keep SoulAgent prose and paths out of free-form browser fields."""
    text = _squash(str(value or ""))
    if skill_name.casefold() != "soulagent" or not text:
        return text
    return "SoulAgent operation failed" if error else "SoulAgent operation update"


def _squash(text: str) -> str:
    return " ".join(text.split())


def _preview(value: Any, *, limit: int = _PREVIEW_LIMIT) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        safe = {str(k): _mask_value(str(k), v) for k, v in value.items()}
        try:
            text = json.dumps(jsonable(safe), ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(safe)
    elif isinstance(value, (list, tuple)):
        text = _squash(str(jsonable(value)))
    else:
        text = _squash(str(value))
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def project_event(
    event: TaskEventORM,
    *,
    fallback_skill_name: str = "",
) -> dict[str, Any]:
    """One ActivityItem: identity, status, and a safe preview."""
    event_type = event.event_type or ""
    projection_skill = event.skill_name or fallback_skill_name
    title = (
        event.name
        or event.tool_name
        or event.skill_name
        or event_type
        or "event"
    )
    title = public_skill_text(projection_skill, title)
    return {
        "task_id": event.task_id,
        "seq": int(event.seq or 0),
        "timestamp": utc_iso(event.created_at),
        "kind": classify_event(event_type),
        "phase": event_type,
        "status": event.status or event.lifecycle_status or "",
        "tool": event.tool_name or "",
        "skill": event.skill_name or "",
        "workflow_run_id": event.workflow_run_id or "",
        "workflow_step_id": event.workflow_step_id or "",
        "subtask_id": event.subtask_id or "",
        "title": title,
        "summary": public_skill_text(projection_skill, event.summary)[
            :_PREVIEW_LIMIT
        ],
        "safe_args": _preview(
            public_skill_payload(projection_skill, event.input_json, result=False)
        ),
        "safe_result": _preview(
            public_skill_payload(projection_skill, event.output_json, result=True)
        ),
        "pct": event.pct,
        "duration_ms": event.duration_ms,
        "error": public_skill_text(projection_skill, event.error, error=True)[
            :_PREVIEW_LIMIT
        ],
        "group_key": event.tool_name or event.skill_name or event.workflow_run_id or "",
        "replace_key": replace_key_for(event_type),
    }


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return _DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(value, _MAX_LIMIT))


async def list_events_after(
    agent: Any,
    task_id: str,
    *,
    after_seq: int = 0,
    limit: int | None = None,
) -> list[TaskEventORM]:
    cap = clamp_limit(limit)
    after = max(0, int(after_seq or 0))
    async with agent.db.session() as session:
        rows = (
            await session.execute(
                select(TaskEventORM)
                .where(TaskEventORM.task_id == task_id, TaskEventORM.seq > after)
                .order_by(TaskEventORM.seq.asc())
                .limit(cap)
            )
        ).scalars().all()
    return list(rows)


async def project_events_after(
    agent: Any,
    task_id: str,
    *,
    after_seq: int = 0,
    limit: int | None = None,
    fallback_skill_name: str | None = None,
) -> list[dict[str, Any]]:
    rows = await list_events_after(agent, task_id, after_seq=after_seq, limit=limit)
    projection_skill = fallback_skill_name
    if projection_skill is None and any(not row.skill_name for row in rows):
        task = await agent.tasks.get_task(task_id)
        projection_skill = (
            "soulagent"
            if task is not None and is_soulagent_turn_input(task.user_input)
            else ""
        )
    projection_skill = projection_skill or ""
    return [
        project_event(row, fallback_skill_name=projection_skill) for row in rows
    ]
