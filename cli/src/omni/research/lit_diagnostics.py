"""Retrieval-process facts for presentation and ``/task show``.

FutureHouse-style transparency: which query ran, which connectors answered,
which papers were kept. Data already lives on ``search_literature`` results;
this module projects it into a stable event and a few human lines.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

LIT_DIAGNOSTICS_EVENT = "lit.diagnostics"


def lit_diagnostics_from_payload(payload: Any) -> dict[str, Any] | None:
    """Project one ``search_literature`` / corpus result into diagnostics."""
    if not isinstance(payload, dict):
        return None
    query = str(payload.get("query") or "").strip()
    connectors = [str(item) for item in (payload.get("connectors") or []) if item]
    providers = payload.get("providers") if isinstance(payload.get("providers"), list) else []
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    matches = payload.get("matches") if isinstance(payload.get("matches"), list) else []
    source_ids = [str(item) for item in (payload.get("source_ids") or []) if str(item).strip()]
    kept_titles = _titles(results or matches)
    if not (query or connectors or kept_titles or source_ids or providers):
        return None
    kept_ids = [
        str(item.get("source_id") or item.get("id") or "").strip()
        for item in (results or matches)
        if isinstance(item, dict)
    ]
    kept_ids = [item for item in kept_ids if item]
    return {
        "query": query,
        "connectors": connectors,
        "providers": [
            {
                "name": str(item.get("name") or ""),
                "state": str(item.get("state") or ""),
                "found": item.get("found"),
            }
            for item in providers
            if isinstance(item, dict)
        ],
        "kept_count": len(results or matches),
        "kept_titles": kept_titles[:8],
        "kept_source_ids": list(dict.fromkeys(kept_ids or source_ids))[:12],
        "status": str(payload.get("status") or ""),
        "note": str(payload.get("note") or "")[:240],
    }


def lit_diagnostics_from_sources(*payloads: Any) -> list[dict[str, Any]]:
    """Collect diagnostics from tool results, drained payloads, or events."""
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()

    def consider(payload: Any) -> None:
        item = lit_diagnostics_from_payload(payload)
        if item is None:
            return
        key = (item.get("query") or "", ",".join(item.get("connectors") or []), item.get("kept_count") or 0)
        if key in seen:
            return
        seen.add(key)
        found.append(item)

    def walk(value: Any, *, tool_name: str = "") -> None:
        if isinstance(value, dict):
            name = str(value.get("name") or value.get("tool_name") or tool_name or "")
            if name in {"search_literature", "search_corpus"} or value.get("connectors") or value.get("matches"):
                consider(value)
            result = value.get("result") or value.get("output_json")
            if isinstance(result, dict):
                if name in {"search_literature", "search_corpus"} or result.get("connectors"):
                    consider(result)
            for item in value.values():
                if isinstance(item, (dict, list)):
                    walk(item, tool_name=name)
            return
        if isinstance(value, list):
            for item in value:
                walk(item, tool_name=tool_name)

    for payload in payloads:
        name = str(getattr(payload, "name", "") or "")
        if name in {"search_literature", "search_corpus"}:
            result = getattr(payload, "result", None)
            consider(result if isinstance(result, dict) else None)
            if isinstance(result, dict):
                continue
        event_type = str(getattr(payload, "event_type", "") or "")
        if event_type == LIT_DIAGNOSTICS_EVENT:
            output = getattr(payload, "output_json", None)
            if isinstance(output, dict):
                if isinstance(output.get("searches"), list):
                    for item in output["searches"]:
                        consider(item)
                else:
                    consider(output)
            continue
        if name == "search_literature" or event_type.endswith(".tool.done"):
            output = getattr(payload, "output_json", None)
            tool = str(getattr(payload, "tool_name", "") or name)
            if tool in {"search_literature", "search_corpus"} and isinstance(output, dict):
                consider(output)
        walk(payload)

    return found


def format_lit_diagnostics(items: Sequence[dict[str, Any]]) -> list[str]:
    """Human lines for presentation / ``/task show``."""
    lines: list[str] = []
    for item in items:
        query = str(item.get("query") or "").strip()
        connectors = [str(name) for name in (item.get("connectors") or []) if name]
        kept = int(item.get("kept_count") or 0)
        titles = [str(title) for title in (item.get("kept_titles") or []) if title]
        head = f"Searched {query!r}" if query else "Literature search"
        if connectors:
            head += f" via {', '.join(connectors)}"
        head += f" · kept {kept}"
        lines.append(head)
        for title in titles[:4]:
            lines.append(f"kept: {title}")
        providers = item.get("providers") if isinstance(item.get("providers"), list) else []
        skipped = [
            f"{row.get('name')}:{row.get('state')}"
            for row in providers
            if isinstance(row, dict) and str(row.get("state") or "") not in {"ok", "available", ""}
        ]
        if skipped:
            lines.append("connectors: " + ", ".join(skipped[:6]))
    return lines


def _titles(rows: Sequence[Any]) -> list[str]:
    titles: list[str] = []
    for item in rows:
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("cite") or "").strip()
            if title:
                titles.append(title)
    return titles


__all__ = [
    "LIT_DIAGNOSTICS_EVENT",
    "format_lit_diagnostics",
    "lit_diagnostics_from_payload",
    "lit_diagnostics_from_sources",
]
