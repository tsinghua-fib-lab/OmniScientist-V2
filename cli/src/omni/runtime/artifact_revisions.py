"""Artifact revision transaction service."""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omni.config.paths import OmniPaths
from omni.research.store import ResearchStore
from omni.runtime.artifact_intents import ArtifactIntent, artifact_intent_from_spec


@dataclass(slots=True)
class ArtifactRevisionResult:
    status: str
    message: str
    source_path: str = ""
    task_id: str = ""
    artifacts: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    changed_paths: list[str] = field(default_factory=list)
    intent: ArtifactIntent | None = None

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


async def revise_artifact(
    *,
    source_path: Path,
    instruction: str,
    paths: OmniPaths,
    db: Any,
    artifacts: Any,
    session_id: str,
    task_id: str = "",
    subtask_id: str = "",
    edit_spec: dict[str, Any] | None = None,
) -> ArtifactRevisionResult:
    source_path = Path(source_path).expanduser()
    from omni.runtime.artifact_contracts import contract_for_path

    contract = contract_for_path(source_path)
    if contract is None:
        return ArtifactRevisionResult(
            status="failed",
            source_path=str(source_path),
            error=f"no artifact contract for {source_path.suffix}",
            message=f"Not completed: no revision contract is registered for `{source_path.suffix}` artifacts.",
        )
    if not source_path.is_file():
        return ArtifactRevisionResult(
            status="failed",
            source_path=str(source_path),
            error=f"source not found: {source_path}",
            message=f"Not completed: editable source file not found: {source_path}",
        )
    try:
        original = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ArtifactRevisionResult(
            status="failed",
            source_path=str(source_path),
            error=str(exc),
            message=f"Not completed: could not read source file: {exc}",
        )

    # Grounding only (not routing): consume normalized planner fields. A miss
    # returns an observation so the caller can escalate to a full redraw.
    elements = contract.extract_elements(original)
    intent = artifact_intent_from_spec(edit_spec or {}, elements=elements)
    if intent is None:
        return ArtifactRevisionResult(
            status="failed",
            source_path=str(source_path),
            error="could not ground a concrete in-place edit",
            message=(
                "Not completed: the structured edit did not resolve to one exact element and a valid style. "
                "The runtime may escalate this request to a source-preserving redraw."
            ),
        )

    patched, changes = contract.patch(original, intent)
    if patched == original:
        return ArtifactRevisionResult(
            status="failed",
            source_path=str(source_path),
            error="contract patch produced no changes",
            intent=intent,
            message=(
                "Not completed: the artifact contract found no safe location for the requested edit."
            ),
        )

    tmp_root = paths.project_dir / "artifacts" / ".tmp-revisions"
    tmp_root.mkdir(parents=True, exist_ok=True)
    rev = uuid.uuid4().hex[:8]
    stem = f"{source_path.stem}_rev_{rev}"
    stored: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="artifact-", dir=str(tmp_root)) as td:
        work = Path(td)
        revised_source = work / f"{stem}{source_path.suffix}"
        revised_source.write_text(patched, encoding="utf-8")
        render = await contract.render(revised_source, output_stem=work / stem)
        if not render.ok:
            return ArtifactRevisionResult(
                status="failed",
                source_path=str(source_path),
                error=render.error,
                intent=intent,
                message=f"Not completed: the revised source could not be rendered: {render.error[:240]}",
                changed_paths=[str(revised_source)],
            )
        all_files = [(revised_source, source_path.suffix.lstrip("."), _mime_for_source(source_path))]
        all_files.extend((item.path, item.format, item.mime) for item in render.files)
        for file_path, fmt, mime in all_files:
            art = await artifacts.put_file(
                file_path,
                kind="figure",
                title=f"{source_path.stem} revision {fmt.upper()}",
                mime=mime,
                session_id=session_id,
                task_id=task_id,
                subtask_id=subtask_id,
                meta={
                    "revision_of": str(source_path),
                    "instruction": instruction,
                    "contract": contract.name,
                    "intent": {
                        "action": intent.action,
                        "target": intent.target,
                        "change": intent.change,
                        "style": intent.style,
                        "confidence": intent.confidence,
                        "reasons": list(intent.reasons),
                    },
                    "format": fmt,
                    "changes": changes,
                },
            )
            stored.append({
                "title": art.title,
                "format": fmt,
                "uri": art.uri,
                "path": str(art.path),
                "mime": art.mime,
            })

    run = await ResearchStore(db).add_run(
        title=f"Artifact revision: {source_path.name}",
        session_id=session_id,
        subtask_id=subtask_id,
        cmd=getattr(render, "command", "") or f"render {source_path.name}",
        code_uri=next((a["uri"] for a in stored if a["format"] == source_path.suffix.lstrip(".")), ""),
        inputs={
            "source_path": str(source_path),
            "instruction": instruction,
            "intent": {
                "action": intent.action,
                "target": intent.target,
                "change": intent.change,
                "style": intent.style,
                "confidence": intent.confidence,
                "reasons": list(intent.reasons),
            },
            "changes": changes,
        },
        output_uris=[a["uri"] for a in stored],
        metrics={"artifact_count": len(stored), "change_count": len(changes)},
        status="succeeded",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    return ArtifactRevisionResult(
        status="succeeded",
        source_path=str(source_path),
        task_id=run.id,
        artifacts=stored,
        changed_paths=[a["path"] for a in stored],
        intent=intent,
        message=_success_message(source_path, intent, changes, run.id, stored),
    )


async def ensure_graphviz_derivatives(
    *,
    dot_paths: list[Path],
    paths: OmniPaths,
    db: Any,
    session_id: str,
    reason: str,
    mirror_dir: Path | None = None,
) -> ArtifactRevisionResult | None:
    """Best-effort contract guard for free-form edits to DOT files."""
    rendered: list[str] = []
    errors: list[str] = []
    candidates = [Path(p).expanduser() for p in dot_paths]
    candidates = [
        p
        for p in candidates
        if p.suffix.lower() == ".dot" and p.is_file() and _within_artifacts(paths, p, mirror_dir)
    ]
    if not candidates:
        return None
    started = datetime.now(UTC)
    command = ""
    for source in candidates:
        from omni.runtime.artifact_contracts import contract_for_path

        contract = contract_for_path(source)
        if contract is None:
            continue
        result = await contract.render(source, output_stem=source.with_suffix(""))
        command = getattr(result, "command", "") or command
        if not result.ok:
            errors.append(f"{source.name}: {result.error[:160]}")
            continue
        rendered.extend(str(item.path) for item in result.files)
    status = "failed" if errors else "succeeded"
    run = await ResearchStore(db).add_run(
        title="Artifact contract render",
        session_id=session_id,
        cmd=command or "artifact contract render",
        code_uri=str(candidates[0]),
        inputs={"source_paths": [str(p) for p in candidates], "reason": reason},
        output_uris=rendered,
        metrics={"source_count": len(candidates), "rendered_count": len(rendered), "error_count": len(errors)},
        status=status,
        started_at=started,
        finished_at=datetime.now(UTC),
    )
    if errors:
        return ArtifactRevisionResult(
            status="failed",
            source_path=str(candidates[0]),
            task_id=run.id,
            error="; ".join(errors),
            message="Not completed: a changed source artifact could not be re-rendered.\n" + "\n".join(f"- {e}" for e in errors),
            changed_paths=[str(p) for p in candidates],
        )
    return ArtifactRevisionResult(
        status="succeeded",
        source_path=str(candidates[0]),
        task_id=run.id,
        message=f"Artifact contract completed: derived artifacts were re-rendered.\n- run: {run.id[:8]}",
        changed_paths=rendered,
    )


def _success_message(source: Path, intent: ArtifactIntent, changes: list[str], task_id: str, stored: list[dict[str, str]]) -> str:
    lines = [
        "Completed the attached figure edit as a versioned artifact transaction.",
        f"- Target: {intent.target or source.name}",
        f"- Request: {intent.change or 'structured style update'}",
        f"- Changes: {'; '.join(changes) if changes else 'updated style'}",
        f"- Verification: {source.suffix.lstrip('.').upper() or 'source'} was re-rendered and registered with its derivatives.",
        f"- Run: {task_id[:8]}",
        "",
        "Artifacts:",
    ]
    for item in stored:
        lines.append(f"- {item['format'].upper()}: {item['path']} ({item['uri']})")
    return "\n".join(lines)


def _mime_for_source(path: Path) -> str:
    if path.suffix.lower() == ".dot":
        return "text/vnd.graphviz"
    return "text/plain"


def _within_artifacts(paths: OmniPaths, path: Path, mirror_dir: Path | None = None) -> bool:
    """True when ``path`` is a managed ``.dot`` Omni may safely re-render.

    Trusted roots are the durable workspace store and, when set, the trusted
    launch/output directory where figure bundles (with their ``.dot`` source)
    now live. Anything outside both is left untouched.
    """
    resolved = path.resolve()
    roots = [paths.artifacts_dir]
    if mirror_dir is not None:
        roots.append(Path(mirror_dir))
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False
