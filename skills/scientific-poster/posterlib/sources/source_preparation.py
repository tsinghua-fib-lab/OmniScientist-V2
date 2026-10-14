"""Grounding-source and asset preparation for scientific-poster actions."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import image_assets

from posterlib.runtime import runtime_io

from . import latex_source, paper_extraction, paper_inputs, pdf_worker


@dataclass(frozen=True)
class DraftSource:
    """Grounding text and source figures prepared before authoring."""

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
    latex_dir = str(input_data.get("latex_project_dir") or "").strip()
    arxiv_id = str(input_data.get("arxiv_id") or "").strip()
    if input_data.get("main_tex") and not latex_dir:
        raise paper_extraction.PaperSourceError(
            "invalid_payload", "main_tex requires latex_project_dir"
        )
    if sum(bool(value) for value in (source_pdf, latex_dir, arxiv_id)) > 1:
        raise paper_extraction.PaperSourceError(
            "invalid_payload", "Supply one paper_path, latex_project_dir, or arxiv_id"
        )
    if not (source_pdf or latex_dir or arxiv_id):
        text = await resolve_explicit_source_text(input_data, ctx=ctx)
        return DraftSource(
            text=text,
            authoring_request=authoring_request(input_data),
            assets=(),
            warnings=(),
            summary={"kind": "text", "character_count": len(text)},
        )

    await runtime_io.progress(progress_callback, "poster.prepare-source", 0.04)
    if latex_dir:
        project = Path(latex_dir).expanduser()
        if not project.is_absolute() and input_data.get("cwd"):
            project = Path(str(input_data["cwd"])) / project
        prepared_latex = await asyncio.to_thread(
            latex_source.prepare_latex_project,
            project,
            workspace / "latex-figures",
            main_tex=str(input_data.get("main_tex") or "").strip(),
        )
        figures = prepared_latex["figures"]
        return DraftSource(
            text=str(prepared_latex["text"]),
            authoring_request=authoring_request(input_data),
            assets=tuple(
                {
                    "path": figure["path"],
                    "description": figure["caption"] or figure["carrier_label"],
                    "source_kind": "latex_figure",
                    "content_sha256": figure["sha256"],
                    "figure_number": figure["figure_number"],
                    "carrier_number": figure["carrier_number"],
                    "carrier_label": figure["carrier_label"],
                    "caption": figure["caption"],
                    "context": figure["context"],
                }
                for figure in figures
            ),
            warnings=tuple(prepared_latex["warnings"]),
            summary={
                "kind": "latex",
                "path": prepared_latex["path"],
                "title": prepared_latex["title"],
                "authors": prepared_latex["authors"],
                "affiliations": list(prepared_latex["affiliations"]),
                "figure_count": len(figures),
                "equation_count": len(prepared_latex["equations"]),
                "equations": prepared_latex["equations"],
                "figures": figures,
            },
        )
    if arxiv_id:
        pdf_path = await asyncio.to_thread(
            paper_inputs.download_arxiv_pdf,
            arxiv_id,
            workspace / "arxiv-paper",
        )
    else:
        pdf_path = await runtime_io.resolve_path(
            ctx,
            source_pdf,
            base_dir=input_data.get("cwd"),
        )
    if pdf_path is None:
        raise paper_extraction.PaperSourceError(
            "source_not_found",
            f"The requested PDF could not be resolved: {source_pdf}",
        )
    prepared = await pdf_worker.prepare_pdf_async(
        pdf_path,
        workspace / "paper-figures",
    )
    assets = tuple(
        {
            "path": figure["path"],
            "description": _pdf_asset_description(figure),
            "source_kind": str(figure.get("source_kind") or "pdf_figure"),
            "content_sha256": figure["sha256"],
            **(
                {"figure_number": figure["figure_number"]}
                if "figure_number" in figure
                else {}
            ),
            "carrier_number": figure.get("carrier_number"),
            "carrier_label": figure.get("carrier_label"),
            "page": figure["page"],
            "crop_bbox": figure["crop_bbox"],
            "extraction_mode": figure["extraction_mode"],
            **(
                {"caption": str(figure["caption"])}
                if str(figure.get("caption") or "").strip()
                else {}
            ),
            **(
                {"context": str(figure["context"])}
                if str(figure.get("context") or "").strip()
                else {}
            ),
        }
        for figure in prepared.figures
    )
    warnings = (
        ()
        if assets
        else (
            (
                "The PDF text was extracted, but no caption- or layout-anchored visual "
                "carriers were found; the poster must use grounded HTML/CSS diagrams "
                "instead."
            ),
        )
    )
    return DraftSource(
        text=prepared.text,
        authoring_request=authoring_request(input_data),
        assets=assets,
        warnings=prepared.warnings + warnings,
        summary={
            "kind": "pdf",
            "path": str(pdf_path),
            "title": prepared.title,
            "authors": prepared.authors,
            "affiliations": list(prepared.affiliations),
            "page_count": prepared.page_count,
            "figure_count": len(prepared.figures),
            "equation_count": len(prepared.equations),
            "equations": [dict(item) for item in prepared.equations],
            "figures": [
                {
                    "source_kind": str(
                        figure.get("source_kind") or "pdf_figure"
                    ),
                    "carrier_number": figure.get("carrier_number"),
                    "carrier_label": figure.get("carrier_label"),
                    **(
                        {"figure_number": figure["figure_number"]}
                        if "figure_number" in figure
                        else {}
                    ),
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
    return image_assets.prepare_asset_manifest(
        raw_values,
        resolve=lambda item: resolved.get(item),
    )


def structured_source_equations(summary: dict[str, Any]) -> list[dict[str, str]]:
    """Return optional LaTeX transcription shortcuts from a structured source."""

    raw = summary.get("equations")
    if not isinstance(raw, list):
        return []
    return [
        {
            "latex": str(item.get("latex") or ""),
            "source_label": str(item.get("source_label") or ""),
            "context": str(item.get("context") or ""),
        }
        for item in raw
        if isinstance(item, dict)
        and str(item.get("latex") or "").strip()
        and str(item.get("source_label") or "").strip()
    ]


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
        or _has_value(input_data.get("latex_project_dir"))
        or _has_value(input_data.get("arxiv_id"))
        or any(
            _has_value(input_data.get(name))
            for name in ("source_text", "research")
        )
        or _has_value(input_data.get("source"))
    )


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
        raise paper_extraction.PaperSourceError(
            "source_read_failed",
            "source must be a local path or artifact URI for a UTF-8 text file",
        )
    path = await runtime_io.resolve_path(
        ctx,
        raw_source.strip(),
        base_dir=input_data.get("cwd"),
    )
    if path is None:
        raise paper_extraction.PaperSourceError(
            "source_not_found",
            f"The requested grounding file could not be resolved: {raw_source}",
        )
    try:
        return await asyncio.to_thread(path.read_text, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise paper_extraction.PaperSourceError("source_read_failed", str(exc)) from exc


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
    normalized.pop("carrier_number", None)
    normalized.pop("carrier_label", None)
    normalized.pop("page", None)
    normalized.pop("crop_bbox", None)
    normalized.pop("extraction_mode", None)
    return normalized


def _pdf_asset_description(figure: dict[str, Any]) -> str:
    label = str(figure.get("carrier_label") or "PDF visual carrier").strip()
    return f"{label} from source PDF, page {figure['page']}"


def _asset_source(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("uri", "path", "source", "file"):
            if value.get(key):
                return str(value[key]).strip()
        return ""
    return str(value or "").strip()


async def restore_checkpoint_assets(
    state: dict[str, Any], ctx: Any
) -> list[dict[str, Any]]:
    """Rebuild the original token-to-byte manifest and reject changed assets."""

    assets, _warnings = await prepare_assets(state["asset_inputs"], ctx)
    asset_sha256s = sorted(str(item["content_sha256"]) for item in assets)
    expected_figures = {str(item["content_sha256"]) for item in assets if image_assets.is_source_figure_asset(item)}
    planned_asset_sha256s = [str(item) for item in state["asset_sha256s"]]
    planned_source_figures = [str(item) for item in state["source_figure_sha256s"]]
    if (
        asset_sha256s != planned_asset_sha256s
        or sorted(expected_figures) != planned_source_figures
    ):
        missing_assets = sorted(set(planned_asset_sha256s) - set(asset_sha256s))
        unexpected_assets = sorted(set(asset_sha256s) - set(planned_asset_sha256s))
        missing_figures = sorted(set(planned_source_figures) - expected_figures)
        unexpected_figures = sorted(expected_figures - set(planned_source_figures))
        raise paper_extraction.PaperSourceError(
            "source_read_failed",
            "Prepared poster assets changed after the plan checkpoint: "
            + json.dumps(
                {
                    "missing_assets": missing_assets,
                    "unexpected_assets": unexpected_assets,
                    "missing_source_figures": missing_figures,
                    "unexpected_source_figures": unexpected_figures,
                },
                sort_keys=True,
            ),
        )
    return assets
