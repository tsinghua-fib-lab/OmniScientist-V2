from __future__ import annotations

from types import SimpleNamespace

from omni.research.lit_diagnostics import format_lit_diagnostics, lit_diagnostics_from_sources


def test_projects_search_literature_and_formats_lines() -> None:
    record = SimpleNamespace(
        name="search_literature",
        result={
            "query": "retrieval augmented generation",
            "connectors": ["arxiv", "openalex"],
            "results": [{"title": "Attention Is All You Need", "source_id": "src1"}],
            "providers": [{"name": "s2", "state": "quota"}],
            "status": "ok",
        },
    )
    items = lit_diagnostics_from_sources(record)
    assert items
    assert items[0]["query"] == "retrieval augmented generation"
    assert items[0]["kept_titles"] == ["Attention Is All You Need"]
    lines = format_lit_diagnostics(items)
    assert any("retrieval augmented generation" in line for line in lines)
    assert any("kept: Attention Is All You Need" in line for line in lines)
    assert any("s2:quota" in line for line in lines)
