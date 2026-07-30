"""Shared task result and artifact helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from omni.core.timefmt import ensure_aware


def is_dot_artifact(value: Any, *, format_hint: str = "") -> bool:
    """Return whether an artifact reference points to a DOT source file.

    This is a presentation policy only: callers still persist and index DOT
    sources so later figure revisions can use them, but user-facing result
    lists and outbound channel delivery omit them.
    """
    fmt = str(format_hint or "")
    path = ""
    uri = ""
    if isinstance(value, dict):
        fmt = str(value.get("format") or fmt)
        path = str(value.get("path") or value.get("file") or "")
        uri = str(value.get("uri") or value.get("artifact_uri") or "")
    elif isinstance(value, str):
        path = value
    else:
        fmt = str(getattr(value, "format", "") or fmt)
        path = str(getattr(value, "path", "") or "")
        uri = str(getattr(value, "uri", "") or "")
    if fmt.lower().lstrip(".") == "dot":
        return True
    target = (path or uri).split("?", 1)[0].split("#", 1)[0]
    return target.lower().endswith(".dot")


def _result_summary(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("summary", "text", "abstract", "message", "title"):
            if result.get(key):
                return str(result[key])[:400]
        return str({k: result[k] for k in list(result)[:4]})[:400]
    return str(result)[:400]


def _action_required_records(value: Any) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    records: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def visit(current: Any) -> None:
        if isinstance(current, dict):
            action = current.get("action_required")
            if isinstance(action, dict):
                records.append((current, action))
            for nested in current.values():
                visit(nested)
        elif isinstance(current, (list, tuple)):
            for nested in current:
                visit(nested)

    visit(value)
    return records


def action_required_presentation(value: Any) -> tuple[str, str, list[str]] | None:
    """Project user-supplied configuration into ``needs_input`` presentation.

    Dependency installation is an owner CLI lifecycle failure, not information
    the conversational user can supply. If any failed branch needs installation,
    keep the task failed and let its error carry the repair command.
    """
    records = _action_required_records(value)
    if any(str(action.get("kind") or "").lower() == "install" for _, action in records):
        return None
    for result, action in records:
        if str(action.get("kind") or "").lower() != "configure":
            continue
        text = next(
            (
                str(result.get(key)).strip()
                for key in ("summary", "error", "message", "text")
                if str(result.get(key) or "").strip()
            ),
            "Additional configuration is required before this skill can run.",
        )
        command = str(action.get("command") or result.get("setup_command") or "").strip()
        if command and command not in text:
            text += f" Run `{command}` and retry."
        error_info = result.get("error_info")
        reason = (
            str(error_info.get("code") or "action_required")
            if isinstance(error_info, dict)
            else "action_required"
        )
        actions = [
            str(item).strip()
            for item in result.get("next_actions") or []
            if str(item).strip()
        ]
        if command:
            actions = [command, *[item for item in actions if item != command]]
        return text, reason, actions
    return None


def installation_required_presentation(value: Any) -> tuple[str, str, list[str]] | None:
    """Project an owner-managed dependency failure into a terminal error.

    Installation is not conversational input.  Callers use this projection to
    preserve the failed status while still surfacing the exact CLI repair
    command emitted by the skill runtime.
    """
    for result, action in _action_required_records(value):
        if str(action.get("kind") or "").lower() != "install":
            continue
        text = next(
            (
                str(result.get(key)).strip()
                for key in ("summary", "error", "message", "text")
                if str(result.get(key) or "").strip()
            ),
            "A required skill component is not installed.",
        )
        command = str(action.get("command") or result.get("setup_command") or "").strip()
        if command and command not in text:
            text += f" Run `{command}` in a terminal, then retry."
        error_info = result.get("error_info")
        reason = (
            str(error_info.get("code") or "runtime_dependency_missing")
            if isinstance(error_info, dict)
            else "runtime_dependency_missing"
        )
        actions = [
            str(item).strip()
            for item in result.get("next_actions") or []
            if str(item).strip()
        ]
        if command:
            actions = [command, *[item for item in actions if item != command]]
        return text, reason, actions
    return None


def _artifact_uris(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []
    uris = []
    for k, v in result.items():
        if isinstance(v, str) and (v.startswith("artifact://") or k.endswith("_uri")):
            uris.append(v)
    return uris


def _notify_title(entry: Any, result: Any) -> str:
    field = (entry.notification or {}).get("title_field")
    if field and isinstance(result, dict) and result.get(field):
        return str(result[field])[:120]
    return (entry.notification or {}).get("display_label") or entry.name


def _skill_execution_event(
    task_id: str,
    execution_id: str,
    skill_name: str,
    **payload: Any,
) -> dict[str, Any]:
    """Build an event payload without losing either execution identity."""
    return {
        **payload,
        "task_id": task_id,
        "object_kind": "skill_execution",
        "object_id": execution_id,
        "subtask_id": execution_id,
        "skill": skill_name,
    }


def _task_result_message(
    execution_id: str,
    skill_name: str,
    summary: str,
    artifacts: list[dict[str, str]],
    *,
    task_id: str = "",
) -> str:
    identity = f"task `{task_id[:8]}`, " if task_id else ""
    lines = [
        f"[Background skill execution completed] {skill_name} "
        f"({identity}execution `{execution_id[:8]}`)"
    ]
    if summary:
        lines.append(summary.strip())
    visible_artifacts = [
        artifact for artifact in artifacts if not is_dot_artifact(artifact)
    ]
    if visible_artifacts:
        lines.append("\nArtifacts:")
        for artifact in visible_artifacts[:12]:
            label = artifact.get("label") or "artifact"
            uri = artifact.get("uri") or ""
            path = artifact.get("path") or ""
            lines.append(
                f"- {label}: {path} ({uri})"
                if path and uri
                else f"- {label}: {uri or path}"
            )
        continuation = (
            "\nTo continue from these artifacts, use open_artifact to read content "
            "or list_session_artifacts to list session artifacts."
        )
        if task_id:
            continuation += (
                f" Inspect the complete task with `/task show {task_id[:8]}` "
                f"or attach it with `/task attach {task_id[:8]}`."
            )
        lines.append(continuation)
    return "\n".join(lines)


def _collect_artifacts(result: Any, *, limit: int = 24) -> list[dict[str, str]]:
    """Recursively collect ``{label, uri, path}`` artifacts from a result tree.

    Unlike :func:`_artifact_uris` (top-level only) this walks nested workflow
    step results and figure outputs (which carry ``dot``/``svg``/``png`` path +
    uri pairs), so the transcript write-back lists *all* produced artifacts.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(label: Any, uri: str, path: str) -> None:
        key = uri or path
        if not key or key in seen:
            return
        seen.add(key)
        out.append({"label": str(label or "artifact")[:80], "uri": uri, "path": path})

    def visit(obj: Any, label_hint: str = "") -> None:
        if len(out) >= limit:
            return
        if isinstance(obj, dict):
            uri = str(obj.get("uri") or obj.get("artifact_uri") or "")
            path = str(obj.get("path") or obj.get("file") or "")
            label = obj.get("title") or obj.get("format") or obj.get("kind") or label_hint
            if uri.startswith("artifact://") or path.startswith(("/", "artifact://")):
                add(label, uri, path)
            for k, v in obj.items():
                if k in {"uri", "artifact_uri", "path", "file", "title", "format", "kind"}:
                    continue
                visit(v, k)
        elif isinstance(obj, list):
            for v in obj:
                visit(v, label_hint)
        elif isinstance(obj, str):
            if obj.startswith("artifact://"):
                add(label_hint, obj, "")
            elif label_hint.endswith("_uri") and obj:
                add(label_hint, obj, "")

    visit(result)
    return out


