"""Controlled execution, cancellation, and result settlement for one agent turn."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.exc import OperationalError

from omni.agent.capabilities import (
    CAPABILITY_TASK_INSPECT,
    CAPABILITY_TASK_REVIEW,
    contract_outputs,
)
from omni.agent.intent_plan import IntentPlan
from omni.agent.plan_result import PlanExecutionResult
from omni.agent.plan_runner_utils import last_tool_step, loop_result_event, plan_summary
from omni.core.execution_control import ExecutionCancelled, ExecutionControl
from omni.core.react_agent import AgentLoopResult, ToolInvocationRecord
from omni.runtime.presentation import ArtifactRef, artifact_refs
from omni.runtime.remaining import (
    failed_canonical_file_debts,
    remaining_contract_files,
    remaining_deliverables,
    remaining_figure,
    remaining_slides,
    remaining_writing,
    survey_closer_eligible,
)
from omni.runtime.task_title import short_task_title
from omni.storage.db import sqlite_busy


@dataclass
class TurnResult:
    text: str
    session_id: str
    task_id: str = ""
    kind: str = "text"
    tool_trace: list[Any] = field(default_factory=list)
    submitted_workflow_ids: list[str] = field(default_factory=list)
    submitted_subtask_ids: list[str] = field(default_factory=list)
    drained_results: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    terminated_reason: str = "done"
    plan_summary: str = ""
    degraded_warnings: list[str] = field(default_factory=list)
    # User-visible footnotes (identical-twin hint). Not degraded, not model context.
    user_notices: list[str] = field(default_factory=list)
    twin_task_id: str = ""
    settlement_status: str = ""
    # Canonical outputs are presentation data, not assistant prose. Keeping them
    # separate prevents the CLI, channels, and task renderer from each inventing
    # another "Saved outputs" section from tool traces.
    artifacts: list[ArtifactRef] = field(default_factory=list)


_TURN_DEGRADATION: ContextVar[list[str] | None] = ContextVar(
    "omni_turn_degradation", default=None
)


def begin_turn_degradation() -> None:
    """Reset turn-local degradation notes for this asyncio task."""
    _TURN_DEGRADATION.set([])


def note_turn_degradation(warning: str) -> None:
    """Record a host-owned degradation that must reach ``finish_turn``."""
    text = str(warning or "").strip()
    if not text:
        return
    current = _TURN_DEGRADATION.get()
    if current is None:
        current = []
        _TURN_DEGRADATION.set(current)
    if text not in current:
        current.append(text)


def turn_degradation_warnings() -> list[str]:
    """Copy of this turn's degradation notes (empty outside a turn)."""
    return list(_TURN_DEGRADATION.get() or [])


def apply_turn_degradation(result: TurnResult) -> TurnResult:
    """Seal turn-local host warnings onto the user-facing result."""
    extra = turn_degradation_warnings()
    if extra:
        result.degraded_warnings = list(
            dict.fromkeys([*result.degraded_warnings, *extra])
        )
    return result


async def artifact_output_refs(store: Any, rows: list[Any]) -> list[ArtifactRef]:
    """Project stored artifact rows into channel-neutral output refs.

    The primary/support split is the load-bearing part: it decides whether a
    file is offered to the reader as a deliverable or kept as a process file, so
    it is spelled once here rather than at each surface that lists outputs.
    """
    if not rows:
        return []
    ordered = sorted(
        rows, key=lambda row: _task_artifact_rank({"kind": row.kind, "title": row.title})
    )
    refs: list[ArtifactRef] = []
    for row in ordered:
        resolved = await store.resolve_path(row.uri)
        path = str(resolved) if resolved is not None else ""
        source = path or str(getattr(row, "rel_path", "") or "")
        suffix = Path(source).suffix.lstrip(".").lower() if source else ""
        refs.append(
            ArtifactRef(
                title=str(row.title or row.kind or "artifact"),
                format=suffix,
                uri=str(row.uri or ""),
                path=path,
                mime=str(getattr(row, "mime", "") or ""),
                size_bytes=int(getattr(row, "size_bytes", 0) or 0),
                presentation_role=_artifact_presentation_role(row, source),
            )
        )
    return refs


def _merge_drained_artifacts(
    artifacts: list[ArtifactRef],
    drained: list[dict[str, Any]],
) -> list[ArtifactRef]:
    """Attach skill-declared files that the store listing has not yet indexed.

    A drained ``single_skill_task`` already finished; its ``result.artifacts``
    are the deliverable. Waiting for a later store walk left the turn with a
    creation receipt and no attachments.
    """
    seen = {(item.uri, item.path) for item in artifacts}
    merged = list(artifacts)
    for item in drained:
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        for ref in artifact_refs(result):
            key = (ref.uri, ref.path)
            if key in seen:
                continue
            seen.add(key)
            merged.append(ref)
    return merged


def _task_contract_inventory(
    rows: list[Any],
    drained: list[dict[str, Any]],
) -> list[Any]:
    """Store rows plus skill-declared files settlement has not indexed yet."""
    return [*rows, *_merge_drained_artifacts([], drained)]


def _mark_unpaid_settlement(result: Any, unpaid_notices: list[str]) -> None:
    """Unpaid named files are degraded, not succeeded — even if settle disagreed."""
    if not unpaid_notices:
        return
    status = str(getattr(result, "settlement_status", "") or "")
    if status in {"", "succeeded", "passed", "pending"}:
        result.settlement_status = "degraded"


_SLIDE_SKILL_NAMES = frozenset({"research-pptx"})


def _slide_skill_already_queued(
    result: AgentLoopResult, drained: list[dict[str, Any]]
) -> bool:
    for item in drained:
        if str(item.get("skill") or "") in _SLIDE_SKILL_NAMES:
            return True
    for record in result.tool_trace:
        if record.name != "run_skill":
            continue
        skill = ""
        if isinstance(record.result, dict):
            skill = str(record.result.get("skill_name") or "")
        if not skill and isinstance(record.arguments, dict):
            skill = str(record.arguments.get("skill_name") or "")
        if skill in _SLIDE_SKILL_NAMES:
            return True
    return False


_MANUSCRIPT_URI_KINDS = frozenset(
    {"paper", "report", "review", "manuscript", "document"}
)


