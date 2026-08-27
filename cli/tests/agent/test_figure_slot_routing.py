"""Figure slots: explicit name, admission, default scientific-figure."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omni.agent.figure_runner import _failure_reason_code, host_fill_figure
from omni.agent.intent_plan import IntentPlan, IntentType, VerificationPlan
from omni.agent.skill_lookup import FIND_SKILL_NEXT_ACTION, rank_skill_matches, skill_contract_card
from omni.agent.turn_execution import TurnCompletion
from omni.config import load_settings
from omni.core.react_agent import AgentLoopResult
from omni.skills_runtime.registry import SkillRegistry
from omni.skills_runtime.slot_routing import (
    allow_slot_fallback,
    skip_observation,
    user_named_skill,
)


def _vlm(*, available: bool) -> SimpleNamespace:
    return SimpleNamespace(
        available=available,
        setup_command="omni config vlm",
        error_code="" if available else "vlm_not_configured",
        missing=() if available else ("model", "endpoint", "api_key"),
    )


def _registry(*, vlm: bool | None = None) -> SkillRegistry:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    if vlm is not None:
        registry.use_admission_services({"vlm": _vlm(available=vlm)})
    return registry


def test_explicit_catalog_name_is_not_a_hint_list() -> None:
    text = "生成 query/retriever/reranker/LLM 科研架构图"
    assert not user_named_skill(text, "livefigure")
    assert not user_named_skill(text, "scientific-figure")
    assert user_named_skill("用 livefigure 做可编辑图", "livefigure")
    assert user_named_skill("$scientific-figure 画架构图", "scientific-figure")
    assert not user_named_skill("one editable PPTX figure", "livefigure")
    assert not user_named_skill("只要 svg", "scientific-figure")
    assert not user_named_skill("用 Graphviz 画一张 SVG", "scientific-figure")
    assert not allow_slot_fallback(
        preferred="livefigure",
        slot="artifact.figure",
        user_message="用 livefigure 画架构图",
    )
    assert allow_slot_fallback(
        preferred="livefigure",
        slot="artifact.figure",
        user_message=text,
    )
    assert not allow_slot_fallback(
        preferred="livefigure",
        slot="figure.editable.pptx",
        user_message=text,
    )


def test_resolve_unspecified_figure_is_scientific_figure_even_when_vlm_up() -> None:
    off = _registry(vlm=False)
    selected, rejected = off.resolve_capability(
        "artifact.figure", request="生成科研架构图"
    )
    assert selected is not None and selected.name == "scientific-figure"
    assert any("livefigure" in item.name and "vlm_not_configured" in why for item, why in rejected)

    on = _registry(vlm=True)
    selected, _ = on.resolve_capability("artifact.figure", request="生成科研架构图")
    assert selected is not None and selected.name == "scientific-figure"


def test_graphviz_words_do_not_rerank_resolve_capability() -> None:
    """Host resolve is admission + priority. Graphviz words are the model's job."""
    registry = _registry(vlm=True)
    selected, _ = registry.resolve_capability(
        "artifact.figure", request="用 Graphviz 只要 SVG"
    )
    assert selected is not None and selected.name == "scientific-figure"


def test_named_editable_figure_does_not_fall_back_when_vlm_missing() -> None:
    registry = _registry(vlm=False)
    selected, rejected = registry.resolve_capability(
        "figure.editable.pptx", request="用 livefigure 做可编辑 PPTX"
    )
    assert selected is None
    assert any(item.name == "livefigure" for item, _ in rejected)


def test_find_skill_ranks_generic_architecture_to_scientific_figure() -> None:
    on = _registry(vlm=True)
    services = on.admission_services()
    hits = rank_skill_matches(
        on.list_selectable(),
        "architecture diagram 架构图",
        services=services,
    )
    assert hits[0].name == "scientific-figure"

    off = _registry(vlm=False)
    services = off.admission_services()
    hits = rank_skill_matches(
        off.list_selectable(),
        "architecture diagram 架构图",
        services=services,
    )
    assert hits[0].name == "scientific-figure"
    live = next(
        card
        for card in (skill_contract_card(e, services=services) for e in hits)
        if card["name"] == "livefigure"
    )
    assert live["availability"] == "unavailable"
    assert live["fallback"] == "scientific-figure"
    assert live["unavailable_reason"] == "vlm_not_configured"


def test_find_skill_named_scientific_figure_still_wins() -> None:
    registry = _registry(vlm=True)
    hits = rank_skill_matches(
        registry.list_selectable(),
        "scientific-figure generate architecture diagram DOT SVG PNG",
        services=registry.admission_services(),
    )
    assert hits[0].name == "scientific-figure"


