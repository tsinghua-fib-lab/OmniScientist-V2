"""Capability slice: true desk, model produce, refs home, honest interrupt."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType, ToolPolicy, VerificationPlan
from omni.agent.interaction_lifecycle import unblock_produce_tools
from omni.config import load_settings
from omni.core.tool_result import TOOL_NOT_STARTED, TOOL_OUTCOME_UNKNOWN, interrupted_tool_payload
from omni.runtime.engine_observation import collect_typed_refs
from omni.runtime.execution_policy import resources_for_tool
from omni.runtime.research_state import (
    TaskRef,
    TaskResearchState,
    refresh_system_research_brief,
)
from omni.runtime.task_recorder import _merge_task_ids
from omni.skills_runtime.registry import SkillRegistry


def test_merge_collects_singular_and_nested_source_ids() -> None:
    task = SimpleNamespace(source_ids=[], claim_ids=[], evidence_ids=[])
    _merge_task_ids(
        task,
        {
            "source_id": "s1",
            "claim_id": "c1",
            "results": [{"source_id": "s2", "title": "Paper"}],
            "observation": {"created_refs": ["source:s3", "evidence:e1"]},
        },
    )
    assert task.source_ids == ["s1", "s2", "s3"]
    assert task.claim_ids == ["c1"]
    assert task.evidence_ids == ["e1"]


def test_collect_typed_refs_walks_matches_and_created_refs() -> None:
    refs = collect_typed_refs(
        {
            "matches": [{"source_id": "src-a"}],
            "created_refs": ["claim:c-1"],
        }
    )
    assert "source:src-a" in refs
    assert "claim:c-1" in refs


def test_full_writing_debt_requires_claims() -> None:
    state = TaskResearchState(
        task_id="task-1",
        provenance_mode="full",
        required_outputs=["draft.manuscript"],
        missing_deliverables=["draft.manuscript"],
        sources=[TaskRef("source", "src-1", title="ITI")],
    )
    keys = {key for key, _ in state.debt_findings()}
    assert "full_writing_no_claims" in keys
    assert "empty_sources_for_writing" not in keys


def test_unmatched_tool_is_a_debt() -> None:
    state = TaskResearchState(
        task_id="task-1",
        unmatched_tools=["search_literature"],
    )
    keys = {key for key, message in state.debt_findings()}
    assert "unknown_outcome:search_literature" in keys
    assert "interrupted tools (outcome unknown)" in state.snapshot_text()


def test_refresh_system_research_brief_replaces_the_desk() -> None:
    system = (
        "You are Omni.\n"
        "[Task research state]\n"
        "task=old provenance=light\n"
        "sources (0): none\n"
        "[Memory]\n"
        "remembered fact\n"
    )
    refreshed = refresh_system_research_brief(
        system,
        "[Task research state]\ntask=new provenance=full\nsources (1): source:s1",
    )
    assert "task=new" in refreshed
    assert "task=old" not in refreshed
    assert "[Memory]" in refreshed
    assert "remembered fact" in refreshed


def test_interrupted_payload_is_honest_about_side_effects() -> None:
    started = interrupted_tool_payload("search_literature", started=True)
    assert started["error_code"] == TOOL_OUTCOME_UNKNOWN
    assert "Do not retry blindly" in started["error"]
    unread = interrupted_tool_payload("search_corpus", started=True)
    assert unread["error_code"] == TOOL_OUTCOME_UNKNOWN
    assert "read-only" in unread["error"]
    before = interrupted_tool_payload("write_file", started=False)
    assert before["error_code"] == TOOL_NOT_STARTED


def test_search_literature_serializes_on_the_store() -> None:
    keys = resources_for_tool("search_literature", {"query": "RAG"}, scope="/tmp/ws")
    assert keys == ["store:/tmp/ws"]
    assert resources_for_tool("search_corpus", {"query": "RAG"}, scope="/tmp/ws") == []


def test_unblock_produce_tools_when_a_manuscript_is_owed() -> None:
    policy = ToolPolicy(blocked_tools=["write_file", "edit_file", "bash"])
    plan = IntentPlan(
        task_id="t1",
        user_message="write a survey",
        intent_type=IntentType.REACT_FALLBACK,
        verification_plan=VerificationPlan(required_outputs=["draft.manuscript"]),
    )
    opened = unblock_produce_tools(policy, plan)
    assert "write_file" not in opened.blocked_tools
    assert "edit_file" not in opened.blocked_tools
    assert "bash" in opened.blocked_tools
    answer = IntentPlan(
        task_id="t2",
        user_message="what is RAG?",
        intent_type=IntentType.DIRECT_ANSWER,
        verification_plan=VerificationPlan(required_outputs=["answer"]),
    )
    assert unblock_produce_tools(policy, answer).blocked_tools == policy.blocked_tools


def test_react_discovery_hint_does_not_dump_the_catalog() -> None:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    hint = registry.react_skill_catalog()
    assert "find_skill" in hint
    assert "cite_source" in hint
    assert "scientific-figure" in hint
    assert "research-pptx" in hint
    assert "Skill contract catalog" not in hint
    assert '"properties"' not in hint


@pytest.mark.asyncio
async def test_escalate_creates_a_child_and_inherits_ledger() -> None:
    from omni.agent.orchestrator import OmniAgent

    parent = SimpleNamespace(
        id="parent-1",
        kind="turn",
        depth=0,
        source_ids=["s1"],
        claim_ids=[],
        evidence_ids=[],
        artifact_ids=[],
        plan_json={},
        provenance_mode="full",
        intent_type="",
    )
    child = SimpleNamespace(id="child-1")
    tasks = SimpleNamespace(
        get_task=AsyncMock(return_value=parent),
        create_task=AsyncMock(return_value=child),
        inherit_research_ledger=AsyncMock(),
        append_event=AsyncMock(),
    )
    agent = OmniAgent.__new__(OmniAgent)
    agent.tasks = tasks

    spawned: list[object] = []

    def fake_create_task(coro: object, **_kwargs: object) -> None:
        spawned.append(coro)
        getattr(coro, "close", lambda: None)()

    with patch("omni.agent.turn_escalate.asyncio.create_task", side_effect=fake_create_task):
        created = await agent._maybe_escalate(
            "Finish the survey", "sess", "cli", task_id="parent-1"
        )
    assert created == "child-1"
    tasks.create_task.assert_awaited()
    tasks.inherit_research_ledger.assert_awaited_with("child-1", parent)
    assert spawned


@pytest.mark.asyncio
async def test_nested_escalate_is_refused() -> None:
    from omni.agent.orchestrator import OmniAgent

    parent = SimpleNamespace(id="esc-1", kind="escalated", depth=1)
    agent = OmniAgent.__new__(OmniAgent)
    agent.tasks = SimpleNamespace(get_task=AsyncMock(return_value=parent))
    assert await agent._maybe_escalate("more", "s", "cli", task_id="esc-1") is None
