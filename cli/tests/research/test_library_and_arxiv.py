"""Offline tests for the arXiv client robustness + reference library."""

from __future__ import annotations

import asyncio

import pytest

from omni.memory.library import add_papers, load_library, to_bibtex, to_csv
from omni.research import arxiv as _arxiv_common
from omni.research.arxiv import (
    ArxivError,
    fetch_by_id,
    normalize_arxiv_id,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2310.06825", "2310.06825"),
        ("2310.06825v3", "2310.06825"),
        ("arXiv:2310.06825", "2310.06825"),
        ("https://arxiv.org/abs/2310.06825v2", "2310.06825"),
        ("http://arxiv.org/pdf/2310.06825.pdf", "2310.06825"),
        ("看看 arXiv 2310.06825 这篇论文", "2310.06825"),
        ("", ""),
    ],
)
def test_normalize_arxiv_id(raw, expected):
    assert normalize_arxiv_id(raw) == expected


def test_fetch_by_id_empty_is_graceful_error():
    res = asyncio.run(fetch_by_id("not-an-arxiv-thing!!"))
    assert res["status"] == "error"
    assert "arXiv" in res["error"]


def test_fetch_by_id_network_failure_returns_error_dict(monkeypatch):
    """A network failure must surface as a clean tool result, never raise."""

    async def _boom(*_a, **_k):
        raise ArxivError("无法连接 arXiv（export.arxiv.org）。")

    monkeypatch.setattr(_arxiv_common, "_query", _boom)
    res = asyncio.run(fetch_by_id("2310.06825"))
    assert res["status"] == "error"
    assert res["arxiv_id"] == "2310.06825"
    assert "arXiv" in res["error"]


def test_library_add_dedup_and_export(tmp_path):
    lib = tmp_path / "library.jsonl"
    paper = {
        "arxiv_id": "2310.06825",
        "title": "Mistral 7B",
        "authors": ["Albert Q. Jiang", "Guillaume Lample"],
        "published": "2023-10-10T00:00:00Z",
        "abs_url": "http://arxiv.org/abs/2310.06825v1",
    }
    assert add_papers(lib, [paper]) == 1
    # same id again → deduped
    assert add_papers(lib, [paper]) == 0
    entries = load_library(lib)
    assert len(entries) == 1
    assert entries[0]["year"] == "2023"

    bib = to_bibtex(entries)
    assert "@article{" in bib
    assert "Mistral 7B" in bib
    assert "eprint = {2310.06825}" in bib

    csv_text = to_csv(entries)
    assert "arxiv_id" in csv_text.splitlines()[0]
    assert "2310.06825" in csv_text


def test_library_keeps_explicit_year_and_openalex_origin(tmp_path):
    lib = tmp_path / "library.jsonl"
    assert add_papers(lib, [{
        "title": "A Clinical RAG Study",
        "year": 2024,
        "origin": "openalex",
        "doi": "10.1/rag",
    }]) == 1
    entry = load_library(lib)[0]
    assert entry["year"] == "2024"
    assert entry["source"] == "openalex"
