"""Grounding-source and asset preparation for scientific-poster actions."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import poster_assets

from posterlib.runtime import runtime_io

from . import paper_source


@dataclass(frozen=True)
class DraftSource:
    """Grounding text and figures prepared before poster authoring begins."""

    text: str
    authoring_request: str
    assets: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    summary: dict[str, Any]


async def prepare_draft_source(
    input_data: dict[str, Any],
    *,
    ctx: Any,
    workspace: Path,
    progress_callback: Any,
) -> DraftSource:
    """Prepare a complete local source before any HTML authoring model call."""

    source_pdf = paper_path(input_data) or attachment_uri(input_data)
    if not source_pdf:
        text = await resolve_explicit_source_text(input_data, ctx=ctx)
        return DraftSource(
            text=text,
            authoring_request=authoring_request(input_data),
            assets=(),
            warnings=(),
            summary={"kind": "text", "character_count": len(text)},
        )

    await runtime_io.progress(progress_callback, "poster.prepare-source", 0.04)
    pdf_path = await runtime_io.resolve_path(
        ctx,
        source_pdf,
        base_dir=input_data.get("cwd"),
    )
    if pdf_path is None:
        raise paper_source.PaperSourceError(
            "source_not_found",
            f"The requested PDF could not be resolved: {source_pdf}",
        )
    prepared = await asyncio.to_thread(
        paper_source.prepare_pdf,
        pdf_path,
        workspace / "paper-figures",
    )
    assets = tuple(
        {
            "path": figure["path"],
            "description": _figure_description(figure),
            "source_kind": "pdf_figure",
            "content_sha256": figure["sha256"],
            "figure_number": figure["figure_number"],
            "page": figure["page"],
            "crop_bbox": figure["crop_bbox"],
            "extraction_mode": figure["extraction_mode"],
        }
        for figure in prepared.figures
    )
    warnings = (
        ()
        if assets
        else (
            "The PDF text was extracted, but no caption-anchored figures were found; "
            "the poster must use grounded HTML/CSS diagrams instead.",
        )
    )
    return DraftSource(
        text=prepared.text,
        authoring_request=authoring_request(input_data),
        assets=assets,
        warnings=warnings,
        summary={
            "kind": "pdf",
            "path": str(pdf_path),
            "title": prepared.title,
            "authors": prepared.authors,
            "page_count": prepared.page_count,
            "figure_count": len(prepared.figures),
            "figures": [
                {
                    "figure_number": figure["figure_number"],
                    "page": figure["page"],
                    "crop_bbox": figure["crop_bbox"],
                    "extraction_mode": figure["extraction_mode"],
                    "sha256": figure["sha256"],
                }
                for figure in prepared.figures
            ],
        },
    )


async def prepare_assets(
    values: Any,
    ctx: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve local assets and build the portable core manifest."""

    raw_values = normalize_asset_inputs(values)
    resolved: dict[str, Path | None] = {}
    for value in raw_values:
        source = _asset_source(value)
        if source and source not in resolved:
            resolved[source] = await runtime_io.resolve_path(ctx, source)
    return poster_assets.prepare_asset_manifest(
        raw_values,
        resolve=lambda item: resolved.get(item),
    )


def paper_path(input_data: dict[str, Any]) -> str:
    """Return the single explicit canonical PDF input."""

    explicit = input_data.get("paper_path")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return ""


def attachment_uri(input_data: dict[str, Any]) -> str:
    """Return the CLI's canonical attached file when no paper_path was supplied."""

    value = input_data.get("file_uri")
    return value.strip() if isinstance(value, str) else ""


def has_draft_source(input_data: dict[str, Any]) -> bool:
    """Return whether a draft or estimate action has grounded input."""

    return bool(
        paper_path(input_data)
        or attachment_uri(input_data)
        or source_text(input_data)
        or _has_value(input_data.get("source"))
    )


def source_text(input_data: dict[str, Any]) -> str:
    """Normalize inline grounding without treating instructions as evidence."""

    value: Any = ""
    for key in ("source_text", "research"):
        candidate = input_data.get(key)
        if candidate not in (None, "", {}, []):
            value = candidate
            break
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return str(value).strip()


def explicit_source_text(input_data: dict[str, Any]) -> str:
    """Return inline grounding fields; ``source`` is always a UTF-8 file."""

    for key in ("source_text", "research"):
        value = input_data.get(key)
        if value in (None, "", {}, []):
            continue
        if isinstance(value, str):
            return value.strip()
        try:
            return json.dumps(
                value, ensure_ascii=False, sort_keys=True, allow_nan=False
            )
        except (TypeError, ValueError):
            return str(value).strip()
    return ""


async def resolve_explicit_source_text(
    input_data: dict[str, Any],
    *,
    ctx: Any,
) -> str:
    """Resolve inline grounding or read ``source`` with portable file semantics."""

    inline = explicit_source_text(input_data)
    if inline:
        return inline
    raw_source = input_data.get("source")
    if not _has_value(raw_source):
        return ""
    if not isinstance(raw_source, str):
        raise paper_source.PaperSourceError(
            "source_read_failed",
            "source must be a local path or artifact URI for a UTF-8 text file",
        )
    path = await runtime_io.resolve_path(
        ctx,
        raw_source.strip(),
        base_dir=input_data.get("cwd"),
    )
    if path is None:
        raise paper_source.PaperSourceError(
            "source_not_found",
            f"The requested grounding file could not be resolved: {raw_source}",
        )
    try:
        return await asyncio.to_thread(path.read_text, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise paper_source.PaperSourceError("source_read_failed", str(exc)) from exc


def authoring_request(input_data: dict[str, Any]) -> str:
    """Combine user-owned authoring directions without admitting them as evidence."""

    values = []
    for key in ("input", "instructions", "workflow_goal"):
        value = input_data.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in values:
            values.append(value.strip())
    return "\n\n".join(values)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return bool(value)
    return True


def normalize_asset_inputs(values: Any) -> list[Any]:
    """Normalize a scalar or iterable asset payload to a list."""

    if values is None:
        return []
    if isinstance(values, (str, Path, dict)):
        return [values]
    try:
        return list(values)
    except TypeError:
        return [values]


def normalize_user_asset(value: Any) -> Any:
    """Prevent caller-supplied images from impersonating extracted PDF figures."""

    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    normalized["source_kind"] = "user_asset"
    normalized.pop("content_sha256", None)
    normalized.pop("figure_number", None)
    normalized.pop("page", None)
    normalized.pop("crop_bbox", None)
    normalized.pop("extraction_mode", None)
    return normalized


def _figure_description(figure: dict[str, Any]) -> str:
    parts = [f"Figure {figure['figure_number']} from source PDF, page {figure['page']}"]
    caption = str(figure.get("caption") or "").strip()
    context = str(figure.get("context") or "").strip()
    if caption:
        parts.append(f"caption: {caption}")
    if context:
        parts.append(f"paper discussion: {context}")
    return ". ".join(parts)


def _asset_source(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("uri", "path", "source", "file"):
            if value.get(key):
                return str(value[key]).strip()
        return ""
    return str(value or "").strip()
