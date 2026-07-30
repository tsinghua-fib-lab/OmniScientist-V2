"""Shared glue for connector skill engines (ingest results → corpus + library).

Enablement and secret-scope are delegated to :class:`ConnectorRegistry` so the
connector contract (which sources exist, which are enabled, and which secrets
each may read) lives in exactly one place.
"""

from __future__ import annotations

from typing import Any

from omni.research.corpus import ingest_many
from omni.research.literature_select import format_literature_hits
from omni.research.registry import ConnectorRegistry, ResolvedConnector
from omni.research.store import ResearchStore


def connector_enabled(ctx: Any, name: str) -> bool:
    """Whether connector ``name`` is enabled in ``research.connectors``."""
    return ConnectorRegistry(ctx.settings).is_enabled(name)


def resolve_connector(ctx: Any, name: str) -> ResolvedConnector | None:
    """Resolve an *enabled* connector bound to its scoped secrets.

    Returns ``None`` when the connector is unknown or disabled — the engine
    should surface a ``connector_disabled`` error in that case. On success the
    engine reads credentials only from :attr:`ResolvedConnector.secrets`, never
    from the raw settings, so each connector sees only its own scoped secrets.
    """
    registry = ConnectorRegistry(ctx.settings)
    if not registry.is_enabled(name):
        return None
    return registry.resolve(name)


def _save_to_library(ctx: Any, papers: list[dict[str, Any]]) -> None:
    paths = getattr(ctx, "paths", None)
    if paths is None or not papers:
        return
    try:
        from omni.memory.library import add_papers

        add_papers(paths.library, papers)
    except Exception:  # noqa: BLE001
        pass


async def index_results(
    ctx: Any, results: list[dict[str, Any]], *, origin: str, query: str = ""
) -> dict[str, Any]:
    """Ingest connector ``results`` into the corpus + library; return a summary dict."""
    store = ResearchStore(ctx.db)
    research = getattr(ctx.settings, "research", None)
    as_of = getattr(research, "as_of", "") or ""
    target_words = int(getattr(research, "chunk_target_words", 180) or 180)
    ingested = await ingest_many(
        store, ctx.llm, results, origin=origin, date_pin=as_of, target_words=target_words
    )
    indexed = sum(1 for r in ingested if not r["deduped"])
    deduped = sum(1 for r in ingested if r["deduped"])
    _save_to_library(ctx, results)
    hits = format_literature_hits(results)
    summary = (
        f"{origin}: indexed {indexed} new works ({deduped} duplicates removed)."
    )
    if hits:
        summary = f"{summary}\n{hits}"
    summary = (
        f"{summary}\nUse omni lit or search_corpus for grounded citations."
    )
    return {
        "status": "ok",
        "outcome": {"code": "indexed", "indexed": indexed, "deduped": deduped},
        "query": query, "indexed": indexed, "deduped": deduped,
        "results": results,
        "sources": [{"source_id": r["source_id"], "title": r["title"],
                     "chunks": r["chunks_added"]} for r in ingested],
        "summary": summary,
    }


__all__ = ["index_results", "connector_enabled", "resolve_connector"]
