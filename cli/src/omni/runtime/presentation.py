"""Channel-neutral presentation models for turns and task completions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omni.core.termination import base_termination_reason, is_bounded_termination
from omni.runtime.notifications import TaskNotification
from omni.runtime.task_results import action_required_presentation, is_dot_artifact

_TERMINATION_LABELS = {
    "max_iterations": "iteration limit reached",
    "max_tool_calls": "tool budget reached",
    "max_total_tokens": "token budget reached",
    "max_cost": "cost budget reached",
    "timeout": "execution timed out",
    "no_progress": "tool calls made no further progress",
    "llm_error": "model call failed",
    "llm_transcript_invalid": "model service rejected the tool transcript",
    "llm_auth_error": "model authentication failed",
    "llm_configuration_error": "model configuration is unavailable",
    "llm_rate_limited": "model service rate limited the request",
    "llm_unavailable": "model service is temporarily unavailable",
    "llm_invalid_request": "model service rejected the request",
    "llm_timeout": "model call timed out",
    "artifact_contract_failed": "artifact rendering or validation failed",
    "artifact_revision_failed": "artifact revision failed",
}


def termination_reason_label(reason: str) -> str:
    """Return one channel-neutral, user-safe label for a terminal reason."""
    canonical = base_termination_reason(reason)
    return _TERMINATION_LABELS.get(canonical, canonical or "unknown")


@dataclass(frozen=True)
class ArtifactRef:
    title: str = "artifact"
    format: str = ""
    uri: str = ""
    path: str = ""
    mime: str = ""
    size_bytes: int = 0
    # Inlined body for small text/report artifacts (empty for large/binary ones,
    # which stay link-only). Populated by ``omni.runtime.artifact_preview``.
    preview: str = ""
    preview_truncated: bool = False

    @property
    def target(self) -> str:
        return self.path or self.uri or "-"

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/") or self.format.lower() in {"png", "jpg", "jpeg", "gif", "webp"}

    @property
    def is_markdown(self) -> bool:
        return self.mime == "text/markdown" or self.format.lower() in {"md", "markdown", "report"}

    @property
    def display_format(self) -> str:
        fmt = self.format.lower().lstrip(".")
        if fmt:
            return fmt
        source = self.path or self.uri
        if source and "." in source.rsplit("/", 1)[-1]:
            return source.rsplit(".", 1)[-1].lower()
        return ""


# Process/source files that back a rendered deliverable (diagram sources,
# structured intermediates, logs). DOT sources remain internal and are omitted
# from user-facing results; other sidecars retain their existing treatment.
_SIDECAR_FORMATS = {"dot", "gv", "mmd", "json", "yaml", "yml", "log"}
_SIDECAR_MIMES = {"text/vnd.graphviz", "application/json", "application/yaml"}


def is_sidecar_artifact(ref: ArtifactRef) -> bool:
    """True for process/source artifacts that support a rendered deliverable."""
    return ref.display_format in _SIDECAR_FORMATS or ref.mime.lower() in _SIDECAR_MIMES


def _human_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


@dataclass(frozen=True)
class ResearchRefs:
    source_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    run_id: str = ""  # experiment_run ledger id (research vocabulary)

    @property
    def has_any(self) -> bool:
        return bool(self.source_ids or self.claim_ids or self.evidence_ids or self.run_id)


@dataclass(frozen=True)
class TaskPresentation:
    # Retained as a compatibility alias for older channel/plugin consumers.
    # New code distinguishes the owning Task from the concrete execution object.
    subtask_id: str
    skill: str
    status: str
    summary: str = ""
    details: list[str] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    research: ResearchRefs = field(default_factory=ResearchRefs)
    next_actions: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    contract_level: str = ""
    verification_status: str = ""
    task_id: str = ""
    object_kind: str = "skill_execution"
    object_id: str = ""

    def __post_init__(self) -> None:
        reference_id = self.object_id or self.subtask_id
        if not self.object_id and reference_id:
            object.__setattr__(self, "object_id", reference_id)
        if not self.subtask_id and reference_id:
            object.__setattr__(self, "subtask_id", reference_id)

    @property
    def reference_id(self) -> str:
        """Return the concrete execution object represented by this card."""
        return self.object_id or self.subtask_id

    @property
    def identity_tokens(self) -> list[str]:
        """Return unambiguous short labels for the object and owning Task."""
        labels = {
            "task": "task",
            "workflow_run": "workflow",
            "workflow_step": "step",
            "skill_execution": "execution",
            "scheduled_goal": "schedule",
        }
        tokens: list[str] = []
        if self.reference_id:
            label = labels.get(self.object_kind, "object")
            tokens.append(f"{label}={self.reference_id[:8]}")
        if self.task_id and self.reference_id != self.task_id:
            tokens.append(f"task={self.task_id[:8]}")
        return tokens

    def to_markdown(self, *, include_local_paths: bool = True) -> str:
        """Render the completion; ``include_local_paths=False`` is the IM mode.

        IM recipients cannot open server-side absolute paths, so that mode
        lists artifacts as ``name (format, size)`` — the file itself arrives as
        a native upload — while the CLI keeps full paths and URIs.
        """
        mark = "✅" if self.status == "succeeded" else "❌" if self.status == "failed" else "◷"
        identity = "".join(f" `{token}`" for token in self.identity_tokens)
        lines = [f"{mark} **{self.skill}** ({self.status}){identity}"]
        if self.summary:
            lines += ["", self.summary]
        if self.details:
            lines += ["", "**Result summary**", *[f"- {item}" for item in self.details]]
        if self.error:
            lines += ["", f"Error: {self.error}"]
        if self.contract_level or self.verification_status:
            lines += ["", "**Execution contract**"]
            if self.contract_level:
                lines.append(f"- contract: {self.contract_level}")
            if self.verification_status:
                lines.append(f"- verification: {self.verification_status}")
        visible_artifacts = [art for art in self.artifacts if not is_dot_artifact(art)]
        if visible_artifacts:
            lines += ["", "**Artifacts**"]
            for art in visible_artifacts:
                if include_local_paths:
                    suffix = f" `{art.uri}`" if art.path and art.uri else ""
                    lines.append(f"- {art.title}: {art.target}{suffix}")
                else:
                    lines.append(f"- {_im_artifact_label(art)}")
            for art in visible_artifacts:
                if not art.preview:
                    continue
                heading = f"{art.title} (preview)" if art.preview_truncated else art.title
                lines += ["", f"**{heading}**"]
                if art.is_markdown:
                    lines += ["", art.preview]
                else:
                    lines += ["", "```", art.preview, "```"]
                if art.preview_truncated:
                    opener = art.uri if not include_local_paths else (art.uri or art.target)
                    hint = (
                        f"_Preview truncated; open the full artifact with_ `open_artifact {opener}`."
                        if opener
                        else (
                            f"_Preview truncated; see_ `/task show {self.task_id[:8]}`."
                            if self.task_id
                            else "_Preview truncated._"
                        )
                    )
                    lines += ["", hint]
        if self.research.has_any:
            lines += ["", "**Research record**"]
            if self.research.run_id:
                lines.append(f"- run: `{self.research.run_id[:8]}`")
            if self.research.source_ids:
                lines.append(f"- sources: {', '.join(_short_many(self.research.source_ids))}")
            if self.research.claim_ids:
                lines.append(f"- claims: {', '.join(_short_many(self.research.claim_ids))}")
            if self.research.evidence_ids:
                lines.append(f"- evidence: {', '.join(_short_many(self.research.evidence_ids))}")
        actions = self.next_actions or default_task_actions(
            self.task_id,
            object_kind=self.object_kind,
            object_id=self.reference_id,
        )
        if actions:
            lines += ["", "**Next actions**", *[f"- {a}" for a in actions]]
        return "\n".join(lines)

    def to_plain_text(self) -> str:
        return _strip_markdown(self.to_markdown())


def _im_artifact_label(art: ArtifactRef) -> str:
    meta = ", ".join(part for part in (art.display_format, _human_size(art.size_bytes)) if part)
    label = art.title if not meta else f"{art.title} ({meta})"
    if is_sidecar_artifact(art):
        label += " — process file"
    return label


@dataclass(frozen=True)
class TurnPresentation:
    assistant_text: str
    session_id: str = ""
    task_id: str = ""
    ack: str = ""
    plan_summary: str = ""
    submitted_workflow_ids: list[str] = field(default_factory=list)
    submitted_subtask_ids: list[str] = field(default_factory=list)
    tasks: list[TaskPresentation] = field(default_factory=list)
    degraded_warnings: list[str] = field(default_factory=list)
    verification_status: str = ""
    next_actions: list[str] = field(default_factory=list)

    def to_markdown(self, *, include_local_paths: bool = True) -> str:
        lines: list[str] = []
        if self.ack:
            lines.append(self.ack)
        elif self.task_id and not self.assistant_text.strip():
            lines.append(f"Request accepted: `task_id={self.task_id[:8]}`. Processing has started.")
        if self.plan_summary:
            lines += ["", self.plan_summary] if lines else [self.plan_summary]
        duplicate_submission_summary = (
            bool(self.tasks)
            and self.tasks[0].status in {"submitted", "pending", "running"}
            and self.assistant_text.strip()
            and self.assistant_text.strip() == self.tasks[0].summary.strip()
        )
        if self.assistant_text and not duplicate_submission_summary:
            lines.append(self.assistant_text)
        if self.submitted_workflow_ids and not self.tasks:
            ids = ", ".join(value[:8] for value in self.submitted_workflow_ids)
            lines += ["", f"Submitted workflow run(s): `{ids}`"]
        if self.submitted_subtask_ids and not any(task.status in {"submitted", "pending", "running"} for task in self.tasks):
            ids = ", ".join(t[:8] for t in self.submitted_subtask_ids)
            lines += ["", f"Submitted skill execution(s): `{ids}`"]
        for task in self.tasks:
            lines += ["", task.to_markdown(include_local_paths=include_local_paths)]
        if self.degraded_warnings:
            lines += ["", "**Degraded execution**", *[f"- {item}" for item in self.degraded_warnings]]
        if self.verification_status:
            lines += ["", f"verification: `{self.verification_status}`"]
        actions = self.next_actions
        if actions:
            lines += ["", "**Next actions**", *[f"- {a}" for a in actions]]
        return "\n".join(lines).strip()

    def to_plain_text(self) -> str:
        return _strip_markdown(self.to_markdown())


def artifact_refs(result: dict[str, Any]) -> list[ArtifactRef]:
    out: list[ArtifactRef] = []
    seen: set[str] = set()

    def add(value: Any, *, title: str = "", fmt: str = "") -> None:
        if isinstance(value, dict):
            uri = str(value.get("uri") or value.get("artifact_uri") or "")
            path = str(value.get("path") or value.get("file") or "")
            key = uri or path
            if key and key in seen:
                return
            if key:
                seen.add(key)
            out.append(
                ArtifactRef(
                    title=str(value.get("title") or title or value.get("name") or fmt or "artifact"),
                    format=str(value.get("format") or fmt or ""),
                    uri=uri,
                    path=path,
                    mime=str(value.get("mime") or ""),
                    size_bytes=_coerce_size(value.get("size_bytes")),
                )
            )
        elif isinstance(value, str):
            if value in seen:
                return
            seen.add(value)
            out.append(ArtifactRef(title=title or fmt or "artifact", format=fmt, uri=value))

    def visit(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        for item in payload.get("artifacts") or []:
            add(item)
        for key, value in payload.items():
            if key == "artifacts":
                continue
            if isinstance(value, str) and (value.startswith("artifact://") or key.endswith("_uri")):
                add(value, title=key, fmt=key.removesuffix("_uri"))
            elif key in {"output_uris", "files"} and isinstance(value, list):
                for item in value:
                    add(item, title=key)
        for step in payload.get("steps") or []:
            if isinstance(step, dict):
                visit(step.get("result"))

    visit(result)
    return [ref for ref in out if not is_dot_artifact(ref)]


def _coerce_size(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def workflow_details(result: dict[str, Any]) -> list[str]:
    steps = result.get("steps")
    if not isinstance(steps, list):
        return []
    out: list[str] = []
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        skill = str(step.get("skill_name") or step.get("id") or f"step-{idx}")
        status = str(step.get("status") or "")
        note = str(step.get("summary") or step.get("warning") or step.get("error") or step.get("skip_reason") or "")
        step_result = step.get("result")
        if isinstance(step_result, dict) and not note:
            for key in ("summary", "text", "message", "title", "abstract", "error"):
                if step_result.get(key):
                    note = str(step_result[key])
                    break
        if note:
            note = " ".join(note.split())
            if len(note) > 220:
                note = note[:217].rstrip() + "..."
        label = f"{idx}. {skill}"
        if status:
            label += f" [{status}]"
        if note:
            label += f": {note}"
        out.append(label)
        if len(out) >= 8:
            break
    return out


def research_refs(result: dict[str, Any]) -> ResearchRefs:
    research = result.get("research") if isinstance(result.get("research"), dict) else {}
    return ResearchRefs(
        source_ids=[str(x) for x in research.get("source_ids") or []],
        claim_ids=[str(x) for x in research.get("claim_ids") or []],
        evidence_ids=[str(x) for x in research.get("evidence_ids") or []],
        run_id=str(research.get("run_id") or result.get("run_id") or ""),
    )


def task_presentation_from_result(
    *,
    subtask_id: str,
    task_id: str | None = None,
    object_kind: str = "skill_execution",
    object_id: str = "",
    skill: str,
    status: str,
    result: dict[str, Any] | None = None,
    error: str = "",
    trace: list[dict[str, Any]] | None = None,
) -> TaskPresentation:
    result = result or {}
    reference_id = object_id or subtask_id
    # ``None`` marks a legacy direct caller whose only identifier historically
    # served as the command target. An explicit empty string means that the
    # owner is genuinely unknown and must not create a malformed command.
    owner_task_id = subtask_id if task_id is None else task_id
    summary = str(result.get("text") or result.get("summary") or result.get("message") or result.get("title") or "")
    required_action = action_required_presentation(result)
    required_actions: list[str] = []
    if required_action is not None:
        projected_summary, _, required_actions = required_action
        summary = summary or projected_summary
        if status == "failed":
            status = "needs_input"
            error = ""
    actions = [
        *required_actions,
        *default_task_actions(
            owner_task_id,
            object_kind=object_kind,
            object_id=reference_id,
        ),
    ]
    return TaskPresentation(
        subtask_id=reference_id,
        skill=skill,
        status=status,
        summary=summary,
        details=workflow_details(result),
        artifacts=artifact_refs(result),
        research=research_refs(result),
        next_actions=list(dict.fromkeys(actions)),
        trace=list(trace or []),
        error=error,
        task_id=owner_task_id,
        object_kind=object_kind,
        object_id=reference_id,
    )


def task_presentation_from_notification(note: TaskNotification) -> TaskPresentation:
    payload = note.payload if isinstance(note.payload, dict) else {}
    if note.summary and not payload.get("summary"):
        payload = {**payload, "summary": note.summary}
    if note.artifacts and not payload.get("artifacts"):
        payload = {**payload, "artifacts": note.artifacts}
    return task_presentation_from_result(
        subtask_id=note.reference_id,
        task_id=note.task_id,
        object_kind=note.object_kind,
        object_id=note.reference_id,
        skill=note.display_name,
        status=note.status,
        result=payload,
    )


def submitted_task_presentation_from_tool_result(
    result: dict[str, Any],
    *,
    task_id: str | None = None,
) -> TaskPresentation | None:
    subtask_id = str(result.get("subtask_id") or "")
    if not subtask_id:
        return None
    skill = str(result.get("skill_name") or result.get("skill") or "background-task")
    details: list[str] = []
    details.append(f"Plan decision: use {skill} as the background provider.")
    mode = str(result.get("mode") or "background")
    if mode:
        details.append(f"Execution mode: {mode}")
    planned = str(result.get("planned_skill_name") or "")
    if planned and planned != skill:
        details.append(f"Provider correction: {planned} -> {skill}")
    step_count = result.get("step_count")
    if isinstance(step_count, int) and step_count > 0:
        details.append(f"Workflow steps: {step_count}")
    resolution = result.get("capability_resolution")
    if isinstance(resolution, dict):
        reasons = [str(x) for x in resolution.get("reasons") or [] if str(x)]
        if reasons:
            details.append("Selection reasons: " + "; ".join(reasons[:3]))
        selected_score = resolution.get("selected_score")
        planned_score = resolution.get("planned_score")
        if selected_score is not None and planned_score is not None:
            details.append(f"Match score: selected={selected_score}, planned={planned_score}")
    notify = str(result.get("notify_channel") or "")
    if notify == "cli":
        details.append("Status: running in the background; completion is written to /inbox and shown by the REPL watcher.")
    elif notify:
        details.append(f"Status: running in the background; completion will be sent through {notify}.")
    else:
        details.append(
            "Status: running in the background; use the Task action below after it completes."
        )
    legacy_owner = subtask_id if task_id is None else task_id
    owner_task_id = str(result.get("task_id") or legacy_owner)
    return TaskPresentation(
        subtask_id=subtask_id,
        task_id=owner_task_id,
        object_kind="skill_execution",
        object_id=subtask_id,
        skill=skill,
        status="submitted",
        summary=str(result.get("message") or f"Submitted background task {skill}."),
        details=details,
        next_actions=[
            "/task watch: monitor current-workspace task progress",
            "/inbox: view task completion notifications",
            *_task_navigation_actions(owner_task_id),
        ],
    )


def turn_presentation_from_result(turn: Any, *, channel: str = "cli") -> TurnPresentation:
    task_id = str(getattr(turn, "task_id", "") or "")

    def completion_presentation(data: dict[str, Any]) -> TaskPresentation:
        owner_task_id = task_id or str(data.get("task_id") or "")
        workflow_run_id = str(data.get("workflow_run_id") or "")
        subtask_id = str(data.get("subtask_id") or "")
        object_kind = str(data.get("object_kind") or "")
        if not object_kind:
            object_kind = (
                "workflow_run"
                if workflow_run_id or data.get("kind") == "workflow"
                else "skill_execution"
            )
        object_id = str(data.get("object_id") or workflow_run_id or subtask_id)
        return task_presentation_from_result(
            subtask_id=object_id,
            task_id=owner_task_id,
            object_kind=object_kind,
            object_id=object_id,
            skill=str(
                data.get("skill")
                or ("Research workflow" if object_kind == "workflow_run" else "")
            ),
            status=str(data.get("status", "")),
            result=data.get("result") if isinstance(data.get("result"), dict) else {},
            error=str(data.get("error") or ""),
            trace=data.get("trace") if isinstance(data.get("trace"), list) else [],
        )

    tasks = [
        completion_presentation(d)
        for d in getattr(turn, "drained_results", []) or []
        if isinstance(d, dict)
    ]
    workflows = list(getattr(turn, "submitted_workflow_ids", []) or [])
    submitted = list(getattr(turn, "submitted_subtask_ids", []) or [])
    actions: list[str] = []
    if workflows and not tasks:
        actions = [
            "/task watch: monitor task progress",
            *_task_navigation_actions(
                task_id,
                object_kind="workflow_run",
                object_id=workflows[0],
            ),
        ]
    elif submitted and not tasks:
        tasks = _submitted_tasks_from_trace(turn, submitted, task_id=task_id)
    if submitted and not tasks and not workflows:
        actions = [
            "/task watch: monitor task progress",
            *_task_navigation_actions(task_id),
        ]
    elif tasks:
        actions = []
    text = str(getattr(turn, "text", "") or "")
    kind = str(getattr(turn, "kind", "") or "")
    reason = str(getattr(turn, "terminated_reason", "") or "") or "unknown"
    plan_summary = str(getattr(turn, "plan_summary", "") or "")
    degraded_warnings = list(getattr(turn, "degraded_warnings", []) or [])
    verification_status = str(getattr(turn, "verification_status", "") or "")
    if _is_im_channel(channel):
        plan_summary = ""
        if kind in {"needs_input", "text"}:
            verification_status = ""
    if kind in {"partial", "text"} and is_bounded_termination(reason) and _is_im_channel(channel):
        label = termination_reason_label(reason)
        bounded_notice = (
            f"This turn converged on the available result; some exploration stopped because {label}."
            + (f"\ntask_id: `{task_id[:8]}`" if task_id else "")
        )
        if kind == "text" and text.strip():
            text = f"{text.rstrip()}\n\n{bounded_notice}"
            degraded_warnings = [*degraded_warnings, "This is the best available result within the execution budget."]
        else:
            inspect_hint = (
                f"\nInspect details locally with `/task show {task_id[:8]}`."
                if task_id
                else ""
            )
            text = (
                f"{bounded_notice}\n"
                "Internal tool traces are hidden from chat; the complete audit record is stored in local task events."
                "\nAdd more constraints."
                f"{inspect_hint}"
            )
            degraded_warnings = [
                *degraded_warnings,
                "Internal tool traces are hidden; audit data remains available in task events.",
            ]
        plan_summary = ""
    elif kind == "error":
        prefix = f"Warning: this turn did not complete: {termination_reason_label(reason)}."
        if prefix not in text:
            text = f"{prefix}\n\n{text}".strip()
    return TurnPresentation(
        assistant_text=text,
        session_id=str(getattr(turn, "session_id", "") or ""),
        task_id=task_id,
        submitted_workflow_ids=workflows,
        submitted_subtask_ids=submitted,
        tasks=tasks,
        plan_summary=plan_summary,
        degraded_warnings=degraded_warnings,
        verification_status=verification_status,
        next_actions=actions,
    )


def _is_im_channel(channel: str) -> bool:
    return channel.lower() in {"wechat", "weixin", "feishu", "lark", "dingtalk", "dingding"}


def _submitted_tasks_from_trace(
    turn: Any,
    submitted: list[str],
    *,
    task_id: str,
) -> list[TaskPresentation]:
    by_id: dict[str, TaskPresentation] = {}
    for record in getattr(turn, "tool_trace", []) or []:
        result = getattr(record, "result", None)
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or result.get("phase") or "")
        if status not in {"submitted", "pending"} and result.get("phase") != "submitted":
            continue
        presentation = submitted_task_presentation_from_tool_result(result, task_id=task_id)
        if presentation is not None:
            by_id[presentation.subtask_id] = presentation
    presentations = [by_id[subtask_id] for subtask_id in submitted if subtask_id in by_id]
    for subtask_id in submitted:
        if subtask_id not in by_id:
            presentations.append(TaskPresentation(
                subtask_id=subtask_id,
                task_id=task_id,
                object_kind="skill_execution",
                object_id=subtask_id,
                skill="background-skill",
                status="submitted",
                summary=(
                    f"Submitted background skill execution `{subtask_id[:8]}`. "
                    "Use /inbox after completion."
                ),
                next_actions=[
                    "/task watch: monitor current-workspace task progress",
                    "/inbox: view task completion notifications",
                    *_task_navigation_actions(task_id),
                ],
            ))
    return presentations


def default_task_actions(
    task_id: str,
    *,
    object_kind: str = "",
    object_id: str = "",
) -> list[str]:
    """Build safe follow-up commands from canonical and concrete identities."""
    return [
        *_task_navigation_actions(
            task_id,
            object_kind=object_kind,
            object_id=object_id,
        ),
        "/verify --session: audit this session's research claims and evidence",
    ]


def _task_navigation_actions(
    task_id: str,
    *,
    object_kind: str = "",
    object_id: str = "",
) -> list[str]:
    if not task_id:
        return []
    actions: list[str] = []
    tid = task_id[:8]
    actions.extend([
        f"/task show {tid}: inspect details, trace, and the complete result",
        f"/task attach {tid}: attach the result for follow-up or revision",
    ])
    if object_kind == "workflow_run" and object_id and object_id != task_id:
        actions.append(f"/task show {object_id[:8]}: inspect this workflow run")
    return actions


def _short_many(values: list[str]) -> list[str]:
    return [str(v)[:8] for v in values[:5]]


def _strip_markdown(text: str) -> str:
    return (
        text.replace("**", "")
        .replace("`", "")
        .replace("✅", "✓")
        .replace("❌", "✗")
    )
