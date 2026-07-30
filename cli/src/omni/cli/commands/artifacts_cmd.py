"""`omni artifacts` — review generated artifact versions and diffs."""

from __future__ import annotations

import difflib
from pathlib import Path

import typer
from rich.table import Table
from sqlalchemy import select

from omni.cli.render import console, data_table, error, info
from omni.cli.state import AppState, make_agent, run_async
from omni.core.identifiers import short_id, shortest_unique_prefixes
from omni.runtime.artifact_contracts import contract_for_path
from omni.storage.models import ArtifactORM, SubtaskORM

app = typer.Typer(help="Inspect, preview, and compare artifact versions.", no_args_is_help=True)

_TEXT_SUFFIXES = {".dot", ".md", ".txt", ".json", ".yaml", ".yml", ".py", ".tex", ".svg", ".csv"}


@app.command("preview")
def preview_command(
    ctx: typer.Context,
    artifact: str = typer.Argument(..., help="Artifact URI, ID/prefix, or local path."),
    max_chars: int = typer.Option(1600, "--max-chars", help="Maximum text preview length."),
) -> None:
    """Preview artifact metadata and the beginning of text artifacts."""
    state: AppState = ctx.obj or AppState()
    run_async(_preview(state, artifact=artifact, max_chars=max_chars))


@app.command("diff")
def diff_command(
    ctx: typer.Context,
    old: str = typer.Argument(..., help="Old artifact ID, URI, or path."),
    new: str = typer.Argument(..., help="New artifact ID, URI, or path."),
    max_lines: int = typer.Option(240, "--max-lines", help="Maximum diff lines to print."),
) -> None:
    """Compare two text artifacts."""
    state: AppState = ctx.obj or AppState()
    run_async(_diff(state, old=old, new=new, max_lines=max_lines))


@app.command("versions")
def versions_command(
    ctx: typer.Context,
    artifact: str = typer.Argument(..., help="Artifact URI, ID/prefix, or local path."),
) -> None:
    """List versions and derivatives related to an artifact."""
    state: AppState = ctx.obj or AppState()
    run_async(_versions(state, artifact=artifact))


@app.command("review")
def review_command(
    ctx: typer.Context,
    artifact: str = typer.Argument(..., help="Artifact URI, ID/prefix, or local path."),
) -> None:
    """Review artifact health, version lineage, and reproducibility evidence."""
    state: AppState = ctx.obj or AppState()
    run_async(_review(state, artifact=artifact))


@app.command("help")
def help_cmd() -> None:
    """Show artifact subcommands and common examples (`/artifacts help` in the REPL)."""
    data_table(
        "Artifact subcommands",
        ["command", "purpose", "example"],
        [
            ["preview <id|uri|path>", "Preview artifact metadata and the start of text artifacts", "/artifacts preview 1a2b3c"],
            ["diff <old> <new>", "Compare two text artifacts (DOT, Markdown, SVG, JSON, ...)", "/artifacts diff 1a2b3c 4d5e6f"],
            ["versions <id|uri|path>", "List versions and derivatives related to an artifact", "/artifacts versions 1a2b3c"],
            ["review <id|uri|path>", "Review artifact health, lineage, and reproducibility evidence", "/artifacts review 1a2b3c"],
            ["help", "Show this artifact command reference", "/artifacts help"],
        ],
    )
    info("Options: `--max-chars` (preview) and `--max-lines` (diff). Artifacts resolve by id/prefix, an artifact:// URI, or a local path.")


async def _preview(state: AppState, *, artifact: str, max_chars: int) -> None:
    agent = await make_agent(state)
    try:
        row, path = await _resolve_artifact(agent, artifact)
        if row is None and path is None:
            error(f"Artifact not found: {artifact}")
            raise typer.Exit(code=1)
        _print_artifact_header(row, path)
        if path is not None and _is_text(path):
            text = path.read_text(encoding="utf-8", errors="replace")
            console.print("\n[bold cyan]preview[/bold cyan]")
            console.print(text[:max(0, max_chars)])
        elif path is not None:
            info(f"Binary or non-text artifact: {path}")
    finally:
        await agent.aclose()


