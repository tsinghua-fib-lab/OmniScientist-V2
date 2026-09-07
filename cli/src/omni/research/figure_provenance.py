"""Bind a figure artifact to the script or DOT that produced it."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

FIGURE_PROVENANCE_EVENT = "artifact.provenance"
_SCRIPT_SUFFIXES = (".dot", ".gv", ".py", ".mmd")


def sibling_source_script(path: str | Path) -> str:
    """Return a same-stem DOT/script next to ``path``, if one exists."""
    raw = Path(str(path or ""))
    if not raw.name:
        return ""
    for suffix in _SCRIPT_SUFFIXES:
        candidate = raw.with_suffix(suffix)
        if candidate.is_file():
            return str(candidate)
    return ""


def figure_provenance(
    artifact: Any,
    *,
    tool_trace: Sequence[Any] = (),
) -> dict[str, Any]:
    """Describe how a figure was produced. Missing script is recorded, not invented."""
    path = str(
        getattr(artifact, "path", "")
        or getattr(artifact, "rel_path", "")
        or (artifact.get("path") if isinstance(artifact, dict) else "")
        or ""
    )
    uri = str(
        getattr(artifact, "uri", "")
        or (artifact.get("uri") if isinstance(artifact, dict) else "")
        or ""
    )
    meta = getattr(artifact, "meta", None)
    if meta is None and isinstance(artifact, dict):
        meta = artifact.get("meta") or artifact.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    source_script = str(meta.get("source_script") or meta.get("source_dot") or "").strip()
    if not source_script:
        source_script = sibling_source_script(path)
    if not source_script:
        source_script = _script_from_trace(tool_trace, path)
    method = _method_for_script(source_script)
    if not method and not source_script:
        method = "Producer did not leave a DOT/script sibling."
    return {
        "uri": uri,
        "path": path,
        "source_script": source_script,
        "has_provenance": bool(source_script),
        "method": method,
        "package": bool(source_script),
    }


def slide_artifact_paths(artifacts: Sequence[Any]) -> list[str]:
    """PPTX / slide paths so review can see a deck without reading markdown."""
    found: list[str] = []
    seen: set[str] = set()
    for artifact in artifacts or []:
        kind = str(getattr(artifact, "kind", "") or "").lower()
        path = str(getattr(artifact, "path", "") or getattr(artifact, "rel_path", "") or "")
        suffix = Path(path).suffix.lower()
        if suffix not in {".pptx", ".ppt", ".key"} and kind not in {
            "slides",
            "pptx",
            "presentation",
        }:
            continue
        if path and path not in seen:
            seen.add(path)
            found.append(path)
    return found


def format_figure_packages(payload: dict[str, Any] | None) -> list[str]:
    data = payload if isinstance(payload, dict) else {}
    figures = data.get("figures") if isinstance(data.get("figures"), list) else []
    lines: list[str] = []
    for item in figures[:6]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("uri") or "figure")
        name = Path(path).name or path
        script = Path(str(item.get("source_script") or "")).name
        method = str(item.get("method") or "").strip()
        if item.get("has_provenance") and script:
            lines.append(f"{name} ← {script}" + (f" ({method})" if method else ""))
        elif method:
            lines.append(f"{name}: {method}")
        else:
            lines.append(f"{name}: no source script on record")
    return lines


def _method_for_script(source_script: str) -> str:
    suffix = Path(source_script).suffix.lower()
    if suffix in {".dot", ".gv"}:
        return "Rendered from sibling Graphviz/DOT."
    if suffix == ".py":
        return "Rendered from the producer Python script."
    if suffix == ".mmd":
        return "Rendered from a Mermaid source."
    if source_script:
        return "Bound to the producer source file."
    return ""


def figures_missing_script(artifacts: Sequence[Any], *, tool_trace: Sequence[Any] = ()) -> list[str]:
    """Paths of figure artifacts that have no source script or DOT sibling."""
    missing: list[str] = []
    for artifact in artifacts:
        kind = str(getattr(artifact, "kind", "") or "").lower()
        mime = str(getattr(artifact, "mime", "") or "").lower()
        path = str(getattr(artifact, "path", "") or getattr(artifact, "rel_path", "") or "")
        suffix = Path(path).suffix.lower()
        if suffix in {".pptx", ".ppt", ".key"} or kind in {
            "slides",
            "pptx",
            "presentation",
        }:
            continue
        is_figure = kind == "figure" or mime.startswith("image/") or suffix in {
            ".png", ".svg", ".jpg", ".jpeg", ".webp",
        }
        if not is_figure:
            continue
        report = figure_provenance(artifact, tool_trace=tool_trace)
        if not report["has_provenance"]:
            missing.append(path or report.get("uri") or "figure")
    return missing


def _script_from_trace(tool_trace: Sequence[Any], figure_path: str) -> str:
    stem = Path(figure_path).stem.lower() if figure_path else ""
    for record in tool_trace or []:
        arguments = getattr(record, "arguments", None)
        if not isinstance(arguments, dict):
            continue
        path = str(arguments.get("path") or "")
        if not path:
            continue
        suffix = Path(path).suffix.lower()
        if suffix not in _SCRIPT_SUFFIXES:
            continue
        if stem and Path(path).stem.lower() == stem:
            return path
        if stem and stem in Path(path).stem.lower():
            return path
    return ""


__all__ = [
    "FIGURE_PROVENANCE_EVENT",
    "figure_provenance",
    "figures_missing_script",
    "format_figure_packages",
    "sibling_source_script",
    "slide_artifact_paths",
]
