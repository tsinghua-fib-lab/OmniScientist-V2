"""Global, cross-workspace task index — the control-plane half of the task store.

Two-tier storage, matching how Codex/opencode persist work:

* **Data layer** — each workspace keeps its heavy per-task data (events,
  subtasks, workflow steps, artifacts, ROM) in its own ``sessions.sqlite3``.
* **Control/index layer** — this module maintains one machine-global table
  (:class:`~omni.storage.models.TaskIndexORM` in ``~/.omni/control.sqlite3``)
  that holds a small row per task, keyed to the *owning workspace*.

That index is what lets any CLI — launched from any directory — both *list*
tasks across every workspace and *route* a task id back to the workspace that
owns it. Without it, ``omni task --all`` (which scans every workspace) lists a
task that ``omni task show <id>`` (which only queried the CWD workspace) then
cannot find — the "global list, local lookup" bug this module removes.

Writes are always best-effort: a control-store failure must never break the
workspace write it mirrors, so every method swallows its own errors.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from omni.storage.db import Database, get_database
from omni.storage.models import TaskIndexORM, TaskORM, _utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omni.config import OmniSettings
    from omni.config.paths import OmniPaths

logger = logging.getLogger(__name__)

# Guards a one-shot legacy backfill per (process, control DB): brand-new tasks
# are dual-written at creation, so a full workspace sweep is only needed once to
# import rows that predate the index.
_reconciled: set[str] = set()


def _workspace_kind(paths: OmniPaths) -> str:
    """Same recipe classification the workspace registry records for a workspace."""
    if paths.workspace_root is None:
        return "named"
    if paths.project_dir == paths.workspace_root / ".omni":
        return "in-place"
    return "path"


def _row_values(
    task: TaskORM,
    *,
    project_dir: str,
    workspace_root: str,
    workspace_kind: str,
    workspace_label: str,
) -> dict[str, Any]:
    """Denormalise a :class:`TaskORM` into the index row's non-PK columns."""
    return {
        "project_dir": project_dir or "",
        "workspace_root": workspace_root or "",
        "workspace_kind": workspace_kind or "",
        "workspace_label": workspace_label or task.project or "",
        "project": task.project or "default",
        "channel": task.channel or "cli",
        "external_key": task.external_key or "",
        "session_id": task.session_id or "",
        "parent_task_id": task.parent_task_id or "",
        "schedule_id": task.schedule_id or "",
        "kind": task.kind or "turn",
        "status": task.status or "",
        "title": task.title or "",
        "created_at": task.created_at,
        "finished_at": task.finished_at,
        "archived_at": task.archived_at,
        "updated_at": _utcnow(),
    }


async def _upsert(session: Any, task: TaskORM, values: dict[str, Any]) -> None:
    stmt = sqlite_insert(TaskIndexORM).values(task_id=task.id, **values)
    stmt = stmt.on_conflict_do_update(index_elements=["task_id"], set_=values)
    await session.execute(stmt)


