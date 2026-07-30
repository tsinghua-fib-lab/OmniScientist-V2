"""Literature corpus: chunking, embedding ingest, and grounded retrieval.

This is the engine behind omni's literature subsystem (PaperQA2-style): a
persistent local corpus of sources + passages with a grounded ``search_corpus``
that returns passages tagged with their source so the agent can cite ``[S#]``
in-text and bind each citation to a row in the ``evidence`` table.

Embeddings reuse the existing ``LLMClient.embed`` surface (inline vectors +
cosine, the same approach :mod:`omni.memory` uses) — no new vector backend.
When embeddings are unavailable (offline ``mock`` still returns deterministic
hash embeddings; a provider without embeddings degrades to keyword overlap) the
retrieval still works, just less precisely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from omni.memory.vectors import similarity_scores
from omni.research.rerank import DEFAULT_RRF_K, reciprocal_rank_fusion
from omni.research.store import ResearchStore

_WORD_RE = re.compile(r"\S+")
_TOKEN_RE = re.compile(r"[0-9a-z]+")
_PARA_RE = re.compile(r"\n\s*\n")


@dataclass
class Passage:
    """A retrieved corpus passage with its source context, for citation."""

    chunk_id: str
    source_id: str
    score: float
    text: str
    title: str
    arxiv_id: str
    doi: str
    url: str
    year: str
    ord: int
    page: int | None = None

    def cite_label(self, index: int) -> str:
        return f"S{index}"

    def to_dict(self, index: int) -> dict[str, Any]:
        out = {
            "cite": self.cite_label(index),
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "score": round(self.score, 4),
            "title": self.title,
            "arxiv_id": self.arxiv_id,
            "doi": self.doi,
            "url": self.url,
            "year": self.year,
            "text": self.text,
        }
        if self.page is not None:
            out["page"] = self.page
        return out


def chunk_text(
    text: str, *, target_words: int = 180, overlap: int = 30, section: str = ""
) -> list[dict[str, Any]]:
    """Split ``text`` into overlapping word-windows, paragraph-aware.

    Paragraphs are accumulated up to ``target_words``; long paragraphs are split
    with a small ``overlap`` so a sentence straddling a boundary is still
    retrievable. Returns chunk dicts (``ord``/``section``/``text``/``tokens``).
    """
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in _PARA_RE.split(text) if p.strip()]
    chunks: list[dict[str, Any]] = []
    buf: list[str] = []
    buf_words = 0

    def flush() -> None:
        nonlocal buf, buf_words
        if buf:
            body = "\n\n".join(buf).strip()
            if body:
                chunks.append({"ord": len(chunks), "section": section,
                               "text": body, "tokens": len(_WORD_RE.findall(body))})
            buf = []
            buf_words = 0

    for para in paragraphs:
        words = _WORD_RE.findall(para)
        if len(words) > target_words:
            flush()
            step = max(1, target_words - overlap)
            for start in range(0, len(words), step):
                window = words[start : start + target_words]
                if not window:
                    continue
                body = " ".join(window)
                chunks.append({"ord": len(chunks), "section": section,
                               "text": body, "tokens": len(window)})
                if start + target_words >= len(words):
                    break
            continue
        if buf_words + len(words) > target_words:
            flush()
        buf.append(para)
        buf_words += len(words)
    flush()
    return chunks


async def embed_texts(llm: Any, texts: list[str]) -> list[list[float]]:
    """Best-effort batch embedding; returns ``[]`` rows when unsupported."""
    if llm is None or not texts:
        return [[] for _ in texts]
    try:
        vecs = await llm.embed(texts)
    except NotImplementedError:
        return [[] for _ in texts]
    except Exception:  # noqa: BLE001 — never let embedding break ingest
        return [[] for _ in texts]
    out: list[list[float]] = []
    for i in range(len(texts)):
        out.append(list(vecs[i]) if i < len(vecs) and vecs[i] else [])
    return out


async def ingest_source(
    store: ResearchStore,
    llm: Any,
    *,
    meta: dict[str, Any],
    full_text: str = "",
    origin: str = "manual",
    date_pin: str = "",
    target_words: int = 180,
) -> dict[str, Any]:
    """Add a source and index its text into the corpus (deduped).

    ``full_text`` is chunked + embedded; when absent we fall back to the
    abstract/summary so even metadata-only sources are retrievable.
    """
    existing = await store.find_source(meta)
    source = await store.add_source(meta, origin=origin, date_pin=date_pin)
    body = full_text.strip() or str(meta.get("summary") or meta.get("abstract") or "")
    added = 0
    if existing is None and body:
        chunks = chunk_text(body, target_words=target_words)
        if chunks:
            vecs = await embed_texts(llm, [c["text"] for c in chunks])
            for c, v in zip(chunks, vecs, strict=False):
                c["embedding"] = v
            added = await store.add_chunks(source.id, chunks)
    return {"source_id": source.id, "chunks_added": added,
            "deduped": existing is not None, "title": source.title}


async def ingest_pdf(
    store: ResearchStore,
    llm: Any,
    *,
    meta: dict[str, Any],
    pdf_bytes: bytes = b"",
    pdf_path: str = "",
    origin: str = "manual",
    date_pin: str = "",
    target_words: int = 180,
) -> dict[str, Any]:
    """Ingest a source from PDF full text, page-aware, with graceful fallback.

    Extracts text per page (via optional ``pypdf``) and chunks each page so every
    passage carries a ``page`` locator for precise citations. When ``pypdf`` is
    unavailable or the PDF yields no text, falls back to :func:`ingest_source`
    (abstract/summary) so the source is still recorded and retrievable.
    """
    from omni.research.pdf import extract_pdf_pages, read_pdf_pages

    pages = extract_pdf_pages(pdf_bytes) if pdf_bytes else read_pdf_pages(pdf_path)
    body_pages = [(i + 1, txt) for i, txt in enumerate(pages) if txt.strip()]
    if not body_pages:
        # No parser / no extractable text → abstract-only ingest (today's path).
        result = await ingest_source(
            store, llm, meta=meta, full_text="", origin=origin,
            date_pin=date_pin, target_words=target_words,
        )
        result["full_text"] = False
        return result

    existing = await store.find_source(meta)
    source = await store.add_source(meta, origin=origin, date_pin=date_pin)
    added = 0
    if existing is None:
        chunks: list[dict[str, Any]] = []
        for page_no, text in body_pages:
            for c in chunk_text(text, target_words=target_words):
                c["ord"] = len(chunks)
                c["page"] = page_no
                chunks.append(c)
        if chunks:
            vecs = await embed_texts(llm, [c["text"] for c in chunks])
            for c, v in zip(chunks, vecs, strict=False):
                c["embedding"] = v
            added = await store.add_chunks(source.id, chunks)
    return {"source_id": source.id, "chunks_added": added,
            "deduped": existing is not None, "title": source.title,
            "pages": len(body_pages), "full_text": True}


async def ingest_many(
    store: ResearchStore,
    llm: Any,
    results: list[dict[str, Any]],
    *,
    origin: str = "manual",
    date_pin: str = "",
    target_words: int = 180,
) -> list[dict[str, Any]]:
    """Ingest a batch of paper dicts (connectors → corpus). Returns per-source results."""
    out: list[dict[str, Any]] = []
    for r in results:
        out.append(await ingest_source(
            store, llm, meta=r, full_text=str(r.get("summary") or ""),
            origin=str(r.get("origin") or origin), date_pin=date_pin,
            target_words=target_words,
        ))
    return out


async def link_references(
    store: ResearchStore,
    llm: Any,
    citing_source_id: str,
    references: list[dict[str, Any]],
    *,
    origin: str = "manual",
    date_pin: str = "",
    target_words: int = 180,
) -> dict[str, Any]:
    """Ingest ``references`` as sources and record citation edges from the citer.

    Ingesting first means each edge resolves straight to its ``cited_source_id``,
    so the resulting graph is immediately traversable in both directions.
    """
    if not citing_source_id or not references:
        return {"indexed": 0, "edges": 0}
    ingested = await ingest_many(
        store, llm, references, origin=origin, date_pin=date_pin, target_words=target_words
    )
    edges = await store.add_citations(citing_source_id, references, origin=origin)
    return {
        "indexed": sum(1 for r in ingested if not r["deduped"]),
        "edges": edges,
    }


def _keyword_overlap(query: str, text: str) -> float:
    q = {w for w in _TOKEN_RE.findall(query.lower()) if len(w) > 1}
    if not q:
        return 0.0
    t = {w for w in _TOKEN_RE.findall(text.lower()) if len(w) > 1}
    if not t:
        return 0.0
    return len(q & t) / len(q)


def _as_of_ok(retrieved_at: datetime | None, as_of: str) -> bool:
    if not as_of:
        return True
    if retrieved_at is None:
        return True
    try:
        return retrieved_at.date().isoformat() <= as_of[:10]
    except (AttributeError, ValueError):
        return True


def _passage(ch: Any, src: Any, score: float) -> Passage:
    return Passage(
        chunk_id=ch.id, source_id=ch.source_id, score=float(score),
        text=ch.text, ord=ch.ord, page=getattr(ch, "page", None),
        title=getattr(src, "title", "") if src else "",
        arxiv_id=getattr(src, "arxiv_id", "") if src else "",
        doi=getattr(src, "doi", "") if src else "",
        url=getattr(src, "url", "") if src else "",
        year=getattr(src, "year", "") if src else "",
    )


async def search_corpus(
    store: ResearchStore,
    llm: Any,
    query: str,
    *,
    k: int = 6,
    as_of: str = "",
    hybrid: bool = True,
    rrf_k: int = DEFAULT_RRF_K,
    vector_backend: str = "auto",
) -> list[Passage]:
    """Rank corpus passages for ``query`` (hybrid semantic + lexical).

    When embeddings are available *and* ``hybrid`` is on, the semantic (cosine)
    and lexical (keyword-overlap) rankings are fused with Reciprocal Rank Fusion
    so a passage strong on either signal surfaces — markedly more robust than
    picking one signal per chunk. The semantic scan uses sqlite-vec when the
    optional extension is installed (``vector_backend``), otherwise a Python
    cosine scan. Without embeddings (offline / no ``/embeddings`` endpoint) it
    degrades to the exact keyword-overlap ranking as before.

    ``as_of`` (a ``YYYY-MM-DD`` date pin) restricts results to sources retrieved
    on or before that date for reproducible, date-restricted retrieval (M3).
    """
    query = (query or "").strip()
    if not query:
        return []
    chunks = await store.all_chunks()
    if not chunks:
        return []
    sources = {s.id: s for s in await store.list_sources(limit=10_000)}

    # Date-pin filter first: everything downstream ranks the same candidate set.
    candidates = [
        ch for ch in chunks
        if _as_of_ok(getattr(sources.get(ch.source_id), "retrieved_at", None), as_of)
    ]
    if not candidates:
        return []

    # ``none`` disables semantic retrieval entirely → keyword-only.
    semantic_on = (vector_backend or "auto").strip().lower() not in ("none", "off")
    qvec: list[float] = []
    if semantic_on:
        qvecs = await embed_texts(llm, [query])
        qvec = qvecs[0] if qvecs else []

    if hybrid and qvec:
        return _hybrid_search(
            candidates, sources, query, qvec, k=k, rrf_k=rrf_k, vector_backend=vector_backend
        )

    # Single-signal path (byte-for-byte the historical behaviour): cosine for
    # embedded chunks when we have a query vector, keyword overlap otherwise.
    sims = (
        similarity_scores(
            qvec, [(ch.id, ch.embedding) for ch in candidates if ch.embedding],
            backend=vector_backend,
        )
        if qvec else {}
    )
    scored: list[Passage] = []
    for ch in candidates:
        if qvec and ch.embedding:
            score = sims.get(ch.id, 0.0)
        else:
            score = _keyword_overlap(query, ch.text)
        if score <= 0.0:
            continue
        scored.append(_passage(ch, sources.get(ch.source_id), score))
    scored.sort(key=lambda p: p.score, reverse=True)
    return scored[: max(1, k)]


def _hybrid_search(
    candidates: list[Any],
    sources: dict[str, Any],
    query: str,
    qvec: list[float],
    *,
    k: int,
    rrf_k: int,
    vector_backend: str = "auto",
) -> list[Passage]:
    """Fuse semantic + lexical rankings over ``candidates`` via RRF."""
    sem = similarity_scores(
        qvec, [(ch.id, ch.embedding) for ch in candidates if ch.embedding],
        backend=vector_backend,
    )
    lex = [(ch.id, _keyword_overlap(query, ch.text)) for ch in candidates]
    sem_ranked = [cid for cid, s in sorted(sem.items(), key=lambda t: t[1], reverse=True) if s > 0.0]
    lex_ranked = [cid for cid, s in sorted(lex, key=lambda t: t[1], reverse=True) if s > 0.0]

    fused = reciprocal_rank_fusion([sem_ranked, lex_ranked], k=rrf_k)
    if not fused:
        return []
    by_id = {ch.id: ch for ch in candidates}
    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    out: list[Passage] = []
    for chunk_id, score in ordered[: max(1, k)]:
        ch = by_id.get(chunk_id)
        if ch is None:
            continue
        out.append(_passage(ch, sources.get(ch.source_id), score))
    return out


__all__ = [
    "Passage", "chunk_text", "embed_texts", "ingest_source", "ingest_pdf",
    "ingest_many", "link_references", "search_corpus",
]
