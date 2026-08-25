"""Task research state is this-task facts, not a workspace brief or host planner."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omni.runtime.engine_observation import (
    SCHEMA,
    attach_engine_observation,
    build_engine_observation,
    collect_typed_refs,
)
from omni.runtime.research_state import (
    LiveTaskResearchFeed,
    TaskRef,
    TaskResearchState,
    load_task_research_state,
)


def test_engine_observation_lifts_funnel_and_refs() -> None:
    result = {
        "status": "ok",
        "query": "activation steering",
        "n_kept": 2,
        "source_ids": ["src-1"],
        "results": [{"title": "ITI", "arxiv_id": "2306.03341"}],
    }
    observation = build_engine_observation(
        result, extra={"skill_name": "openalex-search", "status": "succeeded"}
    )
    assert observation["schema"] == SCHEMA
    assert observation["role"] == "retrieve"
    assert observation["metrics"]["n_kept"] == 2
    assert "source:src-1" in observation["created_refs"]
    assert "task complete" not in observation["summary"].lower()


def test_empty_funnel_envelope_is_degraded_with_a_limitation() -> None:
    result = {"status": "empty", "query": "latent space", "count": 0, "results": []}
    wrapped = attach_engine_observation(
        {"status": "succeeded", "skill_name": "search_literature"},
        result,
        extra={"skill_name": "search_literature"},
    )
    observation = wrapped["observation"]
    assert observation["status"] == "degraded"
    assert any("0 sources" in item for item in observation["limitations"])
    assert "source_ids" not in wrapped


def test_figure_result_is_produce_not_a_funnel() -> None:
    result = {
        "status": "ok",
        "artifacts": [{"id": "art-1", "format": "png"}],
        "artifact_ids": ["art-1"],
    }
    observation = build_engine_observation(
        result, extra={"skill_name": "scientific-figure"}
    )
    assert observation["role"] == "produce"
    assert collect_typed_refs(result) == ["artifact:art-1"]


def test_snapshot_skips_empty_light_answer_tasks() -> None:
    state = TaskResearchState(task_id="task-1", provenance_mode="light")
    assert state.is_empty()
    assert state.snapshot_text() == ""


def test_snapshot_projects_recommended_next_actions() -> None:
    state = TaskResearchState(
        task_id="task-aaaa",
        sources=[TaskRef("source", "src-1", title="ITI")],
        latest_observation={
            "summary": "literature n_kept=0",
            "limitations": ["literature funnel kept 0 sources"],
            "metrics": {"n_kept": 0},
            "recommended_next_actions": ["retry retrieval with a different query"],
        },
    )
    text = state.snapshot_text()
    assert "suggested next: retry retrieval with a different query" in text
    assert "Stop or ask the user is a legal next move" in text
    assert "do not expand tools or debts" in text


def test_snapshot_is_task_scoped_and_hash_stable() -> None:
    state = TaskResearchState(
        task_id="task-aaaa",
        provenance_mode="full",
        required_outputs=["draft.manuscript"],
        sources=[TaskRef("source", "src-1", title="ITI")],
        missing_deliverables=["draft.manuscript"],
        unsupported_claims=1,
        resumed=True,
    )
    text = state.snapshot_text()
    assert "task=task-aaa" in text
    assert "source:src-1 ITI" in text
    assert "draft.manuscript" in text
    assert "another task" in text
    assert "find_skill" in text
    assert state.state_hash == state.state_hash


def test_delta_is_empty_when_hash_is_unchanged() -> None:
    state = TaskResearchState(
        task_id="t1",
        sources=[TaskRef("source", "s1")],
        missing_deliverables=["artifact.figure"],
    )
    assert state.delta_against(state) == ""
    later = TaskResearchState(
        task_id="t1",
        sources=[TaskRef("source", "s1"), TaskRef("source", "s2", title="New")],
        missing_deliverables=["artifact.figure"],
    )
    delta = later.delta_against(state)
    assert "+ source:s2 New" in delta
    assert "missing deliverables" not in delta


def test_debt_findings_are_bookkeeping_not_quality() -> None:
    state = TaskResearchState(
        task_id="t1",
        provenance_mode="full",
        missing_deliverables=["artifact.figure"],
        unsupported_claims=2,
    )
    keys = {key for key, _ in state.debt_findings()}
    assert "missing_deliverable:artifact.figure" in keys
    assert "unsupported_claims" in keys
    for _key, message in state.debt_findings():
        assert "complete" not in message.lower() or "enough" not in message.lower()


def test_light_answer_has_no_rom_debt() -> None:
    state = TaskResearchState(task_id="t1", provenance_mode="light")
    assert state.debt_findings() == []


@pytest.mark.asyncio
async def test_load_state_uses_this_task_ids_only() -> None:
    task = SimpleNamespace(
        id="task-1",
        plan_json={
            "outputs": ["answer", "artifact.figure"],
            "verification_plan": {"required_outputs": ["artifact.figure"]},
            "provenance_mode": "light",
        },
        provenance_mode="light",
        current_authority_fingerprint="fp",
        source_ids=["src-mine"],
        claim_ids=[],
        evidence_ids=[],
        artifact_ids=[],
        submitted_subtask_ids=[],
        submitted_workflow_ids=[],
    )
    figure = SimpleNamespace(id="art-1", title="Loop", kind="figure", mime="image/png")
    tasks = SimpleNamespace(get_task=AsyncMock(return_value=task))
    artifacts = SimpleNamespace(list_by_task=AsyncMock(return_value=[figure]))

    state = await load_task_research_state(tasks, artifacts, None, "task-1")
    assert state is not None
    assert state.sources[0].id == "src-mine"
    assert state.missing_deliverables == []
    assert "src-other" not in state.snapshot_text()


@pytest.mark.asyncio
async def test_feed_replays_missing_deliverable_once() -> None:
    task = SimpleNamespace(
        id="task-1",
        plan_json={"verification_plan": {"required_outputs": ["draft.manuscript"]}},
        provenance_mode="light",
        current_authority_fingerprint="",
        source_ids=[],
        claim_ids=[],
        evidence_ids=[],
        artifact_ids=[],
        submitted_subtask_ids=[],
        submitted_workflow_ids=[],
    )
    tasks = SimpleNamespace(
        get_task=AsyncMock(return_value=task),
        list_events=AsyncMock(return_value=[]),
        list_tasks_for_session=AsyncMock(return_value=[]),
    )
    artifacts = SimpleNamespace(list_by_task=AsyncMock(return_value=[]))
    feed = LiveTaskResearchFeed(
        tasks=tasks, artifacts=artifacts, db=None, task_id="task-1"
    )
    first = await feed.before_text_finish()
    second = await feed.before_text_finish()
    assert "still owes draft.manuscript" in first
    assert second == ""


@pytest.mark.asyncio
async def test_feed_replays_each_debt_key_once() -> None:
    task = SimpleNamespace(
        id="task-1",
        plan_json={},
        provenance_mode="full",
        current_authority_fingerprint="",
        source_ids=[],
        claim_ids=[],
        evidence_ids=[],
        artifact_ids=[],
        submitted_subtask_ids=[],
        submitted_workflow_ids=[],
    )
    tasks = SimpleNamespace(
        get_task=AsyncMock(return_value=task),
        list_events=AsyncMock(return_value=[]),
        list_tasks_for_session=AsyncMock(return_value=[]),
    )
    artifacts = SimpleNamespace(list_by_task=AsyncMock(return_value=[]))
    feed = LiveTaskResearchFeed(
        tasks=tasks, artifacts=artifacts, db=None, task_id="task-1"
    )
    first = await feed.before_text_finish()
    second = await feed.before_text_finish()
    assert "full provenance" in first.lower() or "no claims" in first
    assert second == "" or first != second
    third = await feed.before_text_finish()
    assert third == ""


@pytest.mark.asyncio
async def test_unchanged_state_does_not_inject_a_delta() -> None:
    task = SimpleNamespace(
        id="task-1",
        plan_json={"verification_plan": {"required_outputs": ["artifact.figure"]}},
        provenance_mode="light",
        current_authority_fingerprint="",
        source_ids=[],
        claim_ids=[],
        evidence_ids=[],
        artifact_ids=[],
        submitted_subtask_ids=[],
        submitted_workflow_ids=[],
    )
    tasks = SimpleNamespace(get_task=AsyncMock(return_value=task))
    artifacts = SimpleNamespace(list_by_task=AsyncMock(return_value=[]))
    feed = LiveTaskResearchFeed(
        tasks=tasks, artifacts=artifacts, db=None, task_id="task-1"
    )
    opening = await feed.opening_snapshot()
    assert "artifact.figure" in opening
    assert await feed.after_tool_batch() == ""


@pytest.mark.asyncio
async def test_inherited_artifact_ids_count_for_this_task() -> None:
    task = SimpleNamespace(
        id="retry-1",
        plan_json={"verification_plan": {"required_outputs": ["artifact.figure"]}},
        provenance_mode="light",
        current_authority_fingerprint="",
        source_ids=["src-1"],
        claim_ids=[],
        evidence_ids=[],
        artifact_ids=["art-parent"],
        submitted_subtask_ids=[],
        submitted_workflow_ids=[],
        retry_of_task_id="orig-1",
        status="running",
    )
    figure = SimpleNamespace(id="art-parent", title="RAG", kind="figure", mime="image/png")
    tasks = SimpleNamespace(get_task=AsyncMock(return_value=task))
    artifacts = SimpleNamespace(
        list_by_task=AsyncMock(return_value=[]),
        get=AsyncMock(return_value=figure),
    )
    state = await load_task_research_state(tasks, artifacts, None, "retry-1")
    assert state is not None
    assert state.missing_deliverables == []
    assert state.artifacts[0].id == "art-parent"


@pytest.mark.asyncio
async def test_snapshot_includes_empty_funnel_limitation() -> None:
    task = SimpleNamespace(
        id="task-1",
        plan_json={"verification_plan": {"required_outputs": ["draft.manuscript"]}},
        provenance_mode="light",
        current_authority_fingerprint="",
        source_ids=[],
        claim_ids=[],
        evidence_ids=[],
        artifact_ids=[],
        submitted_subtask_ids=[],
        submitted_workflow_ids=[],
        retry_of_task_id="",
        status="cancelled",
    )
    observation = {
        "schema": SCHEMA,
        "status": "degraded",
        "summary": "literature n_kept=0",
        "created_refs": [],
        "limitations": ["literature funnel kept 0 sources"],
        "metrics": {"n_kept": 0},
    }
    event = SimpleNamespace(
        output_json={"observation": observation},
        event_type="react.tool.done",
    )
    tasks = SimpleNamespace(
        get_task=AsyncMock(return_value=task),
        list_events=AsyncMock(return_value=[event]),
    )
    artifacts = SimpleNamespace(list_by_task=AsyncMock(return_value=[]))
    state = await load_task_research_state(tasks, artifacts, None, "task-1", resumed=True)
    assert state is not None
    assert state.empty_funnel
    text = state.snapshot_text()
    assert "n_kept: 0" in text
    assert "0 sources" in text
    assert "find_skill" in text
    keys = {key for key, _ in state.debt_findings()}
    assert "empty_sources_for_writing" in keys