def _aware_dt(dt: datetime | None) -> datetime:
    """Normalise a possibly-naive DB timestamp to UTC for comparisons."""
    if dt is None:
        return datetime.now(UTC)
    return ensure_aware(dt)


def _result_has_artifacts(result_json: Any) -> bool:
    return bool(_collect_artifacts(result_json, limit=1))


def _result_error_message(result: dict[str, Any]) -> str:
    for key in ("error", "message", "summary", "text"):
        if result.get(key):
            return str(result[key])
    info = result.get("error_info")
    if isinstance(info, dict) and info.get("message"):
        return str(info["message"])
    return "unknown error"


def _partial_outputs_are_deliverable(value: Any) -> bool:
    """True when a prompt-skill trace actually wrote a file, not just ran bash.

    Bash/read traces used to count as visible output, so a skill that extracted
    a PDF and then announced ``done`` with an empty ``text`` was settled as
    succeeded. Only writes (or an explicit path that is not a shell command)
    are a deliverable.
    """
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "")
        path = str(item.get("path") or "").strip()
        if tool in {"write_file", "edit_file"} and path:
            return True
        if path and tool not in {"bash", "read_file"}:
            return True
    return False


def _result_has_visible_output(result: Any) -> bool:
    if not isinstance(result, dict):
        return result is not None
    for key in ("summary", "text", "message", "title", "warning", "error"):
        if str(result.get(key) or "").strip():
            return True
    for key in ("artifacts", "files", "output_uris", "steps"):
        value = result.get(key)
        if isinstance(value, list) and value:
            return True
    if _partial_outputs_are_deliverable(result.get("partial_outputs")):
        return True
    research = result.get("research")
    if isinstance(research, dict) and any(research.get(key) for key in ("source_ids", "claim_ids", "evidence_ids", "task_id")):
        return True
    return False


def _result_warning_message(result: dict[str, Any]) -> str:
    for key in ("warning", "message", "summary", "text"):
        if result.get(key):
            return str(result[key])
    info = result.get("error_info")
    if isinstance(info, dict) and info.get("message"):
        return str(info["message"])
    return "partial result"
