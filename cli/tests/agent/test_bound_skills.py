"""Contract-bound skill cards are injected from required_outputs, not find_skill."""

from __future__ import annotations

import pytest

from omni.agent.bound_skills import (
    render_bound_skill_block,
    resolve_bound_skills,
)
from omni.agent.intent_plan import IntentPlan, IntentType, SkillSelection, VerificationPlan
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolInvocationRecord, ToolSpec
from omni.core.scientific_progress import leftover_skill_pressure
from omni.skills_runtime.registry import SkillRegistry
from tests.conftest import ScriptedLLM


def _registry() -> SkillRegistry:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return registry


def _plan(message: str, *outputs: str, skills: list[str] | None = None) -> IntentPlan:
    selected = [
        SkillSelection(skill=name, reason="test") for name in (skills or [])
    ]
    return IntentPlan(
        task_id="bound-skill",
        user_message=message,
        intent_type=IntentType.REACT_FALLBACK,
        outputs=list(outputs),
        selected_skills=selected,
        verification_plan=VerificationPlan(required_outputs=list(outputs)),
    )


def test_figure_debt_injects_scientific_figure_without_find_skill() -> None:
    bindings = resolve_bound_skills(
        _plan("draw a RAG architecture", "artifact.figure"),
        _registry(),
    )
    names = [item.skill for item in bindings]
    assert "scientific-figure" in names
    block = render_bound_skill_block(bindings)
    assert "[Bound skills]" in block
    assert "scientific-figure" in block
    assert "run_skill" in block
    assert "find_skill the same name" in block


def test_slides_debt_injects_research_pptx() -> None:
    bindings = resolve_bound_skills(
        _plan("make a seminar deck on RAG", "artifact.slides"),
        _registry(),
    )
    assert any(item.skill == "research-pptx" for item in bindings)


def test_named_livefigure_stays_on_livefigure() -> None:
    bindings = resolve_bound_skills(
        _plan("$livefigure one editable architecture slide", "artifact.figure"),
        _registry(),
    )
    assert bindings
    assert bindings[0].skill == "livefigure"


def test_answer_only_plan_injects_nothing() -> None:
    bindings = resolve_bound_skills(
        _plan("what is RAG?", "answer"),
        _registry(),
    )
    assert bindings == []
    assert render_bound_skill_block(bindings) == ""


def test_git_bash_is_not_leftover_pressure() -> None:
    trace = [
        ToolInvocationRecord(name="bash", arguments={"command": "git status"}, status="succeeded"),
        ToolInvocationRecord(name="bash", arguments={"command": "git diff"}, status="succeeded"),
    ]
    assert leftover_skill_pressure(trace, bound_skills={"scientific-figure"}) == 0


def test_outbox_pptx_bash_is_leftover_until_run_skill() -> None:
    leftover = ToolInvocationRecord(
        name="bash",
        arguments={"command": 'python gen.py -o "$OMNI_OUTPUT_DIR/fig.pptx"'},
        observation="FileNotFoundError: '$OMNI_OUTPUT_DIR/fig.pptx'",
        status="failed",
    )
    assert leftover_skill_pressure([leftover], bound_skills={"scientific-figure"}) == 1
    consumed = [
        leftover,
        ToolInvocationRecord(
            name="run_skill",
            arguments={"skill_name": "scientific-figure", "input": {"input": "RAG"}},
            status="succeeded",
        ),
    ]
    assert leftover_skill_pressure(consumed, bound_skills={"scientific-figure"}) == 0


class _CaptureLLM(ScriptedLLM):
    def __init__(self, script: list[ChatWithToolsResult]) -> None:
        super().__init__(script)
        self.messages: list[list[dict]] = []

    async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
        self.messages.append(list(messages))
        return await super().chat_with_tools(messages, tools, **kwargs)


@pytest.mark.asyncio
async def test_leftover_bash_is_steered_to_bound_skill() -> None:
    llm = _CaptureLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "1",
                        "bash",
                        {"command": 'python gen.py > "$OMNI_OUTPUT_DIR/fig.pptx"'},
                    )
                ]
            ),
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "2",
                        "run_skill",
                        {"skill_name": "scientific-figure", "input": {"input": "RAG"}},
                    )
                ]
            ),
            ChatWithToolsResult(content="Figure is on the skill path."),
        ]
    )

    async def invoker(name: str, args: dict) -> dict:
        if name == "bash":
            return {"exit_code": 1, "output": "FileNotFoundError: '$OMNI_OUTPUT_DIR/fig.pptx'"}
        return {"status": "ok", "skill_name": args.get("skill_name")}

    tools = [
        ToolSpec("bash", "shell", {"type": "object", "properties": {"command": {"type": "string"}}}),
        ToolSpec(
            "run_skill",
            "run",
            {"type": "object", "properties": {"skill_name": {"type": "string"}}},
        ),
    ]
    result = await ReActLoopAgent(
        llm,
        invoker,
        max_iterations=6,
        bound_skills={"scientific-figure"},
    ).run(system_prompt="s", user_message="draw RAG", tools=tools)

    assert result.tool_names() == ["bash", "run_skill"]
    steered = " ".join(
        str(message.get("content") or "")
        for batch in llm.messages
        for message in batch
        if message.get("role") == "user"
    )
    assert "scientific-figure" in steered
    assert "leftover" in steered
