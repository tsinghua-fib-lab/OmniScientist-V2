"""Channel-neutral presentation models for turns and task completions."""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from omni.core.termination import (
    TERMINATION_LABELS,
    is_bounded_termination,
    termination_reason_label,
)
from omni.runtime.notifications import TaskNotification
from omni.runtime.task_results import action_required_presentation, is_dot_artifact
from omni.runtime.turn_outcome import display_warnings

# ``omni.core.termination`` is the single owner of the terminal vocabulary.
# These aliases keep the existing presentation-layer import path working.
_TERMINATION_LABELS = TERMINATION_LABELS
MAX_PRESENTED_ARTIFACTS = 12


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
    # ``primary`` is a user deliverable; ``support`` is provenance, an input
    # snapshot, a bundle manifest, or another machine-facing sidecar.
    presentation_role: str = "primary"

    @property
    def target(self) -> str:
        return self.path or self.uri or "-"

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/") or self.format.lower() in {"png", "jpg", "jpeg", "gif", "webp"}

    @property
    def is_markdown(self) -> bool:
        """Whether the body is a document to render rather than text to quote.

        Reads through ``display_format`` so a report stored with an empty
        ``format`` still counts on the strength of its ``.md`` path; producers
        that write the file and let the extension speak were otherwise having
        their deliverable fenced as if it were command output.
        """
        return self.mime == "text/markdown" or self.display_format in {"md", "markdown", "report"}

    @property
    def display_format(self) -> str:
        fmt = self.format.lower().lstrip(".")
        if fmt:
            return fmt
        source = self.path or self.uri
        if source and "." in source.rsplit("/", 1)[-1]:
            return source.rsplit(".", 1)[-1].lower()
        return ""

    @property
    def is_primary(self) -> bool:
        return self.presentation_role != "support"

    def to_dict(self) -> dict[str, Any]:
        """Return the channel-neutral, JSON-safe artifact projection."""
        return {
            "title": self.title,
            "format": self.format,
            "uri": self.uri,
            "path": self.path,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
            "presentation_role": self.presentation_role,
        }


# Process/source files that back a rendered deliverable (diagram sources,
# structured intermediates, logs). DOT sources remain internal and are omitted
# from user-facing results; other sidecars retain their existing treatment.
_SIDECAR_FORMATS = {"dot", "gv", "mmd", "json", "yaml", "yml", "log"}
_SIDECAR_MIMES = {"text/vnd.graphviz", "application/json", "application/yaml"}


def is_sidecar_artifact(ref: ArtifactRef) -> bool:
    """True for process/source artifacts that support a rendered deliverable."""
    return ref.display_format in _SIDECAR_FORMATS or ref.mime.lower() in _SIDECAR_MIMES


def presentable_artifacts(artifacts: list[ArtifactRef]) -> list[ArtifactRef]:
    """Return declared primary deliverables that belong in user-facing output."""
    return [
        artifact
        for artifact in artifacts
        if artifact.is_primary and not is_dot_artifact(artifact)
    ]


def _collect_output_artifacts(presentation: object) -> list[ArtifactRef]:
    """Primary deliverables this card would list, uncapped.

    A turn that already named its outputs lists those and only those — the same
    list the CLI Outputs table prints. A turn that named none still has files on
    nested task cards (``/task`` summaries, ``everything from today``); those
    cards are the inventory, matching how ``to_markdown`` keeps their artifacts
    when the turn itself listed nothing.
    """
    primary = presentable_artifacts(list(getattr(presentation, "artifacts", None) or []))
    tasks = getattr(presentation, "tasks", None)
    if primary or not tasks:
        return primary
    collected: list[ArtifactRef] = []
    seen: set[str] = set()
    for task in tasks:
        for artifact in presentable_artifacts(list(getattr(task, "artifacts", None) or [])):
            key = artifact.path or artifact.uri
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            collected.append(artifact)
    return collected


def output_inventory(presentation: object) -> list[ArtifactRef]:
    """The files CLI Outputs and IM attachments both ship, in that order."""
    return _collect_output_artifacts(presentation)[:MAX_PRESENTED_ARTIFACTS]


def artifact_attachment_keys(artifact: ArtifactRef) -> set[str]:
    """Stable identities used to decide whether a file was already uploaded."""
    return {key for key in (artifact.uri, artifact.path) if key}


def inventory_attachment_keys(presentation: object) -> set[str]:
    """Union of uri/path keys the reader would receive from this card."""
    keys: set[str] = set()
    for artifact in output_inventory(presentation):
        keys.update(artifact_attachment_keys(artifact))
    return keys


