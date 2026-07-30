"""BUG-01: 429 / missing-key literature search must degrade, not abort the run.

The field reports (Yu / Zeng / Xu / Zhao / Fang / Chen) described three
different failures that looked like one: Semantic Scholar 429, a missing S2
key, and an OpenAlex quota 429 each stopped the whole research turn. This
module replays those envelopes against the current host funnel and the
research-ideation adapter so a regression cannot hide behind a rewritten
SKILL.md.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omni.config import load_settings
from omni.research import connectors
from omni.research.http_policy import FailureKind, classify
from omni.research.retrieval import search_literature
from omni.research.tools import build_research_tools
from omni.runtime.workflow_plan import _step_failure_recoverable
from omni.skills_runtime.context import ExecContext
from omni.storage.db import get_database

SKILL_ROOT = Path(__file__).resolve().parents[3] / "skills" / "research-ideation"


def _load_engine():
    path = SKILL_ROOT / "engine.py"
    spec = importlib.util.spec_from_file_location("bug01_ideation_engine", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _ctx(*, connectors_allow: list[str] | None = None, s2_key: str = "") -> ExecContext:
    settings = load_settings()
    settings.research.contact_email = "test@example.com"
    settings.research.semantic_scholar_api_key = s2_key
    if connectors_allow is not None:
        settings.research.connectors = connectors_allow
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    return ExecContext(
        settings=settings,
        paths=settings.paths,
        project=settings.paths.project_name,
        session_id="bug01",
        channel="cli",
        db=db,
        llm=None,
    )


def _openalex_hit() -> dict[str, Any]:
    return {
        "results": [{
            "title": "OpenAlex Fallback Paper",
            "publication_year": 2024,
            "doi": "https://doi.org/10.1/oa-fallback",
            "id": "https://openalex.org/W1",
            "authorships": [{"author": {"display_name": "A Author"}}],
        }]
    }


def _s2_429() -> connectors.ConnectorError:
    return connectors.ConnectorError(
        "https://api.semanticscholar.org/graph/v1/paper/search returned HTTP 429: 'rate limited'",
        kind=FailureKind.QUOTA_EXHAUSTED,
        provider="semanticscholar",
        status_code=429,
        remediation="semanticscholar is out of quota for now; retry later.",
    )


def _openalex_quota() -> connectors.ConnectorError:
    return connectors.ConnectorError(
        "https://api.openalex.org/works returned HTTP 429: 'Insufficient budget, you only have $0'",
        kind=FailureKind.QUOTA_EXHAUSTED,
        provider="openalex",
        status_code=429,
        remediation="openalex is out of quota for now; let another connector cover this query.",
    )


@pytest.mark.asyncio
async def test_s2_429_falls_back_to_openalex_instead_of_aborting(monkeypatch):
    """于恒彬: S2 HTTP 429 must not sink the search when another source still works."""

    async def _fake(url, params, **_kw):
        if "semanticscholar" in url:
            raise _s2_429()
        if "openalex" in url:
            return _openalex_hit()
        return {}

    async def _no_arxiv(*_a, **_k):
        return []

    monkeypatch.setattr(connectors, "_get_json", _fake)
    monkeypatch.setattr("omni.research.arxiv.search", _no_arxiv)
    ctx = await _ctx(connectors_allow=["semanticscholar", "openalex", "arxiv"], s2_key="")
    out = await search_literature(ctx, query="latent space intervention", rows=3)

    assert out["status"] == "partial"
    assert out["count"] >= 1
    assert any(r["title"] == "OpenAlex Fallback Paper" for r in out["results"])
    assert any("semanticscholar" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_missing_s2_key_does_not_stop_ideation(monkeypatch, settings):
    """曾冠阳 / 方羿: a missing S2 key is not needs_input and not s2_api_key_missing."""
    engine_module = _load_engine()
    settings.research.connectors = ["semanticscholar", "arxiv"]
    settings.research.semantic_scholar_api_key = ""
    seen: dict[str, Any] = {}

    def observed_pipeline(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {
            "status": "ok",
            "outcome": {"code": "ideas_generated", "count": 1},
            "final_idea": {"title": "A direction"},
            "summary": "ideated without a Semantic Scholar key",
        }

    monkeypatch.setattr(engine_module._core, "run_pipeline", observed_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(settings=settings, llm=object(), artifacts=None, paths=None)

    result = await engine.execute(input="How to steer LLM agents?", use_tools=False)

    assert result["status"] == "ok"
    assert result.get("outcome", {}).get("code") != "s2_api_key_missing"
    assert seen["search"] is not None
    assert seen["s2_api_key"] == ""


@pytest.mark.asyncio
async def test_openalex_quota_falls_back_to_arxiv(monkeypatch):
    """许安杰: OpenAlex $0 quota must leave a persistable arXiv result."""

    async def _fake(url, params, **_kw):
        if "openalex" in url:
            raise _openalex_quota()
        if "semanticscholar" in url:
            raise _s2_429()
        return {}

    async def _arxiv(query, max_results=8):  # noqa: ARG001
        return [{
            "title": "ArXiv Fallback Paper",
            "arxiv_id": "2401.00001",
            "summary": "A preprint that survived the quota.",
            "url": "https://arxiv.org/abs/2401.00001",
            "origin": "arxiv",
        }]

    monkeypatch.setattr(connectors, "_get_json", _fake)
    monkeypatch.setattr("omni.research.arxiv.search", _arxiv)
    ctx = await _ctx(connectors_allow=["semanticscholar", "openalex", "arxiv"])
    out = await search_literature(ctx, query="agentic LLM", rows=3)

    assert out["status"] == "partial"
    assert any(r.get("arxiv_id") == "2401.00001" for r in out["results"])
    assert any(p["name"] == "arxiv" and p["state"] == "ok" for p in out["providers"])


def test_openalex_insufficient_budget_is_quota_not_a_retry_loop():
    """The 40.9s stall: 'Insufficient budget, you only have $0' must not back off."""
    kind = classify(429, {}, "Insufficient budget, you only have $0")
    assert kind is FailureKind.QUOTA_EXHAUSTED


def test_burst_429_is_transient_and_honours_retry_after():
    kind = classify(429, {"Retry-After": "2"}, "slow down")
    assert kind is FailureKind.TRANSIENT


@pytest.mark.asyncio
async def test_disabled_s2_still_runs_ideation_through_the_funnel(monkeypatch, settings):
    """赵萌生: disabling Semantic Scholar must not refuse the whole skill."""
    engine_module = _load_engine()
    settings.research.connectors = ["arxiv"]
    settings.research.semantic_scholar_api_key = "must-not-be-used"
    seen: dict[str, Any] = {}

    def observed_pipeline(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "ok", "summary": "ideated on arXiv", "final_idea": {"title": "T"}}

    monkeypatch.setattr(engine_module._core, "run_pipeline", observed_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(settings=settings, llm=object(), artifacts=None, paths=None)

    result = await engine.execute(input="RAG factuality", use_tools=False)

    assert result["status"] == "ok"
    assert result.get("outcome", {}).get("code") != "connector_disabled"
    assert seen["search"] is not None
    assert seen["s2_api_key"] == ""


@pytest.mark.asyncio
async def test_empty_funnel_lets_ideation_continue_llm_only(monkeypatch):
    """All live sources dead → empty results, not a raised abort; pipeline already continues."""
    engine_module = _load_engine()

    async def empty_funnel(_ctx, *, query: str, rows: int, **_kw):  # noqa: ARG001
        return {"status": "empty", "results": [], "errors": ["semanticscholar: HTTP 429"]}

    import omni.research as research

    monkeypatch.setattr(research, "search_literature", empty_funnel)

    def fake_pipeline(**kwargs: Any) -> dict[str, Any]:
        papers = kwargs["search"]("unexplored topic", 6)
        assert papers == []
        return {
            "status": "partial",
            "outcome": {"code": "ideas_generated_partial"},
            "warning": "Continuing with LLM-only reasoning",
            "final_idea": {"title": "Prior-only idea"},
        }

    monkeypatch.setattr(engine_module._core, "run_pipeline", fake_pipeline)
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(settings=SimpleNamespace(), llm=object(), artifacts=None, paths=None)
    monkeypatch.setattr(engine_module, "_resolve_s2_key", lambda _ctx: "")

    result = await engine.execute(input="An unexplored topic", use_tools=False)

    assert result["status"] == "partial"
    assert result["final_idea"]["title"] == "Prior-only idea"


def test_continue_with_partial_recovers_ideation_error_but_cannot_invent_sibling_steps():
    """许安杰: a 1-step workflow cannot grow code/experiment steps after a support miss.

    continue_with_partial marks the failed ideation step recoverable. It does
    not spawn the independent deliverables the planner never sealed. That is
    why a 0/1 failed workflow is honest for a one-step plan, and why the
    multi-deliverable request must not collapse to a single research-ideation
    step.
    """
    entry = SimpleNamespace(workflow={"failure_policy": "continue_with_partial"})
    assert _step_failure_recoverable(
        {},
        entry,
        {"status": "error", "recoverable": True, "blocking": False},
    )


@pytest.mark.asyncio
async def test_openalex_search_skill_falls_back_to_arxiv_after_quota(monkeypatch):
    """陈治宇: the OpenAlex skill must not strand a literature request on 429."""
    path = Path(__file__).resolve().parents[3] / "skills" / "openalex-search" / "engine.py"
    spec = importlib.util.spec_from_file_location("bug01_openalex_engine", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def _fake(url, params, **_kw):
        if "openalex" in url:
            raise _openalex_quota()
        return {}

    async def _arxiv(query, max_results=8):  # noqa: ARG001
        return [{
            "title": "ArXiv After OpenAlex Quota",
            "arxiv_id": "2401.00002",
            "summary": "Survived the OpenAlex budget.",
            "origin": "arxiv",
        }]

    monkeypatch.setattr(connectors, "_get_json", _fake)
    monkeypatch.setattr("omni.research.arxiv.search", _arxiv)
    ctx = await _ctx(connectors_allow=["openalex", "arxiv"])
    engine = module.OpenAlexSearchEngine()
    engine.ctx = ctx

    result = await engine.execute(query="single-cell foundation models")

    assert result["status"] == "partial"
    assert result["outcome"]["code"] == "openalex_degraded_fallback"
    assert any(r.get("arxiv_id") == "2401.00002" for r in result["results"])
    assert "Fell back" in result["warning"]


@pytest.mark.asyncio
async def test_search_literature_tool_never_raises_on_total_outage(monkeypatch):
    async def _dead(url, params, **_kw):  # noqa: ARG001
        raise _openalex_quota()

    async def _no_arxiv(*_a, **_k):
        return []

    monkeypatch.setattr(connectors, "_get_json", _dead)
    monkeypatch.setattr("omni.research.arxiv.search", _no_arxiv)
    ctx = await _ctx(connectors_allow=["openalex", "arxiv"])
    tool = {t.spec.name: t for t in build_research_tools(ctx)}["search_literature"]
    out = await tool.handler({"query": "anything"})
    assert out["status"] == "empty"
    assert out["results"] == []
    assert out["remediation"]