def test_graphviz_words_do_not_force_a_hint_rerank() -> None:
    """Host ranker does not add a Graphviz/SVG/PNG bonus; exact name still wins."""
    registry = _registry(vlm=True)
    services = registry.admission_services()
    hits = rank_skill_matches(
        registry.list_selectable(),
        "graphviz SVG PNG source_artifact_dot",
        services=services,
    )
    names = [item.name for item in hits]
    assert "scientific-figure" in names
    named = rank_skill_matches(
        registry.list_selectable(),
        "scientific-figure",
        services=services,
    )
    assert named[0].name == "scientific-figure"


def test_figure_skill_copy_names_default_and_editable_only() -> None:
    registry = _registry(vlm=True)
    live = next(item for item in registry.list_selectable() if item.name == "livefigure")
    sci = next(item for item in registry.list_selectable() if item.name == "scientific-figure")
    services = registry.admission_services()
    live_card = skill_contract_card(live, services=services)
    sci_card = skill_contract_card(sci, services=services)
    live_text = (
        f"{live.description} {live.when_to_use} {live_card.get('when_not_to_use', '')}"
    ).casefold()
    sci_text = (
        f"{sci.description} {sci.when_to_use} {sci_card.get('when_not_to_use', '')}"
    ).casefold()
    assert "first-choice" not in live_text
    assert "preferred first" not in live_text
    assert "editable" in live_text and "pptx" in live_text
    assert "scientific-figure" in live_text
    assert "default" in sci_text
    assert live_card["next_action"] == FIND_SKILL_NEXT_ACTION
    assert "call run_skill now" in FIND_SKILL_NEXT_ACTION.casefold()
    assert not live_card.get("instructions")


def test_skip_observation_names_reason_and_setup() -> None:
    text = skip_observation(
        skipped="livefigure",
        fallback="scientific-figure",
        reason="vlm_not_configured",
    )
    assert "reason=vlm_not_configured" in text
    assert "omni config vlm" in text
    assert "scientific-figure" in text


def test_livefigure_permission_error_is_sandbox_write_denied() -> None:
    code = _failure_reason_code(
        {
            "error": "PPTX generation failed",
            "result": {
                "status": "error",
                "summary": "PermissionError writing livefigure.pptx",
                "error": "Operation not permitted",
            },
        }
    )
    assert code == "livefigure_sandbox_write_denied"


@pytest.mark.asyncio
async def test_host_fill_unspecified_uses_scientific_figure_when_vlm_up() -> None:
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-sci"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(status="succeeded", error="", result_json={"ok": True})
        ),
    )
    filled = await host_fill_figure(
        runtime=runtime,
        registry=_registry(vlm=True),
        task_id="t1",
        session_id="s1",
        user_message="生成 query/retriever/reranker/LLM 科研架构图",
        title="RAG",
    )
    assert runtime.enqueue.await_args.args[0] == "scientific-figure"
    assert "source_artifact_path" not in runtime.enqueue.await_args.args[1]
    assert filled["skill"] == "scientific-figure"
    assert filled.get("observations") == []


@pytest.mark.asyncio
async def test_host_fill_unspecified_stays_on_scientific_figure_when_vlm_down() -> None:
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-sci"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(status="succeeded", error="", result_json={"ok": True})
        ),
    )
    filled = await host_fill_figure(
        runtime=runtime,
        registry=_registry(vlm=False),
        task_id="t1",
        session_id="s1",
        user_message="生成 query/retriever/reranker/LLM 科研架构图",
    )
    assert runtime.enqueue.await_count == 1
    assert runtime.enqueue.await_args.args[0] == "scientific-figure"
    assert filled["skill"] == "scientific-figure"
    observed = " ".join(str(line) for line in filled.get("observations") or [])
    assert "Using scientific-figure instead" not in observed


@pytest.mark.asyncio
async def test_host_fill_named_livefigure_does_not_degrade() -> None:
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
                    "action_required": {"kind": "configure", "service": "vlm", "command": "omni config vlm"},
                    "setup_command": "omni config vlm",
                },
            )
        ),
    )
    filled = await host_fill_figure(
        runtime=runtime,
        registry=_registry(vlm=False),
        task_id="t1",
        session_id="s1",
        user_message="用 livefigure 做一张可编辑架构图",
    )
    names = [call.args[0] for call in runtime.enqueue.await_args_list]
    assert names == ["livefigure"]
    assert filled["skill"] == "livefigure"


