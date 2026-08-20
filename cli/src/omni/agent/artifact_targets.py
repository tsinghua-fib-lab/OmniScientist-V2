"""Resolve the concrete artifact target for follow-up and revision turns."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from omni.runtime.session_focus import ActiveTarget, SessionFocusService
from omni.runtime.task_results import _collect_artifacts
from omni.storage.models import MESSAGE_ORDER_DESC, ConversationMessageORM


class ArtifactTargetResolver:
    def __init__(self, *, db: Any, paths: Any, runtime: Any, focus: SessionFocusService, artifacts: Any) -> None:
        self._db = db
        self._paths = paths
        self._runtime = runtime
        self._focus = focus
        self._artifacts = artifacts

    async def active(self, session_id: str) -> ActiveTarget | None:
        target = await self._focus.latest(session_id)
        if target is not None and (
            target.source_path is not None
            or target.skill_execution is not None
            or target.workflow_step is not None
        ):
            return target
        execution = await self._latest_attached_execution(session_id)
        if execution is None:
            return None
        source = await self.execution_dot_path(execution)
        return ActiveTarget(
            focus=SimpleNamespace(
                session_id=session_id,
                target_kind="skill_execution",
                subtask_id=execution.id,
                task_id=getattr(execution, "task_id", ""),
                workflow_run_id=getattr(execution, "workflow_run_id", "") or "",
                workflow_step_id=getattr(execution, "workflow_step_id", "") or "",
                child_task_id="",
                skill_name=execution.skill_name,
                origin="skill_execution_attached",
                artifact_title="",
                source_uri="",
                source_path=str(source or ""),
                artifact_uri="",
                artifact_path="",
                meta={},
            ),
            skill_execution=execution,
            source_path=source,
            artifacts=[],
        )

    async def latest_session_dot_path(self, session_id: str) -> Path | None:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(ConversationMessageORM)
                    .where(ConversationMessageORM.session_id == session_id)
                    .order_by(*MESSAGE_ORDER_DESC)
                    .limit(40)
                )
            ).scalars().all()
        for row in rows:
            for raw in re.findall(r"(/[^`\s)]+?\.dot)\b", row.content or ""):
                path = Path(raw).expanduser()
                if path.is_file() and self._is_workspace_artifact(path):
                    return path
        return None

    async def execution_dot_path(self, execution: Any) -> Path | None:
        for item in _collect_artifacts(getattr(execution, "result_json", None), limit=40):
            path = Path(str(item.get("path") or "")).expanduser()
            uri = str(item.get("uri") or "")
            if path.suffix.lower() == ".dot" and path.is_file():
                return path
            if uri:
                resolved = await self._artifacts.resolve_path(uri)
                if resolved and resolved.suffix.lower() == ".dot" and resolved.is_file():
                    return resolved
        return None

    async def _latest_attached_execution(self, session_id: str) -> Any | None:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(ConversationMessageORM)
                    .where(ConversationMessageORM.session_id == session_id)
                    .order_by(*MESSAGE_ORDER_DESC)
                    .limit(80)
                )
            ).scalars().all()
        for row in rows:
            meta = row.meta or {}
            if meta.get("kind") != "task_attachment":
                continue
            if str(meta.get("attached_object_kind") or "") != "skill_execution":
                continue
            execution_id = str(meta.get("attached_object_id") or "")
            if execution_id:
                execution = await self._runtime.get_subtask(execution_id)
                if execution is not None:
                    return execution
        return None

    def _is_workspace_artifact(self, path: Path) -> bool:
        """True when ``path`` is a managed artifact Omni itself produced.

        Trusted roots are the durable workspace store *and* the trusted launch/
        output directory — where figure bundles (including the ``.dot`` source)
        now live. A ``.dot`` under either is safe to treat as a re-renderable
        source; an arbitrary path scraped from message text is refused.
        """
        resolved = path.resolve()
        roots = [self._paths.artifacts_dir]
        mirror = getattr(self._artifacts, "mirror_dir", None)
        if mirror is not None:
            roots.append(Path(mirror))
        for root in roots:
            try:
                resolved.relative_to(Path(root).resolve())
                return True
            except ValueError:
                continue
        return False


__all__ = ["ArtifactTargetResolver"]
