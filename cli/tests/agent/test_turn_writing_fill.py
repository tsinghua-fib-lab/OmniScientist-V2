"""Native manuscript fill must leave a file, not a second copy in the reply.

WeChat task b0cd360c stored a 6 KB report and then appended the same draft
onto ``result.content``. The chat budget cut the paste, and the figure queued
behind it never sent. Codex treats a long deliverable as a file; the reply is
a pointer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType, SkillSelection, VerificationPlan
from omni.agent.plan_result import PlanExecutionResult
from omni.agent.turn_execution import TurnCompletion
from omni.core.react_agent import AgentLoopResult, ToolInvocationRecord


def _research_result(**kwargs: object) -> AgentLoopResult:
    return AgentLoopResult(
        kind="text",
        tool_trace=[
            ToolInvocationRecord(
                name="search_literature",
                arguments={"query": "RAG"},
                result={"matches": [{"title": "paper"}]},
                status="succeeded",
            )
        ],
        **kwargs,
    )


def _plan() -> IntentPlan:
    return IntentPlan(
        task_id="b0cd360c" + "0" * 24,
        user_message="Write a RAG survey and an architecture figure.",
        intent_type=IntentType.REACT_FALLBACK,
        verification_plan=VerificationPlan(required_outputs=["draft.manuscript"]),
    )


@pytest.mark.asyncio
async def test_a_stored_manuscript_is_not_pasted_into_the_reply() -> None:
    completion = TurnCompletion(
        tasks=SimpleNamespace(),
        task_controller=SimpleNamespace(),
        hooks=SimpleNamespace(),
        runtime=SimpleNamespace(),
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[])),
        llm=object(),
    )
    result = _research_result(content="Materials are ready.")
    draft = "# RAG survey\n\n" + ("long paragraph. " * 80)

    async def fake_synth(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return {
            "text": draft,
            "draft_markdown": draft,
            "summary": "Wrote the manuscript as a file.",
            "artifacts": [
                {
                    "title": "RAG survey",
                    "path": "/tmp/survey.md",
                    "uri": "artifact://aa",
                    "format": "md",
                }
            ],
            "report_uri": "artifact://aa",
        }

    with patch("omni.runtime.final_synthesis.run_native_synthesis", fake_synth):
        notes = await completion._fill_remaining_writing(
            _plan(), result, [], task_id=_plan().task_id, session_id="s1"
        )

    assert draft not in result.content
    assert result.content == "Materials are ready."
    assert any("native synthesis" in note.lower() for note in notes)


@pytest.mark.asyncio
async def test_host_writing_fill_uses_a_short_topic() -> None:
    completion = TurnCompletion(
        tasks=SimpleNamespace(),
        task_controller=SimpleNamespace(),
        hooks=SimpleNamespace(),
        runtime=SimpleNamespace(),
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[])),
        llm=object(),
    )
    captured: dict[str, object] = {}

    async def fake_synth(goal, step, *_args, **_kwargs):  # noqa: ANN001
        captured["topic"] = step["input"]["topic"]
        return {"text": "# short", "draft_markdown": "# short", "artifacts": [{"uri": "a"}]}

    with patch("omni.runtime.final_synthesis.run_native_synthesis", fake_synth):
        await completion._fill_remaining_writing(
            _plan(),
            _research_result(content="ok"),
            [],
            task_id=_plan().task_id,
            session_id="s1",
        )

    assert captured["topic"] == "RAG survey"
    assert "Draft" not in str(captured["topic"])
    assert len(str(captured["topic"])) <= 24


@pytest.mark.asyncio
async def test_an_unstored_draft_still_reaches_the_reply_when_nothing_was_written() -> None:
    """If storage failed, the only copy is the reply — do not drop it."""
    completion = TurnCompletion(
        tasks=SimpleNamespace(),
        task_controller=SimpleNamespace(),
        hooks=SimpleNamespace(),
        runtime=SimpleNamespace(),
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[])),
        llm=object(),
    )
    result = _research_result(content="")

    async def fake_synth(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return {"text": "# draft only", "draft_markdown": "# draft only"}

    with patch("omni.runtime.final_synthesis.run_native_synthesis", fake_synth):
        await completion._fill_remaining_writing(
            _plan(), result, [], task_id=_plan().task_id, session_id="s1"
        )

    assert "# draft only" in result.content


@pytest.mark.asyncio
async def test_memory_only_trace_does_not_host_fill_a_manuscript() -> None:
    completion = TurnCompletion(
        tasks=SimpleNamespace(),
        task_controller=SimpleNamespace(),
        hooks=SimpleNamespace(),
        runtime=SimpleNamespace(),
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[])),
        llm=object(),
    )
    result = AgentLoopResult(
        kind="text",
        content="Found a v3 report from last week.",
        tool_trace=[
            ToolInvocationRecord(
                name="memory_search",
                arguments={"query": "latent space"},
                result={"matches": [{"summary": "v3 report"}]},
                status="succeeded",
            )
        ],
    )
    synth = AsyncMock(return_value={"text": "# should not write", "artifacts": [{"uri": "a"}]})

    with patch("omni.runtime.final_synthesis.run_native_synthesis", synth):
        notes = await completion._fill_remaining_writing(
            _plan(), result, [], task_id=_plan().task_id, session_id="s1"
        )

    synth.assert_not_awaited()
    assert any("no this-turn research evidence" in note for note in notes)
    assert "# should not write" not in result.content


def _react_survey_plan() -> IntentPlan:
    return IntentPlan(
        task_id="ad2d9142" + "0" * 24,
        user_message="帮我调研如何利用隐空间干预的方式提升LLM的Agentic能力",
        intent_type=IntentType.REACT_FALLBACK,
        outputs=["draft.section"],
        selected_skills=[],
        capability_inputs={},
        verification_plan=VerificationPlan(required_outputs=["draft.section"]),
    )


def _survey_plan() -> IntentPlan:
    return IntentPlan(
        task_id="ad2d9142" + "0" * 24,
        user_message="帮我调研如何利用隐空间干预的方式提升LLM的Agentic能力",
        intent_type=IntentType.SINGLE_SKILL_TASK,
        outputs=["sources", "draft.section"],
        selected_skills=[
            SkillSelection(
                skill="openalex-search",
                reason="written survey",
                matched_capabilities=["literature.search"],
            )
        ],
        capability_inputs={"literature.search": {"query": "latent intervention"}},
        verification_plan=VerificationPlan(required_outputs=["draft.section"]),
    )


@pytest.mark.asyncio
async def test_survey_closer_retrieves_then_writes_when_the_loop_only_looked_up() -> None:
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-lit"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(
                status="succeeded",
                skill_name="openalex-search",
                result_json={"matches": [{"title": "Activation Steering"}]},
                error="",
                trace_log=[],
            )
        ),
    )
    registry = SimpleNamespace(resolve_capability=lambda *_args, **_kwargs: (None, []))
    completion = TurnCompletion(
        tasks=SimpleNamespace(),
        task_controller=SimpleNamespace(),
        hooks=SimpleNamespace(),
        runtime=runtime,
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[])),
        llm=object(),
        registry=registry,
    )
    result = AgentLoopResult(
        kind="text",
        content="Found last week's report.",
        tool_trace=[
            ToolInvocationRecord(
                name="search_tasks",
                arguments={"query": "latent"},
                result={"matches": [{"id": "cb077c5b"}]},
                status="succeeded",
            )
        ],
    )
    drained: list[dict] = []

    async def fake_synth(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return {
            "text": "# new survey",
            "draft_markdown": "# new survey",
            "summary": "Wrote the manuscript as a file.",
            "artifacts": [{"uri": "artifact://new"}],
            "report_uri": "artifact://new",
        }

    with patch("omni.runtime.final_synthesis.run_native_synthesis", fake_synth):
        notes = await completion._fill_remaining_writing(
            _survey_plan(), result, drained, task_id=_survey_plan().task_id, session_id="s1"
        )

    runtime.enqueue.assert_awaited()
    assert any("retrieved literature" in note.lower() for note in notes)
    assert any("native synthesis" in note.lower() for note in notes)
    assert result.content == "Found last week's report."
    assert any(record.name == "run_skill" for record in result.tool_trace)


@pytest.mark.asyncio
async def test_react_fallback_survey_wording_still_retrieves_then_writes() -> None:
    """Workflow demote dropped literature.search; 调研 wording still closes."""
    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-lit"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(
                status="succeeded",
                skill_name="openalex-search",
                result_json={"matches": [{"title": "Activation Steering"}]},
                error="",
                trace_log=[],
            )
        ),
    )
    entry = SimpleNamespace(name="openalex-search")
    registry = SimpleNamespace(resolve_capability=lambda *_args, **_kwargs: (entry, []))
    completion = TurnCompletion(
        tasks=SimpleNamespace(),
        task_controller=SimpleNamespace(),
        hooks=SimpleNamespace(),
        runtime=runtime,
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[])),
        llm=object(),
        registry=registry,
    )
    result = AgentLoopResult(
        kind="text",
        content="Found last week's report.",
        tool_trace=[
            ToolInvocationRecord(
                name="memory_search",
                arguments={"query": "latent"},
                result={"matches": [{"id": "m1"}]},
                status="succeeded",
            )
        ],
    )

    async def fake_synth(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return {
            "text": "# new survey",
            "draft_markdown": "# new survey",
            "summary": "Wrote the manuscript as a file.",
            "artifacts": [{"uri": "artifact://new"}],
            "report_uri": "artifact://new",
        }

    with patch("omni.runtime.final_synthesis.run_native_synthesis", fake_synth):
        notes = await completion._fill_remaining_writing(
            _react_survey_plan(),
            result,
            [],
            task_id=_react_survey_plan().task_id,
            session_id="s1",
        )

    runtime.enqueue.assert_awaited()
    assert runtime.enqueue.await_args.args[0] == "openalex-search"
    assert any("retrieved literature" in note.lower() for note in notes)
    assert any("native synthesis" in note.lower() for note in notes)


@pytest.mark.asyncio
async def test_complete_plan_writes_the_manuscript_after_a_drained_search() -> None:
    recorder = SimpleNamespace(timeline=[])

    async def _emit(*_args, **_kwargs):  # noqa: ANN002, ANN003
        recorder.timeline.append("hook")

    async def _persist(*_args, **_kwargs):  # noqa: ANN002, ANN003
        recorder.timeline.append("persist")

    async def _memory(*_args, **_kwargs):  # noqa: ANN002, ANN003
        recorder.timeline.append("memory")

    async def _finish(*_args, **_kwargs):  # noqa: ANN002, ANN003
        recorder.timeline.append("finish")

    async def _settle(_task_id: str, result: PlanExecutionResult) -> PlanExecutionResult:
        recorder.timeline.append("settle")
        return result

    completion = TurnCompletion(
        tasks=SimpleNamespace(),
        task_controller=SimpleNamespace(finish_turn=_finish),
        hooks=SimpleNamespace(emit=_emit),
        runtime=SimpleNamespace(),
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[])),
        llm=object(),
    )
    execution = PlanExecutionResult(
        handled=True,
        text="OpenAlex returned 6 papers.",
        tool_trace=[
            ToolInvocationRecord(
                name="run_skill",
                arguments={"skill_name": "openalex-search"},
                result={"status": "succeeded", "subtask_id": "sub-1"},
                status="succeeded",
            )
        ],
        drained_results=[
            {
                "subtask_id": "sub-1",
                "skill": "openalex-search",
                "status": "succeeded",
                "result": {"matches": [{"title": "paper"}]},
            }
        ],
    )

    async def fake_synth(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return {
            "text": "# survey",
            "summary": "Wrote the manuscript as a file.",
            "artifacts": [{"uri": "artifact://survey"}],
            "report_uri": "artifact://survey",
        }

    with patch("omni.runtime.final_synthesis.run_native_synthesis", fake_synth):
        turn = await completion.complete_plan(
            plan=_survey_plan(),
            result=execution,
            session_id="s1",
            user_message=_survey_plan().user_message,
            drain_tasks=True,
            persist_message=_persist,
            record_turn_memory=_memory,
            apply_settlement=_settle,
        )

    assert "Wrote the manuscript as a file." in execution.text or execution.text == "OpenAlex returned 6 papers."
    assert any("native synthesis" in note.lower() for note in turn.degraded_warnings)
    assert recorder.timeline[:2] == ["hook", "persist"]
