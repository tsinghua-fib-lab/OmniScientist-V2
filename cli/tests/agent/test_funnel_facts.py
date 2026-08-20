"""Empty literature funnels are observations, not this-turn research."""

from __future__ import annotations

import json
from pathlib import Path

from omni.core.funnel_facts import (
    has_literature_hits,
    is_empty_literature_funnel,
    literature_funnel_facts,
    project_skill_observation,
)
from omni.core.observation import compact_observation


def _ideation(*, papers: list[dict], queries: list[str] = ("latent space",)) -> dict:
    return {
        "status": "partial" if not papers else "ok",
        "warning": (
            "Literature search returned zero relevant papers for the generated "
            "queries. Continuing with LLM-only reasoning."
            if not papers
            else ""
        ),
        "summary": "Generated 3 candidate ideas, best score: 8.0/10",
        "steps": {
            "search": {
                "queries": list(queries),
                "paper_count": len(papers),
                "papers": papers,
            }
        },
        "final_idea": {"title": "Steer activations"},
    }


def test_ideation_empty_funnel_lifts_queries_and_zero_kept() -> None:
    facts = literature_funnel_facts(_ideation(papers=[]))
    assert facts is not None
    assert facts["queries"] == ["latent space"]
    assert facts["n_kept"] == 0
    assert facts["n_retrieved"] == 0
    assert "zero relevant papers" in facts["warning"]
    assert is_empty_literature_funnel(_ideation(papers=[])) is True


def test_ideation_with_papers_is_not_empty() -> None:
    payload = _ideation(papers=[{"title": "Representation Engineering", "arxiv_id": "2310.01405"}])
    facts = literature_funnel_facts(payload)
    assert facts is not None
    assert facts["n_kept"] == 1
    assert is_empty_literature_funnel(payload) is False
    assert has_literature_hits(payload) is True


def test_search_literature_empty_and_hits() -> None:
    empty = {"status": "empty", "query": "latent space", "count": 0, "results": []}
    hits = {
        "status": "ok",
        "query": "activation steering",
        "count": 2,
        "results": [{"title": "ITI"}, {"title": "RepE"}],
        "per_source": {"openalex": {"found": 8, "kept": 2}},
    }
    assert is_empty_literature_funnel(empty) is True
    facts = literature_funnel_facts(hits)
    assert facts is not None
    assert facts["queries"] == ["activation steering"]
    assert facts["n_kept"] == 2
    assert facts["n_retrieved"] == 8


def test_figure_partial_is_not_a_literature_funnel() -> None:
    figure = {
        "status": "partial",
        "warning": "This is a weaker Graphviz schematic synthesized from the named stages.",
        "figure_kind": "generic",
        "artifacts": [{"format": "svg", "path": "/tmp/x.svg"}],
    }
    assert literature_funnel_facts(figure) is None
    assert is_empty_literature_funnel(figure) is False
    assert has_literature_hits(figure) is False


def test_arxiv_fetch_counts_as_hits() -> None:
    paper = {
        "status": "ok",
        "title": "Inference-Time Intervention",
        "arxiv_id": "2306.03341",
    }
    assert has_literature_hits(paper) is True
    assert is_empty_literature_funnel(paper) is False


def test_wrapper_projects_empty_funnel_as_degraded() -> None:
    wrapped = project_skill_observation(
        _ideation(papers=[]),
        extra={"skill_name": "research-ideation", "mode": "inline"},
    )
    assert wrapped["status"] == "degraded"
    assert wrapped["n_kept"] == 0
    assert wrapped["queries"] == ["latent space"]
    assert "zero relevant papers" in wrapped["warning"]
    assert wrapped["skill_name"] == "research-ideation"
    assert wrapped["observation"]["schema"] == "omni.engine.observation/v1"
    assert wrapped["observation"]["metrics"]["n_kept"] == 0


def test_compact_observation_keeps_funnel_facts_above_paper_bodies() -> None:
    payload = project_skill_observation(
        _ideation(papers=[]),
        extra={"skill_name": "research-ideation", "mode": "inline"},
    )
    payload["result"]["steps"]["search"]["papers"] = [
        {"title": f"Paper {index}", "abstract": "A" * 2000} for index in range(20)
    ]
    observation = compact_observation(payload, max_chars=8000)
    assert '"n_kept": 0' in observation
    assert "latent space" in observation
    assert "zero relevant papers" in observation
    assert observation.count("A" * 2000) == 0


def test_compact_observation_head_tail_truncates_a_plain_string() -> None:
    text = "HEAD\n" + ("body\n" * 200) + "TAIL"
    observation = compact_observation(text, max_chars=200)
    assert len(observation) <= 200
    assert observation.startswith("Warning: truncated output")
    assert "original token count:" in observation
    assert "Total output lines:" in observation
    assert "HEAD" in observation
    assert "TAIL" in observation
    assert "chars truncated" in observation


def test_compact_observation_keeps_full_source_id_list() -> None:
    source_ids = [f"src-{index:02d}" for index in range(22)]
    observation = compact_observation(
        {
            "status": "ok",
            "count": 17,
            "source_ids": source_ids,
            "results": [{"title": f"Paper {index}"} for index in range(17)],
        },
        max_chars=8000,
    )
    parsed = json.loads(observation)
    assert parsed["source_ids"] == source_ids
    assert parsed["count"] == 17
    assert parsed["results"][-1] == "… 9 more"


def test_compact_observation_spills_source_ids_when_over_budget(tmp_path) -> None:
    source_ids = [f"source-{index:04d}-{'x' * 80}" for index in range(80)]
    observation = compact_observation(
        {"status": "ok", "source_ids": source_ids, "count": 80},
        max_chars=1500,
        spill_dir=tmp_path,
    )
    parsed = json.loads(observation)
    assert parsed["source_ids_count"] == 80
    spilled = Path(parsed["source_ids_spill"])
    assert spilled.is_file()
    assert spilled.read_text(encoding="utf-8").splitlines() == source_ids
    assert parsed["source_ids"][-1].endswith("more")
    assert parsed["source_ids"][0] == source_ids[0]


def test_compact_observation_uses_head_tail_when_spill_still_overflows(tmp_path) -> None:
    source_ids = [f"source-{index:04d}-{'x' * 80}" for index in range(80)]
    payload = {"status": "ok", "source_ids": source_ids, "count": 80}
    fitted = compact_observation(payload, max_chars=1500, spill_dir=tmp_path)
    cap = len(fitted) - 1
    observation = compact_observation(payload, max_chars=cap, spill_dir=tmp_path)
    assert len(observation) <= cap
    assert "Warning: truncated output" in observation
    assert "original token count:" in observation
    assert "Total output lines:" in observation
    assert "chars truncated" in observation
    assert '"status"' in observation
    assert "Full source_ids saved to:" in observation
    spilled = Path(json.loads(fitted)["source_ids_spill"])
    assert spilled.read_text(encoding="utf-8").splitlines() == source_ids