@pytest.mark.asyncio
async def test_host_fill_engine_fail_does_not_retry_sibling() -> None:
    calls: list[str] = []

    async def enqueue(skill: str, *_args: Any, **_kwargs: Any) -> str:
        calls.append(skill)
        return f"sub-{skill}"

    runtime = SimpleNamespace(
        enqueue=AsyncMock(side_effect=enqueue),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(
                status="failed",
                error="engine crashed",
                result_json={"status": "error", "error": "engine crashed"},
            )
        ),
    )
    filled = await host_fill_figure(
        runtime=runtime,
        registry=_registry(vlm=True),
        task_id="t1",
        session_id="s1",
        user_message="生成科研架构图",
    )
    assert calls == ["scientific-figure"]
    assert filled["skill"] == "scientific-figure"
    assert filled.get("status") != "succeeded"

    calls.clear()
    named = await host_fill_figure(
        runtime=runtime,
        registry=_registry(vlm=True),
        task_id="t1",
        session_id="s1",
        user_message="$livefigure 做可编辑图",
        explicit_skill="livefigure",
    )
    assert calls == ["livefigure"]
    assert named["skill"] == "livefigure"


@pytest.mark.asyncio
async def test_host_fill_already_failed_does_not_switch() -> None:
    runtime = SimpleNamespace(enqueue=AsyncMock())
    filled = await host_fill_figure(
        runtime=runtime,
        registry=_registry(vlm=True),
        task_id="t1",
        session_id="s1",
        user_message="生成科研架构图",
        prior_failed=["scientific-figure"],
    )
    runtime.enqueue.assert_not_called()
    assert filled["status"] == "blocked"
    assert filled["reason"] == "already_failed"


@pytest.mark.asyncio
async def test_host_fill_ignores_model_invented_dot_for_unspecified_figure() -> None:
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-1"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(status="succeeded", error="", result_json={})
        ),
    )
    await host_fill_figure(
        runtime=runtime,
        registry=_registry(vlm=True),
        task_id="t1",
        session_id="s1",
        user_message="生成科研架构图",
        source_artifact_path="figures/rag-architecture.dot",
    )
    params = runtime.enqueue.await_args.args[1]
    assert runtime.enqueue.await_args.args[0] == "scientific-figure"
    assert "source_artifact_path" not in params


@pytest.mark.asyncio
async def test_host_fill_passes_dot_only_when_scientific_figure_is_explicit() -> None:
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-1"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(status="succeeded", error="", result_json={})
        ),
    )
    await host_fill_figure(
        runtime=runtime,
        registry=_registry(vlm=True),
        task_id="t1",
        session_id="s1",
        user_message="用 Graphviz 渲染 rag-architecture.dot",
        source_artifact_path="figures/rag-architecture.dot",
    )
    assert "source_artifact_path" not in runtime.enqueue.await_args.args[1]

    await host_fill_figure(
        runtime=runtime,
        registry=_registry(vlm=True),
        task_id="t1",
        session_id="s1",
        user_message="$scientific-figure 渲染 rag-architecture.dot",
        source_artifact_path="figures/rag-architecture.dot",
        explicit_skill="scientific-figure",
        pass_source=True,
    )
    assert runtime.enqueue.await_args.args[0] == "scientific-figure"
    assert runtime.enqueue.await_args.args[1]["source_artifact_path"] == "figures/rag-architecture.dot"


@pytest.mark.asyncio
async def test_turn_fill_unspecified_figure_without_dot_uses_scientific_figure() -> None:
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-fill"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(status="succeeded", error="", result_json={"ok": True})
        ),
    )
    completion = TurnCompletion(
        tasks=SimpleNamespace(),
        task_controller=SimpleNamespace(),
        hooks=SimpleNamespace(),
        runtime=runtime,
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[])),
        llm=object(),
        registry=_registry(vlm=True),
    )
    plan = IntentPlan(
        task_id="eda313e1" + "0" * 24,
        user_message="生成 query/retriever/reranker/LLM 科研架构图",
        intent_type=IntentType.REACT_FALLBACK,
        verification_plan=VerificationPlan(required_outputs=["artifact.figure"]),
    )
    notes = await completion._fill_remaining_figure(
        plan,
        AgentLoopResult(kind="text", content="done"),
        [],
        task_id=plan.task_id,
        session_id="s1",
    )
    runtime.enqueue.assert_awaited_once()
    assert runtime.enqueue.await_args.args[0] == "scientific-figure"
    assert any("scientific-figure" in note for note in notes)
