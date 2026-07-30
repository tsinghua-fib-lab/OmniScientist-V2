"""Model-first artifact editing + no-dead-end escalation (Route B).

The model is the sole intent router: it proposes the ``artifact.revise`` /
``artifact.figure`` capabilities and the runtime executes them (escalating a
minor edit to a full source-preserving redraw when the target cannot be
grounded, instead of returning ``artifact_revision_failed``). The only
deterministic pre-emption is an explicit signal (verbatim element name +
concrete colour). There is no keyword rule brain; offline (no model) the
non-explicit turns degrade to an honest placeholder.
"""

from __future__ import annotations

import pytest

from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.storage.models import SubtaskORM
from tests.conftest import PlanningLLM
from tests.conftest import install_fake_dot as _fake_dot

DOT = """
digraph G {
  graph [label="RAG Architecture"];
  subgraph cluster_phase2 {
    label="Phase II — Online Query Pipeline";
    color="#dc2626";
    bgcolor="#fef2f2";
    node [fillcolor="#fee2e2", color="#b91c1c", style="filled"];
    q [label="Query"];
    r [label="Reranker"];
  }
  q -> r;
}
"""

_REVISE_PLAN = {
    "intent_type": "single_skill_task",
    "confidence": 0.8,
    "required_capabilities": ["artifact.revise"],
    "outputs": ["artifact"],
    "rationale": "user wants to edit the attached figure in place",
}

_MINOR_REVISE_PLAN = {
    **_REVISE_PLAN,
    "capability_inputs": {
        "artifact.revise": {
            "target": "Phase II - Online Query Pipeline",
            "style": "blue",
            "scope": "element",
            "instruction": "Use a blue palette for the named pipeline phase.",
        }
    },
}

_FIGURE_PLAN = {
    "intent_type": "single_skill_task",
    "confidence": 0.8,
    "required_capabilities": ["artifact.figure"],
    "outputs": ["artifact"],
    "rationale": "structural redraw of the attached figure",
}

_ANSWER_PLAN = {
    "intent_type": "direct_answer",
    "confidence": 0.8,
    "outputs": ["answer"],
    "rationale": "answer a question about the active figure",
}


