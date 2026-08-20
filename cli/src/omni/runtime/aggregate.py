"""Cross-workspace aggregation for ``--all`` / ``all`` views.

Task lists read the machine-global ``task_index`` in ``control.sqlite3`` — the
same control-plane Codex keeps for thread listings — so ``/task all`` does not
open every workspace store or take schema-init locks. An empty index is
reconciled once from the catalog. Schedule and clarification views still scan
each catalog workspace directly.

This powers ``omni task list --all`` / the REPL ``/task all`` and ``omni schedule
all`` / ``/schedule all``.
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
from omni.runtime.task_index import TaskIndex, reconcile_index, settings_for_workspace
from omni.storage.db import get_database
from omni.storage.models import ScheduleORM, TaskIndexORM

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
    kind: str = ""


def _agg_from_index(row: TaskIndexORM) -> AggTaskRow:
    label = row.workspace_label or row.project or Path(row.project_dir or "").name
    return AggTaskRow(
        workspace=label,
        id=row.task_id,
        status=row.status,
        session_id=row.session_id,
        channel=row.channel,
        title=row.title or "",
        created_at=row.created_at,
        archived_at=row.archived_at,
        kind=row.kind or "",
    )


async def _indexed_task_page(
    *,
    cap: int,
    status: str | None = None,
    home: Path | None = None,
    include_archived: bool = False,
    kind: str | None = None,
    session: str | None = None,
) -> tuple[list[AggTaskRow], int]:
    """Read the global task index; reconcile once when the index is empty."""
    root = home or user_home()
    control_path = root / "control.sqlite3"
    index = TaskIndex(control_path)
    if not await index.list(include_archived=True, limit=1):
        await reconcile_index(root, control_db=control_path)
    total = await index.count(
        status=status,
        kind=kind,
        include_archived=include_archived,
        session=session,
    )
    fetch = total if cap <= 0 else min(total, cap)
    if fetch <= 0:
        return [], total
    rows = await index.list(
        status=status,
        kind=kind,
        include_archived=include_archived,
        session=session,
        limit=fetch,
    )
    return [_agg_from_index(row) for row in rows], total


async def list_tasks_all_workspaces(
    *,
    limit_per: int = 50,
    limit: int | None = None,
    status: str | None = None,
    home: Path | None = None,
    include_archived: bool = False,
    kind: str | None = None,
    session: str | None = None,
) -> list[AggTaskRow]:
    """Recent tasks across every workspace, newest first.

    ``limit`` (or legacy ``limit_per``) is a **global** cap, not per workspace.
    ``limit <= 0`` returns every matching index row.
    """
    rows, _total = await _indexed_task_page(
        cap=limit if limit is not None else limit_per,
        status=status,
        home=home,
        include_archived=include_archived,
        kind=kind,
        session=session,
    )
    return rows


async def list_tasks_all_workspaces_with_total(
    *,
    limit: int = 30,
    status: str | None = None,
    home: Path | None = None,
    include_archived: bool = False,
    kind: str | None = None,
    session: str | None = None,
) -> tuple[list[AggTaskRow], int]:
    """Like :func:`list_tasks_all_workspaces` plus the untruncated match count."""
    return await _indexed_task_page(
        cap=limit,
        status=status,
        home=home,
        include_archived=include_archived,
        kind=kind,
        session=session,
    )


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
