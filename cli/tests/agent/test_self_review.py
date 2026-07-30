"""In-execution self-review (P1): the main loop judges + revises before returning."""

from __future__ import annotations

import json

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.core.react_agent import AgentLoopResult


class _ScriptedLLM:
    """Returns scripted reviewer verdicts; empty for anything else."""

    def __init__(self, verdicts: list[dict]) -> None:
        self.model = "scripted"
        self._verdicts = list(verdicts)

    async def chat(self, system: str, user: str, *, temperature: float = 0.3) -> str:
        if '"verdict"' in system and self._verdicts:
            return json.dumps(self._verdicts.pop(0), ensure_ascii=False)
        return ""


class _FakeReact:
    """A react double whose revision runs yield successive contents."""

    def __init__(self, revised_contents: list[str]) -> None:
        self._contents = list(revised_contents)
        self.runs = 0

    async def run(self, **kwargs) -> AgentLoopResult:  # noqa: ANN003
        self.runs += 1
        content = self._contents.pop(0) if self._contents else "revised."
        return AgentLoopResult(kind="text", content=content)


async def _agent_and_run(verdicts: list[dict], *, max_revises: int = 1):
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.react.self_review = True
    settings.react.self_review_min_score = 0.6
    settings.react.self_review_max_revises = max_revises
    agent = await OmniAgent.create(settings)
    agent.llm = _ScriptedLLM(verdicts)
    session_id = await agent.ensure_session(channel="cli", external_key="sr-1")
    run = await agent.tasks.create_task(session_id=session_id, channel="cli", user_input="q")
    return agent, run.id


async def _last_self_review(agent, task_id: str) -> dict:
    events = await agent.tasks.list_events(task_id)
    reviews = [e for e in events if e.event_type == "self_review"]
    assert reviews, "expected a self_review event"
    return reviews[-1].output_json or {}


@pytest.mark.asyncio
async def test_weak_answer_is_revised_then_accepted():
    agent, task_id = await _agent_and_run(
        [{"verdict": "revise", "score": 0.3, "notes": "补出处"},
         {"verdict": "pass", "score": 0.9}]
    )
    try:
        react = _FakeReact(["a better, sourced answer."])
        first = AgentLoopResult(kind="text", content="first draft.")
        out = await agent._self_review_correct(
            react=react, result=first, system="sys", user_message="q",
            tool_specs=[], history=[], task_id=task_id,
        )
        assert react.runs == 1                      # one bounded revision
        assert out.content == "a better, sourced answer."
        rec = await _last_self_review(agent, task_id)
        assert rec["revises"] == 1 and rec["action"] == "accept"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_clean_answer_passes_without_revision():
    agent, task_id = await _agent_and_run([{"verdict": "pass", "score": 0.95}])
    try:
        react = _FakeReact([])
        first = AgentLoopResult(kind="text", content="already good.")
        out = await agent._self_review_correct(
            react=react, result=first, system="sys", user_message="q",
            tool_specs=[], history=[], task_id=task_id,
        )
        assert react.runs == 0                      # no revision needed
        assert out.content == "already good."
        rec = await _last_self_review(agent, task_id)
        assert rec["revises"] == 0 and rec["action"] == "accept"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_persistently_weak_answer_is_bounded():
    agent, task_id = await _agent_and_run(
        [{"verdict": "revise", "score": 0.2}, {"verdict": "revise", "score": 0.2},
         {"verdict": "revise", "score": 0.2}],
        max_revises=1,
    )
    try:
        react = _FakeReact(["still weak.", "still weak."])
        first = AgentLoopResult(kind="text", content="draft.")
        await agent._self_review_correct(
            react=react, result=first, system="sys", user_message="q",
            tool_specs=[], history=[], task_id=task_id,
        )
        assert react.runs == 1                      # capped at max_revises, no infinite loop
        rec = await _last_self_review(agent, task_id)
        assert rec["revises"] == 1 and rec["action"] != "accept"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_exhausted_usage_budget_skips_post_loop_review():
    agent, task_id = await _agent_and_run(
        [{"verdict": "revise", "score": 0.2, "notes": "spend more"}]
    )
    try:
        react = _FakeReact(["should not run."])
        first = AgentLoopResult(
            kind="text",
            content="bounded answer.",
            usage_budget={
                "max_total_tokens": 100,
                "max_cost_usd": 0.0,
                "total_tokens": 100,
                "cost_usd": 0.0,
                "enforced": True,
            },
        )

        out = await agent._self_review_correct(
            react=react,
            result=first,
            system="sys",
            user_message="q",
            tool_specs=[],
            history=[],
            task_id=task_id,
        )

        assert react.runs == 0
        assert out.content == "bounded answer."
        assert len(agent.llm._verdicts) == 1  # type: ignore[attr-defined]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_disabled_self_review_is_a_noop():
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.react.self_review = False
    agent = await OmniAgent.create(settings)
    agent.llm = _ScriptedLLM([{"verdict": "revise", "score": 0.1}])
    try:
        session_id = await agent.ensure_session(channel="cli", external_key="sr-2")
        run = await agent.tasks.create_task(session_id=session_id, channel="cli", user_input="q")
        react = _FakeReact(["should not run."])
        first = AgentLoopResult(kind="text", content="draft.")
        out = await agent._self_review_correct(
            react=react, result=first, system="sys", user_message="q",
            tool_specs=[], history=[], task_id=run.id,
        )
        assert react.runs == 0 and out.content == "draft."
        events = await agent.tasks.list_events(run.id)
        assert not [e for e in events if e.event_type == "self_review"]
    finally:
        await agent.aclose()
