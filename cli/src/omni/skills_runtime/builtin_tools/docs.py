"""Self-knowledge docs tools: docs_search, docs_read.

omni's own documentation (``cli/docs`` in source, ``omni/data/docs`` when
installed) is exposed to the agent as a read-only self-knowledge corpus so
questions about omni itself (architecture, storage, memory, commands, usage)
are answered from the docs and cited — instead of the model flailing on the
filesystem or guessing.

Security: these tools are hard-scoped to the docs directory. They serve only
``.md`` files under it, block path traversal, and can never reach source code,
config, ``.env`` or ``secrets.toml``. There is no keyword router deciding when
to consult docs — the model chooses ``docs_search`` (nudged by the system
prompt); this stays a single-brain design.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from omni.core.react_agent import ToolSpec
from omni.data import DOCS_DIR
from omni.skills_runtime.context import ExecContext, Tool

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SNIPPET_CHARS = 320
_MAX_FILE_CHARS = 60_000


def _tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    return [token for token in _TOKEN_RE.findall(normalized) if len(token) >= 2]


def _iter_docs(docs_dir: Path) -> list[Path]:
    if not docs_dir.is_dir():
        return []
    return sorted(p for p in docs_dir.rglob("*.md") if p.is_file())


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) sections by ATX headings."""
    sections: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            if buf or heading:
                sections.append((heading, "\n".join(buf).strip()))
            heading = line.lstrip("#").strip()
            buf = []
        else:
            buf.append(line)
    if buf or heading:
        sections.append((heading, "\n".join(buf).strip()))
    return sections or [("", text.strip())]


def _snippet(heading: str, body: str, q_tokens: set[str]) -> str:
    lines = [ln for ln in body.splitlines() if ln.strip()]
    for ln in lines:
        if q_tokens & set(_tokenize(ln)):
            snip = ln.strip()
            break
    else:
        snip = " ".join(lines)[:_SNIPPET_CHARS] if lines else ""
    if len(snip) > _SNIPPET_CHARS:
        snip = snip[:_SNIPPET_CHARS] + "…"
    return f"{heading}｜{snip}" if heading else snip


def _resolve_doc(docs_dir: Path, name: str) -> Path | None:
    """Resolve a doc name to a path strictly inside ``docs_dir`` (.md only)."""
    name = (name or "").strip().strip("/")
    if not name:
        return None
    if not name.lower().endswith(".md"):
        name += ".md"
    try:
        candidate = (docs_dir / name).resolve()
        candidate.relative_to(docs_dir.resolve())
    except (ValueError, OSError):
        return None
    return candidate if candidate.is_file() else None


def build_docs_tools(ctx: ExecContext) -> list[Tool]:  # noqa: ARG001 - ctx kept for signature parity
    docs_dir = DOCS_DIR

    def _available() -> list[str]:
        return [str(p.relative_to(docs_dir)) for p in _iter_docs(docs_dir)]

    async def docs_search(args: dict) -> Any:
        query = str(args.get("query", "")).strip()
        if not query:
            return {"error": "query is required"}
        k = max(1, int(args.get("k", 5) or 5))
        q_tokens = _tokenize(query)
        q_set = set(q_tokens)
        if not q_set:
            return {"status": "empty", "matches": [], "available_docs": _available()}
        scored: list[tuple[float, str, str, str]] = []
        for path in _iter_docs(docs_dir):
            rel = str(path.relative_to(docs_dir))
            text = path.read_text(encoding="utf-8", errors="ignore")
            for heading, body in _split_sections(text):
                counts = Counter(_tokenize(f"{heading} {heading} {heading} {body}"))
                score = sum(counts[t] for t in q_set)
                if score <= 0:
                    continue
                scored.append((float(score), rel, heading, _snippet(heading, body, q_set)))
        if not scored:
            return {
                "status": "empty",
                "matches": [],
                "available_docs": _available(),
                "note": "No matching section was found. Use docs_read on one of the listed documents.",
            }
        scored.sort(key=lambda r: r[0], reverse=True)
        matches = [
            {"doc": rel, "heading": heading, "snippet": snip}
            for _score, rel, heading, snip in scored[:k]
        ]
        return {
            "status": "ok",
            "matches": matches,
            "note": "Matches come from Omni's bundled documentation. Use docs_read(<doc>) for the full text and cite the document name.",
        }

    async def docs_read(args: dict) -> str:
        name = str(args.get("doc", args.get("name", args.get("path", "")))).strip()
        if not name:
            return "Available bundled documents:\n" + "\n".join(_available())
        path = _resolve_doc(docs_dir, name)
        if path is None:
            return (
                f"ERROR: bundled document '{name}' was not found; only bundled Markdown files are readable. Available documents:\n"
                + "\n".join(_available())
            )
        return path.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_CHARS]

    return [
        Tool(
            ToolSpec(
                "docs_search",
                "Search Omni's bundled documentation about architecture, storage, memory, commands, and usage. Returns sections with document names.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "k": {"type": "integer", "description": "Maximum sections to return; default 5"},
                    },
                    "required": ["query"],
                },
            ),
            docs_search,
        ),
        Tool(
            ToolSpec(
                "docs_read",
                "Read a bundled Omni Markdown document. Omit doc to list available documents.",
                {"type": "object", "properties": {"doc": {"type": "string"}}},
            ),
            docs_read,
        ),
    ]
