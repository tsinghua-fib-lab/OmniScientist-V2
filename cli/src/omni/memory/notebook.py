"""Lab notebook (``NOTEBOOK.md``) — human-readable, git-friendly research log."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

_HEADER = "# Lab Notebook\n\n> Maintained by OmniScientist and safe to edit manually.\n"


def append_entry(notebook: Path, title: str, body: str, *, tags: list[str] | None = None) -> None:
    notebook.parent.mkdir(parents=True, exist_ok=True)
    if not notebook.exists():
        notebook.write_text(_HEADER, encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    tagline = (" " + " ".join(f"#{t}" for t in tags)) if tags else ""
    block = f"\n## {stamp} — {title}{tagline}\n\n{body.strip()}\n"
    with notebook.open("a", encoding="utf-8") as fh:
        fh.write(block)


def read_recent(notebook: Path, *, max_chars: int = 1500) -> str:
    if not notebook.exists():
        return ""
    text = notebook.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]
