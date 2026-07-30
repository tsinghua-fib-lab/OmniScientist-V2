"""PDF full-text extraction (optional ``pypdf``, pure-offline).

Ingesting a paper's *full text* instead of just its abstract is the single
biggest quality lever for grounded retrieval. That needs a PDF parser, which is
a heavyweight dependency, so ``pypdf`` is **optional**: install ``omni[pdf]`` to
enable it. Without it, :func:`pdf_available` returns ``False`` and the ingest
paths degrade to the abstract (exactly today's behaviour) — omni stays
local-first and every existing test keeps passing.

Extraction is page-aware: :func:`extract_pdf_pages` returns one string per page
so chunks can carry a ``page`` locator for precise ``[S#]`` citations.
"""

from __future__ import annotations

import os
from pathlib import Path

_TRIED = False
_PYPDF: object | None = None


def _pypdf() -> object | None:
    """Lazily import ``pypdf`` once; cache the module (or ``None`` if absent)."""
    global _TRIED, _PYPDF
    if _TRIED:
        return _PYPDF
    _TRIED = True
    if os.environ.get("OMNI_DISABLE_PYPDF"):
        _PYPDF = None
        return None
    try:
        import pypdf

        _PYPDF = pypdf
    except Exception:  # noqa: BLE001 — any import failure → graceful fallback
        _PYPDF = None
    return _PYPDF


def pdf_available() -> bool:
    """Whether PDF full-text extraction is available (``pypdf`` importable)."""
    return _pypdf() is not None


def extract_pdf_pages(data: bytes) -> list[str]:
    """Extract text per page from PDF ``data``; ``[]`` when unavailable/empty.

    Never raises on a malformed PDF or a missing parser — retrieval must degrade
    gracefully, not crash ingest. A page that yields no extractable text (e.g. a
    scanned image without OCR) becomes an empty string, preserving page indices.
    """
    pypdf = _pypdf()
    if pypdf is None or not data:
        return []
    import io

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception:  # noqa: BLE001 — corrupt/encrypted PDF → no text
        return []
    pages: list[str] = []
    for page in getattr(reader, "pages", []):
        try:
            pages.append((page.extract_text() or "").strip())
        except Exception:  # noqa: BLE001 — one bad page shouldn't lose the rest
            pages.append("")
    return pages


def extract_pdf_text(data: bytes) -> str:
    """Extract all text from PDF ``data`` as one string (``""`` when empty)."""
    return "\n\n".join(p for p in extract_pdf_pages(data) if p).strip()


def read_pdf_pages(path: str | Path) -> list[str]:
    """Read a local PDF file and extract its pages; ``[]`` on any failure."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    return extract_pdf_pages(data)


__all__ = [
    "pdf_available",
    "extract_pdf_pages",
    "extract_pdf_text",
    "read_pdf_pages",
]
