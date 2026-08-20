"""Health-aware literature retrieval facade.

This is the single funnel every "search the literature" path flows through —
the ``search_literature`` builtin tool and the ``research-ideation`` skill both
call :func:`search_literature` here. It turns a query into a resilient
cross-connector fan-out:

* **Health gate** — skip connectors whose breaker is open (a spent quota, a
  dead credential) instead of stalling on them again (BUG-01's 40s stall).
* **Concurrent fan-out** — query the usable connectors with ``asyncio.gather``
  rather than a serial ``await`` loop.
* **Structured failure** — each connector error is classified
  (transient/quota/auth/terminal) and recorded against the breaker.
* **Local-corpus floor** — when every live source comes back empty, fall back
  to the offline corpus so an air-gapped or fully rate-limited run still
  grounds *something*.
* **Three-state outcome** — ``ok`` / ``partial`` / ``empty`` plus per-provider
  diagnostics and remediation. It **never raises**: a failure is data the model
  and the workflow layer can act on, not a process abort.
"""

from __future__ import annotations

import asyncio
from typing import Any

from omni.research.http_policy import failure_from_exception
from omni.research.literature_select import fetch_window, select_relevant_papers
from omni.research.provider_health import shared_provider_health
from omni.research.registry import ConnectorRegistry

# Compact projection for the model-facing ``results`` list. ``summary`` is kept
# (abstracts are what ideation/grounding actually use); everything else is
# identity/metadata for de-dup and citation.
_LIT_FIELDS = (
    "title", "authors", "year", "doi", "arxiv_id", "url", "venue", "summary", "origin",
    "source_id",
    "category", "nct_id", "status", "phases", "conditions", "interventions",
    "primary_outcomes", "has_results",
)


def _paper_key(paper: dict[str, Any]) -> str:
    """Stable identity for cross-connector de-dup: DOI ▸ arXiv id ▸ title."""
    for key in ("doi", "arxiv_id"):
        val = str(paper.get(key) or "").strip().lower()
        if val:
            return f"{key}:{val}"
    return "title:" + " ".join(str(paper.get("title") or "").lower().split())


