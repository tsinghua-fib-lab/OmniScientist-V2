#!/usr/bin/env python3
"""Portable runner for openalex-search without Omni runtime."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SKILL = "openalex-search"
_TOKEN = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset({"the", "and", "for", "with", "from", "that", "this", "into", "using"})


def _authors(work: dict[str, Any]) -> list[str]:
    return [
        str(author.get("author", {}).get("display_name", ""))
        for author in work.get("authorships", [])
        if author.get("author", {}).get("display_name")
    ]


def _tokens(text: str) -> list[str]:
    return [tok for tok in _TOKEN.findall(str(text or "").lower()) if tok not in _STOP]


def _select(query: str, papers: list[dict[str, Any]], *, keep: int) -> list[dict[str, Any]]:
    """Title-only cousin of ``omni.research.literature_select`` (stdlib, no Omni)."""
    q = _tokens(query)
    if not papers:
        return []
    keep = max(1, int(keep or 1))
    if not q:
        return papers[:keep]

    def score(paper: dict[str, Any]) -> float:
        title = set(_tokens(str(paper.get("title") or "")))
        hits = sum(2.0 if tok in title else 0.0 for tok in q)
        return hits / (2.0 * len(q))

    def distinct(paper: dict[str, Any]) -> int:
        title = set(_tokens(str(paper.get("title") or "")))
        return sum(1 for tok in q if tok in title)

    ranked = sorted(papers, key=score, reverse=True)
    selected: list[dict[str, Any]] = []
    for paper in ranked:
        weak = len(q) >= 3 and distinct(paper) < 2
        effective = 0.0 if weak else score(paper)
        if len(selected) < keep and (effective >= 0.12 or not selected):
            selected.append(paper)
        if len(selected) >= keep:
            break
    return selected or ranked[:1]


def _summary(results: list[dict[str, Any]]) -> str:
    lines = [f"Found {len(results)} OpenAlex works."]
    for index, paper in enumerate(results[:12], start=1):
        title = str(paper.get("title") or "").strip() or "(untitled)"
        year = str(paper.get("year") or "").strip()
        venue = str(paper.get("venue") or "").strip()
        head = f"{index}. {year} · {title}" if year else f"{index}. {title}"
        if venue:
            head = f"{head} ({venue})"
        lines.append(head)
    lines.append("Install Omni to index them into a persistent corpus.")
    return "\n".join(lines)


def run(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query") or payload.get("input") or "").strip()
    if not query:
        return {"status": "error", "skill": SKILL, "error": "query is required"}
    rows = max(1, min(int(payload.get("max_results", 8) or 8), 25))
    fetch = max(rows, min(25, max(rows * 2, rows + 4)))
    params = {"search": query, "per-page": fetch}
    if payload.get("email"):
        params["mailto"] = str(payload["email"])
    url = f"https://api.openalex.org/works?{urllib.parse.urlencode(params)}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "portable-openalex-search/1.0"})
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "skill": SKILL, "query": query, "error": f"unable to reach OpenAlex: {exc}"}
    fetched = []
    for work in data.get("results", []):
        fetched.append({
            "title": work.get("title") or work.get("display_name") or "",
            "authors": _authors(work),
            "year": work.get("publication_year"),
            "doi": work.get("doi") or "",
            "url": work.get("id") or "",
            "venue": (work.get("primary_location") or {}).get("source", {}).get("display_name", ""),
            "summary": "",
        })
    results = _select(query, fetched, keep=rows)
    artifacts: list[dict[str, str]] = []
    if payload.get("output_dir"):
        out_dir = Path(payload["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "openalex-results.json"
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts.append({"title": "OpenAlex results", "format": "json", "path": str(out_path)})
    return {
        "status": "ok",
        "skill": SKILL,
        "query": query,
        "count": len(results),
        "results": results,
        "artifacts": artifacts,
        "summary": _summary(results),
        "provenance": {"sources": [{"kind": "openalex_api", "url": url}]},
    }


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.json_file:
        raw = Path(args.json_file).expanduser().read_text(encoding="utf-8-sig")
    elif args.json:
        raw = args.json
    elif not sys.stdin.isatty():
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
    else:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("input JSON must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the portable openalex-search skill.")
    parser.add_argument("--json", help="Input JSON object.")
    parser.add_argument(
        "--json-file",
        help="UTF-8 JSON file. Prefer this on Windows/PowerShell; --json quoting is unreliable there.",
    )
    parser.add_argument("--self-test", action="store_true", help="Run an offline smoke test.")
    args = parser.parse_args(argv)
    if args.self_test:
        print(json.dumps({"status": "ok", "skill": SKILL, "portable_runner": True}, ensure_ascii=False))
        return 0
    try:
        result = run(_load_payload(args))
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "skill": SKILL, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
