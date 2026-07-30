"""BUG-02: a drained single_skill_task must deliver the skill, not a receipt.

Classmates saw paper-review / literature-search finish, then the foreground
answer stop at ``Created execution``. ReAct on the same skill delivered the
body because the model spoke from the tool result. Codex keeps that shape:
a tool that finishes in the turn returns its result. Omni still acks with a
receipt when the child outlives the turn (IM / daemon).
"""

from __future__ import annotations

import sys

import pytest

from omni.agent import OmniAgent
from omni.agent.plan_runner_utils import completed_skill_answer, delivered_skill_answer
from omni.config import load_settings
from omni.runtime.presentation import turn_presentation_from_result
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from tests.conftest import PlanningLLM


def _echo_skill(*, artifacts: bool = False) -> SkillEntry:
    payload = {
        "status": "ok",
        "summary": "review ready",
        "text": "The paper is sound on the main claim.",
    }
    if artifacts:
        payload["artifacts"] = [
            {
                "title": "review.md",
                "path": "/tmp/review.md",
                "format": "md",
            }
        ]
    script = (
        "import json,sys;"
        f"print(json.dumps({payload!r}, ensure_ascii=False))"
    )
    return SkillEntry(
        name="paper-review",
        description="Review a paper.",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        capabilities=["review.paper"],
        priority=90,
        input_schema={
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}, "text": {"type": "string"}},
        },
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


def test_delivered_skill_answer_prefers_result_text() -> None:
    drained = [
        {
            "status": "succeeded",
            "skill": "paper-review",
            "result": {
                "status": "ok",
                "text": "The paper is sound.",
                "summary": "review ready",
            },
        }
    ]
    assert delivered_skill_answer(drained) == "The paper is sound."
    assert completed_skill_answer(drained, skill="paper-review") == "The paper is sound."
    assert delivered_skill_answer([]) == ""
    assert completed_skill_answer(
        [{"status": "succeeded", "result": {}}],
        skill="paper-review",
    ) == "`paper-review` completed."


@pytest.mark.asyncio
async def test_drained_single_skill_turn_delivers_result_not_receipt() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_echo_skill(artifacts=True))
    agent.llm = PlanningLLM(
        {
            "intent_type": "single_skill_task",
            "confidence": 0.93,
            "required_capabilities": ["review.paper"],
            "outputs": ["answer"],
            "execution_mode": "background",
            "rationale": "user asked for a paper review",
        }
    )
    try:
        turn = await agent.handle_turn(
            "Review paper.pdf for NeurIPS.",
            channel="cli",
            drain_tasks=True,
        )

        assert turn.settlement_status == "succeeded"
        assert "The paper is sound on the main claim." in turn.text
        assert "Created `" not in turn.text
        assert "/task show" not in turn.text
        assert any(item.title == "review.md" for item in turn.artifacts)
        markdown = turn_presentation_from_result(turn).to_markdown()
        assert "The paper is sound on the main claim." in markdown
        assert markdown.count("The paper is sound on the main claim.") == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_background_single_skill_turn_still_acks_submission() -> None:
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_echo_skill())
    agent.llm = PlanningLLM(
        {
            "intent_type": "single_skill_task",
            "confidence": 0.93,
            "required_capabilities": ["review.paper"],
            "outputs": ["answer"],
            "execution_mode": "background",
            "rationale": "user asked for a paper review",
        }
    )
    try:
        turn = await agent.handle_turn(
            "Review paper.pdf for NeurIPS.",
            channel="cli",
            drain_tasks=False,
        )

        assert str(turn.settlement_status).startswith("pending")
        assert "Created `paper-review` execution" in turn.text
        assert f"/task show {turn.task_id[:8]}" in turn.text
        assert not turn.drained_results
    finally:
        await agent.aclose()
