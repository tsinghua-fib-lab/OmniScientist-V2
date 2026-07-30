"""openalex-search engine — query OpenAlex and index results into the corpus."""

from __future__ import annotations

from typing import Any

from omni.research import connectors
from omni.research.engine_util import index_results, resolve_connector


class OpenAlexSearchEngine:
    async def execute(self, progress_callback: Any = None, **input_data: Any) -> dict[str, Any]:
        ctx = getattr(self, "ctx", None)
        if ctx is None or getattr(ctx, "db", None) is None:
            return {"status": "error", "outcome": {"code": "missing_store"},
                    "error": "openalex-search requires a workspace store"}
        resolved = resolve_connector(ctx, "openalex")
        if resolved is None:
            return {"status": "error", "outcome": {"code": "connector_disabled"},
                    "error": "The OpenAlex connector is disabled in research.connectors."}
        query = str(input_data.get("query") or input_data.get("input") or "").strip()
        if not query:
            return {"status": "error", "outcome": {"code": "missing_query"},
                    "error": "openalex-search needs a 'query'"}
        rows = int(input_data.get("max_results", 8) or 8)
        email = resolved.secrets.get("contact_email", "")
        if progress_callback:
            await progress_callback("querying OpenAlex", 0.3)
        try:
            results = await connectors.openalex_search(query, rows=rows, email=email)
        except connectors.ConnectorError as exc:
            summary = f"OpenAlex search failed: {exc}"
            return {
                "status": "error",
                "outcome": {"code": "network_error"},
                "query": query,
                "indexed": 0,
                "error": str(exc),
                "summary": summary,
                "recoverable": True,
                "blocking": False,
                "error_info": {
                    "code": "network_error",
                    "message": str(exc),
                    "retryable": True,
                    "workflow_recoverable": True,
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
        out = await index_results(ctx, results, origin="openalex", query=query)
        if progress_callback:
            await progress_callback("done", 1.0)
        return out