def drop_delivered_attachments(
    presentation: object, delivered: set[str]
) -> object:
    """Keep only files this channel has not already uploaded."""
    if not delivered:
        return presentation
    artifacts = list(getattr(presentation, "artifacts", None) or [])
    kept = [
        artifact
        for artifact in artifacts
        if not (artifact_attachment_keys(artifact) & delivered)
    ]
    if kept == artifacts:
        return presentation
    return replace(presentation, artifacts=kept)


def turn_covers_deliverables(presentation: object) -> bool:
    """Whether this card attached any files.

    A later skill notice uses the recorded uri/path set, not this boolean, to
    decide what is still owed. The boolean only answers "did this send hand
    the reader a file" — a pending-child IM turn withholds on purpose so the
    completion notice can carry the full inventory once.
    """
    return bool(output_inventory(presentation))


def promises_later_deliverables(text: str) -> bool:
    """True when the answer tells the reader a file is still coming.

    Settlement must match that copy: if a child is actually running, IM
    withholds the files that are already ready so the completion notice can
    hand the full inventory over once.
    """
    raw = str(text or "")
    if not raw:
        return False
    lowered = raw.casefold()
    needles = (
        "files will be sent",
        "remaining deliverables are still running",
        "\u53ef\u4ee5\u901a\u8fc7\u5b50\u4efb\u52a1",
        "\u751f\u6210\u5b8c\u6210\u540e",
        "\u53d6\u56de .pptx",
        "\u53d6\u56de.pptx",
    )
    return any(needle in raw or needle in lowered for needle in needles)


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
    settlement_status: str = ""
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
        mark = (
            "✅" if self.status == "succeeded"
            else "❌" if self.status == "failed"
            else "!" if self.status == "degraded"
            else "◷"
        )
        identity = "".join(f" `{token}`" for token in self.identity_tokens)
        lines = [f"{mark} **{self.skill}** ({self.status}){identity}"]
        if self.summary:
            lines += ["", self.summary]
        if self.details:
            lines += ["", "**Result summary**", *[f"- {item}" for item in self.details]]
        if self.error:
            lines += ["", f"Error: {self.error}"]
        if self.contract_level or self.settlement_status:
            lines += ["", "**Execution contract**"]
            if self.contract_level:
                lines.append(f"- contract: {self.contract_level}")
            if self.settlement_status:
                lines.append(f"- verification: {self.settlement_status}")
        all_visible_artifacts = _collect_output_artifacts(self)
        visible_artifacts = output_inventory(self)
        if visible_artifacts:
            lines += ["", "**Artifacts**"]
            for art in visible_artifacts:
                if include_local_paths:
                    lines.append(f"- **{artifact_display_label(art.title)}**: `{art.target}`")
                else:
                    lines.append(f"- {_chat_artifact_line(art)}")
            for art in visible_artifacts:
                if not art.preview:
                    continue
                title = artifact_display_label(art.title)
                heading = f"{title} (preview)" if art.preview_truncated else title
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
            remaining = len(all_visible_artifacts) - len(visible_artifacts)
            if remaining > 0:
                suffix = (
                    f"; inspect `/task show {self.task_id[:8]}`"
                    if self.task_id
                    else ""
                )
                lines.append(
                    f"- {remaining} additional artifact(s){suffix}."
                )
        elif self.status in {"failed", "degraded", "partial", "needs_input"}:
            lines += ["", "**Artifacts**", "- No saved artifact was produced."]
        # Ledger ids are for the reader who can look them up. A chat reader
        # cannot: the run id names a row in a store on someone else's machine,
        # and it arrived under a heading of its own beneath the deliverable
        # they had actually asked for.
        if self.research.has_any and include_local_paths:
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


# Acronyms a ``.capitalize()`` would ruin. Everything else title-cases.
_LABEL_WORDS = {
    "pptx": "PPTX", "ppt": "PPT", "pdf": "PDF", "svg": "SVG", "png": "PNG",
    "jpg": "JPG", "jpeg": "JPEG", "docx": "DOCX", "csv": "CSV", "json": "JSON",
    "html": "HTML", "dot": "DOT", "md": "Markdown", "id": "ID",
    # A key names the field, not the deliverable: ``pptx_uri`` is a PPTX.
    "uri": "", "uris": "", "url": "", "path": "", "file": "",
}


def artifact_display_label(raw: str) -> str:
    """Humanise an artifact label so a raw result key never reaches the reader.

    ``pptx_uri`` reached the artifact list verbatim because that producer
    attached its deliverable as a bare ``artifact://`` string field rather than
    an artifact record, leaving the reader to infer the output type from a
    Python identifier. Anything carrying capitals or spaces was written by a
    human ("Scientific Figure SVG") and is passed through untouched.
    """
    text = str(raw or "").strip()
    if not text or text != text.lower() or " " in text:
        return text or "artifact"
    if text in {"artifact", "artifacts"}:
        return text  # our own placeholder for an unlabelled output
    words = [_LABEL_WORDS.get(word, word.capitalize()) for word in re.split(r"[_\-.]+", text)]
    return " ".join(word for word in words if word) or "artifact"