class TaskIndex:
    """Read/write access to the global task index for one workspace's writes.

    Instances created via :meth:`for_workspace` carry that workspace's identity
    columns and are what :class:`~omni.runtime.task_recorder.TaskRecorder`
    dual-writes through. An identity-less instance (``TaskIndex(control_db)``) is
    fine for read-only ``resolve`` / ``list`` queries.
    """

    def __init__(
        self,
        control_db_path: str | Path,
        *,
        project_dir: str = "",
        workspace_root: str = "",
        workspace_kind: str = "",
        workspace_label: str = "",
    ) -> None:
        self._path = Path(control_db_path)
        self._project_dir = str(project_dir or "")
        self._workspace_root = str(workspace_root or "")
        self._workspace_kind = str(workspace_kind or "")
        self._workspace_label = str(workspace_label or "")
        self._db: Database | None = None

    @classmethod
    def for_workspace(cls, paths: OmniPaths) -> TaskIndex:
        return cls(
            paths.control_db,
            project_dir=str(paths.project_dir),
            workspace_root=str(paths.workspace_root) if paths.workspace_root else "",
            workspace_kind=_workspace_kind(paths),
            workspace_label=paths.project_name,
        )

    async def _store(self) -> Database:
        if self._db is None:
            db = get_database(self._path)
            await db.init()
            self._db = db
        return self._db

    def _values_for(self, task: TaskORM) -> dict[str, Any]:
        return _row_values(
            task,
            project_dir=self._project_dir,
            workspace_root=self._workspace_root,
            workspace_kind=self._workspace_kind,
            workspace_label=self._workspace_label,
        )

    async def record(self, task: TaskORM | None) -> None:
        """Upsert one task's index row (best-effort; never raises to the caller)."""
        if task is None or not getattr(task, "id", ""):
            return
        try:
            store = await self._store()
            async with store.session() as s:
                await _upsert(s, task, self._values_for(task))
                await s.commit()
        except Exception:  # noqa: BLE001 - the index must never break a workspace write.
            logger.debug("task index: record failed for %s", getattr(task, "id", "?"), exc_info=True)

    async def record_many(self, tasks: list[TaskORM]) -> None:
        """Upsert several tasks in one transaction (best-effort batch of :meth:`record`)."""
        rows = [t for t in tasks if getattr(t, "id", "")]
        if not rows:
            return
        try:
            store = await self._store()
            async with store.session() as s:
                for task in rows:
                    await _upsert(s, task, self._values_for(task))
                await s.commit()
        except Exception:  # noqa: BLE001
            logger.debug("task index: batch record failed", exc_info=True)

    async def remove(self, task_ids: list[str]) -> None:
        ids = [str(t) for t in task_ids if t]
        if not ids:
            return
        try:
            store = await self._store()
            async with store.session() as s:
                await s.execute(delete(TaskIndexORM).where(TaskIndexORM.task_id.in_(ids)))
                await s.commit()
        except Exception:  # noqa: BLE001
            logger.debug("task index: remove failed", exc_info=True)

    async def backfill_workspace(self, db: Database, *, marker: Path | None = None) -> None:
        """Import this workspace's existing tasks into the index once (marker-guarded).

        New tasks are dual-written at creation; this only carries rows that were
        created before the index existed. The marker keeps steady-state startup
        free of any control-store work.
        """
        if marker is not None and Path(marker).exists():
            return
        try:
            store = await self._store()
            async with db.session() as src:
                tasks = list((await src.execute(select(TaskORM))).scalars().all())
            if tasks:
                async with store.session() as dst:
                    for task in tasks:
                        await _upsert(dst, task, self._values_for(task))
                    await dst.commit()
        except Exception:  # noqa: BLE001
            logger.debug("task index: workspace backfill failed", exc_info=True)
            return
        if marker is not None:
            try:
                Path(marker).touch()
            except OSError:
                pass

    async def resolve(self, ident: str) -> TaskIndexORM | None:
        """Return the index row for an exact id or a *unique* id prefix, else None."""
        ident = (ident or "").strip()
        if not ident:
            return None
        try:
            store = await self._store()
            async with store.session() as s:
                exact = await s.get(TaskIndexORM, ident)
                if exact is not None:
                    return exact
                rows = list(
                    (
                        await s.execute(
                            select(TaskIndexORM)
                            .where(TaskIndexORM.task_id.like(f"{ident}%"))
                            .limit(2)
                        )
                    ).scalars().all()
                )
            return rows[0] if len(rows) == 1 else None
        except Exception:  # noqa: BLE001
            logger.debug("task index: resolve failed for %s", ident, exc_info=True)
            return None

    async def list(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        include_archived: bool = False,
        session: str | None = None,
        limit: int = 500,
    ) -> list[TaskIndexORM]:
        try:
            store = await self._store()
            async with store.session() as s:
                q = select(TaskIndexORM).order_by(TaskIndexORM.created_at.desc())
                if status:
                    q = q.where(TaskIndexORM.status == status)
                if kind:
                    q = q.where(TaskIndexORM.kind == kind)
                if not include_archived:
                    q = q.where(TaskIndexORM.archived_at.is_(None))
                q = q.limit(max(1, int(limit)))
                rows = list((await s.execute(q)).scalars().all())
        except Exception:  # noqa: BLE001
            logger.debug("task index: list failed", exc_info=True)
            return []
        if session:
            rows = [r for r in rows if (r.session_id or "").startswith(session)]
        return rows


def settings_for_workspace(
    *, kind: str, root: str, name: str, trusted: bool | None = None
) -> OmniSettings:
    """Rebuild :class:`OmniSettings` for a workspace from its registry recipe.

    Mirrors ``HomeService._settings_for_record``: a named ``-P`` project resolves
    by name; a path-keyed / in-place workspace re-resolves from its repo root
    (which deterministically reproduces the same ``project_dir`` even if the repo
    has since moved, since path-keying hashes the absolute root).
    """
    from omni.config import load_settings

    if kind == "named" or not root:
        return load_settings(project=name or "default", trusted=trusted)
    return load_settings(cwd=Path(root), trusted=trusted)


