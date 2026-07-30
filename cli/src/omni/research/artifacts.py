"""Structured, auditable research artifact builders."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any

from omni.research.repro import sha256_file

FIGURE_BUNDLE_SCHEMA = "omni.figure_bundle/v1"


async def build_evidence_table(
    *,
    store: Any,
    artifacts: Any,
    session_id: str = "",
    task_id: str = "",
    title: str = "Evidence table",
    limit: int = 200,
) -> dict[str, Any]:
    """Export claim→evidence→source bindings as Markdown and CSV artifacts."""
    claims = await store.list_claims(limit=limit, session_id=session_id)
    rows: list[dict[str, Any]] = []
    source_ids: list[str] = []
    evidence_ids: list[str] = []
    for claim in reversed(claims):
        evidence = await store.evidence_for_claim(claim.id)
        if not evidence:
            rows.append(_evidence_row(claim, None, None))
            continue
        for edge in evidence:
            source = await store.get_source(edge.source_id) if edge.source_id else None
            rows.append(_evidence_row(claim, edge, source))
            evidence_ids.append(edge.id)
            if edge.source_id:
                source_ids.append(edge.source_id)

    fields = [
        "claim_id", "claim", "polarity", "confidence", "evidence_id", "stance",
        "strength", "source_id", "source", "locator", "quote",
    ]
    csv_buffer = io.StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    markdown = _evidence_markdown(title, rows)
    meta = {
        "schema": "omni.evidence_table/v1",
        "claim_ids": [claim.id for claim in claims],
        "evidence_ids": _dedup(evidence_ids),
        "source_ids": _dedup(source_ids),
    }
    csv_artifact = await artifacts.put_bytes(
        csv_buffer.getvalue().encode("utf-8"), kind="evidence", title=f"{title} CSV",
        ext="csv", mime="text/csv", session_id=session_id, task_id=task_id, meta=meta,
    )
    md_artifact = await artifacts.put_bytes(
        markdown.encode("utf-8"), kind="evidence", title=f"{title} Markdown",
        ext="md", mime="text/markdown", session_id=session_id, task_id=task_id, meta=meta,
    )
    return {
        "status": "ok",
        "kind": "evidence_table",
        "summary": f"Exported {len(rows)} evidence rows for {len(claims)} claims.",
        "artifacts": [_artifact_dict(csv_artifact, "csv"), _artifact_dict(md_artifact, "markdown")],
        **meta,
    }


async def build_research_notebook(
    *,
    store: Any,
    artifacts: Any,
    session_id: str = "",
    task_id: str = "",
    title: str = "Research notebook",
    limit: int = 100,
) -> dict[str, Any]:
    """Create a portable snapshot of hypotheses, sources, claims, and runs."""
    hypotheses = await store.list_hypotheses(limit=limit)
    if session_id:
        hypotheses = [item for item in hypotheses if item.session_id == session_id]
    sources = await store.list_sources(limit=limit)
    claims = await store.list_claims(limit=limit, session_id=session_id)
    runs = await store.list_runs(limit=limit, session_id=session_id)
    source_labels = {source.id: f"S{index}" for index, source in enumerate(reversed(sources), start=1)}
    lines = [f"# {title}", "", "## Hypotheses", ""]
    if hypotheses:
        lines.extend(
            f"- `{item.id[:8]}` [{item.status}; {item.confidence:.0%}] {item.statement}"
            for item in reversed(hypotheses)
        )
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Claims and evidence", ""])
    if claims:
        for claim in reversed(claims):
            edges = await store.evidence_for_claim(claim.id)
            refs = [source_labels.get(edge.source_id, edge.source_id[:8]) for edge in edges if edge.source_id]
            suffix = f" [{', '.join(refs)}]" if refs else " [unsupported]"
            lines.append(f"- `{claim.id[:8]}` ({claim.confidence:.0%}) {claim.text}{suffix}")
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Sources", ""])
    if sources:
        for source in reversed(sources):
            label = source_labels[source.id]
            identifier = source.doi or source.arxiv_id or source.url
            lines.append(f"- [{label}] {source.title or '(untitled)'}" + (f" — {identifier}" if identifier else ""))
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Experiment runs", ""])
    if runs:
        for run in reversed(runs):
            metrics = ", ".join(f"{key}={value}" for key, value in list((run.metrics or {}).items())[:8])
            lines.append(
                f"- `{run.id[:8]}` [{run.status}] {run.title or run.cmd or '(untitled run)'}"
                + (f"; {metrics}" if metrics else "")
                + (f"; seed={run.seed}" if run.seed is not None else "")
            )
    else:
        lines.append("- None recorded.")
    lines.append("")
    meta = {
        "schema": "omni.research_notebook/v1",
        "source_ids": [item.id for item in sources],
        "claim_ids": [item.id for item in claims],
        "run_ids": [item.id for item in runs],
    }
    artifact = await artifacts.put_bytes(
        "\n".join(lines).encode("utf-8"), kind="notebook", title=title,
        ext="md", mime="text/markdown", session_id=session_id, task_id=task_id, meta=meta,
    )
    return {
        "status": "ok",
        "kind": "research_notebook",
        "summary": (
            f"Snapshot: {len(hypotheses)} hypotheses, {len(claims)} claims, "
            f"{len(sources)} sources, {len(runs)} runs."
        ),
        "artifacts": [_artifact_dict(artifact, "markdown")],
        **meta,
    }


async def build_figure_bundle(
    *,
    artifacts: Any,
    figure_uri: str,
    code_uri: str,
    data_uris: list[str],
    run_ids: list[str],
    session_id: str = "",
    task_id: str = "",
    subtask_id: str = "",
    title: str = "Scientific figure bundle",
) -> dict[str, Any]:
    """Bind a figure to the exact code, data bytes, and experiment runs behind it."""
    if not figure_uri or not code_uri or not data_uris or not run_ids:
        return {
            "status": "error",
            "error": "figure_uri, code_uri, at least one data_uri, and run_ids are required",
        }
    figure = await _bundle_entry(artifacts, figure_uri)
    code = await _bundle_entry(artifacts, code_uri)
    data = [await _bundle_entry(artifacts, uri) for uri in data_uris]
    missing = [
        uri
        for uri, entry in [
            (figure_uri, figure),
            (code_uri, code),
            *zip(data_uris, data, strict=True),
        ]
        if entry is None
    ]
    if missing:
        return {"status": "error", "error": f"artifact not found: {', '.join(missing)}"}
    manifest = {
        "schema": FIGURE_BUNDLE_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "title": title,
        "figure": figure,
        "code": code,
        "data": data,
        "run_ids": _dedup(run_ids),
    }
    stored = await artifacts.put_bytes(
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        kind="bundle",
        title=title,
        ext="figure-bundle.json",
        mime="application/json",
        session_id=session_id,
        task_id=task_id,
        subtask_id=subtask_id,
        meta={
            "schema": FIGURE_BUNDLE_SCHEMA,
            "figure_uri": figure_uri,
            "code_uri": code_uri,
            "data_uris": list(data_uris),
            "run_ids": _dedup(run_ids),
        },
    )
    await artifacts.set_meta(
        figure_uri,
        {
            "figure_bundle": {
                "schema": FIGURE_BUNDLE_SCHEMA,
                "manifest_uri": stored.uri,
                "run_ids": _dedup(run_ids),
            }
        },
    )
    return {
        "status": "ok",
        "manifest_uri": stored.uri,
        "manifest": manifest,
        "figure_uri": figure_uri,
        "code_uri": code_uri,
        "data_uris": list(data_uris),
        "run_ids": _dedup(run_ids),
    }


async def verify_figure_bundle(*, artifacts: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    """Re-hash the managed artifacts and evaluate figure/code/data consistency."""
    from omni.eval.research_quality import evaluate_figure_consistency

    actual_hashes: dict[str, str] = {}
    entries = [manifest.get("figure"), manifest.get("code"), *(manifest.get("data") or [])]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        uri = str(entry.get("uri") or "")
        path = await artifacts.resolve_path(uri) if uri else None
        actual_hashes[uri] = sha256_file(path) if path is not None and path.is_file() else ""
    result = evaluate_figure_consistency({"manifest": manifest, "actual_hashes": actual_hashes})
    return result.to_dict()


async def _bundle_entry(artifacts: Any, uri: str) -> dict[str, Any] | None:
    path = await artifacts.resolve_path(uri)
    if path is None or not path.is_file():
        return None
    row = await artifacts.get(uri)
    return {
        "uri": uri,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "mime": str(getattr(row, "mime", "") or "application/octet-stream"),
    }


def _evidence_row(claim: Any, edge: Any, source: Any) -> dict[str, Any]:
    return {
        "claim_id": claim.id,
        "claim": claim.text,
        "polarity": claim.polarity,
        "confidence": claim.confidence,
        "evidence_id": getattr(edge, "id", ""),
        "stance": getattr(edge, "stance", ""),
        "strength": getattr(edge, "strength", ""),
        "source_id": getattr(source, "id", "") or getattr(edge, "source_id", ""),
        "source": getattr(source, "title", ""),
        "locator": getattr(edge, "locator", ""),
        "quote": getattr(edge, "quote", ""),
    }


def _evidence_markdown(title: str, rows: list[dict[str, Any]]) -> str:
    lines = [f"# {title}", "", "| Claim | Stance | Source | Locator | Quote |", "|---|---|---|---|---|"]
    for row in rows:
        lines.append("| " + " | ".join(
            _md_cell(row[key]) for key in ("claim", "stance", "source", "locator", "quote")
        ) + " |")
    if not rows:
        lines.append("| No claims recorded |  |  |  |  |")
    return "\n".join(lines) + "\n"


def _md_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _artifact_dict(artifact: Any, fmt: str) -> dict[str, Any]:
    return {
        "title": artifact.title,
        "format": fmt,
        "uri": artifact.uri,
        "path": str(artifact.path),
        "mime": artifact.mime,
        "size_bytes": artifact.size_bytes,
    }


def _dedup(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


__all__ = [
    "FIGURE_BUNDLE_SCHEMA",
    "build_evidence_table",
    "build_figure_bundle",
    "build_research_notebook",
    "verify_figure_bundle",
]
