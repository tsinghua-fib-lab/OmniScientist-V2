"""Host projection of literature-funnel facts from a skill or tool result.

Codex shows the search string and hit count in ``function_call_output``. Omni
lifts the same facts onto the ``run_skill`` wrapper so ``compact_observation``
keeps them, and so an empty funnel is not treated as this-turn research.
Counts only what the provider already kept — the host does not re-rank papers.
"""

from __future__ import annotations

from typing import Any

_PAPER_LIST_KEYS = ("papers", "results", "matches", "hits")
_COUNT_KEYS = ("n_kept", "paper_count", "count")

# Retrieve tools that reopen the produce path after a sealed skill emptied.
# ``run_skill`` is omitted: the name alone cannot tell ideation from arxiv-fetch.
RETRIEVE_TOOL_NAMES = frozenset(
    {
        "search_literature",
        "search_corpus",
        "citation_neighbors",
        "arxiv-fetch",
        "arxiv_search",
        "openalex-search",
        "openalex_search",
        "crossref_search",
        "pubmed_search",
        "web_search",
        "web_fetch",
    }
)


def literature_funnel_facts(payload: Any) -> dict[str, Any] | None:
    """Return queries / n_retrieved / n_kept when the payload is literature-shaped.

    ``None`` if this is not a literature result (figure, slides, …).
    """
    block = _funnel_block(payload)
    if block is None:
        return None
    root = payload if isinstance(payload, dict) else {}
    n_kept = _n_kept(block)
    facts: dict[str, Any] = {
        "queries": _queries(block, root),
        "n_retrieved": _n_retrieved(block, n_kept),
        "n_kept": n_kept,
    }
    warning = _warning(root, block)
    if warning:
        facts["warning"] = warning
    return facts


def is_empty_literature_funnel(payload: Any) -> bool:
    """True when a literature-shaped result kept zero papers."""
    facts = literature_funnel_facts(payload)
    return facts is not None and int(facts["n_kept"]) <= 0


def has_literature_hits(payload: Any) -> bool:
    """True when the payload kept at least one paper (funnel or fetch)."""
    facts = literature_funnel_facts(payload)
    if facts is not None:
        return int(facts["n_kept"]) > 0
    return _paper_count(payload) > 0


def project_skill_observation(
    result: Any,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lift funnel facts onto the wrapper. An empty funnel is ``degraded``."""
    payload = dict(extra or {})
    facts = literature_funnel_facts(result)
    empty = facts is not None and int(facts["n_kept"]) <= 0
    current = str(payload.get("status") or "").strip().lower()
    if empty and current not in {
        "failed",
        "cancelled",
        "interrupted",
        "blocked",
        "rejected",
        "error",
        "needs_input",
    }:
        payload["status"] = "degraded"
    elif not current:
        payload["status"] = "succeeded"
    if facts:
        payload["queries"] = facts["queries"]
        payload["n_retrieved"] = facts["n_retrieved"]
        payload["n_kept"] = facts["n_kept"]
        if facts.get("warning"):
            payload["warning"] = facts["warning"]
    elif isinstance(result, dict) and result.get("warning"):
        payload.setdefault("warning", result["warning"])
    payload["result"] = result
    if isinstance(result, dict):
        inner_status = str(result.get("status") or "").strip().lower()
        inner_outcome = result.get("outcome")
        if isinstance(inner_outcome, dict):
            inner_outcome = str(
                inner_outcome.get("code") or inner_outcome.get("status") or ""
            ).strip().lower()
        else:
            inner_outcome = str(inner_outcome or "").strip().lower()
        if inner_status == "needs_input" or inner_outcome == "needs_input":
            payload["status"] = "needs_input"
            payload["outcome"] = "needs_input"
            for key in ("error", "summary", "message", "error_info", "next_actions"):
                if result.get(key) and not payload.get(key):
                    payload[key] = result[key]
    from omni.runtime.engine_observation import attach_engine_observation

    return attach_engine_observation(payload, result, extra=extra)


def _funnel_block(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    steps = payload.get("steps")
    if isinstance(steps, dict) and isinstance(steps.get("search"), dict):
        return steps["search"]
    inner = payload.get("result")
    if isinstance(inner, dict):
        found = _funnel_block(inner)
        if found is not None:
            return found
    if _looks_like_funnel(payload):
        return payload
    return None


def _looks_like_funnel(block: dict[str, Any]) -> bool:
    has_papers = any(key in block for key in (*_PAPER_LIST_KEYS, *_COUNT_KEYS, "n_retrieved"))
    return _has_query(block) and has_papers


def _has_query(block: dict[str, Any]) -> bool:
    queries = block.get("queries")
    if isinstance(queries, list) and any(str(item).strip() for item in queries):
        return True
    return bool(str(block.get("query") or "").strip())


def _queries(block: dict[str, Any], root: dict[str, Any]) -> list[str]:
    raw = block.get("queries")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()][:8]
    query = str(block.get("query") or root.get("query") or "").strip()
    return [query] if query else []


def _n_kept(block: dict[str, Any]) -> int:
    for key in _COUNT_KEYS:
        if key not in block:
            continue
        try:
            return max(0, int(block[key]))
        except (TypeError, ValueError):
            continue
    per_source = block.get("per_source")
    if isinstance(per_source, dict):
        kept = 0
        saw = False
        for row in per_source.values():
            if isinstance(row, dict) and "kept" in row:
                saw = True
                kept += _as_int(row.get("kept"))
        if saw:
            return kept
    return _paper_list_len(block)


def _n_retrieved(block: dict[str, Any], n_kept: int) -> int:
    if "n_retrieved" in block:
        try:
            return max(n_kept, int(block["n_retrieved"]))
        except (TypeError, ValueError):
            pass
    per_source = block.get("per_source")
    if isinstance(per_source, dict):
        found = 0
        saw = False
        for row in per_source.values():
            if isinstance(row, dict) and "found" in row:
                saw = True
                found += _as_int(row.get("found"))
        if saw:
            return max(found, n_kept)
    return n_kept


def _warning(root: dict[str, Any], block: dict[str, Any]) -> str:
    inner = root.get("result") if isinstance(root.get("result"), dict) else {}
    for source in (root, block, inner):
        text = str(source.get("warning") or "").strip()
        if text:
            return text
    return ""


def _paper_count(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    n = _paper_list_len(payload)
    if n:
        return n
    if _single_paper(payload):
        return 1
    inner = payload.get("result")
    if isinstance(inner, dict):
        n = _paper_list_len(inner)
        if n:
            return n
        if _single_paper(inner):
            return 1
    return 0


def _single_paper(block: dict[str, Any]) -> bool:
    if not (block.get("title") and (block.get("arxiv_id") or block.get("doi") or block.get("identifier"))):
        return False
    return str(block.get("status") or "").lower() in {"", "ok", "succeeded"}


def _paper_list_len(block: dict[str, Any]) -> int:
    for key in _PAPER_LIST_KEYS:
        items = block.get(key)
        if not isinstance(items, list) or not items:
            continue
        sample = [item for item in items[:3] if isinstance(item, dict)]
        if sample and any(
            item.get("title") or item.get("arxiv_id") or item.get("doi") or item.get("paper_id")
            for item in sample
        ):
            return len(items)
    return 0


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "RETRIEVE_TOOL_NAMES",
    "has_literature_hits",
    "is_empty_literature_funnel",
    "literature_funnel_facts",
    "project_skill_observation",
]