def _is_task_manuscript(artifact: Any) -> bool:
    """Whether this row is a markdown manuscript this task can bind to slides."""
    kind = str(getattr(artifact, "kind", "") or "").lower()
    if kind and kind not in _MANUSCRIPT_URI_KINDS:
        return False
    mime = str(getattr(artifact, "mime", "") or "").lower()
    if mime == "text/markdown":
        return True
    for raw in (
        getattr(artifact, "path", ""),
        getattr(artifact, "rel_path", ""),
        getattr(artifact, "uri", ""),
    ):
        name = str(raw or "").split("?", 1)[0].lower()
        if name.endswith(".md") or name.endswith(".markdown"):
            return True
    return False


async def _resolved_artifact_file(store: Any, uri: str) -> Path | None:
    resolve = getattr(store, "resolve_path", None) if store is not None else None
    if not callable(resolve) or not uri:
        return None
    resolved = await resolve(uri)
    if resolved is None:
        return None
    path = Path(resolved)
    return path if path.is_file() else None


async def _manuscript_uri(store: Any, artifacts: list[Any]) -> str:
    """Durable handle for this task's manuscript, or empty.

    ``research-pptx`` accepts ``artifact://`` or an existing absolute path.
    A store-relative ``rel_path`` is not a handle: the child cwd is not the
    control store, so passing ``artifacts/foo.md`` fails validate_params.
    """
    for artifact in artifacts or []:
        if not _is_task_manuscript(artifact):
            continue
        uri = str(getattr(artifact, "uri", "") or "").strip()
        if uri.startswith("artifact://"):
            resolve = getattr(store, "resolve_path", None) if store is not None else None
            if callable(resolve) and await _resolved_artifact_file(store, uri) is None:
                continue
            return uri
        absolute = str(getattr(artifact, "path", "") or "").strip()
        if absolute:
            path = Path(absolute).expanduser()
            if path.is_absolute() and path.is_file():
                return str(path)
    return ""


async def host_fill_slides(
    *,
    runtime: Any,
    registry: Any | None,
    task_id: str,
    session_id: str,
    user_message: str,
    markdown_uri: str = "",
    drain_tasks: bool,
    channel: str,
) -> dict[str, Any]:
    """Submit the slides provider for a still-owed ``artifact.slides`` debt.

    CLI waits in-turn. IM queues the child so the parent can withhold files
    until the deck exists. ``markdown_uri`` must be ``artifact://`` or an
    absolute path the child can open; omit it when no durable handle exists.
    """
    skill = "research-pptx"
    if registry is not None:
        resolve = getattr(registry, "resolve_capability", None)
        if callable(resolve):
            entry, _rejected = resolve("slides.generate")
            if entry is not None and getattr(entry, "name", ""):
                skill = str(entry.name)
    params: dict[str, Any] = {"topic": user_message}
    if markdown_uri:
        params["markdown_uri"] = markdown_uri
    notify = "" if drain_tasks else (channel or "")
    subtask_id = await runtime.enqueue(
        skill,
        params,
        notify,
        session_id=session_id,
        task_id=task_id,
        queue=not drain_tasks,
    )
    status = "submitted"
    error = ""
    payload: Any = None
    if drain_tasks:
        process = getattr(runtime, "process", None)
        if callable(process):
            await process(subtask_id)
        getter = getattr(runtime, "get_subtask", None)
        task = await getter(subtask_id) if callable(getter) else None
        status = str(getattr(task, "status", "") or status)
        error = str(getattr(task, "error", "") or "")
        payload = getattr(task, "result_json", None) if task is not None else None
    return {
        "subtask_id": subtask_id,
        "skill": skill,
        "status": status,
        "error": error,
        "result": payload,
    }


def ground_task_inspection_result(
    plan: IntentPlan,
    result: AgentLoopResult,
) -> AgentLoopResult:
    """Render task status from ``get_task`` data instead of model prose.

    A model remains useful for resolving which earlier task the user means, but
    status words are protocol facts. Once ``task.inspect`` is selected, the
    final answer is therefore projected deterministically from the latest
    successful ``get_task`` observation. This prevents an existing artifact
    from being paraphrased as proof that a degraded or failed task succeeded.
    """
    if CAPABILITY_TASK_INSPECT not in plan.capability_inputs:
        return result
    payload = next(
        (
            record.result
            for record in reversed(result.tool_trace)
            if record.name == "get_task"
            and record.status == "succeeded"
            and isinstance(record.result, dict)
            and record.result.get("task_id")
            and record.result.get("status")
            and not record.result.get("error")
        ),
        None,
    )
    if payload is None:
        result.kind = "error"
        result.terminated_reason = "task_inspect_ungrounded"
        result.content = (
            "No authoritative task record was returned, so the task status "
            "will not be guessed."
        )
        return result

    result.kind = "text"
    result.terminated_reason = "done"
    result.content = _format_task_inspection(payload)
    return result


def append_task_review_status(
    plan: IntentPlan,
    result: AgentLoopResult,
) -> AgentLoopResult:
    """Append an authoritative per-task status footer to a ``task.review`` answer.

    A review keeps the model's narrative (unlike ``task.inspect``, which the host
    projects verbatim), so the model composes the retrospective. But status words
    are protocol facts: to stop a failed or degraded task from being narrated as a
    success across a long list, the host appends a compact footer built from the
    durable records the turn actually read (``get_task`` / ``list_recent_tasks`` /
    ``search_tasks`` observations). It never rewrites the prose.
    """
    if CAPABILITY_TASK_REVIEW not in plan.capability_inputs:
        return result
    if result.kind != "text":
        return result
    seen: dict[str, dict[str, str]] = {}
    for record in result.tool_trace:
        if record.status != "succeeded" or not isinstance(record.result, dict):
            continue
        payload = record.result
        if record.name == "get_task" and payload.get("task_id"):
            rows: list[dict[str, Any]] = [payload]
        elif record.name in {"list_recent_tasks", "search_tasks"}:
            rows = [
                row
                for row in (payload.get("tasks") or payload.get("matches") or [])
                if isinstance(row, dict)
            ]
        else:
            continue
        for row in rows:
            task_id = str(row.get("task_id") or "")
            if not task_id or task_id in seen:
                continue
            seen[task_id] = {
                "task_id": task_id,
                "status": str(
                    row.get("task_status") or row.get("status") or "unknown"
                ),
                "workspace": str(row.get("workspace") or ""),
                "title": _inspection_text(row.get("title"), limit=80),
            }
    if not seen:
        return result
    lines = ["", "Reviewed tasks (authoritative status):"]
    for row in list(seen.values())[:12]:
        workspace = f" · {row['workspace']}" if row["workspace"] else ""
        title = f" — {row['title']}" if row["title"] else ""
        lines.append(f"- `{row['task_id'][:8]}` **{row['status']}**{workspace}{title}")
    remaining = len(seen) - 12
    if remaining > 0:
        lines.append(f"- {remaining} more task(s).")
    retryable = [
        row
        for row in seen.values()
        if row["status"] in {"failed", "cancelled", "interrupted", "degraded"}
    ]
    if retryable:
        lines.extend(
            [
                "",
                "Recovery candidates (confirm one to create a new linked attempt; the original is preserved):",
            ]
        )
        for row in retryable[:8]:
            title = f" — {row['title']}" if row["title"] else ""
            lines.append(
                f"- `{row['task_id'][:8]}` **{row['status']}**{title}: "
                f"`/task retry {row['task_id'][:8]}`"
            )
        if len(retryable) > 8:
            lines.append(f"- {len(retryable) - 8} more candidate(s); inspect with `/task all`.")
    result.content = (result.content or "").rstrip() + "\n" + "\n".join(lines)
    return result