async def _diff(state: AppState, *, old: str, new: str, max_lines: int) -> None:
    agent = await make_agent(state)
    try:
        old_row, old_path = await _resolve_artifact(agent, old)
        new_row, new_path = await _resolve_artifact(agent, new)
        if old_path is None or new_path is None:
            error("Both artifacts must resolve to local files.")
            raise typer.Exit(code=1)
        if not (_is_text(old_path) and _is_text(new_path)):
            error("Diff currently supports text artifacts such as DOT, Markdown, SVG, and JSON.")
            raise typer.Exit(code=1)
        old_text = old_path.read_text(encoding="utf-8", errors="replace").splitlines()
        new_text = new_path.read_text(encoding="utf-8", errors="replace").splitlines()
        diff = list(
            difflib.unified_diff(
                old_text,
                new_text,
                fromfile=_artifact_label(old_row, old_path),
                tofile=_artifact_label(new_row, new_path),
                lineterm="",
            )
        )
        if not diff:
            info("The two artifacts are identical.")
            return
        console.print("\n".join(diff[:max(1, max_lines)]))
        if len(diff) > max_lines:
            info(f"Diff truncated: showing {max_lines} of {len(diff)} lines.")
    finally:
        await agent.aclose()


async def _versions(state: AppState, *, artifact: str) -> None:
    agent = await make_agent(state)
    try:
        row, path = await _resolve_artifact(agent, artifact)
        if row is None and path is None:
            error(f"Artifact not found: {artifact}")
            raise typer.Exit(code=1)
        rows = await _related_artifacts(agent, row, path)
        artifact_prefixes = shortest_unique_prefixes([item.id for item in rows])
        table = _table("artifact versions", ["id", "title", "format", "task", "created", "path"])
        for item in rows:
            full = agent.paths.project_dir / item.rel_path
            table.add_row(
                artifact_prefixes[item.id],
                item.title or "-",
                Path(item.rel_path).suffix.lstrip(".") or item.mime,
                short_id(item.task_id or item.subtask_id) or "-",
                str(item.created_at),
                str(full),
            )
        console.print(table)
    finally:
        await agent.aclose()


async def _review(state: AppState, *, artifact: str) -> None:
    agent = await make_agent(state)
    try:
        row, path = await _resolve_artifact(agent, artifact)
        if row is None and path is None:
            error(f"Artifact not found: {artifact}")
            raise typer.Exit(code=1)
        _print_artifact_header(row, path)
        checks: list[tuple[str, str, str]] = []
        if path is None:
            checks.append(("file.exists", "failed", "Artifact metadata exists, but the local file is unavailable."))
        else:
            checks.append(("file.exists", "passed", str(path)))
            size = path.stat().st_size
            checks.append(("file.size", "passed" if size > 0 else "failed", f"{size} bytes"))
            checks.append(("file.kind", "passed", path.suffix.lower().lstrip(".") or "unknown"))
            if _is_text(path):
                text = path.read_text(encoding="utf-8", errors="replace")
                checks.append(("text.previewable", "passed", f"{len(text)} chars"))
            contract = contract_for_path(path)
            if contract is not None:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    elements = contract.extract_elements(text)
                    checks.append(("contract.elements", "passed" if elements else "warning", f"{len(elements)} elements"))
                except OSError as exc:
                    checks.append(("contract.elements", "failed", str(exc)))
                derivative_count = _derivative_count(path)
                checks.append(
                    ("render.derivatives", "passed" if derivative_count else "warning", f"{derivative_count} sibling render files")
                )
            else:
                checks.append(("contract.available", "warning", f"no contract for {path.suffix or 'path'}"))
        versions = await _related_artifacts(agent, row, path)
        checks.append(("versions", "passed" if len(versions) > 1 else "warning", f"{len(versions)} related artifact(s)"))
        if row is not None:
            revision_of = str((row.meta or {}).get("revision_of") or "")
            checks.append(("revision.link", "passed" if revision_of else "warning", revision_of or "no revision_of metadata"))
            if row.subtask_id:
                task = await _task_for_artifact(agent, row)
                if task is None:
                    checks.append(("task.exists", "warning", f"task {short_id(row.subtask_id)} not found"))
                else:
                    checks.append(("task.status", "passed" if task.status == "succeeded" else "warning", task.status))
                    refs = _research_refs(task.result_json or {})
                    detail = _research_refs_detail(refs)
                    checks.append(("research.provenance", "passed" if detail else "warning", detail or "no source/claim/evidence refs"))
        table = _table("artifact review", ["check", "status", "detail"])
        for check, status, detail in checks:
            table.add_row(check, status, detail)
        console.print(table)
    finally:
        await agent.aclose()


