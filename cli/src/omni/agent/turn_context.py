"""Short-horizon context binding for one user turn.

Long-term memory is useful for preferences and project facts, but follow-up
phrases such as "this figure", "that paper", and "the previous result" need a
small deterministic focus object before semantic planning. This module builds
that object from session focus and recent artifacts, then stores it as run
events for auditability.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from omni.config.paths import OmniPaths
from omni.runtime.session_focus import ActiveTarget, SessionFocusService
from omni.runtime.taskref import extract_task_ids
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import Database


@dataclass(frozen=True, slots=True)
class ContextTarget:
    kind: str
    label: str
    confidence: float
    origin: str = ""
    skill_execution_id: str = ""
    child_task_id: str = ""
    workflow_run_id: str = ""
    workflow_step_id: str = ""
    task_id: str = ""
    skill_name: str = ""
    artifact_uri: str = ""
    artifact_path: str = ""
    source_uri: str = ""
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContextArtifact:
    title: str
    uri: str
    kind: str
    path: str = ""
    subtask_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TurnContext:
    session_id: str
    channel: str
    user_message: str
    active_target: ContextTarget | None = None
    referenced_task_ids: list[str] = field(default_factory=list)
    recent_artifacts: list[ContextArtifact] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "channel": self.channel,
            "active_target": self.active_target.to_dict() if self.active_target else None,
            "referenced_task_ids": list(self.referenced_task_ids),
            "recent_artifacts": [item.to_dict() for item in self.recent_artifacts],
            "notes": list(self.notes),
        }

    def to_planner_summary(self, *, max_chars: int = 1800) -> str:
        lines: list[str] = []
        if self.active_target is not None:
            target = self.active_target
            parts = [
                f"kind={target.kind}",
                f"label={target.label}",
                f"confidence={target.confidence:.2f}",
            ]
            if target.task_id:
                parts.append(f"task={target.task_id[:8]}")
            if target.workflow_run_id:
                parts.append(f"workflow={target.workflow_run_id[:8]}")
            if target.child_task_id:
                parts.append(f"child_task={target.child_task_id[:8]}")
            if target.workflow_step_id:
                parts.append(f"workflow_step={target.workflow_step_id}")
            if target.skill_execution_id:
                parts.append(f"skill_execution={target.skill_execution_id[:8]}")
            if target.skill_name:
                parts.append(f"skill={target.skill_name}")
            if target.source_path:
                parts.append(f"source_path={target.source_path}")
            elif target.source_uri:
                parts.append(f"source_uri={target.source_uri}")
            lines.append("Active target: " + "; ".join(parts))
        if self.referenced_task_ids:
            lines.append("Explicit task references: " + ", ".join(self.referenced_task_ids[:5]))
        if self.recent_artifacts:
            lines.append("Recent session artifacts:")
            for artifact in self.recent_artifacts[:5]:
                label = artifact.title or artifact.kind or "artifact"
                location = artifact.uri or artifact.path
                lines.append(f"- {label}: {location}")
        if self.notes:
            lines.append("Context notes: " + "; ".join(self.notes[:4]))
        text = "\n".join(lines)
        if not text:
            return ""
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """Read-only estimate of the context carried into the next model turn."""

    session_id: str
    model: str
    stored_messages: int
    active_messages: int
    prompt_messages: int
    compacted_messages: int
    transcript_tokens: int
    injected_tokens: int
    context_window_tokens: int
    injected_blocks: dict[str, int] = field(default_factory=dict)
    focus_kind: str = ""
    focus_label: str = ""

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        model: str,
        stored_messages: int,
        active_messages: int,
        prompt_history: list[dict[str, Any]],
        compacted_messages: int,
        injected_text: dict[str, str],
        context_window_tokens: int,
        focus: Any = None,
    ) -> ContextSnapshot:
        """Build token metrics from the bounded prompt inputs."""
        from omni.memory.compaction import estimate_messages_tokens, estimate_tokens

        block_tokens = {
            name: estimate_tokens(text.strip())
            for name, text in injected_text.items()
            if text.strip()
        }
        focus_label = next(
            (
                str(getattr(focus, field_name, ""))
                for field_name in ("artifact_title", "skill_name", "artifact_kind")
                if getattr(focus, field_name, "")
            ),
            "",
        )
        return cls(
            session_id=session_id,
            model=model,
            stored_messages=stored_messages,
            active_messages=active_messages,
            prompt_messages=len(prompt_history),
            compacted_messages=compacted_messages,
            transcript_tokens=estimate_messages_tokens(prompt_history),
            injected_tokens=sum(block_tokens.values()),
            context_window_tokens=context_window_tokens,
            injected_blocks=block_tokens,
            focus_kind=str(getattr(focus, "artifact_kind", "") or ""),
            focus_label=focus_label,
        )

    @property
    def total_tokens(self) -> int:
        return self.transcript_tokens + self.injected_tokens

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.context_window_tokens - self.total_tokens)

    @property
    def utilization_pct(self) -> float:
        if self.context_window_tokens <= 0:
            return 0.0
        return min(100.0, self.total_tokens * 100 / self.context_window_tokens)

    @property
    def clearable_tokens(self) -> int:
        """Tokens removed by starting a clean session; durable blocks remain."""
        return self.transcript_tokens

    def render(self, *, near_compaction_threshold: bool = False) -> str:
        """Render CLI-neutral token diagnostics for `/context`."""
        near = " (near automatic compaction threshold)" if near_compaction_threshold else ""
        lines = [
            f"Context snapshot · session {self.session_id[:8]}",
            f"  model: {self.model or '-'}",
            f"  transcript: {self.prompt_messages} prompt / {self.active_messages} active / "
            f"{self.stored_messages} stored messages; {self.compacted_messages} compacted; "
            f"~{self.transcript_tokens:,} tokens{near}",
            f"  prompt estimate: ~{self.total_tokens:,} / {self.context_window_tokens:,} tokens "
            f"({self.utilization_pct:.1f}% used; ~{self.remaining_tokens:,} remaining)",
        ]
        if self.focus_label:
            kind = f" ({self.focus_kind})" if self.focus_kind else ""
            lines.append(f"  focus: {self.focus_label}{kind}")
        if self.injected_blocks:
            lines.append(f"  injected context: ~{self.injected_tokens:,} tokens")
            lines.extend(f"    - {name}: ~{tokens:,}" for name, tokens in self.injected_blocks.items())
        else:
            lines.append("  injected context: none")
        lines.append(
            f"  /clear starts a clean session and saves ~{self.clearable_tokens:,} transcript "
            "tokens; tasks, artifacts, research records, and durable memory remain."
        )
        return "\n".join(lines)


class TurnContextAssembler:
    """Build the bounded context visible to planner/runtime for one turn."""

    def __init__(
        self,
        *,
        db: Database,
        paths: OmniPaths,
        focus: SessionFocusService,
        artifacts: ArtifactStore,
    ) -> None:
        self._db = db
        self._paths = paths
        self._focus = focus
        self._artifacts = artifacts

    async def assemble(
        self,
        *,
        session_id: str,
        channel: str,
        user_message: str,
        artifact_limit: int = 8,
    ) -> TurnContext:
        active = await self._active_target(session_id)
        recent = await self._recent_artifacts(session_id, limit=artifact_limit)
        refs = extract_task_ids(user_message or "")
        notes: list[str] = []
        if active is not None:
            notes.append(f"session focus from {active.origin or 'unknown'}")
        if refs:
            notes.append("user explicitly referenced task/run ids")
        return TurnContext(
            session_id=session_id,
            channel=channel,
            user_message=user_message,
            active_target=active,
            referenced_task_ids=refs,
            recent_artifacts=recent,
            notes=notes,
        )

    async def _active_target(self, session_id: str) -> ContextTarget | None:
        target = await self._focus.latest(session_id)
        if target is None:
            return None
        return _context_target(target)

    async def _recent_artifacts(self, session_id: str, *, limit: int) -> list[ContextArtifact]:
        rows = await self._artifacts.list_by_session(session_id, limit=limit)
        out: list[ContextArtifact] = []
        for row in rows:
            full = self._paths.project_dir / row.rel_path if row.rel_path else Path()
            out.append(
                ContextArtifact(
                    title=row.title,
                    uri=row.uri,
                    kind=row.kind,
                    path=str(full) if row.rel_path else "",
                    subtask_id=row.subtask_id,
                )
            )
        return out


def _context_target(target: ActiveTarget) -> ContextTarget:
    focus = target.focus
    label = (
        str(getattr(focus, "artifact_title", "") or "")
        or str(getattr(focus, "skill_name", "") or "")
        or str(getattr(focus, "artifact_kind", "") or "")
        or "current target"
    )
    source_path = str(target.source_path or getattr(focus, "source_path", "") or "")
    artifact_path = str(getattr(focus, "artifact_path", "") or "")
    kind = str(getattr(focus, "artifact_kind", "") or Path(source_path or artifact_path).suffix.lstrip(".") or "artifact")
    return ContextTarget(
        kind=kind,
        label=label,
        confidence=float(getattr(focus, "confidence", 0.0) or 0.0),
        origin=str(getattr(focus, "origin", "") or ""),
        skill_execution_id=str(getattr(focus, "subtask_id", "") or ""),
        child_task_id=str(getattr(focus, "child_task_id", "") or ""),
        workflow_run_id=str(getattr(focus, "workflow_run_id", "") or ""),
        workflow_step_id=str(getattr(focus, "workflow_step_id", "") or ""),
        task_id=str(getattr(focus, "task_id", "") or ""),
        skill_name=str(getattr(focus, "skill_name", "") or ""),
        artifact_uri=str(getattr(focus, "artifact_uri", "") or ""),
        artifact_path=artifact_path,
        source_uri=str(getattr(focus, "source_uri", "") or ""),
        source_path=source_path,
    )