async def _scan_for_task(
    home: Path, ident: str, control_path: Path
) -> OmniSettings | None:
    """Locate ``ident`` by opening each catalog workspace DB directly.

    The self-healing fallback for ids older than the reconcile window (or created
    before any index existed): on a hit it backfills that row into the index and
    returns settings for the owning workspace.
    """
    from omni.config.workspaces import iter_catalog_workspaces

    exact_matches: list[tuple[TaskORM, dict[str, Any], Path]] = []
    prefix_matches: list[tuple[TaskORM, dict[str, Any], Path]] = []
    for rec in iter_catalog_workspaces(home):
        db_path = Path(rec.get("db", ""))
        if not db_path.exists():
            continue
        try:
            db = get_database(db_path)
            await db.init()
            async with db.session() as s:
                task = await s.get(TaskORM, ident)
                if task is not None:
                    exact_matches.append((task, rec, db_path))
                    continue
                rows = list(
                    (
                        await s.execute(
                            select(TaskORM)
                            .where(TaskORM.id.startswith(ident, autoescape=True))
                            .limit(2)
                        )
                    ).scalars().all()
                )
                prefix_matches.extend((row, rec, db_path) for row in rows)
        except Exception:  # noqa: BLE001 - a bad workspace must not break resolution
            logger.debug("task index: scan skipped a workspace", exc_info=True)
            continue
    matches = exact_matches or prefix_matches
    if len(matches) != 1:
        return None
    task, rec, db_path = matches[0]
    project_dir = str(rec.get("project_dir") or db_path.parent)
    workspace_root = str(rec.get("root") or "")
    workspace_kind = str(rec.get("kind") or "")
    workspace_label = str(rec.get("name") or db_path.parent.name)
    store = get_database(control_path)
    await store.init()
    async with store.session() as dst:
        await _upsert(
            dst,
            task,
            _row_values(
                task,
                project_dir=project_dir,
                workspace_root=workspace_root,
                workspace_kind=workspace_kind,
                workspace_label=workspace_label,
            ),
        )
        await dst.commit()
    return settings_for_workspace(
        kind=workspace_kind, root=workspace_root, name=workspace_label
    )


async def reconcile_index(
    home: Path | None = None,
    *,
    control_db: str | Path | None = None,
    force: bool = False,
    limit_per: int = 1000,
) -> None:
    """Backfill the global index from every catalog workspace (idempotent).

    Runs at most once per (process, control DB) unless ``force``; brand-new tasks
    are dual-written, so this only imports rows that predate the index.
    """
    from omni.config.paths import user_home
    from omni.config.workspaces import iter_catalog_workspaces

    home = home or user_home()
    control_path = Path(control_db) if control_db is not None else (home / "control.sqlite3")
    key = str(control_path.resolve())
    if not force and key in _reconciled:
        return
    _reconciled.add(key)
    try:
        store = get_database(control_path)
        await store.init()
    except Exception:  # noqa: BLE001
        return
    for rec in iter_catalog_workspaces(home):
        db_path = Path(rec.get("db", ""))
        if not db_path.exists():
            continue
        try:
            db = get_database(db_path)
            await db.init()
            async with db.session() as src:
                tasks = list(
                    (
                        await src.execute(
                            select(TaskORM)
                            .order_by(TaskORM.created_at.desc())
                            .limit(max(1, int(limit_per)))
                        )
                    ).scalars().all()
                )
            if not tasks:
                continue
            project_dir = str(rec.get("project_dir") or db_path.parent)
            workspace_root = str(rec.get("root") or "")
            workspace_kind = str(rec.get("kind") or "")
            workspace_label = str(rec.get("name") or db_path.parent.name)
            async with store.session() as dst:
                for task in tasks:
                    await _upsert(
                        dst,
                        task,
                        _row_values(
                            task,
                            project_dir=project_dir,
                            workspace_root=workspace_root,
                            workspace_kind=workspace_kind,
                            workspace_label=workspace_label,
                        ),
                    )
                await dst.commit()
        except Exception:  # noqa: BLE001
            logger.debug("task index: reconcile skipped a workspace", exc_info=True)
            continue


async def resolve_task_workspace(settings: OmniSettings, ident: str) -> OmniSettings | None:
    """Settings for the workspace that owns task ``ident`` (index-first, self-healing).

    Returns ``None`` when the id is not a known cross-workspace task (so callers
    fall back to the local workspace unchanged). Order: exact/prefix index hit →
    one-shot reconcile + retry → direct cross-workspace scan.
    """
    from omni.config.paths import user_home

    ident = (ident or "").strip()
    if not ident:
        return None
    paths = getattr(settings, "paths", None)
    home = paths.home if paths is not None else user_home()
    control_path = Path(paths.control_db) if paths is not None else (home / "control.sqlite3")

    index = TaskIndex(control_path)
    row = await index.resolve(ident)
    if row is None:
        await reconcile_index(home, control_db=control_path)
        row = await index.resolve(ident)
    if row is not None:
        return settings_for_workspace(
            kind=row.workspace_kind, root=row.workspace_root, name=row.workspace_label
        )
    return await _scan_for_task(home, ident, control_path)


__all__ = [
    "TaskIndex",
    "reconcile_index",
    "resolve_task_workspace",
    "settings_for_workspace",
]