def _chat_artifact_line(art: ArtifactRef) -> str:
    """Name one deliverable and say where it is, on one line.

    This was two blocks: an inventory of names and sizes, then a second list
    repeating the same names against their paths. A reader on a phone had to
    scroll between them and match by title to learn where the file they had
    just been handed lives, and a reply carrying three outputs said each of
    their names twice before saying anything about any of them.

    The path is for the owner of the machine that ran the work, who has
    somewhere to paste it; it is collected here rather than left in the prose,
    where a path mid-sentence is unreadable and the attachment beside the
    message is what the recipient acts on. The terminal prints its locations
    inline and never reaches this.
    """
    label = _im_artifact_label(art)
    return f"{label}: `{art.path}`" if art.path else label


def _im_artifact_label(art: ArtifactRef) -> str:
    meta = ", ".join(part for part in (art.display_format, _human_size(art.size_bytes)) if part)
    title = artifact_display_label(art.title)
    label = title if not meta else f"{title} ({meta})"
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
    artifacts: list[ArtifactRef] = field(default_factory=list)
    degraded_warnings: list[str] = field(default_factory=list)
    user_notices: list[str] = field(default_factory=list)
    settlement_status: str = ""
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
            and self.assistant_text.strip()
            and self.assistant_text.strip() == self.tasks[0].summary.strip()
        )
        if self.assistant_text and not duplicate_submission_summary:
            lines.append(self.assistant_text)
        all_visible_outputs = _collect_output_artifacts(self)
        visible_outputs = output_inventory(self)
        if visible_outputs:
            lines += ["", "**Outputs**"]
            for artifact in visible_outputs:
                if include_local_paths:
                    location = artifact.path or "saved output (inspect the task for its local path)"
                    lines.append(
                        f"- **{artifact_display_label(artifact.title)}**: `{location}`"
                    )
                else:
                    lines.append(f"- {_chat_artifact_line(artifact)}")
            remaining = len(all_visible_outputs) - len(visible_outputs)
            if remaining > 0:
                suffix = (
                    f"; inspect `/task show {self.task_id[:8]}`"
                    if self.task_id
                    else ""
                )
                lines.append(f"- {remaining} additional artifact(s){suffix}.")
        # A second identifier for the same request, under a heading of its own.
        # The terminal reader can look an execution up; in a thread the task is
        # what this work is referred to by, and the reply has already named it —
        # the one that prompted this quoted both ids before saying anything.
        if include_local_paths:
            if self.submitted_workflow_ids and not self.tasks:
                ids = ", ".join(value[:8] for value in self.submitted_workflow_ids)
                lines += ["", f"Submitted workflow run(s): `{ids}`"]
            if self.submitted_subtask_ids and not any(
                task.status in {"submitted", "pending", "running"} for task in self.tasks
            ):
                ids = ", ".join(t[:8] for t in self.submitted_subtask_ids)
                lines += ["", f"Submitted skill execution(s): `{ids}`"]
        for task in self.tasks:
            rendered_task = replace(task, artifacts=[]) if visible_outputs else task
            lines += ["", rendered_task.to_markdown(include_local_paths=include_local_paths)]
        if self.user_notices:
            lines += ["", *self.user_notices]
        if self.degraded_warnings:
            lines += ["", "**Degraded execution**", *[f"- {item}" for item in self.degraded_warnings]]
        if self.settlement_status:
            lines += ["", f"verification: `{self.settlement_status}`"]
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
            role = str(value.get("presentation_role") or "").strip().lower()
            if role not in {"primary", "support"}:
                label = str(value.get("title") or title or "").strip().lower()
                source = path.replace("\\", "/").lower()
                role = (
                    "support"
                    if label in {"manifest_uri", "provenance_uri", "input_uri"}
                    or source.endswith((".provenance.json", ".figure-bundle.json"))
                    else "primary"
                )
            out.append(
                ArtifactRef(
                    # ``kind`` before ``format``: a producer that named neither a
                    # title nor a key still said what the thing is ("report"),
                    # which reads better than its file extension.
                    title=str(
                        value.get("title")
                        or title
                        or value.get("name")
                        or value.get("kind")
                        or fmt
                        or "artifact"
                    ),
                    format=str(value.get("format") or fmt or ""),
                    uri=uri,
                    path=path,
                    mime=str(value.get("mime") or ""),
                    size_bytes=_coerce_size(value.get("size_bytes")),
                    presentation_role=role,
                )
            )
        elif isinstance(value, str):
            if value in seen:
                return
            seen.add(value)
            normalized_title = str(title or "").strip().lower()
            role = (
                "support"
                if normalized_title in {"manifest_uri", "provenance_uri", "input_uri"}
                else "primary"
            )
            out.append(
                ArtifactRef(
                    title=title or fmt or "artifact",
                    format=fmt,
                    uri=value,
                    presentation_role=role,
                )
            )

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
    raw_actions = result.get("next_actions")
    declared_actions = (
        [str(item).strip() for item in raw_actions if str(item).strip()]
        if isinstance(raw_actions, list)
        else []
    )
    setup_command = str(result.get("setup_command") or "").strip()
    if setup_command:
        declared_actions = [
            setup_command,
            *[item for item in declared_actions if item != setup_command],
        ]
    actions = [
        *required_actions,
        *declared_actions,
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


def task_presentation_from_notification(
    note: TaskNotification, *, channel: str = "cli"
) -> TaskPresentation:
    """Render a finished task for the channel that will carry the news.

    A completion arrives on its own, with none of the shaping a reply gets on
    the way out, so a chat reader was handed the full follow-up menu: the
    skill's suggestions and the host's, six lines of commands under a result
    they had asked for once — and, on task cbffcbb6, the whole report as well.
    """
    payload = note.payload if isinstance(note.payload, dict) else {}
    if note.summary and not payload.get("summary"):
        payload = {**payload, "summary": note.summary}
    if note.artifacts and not payload.get("artifacts"):
        payload = {**payload, "artifacts": note.artifacts}
    presentation = task_presentation_from_result(
        subtask_id=note.reference_id,
        task_id=note.task_id,
        object_kind=note.object_kind,
        object_id=note.reference_id,
        skill=note.display_name,
        status=note.status,
        result=payload,
    )
    if not _is_im_channel(channel):
        return presentation
    return replace(
        presentation,
        summary=_chat_task_body(payload, presentation),
        next_actions=_task_inspect_action(
            presentation.task_id or presentation.subtask_id
        ),
        settlement_status="",
        contract_level="",
    )


def _chat_task_body(payload: dict[str, Any], presentation: TaskPresentation) -> str:
    """What a finished task says in a chat thread.

    ``task_presentation_from_result`` reads the body from ``text`` before
    ``summary``, which is right at a terminal: ``/task show`` is where the whole
    result is meant to be legible. research-ideation returns both — a
    thirty-two-thousand character report and an eighty-character summary of it —
    and the report went to WeChat, eighteen bubbles of it, of which upstream
    accepted ten. The report was already a file; only its summary was missing.

    A body that fits is left alone, so nothing changes for the tasks that were
    never the problem. Past that the skill's own summary is preferred over any
    cut this could make of the long one.
    """
    body = presentation.summary
    if len(body) <= _CHAT_ANSWER_BUDGET:
        return body
    stated = str(payload.get("summary") or "").strip()
    if stated and len(stated) <= _CHAT_ANSWER_BUDGET:
        return stated
    return _bounded_answer(
        body,
        has_attachments=bool(presentable_artifacts(presentation.artifacts)),
        task_id=presentation.task_id or presentation.subtask_id,
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


def turn_presentation_from_result(
    turn: Any,
    *,
    channel: str = "cli",
    output_roots: Sequence[Path] = (),
) -> TurnPresentation:
    """Render one turn for one channel.

    The inventory is the task's, on every channel, so a chat reader is handed
    what ``/task show`` would list for the same task and nothing else. This once
    completed a chat reply from the whole conversation, on the theory that a
    follow-up opens a new task and so a reply reporting three finished materials
    would ship only the one file that turn had touched. The reply that motivated
    it was reporting materials that did not exist: the survey it named was
    written later, by the task the user finally asked for it in. Widening the
    inventory did not correct a scope error, it dressed an over-claim in whatever
    files were nearby — and once the conversation held a second topic, that meant
    yesterday's research reports.

    ``output_roots`` are the directories this channel may send files from. A chat
    channel passes them so a reply that names a deliverable it did not produce
    this turn can still arrive with the file attached; the terminal has no use for
    them, because its reader is already sitting next to the file. That lookup is
    reserved for turns that produced nothing themselves — see
    :func:`may_fetch_named_files`, which is the last way an over-claim could still
    hand over another task's work.
    """
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
    artifacts = [
        artifact
        for artifact in (getattr(turn, "artifacts", []) or [])
        if isinstance(artifact, ArtifactRef)
    ]
    raw_text = str(getattr(turn, "text", "") or "")
    if _is_im_channel(channel):
        if may_fetch_named_files(artifacts):
            artifacts = [
                *artifacts,
                *artifacts_named_in_text(raw_text, roots=output_roots),
            ]
    text = project_artifact_locations(
        raw_text,
        artifacts,
        include_local_paths=not _is_im_channel(channel),
    )
    kind = str(getattr(turn, "kind", "") or "")
    reason = str(getattr(turn, "terminated_reason", "") or "") or "unknown"
    plan_summary = str(getattr(turn, "plan_summary", "") or "")
    degraded_warnings = display_warnings(turn)
    user_notices = [str(item) for item in (getattr(turn, "user_notices", None) or []) if str(item).strip()]
    settlement_status = str(getattr(turn, "settlement_status", "") or "")
    if _is_im_channel(channel):
        plan_summary = ""
        if kind == "needs_input":
            settlement_status = ""
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
    chat = _is_im_channel(channel)
    text = _compose_channel_answer(
        text,
        _host_cards_from_turn(turn, chat=chat),
        chat=chat,
        echoed=_is_observation_echo(text, turn),
    )
    presentation = TurnPresentation(
        assistant_text=text,
        session_id=str(getattr(turn, "session_id", "") or ""),
        task_id=task_id,
        submitted_workflow_ids=workflows,
        submitted_subtask_ids=submitted,
        tasks=tasks,
        artifacts=artifacts,
        plan_summary=plan_summary,
        degraded_warnings=degraded_warnings,
        user_notices=user_notices,
        settlement_status=settlement_status,
        next_actions=actions,
    )
    return _chat_shaped(presentation) if _is_im_channel(channel) else presentation


# Extensions a reader could plausibly be pointed at as *the deliverable*. Names
# are matched, not paths, so this only has to describe what a file is — anything
# resolved still has to be found inside omni's own output area to count.
_NAMED_DELIVERABLE_SUFFIXES = frozenset({
    ".bib", ".csv", ".doc", ".docx", ".gif", ".jpeg", ".jpg", ".json", ".md",
    ".mmd", ".pdf", ".png", ".pptx", ".svg", ".txt", ".xlsx",
})

# A filename as it appears mid-sentence. The name itself may hold anything the
# artifact store slugifies into it, including non-Latin scripts (a survey titled
# in Chinese keeps its title in the filename), so the run is bounded by the
# punctuation that ends a citation rather than by an allow-list of characters. A
# trailing "." is left to ``rstrip`` below because a sentence may end on the name.
_NAMED_FILE = re.compile(
    r"[^\s\"'`()<>\[\]{}|,;:*?！？，。；：、（）「」『』【】]+"
    r"\.(?:" + "|".join(sorted(s[1:] for s in _NAMED_DELIVERABLE_SUFFIXES)) + r")",
    re.IGNORECASE,
)

# How many files one reply may drag in, and how much of a root it is worth
# walking to find them. Both exist so a reply that mentions a directory listing
# cannot turn into an unbounded scan.
_MAX_NAMED_ARTIFACTS = 8
_MAX_INDEXED_FILES = 5000


def may_fetch_named_files(produced: Sequence[ArtifactRef]) -> bool:
    """Whether a reply's own words may add files to what the turn produced.

    Only a turn that produced nothing may. A turn that produced deliverables
    hands over exactly those, which is the one thing both surfaces agree on — so
    a reply announcing more than the turn did cannot dress the claim in files
    from another task. "All three materials are complete" was said on a turn that
    drew a figure, and the survey it named does exist, written by the task the
    user finally asked for it in.

    Asking after earlier work is the case this exists for, and it produces nothing
    by definition: the model answers from context and points at where the file is.
    That a turn with an empty inventory may still over-claim is the residue; a
    reader told about a file and handed it is at least handed the file named.

    Language-neutral on purpose. Reading the request itself would mean matching
    how it was phrased, in the languages these channels are used in, inside the
    control plane — both a contract violation here and a weaker signal than one
    the host knows for certain.
    """
    return not produced


def artifacts_named_in_text(text: str, *, roots: Sequence[Path]) -> list[ArtifactRef]:
    """Deliverables the reply points at, for a turn that produced none itself.

    Asked a follow-up about work already done, the model answers from context —
    "all three are ready, the figure is at /Users/…/artifacts/figure/….png" — and
    that reply goes out with nothing attached and a server-local path in the body,
    which is the one form of the answer its recipient cannot act on. The files
    exist; only the list of what to send with the message was empty.

    So the reply's own text is taken as the statement of what it delivers. A name
    is resolved by looking for it *inside* the given roots, which is what keeps
    this from becoming a way to name any file on the host and have it uploaded:
    ``/etc/passwd`` is a perfectly good filename and resolves to nothing.

    Callers gate this on :func:`may_fetch_named_files`.
    """
    if not text or not roots:
        return []
    wanted: list[str] = []
    for match in _NAMED_FILE.finditer(text):
        name = Path(match.group(0).rstrip(".").replace("\\", "/")).name
        if name and name not in wanted:
            wanted.append(name)
    if not wanted:
        return []

    index = _index_output_files(roots)
    found: list[ArtifactRef] = []
    for name in wanted:
        path = index.get(name)
        if path is None:
            continue
        found.append(_ref_for_file(path))
        if len(found) >= _MAX_NAMED_ARTIFACTS:
            break
    return found


def _index_output_files(roots: Sequence[Path]) -> dict[str, Path]:
    """Map filename to location for the deliverables under ``roots``.

    Keyed by name because that is how a reply refers to a file — the path it
    quotes may be stale, mirrored elsewhere, or the store's internal layout.
    """
    index: dict[str, Path] = {}
    seen = 0
    for root in roots:
        try:
            candidates = Path(root).rglob("*")
        except OSError:
            continue
        try:
            for path in candidates:
                seen += 1
                if seen > _MAX_INDEXED_FILES:
                    return index
                if path.suffix.lower() not in _NAMED_DELIVERABLE_SUFFIXES:
                    continue
                if path.name not in index and path.is_file():
                    index[path.name] = path
        except OSError:
            continue
    return index


def _ref_for_file(path: Path) -> ArtifactRef:
    """Describe a file found on disk the way a produced artifact is described."""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return ArtifactRef(
        title=artifact_display_label(path.stem) or path.name,
        format=path.suffix.lstrip(".").lower(),
        path=str(path),
        mime=mimetypes.guess_type(path.name)[0] or "",
        size_bytes=size,
    )


def project_artifact_locations(
    text: str,
    artifacts: list[ArtifactRef],
    *,
    include_local_paths: bool,
) -> str:
    """Replace internal artifact locations at the display boundary.

    The URI remains in durable task data and machine-readable inspection, but it
    is not a useful location for a person. CLI output uses the resolved path;
    chat output uses the filename because a server-local absolute path is not
    meaningful to the recipient.

    A path is projected for chat on the same grounds as a URI. The reply that
    prompted this offered a WeChat reader the figure's location as
    ``/Users/antonio/.omni/projects/default/artifacts/figure/…``, a directory on
    somebody else's machine: the file arrives beside the message as an upload, and
    its name is the only part of that location they can match it against.
    """
    projected = text
    for artifact in sorted(artifacts, key=lambda item: len(item.uri), reverse=True):
        if not artifact.uri or artifact.uri not in projected:
            continue
        if include_local_paths:
            replacement = artifact.path or artifact_display_label(artifact.title)
        else:
            replacement = _artifact_filename(artifact)
        # Avoid ``/path (/path)`` when a model printed both the resolved path and
        # the internal URI. Other occurrences (for example a table cell) are
        # projected in place.
        projected = projected.replace(f" ({artifact.uri})", "")
        projected = projected.replace(artifact.uri, replacement)
    if include_local_paths:
        return projected
    for artifact in sorted(artifacts, key=lambda item: len(item.path), reverse=True):
        if artifact.path and artifact.path in projected:
            projected = projected.replace(artifact.path, _artifact_filename(artifact))
    return projected


def _artifact_filename(artifact: ArtifactRef) -> str:
    """What to call an artifact where its location cannot be used."""
    if artifact.path:
        return artifact.path.replace("\\", "/").rsplit("/", 1)[-1]
    return artifact_display_label(artifact.title)


def _is_im_channel(channel: str) -> bool:
    # Imported lazily: security imports TurnPresentation from this module.
    from omni.channels.security import is_im_channel

    return is_im_channel(channel)


# How the host chose a provider. Worth reading when routing is what you are
# debugging; noise to someone who asked for a paper and got a routing report.
_ROUTING_DETAILS = ("Plan decision:", "Execution mode:", "Provider correction:",
                    "Selection reasons:", "Match score:")

# Identifiers are shown as eight characters everywhere a person reads them; a
# message that pasted the raw 32 in mid-sentence was quoting an internal record.
_FULL_HEX_ID = re.compile(r"\b[0-9a-f]{32}\b")

# A card in one of these states is reporting that work was accepted, not what
# it produced. ``running`` is left out: a card that reached chat mid-flight is
# reporting progress on something the reader is already waiting for.
_SUBMISSION_STATUSES = frozenset({"submitted", "pending"})

# What one answer may occupy in a chat thread. WeChat iLink splits a reply at
# 1800 characters per message, so a long one does not arrive as a long message —
# it arrives as a queue of them, and the run that prompted this spent ten on a
# paper and then could not send the figures behind it. The budget leaves room for
# the inventory the host appends below the answer.
_CHAT_ANSWER_BUDGET = 2400

# Tool-observation copy that must not become the user-visible reply. The model
# quoting an IM approval denial as a "root cause" essay is how b0cd360c spent
# the chat budget on forensics and never attached the figure.
_POLICY_FORENSICS = (
    "sensitive tools triggered from an IM channel",
    "require local confirmation",
    "run the request from the CLI on the owner's machine",
    "writing outside of the project; rejected by user approval settings",
)

_PENDING_IM_NOTE = (
    "Remaining deliverables are still running. Files will be sent when they are ready."
)


def _chat_shaped(presentation: TurnPresentation) -> TurnPresentation:
    """Reduce a reply to what its reader can act on from inside a chat thread.

    The CLI reader is at the machine that ran the work: routing diagnostics and a
    menu of follow-up commands are cheap there, because the next line of the
    terminal is where they would be used. A chat reader pays for the same text in
    screen-fulls, and on WeChat in separate messages — the reply that prompted
    this carried four command suggestions and a provider-selection report behind
    a paper it had already sent in eight pieces.

    So this keeps what the request produced and one way to look further, and
    drops the host explaining itself. Task identity survives: it is how the
    reader refers to this work later, and the short form is what every other
    surface shows.

    A submission card is dropped outright. It says the work started, gives its
    id, and offers a menu — and the reply it sits under has just said all
    three, so a request that has produced nothing yet arrives as a screenful of
    the host acknowledging itself. The completion card, which carries the
    result, is what the reader is waiting for. Only the way back to the task
    has to survive the card, so it moves up to the reply.

    A turn that still owes a background figure must not attach the paper it
    already wrote: CLI Outputs wait until drain finishes, and the completion
    notice is where the same inventory is handed over once.
    """
    def spoken(task: TaskPresentation) -> TaskPresentation:
        return replace(
            task,
            summary=_FULL_HEX_ID.sub(lambda m: m.group(0)[:8], task.summary),
            details=[
                item
                for item in task.details
                if not item.startswith(_ROUTING_DETAILS)
            ],
            next_actions=_task_inspect_action(task.task_id or task.subtask_id),
        )

    answer = _FULL_HEX_ID.sub(lambda m: m.group(0)[:8], presentation.assistant_text)
    forensics = _looks_like_policy_forensics(answer)
    answer = _strip_policy_forensics(answer)
    answer = _strip_cli_manage_hints(answer)
    pending = presentation.settlement_status == "pending_child_task" or (
        promises_later_deliverables(answer)
        and bool(presentation.submitted_subtask_ids)
    )
    artifacts = [] if pending else presentation.artifacts
    if pending:
        answer = _pending_im_answer(answer, presentation.task_id)
    elif forensics and presentable_artifacts(artifacts):
        answer = "Deliverables are attached."
    cards = [task for task in presentation.tasks if task.status not in _SUBMISSION_STATUSES]
    announced_a_submission = len(cards) != len(presentation.tasks)
    # A reply that has already told the reader how to look further does not
    # need a menu underneath repeating it. The submission notice names the
    # command and the id in its own sentence, so the block below was the third
    # time one short message said where to find the same task.
    said_where_to_look = bool(presentation.task_id) and (
        f"/task show {presentation.task_id[:8]}" in answer
    )
    return replace(
        presentation,
        assistant_text=_bounded_answer(
            answer,
            has_attachments=bool(presentable_artifacts(artifacts)),
            task_id=presentation.task_id,
        ),
        artifacts=artifacts,
        tasks=[spoken(task) for task in cards],
        degraded_warnings=[],
        settlement_status="",
        next_actions=(
            _task_inspect_action(presentation.task_id)
            if (presentation.next_actions or (announced_a_submission and not cards))
            and not said_where_to_look
            else []
        ),
    )


def _looks_like_policy_forensics(text: str) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in _POLICY_FORENSICS)


def _strip_policy_forensics(text: str) -> str:
    if not text or not _looks_like_policy_forensics(text):
        return text
    kept = [part for part in text.split("\n\n") if not _looks_like_policy_forensics(part)]
    return "\n\n".join(kept).strip()


def _pending_im_answer(text: str, task_id: str) -> str:
    """Keep a pending IM turn to a status line, not a paper dump."""
    inspect = f" Inspect `/task show {task_id[:8]}`." if task_id else ""
    cleaned = text.strip()
    if (
        cleaned
        and len(cleaned) <= 400
        and not _looks_like_policy_forensics(cleaned)
    ):
        if _PENDING_IM_NOTE not in cleaned:
            return f"{cleaned.rstrip()} {inspect}{_PENDING_IM_NOTE}".strip()
        return cleaned
    return f"Request is in progress.{inspect} {_PENDING_IM_NOTE}"


def _bounded_answer(text: str, *, has_attachments: bool, task_id: str) -> str:
    """Keep an answer to one chat message, saying where the rest of it is.

    The backstop for an answer that should have been a file and was not. It is
    not a substitute for writing one: what it truncates is unrecoverable from the
    message, so it always names somewhere the whole thing survives.

    Cutting at a blank line rather than a character count keeps a sentence from
    ending mid-word, which reads like a delivery failure rather than a summary.
    """
    if len(text) <= _CHAT_ANSWER_BUDGET:
        return text
    head = text[:_CHAT_ANSWER_BUDGET]
    if (break_at := head.rfind("\n\n")) > _CHAT_ANSWER_BUDGET // 2:
        head = head[:break_at]
    where = (
        "The full text is attached."
        if has_attachments
        else f"Read all of it with `/task show {task_id[:8]}`."
        if task_id
        else "The rest is recorded with this task."
    )
    return f"{head.rstrip()}\n\n_Shortened for chat._ {where}"


def _task_inspect_action(task_id: str) -> list[str]:
    """The two follow-ups a chat reader needs: see the result, or carry it on.

    Everything else a task menu offers is either the host explaining itself or a
    command whose id the reader would have to reconstruct. These two are the
    whole interface to a finished task from inside a thread, and they are worded
    exactly as every other surface words them.
    """
    return _task_navigation_actions(task_id)


_SCHEDULE_TOOL_NAMES = frozenset({"schedule_task", "resolve_action_checkpoint"})

# A chat reader cannot run local inspect/list verbs. Approve/deny stay: those
# are the only way the machine owner finishes an IM-originated proposal.
_CLI_MANAGE_HINT = re.compile(
    r"(?:View or manage it with\s+)?"
    r"`?omni schedule show(?:\s+[A-Za-z0-9_-]+)?`?"
    r"\.?",
    re.IGNORECASE,
)


def _host_cards_from_turn(turn: Any, *, chat: bool) -> list[str]:
    """Structured cards the host can render without trusting model prose."""
    cards: list[str] = []
    schedule = _schedule_result_from_turn(turn)
    if schedule is not None:
        from omni.scheduling.presentation import build_card

        cards.append(build_card(schedule, chat=chat))
    return cards


def _tool_observation_texts(turn: Any) -> list[str]:
    texts: list[str] = []
    for record in getattr(turn, "tool_trace", []) or []:
        result = getattr(record, "result", None)
        if isinstance(result, dict):
            for key in ("summary", "message"):
                value = str(result.get(key) or "").strip()
                if value:
                    texts.append(value)
        observation = str(getattr(record, "observation", None) or "").strip()
        if observation:
            texts.append(observation)
    return texts


def _is_observation_echo(text: str, turn: Any) -> bool:
    """Whether the visible reply is a tool receipt, not a separate human answer."""
    stripped = text.strip()
    if not stripped:
        return False
    collapsed = " ".join(stripped.split())
    for obs in _tool_observation_texts(turn):
        folded = " ".join(obs.split())
        if stripped == obs or collapsed == folded:
            return True
        if len(folded) >= 40 and folded in collapsed:
            return True
    return collapsed.startswith("Scheduled '") and "next run" in collapsed


def _compose_channel_answer(
    text: str,
    cards: list[str],
    *,
    chat: bool,
    echoed: bool,
) -> str:
    """IM never ships a tool observation as the only bubble when a card exists.

    CLI already has process lines, so a model-written reply is left alone unless
    it is the receipt itself.
    """
    stripped = text.strip()
    joined = "\n\n".join(card.strip() for card in cards if card.strip())
    if not joined:
        return text
    if not stripped or echoed:
        return joined
    if any(card.strip() and card.strip() in stripped for card in cards):
        return stripped
    if not chat:
        return stripped
    return f"{stripped}\n\n{joined}"


def _strip_cli_manage_hints(text: str) -> str:
    """Drop ``omni schedule show`` manage hints a chat reader cannot run."""
    if not text:
        return text
    cleaned = _CLI_MANAGE_HINT.sub("", text)
    cleaned = re.sub(r"View or manage it with\s*\.?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _schedule_result_from_turn(turn: Any) -> Any:
    """Last schedule-shaped tool payload on this turn, if the host should render it."""
    # Imported lazily: ``omni.scheduling`` pulls channel security, which imports
    # this module's ``TurnPresentation``.
    from omni.scheduling.presentation import result_from_tool_payload

    for record in reversed(list(getattr(turn, "tool_trace", []) or [])):
        name = str(getattr(record, "name", "") or "")
        if name not in _SCHEDULE_TOOL_NAMES:
            continue
        payload = getattr(record, "result", None)
        if not isinstance(payload, dict):
            continue
        hydrated = result_from_tool_payload(payload)
        if hydrated is not None:
            return hydrated
    return None


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
