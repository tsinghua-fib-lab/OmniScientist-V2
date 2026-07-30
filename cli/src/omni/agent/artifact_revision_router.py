"""Attached-artifact revision routing.

When a turn references the session's active figure, this router decides how to
act on it without dead-ending: try a transactional in-place patch first, and if
that cannot be grounded (or the model explicitly chose a redraw), escalate to a
full redraw task that preserves the source. It also enforces graphviz
derivatives when a ReAct turn wrote a ``.dot`` file directly.

A coordinator over the artifact-target resolver, the task recorder, the
conversation store (persist), turn memory (record), session focus, the subtask
runtime, the skill registry, and workspace storage — extracted from the
orchestrator so the turn path only sees ``apply`` / ``enforce_contracts``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omni.agent.artifact_targets import ArtifactTargetResolver
from omni.agent.artifact_text import artifact_revision_source_payload
from omni.agent.conversation_store import ConversationStore
from omni.agent.turn_execution import TurnResult
from omni.agent.turn_memory import TurnMemory
from omni.core.react_agent import AgentLoopResult
from omni.runtime.artifact_contracts import contract_for_path
from omni.runtime.artifact_revisions import (
    ArtifactRevisionResult,
    ensure_graphviz_derivatives,
    revise_artifact,
)
from omni.runtime.session_focus import ActiveTarget, SessionFocusService
from omni.runtime.subtask_runtime import SubtaskRuntime
from omni.runtime.task_recorder import TaskRecorder
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import Database


class ArtifactRevisionRouter:
    """Route follow-up edits of the session's active figure (minor patch / redraw)."""

    def __init__(
        self,
        *,
        artifact_targets: ArtifactTargetResolver,
        tasks: TaskRecorder,
        conversations: ConversationStore,
        turn_memory: TurnMemory,
        focus: SessionFocusService,
        runtime: SubtaskRuntime,
        registry: SkillRegistry,
        paths,  # noqa: ANN001 - OmniPaths (avoid a heavy import cycle)
        db: Database,
        artifacts: ArtifactStore,
    ) -> None:
        self._artifact_targets = artifact_targets
        self._tasks = tasks
        self._conversations = conversations
        self._turn_memory = turn_memory
        self._focus = focus
        self._runtime = runtime
        self._registry = registry
        self._paths = paths
        self._db = db
        self._artifacts = artifacts

    async def _active_target(self, session_id: str) -> ActiveTarget | None:
        return await self._artifact_targets.active(session_id)

    async def _latest_session_dot_path(self, session_id: str) -> Path | None:
        return await self._artifact_targets.latest_session_dot_path(session_id)

    async def _task_dot_path(self, execution: Any) -> Path | None:
        return await self._artifact_targets.execution_dot_path(execution)

    async def apply(
        self,
        user_message: str,
        *,
        session_id: str,
        channel: str,
        task_id: str = "",
        drain_tasks: bool = False,
        on_tool_event: Any = None,
        force_major: bool = False,
        edit_spec: dict[str, Any] | None = None,
    ) -> TurnResult | None:
        """Edit the active figure without dead-ending: try the in-place patch,
        else auto-escalate to a full redraw. ``None`` only when no active figure,
        so the caller falls back to normal planning."""
        minor = await self._route_minor(
            user_message,
            session_id=session_id,
            task_id=task_id,
            edit_spec=edit_spec,
        )
        if minor is not None and minor.kind != "error":
            return minor
        major = await self._route_major(
            user_message,
            session_id=session_id,
            channel=channel,
            task_id=task_id,
            drain_tasks=drain_tasks,
            on_tool_event=on_tool_event,
            force=force_major or (minor is not None),
        )
        if major is not None:
            return major
        # Nothing could act on an active figure (or none exists). Discard a
        # low-confidence minor error so the turn is not dead-ended.
        return None

    async def _route_minor(
        self,
        user_message: str,
        *,
        session_id: str,
        task_id: str = "",
        edit_spec: dict[str, Any] | None = None,
    ) -> TurnResult | None:
        """Handle small follow-up edits to attached figure artifacts transactionally."""
        target = await self._active_target(session_id)
        if target is None:
            return None
        execution = target.skill_execution
        step = target.workflow_step
        execution_id = str(getattr(execution, "id", "") or target.focus.subtask_id or "")
        source_path = target.source_path if target is not None else None
        if source_path is None:
            source_path = await self._latest_session_dot_path(session_id)
        if source_path is None:
            source_path = await self._task_dot_path(execution) if execution is not None else None
        if source_path is None:
            return None
        focus_skill_name = str(
            getattr(target.focus, "skill_name", "")
            or getattr(execution, "skill_name", "")
            or getattr(step, "skill_name", "")
            or ""
        )
        contract = contract_for_path(source_path)
        if contract is None:
            return None
        try:
            source_path.read_text(encoding="utf-8")
        except OSError:
            return None
        if task_id:
            await self._tasks.append_event(
                task_id,
                event_type="plan.target.artifact",
                status="succeeded",
                name="session_focus",
                subtask_id=execution_id,
                skill_name=focus_skill_name,
                workflow_run_id=str(target.focus.workflow_run_id or ""),
                workflow_step_id=str(target.focus.workflow_step_id or ""),
                output_json={
                    "origin": getattr(target.focus, "origin", "") if target else "",
                    "source_path": str(source_path),
                    "intent_action": "minor_artifact_revision",
                },
                summary=f"resolved artifact target {(execution_id or target.focus.task_id)[:8]}",
            )
        result = await revise_artifact(
            source_path=source_path,
            instruction=user_message,
            paths=self._paths,
            db=self._db,
            artifacts=self._artifacts,
            session_id=session_id,
            task_id=task_id,
            subtask_id=execution_id,
            edit_spec=edit_spec,
        )
        if not result.ok:
            # Patch could not ground/apply: return an observation for
            # ``apply`` to escalate (no terminal message here).
            return TurnResult(
                text=result.message,
                session_id=session_id,
                kind="error",
                tool_trace=[],
                terminated_reason="artifact_revision_failed",
            )
        reason = "artifact_revision_done"
        await self._conversations.persist_message(
            session_id,
            "assistant",
            result.message,
            tools=["artifact_revision"],
            kind="text",
            terminated_reason=reason,
            task_id=result.task_id,
            artifact_revision={
                "status": result.status,
                "source_path": result.source_path,
                "artifacts": result.artifacts,
                "error": result.error,
                "changed_paths": result.changed_paths,
            },
        )
        await self._turn_memory.record(
            session_id,
            user_message,
            AgentLoopResult(kind="text", content=result.message, terminated_reason=reason),
            task_id=task_id,
        )
        await self._focus.record_artifact_revision(
            session_id=session_id,
            skill_execution_id=execution_id,
            skill_name=focus_skill_name,
            artifacts=result.artifacts,
            task_id=result.task_id,
            workflow_run_id=str(target.focus.workflow_run_id or ""),
            workflow_step_id=str(target.focus.workflow_step_id or ""),
        )
        return TurnResult(
            text=result.message,
            session_id=session_id,
            kind="text",
            tool_trace=[],
            terminated_reason=reason,
        )

    async def _route_major(
        self,
        user_message: str,
        *,
        session_id: str,
        channel: str,
        task_id: str = "",
        drain_tasks: bool = False,
        on_tool_event: Any = None,
        force: bool = False,
    ) -> TurnResult | None:
        """Escalate a structural edit of an attached artifact to a redraw task.

        This is an executor, not a router: it acts only when ``force`` is set,
        i.e. the caller already decided a full redraw is right (the model chose
        ``artifact.revise`` and the in-place patch could not be grounded). The
        redraw preserves the source so the figure is not dead-ended or reset.
        """
        target = await self._active_target(session_id)
        if target is None:
            return None
        execution = target.skill_execution
        step = target.workflow_step
        execution_id = str(getattr(execution, "id", "") or target.focus.subtask_id or "")
        focus_skill_name = str(
            getattr(target.focus, "skill_name", "")
            or getattr(execution, "skill_name", "")
            or getattr(step, "skill_name", "")
            or ""
        )
        entry = self._registry.get(focus_skill_name)
        if entry is None:
            capability = str(getattr(step, "capability", "") or "")
            entry = (
                self._registry.resolve_capability(capability)[0]
                if capability
                else None
            )
        if entry is None:
            entry = self._registry.resolve_capability("artifact.figure")[0]
        if entry is None:
            return None
        skill_name = entry.name
        revision_meta = entry.artifact_revision if isinstance(entry.artifact_revision, dict) else {}
        supports = {str(x) for x in revision_meta.get("supports") or []}
        if supports and "major_revision" not in supports:
            return None
        source_dot = target.source_path if target is not None else None
        if source_dot is None:
            source_dot = await self._latest_session_dot_path(session_id)
        if source_dot is None:
            source_dot = await self._task_dot_path(execution) if execution is not None else None
        # Routing is the caller's job (model chose artifact.revise, or an
        # explicit in-place edit could not be grounded). Without a real model
        # there is no keyword classifier to guess "major" from, so an
        # unforced offline turn does not escalate — it degrades to a placeholder.
        if not force:
            return None
        if task_id:
            await self._tasks.append_event(
                task_id,
                event_type="plan.target.artifact",
                status="succeeded",
                name="session_focus",
                subtask_id=execution_id,
                skill_name=skill_name,
                workflow_run_id=str(target.focus.workflow_run_id or ""),
                workflow_step_id=str(target.focus.workflow_step_id or ""),
                output_json={
                    "origin": getattr(target.focus, "origin", "") if target else "",
                    "source_path": str(source_dot or ""),
                    "intent_action": "major_artifact_revision",
                    "workflow_step_id": str(target.focus.workflow_step_id or ""),
                    "child_task_id": str(target.focus.child_task_id or ""),
                },
                summary=f"resolved artifact target {(execution_id or target.focus.task_id)[:8]}",
            )
        source_task_id = str(target.focus.child_task_id or execution_id or target.focus.task_id)
        source_object_kind = (
            "task"
            if target.focus.child_task_id or not execution_id
            else "execution"
        )
        params = {
            "input": (
                f"Revise the artifact produced by attached task {source_task_id}. "
                f"Preserve its domain and source structure. User requirement: {user_message}"
            ),
            "source_task_id": source_task_id,
        }
        if target.focus.workflow_run_id:
            params["workflow_run_id"] = str(target.focus.workflow_run_id)
        if target.focus.workflow_step_id:
            params["workflow_step_id"] = str(target.focus.workflow_step_id)
        if source_dot is not None:
            params["source_artifact_path"] = str(source_dot)
            try:
                source_text = source_dot.read_text(encoding="utf-8")
                params.update(artifact_revision_source_payload(source_dot, source_text))
            except OSError:
                pass
        subtask_id = await self._runtime.enqueue(
            skill_name,
            params,
            channel,
            session_id=session_id,
            task_id=task_id,
        )
        if drain_tasks:
            await self._runtime.process(subtask_id, on_event=on_tool_event)
        task_action = (
            f"- Use `/inbox` for completion notifications or `/task show {task_id[:8]}` for full results."
            if task_id
            else "- Use `/inbox` for completion notifications."
        )
        text = (
            f"Submitted `{skill_name}` revision task (execution `id={subtask_id[:8]}`) "
            "for the attached artifact.\n"
            f"- Source {source_object_kind}: {source_task_id[:8]}\n"
            + (f"- Workflow step: {params['workflow_step_id']}\n" if params.get("workflow_step_id") else "")
            + (f"- Source DOT: {source_dot}\n" if source_dot is not None else "")
            + task_action
        )
        await self._conversations.persist_message(
            session_id,
            "assistant",
            text,
            tools=["run_skill"],
            kind="text",
            terminated_reason="major_revision_submitted",
            submitted_task_id=subtask_id,
            source_task_id=source_task_id,
        )
        await self._turn_memory.record(
            session_id,
            user_message,
            AgentLoopResult(kind="text", content=text, terminated_reason="major_revision_submitted"),
            task_id=task_id,
        )
        return TurnResult(
            text=text,
            session_id=session_id,
            submitted_subtask_ids=[subtask_id],
            terminated_reason="major_revision_submitted",
        )

    async def enforce_contracts(
        self,
        result: AgentLoopResult,
        *,
        session_id: str,
    ) -> ArtifactRevisionResult | None:
        dirty: list[Path] = []
        for record in result.tool_trace:
            if record.name not in {"write_file", "edit_file"}:
                continue
            path = Path(str(record.arguments.get("path") or "")).expanduser()
            if path.suffix.lower() == ".dot":
                dirty.append(path)
        if not dirty:
            return None
        return await ensure_graphviz_derivatives(
            dot_paths=dirty,
            paths=self._paths,
            db=self._db,
            session_id=session_id,
            reason=result.terminated_reason,
            mirror_dir=self._artifacts.mirror_dir,
        )
