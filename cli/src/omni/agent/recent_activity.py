"""Principal-scoped, cross-session recent-activity digest.

Codex-aligned working continuity: a fresh turn (new session, no active focus)
must still resolve references to earlier output ("regenerate the last figure",
"again", "that report") without asking the user to re-clarify. Where Codex gets
recent history for free from a single agent transcript, Omni synthesises a
compact digest of the principal's recent deliverables (succeeded/degraded tasks
plus their artifacts) so the semantic planner can bind the referent
deterministically instead of short-circuiting to a clarifying question.

Isolation: entries are filtered to the calling principal via ``principal_of``
(the same mapping recall uses), so ``per_peer`` hosting never leaks one peer's
task list to another, while the owner sees CLI + authorised-IM activity unified.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select

from omni.storage.artifacts import ArtifactStore
from omni.storage.db import Database
from omni.storage.models import ArtifactORM, TaskORM

# Statuses that mean "a product exists to reopen"; running/needs_input turns are
# in-flight and carry nothing to bind a referent to yet.
_TERMINAL_WITH_PRODUCT = ("succeeded", "degraded")
# How many recent turns to scan before principal filtering. Bounded so the
# digest stays a cheap single read even on a busy workspace.
_SCAN_LIMIT = 60
_TITLE_MAX = 80


def _title_for(task: TaskORM) -> str:
    raw = (task.title or task.user_input or "").strip().replace("\n", " ")
    if len(raw) > _TITLE_MAX:
        return raw[: _TITLE_MAX - 1] + "…"
    return raw


async def recent_activity_digest(
    db: Database,
    artifacts: ArtifactStore,
    *,
    principal: str,
    principal_of: Callable[[str, str], str],
    limit: int = 6,
) -> str:
    """Render the principal's recent deliverables as a bounded context block."""
    async with db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(TaskORM)
                    .where(TaskORM.archived_at.is_(None), TaskORM.kind == "turn")
                    .order_by(TaskORM.created_at.desc())
                    .limit(_SCAN_LIMIT)
                )
            )
            .scalars()
            .all()
        )
    produced = [
        t
        for t in rows
        if t.status in _TERMINAL_WITH_PRODUCT
        and principal_of(t.channel or "cli", t.external_key or "") == principal
    ][:limit]
    if not produced:
        return ""

    # Resolve artifact titles for the shown tasks in one batched read so the
    # digest names the concrete outputs the planner can bind a referent to.
    art_ids = [aid for t in produced for aid in (t.artifact_ids or [])]
    titles: dict[str, str] = {}
    if art_ids:
        async with db.session() as s:
            arts = list(
                (await s.execute(select(ArtifactORM).where(ArtifactORM.id.in_(art_ids))))
                .scalars()
                .all()
            )
        titles = {a.id: (a.title or a.kind or "artifact") for a in arts}

    lines = [
        "[Recent activity] Deliverables you produced for this user (newest first). "
        "To act on one — describe it, revise it, or regenerate it — reopen it with "
        "get_task / get_subtask / open_artifact / list_session_artifacts. Do NOT ask "
        "the user to re-describe something listed here:"
    ]
    for t in produced:
        label = _title_for(t) or t.intent_type or "task"
        head = f"- [{t.id[:8]}] {label} · {t.status}"
        named = [titles[aid] for aid in (t.artifact_ids or []) if aid in titles][:2]
        if named:
            head += " · outputs: " + ", ".join(named)
        lines.append(head)
    return "\n".join(lines)
