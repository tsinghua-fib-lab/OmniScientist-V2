"""Cross-workspace aggregation for ``--all`` / ``all`` views.

Reads the workspace **catalog** (registry ∪ channel-anchor ∪ named project DBs),
opens each workspace's store (skipping missing files so we never materialise
empty databases), and returns recent rows tagged with their workspace label.
This powers ``omni task list --all`` / the REPL ``/task all`` and ``omni schedule
all`` / ``/schedule all`` — omni's edge over Claude/Codex, which have no
cross-window view at all. Rows stay consistent with the per-workspace
``task list`` / ``schedule list``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from omni.config.paths import user_home
from omni.config.workspaces import iter_catalog_workspaces
from omni.runtime.action_checkpoints import ActionCheckpointStore, CheckpointRecord
from omni.runtime.task_index import TaskIndex, settings_for_workspace
from omni.runtime.task_recorder import repair_misfiled_chat
from omni.storage.db import get_database
from omni.storage.models import ScheduleORM, TaskORM

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omni.config import OmniSettings

logger = logging.getLogger(__name__)


@dataclass
class AggClarificationRow:
    workspace: str
    record: CheckpointRecord


@dataclass
class AggTaskRow:
    workspace: str
    id: str
    status: str
    session_id: str
    channel: str
    title: str
    created_at: datetime | None
    archived_at: datetime | None = None


async def list_tasks_all_workspaces(
    *,
    limit_per: int = 50,
    status: str | None = None,
    home: Path | None = None,
    include_archived: bool = False,
    kind: str | None = None,
) -> list[AggTaskRow]:
    control_path = (home or user_home()) / "control.sqlite3"
    out: list[AggTaskRow] = []
    for rec in iter_catalog_workspaces(home):
        db_path = Path(rec.get("db", ""))
        if not db_path.exists():
            continue  # don't recreate workspaces that were deleted
        try:
            db = get_database(db_path)
            await db.init()
            # Same one-shot repair the per-workspace list runs, and for the same
            # reason: the ``kind`` filter below is applied in SQL, so a turn
            # still misfiled as ``chat`` would be dropped before anyone sees it.
            await repair_misfiled_chat(db)
            async with db.session() as s:
                q = (
                    select(TaskORM)
                    .order_by(TaskORM.created_at.desc())
                    .limit(limit_per)
                )
                if status:
                    q = q.where(TaskORM.status == status)
                if kind:
                    q = q.where(TaskORM.kind == kind)
                if not include_archived:
                    q = q.where(TaskORM.archived_at.is_(None))
                rows = (await s.execute(q)).scalars().all()
        except Exception:  # noqa: BLE001 — a single bad DB shouldn't break the view
            continue
        label = rec.get("name") or db_path.parent.name
        # Mirror the rows we just read into the global index so a subsequent
        # ``omni task show <id>`` can route to this workspace (the "list here,
        # look up there" fix). Best-effort — never let it break the --all view.
        try:
            await TaskIndex(
                control_path,
                project_dir=str(rec.get("project_dir") or db_path.parent),
                workspace_root=str(rec.get("root") or ""),
                workspace_kind=str(rec.get("kind") or ""),
                workspace_label=label,
            ).record_many(list(rows))
        except Exception:  # noqa: BLE001
            logger.debug("task index: --all sync skipped %s", label, exc_info=True)
        out.extend(
            AggTaskRow(
                workspace=label, id=r.id, status=r.status,
                session_id=r.session_id, channel=r.channel,
                title=(r.title or r.user_input or ""),
                created_at=r.created_at, archived_at=r.archived_at,
            )
            for r in rows
        )
    out.sort(key=lambda t: t.created_at.isoformat() if t.created_at else "", reverse=True)
    return out


@dataclass
class AggScheduleRow:
    workspace: str
    id: str
    title: str
    skill_name: str
    kind: str
    interval_s: int
    cron_expr: str
    enabled: bool
    next_due_at: datetime | None
    last_run_at: datetime | None
    run_count: int
    created_at: datetime | None = None


async def list_schedules_all_workspaces(
    *,
    limit_per: int = 100,
    include_disabled: bool = True,
    home: Path | None = None,
) -> list[AggScheduleRow]:
    """Recurring/one-shot schedules across every catalog workspace.

    Mirrors :func:`list_tasks_all_workspaces` for the ``schedule`` surface: the
    always-on home service dispatches *every* workspace's schedules, so this gives
    the owner one place to see them all (a bare ``schedule list`` is scoped to the
    current workspace only). Rows are ordered soonest-due first, disabled last.
    """
    out: list[AggScheduleRow] = []
    for rec in iter_catalog_workspaces(home):
        db_path = Path(rec.get("db", ""))
        if not db_path.exists():
            continue  # don't recreate workspaces that were deleted
        try:
            db = get_database(db_path)
            await db.init()
            async with db.session() as s:
                q = select(ScheduleORM).order_by(ScheduleORM.next_due_at.asc()).limit(limit_per)
                if not include_disabled:
                    q = q.where(ScheduleORM.enabled.is_(True))
                rows = (await s.execute(q)).scalars().all()
        except Exception:  # noqa: BLE001 — a single bad DB shouldn't break the view
            continue
        label = rec.get("name") or db_path.parent.name
        out.extend(
            AggScheduleRow(
                workspace=label, id=r.id, title=r.title, skill_name=r.skill_name,
                kind=r.kind, interval_s=r.interval_s, cron_expr=r.cron_expr,
                enabled=r.enabled, next_due_at=r.next_due_at,
                last_run_at=r.last_run_at, run_count=r.run_count, created_at=r.created_at,
            )
            for r in rows
        )
    # Soonest-due first; schedules with no next fire (disabled/none) sort last.
    out.sort(key=lambda x: (x.next_due_at is None, x.next_due_at.isoformat() if x.next_due_at else "", x.workspace))
    return out


async def resolve_schedule_workspace(
    settings: OmniSettings, ident: str
) -> OmniSettings | None:
    """Settings for the workspace that owns schedule ``ident`` (cross-workspace scan).

    Schedules live in their owning workspace's DB, but the always-on home service
    fires *every* workspace's schedules and ``schedule all`` lists them together —
    so an id copied from ``schedule all`` must resolve back to its workspace, not
    just the current directory's (the same "list here, look up there" gap the task
    index closes for tasks). Returns ``None`` when the id is not a *unique* match in
    a *different* workspace, so callers fall back to the local workspace unchanged.

    There is no schedule control-index (schedules are low-volume and this is an
    interactive command), so we scan each catalog workspace directly, mirroring
    the task index's self-healing :func:`_scan_for_task` fallback. Ambiguity-safe:
    a prefix that matches two rows in any workspace refuses to route.
    """
    ident = (ident or "").strip()
    if not ident:
        return None
    paths = getattr(settings, "paths", None)
    home = paths.home if paths is not None else user_home()
    local_dir = str(paths.project_dir) if paths is not None else ""

    matches: list[dict] = []
    for rec in iter_catalog_workspaces(home):
        db_path = Path(rec.get("db", ""))
        if not db_path.exists():
            continue  # don't recreate workspaces that were deleted
        try:
            db = get_database(db_path)
            await db.init()
            async with db.session() as s:
                exact = await s.get(ScheduleORM, ident)
                if exact is not None:
                    hit_ids = [exact.id]
                else:
                    hit_ids = list(
                        (
                            await s.execute(
                                select(ScheduleORM.id)
                                .where(ScheduleORM.id.like(f"{ident}%"))
                                .limit(2)
                            )
                        ).scalars().all()
                    )
        except Exception:  # noqa: BLE001 — a single bad DB shouldn't break resolution
            logger.debug("schedule resolve: scan skipped a workspace", exc_info=True)
            continue
        if len(hit_ids) >= 2:
            return None  # ambiguous prefix within a workspace → let the caller stay local
        if hit_ids:
            matches.append(rec)

    # Route only on a single, unambiguous owner. When that owner is the local
    # workspace, return None so the caller keeps its unchanged local path.
    if len(matches) != 1:
        return None
    rec = matches[0]
    if local_dir and str(rec.get("project_dir") or "") == local_dir:
        return None
    return settings_for_workspace(
        kind=str(rec.get("kind") or ""),
        root=str(rec.get("root") or ""),
        name=str(rec.get("name") or Path(rec.get("db", "")).parent.name),
    )


async def list_open_clarifications_all_workspaces(
    *,
    limit: int = 30,
    home: Path | None = None,
) -> list[AggClarificationRow]:
    """Open schedule clarifications across every catalog workspace.

    Clarification drafts live in the owning workspace DB (often the IM channel
    anchor) with ``required_decider`` set to the original requester's principal —
    not hard-coded ``local``. This view is owner observability: no principal
    filter, tagged with workspace so CLI ``schedule clarifications`` can see
    WeChat drafts the same way ``schedule all`` sees WeChat schedules.
    """
    out: list[AggClarificationRow] = []
    per_ws = max(1, limit)
    for rec in iter_catalog_workspaces(home):
        db_path = Path(rec.get("db", ""))
        if not db_path.exists():
            continue
        label = str(rec.get("name") or db_path.parent.name)
        try:
            db = get_database(db_path)
            await db.init()
            store = ActionCheckpointStore(db)
            await store.expire_due()
            rows = await store.list_open(principal=None, limit=per_ws)
        except Exception:  # noqa: BLE001 — one bad DB must not blank the view
            logger.debug("clarifications catalog: skipped %s", label, exc_info=True)
            continue
        out.extend(AggClarificationRow(workspace=label, record=r) for r in rows)
    out.sort(
        key=lambda row: (
            -(row.record.created_at.timestamp() if row.record.created_at else 0.0),
            row.workspace,
            row.record.id,
        )
    )
    return out[: max(1, limit)]
