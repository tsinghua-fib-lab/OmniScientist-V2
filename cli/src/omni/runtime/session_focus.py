"""Session active-target tracking for follow-up artifact edits.

Long-term memory answers "what has the user cared about before"; session focus
answers "what object does 'this figure' refer to right now". Keep it small,
structured, and deterministic so the planner does not need to guess.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from omni.config.paths import OmniPaths
from omni.runtime.artifact_contracts import contract_for_path
from omni.storage.db import Database
from omni.storage.models import (
    ArtifactORM,
    SessionFocusORM,
    SubtaskORM,
    TaskORM,
    WorkflowRunORM,
    WorkflowStepORM,
)


@dataclass(slots=True)
class ArtifactFocusRef:
    title: str = ""
    uri: str = ""
    path: str = ""
    fmt: str = ""
    kind: str = ""
    mime: str = ""

    @property
    def artifact_id(self) -> str:
        return self.uri.removeprefix("artifact://") if self.uri.startswith("artifact://") else ""

    @property
    def suffix(self) -> str:
        raw = self.path or self.uri
        return Path(raw).suffix.lower()


@dataclass(slots=True)
class ActiveTarget:
    focus: SessionFocusORM
    skill_execution: SubtaskORM | None = None
    workflow_run: WorkflowRunORM | None = None
    workflow_step: WorkflowStepORM | None = None
    child_task: TaskORM | None = None
    source_path: Path | None = None
    artifacts: list[ArtifactFocusRef] = field(default_factory=list)


class SessionFocusService:
    def __init__(self, db: Database, paths: OmniPaths) -> None:
        self._db = db
        self._paths = paths

    async def record_skill_execution_result(
        self,
        *,
        session_id: str,
        skill_execution_id: str,
        skill_name: str,
        result: Any,
        origin: str = "skill_execution_completed",
        task_id: str = "",
        workflow_run_id: str = "",
        workflow_step_id: str = "",
        confidence: float = 0.9,
    ) -> SessionFocusORM | None:
        return await self._record_result(
            session_id=session_id,
            target_kind="skill_execution",
            result=result,
            origin=origin,
            task_id=task_id,
            skill_execution_id=skill_execution_id,
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_id,
            skill_name=skill_name,
            confidence=confidence,
        )

    async def record_skill_execution_attachment(
        self,
        execution: SubtaskORM,
        *,
        session_id: str = "",
    ) -> SessionFocusORM | None:
        return await self.record_skill_execution_result(
            session_id=session_id or execution.session_id,
            skill_execution_id=execution.id,
            skill_name=execution.skill_name,
            result=execution.result_json,
            origin="skill_execution_attached",
            task_id=execution.task_id,
            workflow_run_id=execution.workflow_run_id or "",
            workflow_step_id=execution.workflow_step_id or "",
            confidence=0.96,
        )

    async def record_workflow_attachment(
        self,
        workflow: WorkflowRunORM,
        steps: list[WorkflowStepORM],
        *,
        session_id: str = "",
    ) -> SessionFocusORM | None:
        """Bind follow-up context to the concrete artifact-producing step."""
        step = _workflow_artifact_step(steps)
        result = step.result_json if step is not None else workflow.result_json
        return await self._record_result(
            session_id=session_id or workflow.session_id,
            target_kind="workflow_step" if step is not None else "workflow_run",
            result=result,
            origin="workflow_attached",
            task_id=workflow.task_id,
            workflow_run_id=workflow.id,
            workflow_step_id=step.id if step is not None else "",
            skill_execution_id=step.current_execution_id if step is not None else "",
            child_task_id=step.child_task_id if step is not None else "",
            skill_name=(
                step.skill_name or step.capability or step.deliverable
                if step is not None
                else ""
            ),
            confidence=0.96,
        )

    async def _record_result(
        self,
        *,
        session_id: str,
        target_kind: str,
        result: Any,
        origin: str,
        task_id: str = "",
        workflow_run_id: str = "",
        workflow_step_id: str = "",
        skill_execution_id: str = "",
        child_task_id: str = "",
        skill_name: str = "",
        confidence: float = 0.9,
    ) -> SessionFocusORM | None:
        if not session_id:
            return None
        refs = collect_artifact_refs(result)
        if not refs:
            return None
        source = self._choose_source_ref(refs)
        primary = source or refs[0]
        source_path = await self._resolve_path(source) if source else None
        primary_path = await self._resolve_path(primary)
        meta: dict[str, Any] = {
            "artifacts": [asdict(ref) for ref in refs[:20]],
            "source_format": source.fmt if source else primary.fmt,
        }
        row = SessionFocusORM(
            session_id=session_id,
            target_kind=target_kind,
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_id,
            child_task_id=child_task_id,
            subtask_id=skill_execution_id,
            task_id=task_id,
            skill_name=skill_name,
            origin=origin,
            artifact_id=primary.artifact_id,
            artifact_uri=primary.uri,
            artifact_path=str(primary_path or primary.path or ""),
            artifact_kind=primary.kind or primary.fmt,
            artifact_title=primary.title,
            source_uri=source.uri if source else primary.uri,
            source_path=str((source_path or source.path) if source else (primary_path or primary.path or "")),
            source_kind=source.fmt if source else primary.fmt,
            confidence=confidence,
            meta=meta,
        )
        async with self._db.session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
        return row

    async def record_artifact_revision(
        self,
        *,
        session_id: str,
        skill_execution_id: str,
        skill_name: str,
        artifacts: list[dict[str, Any]],
        task_id: str = "",
        workflow_run_id: str = "",
        workflow_step_id: str = "",
    ) -> SessionFocusORM | None:
        return await self.record_skill_execution_result(
            session_id=session_id,
            skill_execution_id=skill_execution_id,
            skill_name=skill_name,
            result={"artifacts": artifacts},
            origin="artifact_revision",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_id,
            confidence=0.98,
        )

    async def latest(self, session_id: str) -> ActiveTarget | None:
        if not session_id:
            return None
        async with self._db.session() as s:
            focus = (
                await s.execute(
                    select(SessionFocusORM)
                    .where(SessionFocusORM.session_id == session_id, SessionFocusORM.active == 1)
                    .order_by(SessionFocusORM.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if focus is None:
                return None
            execution = await s.get(SubtaskORM, focus.subtask_id) if focus.subtask_id else None
            workflow = (
                await s.get(WorkflowRunORM, focus.workflow_run_id)
                if focus.workflow_run_id
                else None
            )
            step = (
                await s.get(WorkflowStepORM, focus.workflow_step_id)
                if focus.workflow_step_id
                else None
            )
            child_task = (
                await s.get(TaskORM, focus.child_task_id)
                if focus.child_task_id
                else None
            )
        source_path = await self.source_path(focus)
        return ActiveTarget(
            focus=focus,
            skill_execution=execution,
            workflow_run=workflow,
            workflow_step=step,
            child_task=child_task,
            source_path=source_path,
            artifacts=[_ref_from_dict(item) for item in (focus.meta or {}).get("artifacts") or []],
        )

    async def source_path(self, focus: SessionFocusORM) -> Path | None:
        for raw in (focus.source_path, focus.artifact_path):
            if raw:
                path = Path(raw).expanduser()
                if path.is_file():
                    return path
        for uri in (focus.source_uri, focus.artifact_uri):
            path = await self._resolve_uri(uri)
            if path is not None:
                return path
        return None

    def _choose_source_ref(self, refs: list[ArtifactFocusRef]) -> ArtifactFocusRef | None:
        for ref in refs:
            path_like = ref.path or ref.uri
            if path_like and contract_for_path(path_like) is not None:
                return ref
        for ref in refs:
            if ref.fmt.lower() in {"dot", "tex", "md", "markdown", "json", "yaml", "yml"}:
                return ref
        for ref in refs:
            if ref.suffix in {".dot", ".md", ".tex", ".json", ".yaml", ".yml"}:
                return ref
        return refs[0] if refs else None

    async def _resolve_path(self, ref: ArtifactFocusRef | None) -> Path | None:
        if ref is None:
            return None
        if ref.path:
            path = Path(ref.path).expanduser()
            if path.is_file():
                return path
        return await self._resolve_uri(ref.uri)

    async def _resolve_uri(self, uri: str) -> Path | None:
        if not uri:
            return None
        if not uri.startswith("artifact://"):
            path = Path(uri.replace("file://", "")).expanduser()
            return path if path.is_file() else None
        art_id = uri.removeprefix("artifact://")
        async with self._db.session() as s:
            row = await s.get(ArtifactORM, art_id)
        if row is None:
            return None
        full = self._paths.project_dir / row.rel_path
        return full if full.is_file() else None


def collect_artifact_refs(result: Any, *, limit: int = 40) -> list[ArtifactFocusRef]:
    refs: list[ArtifactFocusRef] = []
    seen: set[str] = set()

    def add(value: Any, *, title: str = "", fmt: str = "") -> None:
        ref: ArtifactFocusRef | None = None
        if isinstance(value, dict):
            uri = str(value.get("uri") or value.get("artifact_uri") or "")
            path = str(value.get("path") or value.get("file") or "")
            key = uri or path
            if not key:
                return
            ref = ArtifactFocusRef(
                title=str(value.get("title") or value.get("name") or title or fmt or "artifact"),
                uri=uri,
                path=path,
                fmt=str(value.get("format") or fmt or Path(path or uri).suffix.lstrip(".")),
                kind=str(value.get("kind") or ""),
                mime=str(value.get("mime") or ""),
            )
        elif isinstance(value, str) and (value.startswith("artifact://") or value.startswith("/") or value.startswith("file://")):
            ref = ArtifactFocusRef(title=title or fmt or "artifact", uri=value if value.startswith("artifact://") else "", path="" if value.startswith("artifact://") else value, fmt=fmt)
        if ref is None:
            return
        key = ref.uri or ref.path
        if key in seen:
            return
        seen.add(key)
        refs.append(ref)

    def visit_outputs(obj: Any) -> None:
        """Collect explicitly declared outputs before inspecting legacy shapes.

        Provider assessments and revision metadata can reference artifacts that
        were used as evidence or input.  Those references must not become the
        session's active output merely because they appear before ``artifacts``
        in a result mapping.
        """
        if len(refs) >= limit:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "artifacts":
                    visit_output_value(value)
                else:
                    visit_outputs(value)
                if len(refs) >= limit:
                    break
        elif isinstance(obj, list):
            for item in obj:
                visit_outputs(item)
                if len(refs) >= limit:
                    break

    def visit_output_value(obj: Any, *, title: str = "", fmt: str = "") -> None:
        if len(refs) >= limit:
            return
        if isinstance(obj, dict):
            if any(key in obj for key in ("uri", "artifact_uri", "path", "file")):
                add(obj, title=title, fmt=fmt)
                return
            for key, value in obj.items():
                visit_output_value(
                    value,
                    title=str(obj.get("title") or title or key),
                    fmt=key.removesuffix("_uri") if key.endswith("_uri") else fmt,
                )
                if len(refs) >= limit:
                    break
        elif isinstance(obj, list):
            for item in obj:
                visit_output_value(item, title=title, fmt=fmt)
                if len(refs) >= limit:
                    break
        else:
            add(obj, title=title, fmt=fmt)

    def is_reference_field(key: str) -> bool:
        normalized = key.strip().lower()
        return normalized.startswith("source_") or normalized in {
            "context",
            "deliverable_assessment",
            "effective_inputs",
            "evidence_refs",
            "inputs",
            "revision",
            "revision_of",
        }

    def visit(obj: Any, *, title: str = "", fmt: str = "") -> None:
        if len(refs) >= limit:
            return
        if isinstance(obj, dict):
            if "uri" in obj or "artifact_uri" in obj or "path" in obj or "file" in obj:
                add(obj, title=title, fmt=fmt)
            for key, value in obj.items():
                if key in {"uri", "artifact_uri", "path", "file"}:
                    continue
                if is_reference_field(key):
                    continue
                next_fmt = key.removesuffix("_uri") if key.endswith("_uri") else fmt
                next_title = str(obj.get("title") or title or key)
                if isinstance(value, str) and (value.startswith("artifact://") or key.endswith("_uri")):
                    add(value, title=next_title, fmt=next_fmt)
                else:
                    visit(value, title=next_title, fmt=next_fmt)
        elif isinstance(obj, list):
            for item in obj:
                visit(item, title=title, fmt=fmt)
                if len(refs) >= limit:
                    break
        else:
            add(obj, title=title, fmt=fmt)

    visit_outputs(result)
    if refs:
        return refs
    visit(result)
    return refs


def _workflow_artifact_step(steps: list[WorkflowStepORM]) -> WorkflowStepORM | None:
    candidates = [step for step in steps if collect_artifact_refs(step.result_json)]
    if not candidates:
        return None
    for candidate in reversed(candidates):
        if _is_artifact_figure_step(candidate):
            return candidate
    return candidates[-1]


def _is_artifact_figure_step(step: WorkflowStepORM) -> bool:
    capability = str(step.capability or "").lower()
    if capability.startswith("artifact.") or capability.startswith("figure."):
        return True
    for ref in collect_artifact_refs(step.result_json):
        fmt = (ref.fmt or ref.kind or "").lower()
        suffix = ref.suffix
        if fmt in {"dot", "svg", "png", "figure"} or suffix in {".dot", ".svg", ".png"}:
            return True
    return False


def _ref_from_dict(value: Any) -> ArtifactFocusRef:
    if not isinstance(value, dict):
        return ArtifactFocusRef()
    return ArtifactFocusRef(
        title=str(value.get("title") or ""),
        uri=str(value.get("uri") or ""),
        path=str(value.get("path") or ""),
        fmt=str(value.get("fmt") or ""),
        kind=str(value.get("kind") or ""),
        mime=str(value.get("mime") or ""),
    )


__all__ = ["ActiveTarget", "ArtifactFocusRef", "SessionFocusService", "collect_artifact_refs"]
