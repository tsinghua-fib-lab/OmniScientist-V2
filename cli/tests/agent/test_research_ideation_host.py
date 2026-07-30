"""Host-boundary tests for the portable research-ideation pipeline."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[3] / "skills" / "research-ideation"


def _load_module(filename: str, module_name: str) -> Any:
    path = SKILL_ROOT / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_portable_core_accepts_an_injected_llm_port() -> None:
    core = _load_module("core.py", "research_ideation_host_core")
    requests: list[dict[str, Any]] = []

    class StubLLM:
        def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
            requests.append(payload)
            return {"choices": [{"message": {"content": "hosted response"}}]}

    result = core._llm_chat("Generate one idea", llm=StubLLM())

    assert result == "hosted response"
    assert requests[0]["messages"][-1]["content"] == "Generate one idea"


def test_portable_pipeline_prompt_contracts_match_their_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_module("core.py", "research_ideation_prompt_contract_core")

    monkeypatch.setattr(
        core,
        "search_papers",
        lambda *_args, **_kwargs: [
            {"title": "Relevant RAG", "abstract": "Retrieval for QA", "year": 2024},
            {"title": "Unrelated", "abstract": "Other work", "year": 2023},
        ],
    )

    class PromptAwareLLM:
        def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
            prompt = payload["messages"][-1]["content"]
            if prompt.startswith("Generate 1-3"):
                content = '{"queries":["retrieval augmented generation"]}'
            elif prompt.startswith("Select papers"):
                assert "[0] Relevant RAG" in prompt
                content = '{"relevant_indices":[0]}'
            elif prompt.startswith("Extract the most important"):
                content = '{"core_concepts":["RAG"],"domain_concepts":["QA"]}'
            elif prompt.startswith("Normalize and merge"):
                assert "rag" in prompt and "qa" in prompt
                content = (
                    '{"merged_core":["retrieval-augmented generation"],'
                    '"merged_domains":["question answering"],'
                    '"mapping":{"rag":"retrieval-augmented generation",'
                    '"qa":"question answering"}}'
                )
            else:  # pragma: no cover - makes unexpected prompt drift explicit
                raise AssertionError(f"unexpected prompt: {prompt[:80]}")
            return {"choices": [{"message": {"content": content}}]}

    result = core.search_and_extract(
        "How can RAG improve question answering?",
        llm=PromptAwareLLM(),
    )

    assert [paper["title"] for paper in result["papers"]] == ["Relevant RAG"]
    assert result["core_concepts"] == ["retrieval-augmented generation"]
    assert result["domain_concepts"] == ["question answering"]


def test_gap_prompt_uses_the_declared_papers_text_placeholder() -> None:
    core = _load_module("core.py", "research_ideation_gap_prompt_core")

    class GapLLM:
        def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
            prompt = payload["messages"][-1]["content"]
            assert "Relevant-paper summaries:" in prompt
            assert "Relevant RAG (2024)" in prompt
            content = '{"gaps":[{"gap_id":1,"gap":"Evaluate retrieval drift"}]}'
            return {"choices": [{"message": {"content": content}}]}

    gaps = core.identify_gaps(
        "How can RAG improve question answering?",
        ["retrieval-augmented generation"],
        ["question answering"],
        [{"title": "Relevant RAG", "abstract": "Retrieval for QA", "year": 2024}],
        llm=GapLLM(),
    )

    assert gaps[0]["gap"] == "Evaluate retrieval drift"


def test_valid_empty_literature_result_stops_before_grounded_ideation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_module("core.py", "research_ideation_empty_literature_core")
    monkeypatch.setattr(core, "search_papers", lambda *_args, **_kwargs: [])

    class SearchQueryOnlyLLM:
        def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
            prompt = payload["messages"][-1]["content"]
            if prompt.startswith("Generate 1-3"):
                return {
                    "choices": [
                        {"message": {"content": '{"queries":["unexplored topic"]}'}}
                    ]
                }
            raise AssertionError("zero literature must not be presented as grounded evidence")

    result = core.run_pipeline(
        "An unexplored topic",
        n_ideas=1,
        use_tools=False,
        llm=SearchQueryOnlyLLM(),
    )

    assert result["status"] == "partial"
    assert result["outcome"]["code"] == "no_literature_found"
    assert result["steps"]["search"]["paper_count"] == 0
    assert result["sources"] == []
    assert result["research"] == {"source_ids": [], "run_id": ""}
    assert result["run_id"] == ""


def test_omni_adapter_marks_literature_failure_as_recoverable() -> None:
    engine_module = _load_module(
        "engine.py", "research_ideation_literature_failure_engine"
    )

    result = engine_module._pipeline_error(
        engine_module._core.LiteratureSearchError("Semantic Scholar unavailable")
    )

    assert result["outcome"]["code"] == "literature_search_failed"
    assert result["recoverable"] is True
    assert result["blocking"] is False
    assert result["error_info"]["retryable"] is True
    assert result["research"] == {"source_ids": [], "run_id": ""}


@pytest.mark.asyncio
async def test_omni_engine_uses_ctx_llm_without_reading_model_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_module = _load_module("engine.py", "research_ideation_host_engine")
    calls: list[dict[str, Any]] = []

    class ForbiddenSettings:
        @property
        def model(self) -> object:
            raise AssertionError("the skill adapter must not read model configuration")

    class HostLLM:
        async def chat_with_tools(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            **kwargs: Any,
        ) -> SimpleNamespace:
            calls.append({"messages": messages, "tools": tools, **kwargs})
            tool_call = SimpleNamespace(
                id="call_1", name="search", arguments={"query": "RAG"}
            )
            return SimpleNamespace(content="", tool_calls=[tool_call])

    def fake_pipeline(*, llm: Any, **_kwargs: Any) -> dict[str, Any]:
        response = llm.complete(
            {
                "messages": [{"role": "user", "content": "find a direction"}],
                "tools": [{"type": "function", "function": {"name": "search"}}],
                "temperature": 0.1,
            }
        )
        return {"status": "ok", "steps": {}, "host_response": response}

    monkeypatch.setattr(engine_module._core, "run_pipeline", fake_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(
        settings=ForbiddenSettings(),
        llm=HostLLM(),
        artifacts=None,
        paths=None,
    )

    result = await engine.execute(input="RAG factuality", use_tools=False)

    assert result["status"] == "ok"
    assert calls[0]["messages"][-1]["content"] == "find a direction"
    assert calls[0]["tools"][0]["function"]["name"] == "search"
    message = result["host_response"]["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["arguments"] == '{"query": "RAG"}'


@pytest.mark.asyncio
async def test_omni_engine_uses_scoped_s2_key_and_exposes_full_report(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    engine_module = _load_module("engine.py", "research_ideation_scoped_s2_engine")
    pipeline_calls: list[dict[str, Any]] = []
    settings.research.connectors = ["semanticscholar"]
    settings.research.semantic_scholar_api_key = "scoped-s2-secret"

    def fake_pipeline(**kwargs: Any) -> dict[str, Any]:
        pipeline_calls.append(kwargs)
        return {
            "status": "ok",
            "outcome": {"code": "ideas_generated", "count": 1},
            "research_question": "How should ideas be grounded?",
            "steps": {
                "search": {
                    "queries": ["grounded ideation"],
                    "paper_count": 1,
                    "papers": [{"title": "Grounded Ideation", "year": 2025}],
                    "core_concepts": ["grounding"],
                    "domain_concepts": ["research ideation"],
                },
                "gaps": [],
                "raw_ideas": [
                    {
                        "title": "Evidence Loop",
                        "brainstorming": (
                            "<concept>Grounding</concept> links evidence to novelty."
                        ),
                    }
                ],
            },
            "final_idea": {
                "title": "Evidence Loop",
                "background": "Research ideas need evidence.",
                "related_work": "Prior work retrieves papers.",
                "gap_analysis": "Retrieved evidence is not tied to ideation.",
                "proposed_method": "Bind each concept transition to a source.",
            },
            "summary": "Generated one grounded idea",
            "sources": [],
            "research": {"source_ids": [], "run_id": ""},
            "run_id": "",
        }

    monkeypatch.setattr(engine_module._core, "run_pipeline", fake_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(
        settings=settings,
        llm=object(),
        artifacts=None,
        paths=None,
    )

    result = await engine.execute(input="How should ideas be grounded?", use_tools=True)

    assert pipeline_calls[0]["s2_api_key"] == "scoped-s2-secret"
    assert result["text"] == engine_module._build_markdown_report(result)
    assert "### Retrieved paper titles" in result["text"]
    assert "1. Grounded Ideation" in result["text"]
    assert "## Concept-Level Reasoning" in result["text"]
    assert "<concept>Grounding</concept>" in result["text"]


@pytest.mark.asyncio
async def test_omni_engine_respects_disabled_semantic_scholar_connector(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    engine_module = _load_module("engine.py", "research_ideation_disabled_s2_engine")
    settings.research.connectors = ["arxiv"]
    settings.research.semantic_scholar_api_key = "must-not-be-used"

    def forbidden_pipeline(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a disabled connector must stop before network-capable code")

    monkeypatch.setattr(engine_module._core, "run_pipeline", forbidden_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(
        settings=settings,
        llm=object(),
        artifacts=None,
        paths=None,
    )

    result = await engine.execute(input="RAG factuality", use_tools=True)

    assert result["status"] == "error"
    assert result["outcome"]["code"] == "connector_disabled"
    assert "Semantic Scholar" in result["error"]
    assert result["recoverable"] is True
    assert result["blocking"] is False
    assert result["sources"] == []
    assert result["research"] == {"source_ids": [], "run_id": ""}


@pytest.mark.asyncio
async def test_omni_engine_does_not_inherit_process_s2_key(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    engine_module = _load_module("engine.py", "research_ideation_empty_scoped_s2_engine")
    pipeline_calls: list[dict[str, Any]] = []
    settings.research.connectors = ["semanticscholar"]
    settings.research.semantic_scholar_api_key = ""
    monkeypatch.setenv("S2_API_KEY", "ambient-process-secret")

    def fake_pipeline(**kwargs: Any) -> dict[str, Any]:
        pipeline_calls.append(kwargs)
        return {
            "status": "partial",
            "research_question": "RAG factuality",
            "steps": {},
            "summary": "No literature found",
        }

    monkeypatch.setattr(engine_module._core, "run_pipeline", fake_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(
        settings=settings,
        llm=object(),
        artifacts=None,
        paths=None,
    )

    result = await engine.execute(input="RAG factuality", use_tools=False)

    assert result["status"] == "partial"
    assert pipeline_calls[0]["s2_api_key"] == ""


def test_markdown_report_normalizes_non_string_model_fields() -> None:
    engine_module = _load_module(
        "engine.py", "research_ideation_report_normalization_engine"
    )

    report = engine_module._build_markdown_report(
        {
            "research_question": "RAG factuality",
            "steps": {
                "search": {
                    "queries": [123],
                    "paper_count": 0,
                    "papers": 42,
                    "core_concepts": ["retrieval", 456],
                    "domain_concepts": [789],
                },
                "gaps": [
                    "malformed",
                    {"gap_id": 1, "gap": 123, "source": ["paper-a"]},
                ],
                "raw_ideas": {"malformed": True},
            },
            "final_idea": ["malformed"],
        }
    )

    assert "Search queries: 123" in report
    assert "Core concepts: retrieval, 456" in report
    assert "Application domains: 789" in report
    assert "### Gap 1\n123\n- Rationale: ['paper-a']" in report


@pytest.mark.asyncio
async def test_omni_engine_records_real_source_and_run_provenance(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    from omni.research import ResearchStore
    from omni.storage.db import get_database

    engine_module = _load_module("engine.py", "research_ideation_provenance_engine")
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    paper = {
        "paperId": "s2-paper-123",
        "url": "https://www.semanticscholar.org/paper/s2-paper-123",
        "externalIds": {"DOI": "10.1000/example", "ArXiv": "2401.01234"},
        "doi": "10.1000/example",
        "arxiv_id": "2401.01234",
        "title": "Grounded ideation",
        "abstract": "An evidence-grounded ideation method.",
        "summary": "An evidence-grounded ideation method.",
        "year": 2024,
        "authors": ["Ada Researcher"],
    }

    def fake_pipeline(**_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "outcome": {"code": "ideas_generated", "count": 1},
            "research_question": "How should ideas be grounded?",
            "steps": {
                "search": {
                    "queries": ["grounded ideation"],
                    "paper_count": 1,
                    "papers": [paper],
                    "core_concepts": ["grounding"],
                    "domain_concepts": ["research ideation"],
                },
                "gaps": [{"gap_id": 1, "gap": "Evaluate grounding"}],
                "raw_ideas": [{"title": "Grounded Idea"}],
            },
            "sources": [paper],
            "research": {"source_ids": [], "run_id": ""},
            "run_id": "",
            "final_idea": {"title": "Grounded Idea"},
            "summary": "Generated one grounded idea",
        }

    monkeypatch.setattr(engine_module._core, "run_pipeline", fake_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(
        db=db,
        llm=object(),
        artifacts=None,
        paths=None,
        session_id="sess-ideation",
        subtask_id="subtask-ideation",
    )

    result = await engine.execute(input="How should ideas be grounded?", use_tools=False)

    assert result["run_id"]
    assert result["research"] == {
        "source_ids": [result["sources"][0]["source_id"]],
        "run_id": result["run_id"],
    }
    store = ResearchStore(db)
    sources = await store.list_sources()
    assert [(source.doi, source.arxiv_id) for source in sources] == [
        ("10.1000/example", "2401.01234")
    ]
    runs = await store.list_runs(session_id="sess-ideation")
    assert [run.id for run in runs] == [result["run_id"]]
    assert runs[0].inputs["research_question"] == "How should ideas be grounded?"
