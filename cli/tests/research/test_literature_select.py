"""UX-02: keyword-collision filter + owner-visible literature list."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from omni.config import load_settings
from omni.core.llm.providers import MockProvider
from omni.memory.library import add_papers, load_library
from omni.research import connectors
from omni.research.engine_util import index_results
from omni.research.literature_select import (
    fetch_window,
    format_literature_hits,
    paper_relevance,
    select_relevant_papers,
)
from omni.runtime.presentation import task_presentation_from_result
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.executor import execute_skill
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database

QUERY = "RAG hallucination in clinical question answering"

_RAG = {
    "title": "Retrieval-Augmented Generation for Clinical Question Answering",
    "year": "2024",
    "venue": "NeurIPS",
    "summary": "RAG reduces hallucination in clinical QA over electronic health records.",
    "origin": "openalex",
    "doi": "10.1/rag-clinical",
}
_GUIDELINES = {
    "title": "Clinical Practice Guidelines for the Prevention and Management of Pain, Agitation/Sedation, Delirium, Immobility, and Sleep Disruption in Adult Patients in the ICU",
    "year": "2018",
    "venue": "Critical Care Medicine",
    "summary": "ICU pain agitation and delirium practice guidelines for critical care.",
    "origin": "openalex",
    "doi": "10.1/icu-guidelines",
}
_VQA = {
    "title": "A Survey of Visual Question Answering",
    "year": "2021",
    "venue": "IEEE TPAMI",
    "summary": "Vision-language VQA models for image question answering.",
    "origin": "openalex",
    "doi": "10.1/vqa-survey",
}


def test_fetch_window_pulls_a_wider_keyword_set():
    assert fetch_window(8) == 16
    assert fetch_window(20) == 25
    assert fetch_window(1) >= 2


def test_select_drops_single_token_clinical_collision():
    kept, dropped = select_relevant_papers(QUERY, [_RAG, _GUIDELINES, _VQA], keep=8)
    titles = [paper["title"] for paper in kept]
    assert _RAG["title"] in titles
    assert _GUIDELINES["title"] not in titles
    assert any(paper["title"] == _GUIDELINES["title"] for paper in dropped)
    # One generic token ("clinical") must score below a real RAG/clinical hit.
    assert paper_relevance(QUERY, _RAG) > paper_relevance(QUERY, _GUIDELINES)


def test_select_never_empties_a_successful_search():
    kept, dropped = select_relevant_papers(QUERY, [_GUIDELINES], keep=8)
    assert kept == [_GUIDELINES]
    assert dropped == []


def test_select_passes_through_untokenisable_queries():
    papers = [_GUIDELINES, _VQA]
    kept, dropped = select_relevant_papers("x", papers, keep=8)
    assert kept == papers
    assert dropped == []


def test_select_matches_cjk_bigrams():
    query = "临床问答幻觉"
    on_topic = {
        "title": "临床问答中的检索增强与幻觉",
        "summary": "面向电子病历的 RAG。",
        "year": "2024",
    }
    noise = {
        "title": "临床疼痛镇静指南",
        "summary": "重症监护病房实践。",
        "year": "2018",
    }
    kept, dropped = select_relevant_papers(query, [noise, on_topic], keep=2)
    assert kept[0]["title"] == on_topic["title"]
    assert any(paper["title"] == noise["title"] for paper in dropped)


def test_portable_runner_select_drops_title_only_collision():
    path = Path(__file__).resolve().parents[3] / "skills" / "openalex-search" / "scripts" / "run.py"
    spec = importlib.util.spec_from_file_location("portable_openalex_search", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    kept = module._select(QUERY, [_GUIDELINES, _RAG], keep=8)
    titles = [paper["title"] for paper in kept]
    assert _RAG["title"] in titles
    assert _GUIDELINES["title"] not in titles


def test_format_literature_hits_is_a_numbered_year_title_list():
    text = format_literature_hits([_RAG, _GUIDELINES])
    assert "1. 2024 · Retrieval-Augmented Generation for Clinical Question Answering (NeurIPS)" in text
    assert "2. 2018 ·" in text
    assert "Critical Care Medicine" in text


def test_library_keeps_openalex_year_and_origin(tmp_path):
    lib = tmp_path / "library.jsonl"
    assert add_papers(lib, [_RAG]) == 1
    entry = load_library(lib)[0]
    assert entry["year"] == "2024"
    assert entry["source"] == "openalex"


def _run(coro):
    async def _wrap():
        from omni.storage.db import reset_databases

        try:
            return await coro
        finally:
            await reset_databases()

    return asyncio.run(_wrap())


async def _ctx(llm=None) -> ExecContext:
    settings = load_settings()
    settings.research.contact_email = "test@example.com"
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    return ExecContext(
        settings=settings,
        paths=settings.paths,
        project=settings.paths.project_name,
        session_id="s1",
        channel="cli",
        db=db,
        llm=llm,
    )


def test_index_results_summary_lists_titles_for_presentation():
    async def _go():
        ctx = await _ctx(llm=MockProvider())
        out = await index_results(ctx, [_RAG], origin="openalex", query=QUERY)
        markdown = task_presentation_from_result(
            subtask_id="t1",
            skill="openalex-search",
            status="succeeded",
            result=out,
        ).to_markdown()
        return out, markdown

    out, markdown = _run(_go())
    assert "Retrieval-Augmented Generation for Clinical Question Answering" in out["summary"]
    assert "2024 ·" in out["summary"]
    assert "Retrieval-Augmented Generation for Clinical Question Answering" in markdown
    assert "Use omni lit" in markdown


def test_openalex_engine_filters_noise_lists_titles_and_skips_library(monkeypatch):
    seen: dict[str, int] = {}

    async def _fake_search(query, *, rows=8, email=""):  # noqa: ARG001
        seen["rows"] = int(rows)
        return [_GUIDELINES, _VQA, _RAG]

    monkeypatch.setattr(connectors, "openalex_search", _fake_search)

    async def _go():
        registry = SkillRegistry(load_settings())
        registry.build_index()
        ctx = await _ctx(llm=MockProvider())
        result = await execute_skill(
            registry.get("openalex-search"),
            {"query": QUERY, "max_results": 8},
            ctx,
        )
        library = load_library(ctx.paths.library)
        markdown = task_presentation_from_result(
            subtask_id="t1",
            skill="openalex-search",
            status="succeeded",
            result=result,
        ).to_markdown()
        return result, library, markdown

    result, library, markdown = _run(_go())
    titles = [paper["title"] for paper in result["results"]]
    assert seen["rows"] == 16
    assert _RAG["title"] in titles
    assert _GUIDELINES["title"] not in titles
    assert result["dropped"] >= 1
    assert "Dropped" in result["summary"]
    assert _RAG["title"] in result["summary"]
    assert _RAG["title"] in markdown
    assert _GUIDELINES["title"] not in markdown
    lib_titles = {entry["title"] for entry in library}
    assert _RAG["title"] in lib_titles
    assert _GUIDELINES["title"] not in lib_titles
    assert all(entry["source"] == "openalex" for entry in library)
    assert all(entry["year"] for entry in library)
