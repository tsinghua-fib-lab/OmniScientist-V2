"""Simulate catalog novlm rows with VLM admission forced off.

Cases: A-FIG-03, A-FIG-05, A-LF-03, E-03, P-01, A-POS-03, A-REV-03.
Named livefigure hard-stops. An unspecified figure stays on scientific-figure.
Poster and paper-review keep running; they do not swap or become needs_input.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omni.agent.figure_runner import host_fill_figure
from omni.agent.skill_lookup import rank_skill_matches, skill_contract_card
from omni.config import load_settings
from omni.runtime.remaining import infer_figure_and_paper_outputs, infer_slide_outputs
from omni.skills_runtime.admission import skill_admission_rejection
from omni.skills_runtime.registry import SkillRegistry
from omni.skills_runtime.slot_routing import (
    allow_slot_fallback,
    skip_observation,
    user_named_skill,
)

_A_FIG_01 = (
    "Draw a RAG architecture diagram that includes query, retriever, reranker, and LLM."
)
_A_LF_01 = (
    "$livefigure Make one editable PPTX slide of a RAG architecture with query, "
    "retriever, reranker, and LLM."
)
_A_LF_02 = "Make this RAG figure one editable scientific figure in PowerPoint."
_A_POS_01 = "Make a scientific poster summarizing RAG evaluation benchmarks."
_A_REV_01 = "Review arXiv 1706.03762 as a NeurIPS reviewer."
_P_01 = (
    "Prepare materials for a RAG system survey: fetch the abstract of Attention "
    "Is All You Need, generate a scientific architecture figure that includes "
    "query, retriever, reranker, and LLM, and write a paper plus a slide deck."
)
_E_03 = (
    "In one request: search RAG 2024 papers, write a two-paragraph related-work "
    "section, and make one editable LiveFigure PPTX slide. If LiveFigure cannot "
    "run, continue the rest."
)


def _vlm_off() -> SimpleNamespace:
    return SimpleNamespace(
        available=False,
        setup_command="omni config vlm",
        error_code="vlm_not_configured",
        missing=("model", "endpoint", "api_key"),
    )


def _registry() -> SkillRegistry:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    registry.use_admission_services({"vlm": _vlm_off()})
    return registry


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-sci"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(
                status="succeeded",
                error="",
                result_json={"status": "ok", "skill_name": "scientific-figure"},
            )
        ),
    )


def test_livefigure_admission_blocks_engine_when_vlm_missing() -> None:
    registry = _registry()
    live = registry.get("livefigure")
    poster = registry.get("scientific-poster")
    review = registry.get("paper-review")
    sci = registry.get("scientific-figure")
    assert live is not None and poster is not None and review is not None and sci is not None
    services = registry.admission_services()
    rejected = skill_admission_rejection(live, services=services)
    assert rejected is not None
    assert rejected["error_info"]["code"] == "vlm_not_configured"
    assert rejected["action_required"]["command"] == "omni config vlm"
    assert rejected["do_not_retry"] is True
    assert skill_admission_rejection(sci, services=services) is None
    assert skill_admission_rejection(poster, services=services) is None
    assert skill_admission_rejection(review, services=services) is None


@pytest.mark.asyncio
async def test_a_fig_03_unspecified_figure_stays_on_scientific_figure() -> None:
    """A-FIG-03: format-neutral figure uses scientific-figure. Not needs_input."""
    registry = _registry()
    selected, rejected = registry.resolve_capability("artifact.figure", request=_A_FIG_01)
    assert selected is not None and selected.name == "scientific-figure"
    assert any(item.name == "livefigure" and "vlm_not_configured" in why for item, why in rejected)
    assert allow_slot_fallback(
        preferred="livefigure", slot="artifact.figure", user_message=_A_FIG_01
    )

    runtime = _runtime()
    filled = await host_fill_figure(
        runtime=runtime,
        registry=registry,
        task_id="fig-03",
        session_id="s1",
        user_message=_A_FIG_01,
    )
    assert runtime.enqueue.await_count == 1
    assert runtime.enqueue.await_args.args[0] == "scientific-figure"
    observed = " ".join(str(line) for line in filled.get("observations") or [])
    assert "Using scientific-figure instead" not in observed
    assert filled["skill"] == "scientific-figure"
    assert filled.get("status") not in {"needs_input"}


@pytest.mark.asyncio
async def test_a_fig_05_novlm_ignores_invented_dot() -> None:
    """A-FIG-05 / novlm: leftover rag.dot is not passed; provider is scientific-figure."""
    runtime = _runtime()
    filled = await host_fill_figure(
        runtime=runtime,
        registry=_registry(),
        task_id="fig-05",
        session_id="s1",
        user_message=_A_FIG_01,
        source_artifact_path="figures/rag.dot",
    )
    assert runtime.enqueue.await_args.args[0] == "scientific-figure"
    assert "source_artifact_path" not in runtime.enqueue.await_args.args[1]
    assert filled["skill"] == "scientific-figure"


@pytest.mark.asyncio
async def test_a_lf_03_named_livefigure_hard_stops() -> None:
    """A-LF-03: $livefigure does not silently swap to scientific-figure."""
    registry = _registry()
    assert user_named_skill(_A_LF_01, "livefigure")
    selected, rejected = registry.resolve_capability(
        "figure.editable.pptx", request=_A_LF_01
    )
    assert selected is None
    assert any(item.name == "livefigure" for item, _ in rejected)
    assert not allow_slot_fallback(
        preferred="livefigure", slot="artifact.figure", user_message=_A_LF_01
    )

    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-live"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(
                status="failed",
                error="vlm_not_configured",
                result_json={
                    "status": "error",
                    "error_info": {"code": "vlm_not_configured"},
                    "action_required": {
                        "kind": "configure",
                        "service": "vlm",
                        "command": "omni config vlm",
                    },
                    "setup_command": "omni config vlm",
                },
            )
        ),
    )
    filled = await host_fill_figure(
        runtime=runtime,
        registry=registry,
        task_id="lf-03",
        session_id="s1",
        user_message=_A_LF_01,
    )
    names = [call.args[0] for call in runtime.enqueue.await_args_list]
    assert names == ["livefigure"]
    assert "scientific-figure" not in names
    assert filled["skill"] == "livefigure"
    assert filled.get("status") != "succeeded"


def test_a_lf_02_without_dollar_name_is_not_the_hard_stop() -> None:
    """A-LF-02 wording is VLM-on. Without `$livefigure` it is not A-LF-03."""
    assert not user_named_skill(_A_LF_02, "livefigure")
    assert allow_slot_fallback(
        preferred="livefigure", slot="artifact.figure", user_message=_A_LF_02
    )
    selected, rejected = _registry().resolve_capability("artifact.figure", request=_A_LF_02)
    assert selected is not None and selected.name == "scientific-figure"
    assert any("vlm_not_configured" in why for _, why in rejected)


@pytest.mark.asyncio
async def test_e_03_named_livefigure_row_matches_a_lf_03() -> None:
    """E-03 novlm: the LiveFigure row hard-stops; siblings are not this test."""
    registry = _registry()
    assert user_named_skill(_E_03, "livefigure")
    selected, rejected = registry.resolve_capability("figure.editable.pptx", request=_E_03)
    assert selected is None
    assert any(item.name == "livefigure" for item, _ in rejected)
    assert not allow_slot_fallback(
        preferred="livefigure", slot="artifact.figure", user_message=_E_03
    )
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-live"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(
                status="failed",
                error="vlm_not_configured",
                result_json={
                    "status": "error",
                    "error_info": {"code": "vlm_not_configured"},
                    "setup_command": "omni config vlm",
                },
            )
        ),
    )
    filled = await host_fill_figure(
        runtime=runtime,
        registry=registry,
        task_id="e-03",
        session_id="s1",
        user_message=_E_03,
    )
    assert [call.args[0] for call in runtime.enqueue.await_args_list] == ["livefigure"]
    assert filled["skill"] == "livefigure"
    assert infer_figure_and_paper_outputs(_E_03) == []


@pytest.mark.asyncio
async def test_p01_novlm_figure_degrades_slides_stay() -> None:
    """P-01 novlm: unspecified figure stays on scientific-figure; deck is research-pptx."""
    registry = _registry()
    figure, rejected = registry.resolve_capability("artifact.figure", request=_P_01)
    slides, _ = registry.resolve_capability("slides.generate", request=_P_01)
    assert figure is not None and figure.name == "scientific-figure"
    assert any(item.name == "livefigure" and "vlm_not_configured" in why for item, why in rejected)
    assert slides is not None and slides.name == "research-pptx"
    assert infer_figure_and_paper_outputs(_P_01) == ["artifact.figure", "draft.manuscript"]
    assert infer_slide_outputs(_P_01) == ["artifact.slides"]

    runtime = _runtime()
    filled = await host_fill_figure(
        runtime=runtime,
        registry=registry,
        task_id="p-01",
        session_id="s1",
        user_message=_P_01,
    )
    assert runtime.enqueue.await_args.args[0] == "scientific-figure"
    assert "Using scientific-figure instead" not in " ".join(
        str(line) for line in filled.get("observations") or []
    )


def test_a_pos_03_poster_continues_deterministic_only() -> None:
    """A-POS-03: missing VLM is not needs_input and does not swap the poster skill."""
    registry = _registry()
    selected, rejected = registry.resolve_capability("poster.scientific", request=_A_POS_01)
    assert selected is not None and selected.name == "scientific-poster"
    assert not any(item.name == "scientific-poster" for item, _ in rejected)
    admission = skill_admission_rejection(
        selected, services=registry.admission_services()
    )
    assert admission is None
    schema = (selected.output_schema or {}).get("properties") or {}
    mode = schema.get("visual_review_mode") or {}
    assert "deterministic-only" in (mode.get("enum") or [])


def test_a_rev_03_visual_is_partial_not_a_hard_stop() -> None:
    """A-REV-03: paper-review still admits; visual names vlm_not_configured."""
    registry = _registry()
    selected, _ = registry.resolve_capability("review.paper", request=_A_REV_01)
    assert selected is not None and selected.name == "paper-review"
    assert (
        skill_admission_rejection(selected, services=registry.admission_services())
        is None
    )
    notice = skip_observation(
        skipped="livefigure",
        fallback="",
        reason="vlm_not_configured",
    )
    assert "reason=vlm_not_configured" in notice
    assert "omni config vlm" in notice
    assert "Using scientific-figure instead" not in notice


def test_find_skill_novlm_card_exposes_fallback_not_a_silent_win() -> None:
    """Unavailable livefigure stays visible so the model sees the skip, not a swap."""
    registry = _registry()
    services = registry.admission_services()
    hits = rank_skill_matches(
        registry.list_selectable(), _A_FIG_01, services=services
    )
    assert hits[0].name == "scientific-figure"
    live = skill_contract_card(
        next(item for item in hits if item.name == "livefigure"),
        services=services,
    )
    assert live["availability"] == "unavailable"
    assert live["unavailable_reason"] == "vlm_not_configured"
    assert live["fallback"] == "scientific-figure"

    named = rank_skill_matches(
        registry.list_selectable(), "livefigure", services=services
    )
    assert named[0].name == "livefigure"
    card = skill_contract_card(named[0], services=services)
    assert card["availability"] == "unavailable"
    assert card["unavailable_reason"] == "vlm_not_configured"
