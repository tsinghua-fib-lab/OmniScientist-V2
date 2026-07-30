"""Project reference library (``library.jsonl``).

A newline-delimited JSON file of papers the agent has fetched or searched. It
is the data source for ``omni cite export`` (BibTeX / JSON / CSV) and the
human-readable counterpart of M5 artifact memory. Entries are de-duplicated by
arXiv id, DOI, then normalised title so repeated lookups don't pile up.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_WS_RE = re.compile(r"\s+")
_NONWORD_RE = re.compile(r"[^a-z0-9]+")


def _norm_title(title: str) -> str:
    return _WS_RE.sub(" ", (title or "").strip().lower())


def _dedup_key(entry: dict[str, Any]) -> str:
    for field in ("arxiv_id", "doi"):
        val = str(entry.get(field) or "").strip().lower()
        if val:
            return f"{field}:{val}"
    return "title:" + _norm_title(str(entry.get("title") or ""))


def _coerce_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a paper-ish dict into the library schema."""
    published = str(raw.get("published") or raw.get("updated") or "")
    year = str(raw.get("year") or "").strip()
    if not year:
        m = re.search(r"(\d{4})", published)
        if m:
            year = m.group(1)
    authors = raw.get("authors") or []
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(",") if a.strip()]
    return {
        "arxiv_id": str(raw.get("arxiv_id") or "").strip(),
        "title": _WS_RE.sub(" ", str(raw.get("title") or "").strip()),
        "authors": list(authors),
        "year": year,
        "published": published,
        "doi": str(raw.get("doi") or "").strip(),
        "abs_url": str(raw.get("abs_url") or raw.get("url") or "").strip(),
        "pdf_url": str(raw.get("pdf_url") or "").strip(),
        "summary": _WS_RE.sub(" ", str(raw.get("summary") or "").strip()),
        "source": str(raw.get("source") or raw.get("origin") or "arxiv").strip(),
    }


def load_library(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def add_papers(path: Path, papers: list[dict[str, Any]]) -> int:
    """Append new papers to ``library.jsonl`` (deduped). Returns count added.

    Best-effort: filesystem/permission errors are swallowed so a failed library
    write never breaks the originating skill.
    """
    candidates = [_coerce_entry(p) for p in papers if (p.get("title") or p.get("arxiv_id"))]
    if not candidates:
        return 0
    try:
        existing = {_dedup_key(e) for e in load_library(path)}
        new_rows: list[dict[str, Any]] = []
        for entry in candidates:
            key = _dedup_key(entry)
            if key in existing:
                continue
            existing.add(key)
            entry["added_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            new_rows.append(entry)
        if not new_rows:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for row in new_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return len(new_rows)
    except OSError:
        return 0


# ── export formats ───────────────────────────────────────────────────────


def _bibtex_key(entry: dict[str, Any]) -> str:
    first_author = ""
    if entry.get("authors"):
        first_author = _NONWORD_RE.sub("", str(entry["authors"][0]).split()[-1].lower())
    year = entry.get("year") or "n.d."
    stub = entry.get("arxiv_id") or _NONWORD_RE.sub("", _norm_title(entry.get("title", ""))[:12])
    return f"{first_author or 'anon'}{year}{stub}".strip("_") or "ref"


def to_bibtex(entries: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        key = _bibtex_key(entry)
        uniq = key
        n = 1
        while uniq in seen:
            n += 1
            uniq = f"{key}{chr(97 + n - 1)}"
        seen.add(uniq)
        authors = " and ".join(entry.get("authors") or []) or "Unknown"
        fields = [
            ("title", entry.get("title", "")),
            ("author", authors),
            ("year", entry.get("year", "")),
        ]
        if entry.get("arxiv_id"):
            fields.append(("eprint", entry["arxiv_id"]))
            fields.append(("archivePrefix", "arXiv"))
        if entry.get("doi"):
            fields.append(("doi", entry["doi"]))
        url = entry.get("abs_url") or entry.get("pdf_url") or ""
        if url:
            fields.append(("url", url))
        body = ",\n".join(
            f"  {k} = {{{v}}}" for k, v in fields if str(v).strip()
        )
        blocks.append(f"@article{{{uniq},\n{body}\n}}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_csv(entries: list[dict[str, Any]]) -> str:
    import csv
    import io

    cols = ["arxiv_id", "title", "authors", "year", "doi", "abs_url", "pdf_url"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols)
    for entry in entries:
        row = []
        for col in cols:
            val = entry.get(col, "")
            if isinstance(val, list):
                val = "; ".join(str(x) for x in val)
            row.append(val)
        writer.writerow(row)
    return buf.getvalue()


__all__ = ["add_papers", "load_library", "to_bibtex", "to_csv"]