_INSPECTION_TEXT_LIMIT = 800


def _inspection_text(value: Any, *, limit: int = _INSPECTION_TEXT_LIMIT) -> str:
    """One clean display line: strip native tool markup, collapse ws, truncate.

    A durable summary can contain a provider's native tool-call encoding (DSML)
    that leaked into the content channel, or simply be very long. Rendering it
    verbatim is how a whole ``update_plan`` call became the visible answer
    (incident c60c4c85). Strip the markup at this display boundary and elide the
    middle of an over-long value so the status card stays readable — the tool
    result the model already saw is unchanged; only this projection is bounded.
    """
    from omni.core.llm.native_tool_markup import split_native_tool_markup

    text = " ".join(split_native_tool_markup(str(value or "")).content.split())
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3].rstrip()
    tail = text[-(limit // 3) :].lstrip()
    return f"{head} \u2026 {tail}"


def _format_task_inspection(payload: dict[str, Any]) -> str:
    task_id = str(payload.get("task_id") or "")
    status = str(
        payload.get("task_status") or payload.get("status") or "unknown"
    ).lower()
    summary = _inspection_text(payload.get("summary"))
    failure_reason = _inspection_text(payload.get("failure_reason"))
    descriptions = {
        "succeeded": "completed successfully",
        "degraded": "completed with warnings or missing pieces; this is not a full success",
        "failed": "failed",
        "cancelled": "was cancelled",
        "interrupted": "was interrupted",
        "needs_input": "is waiting for user input",
        "running": "is still running",
        "recovering": "is recovering",
    }
    description = descriptions.get(status, "see the durable task record")
    lines = [f"Task `{task_id[:8]}` status: **{status}** ({description})."]
    # A failed task has no real summary; its ``failure_reason`` is a *diagnosis*,
    # not a result. Labelling it "Why it failed" (never "System summary") stops the
    # prior attempt's error text from being read back as though it were a valid
    # deliverable — the recursive-failure loop behind incident 78071dd2.
    if status == "failed":
        reason = failure_reason or summary
        if reason:
            lines.append(f"\nWhy it failed: {reason}")
    else:
        if summary:
            lines.append(f"\nSystem summary: {summary}")
        if failure_reason:
            lines.append(f"\nNote: {failure_reason}")

    artifacts = [
        item for item in payload.get("artifacts") or [] if isinstance(item, dict)
    ]
    artifacts.sort(key=_task_artifact_rank)
    if artifacts:
        lines.append("\nResult artifacts:")
        for item in artifacts[:6]:
            title = str(item.get("title") or item.get("kind") or "artifact")
            location = _user_visible_artifact_location(item)
            if location:
                lines.append(f"- {title}: `{location}`")
            else:
                lines.append(f"- {title}: saved (path unavailable)")
        remaining = len(artifacts) - 6
        if remaining > 0:
            lines.append(
                f"- {remaining} additional visual-evidence or diagnostic artifact(s)."
            )
    else:
        lines.append("\nNo result artifact was recorded.")
    return "\n".join(lines)


def _user_visible_artifact_location(item: dict[str, Any]) -> str:
    """Filesystem path a person can open. Never an ``artifact://`` handle."""
    for key in ("path", "file", "display_path"):
        raw = str(item.get(key) or "").strip()
        if raw and not raw.startswith("artifact://"):
            return raw
    return ""


def _task_artifact_rank(item: dict[str, Any]) -> tuple[int, str]:
    kind = str(item.get("kind") or "").lower()
    order = {"report": 0, "document": 1, "archive": 2, "data": 3, "figure": 4}
    return order.get(kind, 5), str(item.get("title") or "").casefold()


def _artifact_presentation_role(row: Any, source: str) -> str:
    """Classify durable deliverables without adding a storage migration.

    Producers opt in explicitly through artifact metadata.  The narrow legacy
    fallback recognizes only established compound sidecar suffixes; ordinary
    report titles containing words such as "input" or "provenance" remain
    primary deliverables.
    """
    meta = getattr(row, "meta", {})
    if isinstance(meta, dict):
        declared = str(meta.get("presentation_role") or "").lower()
        if declared in {"primary", "support"}:
            return declared

    name = Path(source).name.casefold() if source else ""
    kind = str(getattr(row, "kind", "") or "").strip().lower()
    if kind in {"provenance", "manifest", "input"} or name.endswith(
        (".provenance.json", ".figure-bundle.json")
    ):
        return "support"
    return "primary"


class TurnCompletion:
    """Own persistence, task settlement, verification, and presentation hooks."""

    def __init__(
        self,
        *,
        tasks: Any,
        task_controller: Any,
        hooks: Any,
        runtime: Any,
        artifacts: Any = None,
        llm: Any = None,
        registry: Any = None,
    ) -> None:
        self._tasks = tasks
        self._task_controller = task_controller
        self._hooks = hooks
        self._runtime = runtime
        self._artifacts = artifacts
        self._llm = llm
        self._registry = registry

    async def _task_outputs(self, task_id: str) -> list[ArtifactRef]:
        """Project canonical task artifacts into channel-neutral output refs."""
        if self._artifacts is None or not task_id:
            return []
        return await artifact_output_refs(
            self._artifacts, await self._artifacts.list_by_task(task_id)
        )

    async def complete_plan(
        self,
        *,
        plan: IntentPlan,
        result: PlanExecutionResult,
        session_id: str,
        user_message: str,
        drain_tasks: bool,
        persist_message: Any,
        record_turn_memory: Any,
        apply_settlement: Any,
        channel: str = "",
    ) -> TurnResult:
        """Settle a deterministic plan result and project it to the turn API."""
        task_id = plan.task_id
        loop_kind: str = (
            "needs_input"
            if result.kind == "needs_input"
            else "error"
            if result.kind == "error"
            else "text"
        )
        loop_result = AgentLoopResult(
            kind=loop_kind,  # type: ignore[arg-type]
            content=result.text,
            tool_trace=list(result.tool_trace),
            terminated_reason=result.terminated_reason,
        )
        fill_warnings = await self._fill_authored_renders(
            plan,
            loop_result,
            result.drained_results,
            submitted=result.submitted_subtask_ids,
            task_id=task_id,
            session_id=session_id,
            drain_tasks=drain_tasks,
            channel=channel,
        )
        fill_warnings.extend(
            await self._honest_unpaid_files(
                plan,
                loop_result,
                result.drained_results,
                submitted=result.submitted_subtask_ids,
                task_id=task_id,
            )
        )
        result.text = loop_result.content
        result.tool_trace = loop_result.tool_trace
        result.degraded_warnings = list(
            dict.fromkeys([*result.degraded_warnings, *fill_warnings])
        )
        artifacts = await self._contract_output_artifacts(
            plan,
            await self._task_outputs(task_id),
            result.drained_results,
        )
        unpaid = [note for note in fill_warnings if "still owes" in note]
        present_status = (
            result.terminated_reason
            if result.terminated_reason in {"cancelled", "interrupted"}
            else "degraded"
            if unpaid
            else ""
        )
        await self._hooks.emit(
            "pre_present",
            task_id=task_id,
            payload={"kind": result.kind, "text": result.text},
        )
        await persist_message(
            session_id,
            "assistant",
            result.text,
            tools=[record.name for record in result.tool_trace],
            kind=result.kind,
            terminated_reason=result.terminated_reason,
            submitted_workflow_ids=result.submitted_workflow_ids,
            submitted_subtask_ids=result.submitted_subtask_ids,
        )
        await record_turn_memory(
            session_id,
            user_message,
            AgentLoopResult(
                kind="needs_input" if result.kind == "needs_input" else "text",
                content=result.text,
                tool_trace=result.tool_trace,
                terminated_reason=result.terminated_reason,
            ),
            task_id=task_id,
        )
        await self._task_controller.finish_turn(
            task_id,
            kind=result.kind,
            text=result.text,
            submitted_workflow_ids=result.submitted_workflow_ids,
            submitted_subtask_ids=result.submitted_subtask_ids,
            drain_tasks=drain_tasks,
            error=result.error,
            task_status=present_status,
        )
        await apply_settlement(task_id, result)
        _mark_unpaid_settlement(result, unpaid)
        await self._hooks.emit(
            "post_present",
            task_id=task_id,
            payload={"kind": result.kind, "status": result.settlement_status},
        )
        return TurnResult(
            text=result.text,
            session_id=session_id,
            task_id=task_id,
            kind=result.kind,
            tool_trace=result.tool_trace,
            submitted_workflow_ids=result.submitted_workflow_ids,
            submitted_subtask_ids=result.submitted_subtask_ids,
            drained_results=result.drained_results,
            terminated_reason=result.terminated_reason,
            plan_summary=result.plan_summary or plan_summary(plan),
            degraded_warnings=list(
                dict.fromkeys([*plan.degraded_warnings, *result.degraded_warnings])
            ),
            user_notices=list(plan.user_notices),
            twin_task_id=plan.twin_task_id,
            settlement_status=result.settlement_status,
            artifacts=artifacts,
        )

    async def complete_react(
        self,
        *,
        plan: IntentPlan,
        result: AgentLoopResult,
        session_id: str,
        user_message: str,
        channel: str,
        drain_tasks: bool,
        emit_tool_event: Any,
        maybe_escalate: Any,
        persist_message: Any,
        record_turn_memory: Any,
        apply_settlement: Any,
    ) -> TurnResult:
        """Settle a ReAct result, including any foreground child executions."""
        task_id = plan.task_id
        result = ground_task_inspection_result(plan, result)
        result = append_task_review_status(plan, result)
        final_status, final_payload = loop_result_event(result)
        await self._tasks.append_event(
            task_id,
            event_type="react.finished",
            status=final_status,
            name="react",
            output_json=final_payload,
            summary=f"react {result.kind}: {result.terminated_reason}",
        )
        submitted = [
            str(record.result["subtask_id"])
            for record in result.tool_trace
            if record.name in {"run_skill", "run_workflow"}
            and isinstance(record.result, dict)
            and record.result.get("subtask_id")
            and record.result.get("status") == "submitted"
        ]
        submitted_workflows = [
            str(record.result["workflow_run_id"])
            for record in result.tool_trace
            if record.name == "run_workflow"
            and isinstance(record.result, dict)
            and record.result.get("workflow_run_id")
        ]
        if result.kind == "escalated" and result.escalated_goal:
            escalated_id = await maybe_escalate(
                result.escalated_goal,
                session_id,
                channel,
                task_id=task_id,
            )
            if escalated_id:
                submitted.append(escalated_id)

        drained = await self._drain_submitted(
            task_id=task_id,
            submitted_workflow_ids=submitted_workflows,
            submitted_subtask_ids=submitted,
            drain_tasks=drain_tasks,
            emit_tool_event=emit_tool_event,
        )
        from omni.agent.plan_runner_utils import apply_retrieve_only_projection

        getter = getattr(self._tasks, "get_task", None)
        task = await getter(task_id) if callable(getter) else None
        ledger_ids = [
            str(item).strip()
            for item in (getattr(task, "source_ids", None) or [])
            if str(item).strip()
        ]
        result.content = apply_retrieve_only_projection(
            plan,
            source_ids=ledger_ids,
            model_text=result.content,
        )
        fill_warnings = await self._fill_authored_renders(
            plan,
            result,
            drained,
            submitted=submitted,
            task_id=task_id,
            session_id=session_id,
            drain_tasks=drain_tasks,
            channel=channel,
        )
        fill_warnings.extend(
            await self._honest_unpaid_files(
                plan,
                result,
                drained,
                submitted=submitted,
                task_id=task_id,
            )
        )
        unpaid = [note for note in fill_warnings if "still owes" in note]
        present_status = final_status
        if unpaid and present_status not in {
            "failed",
            "needs_input",
            "cancelled",
            "interrupted",
        }:
            present_status = "degraded"
        await self._hooks.emit(
            "pre_present",
            task_id=task_id,
            payload={"kind": result.kind, "text": result.content},
        )
        await persist_message(
            session_id,
            "assistant",
            result.content,
            tools=result.tool_names(),
            kind=result.kind,
            terminated_reason=result.terminated_reason,
            iterations=result.total_iterations,
            tool_call_count=result.total_tool_calls,
            failed_or_last_step=last_tool_step(result),
        )
        await record_turn_memory(
            session_id,
            user_message,
            result,
            task_id=task_id,
        )
        artifacts = await self._contract_output_artifacts(plan, await self._task_outputs(task_id), drained)
        await self._task_controller.finish_turn(
            task_id,
            kind=result.kind,
            text=result.content,
            submitted_workflow_ids=submitted_workflows,
            submitted_subtask_ids=submitted,
            drain_tasks=drain_tasks,
            error=result.content if result.kind == "error" else "",
            task_status=present_status,
        )
        turn_result = TurnResult(
            text=result.content,
            session_id=session_id,
            kind=result.kind,
            task_id=task_id,
            tool_trace=result.tool_trace,
            submitted_workflow_ids=submitted_workflows,
            submitted_subtask_ids=submitted,
            drained_results=drained,
            usage=result.total_usage,
            terminated_reason=result.terminated_reason,
            plan_summary=plan_summary(plan),
            degraded_warnings=list(
                dict.fromkeys([*plan.degraded_warnings, *fill_warnings])
            ),
            user_notices=list(plan.user_notices),
            twin_task_id=plan.twin_task_id,
            # Unpaid named files are degraded even when the loop stop looks
            # like ordinary text. apply_settlement may still raise a child
            # failure; honesty then refuses to present succeeded.
            settlement_status=present_status,
            artifacts=artifacts,
        )
        await apply_settlement(task_id, turn_result)
        _mark_unpaid_settlement(turn_result, unpaid)
        await self._hooks.emit(
            "post_present",
            task_id=task_id,
            payload={
                "kind": turn_result.kind,
                "terminated_reason": turn_result.terminated_reason,
            },
        )
        return turn_result

    async def _fill_authored_renders(
        self,
        plan: IntentPlan,
        result: AgentLoopResult,
        drained: list[dict[str, Any]],
        *,
        submitted: list[str],
        task_id: str,
        session_id: str,
        drain_tasks: bool,
        channel: str,
    ) -> list[str]:
        """Render already-authored inputs. Never invent a figure or manuscript."""
        if result.kind in {"error", "needs_input"} or result.terminated_reason in {
            "cancelled",
            "interrupted",
        }:
            return []
        if not task_id:
            return []
        if self._artifacts is None:
            return []
        rows = await self._artifacts.list_by_task(task_id)
        required = list(plan.verification_plan.required_outputs) or list(plan.outputs)
        inventory = _task_contract_inventory(rows, drained)
        remaining = remaining_deliverables(required, inventory)
        notes: list[str] = []
        from omni.agent.figure_runner import unrendered_authored_dot

        if remaining_figure(remaining) and unrendered_authored_dot(inventory):
            notes.extend(
                await self._fill_remaining_figure(
                    plan, result, drained, task_id=task_id, session_id=session_id
                )
            )
            rows = await self._artifacts.list_by_task(task_id)
            inventory = _task_contract_inventory(rows, drained)
            remaining = remaining_deliverables(required, inventory)
        if remaining_slides(remaining) and any(_is_task_manuscript(row) for row in inventory):
            notes.extend(
                await self._fill_remaining_slides(
                    plan,
                    result,
                    drained,
                    submitted=submitted,
                    task_id=task_id,
                    session_id=session_id,
                    drain_tasks=drain_tasks,
                    channel=channel,
                )
            )
        return notes

    async def _honest_unpaid_files(
        self,
        plan: IntentPlan,
        result: AgentLoopResult,
        drained: list[dict[str, Any]],
        *,
        submitted: list[str],
        task_id: str,
    ) -> list[str]:
        """Say what is still owed before persist. Host does not write it."""
        if result.kind in {"error", "needs_input"} or result.terminated_reason in {
            "cancelled",
            "interrupted",
        }:
            return []
        if not task_id:
            return []
        rows: list[Any] = []
        if self._artifacts is not None:
            rows = await self._artifacts.list_by_task(task_id)
        required = list(plan.verification_plan.required_outputs) or list(plan.outputs)
        inventory = _task_contract_inventory(rows, drained)
        unpaid = remaining_contract_files(remaining_deliverables(required, inventory))
        failed = failed_canonical_file_debts(result.tool_trace, drained)
        owed = contract_outputs([str(name) for name in required if name])
        extra = [name for name in failed if name in owed] if owed else list(failed)
        unpaid = list(dict.fromkeys([*unpaid, *extra]))
        slide_debt = remaining_slides(unpaid)
        if slide_debt and submitted:
            unpaid = [name for name in unpaid if name not in slide_debt]
        if not unpaid:
            return []
        notice = (
            f"This task still owes {', '.join(unpaid)} on this task_id. "
            "Say continue or resume to reopen this task. The host will not write the file."
        )
        text = (result.content or "").rstrip()
        result.content = f"{text}\n\n{notice}" if text else notice
        return [notice]

    async def _fill_remaining_figure(
        self,
        plan: IntentPlan,
        result: AgentLoopResult,
        drained: list[dict[str, Any]],
        *,
        task_id: str,
        session_id: str,
    ) -> list[str]:
        """If this task already has unrendered DOT, render it here.

        Does not invent a figure. A PNG from an earlier sibling task is not
        delivery. Waits in-turn (``queue=False``) so IM ``drain_tasks=False``
        does not leave the parent running.
        """
        if result.kind in {"error", "needs_input"} or result.terminated_reason in {
            "cancelled",
            "interrupted",
        }:
            return []
        if not task_id:
            return []
        required = list(plan.verification_plan.required_outputs) or list(plan.outputs)
        rows: list[Any] = []
        if self._artifacts is not None:
            rows = await self._artifacts.list_by_task(task_id)
        inventory = _task_contract_inventory(rows, drained)
        if not remaining_figure(remaining_deliverables(required, inventory)):
            return []
        from omni.agent.figure_runner import host_fill_figure, unrendered_authored_dot

        if not unrendered_authored_dot(inventory):
            return []
        enqueue = getattr(self._runtime, "enqueue", None)
        if not callable(enqueue):
            return ["Host could not fill remaining artifact.figure (runtime unavailable)."]

        try:
            filled = await host_fill_figure(
                runtime=self._runtime,
                registry=self._registry,
                task_id=task_id,
                session_id=session_id,
                user_message=plan.user_message,
                title=short_task_title(plan.user_message),
                source_artifact_path=unrendered_authored_dot(inventory),
            )
        except Exception:  # noqa: BLE001 — host fill is best-effort
            return ["Host could not fill remaining artifact.figure."]
        skill = str(filled.get("skill") or "scientific-figure")
        status = str(filled.get("status") or "")
        drained.append(
            {
                "subtask_id": str(filled.get("subtask_id") or ""),
                "task_id": task_id,
                "object_kind": "skill_execution",
                "object_id": str(filled.get("subtask_id") or ""),
                "skill": skill,
                "status": status or "unknown",
                "result": filled.get("result"),
                "error": filled.get("error") or "",
                "trace": [],
            }
        )
        notes = [f"Host filled remaining artifact.figure via {skill}."]
        if status and status not in {"succeeded", "ok"}:
            notes.append(f"Figure fill ended {status}.")
        if filled.get("error"):
            notes.append(str(filled["error"]))
        return notes

    async def _fill_remaining_writing(
        self,
        plan: IntentPlan,
        result: AgentLoopResult,
        drained: list[dict[str, Any]],
        *,
        task_id: str,
        session_id: str,
    ) -> list[str]:
        """Unused salvage: write a manuscript natively. Not the default path.

        ``complete_react`` / ``complete_plan`` do not call this. Tests may
        still invoke it. The produce path is the model calling ``write_file``.
        """
        if result.kind in {"error", "needs_input"} or result.terminated_reason in {
            "cancelled",
            "interrupted",
        }:
            return []
        if self._llm is None and self._artifacts is None:
            return []
        required = list(plan.verification_plan.required_outputs) or list(plan.outputs)
        rows: list[Any] = []
        if self._artifacts is not None and task_id:
            rows = await self._artifacts.list_by_task(task_id)
        writing = remaining_writing(remaining_deliverables(required, rows))
        if not writing:
            return []
        from omni.core.scientific_progress import this_turn_research_evidence

        retrieve_notes: list[str] = []
        if not this_turn_research_evidence(result.tool_trace, drained):
            if survey_closer_eligible(plan):
                retrieve_notes = await self._host_retrieve_survey(
                    plan, result, drained, task_id=task_id, session_id=session_id
                )
            if not this_turn_research_evidence(result.tool_trace, drained):
                return [
                    f"Host did not fill remaining {writing[0]}: "
                    "no this-turn research evidence.",
                    *retrieve_notes,
                ]
        from omni.runtime.final_synthesis import run_native_synthesis

        deliverable = writing[0]
        try:
            synth = await run_native_synthesis(
                plan.user_message,
                {"deliverable": deliverable, "input": {"topic": short_task_title(plan.user_message)}},
                _react_evidence(result, drained, rows),
                llm=self._llm,
                artifacts=self._artifacts,
                session_id=session_id,
                task_id=task_id,
            )
        except Exception:  # noqa: BLE001 — host fill is best-effort
            return [f"Native synthesis could not fill remaining {deliverable}."]
        draft = str(synth.get("text") or synth.get("draft_markdown") or "").strip()
        stored = bool(synth.get("artifacts") or synth.get("report_uri"))
        # Codex treats a long deliverable as a file. Appending the draft into
        # ``result.content`` made WeChat paste the paper, hit the chat budget,
        # and starve the figure uploads queued behind it.
        if draft and not stored:
            result.content = (
                f"{result.content.rstrip()}\n\n{draft}" if (result.content or "").strip() else draft
            )
        elif stored and not (result.content or "").strip():
            result.content = str(
                synth.get("summary") or "Wrote the manuscript as a file."
            )
        warning = str(synth.get("warning") or "").strip()
        notes = [
            *retrieve_notes,
            f"Host filled remaining {deliverable} via native synthesis.",
        ]
        if warning:
            notes.append(warning)
        return notes

    async def _fill_remaining_slides(
        self,
        plan: IntentPlan,
        result: AgentLoopResult,
        drained: list[dict[str, Any]],
        *,
        submitted: list[str],
        task_id: str,
        session_id: str,
        drain_tasks: bool,
        channel: str,
    ) -> list[str]:
        """If this task already has a manuscript, submit research-pptx here.

        Does not invent a deck. Twin-task files do not satisfy this task's
        required_outputs. IM does not drain inline, so the child is queued
        and the parent stays ``pending_child_task`` until the deck lands.
        """
        if result.kind in {"error", "needs_input"} or result.terminated_reason in {
            "cancelled",
            "interrupted",
        }:
            return []
        if not task_id:
            return []
        if _slide_skill_already_queued(result, drained):
            return []
        required = list(plan.verification_plan.required_outputs) or list(plan.outputs)
        rows: list[Any] = []
        if self._artifacts is not None:
            rows = await self._artifacts.list_by_task(task_id)
        inventory = _task_contract_inventory(rows, drained)
        if not remaining_slides(remaining_deliverables(required, inventory)):
            return []
        if not any(_is_task_manuscript(row) for row in inventory):
            return []
        enqueue = getattr(self._runtime, "enqueue", None)
        if not callable(enqueue):
            return ["Host could not fill remaining artifact.slides (runtime unavailable)."]
        markdown_uri = await _manuscript_uri(self._artifacts, inventory)
        try:
            filled = await host_fill_slides(
                runtime=self._runtime,
                registry=self._registry,
                task_id=task_id,
                session_id=session_id,
                user_message=plan.user_message,
                markdown_uri=markdown_uri,
                drain_tasks=drain_tasks,
                channel=channel,
            )
        except Exception:  # noqa: BLE001 — host fill is best-effort
            return ["Host could not fill remaining artifact.slides."]
        subtask_id = str(filled.get("subtask_id") or "")
        if subtask_id and subtask_id not in submitted:
            submitted.append(subtask_id)
        skill = str(filled.get("skill") or "research-pptx")
        status = str(filled.get("status") or "submitted")
        drained.append(
            {
                "subtask_id": subtask_id,
                "task_id": task_id,
                "object_kind": "skill_execution",
                "object_id": subtask_id,
                "skill": skill,
                "status": status or "unknown",
                "result": filled.get("result"),
                "error": filled.get("error") or "",
                "trace": [],
            }
        )
        notes = [f"Host filled remaining artifact.slides via {skill}."]
        if not markdown_uri and any(_is_task_manuscript(row) for row in rows):
            notes.append(
                "Slides fill omitted markdown_uri: manuscript handle was not a durable URI."
            )
        if drain_tasks and status and status not in {"succeeded", "ok", "submitted"}:
            notes.append(f"Slides fill ended {status}.")
        if filled.get("error"):
            notes.append(str(filled["error"]))
        return notes

    async def _contract_output_artifacts(
        self,
        _plan: IntentPlan,
        artifacts: list[ArtifactRef],
        drained: list[dict[str, Any]],
    ) -> list[ArtifactRef]:
        """This task_id's files only. A twin is a footnote, not a deliverable."""
        return _merge_drained_artifacts(artifacts, drained)

    async def _host_retrieve_survey(
        self,
        plan: IntentPlan,
        result: AgentLoopResult,
        drained: list[dict[str, Any]],
        *,
        task_id: str,
        session_id: str,
    ) -> list[str]:
        """Run literature.search onto this task when the closer still has no evidence."""
        enqueue = getattr(self._runtime, "enqueue", None)
        process = getattr(self._runtime, "process", None)
        get_subtask = getattr(self._runtime, "get_subtask", None)
        if not callable(enqueue) or self._registry is None:
            return []
        from omni.agent.capabilities import CAPABILITY_LITERATURE_SEARCH

        selection = next(
            (
                item
                for item in plan.selected_skills
                if CAPABILITY_LITERATURE_SEARCH in (item.matched_capabilities or [])
            ),
            None,
        )
        skill = str(getattr(selection, "skill", "") or "")
        skill_source = str(getattr(selection, "skill_source", "") or "")
        if not skill:
            entry, _rejected = self._registry.resolve_capability(CAPABILITY_LITERATURE_SEARCH)
            if entry is None:
                return []
            skill = entry.name
        params = dict(plan.capability_inputs.get(CAPABILITY_LITERATURE_SEARCH) or {})
        if not str(params.get("query") or params.get("topic") or "").strip():
            params["query"] = plan.user_message
        if skill_source:
            from omni.skills_runtime.context import SKILL_SOURCE_PARAM

            params = {**params, SKILL_SOURCE_PARAM: skill_source}
        try:
            subtask_id = await enqueue(
                skill,
                params,
                "",
                session_id=session_id,
                task_id=task_id,
            )
            if callable(process):
                await process(subtask_id)
            task = await get_subtask(subtask_id) if callable(get_subtask) else None
        except Exception:  # noqa: BLE001 — closer retrieve is best-effort
            return ["Host could not retrieve literature for the remaining manuscript."]
        if task is None:
            return ["Host literature retrieve returned no subtask."]
        payload = getattr(task, "result_json", None)
        status = str(getattr(task, "status", "") or "")
        error = str(getattr(task, "error", "") or "")
        result.tool_trace.append(
            ToolInvocationRecord(
                name="run_skill",
                arguments={"skill_name": skill, "input": params, "mode": "foreground"},
                result={
                    "status": status,
                    "subtask_id": str(subtask_id),
                    "skill_name": skill,
                    "result": payload,
                },
                status="succeeded" if status in {"succeeded", "ok", "partial"} else "failed",
                error=error or None,
            )
        )
        drained.append(
            {
                "subtask_id": str(subtask_id),
                "task_id": task_id,
                "object_kind": "skill_execution",
                "object_id": str(subtask_id),
                "skill": skill,
                "status": status or "unknown",
                "result": payload if isinstance(payload, dict) else {"result": payload},
                "error": error,
                "trace": list(getattr(task, "trace_log", None) or []),
            }
        )
        if status not in {"succeeded", "ok", "partial"}:
            return [f"Host literature retrieve ended {status}."]
        return [f"Host retrieved literature via {skill}."]

    async def _drain_submitted(
        self,
        *,
        task_id: str,
        submitted_workflow_ids: list[str],
        submitted_subtask_ids: list[str],
        drain_tasks: bool,
        emit_tool_event: Any,
    ) -> list[dict[str, Any]]:
        if not drain_tasks or not (submitted_workflow_ids or submitted_subtask_ids):
            return []
        await self._runtime.drain(on_event=emit_tool_event)
        drained: list[dict[str, Any]] = []
        for workflow_id in submitted_workflow_ids:
            workflow = await self._runtime.get_workflow_run(workflow_id)
            if workflow:
                drained.append(
                    {
                        "workflow_run_id": workflow_id,
                        "task_id": task_id,
                        "object_kind": "workflow_run",
                        "object_id": workflow_id,
                        "kind": "workflow",
                        "status": workflow.status,
                        "result": workflow.result_json,
                        "error": workflow.error,
                        "trace": workflow.trace_log,
                    }
                )
        for subtask_id in submitted_subtask_ids:
            task = await self._runtime.get_subtask(subtask_id)
            if task:
                drained.append(
                    {
                        "subtask_id": subtask_id,
                        "task_id": task_id,
                        "object_kind": "skill_execution",
                        "object_id": subtask_id,
                        "skill": task.skill_name,
                        "status": task.status,
                        "result": task.result_json,
                        "error": task.error,
                        "trace": task.trace_log,
                    }
                )
        return drained


class TurnExecution:
    """Run a turn under durable controls and settle cancellation once."""

    def __init__(self, tasks: Any, task_controller: Any, persist_message: Any) -> None:
        self._tasks = tasks
        self._task_controller = task_controller
        self._persist_message = persist_message

    async def run(
        self,
        *,
        execute: Callable[..., Awaitable[TurnResult]],
        user_message: str,
        session_id: str | None,
        existing_task_id: str,
        on_task_ack: Any,
        execute_kwargs: dict[str, Any],
    ) -> TurnResult:
        acknowledged = {
            "task_id": existing_task_id,
            "session_id": session_id or "",
        }
        if existing_task_id:
            recover_controls = getattr(
                self._tasks,
                "recover_consumed_controls",
                None,
            )
            if callable(recover_controls):
                await recover_controls(existing_task_id)

        async def capture_ack(data: dict[str, Any]) -> None:
            acknowledged["task_id"] = str(data.get("task_id") or acknowledged["task_id"])
            acknowledged["session_id"] = str(
                data.get("session_id") or acknowledged["session_id"]
            )
            if on_task_ack is not None:
                result = on_task_ack(data)
                if inspect.isawaitable(result):
                    await result

        async def read_controls() -> list[dict[str, str]]:
            task_id = acknowledged["task_id"]
            return await self._tasks.consume_controls(task_id) if task_id else []

        async def acknowledge_controls(control_ids: list[str]) -> None:
            marker = getattr(self._tasks, "mark_controls_applied", None)
            if callable(marker):
                await marker(control_ids)

        control = ExecutionControl(
            read_controls,
            acknowledge_controls=acknowledge_controls,
        )
        try:
            result = await control.run(
                execute(
                    user_message,
                    **execute_kwargs,
                    on_task_ack=capture_ack,
                    execution_control=control,
                )
            )
            # Process-local delivery evidence closes the foreground UI race
            # when durable acknowledgement fails after a steer was already
            # injected at a ReAct boundary.
            result._delivered_control_ids = control.delivered_control_ids  # type: ignore[attr-defined]
            if result.terminated_reason == "cancelled":
                from omni.runtime.cancel_persist import run_uncancelled

                async def settle_returned() -> None:
                    task_id = result.task_id or acknowledged["task_id"]
                    await self._settle_cancelled_children(task_id)
                    still_open = getattr(self._tasks, "has_active_children", None)
                    if callable(still_open) and await still_open(task_id):
                        await asyncio.sleep(0.35)
                        await self._settle_cancelled_children(task_id)

                # wait_for() can cancel this wrapper; the checkpoint must land.
                await run_uncancelled(settle_returned, serialize=False)
            return apply_turn_degradation(result)
        except ExecutionCancelled:
            result = await self._stopped_result(
                acknowledged["task_id"],
                acknowledged["session_id"] or session_id or "",
                user_message,
                reason="cancelled",
            )
            result._delivered_control_ids = control.delivered_control_ids  # type: ignore[attr-defined]
            return apply_turn_degradation(result)
        except asyncio.CancelledError:
            # Serve stop / update restart cancels the asyncio task with no
            # durable cancel row. That is process death, not a user /stop.
            reason = "cancelled" if control.durable_cancel else "interrupted"
            result = await self._stopped_result(
                acknowledged["task_id"],
                acknowledged["session_id"] or session_id or "",
                user_message,
                reason=reason,
            )
            result._delivered_control_ids = control.delivered_control_ids  # type: ignore[attr-defined]
            return apply_turn_degradation(result)
        except BaseException as exc:
            exc._delivered_control_ids = control.delivered_control_ids  # type: ignore[attr-defined]
            raise

    async def _settle_cancelled_children(self, task_id: str) -> None:
        """Close leftover open children after a cancelled turn.

        ReAct can return a cancelled result without raising, so the wrapper
        never enters :meth:`_stopped_result`. Child persist may also lose a
        re-cancel from the parent waiter. This write runs on the uncancelled
        turn wrapper.
        """
        if not task_id:
            return
        settler = getattr(self._tasks, "settle_open_children_for_cancel", None)
        if not callable(settler):
            return
        try:
            await settler(task_id)
        except OperationalError as exc:
            # A leftover aiosqlite lock must not replace user cancel with a
            # store failure. The child row is retried by the skill persist.
            if not sqlite_busy(exc):
                raise

    async def _cancelled_result(
        self,
        task_id: str,
        session_id: str,
        user_message: str,
    ) -> TurnResult:
        return await self._stopped_result(
            task_id, session_id, user_message, reason="cancelled"
        )

    async def _stopped_result(
        self,
        task_id: str,
        session_id: str,
        user_message: str,
        *,
        reason: str,
    ) -> TurnResult:
        cancelled = reason == "cancelled"
        text = (
            "Execution cancelled. Completed results and artifacts were preserved."
            if cancelled
            else (
                "Execution was interrupted; the owning process exited. "
                "Completed results and artifacts were preserved."
            )
        )
        summary = (
            "execution cancelled by user"
            if cancelled
            else "execution interrupted; owning process exited"
        )
        warning = (
            f"Cancelled before completing: {user_message[:160]}"
            if cancelled
            else f"Interrupted before completing: {user_message[:160]}"
        )
        from omni.runtime.cancel_persist import run_uncancelled

        async def persist_stop() -> None:
            task = await self._tasks.get_task(task_id) if task_id else None
            if task is None or task.status not in {"running", "recovering"}:
                return
            if session_id:
                await self._persist_message(
                    session_id,
                    "assistant",
                    text,
                    kind="partial",
                    terminated_reason=reason,
                )
            await self._tasks.append_event(
                task_id,
                event_type=f"execution.{reason}",
                status=reason,
                name="execution",
                output_json={"kind": "partial", "terminated_reason": reason},
                summary=summary,
            )
            # Close the ReAct span. complete_react never ran on this path.
            await self._tasks.append_event(
                task_id,
                event_type="react.finished",
                status=reason,
                name="react",
                output_json={"kind": "partial", "terminated_reason": reason},
                summary=f"react partial: {reason}",
            )
            await self._task_controller.finish_turn(
                task_id,
                kind="partial",
                text=text,
                task_status=reason,
            )

        if cancelled:
            # serialize=False so settle can drop persist_lock between busy
            # retries. Still a sibling: wait_for(8) must not abort the sleep.
            async def settle() -> None:
                await self._settle_cancelled_children(task_id)

            await run_uncancelled(settle, serialize=False)
        await run_uncancelled(persist_stop)
        return TurnResult(
            text=text,
            session_id=session_id,
            task_id=task_id,
            kind="partial",
            terminated_reason=reason,
            settlement_status="skipped",
            degraded_warnings=[warning],
        )


def _react_evidence(
    result: AgentLoopResult,
    drained: list[dict[str, Any]],
    artifacts: list[Any],
) -> dict[str, Any]:
    """Pack ReAct observations into the dict native synthesis already consumes."""
    bag: dict[str, Any] = {}
    if (result.content or "").strip():
        bag["react"] = {"summary": result.content.strip()[:2000]}
    for index, item in enumerate(drained):
        payload = item.get("result") if isinstance(item, dict) else None
        if not isinstance(payload, dict):
            payload = {
                "summary": str(
                    (item or {}).get("skill")
                    or (item or {}).get("status")
                    or "upstream step"
                )
            }
        bag[str((item or {}).get("subtask_id") or (item or {}).get("workflow_run_id") or f"step-{index}")] = payload
    if artifacts:
        titles = [
            str(getattr(row, "title", "") or getattr(row, "kind", "") or "artifact")
            for row in artifacts
        ]
        bag["artifacts"] = {"summary": "Existing artifacts: " + ", ".join(titles[:8])}
    return bag


__all__ = [
    "TurnCompletion",
    "TurnExecution",
    "TurnResult",
    "apply_turn_degradation",
    "begin_turn_degradation",
    "note_turn_degradation",
    "turn_degradation_warnings",
]