def _dedup_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """De-dup aggregated connector hits and project to the compact return shape."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for p in papers:
        key = _paper_key(p)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({k: p.get(k) for k in _LIT_FIELDS if p.get(k)})
    return out


async def _run_connector(name: str, query: str, rows: int, reg: Any) -> list[dict[str, Any]]:
    """Dispatch one connector search with its scoped secrets (never cross-read)."""
    from omni.research import connectors as _c

    resolved = reg.resolve(name)
    secrets = dict(resolved.secrets) if resolved is not None else {}
    email = str(secrets.get("contact_email", "") or "")
    if name == "openalex":
        return await _c.openalex_search(query, rows=fetch_window(rows), email=email)
    if name == "crossref":
        return await _c.crossref_search(query, rows=rows, email=email)
    if name == "pubmed":
        return await _c.pubmed_search(query, rows=rows, email=email)
    if name == "semanticscholar":
        return await _c.semanticscholar_search(
            query, rows=rows, api_key=str(secrets.get("semantic_scholar_api_key", "") or "")
        )
    if name == "biorxiv":
        return await _c.biorxiv_search(query, rows=rows)
    if name == "clinicaltrials":
        return await _c.clinicaltrials_search(query, rows=rows)
    if name == "arxiv":
        from omni.research.arxiv import search as arxiv_search

        found = await arxiv_search(query, max_results=rows)
        for r in found:
            r.setdefault("origin", "arxiv")
        return found
    return []


def _candidate_order(reg: ConnectorRegistry, requested: list[str], settings: Any) -> list[str]:
    """Which literature connectors to try, in preference order.

    An explicit ``sources`` subset is honoured verbatim (still enablement- and
    capability-filtered). Otherwise: configured ``literature_provider_order``,
    then domain-pack recommendations, then any remaining enabled source — so new
    connectors join the chain by registration, not code changes.
    """
    enabled_specs = reg.enabled()
    enabled = [s.name for s in enabled_specs]
    lit = {s.name for s in enabled_specs if "literature.search" in s.provides}
    if requested:
        return [n for n in requested if n in enabled and n in lit]
    research = getattr(settings, "research", None)
    configured = [n for n in (getattr(research, "literature_provider_order", []) or []) if n in lit]
    from omni.research.domain_packs import DomainPackRegistry

    recommended = DomainPackRegistry(settings).recommended_connectors(available=lit)
    ordered: list[str] = []
    seen: set[str] = set()
    for name in [*configured, *recommended, *enabled]:
        if name in lit and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


async def _corpus_fallback(ctx: Any, query: str) -> list[dict[str, Any]]:
    """Offline floor: map local-corpus passages to paper-shaped dicts."""
    store = _store(ctx)
    if store is None or getattr(ctx, "llm", None) is None:
        return []
    from omni.research.corpus import search_corpus

    research = getattr(ctx.settings, "research", None)
    try:
        passages = await search_corpus(
            store, ctx.llm, query,
            k=int(getattr(research, "corpus_top_k", 6) or 6),
            as_of=str(getattr(research, "as_of", "") or ""),
            hybrid=bool(getattr(research, "hybrid_rerank", True)),
            rrf_k=int(getattr(research, "rrf_k", 60) or 60),
            vector_backend=str(getattr(ctx.settings.memory, "vector_backend", "auto") or "auto"),
        )
    except Exception:  # noqa: BLE001 — the corpus is a best-effort floor
        return []
    return [
        {
            "title": p.title, "authors": [], "year": p.year, "doi": p.doi,
            "arxiv_id": p.arxiv_id, "url": p.url, "summary": p.text,
            "origin": "local_corpus",
        }
        for p in passages
    ]


def _store(ctx: Any) -> Any:
    from omni.research.store import ResearchStore

    return ResearchStore(ctx.db) if getattr(ctx, "db", None) is not None else None


async def search_literature(
    ctx: Any,
    *,
    query: str,
    rows: int = 6,
    sources: list[str] | None = None,
    index: bool = True,
    corpus_fallback: bool | None = None,
) -> dict[str, Any]:
    """Health-aware, concurrent literature search across enabled connectors.

    Returns a structured result (never raises) with ``status`` in
    ``ok``/``partial``/``empty``, the ``results`` list, ``providers``
    diagnostics, and ``remediation`` hints.
    """
    settings = ctx.settings
    research = getattr(settings, "research", None)
    query = str(query or "").strip()
    rows = max(1, min(int(rows or 6), 25))
    if corpus_fallback is None:
        corpus_fallback = bool(getattr(research, "corpus_fallback", True))

    reg = ConnectorRegistry(settings)
    health = shared_provider_health(
        getattr(ctx, "paths", None),
        default_cooldown_s=float(getattr(research, "provider_cooldown_s", 1800.0) or 1800.0),
    )
    requested = [str(s).strip().lower() for s in (sources or []) if str(s).strip()]
    candidates = _candidate_order(reg, requested, settings)

    providers: list[dict[str, Any]] = []
    remediation: list[str] = []
    usable: list[str] = []
    for name in candidates:
        avail = reg.connector_availability(name, health=health)
        if avail.usable:
            usable.append(name)
        else:
            providers.append({
                "name": name, "state": avail.state, "reason": avail.reason,
                "retry_after": avail.retry_after,
            })
            if avail.remediation:
                remediation.append(avail.remediation)
    # Try 'available' before 'degraded' (public-tier sources rank lower).
    usable.sort(key=lambda n: reg.connector_availability(n, health=health).state != "available")

    if not candidates:
        return _result(
            status="empty", query=query, connectors=[], results=[], indexed=0, deduped=0,
            per_source={}, errors=[], providers=providers,
            remediation=remediation or ["Enable literature connectors with `/config set research.connectors`."],
            note="No literature connector is enabled for this query.",
        )

    async def _fetch(name: str) -> tuple[str, list[dict[str, Any]], Any]:
        try:
            found = await _run_connector(name, query, rows, reg)
            return name, found, None
        except Exception as exc:  # noqa: BLE001 — classified below; never propagate
            return name, [], failure_from_exception(exc, name)

    fetched = await asyncio.gather(*[_fetch(n) for n in usable]) if usable else []

    per_source: dict[str, Any] = {}
    errors: list[str] = []
    aggregated: list[dict[str, Any]] = []
    indexed = deduped = 0
    any_failure = bool(providers)  # breaker-skipped/unavailable already count as degradation
    for name, found, failure in fetched:
        if failure is not None:
            any_failure = True
            health.record_failure(
                name, failure.kind, retry_after=failure.retry_after,
                remediation=failure.remediation, message=str(failure),
            )
            errors.append(f"{name}: {failure}")
            per_source[name] = {"error": str(failure)[:160]}
            providers.append({
                "name": name, "state": "failed", "error_kind": failure.kind.value,
                "retry_after": failure.retry_after,
            })
            if failure.remediation:
                remediation.append(failure.remediation)
            continue
        health.record_success(name)
        if not found:
            per_source[name] = {"found": 0}
            providers.append({"name": name, "state": "empty", "found": 0})
            continue
        if index:
            try:
                summary = await _index(ctx, found, origin=name, query=query, keep=rows)
                kept = _stamp_source_ids(
                    list(summary.get("results") or []),
                    summary.get("sources") or [],
                )
                indexed += int(summary.get("indexed", 0))
                deduped += int(summary.get("deduped", 0))
                per_source[name] = {
                    "found": len(found), "kept": len(kept),
                    "indexed": summary.get("indexed", 0),
                    "deduped": summary.get("deduped", 0), "sources": summary.get("sources", []),
                }
            except Exception as exc:  # noqa: BLE001 — an index hiccup must not drop hits
                errors.append(f"{name} index: {exc}")
                per_source[name] = {"found": len(found), "error": str(exc)[:160]}
                kept = found
        else:
            kept, _dropped = select_relevant_papers(query, found, keep=rows)
            per_source[name] = {"found": len(found), "kept": len(kept)}
        providers.append({"name": name, "state": "ok", "found": len(found)})
        aggregated.extend(kept)

    compact = _dedup_papers(aggregated)
    corpus_used = False
    if not compact and corpus_fallback:
        corpus_hits = await _corpus_fallback(ctx, query)
        if corpus_hits:
            corpus_used = True
            compact = _dedup_papers(corpus_hits)
            providers.append({"name": "local_corpus", "state": "ok", "found": len(compact)})

    if compact:
        status = "partial" if (any_failure or corpus_used) else "ok"
    else:
        status = "empty"

    note = _note(status, corpus_used=corpus_used)
    if status == "empty" and not remediation:
        remediation = [
            "No results from live sources or the local corpus. "
            "Retry later (quotas reset), broaden the query, or index sources first."
        ]
    compact = _stamp_source_ids(compact, _sources_from_per_source(per_source))
    source_ids = _unique_ids(
        [*_source_ids_from_per_source(per_source), *(_id_of(item) for item in compact)]
    )
    return _result(
        status=status, query=query, connectors=usable, results=compact,
        indexed=indexed, deduped=deduped, per_source=per_source, errors=errors,
        providers=providers, remediation=_dedup_str(remediation), note=note,
        source_ids=source_ids,
    )


async def _index(
    ctx: Any, found: list[dict[str, Any]], *, origin: str, query: str, keep: int
) -> dict[str, Any]:
    from omni.research.engine_util import index_results

    kept, _dropped = select_relevant_papers(query, found, keep=keep)
    return await index_results(ctx, kept, origin=origin, query=query)


def _note(status: str, *, corpus_used: bool) -> str:
    if corpus_used:
        return ("Live sources returned nothing; results are an offline fallback from the local "
                "corpus (origin=local_corpus). Use search_corpus for grounding.")
    if status == "empty":
        return "No results. See providers/remediation for why and what to try next."
    if status == "partial":
        return ("Some sources were skipped or failed (see providers); usable results were indexed "
                "into the local corpus. Use search_corpus for grounding and cite_source for provenance.")
    return ("Indexed into the local corpus. Use search_corpus for grounding and "
            "cite_source/add_evidence for provenance.")


def _dedup_str(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = str(item).strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _stamp_source_ids(results: list[dict[str, Any]], sources: list[Any]) -> list[dict[str, Any]]:
    by_title: dict[str, str] = {}
    for src in sources:
        if not isinstance(src, dict):
            continue
        sid = str(src.get("source_id") or "").strip()
        title = str(src.get("title") or "").strip()
        if sid and title:
            by_title[title] = sid
    for item in results:
        if not isinstance(item, dict) or item.get("source_id"):
            continue
        title = str(item.get("title") or "").strip()
        if title in by_title:
            item["source_id"] = by_title[title]
    return results


def _sources_from_per_source(per_source: dict[str, Any]) -> list[Any]:
    found: list[Any] = []
    for row in per_source.values():
        if isinstance(row, dict):
            found.extend(row.get("sources") or [])
    return found


def _source_ids_from_per_source(per_source: dict[str, Any]) -> list[str]:
    return _unique_ids(_id_of(item) for item in _sources_from_per_source(per_source))


def _id_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("source_id") or "").strip()
    return str(item or "").strip()


def _unique_ids(items: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _result(
    *, status: str, query: str, connectors: list[str], results: list[dict[str, Any]],
    indexed: int, deduped: int, per_source: dict[str, Any], errors: list[str],
    providers: list[dict[str, Any]], remediation: list[str], note: str,
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    from omni.runtime.engine_observation import attach_engine_observation

    ids = list(source_ids or [])
    if not ids:
        ids = _unique_ids(
            [*_source_ids_from_per_source(per_source), *(_id_of(item) for item in results)]
        )
    payload: dict[str, Any] = {
        "status": status,
        "query": query,
        "connectors": connectors,
        "count": len(results),
        "indexed": indexed,
        "deduped": deduped,
        "results": results,
        "per_source": per_source,
        "errors": errors,
        "providers": providers,
        "remediation": remediation,
        "note": note,
    }
    if ids:
        payload["source_ids"] = ids
    return attach_engine_observation(payload, payload)


__all__ = ["search_literature"]
