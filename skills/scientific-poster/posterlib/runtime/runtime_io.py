"""Host-facing filesystem, artifact, progress, and inspection adapters."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import poster_core

from . import browser_inspection


def create_workspace(input_data: dict[str, Any], ctx: Any) -> Path:
    """Return a stable task workspace or allocate one for an inline action."""

    output_dir = str(input_data.get("output_dir") or "").strip()
    paths = getattr(ctx, "paths", None) if ctx is not None else None
    if ctx is not None and paths is not None:
        base = Path(paths.artifacts_dir) / "poster-workspaces"
    elif ctx is not None:
        base = Path.cwd() / "scientific-poster-output"
    elif output_dir:
        base = Path(output_dir).expanduser()
    else:
        base = Path.cwd() / "scientific-poster-output"
    persisted_task = _slug(
        str(getattr(ctx, "task_id", "") or getattr(ctx, "workflow_task_id", "") or "")
    )
    workflow_step = _slug(str(getattr(ctx, "workflow_step_id", "") or ""))
    if persisted_task:
        name = "-".join(
            part
            for part in ("scientific-poster", persisted_task, workflow_step)
            if part
        )
        candidate = base / name
        if candidate.is_symlink():
            raise OSError(f"poster workspace must not be a symlink: {candidate}")
        candidate.mkdir(parents=True, exist_ok=True)
        if not candidate.is_dir():
            raise OSError(f"poster workspace is not a directory: {candidate}")
        return candidate

    session = _slug(str(getattr(ctx, "session_id", "") or "local"))
    task = _slug(str(input_data.get("task_id") or ""))
    prefix = "-".join(part for part in ("scientific-poster", session, task) if part)
    for _ in range(5):
        candidate = base / f"{prefix}-{uuid.uuid4().hex[:10]}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("could not allocate a unique scientific poster workspace")


async def resolve_path(
    ctx: Any,
    value: str,
    *,
    base_dir: object = None,
) -> Path | None:
    """Resolve a local path or host artifact URI without network access."""

    if not value:
        return None
    store = getattr(ctx, "artifacts", None) if ctx is not None else None
    if store is not None and callable(getattr(store, "resolve_path", None)):
        try:
            resolved = await store.resolve_path(value)
        except Exception:  # noqa: BLE001 - host resolver is an external boundary
            resolved = None
        if resolved is not None:
            return Path(resolved)
        if value.startswith("artifact://"):
            return None
    path = (
        Path(unquote(urlparse(value).path))
        if value.startswith("file://")
        else Path(value).expanduser()
    )
    if not path.is_absolute() and base_dir:
        path = Path(str(base_dir)).expanduser() / path
    return path if path.is_file() else None


async def store_artifact(
    ctx: Any,
    path: Path,
    *,
    kind: str,
    title: str,
    fmt: str,
    mime: str,
) -> dict[str, Any]:
    """Persist through the host store or an immutable local snapshot fallback."""

    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    store = getattr(ctx, "artifacts", None) if ctx is not None else None
    if store is not None and callable(getattr(store, "put_file", None)):
        try:
            stored = await store.put_file(
                path,
                kind=kind,
                title=title,
                mime=mime,
                session_id=str(getattr(ctx, "session_id", "") or ""),
                subtask_id=str(getattr(ctx, "subtask_id", "") or ""),
                workflow_run_id=str(getattr(ctx, "workflow_run_id", "") or ""),
                copy=True,
                meta={"skill": "scientific-poster", "format": fmt, "sha256": digest},
            )
        except Exception as exc:  # noqa: BLE001 - host store is an external boundary
            raise OSError(f"host artifact store failed for {path.name}: {exc}") from exc
        uri = str(_stored_value(stored, "uri") or "").strip()
        stored_value = _stored_value(stored, "path")
        stored_path = Path(stored_value) if stored_value else None
        valid_copy = False
        if uri and stored_path is not None and stored_path.is_file():
            try:
                valid_copy = (
                    hashlib.sha256(stored_path.read_bytes()).hexdigest() == digest
                    and stored_path.resolve() != path.resolve()
                )
            except OSError:
                valid_copy = False
        if not valid_copy:
            raise OSError(
                "host artifact store did not return an immutable copy matching "
                f"{path.name}"
            )
    else:
        stored_path = _local_snapshot(path, raw=raw, digest=digest).resolve()
        uri = stored_path.as_uri()
    return {
        "title": title,
        "format": fmt,
        "uri": uri,
        "path": str(stored_path),
        "mime": mime,
        "size_bytes": len(raw),
        "sha256": digest,
    }


def _stored_value(stored: Any, field: str) -> Any:
    if isinstance(stored, Mapping):
        return stored.get(field)
    return getattr(stored, field, None)


async def inspect_preview(
    html_path: Path,
    out_dir: Path,
    *,
    scale: float,
    expected_source_figure_sha256s: set[str] | None = None,
) -> dict[str, Any]:
    """Run the optional Chromium inspector and normalize its host outcome."""

    try:
        result = await browser_inspection.inspect_document(
            html_path,
            out_dir,
            scale=scale,
            expected_source_figure_sha256s=expected_source_figure_sha256s,
        )
    except Exception as exc:  # noqa: BLE001 - browser tooling is optional
        return {
            **poster_core.outcome_result(
                "inspection_unavailable",
                summary="Rendered inspection is unavailable; review HTML manually.",
            ),
            "warnings": [{"code": "inspection_unavailable", "message": str(exc)}],
        }
    normalized = poster_core.normalize_outcome_result(
        result,
        fallback_code="inspection_unavailable",
        fallback_summary="Rendered inspection returned an invalid result.",
    )
    if not isinstance(result, dict):
        normalized["warnings"] = [
            {"code": "invalid_inspection", "message": "Expected an object."}
        ]
    return normalized


def inspection_report_path(inspection: dict[str, Any], directory: Path) -> Path:
    """Return an existing inspector report or materialize a normalized fallback."""

    raw = str(inspection.get("report_path") or "").strip()
    path = Path(raw) if raw else directory / "dom-report.json"
    if not path.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "dom-report.json"
        path.write_text(
            json.dumps(inspection, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return path


def replace_file_atomic(path: Path, content: bytes) -> None:
    """Replace a non-symlink file atomically after creating its parent directory."""

    if path.is_symlink():
        raise OSError(f"atomic destination may not be a symbolic link: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def write_json_atomic(
    path: Path,
    value: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = True,
    allow_nan: bool = True,
) -> None:
    """Serialize UTF-8 JSON with a trailing newline and replace its file atomically."""

    serialized = json.dumps(
        value,
        ensure_ascii=False,
        indent=indent,
        sort_keys=sort_keys,
        allow_nan=allow_nan,
    )
    replace_file_atomic(path, (serialized + "\n").encode("utf-8"))


def bounded_float(value: Any, *, default: float, low: float, high: float) -> float:
    """Coerce a numeric runtime option into a closed interval."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


async def progress(callback: Any, stage: str, pct: float, **data: Any) -> None:
    """Report progress through the current host callback contract."""

    if callback is None:
        return
    try:
        result = callback(stage, pct, **data)
    except TypeError:
        result = callback(stage, pct)
    if inspect.isawaitable(result):
        await result


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_").lower()[:48]


def _local_snapshot(path: Path, *, raw: bytes, digest: str) -> Path:
    target = path.parent / ".snapshots" / digest / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise OSError(f"snapshot target must not be a symlink: {target}")
    try:
        with target.open("xb") as handle:
            handle.write(raw)
    except FileExistsError:
        pass
    if target.read_bytes() != raw:
        raise OSError(f"snapshot content collision: {target}")
    target.chmod(0o444)
    return target
