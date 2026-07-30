"""openalex-search engine — query OpenAlex and index results into the corpus."""

from __future__ import annotations

from typing import Any

from omni.research import connectors
from omni.research.engine_util import index_results, resolve_connector
from omni.research.literature_select import (
    fetch_window,
    format_literature_hits,
    select_relevant_papers,
)


class OpenAlexSearchEngine:
    async def execute(self, progress_callback: Any = None, **input_data: Any) -> dict[str, Any]:
        ctx = getattr(self, "ctx", None)
        if ctx is None or getattr(ctx, "db", None) is None:
            return {"status": "error", "outcome": {"code": "missing_store"},
                    "error": "openalex-search requires a workspace store"}
        query = str(input_data.get("query") or input_data.get("input") or "").strip()
        if not query:
            return {"status": "error", "outcome": {"code": "missing_query"},
                    "error": "openalex-search needs a 'query'"}
        rows = int(input_data.get("max_results", 8) or 8)
        resolved = resolve_connector(ctx, "openalex")
        if resolved is None:
            fallback = await _funnel_fallback(
                ctx, query, rows, reason="The OpenAlex connector is disabled."
            )
            if fallback is not None:
                return fallback
            return {"status": "error", "outcome": {"code": "connector_disabled"},
                    "error": "The OpenAlex connector is disabled in research.connectors."}
        email = resolved.secrets.get("contact_email", "")
        if progress_callback:
            await progress_callback("querying OpenAlex", 0.3)
        try:
            results = await connectors.openalex_search(
                query, rows=fetch_window(rows), email=email
            )
        except connectors.ConnectorError as exc:
            fallback = await _funnel_fallback(
                ctx, query, rows, reason=f"OpenAlex search failed: {exc}"
            )
            if fallback is not None:
                rem = str(getattr(exc, "remediation", "") or "")
                if rem:
                    fallback["warning"] = f"{fallback['warning']} {rem}".strip()
                return fallback
            summary = f"OpenAlex search failed: {exc}"
            rem = str(getattr(exc, "remediation", "") or "")
            return {
                "status": "error",
                "outcome": {"code": "network_error"},
                "query": query,
                "indexed": 0,
                "error": str(exc),
                "summary": summary,
                "warning": rem,
                "recoverable": True,
                "blocking": False,
                "error_info": {
                    "code": "network_error",
                    "message": str(exc),
                    "retryable": True,
                    "workflow_recoverable": True,
                    "kind": str(getattr(getattr(exc, "kind", None), "value", "") or ""),
                    "remediation": rem,
                },
            }
        if not results:
            summary = "OpenAlex returned no results."
            return {
                "status": "partial",
                "outcome": {"code": "empty_results"},
                "query": query,
                "indexed": 0,
                "summary": summary,
                "warning": summary,
                "recoverable": True,
                "blocking": False,
                "error_info": {
                    "code": "empty_results",
                    "message": summary,
                    "retryable": True,
                    "workflow_recoverable": True,
                },
            }
        if progress_callback:
            await progress_callback("indexing", 0.7)
        kept, dropped = select_relevant_papers(query, results, keep=rows)
        out = await index_results(ctx, kept, origin="openalex", query=query)
        if dropped:
            out["dropped"] = len(dropped)
            out["outcome"] = {
                **(out.get("outcome") or {}),
                "dropped": len(dropped),
                "fetched": len(results),
            }
            summary = str(out.get("summary") or "").rstrip()
            out["summary"] = (
                f"{summary}\nDropped {len(dropped)} off-topic keyword hits."
            )
        if progress_callback:
            # A native milestone: the exact counts only this engine knows, so the
            # completion line reads "found N · indexed M" instead of a bare stage.
            try:
                await progress_callback(
                    "done",
                    1.0,
                    stage_id="literature.done",
                    milestone="Literature search complete",
                    stats={"found": len(results), "indexed": int(out.get("indexed", 0) or 0)},
                )
            except TypeError:
                await progress_callback("done", 1.0)
        return out


async def _funnel_fallback(
    ctx: Any, query: str, rows: int, *, reason: str
) -> dict[str, Any] | None:
    """When OpenAlex cannot answer, use the host funnel's other connectors.

    This skill stays OpenAlex-first. A quota or kill-switch must not strand a
    literature request that arXiv / Crossref / PubMed / Semantic Scholar can
    still satisfy — the same fan-out research-ideation already uses.
    """
    from omni.research import search_literature

    research = getattr(getattr(ctx, "settings", None), "research", None)
    others = [
        str(name)
        for name in (getattr(research, "connectors", None) or [])
        if str(name) and str(name) != "openalex"
    ]
    if not others:
        return None
    fallback = await search_literature(ctx, query=query, rows=rows, sources=others)
    results, _dropped = select_relevant_papers(
        query, list(fallback.get("results") or []), keep=rows
    )
    if not results:
        return None
    used = ", ".join(str(name) for name in (fallback.get("connectors") or []) if name)
    warning = f"{reason} Fell back to {used or 'other literature sources'}."
    hits = format_literature_hits(results)
    summary = f"{warning}\n{hits}" if hits else warning
    return {
        "status": "partial",
        "outcome": {"code": "openalex_degraded_fallback"},
        "query": query,
        "indexed": int(fallback.get("indexed", 0) or 0),
        "results": results,
        "providers": fallback.get("providers") or [],
        "warning": warning,
        "summary": summary,
        "recoverable": True,
        "blocking": False,
        "error_info": {
            "code": "openalex_degraded_fallback",
            "message": reason,
            "retryable": True,
            "workflow_recoverable": True,
        },
    }
