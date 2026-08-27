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


@pytest.mark.asyncio
async def test_the_host_port_reaches_the_funnel_and_speaks_its_field_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``retrieval.py`` has always claimed this skill goes through the funnel.

    It did not — it ran a private Semantic Scholar pipeline — so the fan-out,
    health checks, and corpus floor built for exactly this case never applied.
    The funnel names an abstract ``summary``; the pipeline reasons over
    ``abstract``, and an idea generated from an empty abstract is an idea
    generated from the title alone.
    """
    import asyncio

    engine_module = _load_module("engine.py", "research_ideation_funnel_port_engine")
    asked: dict[str, Any] = {}

    async def fake_funnel(_ctx: Any, *, query: str, rows: int, **_kw: Any) -> dict[str, Any]:
        asked["query"], asked["rows"] = query, rows
        return {"results": [{"title": "Steering agents", "summary": "Latent intervention."}]}

    import omni.research as research

    monkeypatch.setattr(research, "search_literature", fake_funnel)
    ctx = SimpleNamespace(settings=SimpleNamespace(), llm=object(), paths=None)
    port = engine_module._host_search_port(ctx, asyncio.get_running_loop())

    papers = await asyncio.to_thread(port, "latent space steering", 7)

    assert asked == {"query": "latent space steering", "rows": 7}
    assert papers[0]["abstract"] == "Latent intervention."


def test_outside_omni_there_is_no_funnel_to_borrow() -> None:
    """A portable copy has no host, and must keep its own retrieval path."""
    engine_module = _load_module("engine.py", "research_ideation_portable_port_engine")

    assert engine_module._host_search_port(None, None) is None


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


def test_empty_literature_continues_with_llm_only_ideation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_module("core.py", "research_ideation_empty_literature_core")
    calls: dict[str, int] = {}

    def fake_search_and_extract(
        _question: str,
        paper_limit: int,
        _progress: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        calls["paper_limit"] = paper_limit
        return {
            "papers": [],
            "paper_count": 0,
            "queries": ["unexplored topic"],
            "paper_concepts": {},
            "core_concepts": [],
            "domain_concepts": [],
            "search_failures": [
                {
                    "query": "unexplored topic",
                    "error": "Semantic Scholar returned HTTP 429",
                }
            ],
        }

    def fake_identify_gaps(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"gap_id": 1, "gap": "Develop an evidence-free fallback"}]

    def fake_generate_ideas(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls["n_ideas"] = kwargs["n"]
        assert kwargs["reference_papers"] == []
        return [{"title": "Fallback Idea", "proposed_method": "Reason from priors"}]

    monkeypatch.setattr(core, "search_and_extract", fake_search_and_extract)
    monkeypatch.setattr(core, "identify_gaps", fake_identify_gaps)
    monkeypatch.setattr(core, "generate_ideas", fake_generate_ideas)
    monkeypatch.setattr(
        core,
        "critique_idea",
        lambda *_args, **_kwargs: {"overall_score": 9.5},
    )

    result = core.run_pipeline(
        "An unexplored topic",
        use_tools=False,
        llm=object(),
    )

    assert result["status"] == "partial"
    assert result["outcome"]["code"] == "ideas_generated_partial"
    assert result["steps"]["search"]["paper_count"] == 0
    assert result["final_idea"]["title"] == "Fallback Idea"
    assert "Continuing with LLM-only reasoning" in result["warning"]
    assert calls == {"paper_limit": core.DEFAULT_PAPER_LIMIT, "n_ideas": 2}
    assert result["sources"] == []
    assert result["research"] == {"source_ids": [], "run_id": ""}
    assert result["run_id"] == ""


def test_search_isolates_query_failures_and_caps_unique_papers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_module("core.py", "research_ideation_search_fallback_core")
    search_calls: list[tuple[str, int]] = []

    def fake_search(query: str, limit: int, **_kwargs: Any) -> list[dict[str, Any]]:
        search_calls.append((query, limit))
        if query == "broken query":
            raise core.LiteratureSearchError("Semantic Scholar returned HTTP 429")
        return [
            {"title": f"Paper {index}", "abstract": "", "year": 2025}
            for index in range(60)
        ]

    class SearchLLM:
        def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
            prompt = payload["messages"][-1]["content"]
            if prompt.startswith("Generate 1-3"):
                content = '{"queries":["broken query","working query"]}'
            elif prompt.startswith("Select papers"):
                content = '{"relevant_indices":[' + ",".join(map(str, range(60))) + "]}"
            else:  # pragma: no cover - makes unexpected prompt drift explicit
                raise AssertionError(f"unexpected prompt: {prompt[:80]}")
            return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(core, "search_papers", fake_search)

    result = core.search_and_extract("wide topic", llm=SearchLLM())

    assert search_calls == [
        ("broken query", core.DEFAULT_PAPER_LIMIT),
        ("working query", core.DEFAULT_PAPER_LIMIT),
    ]
    assert len(result["papers"]) == core.MAX_TOTAL_PAPERS
    assert result["search_failures"] == [
        {
            "query": "broken query",
            "error": "Semantic Scholar returned HTTP 429",
        }
    ]


def test_ideation_search_tool_degrades_to_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_module("core.py", "research_ideation_tool_fallback_core")

    def unavailable_search(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise core.LiteratureSearchError("Semantic Scholar unavailable")

    monkeypatch.setattr(core, "search_papers", unavailable_search)

    assert core._run_s2_search("novel mechanism", 5) == []


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
    monkeypatch.setattr(
        engine_module,
        "_resolve_s2_key",
        lambda _ctx: "scoped-s2-secret",
    )

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
            return SimpleNamespace(
                content="",
                tool_calls=[tool_call],
                usage={
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            )

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
    assert calls[0]["max_tokens"] == 16384
    message = result["host_response"]["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["arguments"] == '{"query": "RAG"}'
    assert result["host_response"]["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }


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
async def test_omni_engine_uses_new_defaults_and_full_artifact_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_module = _load_module("engine.py", "research_ideation_artifact_engine")
    pipeline_calls: list[dict[str, Any]] = []
    artifact_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        engine_module,
        "_resolve_s2_key",
        lambda _ctx: "scoped-s2-secret",
    )

    def fake_pipeline(**kwargs: Any) -> dict[str, Any]:
        pipeline_calls.append(kwargs)
        return {
            "status": "ok",
            "outcome": {"code": "ideas_generated", "count": 1},
            "research_question": "Grounded ideation",
            "steps": {},
            "summary": "Generated one idea",
        }

    class Artifacts:
        async def put_bytes(self, _data: bytes, **kwargs: Any) -> SimpleNamespace:
            artifact_calls.append(kwargs)
            return SimpleNamespace(uri="artifact://research-ideation/report.md")

    monkeypatch.setattr(engine_module._core, "run_pipeline", fake_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(
        llm=object(),
        artifacts=Artifacts(),
        settings=None,
        paths=None,
        session_id="session-1",
        task_id="task-1",
        subtask_id="subtask-1",
    )

    result = await engine.execute(input="Grounded ideation", use_tools=False)

    assert pipeline_calls[0]["n_ideas"] == 2
    assert artifact_calls[0]["task_id"] == "task-1"
    assert artifact_calls[0]["subtask_id"] == "subtask-1"
    assert result["report_uri"] == "artifact://research-ideation/report.md"


@pytest.mark.asyncio
async def test_omni_engine_runs_when_semantic_scholar_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """A kill-switched S2 connector is one missing source, not a refused run.

    The adapter used to return ``connector_disabled`` before the funnel could
    query arXiv / OpenAlex. That disagreed with the skill contract (no
    connector is required) and with the missing-key path that already continues.
    """
    engine_module = _load_module("engine.py", "research_ideation_disabled_s2_engine")
    settings.research.connectors = ["arxiv"]
    settings.research.semantic_scholar_api_key = "must-not-be-used"
    seen: dict[str, Any] = {}

    def observed_pipeline(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "ok", "summary": "ideated on arXiv", "final_idea": {"title": "T"}}

    monkeypatch.setattr(engine_module._core, "run_pipeline", observed_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(
        settings=settings,
        llm=object(),
        artifacts=None,
        paths=None,
    )

    result = await engine.execute(input="RAG factuality", use_tools=True)

    assert result["status"] == "ok"
    assert seen["search"] is not None
    assert seen["s2_api_key"] == ""


@pytest.mark.asyncio
async def test_a_missing_key_costs_one_source_not_the_whole_run(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """Omni's own registry calls a keyless Semantic Scholar *degraded*, not unusable.

    The skill used to disagree with it and refuse outright, which ended the turn
    on a connector the funnel would have simply ranked lower — behind arXiv,
    OpenAlex, Crossref and PubMed, none of which need a key at all. The pipeline
    already degrades to LLM-only reasoning when literature is thin; it never
    needed a precondition in front of it.
    """
    engine_module = _load_module("engine.py", "research_ideation_empty_scoped_s2_engine")
    settings.research.connectors = ["semanticscholar", "arxiv"]
    settings.research.semantic_scholar_api_key = ""

    seen: dict[str, Any] = {}

    def observed_pipeline(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "ok", "summary": "ideated", "final_idea": {"title": "T"}}

    monkeypatch.setattr(engine_module._core, "run_pipeline", observed_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(
        settings=settings, llm=object(), artifacts=None, paths=None
    )

    result = await engine.execute(input="RAG factuality", use_tools=False)

    assert result["status"] == "ok"
    assert seen["search"] is not None, "the run must go through the host funnel"


@pytest.mark.asyncio
async def test_an_unscoped_process_key_is_still_never_inherited(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    """Dropping the precondition must not drop the secret boundary with it.

    ``search_papers`` falls back to the ambient ``S2_API_KEY`` when its caller
    passes ``None``. A host that scoped the connector to public access says so
    with an empty string, and that distinction is the only thing keeping a
    process-wide credential out of a sandboxed run.
    """
    engine_module = _load_module("engine.py", "research_ideation_scope_boundary_engine")
    settings.research.connectors = ["semanticscholar"]
    settings.research.semantic_scholar_api_key = ""
    monkeypatch.setenv("S2_API_KEY", "ambient-process-secret")

    seen: dict[str, Any] = {}

    def observed_pipeline(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "ok", "summary": "ideated", "final_idea": {"title": "T"}}

    monkeypatch.setattr(engine_module._core, "run_pipeline", observed_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(
        settings=settings, llm=object(), artifacts=None, paths=None
    )

    await engine.execute(input="RAG factuality", use_tools=False)

    assert seen["s2_api_key"] == "", "an empty scope must not become None"
    assert seen["s2_api_key"] != "ambient-process-secret"


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
    assert "## References" not in report


def test_markdown_report_appends_references_from_sources() -> None:
    engine_module = _load_module(
        "engine.py", "research_ideation_report_references_engine"
    )

    report = engine_module._build_markdown_report(
        {
            "research_question": "latent steering",
            "steps": {"search": {"papers": []}},
            "sources": [
                {
                    "title": "Inference-Time Intervention",
                    "authors": ["Kevin Li", "Ada Researcher"],
                    "year": 2023,
                    "venue": "NeurIPS",
                    "doi": "10.1000/iti",
                }
            ],
            "final_idea": {"title": "Steer agents in latent space"},
        }
    )

    assert "## References" in report
    assert (
        "1. Inference-Time Intervention. Kevin Li et al. 2023. NeurIPS. "
        "https://doi.org/10.1000/iti"
    ) in report


@pytest.mark.asyncio
async def test_omni_engine_records_real_source_and_run_provenance(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    from omni.research import ResearchStore
    from omni.storage.db import get_database

    engine_module = _load_module("engine.py", "research_ideation_provenance_engine")
    monkeypatch.setattr(
        engine_module,
        "_resolve_s2_key",
        lambda _ctx: "scoped-s2-secret",
    )
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
