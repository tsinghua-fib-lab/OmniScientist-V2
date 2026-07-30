"""Offline regression tests for the portable research-ideation HTTP client."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
import yaml

SKILL_ROOT = Path(__file__).resolve().parents[3] / "skills" / "research-ideation"


def _load_core(module_name: str):
    path = SKILL_ROOT / "core.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_runner(module_name: str):
    path = SKILL_ROOT / "scripts" / "run.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _frontmatter() -> dict:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])


def _portable_llm(runner, *, api_key: str = "secret"):
    return runner.OpenAICompatibleLLM(
        {
            "base_url": "https://models.invalid/v1",
            "api_key": api_key,
            "model": "test-model",
            "temperature": 0.2,
        }
    )


def test_research_ideation_has_no_openai_sdk_dependency() -> None:
    helix = _frontmatter()["metadata"]["helixforge"]
    dependencies = set(helix["dependencies"])
    runtime_modules = set(
        helix.get("runtime_requirements", {}).get("python_modules", [])
    )
    core_source = (SKILL_ROOT / "core.py").read_text(encoding="utf-8")
    runner_source = (SKILL_ROOT / "scripts" / "run.py").read_text(encoding="utf-8")

    assert not any(item == "openai" or item.startswith("openai>=") for item in dependencies)
    assert "openai" not in runtime_modules
    assert "from openai import" not in core_source
    assert "import requests" not in core_source
    assert "import httpx" in core_source
    assert "LLM_GATEWAY_BASE_URL" not in core_source
    assert "LLM_GATEWAY_BASE_URL" in runner_source


def test_research_ideation_authentication_errors_are_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_core("research_ideation_http_auth_core")
    runner = _load_runner("research_ideation_http_auth_runner")
    calls = 0

    def reject_request(_payload: dict, _config: dict) -> None:
        nonlocal calls
        calls += 1
        raise core.LLMHTTPError(401, "invalid API key")

    monkeypatch.setattr(core, "_chat_completion", reject_request)

    with pytest.raises(core.LLMHTTPError):
        core._llm_chat(
            "test prompt",
            max_retries=5,
            llm=_portable_llm(runner, api_key="invalid"),
        )

    assert calls == 1


def test_research_ideation_uses_openai_compatible_http_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_core("research_ideation_http_protocol_core")
    runner = _load_runner("research_ideation_http_protocol_runner")
    captured: dict[str, object] = {}

    class _Response:
        is_error = False

        @staticmethod
        def json() -> dict:
            return {"choices": [{"message": {"content": "research direction"}}]}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            captured["client"] = kwargs

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, **kwargs: object) -> _Response:
            captured["url"] = url
            captured["request"] = kwargs
            return _Response()

    monkeypatch.setattr(runner.httpx, "Client", _Client)
    result = core._llm_chat(
        "Generate one idea",
        llm=_portable_llm(runner),
    )

    assert result == "research direction"
    assert captured["url"] == "https://models.invalid/v1/chat/completions"
    request = captured["request"]
    assert isinstance(request, dict)
    assert request["headers"]["Authorization"] == "Bearer secret"
    assert request["json"]["model"] == "test-model"


def test_research_ideation_http_tool_call_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_core("research_ideation_http_tools_core")
    runner = _load_runner("research_ideation_http_tools_runner")
    requests: list[dict] = []
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"query":"RAG"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "grounded idea"}}]},
        ]
    )

    def complete(payload: dict, _config: dict) -> dict:
        requests.append(payload)
        return next(responses)

    monkeypatch.setattr(core, "_chat_completion", complete)
    result = core._llm_chat_with_tools(
        "Generate one idea",
        "Use evidence",
        tools=[{"type": "function", "function": {"name": "search"}}],
        tool_handlers={"search": lambda query: {"query": query, "papers": 2}},
        llm=_portable_llm(runner),
    )

    assert result == "grounded idea"
    assert len(requests) == 2
    second_messages = requests[1]["messages"]
    assert second_messages[-1]["role"] == "tool"
    assert second_messages[-1]["tool_call_id"] == "call_1"
    assert '"papers": 2' in second_messages[-1]["content"]


def test_scoped_s2_key_reaches_initial_and_ideation_tool_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_core("research_ideation_scoped_s2_core")
    search_calls: list[tuple[str, str | None]] = []

    def fake_search(
        query: str,
        limit: int = 10,
        sort_by: str = "relevance",
        api_key: str | None = None,
    ) -> list[dict]:
        del limit, sort_by
        search_calls.append((query, api_key))
        return [
            {
                "title": "Grounded Ideation",
                "abstract": "Evidence-grounded research ideation.",
                "year": 2025,
            }
        ]

    class PipelineLLM:
        def complete(self, payload: dict) -> dict:
            prompt = payload["messages"][-1]["content"]
            if prompt.startswith("Generate 1-3"):
                content = '{"queries":["grounded ideation"]}'
            elif prompt.startswith("Select papers"):
                content = '{"relevant_indices":[0]}'
            elif prompt.startswith("Extract the most important"):
                content = (
                    '{"core_concepts":["evidence grounding"],'
                    '"domain_concepts":["research ideation"]}'
                )
            elif prompt.startswith("Normalize and merge"):
                content = (
                    '{"merged_core":["evidence grounding"],'
                    '"merged_domains":["research ideation"],'
                    '"mapping":{}}'
                )
            else:  # pragma: no cover - unexpected prompt drift is actionable
                raise AssertionError(f"unexpected prompt: {prompt[:80]}")
            return {"choices": [{"message": {"content": content}}]}

    class ToolCallingLLM:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _payload: dict) -> dict:
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_s2",
                                        "type": "function",
                                        "function": {
                                            "name": "semantic_scholar_search",
                                            "arguments": (
                                                '{"query":"grounding novelty",'
                                                '"limit":3}'
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<brainstorm><concept>Grounding</concept>"
                                "</brainstorm>\n"
                                '{"title":"Evidence Loop","background":"b",'
                                '"related_work":"r","gap_analysis":"g",'
                                '"proposed_method":"m"}'
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(core, "search_papers", fake_search)
    core.search_and_extract(
        "How should ideas be grounded?",
        llm=PipelineLLM(),
        s2_api_key="scoped-s2-secret",
    )
    core.generate_idea(
        "Retrieved evidence is not tied to ideation.",
        ["evidence grounding"],
        [{"title": "Grounded Ideation", "year": 2025}],
        use_tools=True,
        llm=ToolCallingLLM(),
        s2_api_key="scoped-s2-secret",
    )

    assert search_calls == [
        ("grounded ideation", "scoped-s2-secret"),
        ("grounding novelty", "scoped-s2-secret"),
    ]


def test_ideation_parser_accepts_brainstorm_and_prose_wrapped_json() -> None:
    core = _load_core("research_ideation_brainstorm_parser_core")
    raw = (
        "Draft follows.\n"
        "<brainstorm><concept>Grounding</concept>Trace evidence.</brainstorm>\n"
        "Final proposal:\n"
        '{"title":"Evidence Loop","background":"b","related_work":"r",'
        '"gap_analysis":"g","proposed_method":"m"}\n'
        "End of proposal."
    )

    idea = core._parse_ideation_output(raw)

    assert "<brainstorm>" in core.IDEATION_PROMPT
    assert idea["title"] == "Evidence Loop"
    assert idea["brainstorming"] == (
        "<concept>Grounding</concept>Trace evidence."
    )


def test_json_retry_explicitly_requests_json_only() -> None:
    core = _load_core("research_ideation_json_retry_core")
    prompts: list[str] = []

    class RetryLLM:
        def complete(self, payload: dict) -> dict:
            prompts.append(payload["messages"][-1]["content"])
            content = "This is not JSON." if len(prompts) == 1 else '{"ok":true}'
            return {"choices": [{"message": {"content": content}}]}

    result = core._llm_chat_json("Return a result.", retries=1, llm=RetryLLM())

    assert result == {"ok": True}
    assert prompts[0] == "Return a result."
    assert "previous response could not be parsed as JSON" in prompts[1]
    assert "Return ONLY the JSON object" in prompts[1]
    assert "This is not JSON." in prompts[1]


def test_semantic_scholar_transport_failure_is_not_reported_as_zero_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_core("research_ideation_s2_transport_core")

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _url: str, **_kwargs: object) -> None:
            raise httpx.ConnectError("semantic scholar unavailable")

    monkeypatch.setattr(core.httpx, "Client", _Client)
    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)

    with pytest.raises(core.LiteratureSearchError, match="Semantic Scholar"):
        core.search_papers("retrieval augmented generation")


def test_semantic_scholar_result_preserves_stable_source_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_core("research_ideation_s2_source_core")
    captured: dict[str, object] = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "data": [
                    {
                        "paperId": "s2-paper-123",
                        "url": "https://www.semanticscholar.org/paper/s2-paper-123",
                        "externalIds": {
                            "DOI": "10.1000/example",
                            "ArXiv": "2401.01234",
                        },
                        "title": "Grounded ideation",
                        "abstract": "An evidence-grounded ideation method.",
                        "year": 2024,
                        "authors": [{"name": "Ada Researcher"}],
                    }
                ]
            }

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _url: str, **kwargs: object) -> _Response:
            captured.update(kwargs)
            return _Response()

    monkeypatch.setattr(core.httpx, "Client", _Client)

    papers = core.search_papers(
        "grounded ideation",
        limit=1,
        api_key="scoped-s2-secret",
    )

    fields = str(captured["params"]["fields"])
    assert {"paperId", "url", "externalIds"} <= set(fields.split(","))
    assert captured["headers"]["x-api-key"] == "scoped-s2-secret"
    assert papers == [
        {
            "paperId": "s2-paper-123",
            "url": "https://www.semanticscholar.org/paper/s2-paper-123",
            "externalIds": {"DOI": "10.1000/example", "ArXiv": "2401.01234"},
            "doi": "10.1000/example",
            "arxiv_id": "2401.01234",
            "title": "Grounded ideation",
            "abstract": "An evidence-grounded ideation method.",
            "summary": "An evidence-grounded ideation method.",
            "year": 2024,
            "publicationDate": None,
            "citationCount": 0,
            "venue": "",
            "authors": ["Ada Researcher"],
        }
    ]


def test_explicit_empty_s2_key_does_not_inherit_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _load_core("research_ideation_s2_key_scope_core")
    headers: list[dict[str, str]] = []

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"data": []}

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _url: str, **kwargs: object) -> _Response:
            value = kwargs.get("headers")
            assert isinstance(value, dict)
            headers.append(value)
            return _Response()

    monkeypatch.setattr(core.httpx, "Client", _Client)
    monkeypatch.setenv("S2_API_KEY", "portable-process-secret")

    core.search_papers("portable fallback")
    core.search_papers("host-scoped public access", api_key="")

    assert headers == [
        {"x-api-key": "portable-process-secret"},
        {},
    ]