async def _resolve_artifact(agent, ref: str) -> tuple[ArtifactORM | None, Path | None]:  # noqa: ANN001
    value = (ref or "").strip()
    if not value:
        return None, None
    path = Path(value.replace("file://", "")).expanduser()
    if path.exists():
        return await _row_for_path(agent, path), path
    row = await agent.artifacts.get(value)
    if row is None:
        return None, None
    full = agent.paths.project_dir / row.rel_path
    return row, full if full.exists() else None


async def _row_for_path(agent, path: Path) -> ArtifactORM | None:  # noqa: ANN001
    try:
        rel = str(path.resolve().relative_to(agent.paths.project_dir.resolve()))
    except ValueError:
        rel = ""
    async with agent.db.session() as s:
        if rel:
            row = (
                await s.execute(select(ArtifactORM).where(ArtifactORM.rel_path == rel))
            ).scalar_one_or_none()
            if row is not None:
                return row
        rows = (
            await s.execute(select(ArtifactORM).order_by(ArtifactORM.created_at.desc()).limit(500))
        ).scalars().all()
    return next((row for row in rows if str(agent.paths.project_dir / row.rel_path) == str(path)), None)


async def _related_artifacts(agent, row: ArtifactORM | None, path: Path | None) -> list[ArtifactORM]:  # noqa: ANN001
    target_path = str(path or "")
    target_task = row.subtask_id if row is not None else ""
    target_revision_of = str((row.meta or {}).get("revision_of") or "") if row is not None else ""
    family_keys = {key for key in (target_path, target_revision_of) if key}
    async with agent.db.session() as s:
        rows = (
            await s.execute(select(ArtifactORM).order_by(ArtifactORM.created_at.desc()).limit(1000))
        ).scalars().all()
    related: list[ArtifactORM] = []
    for item in rows:
        item_path = str(agent.paths.project_dir / item.rel_path)
        item_revision_of = str((item.meta or {}).get("revision_of") or "")
        if row is not None and item.id == row.id:
            related.append(item)
        elif target_task and item.subtask_id == target_task:
            related.append(item)
        elif family_keys and (item_path in family_keys or item_revision_of in family_keys):
            related.append(item)
    return sorted({item.id: item for item in related}.values(), key=lambda x: x.created_at)


async def _task_for_artifact(agent, row: ArtifactORM) -> SubtaskORM | None:  # noqa: ANN001
    if not row.subtask_id:
        return None
    async with agent.db.session() as s:
        return await s.get(SubtaskORM, row.subtask_id)


def _print_artifact_header(row: ArtifactORM | None, path: Path | None) -> None:
    table = _table("artifact", ["field", "value"])
    if row is not None:
        table.add_row("id", row.id)
        table.add_row("title", row.title or "-")
        table.add_row("uri", row.uri)
        table.add_row("kind", row.kind)
        table.add_row("mime", row.mime)
        table.add_row("task", row.subtask_id or "-")
    if path is not None:
        table.add_row("path", str(path))
    console.print(table)


def _is_text(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_SUFFIXES


def _artifact_label(row: ArtifactORM | None, path: Path) -> str:
    if row is not None:
        return row.title or row.uri or path.name
    return str(path)


def _derivative_count(path: Path) -> int:
    stem = path.with_suffix("")
    return sum(1 for suffix in (".svg", ".png", ".pdf") if stem.with_suffix(suffix).is_file())


def _research_refs(payload: object) -> dict[str, list[str]]:
    refs = {"source_ids": [], "claim_ids": [], "evidence_ids": []}

    def add(key: str, value: object) -> None:
        if not value:
            return
        values = value if isinstance(value, list) else [value]
        for item in values:
            text = str(item)
            if text and text not in refs[key]:
                refs[key].append(text)

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key in refs:
                add(key, value.get(key))
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return refs


def _research_refs_detail(refs: dict[str, list[str]]) -> str:
    parts: list[str] = []
    for key, values in refs.items():
        if values:
            label = key.removesuffix("_ids")
            parts.append(f"{label}={','.join(item[:8] for item in values[:4])}")
    return "；".join(parts)


def _table(title: str, columns: list[str]) -> Table:
    table = Table(title=title, title_justify="left", title_style="bold", header_style="bold cyan")
    for column in columns:
        table.add_column(column, overflow="fold")
    return table