async def _agent_with_attached_figure(
    *,
    plans: list[dict] | None = None,
    external_key: str = "wx-artifact-model",
) -> tuple[OmniAgent, str, str]:
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = OmniAgent(settings)
    await agent.setup()
    if plans is not None:
        agent.llm = PlanningLLM(plans)
    session_id = await agent.ensure_session(channel="wechat", external_key=external_key)
    source = settings.paths.artifacts_dir / "figure" / "rag_model_routing.dot"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(DOT, encoding="utf-8")
    result = {"summary": "RAG Architecture", "artifacts": [{"title": "RAG DOT", "format": "dot", "path": str(source)}]}
    async with agent.db.session() as s:
        task = SubtaskORM(
            skill_name="scientific-figure",
            status="succeeded",
            session_id=session_id,
            result_json=result,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        subtask_id = task.id
    await agent.runtime._write_back_result(  # noqa: SLF001 - simulate task completion write-back.
        session_id, subtask_id, "scientific-figure", "RAG Architecture", result
    )
    return agent, session_id, subtask_id


@pytest.mark.asyncio
async def test_model_artifact_revise_escalates_to_major_when_target_unresolved(tmp_path, monkeypatch):
    """Model chooses artifact.revise; vague target → full redraw, never dead-end."""
    _fake_dot(tmp_path, monkeypatch)
    agent, session_id, subtask_id = await _agent_with_attached_figure(plans=[_REVISE_PLAN])
    try:
        turn = await agent.handle_turn(
            "生成的这个图还是不太准确，再次 review，重点部分可以用不同颜色标注清楚",
            session_id=session_id,
            channel="wechat",
            drain_tasks=False,
        )

        assert turn.terminated_reason == "major_revision_submitted"
        assert turn.terminated_reason != "artifact_revision_failed"
        assert turn.submitted_subtask_ids
        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 1  # the model routed this turn
        async with agent.db.session() as s:
            submitted = await s.get(SubtaskORM, turn.submitted_subtask_ids[0])
        assert submitted is not None
        assert submitted.input_json["source_task_id"] == subtask_id
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_offline_vague_revision_degrades_without_dead_end(tmp_path, monkeypatch):
    """The reported bug, offline: a vague edit on an attached (revised) figure
    used to fail with artifact_revision_failed. Under Route B there is no offline
    rule brain to guess it into a redraw, but it must still not dead-end — it
    degrades to an honest placeholder instead of an error."""
    _fake_dot(tmp_path, monkeypatch)
    agent, session_id, subtask_id = await _agent_with_attached_figure(external_key="wx-offline-vague")
    try:
        turn = await agent.handle_turn(
            "生成的这个图还是不太准确，再次 review，重点部分可以用不同颜色标注清楚",
            session_id=session_id,
            channel="wechat",
            drain_tasks=False,
        )

        assert turn.terminated_reason != "artifact_revision_failed"
        assert turn.kind != "error"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_concrete_edit_uses_structured_model_plan_and_applies_in_place(tmp_path, monkeypatch):
    """Natural-language edits are interpreted once by the semantic planner.

    The runtime then grounds and applies the structured edit deterministically;
    it does not carry language-specific edit rules.
    """
    _fake_dot(tmp_path, monkeypatch)
    agent, session_id, _ = await _agent_with_attached_figure(
        plans=[_MINOR_REVISE_PLAN], external_key="wx-explicit-fast"
    )
    try:
        turn = await agent.handle_turn(
            "把 Phase II - Online Query Pipeline 改成蓝色",
            session_id=session_id,
            channel="wechat",
        )

        assert turn.terminated_reason == "artifact_revision_done"
        assert "versioned artifact transaction" in turn.text
        assert ".svg" in turn.text and ".png" in turn.text
        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_major_hint_not_hijacked_by_keyword_classifier_when_model_present(tmp_path, monkeypatch):
    """A natural-language request for a more detailed engineering figure is inferred intent;
    with a model present it must
    reach the model (one brain), never a keyword pre-classifier."""
    _fake_dot(tmp_path, monkeypatch)
    agent, session_id, _ = await _agent_with_attached_figure(
        plans=[_FIGURE_PLAN], external_key="wx-major-model"
    )
    try:
        turn = await agent.handle_turn(
            "这张图过于简单，请从工程角度上做优化",
            session_id=session_id,
            channel="wechat",
            drain_tasks=False,
        )

        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 1  # the model routed, not the _MAJOR_HINTS list
        assert turn.kind != "error"
        assert turn.terminated_reason != "artifact_revision_failed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_figure_question_goes_to_model_not_deterministic_qa_when_model_present(tmp_path, monkeypatch):
    """A question about the active figure must not be answered by the
    deterministic keyword QA path when a model is available."""
    _fake_dot(tmp_path, monkeypatch)
    agent, session_id, _ = await _agent_with_attached_figure(
        plans=[_ANSWER_PLAN], external_key="wx-question-model"
    )
    try:
        turn = await agent.handle_turn(
            "上面生成的这个图主要讲了什么？",
            session_id=session_id,
            channel="wechat",
        )

        assert turn.kind != "error"
        assert turn.terminated_reason != "artifact_qa_done"  # deterministic QA path skipped
        events = await agent.tasks.list_events(turn.task_id)
        assert not any(event.event_type == "artifact.qa" for event in events)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_model_artifact_revise_without_active_figure_does_not_dead_end(tmp_path, monkeypatch):
    """artifact.revise with no active figure degrades to the capable assistant."""
    _fake_dot(tmp_path, monkeypatch)
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = OmniAgent(settings)
    await agent.setup()
    agent.llm = PlanningLLM([_REVISE_PLAN])
    session_id = await agent.ensure_session(channel="cli")
    try:
        turn = await agent.handle_turn(
            "把这张图的重点部分标成不同颜色",
            session_id=session_id,
            channel="cli",
        )

        assert turn.terminated_reason != "artifact_revision_failed"
        assert turn.kind != "error"
    finally:
        await agent.aclose()
