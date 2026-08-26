"""Durable escalate: a child turn inherits the parent ROM and continues."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def maybe_escalate_run(
    agent: Any,
    goal: str,
    session_id: str,
    channel: str,
    *,
    task_id: str = "",
) -> str | None:
    """Create a durable child turn that continues the research goal.

    The current turn ends as ``escalated``. The child inherits this task's
    ROM and contract, then runs ``handle_turn`` in the background so the
    coordinator is not blocked. Nested escalate (child of an escalated
    task, or depth ≥ 2) is refused — empty tools are worse than a bound.
    """
    goal_text = str(goal or "").strip()
    if not goal_text:
        return None
    tasks = agent.tasks
    parent = await tasks.get_task(task_id) if task_id else None
    if parent is not None and (
        str(getattr(parent, "kind", "") or "") == "escalated"
        or int(getattr(parent, "depth", 0) or 0) >= 2
    ):
        return None
    child = await tasks.create_task(
        session_id=session_id,
        channel=channel or "cli",
        user_input=goal_text,
        title=("escalate: " + goal_text)[:80],
        parent_task_id=task_id,
        kind="escalated",
        depth=(int(getattr(parent, "depth", 0) or 0) + 1) if parent is not None else 1,
        require_session=True,
    )
    if parent is not None:
        await tasks.inherit_research_ledger(child.id, parent)
    await tasks.append_event(
        child.id,
        event_type="task.escalated",
        status="running",
        name="escalate_run",
        output_json={"from_task_id": task_id, "goal": goal_text},
        summary=f"escalated from {task_id[:8]}" if task_id else "escalated run",
    )
    asyncio.create_task(
        _run_escalated_turn(agent, child.id, goal_text, session_id, channel or "cli"),
        name=f"escalate:{child.id[:8]}",
    )
    return child.id


async def _run_escalated_turn(
    agent: Any,
    task_id: str,
    goal: str,
    session_id: str,
    channel: str,
) -> None:
    try:
        await agent.handle_turn(
            goal,
            session_id=session_id,
            channel=channel,
            existing_task_id=task_id,
            drain_tasks=True,
            origin="schedule",
        )
    except Exception:  # noqa: BLE001 — background escalate must not kill the parent
        logger.exception("escalated turn %s failed", task_id[:8])
