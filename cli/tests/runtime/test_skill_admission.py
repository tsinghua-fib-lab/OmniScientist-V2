"""Host-known admission is a route fact, not a turn stop."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omni.agent.capability_runners import SkillTaskRunner
from omni.agent.intent_plan import IntentPlan, IntentType, SkillSelection
from omni.agent.plan_fallthrough import history_with_failed_attempt
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec, _is_terminal_tool_result
from omni.skills_runtime.admission import (
    is_admission_action,
    service_admission,
    skill_admission_rejection,
)
from omni.skills_runtime.manifest import SkillEntry, SkillKind
from tests.conftest import ScriptedLLM

ECHO = ToolSpec("echo", "echo back", {"type": "object", "properties": {"x": {"type": "string"}}})


def test_admission_action_is_owner_lifecycle_not_confirm() -> None:
    assert is_admission_action({"kind": "configure", "service": "vlm", "command": "omni config vlm"})
    assert is_admission_action({"kind": "configure", "command": "omni config model"})
    assert is_admission_action({"kind": "install", "bins": ["dot"]})
    assert not is_admission_action(
        {"kind": "configure", "action": "confirm_scientist_distillation"}
    )
    assert not is_admission_action({"kind": "ask"})


def test_service_admission_names_the_setup_command() -> None:
    entry = SkillEntry(
        name="livefigure",
        description="editable figure",
        kind=SkillKind.PYTHON_ENGINE,
        requires_services=["vlm"],
    )
    gateway = SimpleNamespace(
        available=False,
        setup_command="omni config vlm",
        error_code="vlm_not_configured",
        missing=("model", "endpoint", "api_key"),
    )
    result = service_admission(entry, {"vlm": gateway})
    assert result is not None
    assert result["action_required"]["kind"] == "configure"
    assert result["action_required"]["service"] == "vlm"
    assert result["do_not_retry"] is True
    assert "vision model (VLM)" in result["summary"]
    assert "omni config vlm" in result["error"]
    assert "Do not retry livefigure" in result["error"]
    assert skill_admission_rejection(entry, services={"vlm": gateway}) == result


def test_failed_route_history_tells_react_not_to_retry_admission() -> None:
    attempt = SimpleNamespace(
        handled=False,
        drained_results=[
            {
                "skill": "livefigure",
                "status": "failed",
                "error": "",
                "result": {
                    "summary": "livefigure cannot run: vision model (VLM) is not configured.",
                    "action_required": {
                        "kind": "configure",
                        "service": "vlm",
                        "command": "omni config vlm",
                    },
                    "setup_command": "omni config vlm",
                    "error_info": {"code": "vlm_not_configured"},
                },
            }
        ],
    )
    content = history_with_failed_attempt([], attempt)[-1]["content"]
    assert "omni config vlm" in content
    assert "do not retry `livefigure`" in content
    assert "vlm_not_configured" in content


def test_admission_configure_is_not_a_terminal_tool_result() -> None:
    assert _is_terminal_tool_result(
        {
            "status": "error",
            "action_required": {
                "kind": "configure",
                "service": "vlm",
                "command": "omni config vlm",
            },
        }
    ) is False
    assert _is_terminal_tool_result(
        {
            "status": "error",
            "action_required": {"kind": "install", "bins": ["dot"]},
        }
    ) is False
    assert _is_terminal_tool_result(
        {
            "status": "needs_input",
            "message": "Make a persona?",
            "action_required": {
                "kind": "configure",
                "action": "confirm_scientist_distillation",
            },
        }
    ) is True


@pytest.mark.asyncio
async def test_react_continues_after_admission_rejection() -> None:
    async def invoker(_name, _args):  # noqa: ANN001
        return {
            "status": "error",
            "summary": "livefigure cannot run: vision model (VLM) is not configured.",
            "action_required": {
                "kind": "configure",
                "service": "vlm",
                "command": "omni config vlm",
            },
            "error_info": {"code": "vlm_not_configured"},
        }

    llm = ScriptedLLM(
        [
            ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {"x": "hi"})]),
            ChatWithToolsResult(
                content="VLM is not configured. I will use another catalog skill."
            ),
        ]
    )
    agent = ReActLoopAgent(llm, invoker, max_iterations=4)
    result = await agent.run(system_prompt="sys", user_message="draw it", tools=[ECHO])
    assert result.kind == "text"
    assert "another catalog skill" in result.content
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_skill_task_runner_hands_admission_back_unhandled() -> None:
    entry = SimpleNamespace(name="livefigure", input_schema={"type": "object"})
    registry = SimpleNamespace(resolve_ref=lambda *_a, **_k: entry)
    result_json = {
        "status": "error",
        "summary": "livefigure cannot run: vision model (VLM) is not configured.",
        "action_required": {
            "kind": "configure",
            "service": "vlm",
            "command": "omni config vlm",
        },
        "error_info": {"code": "vlm_not_configured"},
    }
    task = SimpleNamespace(
        status="failed",
        skill_name="livefigure",
        result_json=result_json,
        error="vlm_not_configured",
        trace_log=[],
    )

    class Runtime:
        async def enqueue(self, *_a, **_k):  # noqa: ANN001
            return "sub-1"

        async def process(self, *_a, **_k):  # noqa: ANN001
            return None

        async def get_subtask(self, *_a, **_k):  # noqa: ANN001
            return task

    class Tasks:
        async def append_event(self, *_a, **_k):  # noqa: ANN001
            return None

    plan = IntentPlan(
        task_id="task-1",
        user_message="draw an editable figure",
        intent_type=IntentType.SINGLE_SKILL_TASK,
        selected_skills=[
            SkillSelection(skill="livefigure", reason="user asked", selection_source="planner")
        ],
        provider_inputs={"livefigure": {"input": "architecture"}},
    )
    runner = SkillTaskRunner()
    outcome = await runner.run(
        plan,
        ctx=SimpleNamespace(task_id="task-1", session_id="s", channel="cli", execution_authority=None),
        tools=[],
        runtime=Runtime(),
        tasks=Tasks(),
        registry=registry,
        drain_tasks=True,
    )
    assert outcome.handled is False
    assert outcome.terminated_reason == "single_skill_failed"
    assert outcome.drained_results[0]["result"]["action_required"]["service"] == "vlm"
