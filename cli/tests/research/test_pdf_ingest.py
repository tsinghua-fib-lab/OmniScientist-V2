"""PDF full-text ingestion — optional pypdf + graceful fallback (P0-B′d)."""

from __future__ import annotations

import io

import pytest

from omni.config import load_settings
from omni.research import pdf as pdfmod
from omni.research.corpus import ingest_pdf, search_corpus
from omni.research.store import ResearchStore
from omni.storage.db import get_database


# ── a tiny in-memory fake of the pypdf surface we use ────────────────────────
class _FakePage:
    def __init__(self, text: str) -> None:
        self._t = text

    def extract_text(self) -> str:
        return self._t


class _FakeReader:
    def __init__(self, stream: io.BytesIO) -> None:
        raw = stream.read().decode("utf-8", "ignore")
        # form-feed separates "pages" in our fake PDF bytes
        self.pages = [_FakePage(p) for p in raw.split("\f")]


class _FakePypdf:
    PdfReader = _FakeReader


def _install_fake_pypdf(monkeypatch) -> None:
    monkeypatch.setattr(pdfmod, "_TRIED", True)
    monkeypatch.setattr(pdfmod, "_PYPDF", _FakePypdf())


def _force_no_pypdf(monkeypatch) -> None:
    monkeypatch.setattr(pdfmod, "_TRIED", True)
    monkeypatch.setattr(pdfmod, "_PYPDF", None)


async def _store() -> ResearchStore:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ResearchStore(db)


def test_extract_pages_with_fake_parser(monkeypatch):
    _install_fake_pypdf(monkeypatch)
    assert pdfmod.pdf_available() is True
    data = b"page one text\fpage two text"
    pages = pdfmod.extract_pdf_pages(data)
    assert pages == ["page one text", "page two text"]
    assert pdfmod.extract_pdf_text(data) == "page one text\n\npage two text"


def test_extract_returns_empty_when_unavailable(monkeypatch):
    _force_no_pypdf(monkeypatch)
    assert pdfmod.pdf_available() is False
    assert pdfmod.extract_pdf_pages(b"whatever") == []


def test_extract_never_raises_on_empty():
    assert pdfmod.extract_pdf_pages(b"") == []


@pytest.mark.asyncio
async def test_ingest_pdf_page_aware(monkeypatch):
    _install_fake_pypdf(monkeypatch)
    store = await _store()
    body = (
        b"The Transformer relies entirely on self-attention mechanisms.\f"
        b"Chloroplasts capture sunlight during photosynthesis in plants."
    )
    res = await ingest_pdf(
        store, None,
        meta={"arxiv_id": "1706.03762", "title": "Attention Is All You Need"},
        pdf_bytes=body,
    )
    assert res["full_text"] is True
    assert res["pages"] == 2
    assert res["chunks_added"] >= 2

    hits = await search_corpus(store, None, "self-attention transformer", k=3)
    assert hits
    # the passage carries the page locator from its source page
    assert hits[0].page == 1
    assert hits[0].to_dict(1)["page"] == 1


@pytest.mark.asyncio
async def test_ingest_pdf_falls_back_to_abstract(monkeypatch):
    _force_no_pypdf(monkeypatch)
    store = await _store()
    res = await ingest_pdf(
        store, None,
        meta={"arxiv_id": "2310.06825", "title": "Mistral 7B",
              "summary": "A compact language model with grouped-query attention."},
        pdf_bytes=b"%PDF-fake-bytes-that-cannot-be-parsed",
    )
    assert res["full_text"] is False
    assert res["chunks_added"] >= 1  # abstract still ingested
    hits = await search_corpus(store, None, "grouped-query attention", k=3)
    assert hits and hits[0].arxiv_id == "2310.06825"
    assert hits[0].page is None  # abstract chunks have no page locator
