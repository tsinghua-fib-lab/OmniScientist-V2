"""Concrete tool contracts remain mandatory inside native plan runners."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omni.agent.figure_runner import ArtifactFigureRunner
from omni.agent.intent_plan import IntentPlan, IntentType, SkillSelection
from omni.config import load_settings
from omni.core.react_agent import ToolSpec
from omni.skills_runtime.context import ExecContext, Tool


@pytest.mark.asyncio
async def test_figure_runner_stops_on_malformed_search_output(tmp_path) -> None:
    handler_calls = 0

    async def malformed_search(_args: dict) -> dict:
        nonlocal handler_calls
        handler_calls += 1
        return {"matches": "not-an-array"}

    search = Tool(
        ToolSpec(
            "search_corpus",
            "search",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer"},
                },
                "required": ["query", "k"],
                "additionalProperties": False,
            },
        ),
        malformed_search,
        output_schema={
            "type": "object",
            "properties": {"matches": {"type": "array"}},
            "required": ["matches"],
            "additionalProperties": False,
        },
    )
    plan = IntentPlan(
        task_id="task-1",
        user_message="Explain RAG and draw it",
        intent_type=IntentType.QA_PLUS_ARTIFACT,
        selected_skills=[
            SkillSelection(
                skill="scientific-figure",
                reason="figure provider",
                matched_capabilities=["artifact.figure"],
            )
        ],
        provider_inputs={"scientific-figure": {"input": "draw RAG"}},
    )

    class Runtime:
        async def enqueue(self, *_args, **_kwargs):
            raise AssertionError("figure execution must not start after malformed evidence")

    ctx = ExecContext(
        settings=load_settings(cwd=tmp_path),
        paths=load_settings(cwd=tmp_path).paths,
        task_id="task-1",
    )
    result = await ArtifactFigureRunner().run(
        plan,
        ctx=ctx,
        tools=[search],
        runtime=Runtime(),
        tasks=SimpleNamespace(),
        registry=SimpleNamespace(),
        drain_tasks=False,
    )

    assert handler_calls == 1
    assert result.kind == "error"
    assert result.terminated_reason == "search_output_contract_violation"
    assert result.submitted_subtask_ids == []
    assert result.tool_trace[0].result["reason"] == "output_contract_violation"
    assert result.tool_trace[0].result["execution_started"] is True
