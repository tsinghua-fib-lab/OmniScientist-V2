"""Typed resolution of task objects across every catalog workspace.

Tasks are the canonical user-facing handle, while workflow runs, workflow
steps, and skill executions remain inspectable drill-down objects.  This
resolver searches all four namespaces together so lookup order can never turn
an ambiguous prefix into an arbitrary object.

Task ids additionally consult the machine-global :class:`TaskIndex` first so an
IM-anchor task that was dual-written to ``control.sqlite3`` still resolves from
any directory — even before the catalog scan opens its workspace DB.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from omni.storage.db import get_database
from omni.storage.models import (
    SubtaskORM,
    TaskORM,
    WorkflowRunORM,
    WorkflowStepORM,
)

if TYPE_CHECKING:
    from omni.config import OmniSettings

logger = logging.getLogger(__name__)

TaskObjectKind = Literal[
    "task",
    "workflow_run",
    "workflow_step",
    "skill_execution",
]
ResolutionStatus = Literal["ok", "not_found", "ambiguous"]


@dataclass(frozen=True, slots=True)
class TaskObjectResolution:
    """Outcome of resolving one id or leading prefix.

    ``settings`` is populated only for a successful match and is already bound
    to the workspace that owns the object.  Failed resolutions deliberately do
    not nominate a workspace, preventing callers from treating a local fallback
    as a successful global lookup.
    """

    status: ResolutionStatus
    object_kind: TaskObjectKind | None = None
    object_id: str = ""
    task_id: str = ""
    settings: OmniSettings | None = None


@dataclass(frozen=True, slots=True)
class _WorkspaceRef:
    db_path: Path
    project_dir: Path
    kind: str
    root: str
    name: str
    local_settings: OmniSettings | None = None

    def load_settings(self) -> OmniSettings:
        if self.local_settings is not None:
            return self.local_settings
        from omni.runtime.task_index import settings_for_workspace

        return settings_for_workspace(kind=self.kind, root=self.root, name=self.name)


@dataclass(frozen=True, slots=True)
class _ObjectMatch:
    kind: TaskObjectKind
    object_id: str
    task_id: str
    workspace: _WorkspaceRef


_OBJECT_TABLES: tuple[tuple[TaskObjectKind, type[Any]], ...] = (
    ("task", TaskORM),
    ("workflow_run", WorkflowRunORM),
    ("workflow_step", WorkflowStepORM),
    ("skill_execution", SubtaskORM),
)


def _workspace_refs(settings: OmniSettings) -> list[_WorkspaceRef]:
    """Return the current workspace plus every distinct catalog workspace."""
    from omni.config.workspaces import iter_catalog_workspaces

    paths = settings.paths
    if paths is None:
        return []
    refs = [
        _WorkspaceRef(
            db_path=paths.project_db,
            project_dir=paths.project_dir,
            kind=(
                "named"
                if paths.workspace_root is None
                else "in-place"
                if paths.project_dir == paths.workspace_root / ".omni"
                else "path"
            ),
            root=str(paths.workspace_root or ""),
            name=paths.project_name,
            local_settings=settings,
        )
    ]
    seen = {str(paths.project_db.resolve())}
    for record in iter_catalog_workspaces(paths.home):
        db_value = str(record.get("db") or "").strip()
        if not db_value:
            continue
        db_path = Path(db_value)
        key = str(db_path.resolve())
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            _WorkspaceRef(
                db_path=db_path,
                project_dir=Path(
                    str(record.get("project_dir") or db_path.parent)
                ),
                kind=str(record.get("kind") or ""),
                root=str(record.get("root") or ""),
                name=str(record.get("name") or db_path.parent.name),
            )
        )
    return refs


def _match(kind: TaskObjectKind, row: Any, workspace: _WorkspaceRef) -> _ObjectMatch:
    object_id = str(row.id)
    task_id = object_id if kind == "task" else str(row.task_id or "")
    return _ObjectMatch(
        kind=kind,
        object_id=object_id,
        task_id=task_id,
        workspace=workspace,
    )


async def _exact_matches(
    workspaces: list[_WorkspaceRef],
    ident: str,
) -> tuple[list[_ObjectMatch], bool]:
    matches: list[_ObjectMatch] = []
    unavailable = False
    for workspace in workspaces:
        if not workspace.db_path.exists():
            continue
        try:
            db = get_database(workspace.db_path)
            await db.init()
            async with db.session() as session:
                for kind, model in _OBJECT_TABLES:
                    row = await session.get(model, ident)
                    if row is not None:
                        matches.append(_match(kind, row, workspace))
        except Exception:  # noqa: BLE001 - one bad store must fail resolution closed
            logger.debug(
                "task object resolver: exact scan skipped %s",
                workspace.db_path,
                exc_info=True,
            )
            unavailable = True
    return matches, unavailable


async def _prefix_matches(
    workspaces: list[_WorkspaceRef],
    ident: str,
) -> tuple[list[_ObjectMatch], bool]:
    matches: list[_ObjectMatch] = []
    unavailable = False
    for workspace in workspaces:
        if not workspace.db_path.exists():
            continue
        try:
            db = get_database(workspace.db_path)
            await db.init()
            async with db.session() as session:
                for kind, model in _OBJECT_TABLES:
                    rows = list(
                        (
                            await session.execute(
                                select(model)
                                .where(model.id.startswith(ident, autoescape=True))
                                .limit(2)
                            )
                        ).scalars().all()
                    )
                    matches.extend(_match(kind, row, workspace) for row in rows)
        except Exception:  # noqa: BLE001 - one bad store must fail resolution closed
            logger.debug(
                "task object resolver: prefix scan skipped %s",
                workspace.db_path,
                exc_info=True,
            )
            unavailable = True
    return matches, unavailable


def _finish(
    matches: list[_ObjectMatch],
    *,
    unavailable: bool,
) -> TaskObjectResolution:
    if unavailable or len(matches) > 1:
        return TaskObjectResolution(status="ambiguous")
    if not matches:
        return TaskObjectResolution(status="not_found")
    match = matches[0]
    try:
        settings = match.workspace.load_settings()
    except Exception:  # noqa: BLE001 - cannot safely route without owner settings
        logger.debug(
            "task object resolver: failed to load workspace %s",
            match.workspace.project_dir,
            exc_info=True,
        )
        return TaskObjectResolution(status="ambiguous")
    return TaskObjectResolution(
        status="ok",
        object_kind=match.kind,
        object_id=match.object_id,
        task_id=match.task_id,
        settings=settings,
    )


async def _resolve_task_via_index(
    settings: OmniSettings, ident: str
) -> TaskObjectResolution | None:
    """Return a task resolution from :class:`TaskIndex` when authoritative.

    Exact index hits short-circuit immediately (after confirming the task row
    still exists). Unique prefix hits are only accepted when a catalog scan
    finds no colliding object of another kind — fail closed on ambiguity.
    Returns ``None`` to fall through to the multi-kind catalog scan.
    """
    from omni.runtime.task_index import TaskIndex, settings_for_workspace

    paths = getattr(settings, "paths", None)
    if paths is None:
        return None
    try:
        row = await TaskIndex(paths.control_db).resolve(ident)
    except Exception:  # noqa: BLE001 - index is best-effort
        logger.debug("task object resolver: TaskIndex resolve failed", exc_info=True)
        return None
    if row is None:
        return None
    try:
        owner = settings_for_workspace(
            kind=row.workspace_kind,
            root=row.workspace_root,
            name=row.workspace_label,
        )
    except Exception:  # noqa: BLE001
        logger.debug("task object resolver: index owner settings failed", exc_info=True)
        return None
    owner_paths = getattr(owner, "paths", None)
    if owner_paths is None or not Path(owner_paths.project_db).exists():
        return None
    try:
        db = get_database(owner_paths.project_db)
        await db.init()
        async with db.session() as session:
            task = await session.get(TaskORM, row.task_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task object resolver: index task verify skipped %s",
            owner_paths.project_db,
            exc_info=True,
        )
        return None
    if task is None:
        return None

    # Exact id (or index returned the full task_id for a unique prefix): accept
    # immediately when the caller asked for the full id.
    if ident == task.id:
        return TaskObjectResolution(
            status="ok",
            object_kind="task",
            object_id=task.id,
            task_id=task.id,
            settings=owner,
        )

    # Prefix path: only trust the index when no other object kind shares it.
    workspaces = _workspace_refs(settings)
    exact, unavailable = await _exact_matches(workspaces, ident)
    if unavailable:
        return TaskObjectResolution(status="ambiguous")
    if exact:
        # An exact hit on some other kind (or a different task) wins over the
        # index prefix — fail closed when more than one exact match.
        return _finish(exact, unavailable=False)
    prefixes, unavailable = await _prefix_matches(workspaces, ident)
    if unavailable:
        return TaskObjectResolution(status="ambiguous")
    if not prefixes:
        # Catalog missed the owning store (should be rare with the anchor in the
        # catalog); the verified index row is still authoritative for this task.
        return TaskObjectResolution(
            status="ok",
            object_kind="task",
            object_id=task.id,
            task_id=task.id,
            settings=owner,
        )
    return _finish(prefixes, unavailable=False)


async def resolve_task_object(
    settings: OmniSettings,
    ident: str,
) -> TaskObjectResolution:
    """Resolve an exact id or globally unique leading prefix.

    Order: TaskIndex (tasks) → exact matches across all object kinds in the
    workspace catalog → unique prefix matches. Multiple exact matches, multiple
    prefix matches, or an unreadable catalog store fail closed as ``ambiguous``.
    """
    ident = (ident or "").strip()
    if not ident:
        return TaskObjectResolution(status="not_found")

    indexed = await _resolve_task_via_index(settings, ident)
    if indexed is not None:
        return indexed

    workspaces = _workspace_refs(settings)
    exact, unavailable = await _exact_matches(workspaces, ident)
    if exact or unavailable:
        return _finish(exact, unavailable=unavailable)
    prefixes, unavailable = await _prefix_matches(workspaces, ident)
    return _finish(prefixes, unavailable=unavailable)


__all__ = [
    "ResolutionStatus",
    "TaskObjectKind",
    "TaskObjectResolution",
    "resolve_task_object",
]
