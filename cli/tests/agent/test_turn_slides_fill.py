"""Host slide fill submits research-pptx when this task still owes a deck.

A twin-task PPTX is not in ``list_by_task(this_id)``, so it cannot skip the
fill. IM queues the child; CLI waits in-turn.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType, VerificationPlan
from omni.agent.task_controller import TaskController
from omni.agent.turn_execution import TurnCompletion, TurnResult
from omni.core.react_agent import AgentLoopResult
from omni.runtime.presentation import ArtifactRef
from omni.runtime.settlement import Settlement


def _plan(*, twin: str = "") -> IntentPlan:
    return IntentPlan(
        task_id="d6179941" + "0" * 24,
        user_message="综述材料 + 生成 PPT：智能体 Loop Engineering",
        intent_type=IntentType.REACT_FALLBACK,
        verification_plan=VerificationPlan(
            required_outputs=["draft.manuscript", "artifact.slides"]
        ),
        twin_task_id=twin,
    )


def _completion(*, artifacts: Any, runtime: Any, registry: Any = None) -> TurnCompletion:
    return TurnCompletion(
        tasks=SimpleNamespace(),
        task_controller=SimpleNamespace(),
        hooks=SimpleNamespace(),
        runtime=runtime,
        artifacts=artifacts,
        llm=object(),
        registry=registry,
    )


def _manuscript() -> SimpleNamespace:
    return SimpleNamespace(
        kind="report",
        title="survey materials",
        rel_path="artifacts/agent_loop_engineering_survey_materials.md",
        path="artifacts/agent_loop_engineering_survey_materials.md",
        uri="artifact://md-survey",
        mime="text/markdown",
    )


@pytest.mark.asyncio
async def test_owed_slides_are_queued_on_im() -> None:
    runtime = SimpleNamespace(enqueue=AsyncMock(return_value="sub-slides-1"))
    registry = SimpleNamespace(
        resolve_capability=lambda _cap: (SimpleNamespace(name="research-pptx"), [])
    )
    completion = _completion(
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[_manuscript()])),
        runtime=runtime,
        registry=registry,
    )
    submitted: list[str] = []
    drained: list[dict] = []

    notes = await completion._fill_remaining_slides(
        _plan(),
        AgentLoopResult(kind="text", content="PPT 生成完成后取回 .pptx。"),
        drained,
        submitted=submitted,
        task_id=_plan().task_id,
        session_id="s1",
        drain_tasks=False,
        channel="wechat",
    )

    runtime.enqueue.assert_awaited_once()
    kwargs = runtime.enqueue.await_args
    assert kwargs.args[0] == "research-pptx"
    assert kwargs.args[2] == "wechat"
    assert kwargs.kwargs["task_id"] == _plan().task_id
    assert kwargs.kwargs["queue"] is True
    assert kwargs.args[1]["markdown_uri"] == "artifact://md-survey"
    assert submitted == ["sub-slides-1"]
    assert drained[0]["subtask_id"] == "sub-slides-1"
    assert any("artifact.slides" in note for note in notes)


@pytest.mark.asyncio
async def test_this_task_deck_skips_host_fill() -> None:
    runtime = SimpleNamespace(enqueue=AsyncMock())
    owned = SimpleNamespace(
        kind="slides",
        title="deck",
        rel_path="presentation/deck.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    completion = _completion(
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[owned])),
        runtime=runtime,
    )

    notes = await completion._fill_remaining_slides(
        _plan(),
        AgentLoopResult(kind="text", content="done"),
        [],
        submitted=[],
        task_id=_plan().task_id,
        session_id="s1",
        drain_tasks=False,
        channel="wechat",
    )

    runtime.enqueue.assert_not_called()
    assert notes == []


@pytest.mark.asyncio
async def test_already_submitted_pptx_is_not_queued_again() -> None:
    runtime = SimpleNamespace(enqueue=AsyncMock())
    completion = _completion(
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[_manuscript()])),
        runtime=runtime,
    )
    drained = [{"skill": "research-pptx", "status": "submitted", "subtask_id": "already"}]

    notes = await completion._fill_remaining_slides(
        _plan(),
        AgentLoopResult(kind="text", content="waiting"),
        drained,
        submitted=["already"],
        task_id=_plan().task_id,
        session_id="s1",
        drain_tasks=False,
        channel="wechat",
    )

    runtime.enqueue.assert_not_called()
    assert notes == []


@pytest.mark.asyncio
async def test_host_fill_slides_uses_artifact_uri_not_rel_path(tmp_path: Path) -> None:
    paper = tmp_path / "RAG系统综述.md"
    paper.write_text("# survey\n", encoding="utf-8")
    uri = "artifact://552970884abb49bf9261d8704f90e55b"
    row = SimpleNamespace(
        kind="document",
        title="RAG系统综述",
        rel_path="artifacts/RAG系统综述.md",
        path="artifacts/RAG系统综述.md",
        uri=uri,
        mime="text/markdown",
    )

    class Store:
        async def list_by_task(self, task_id: str) -> list[Any]:
            return [row]

        async def resolve_path(self, handle: str) -> Path | None:
            return paper if handle == uri else None

    runtime = SimpleNamespace(enqueue=AsyncMock(return_value="sub-slides-uri"))
    completion = _completion(artifacts=Store(), runtime=runtime, registry=None)

    await completion._fill_remaining_slides(
        _plan(),
        AgentLoopResult(kind="text", content="PPT 生成完成后取回 .pptx。"),
        [],
        submitted=[],
        task_id=_plan().task_id,
        session_id="s1",
        drain_tasks=False,
        channel="wechat",
    )

    runtime.enqueue.assert_awaited_once()
    params = runtime.enqueue.await_args.args[1]
    assert params["markdown_uri"] == uri
    assert not params["markdown_uri"].startswith("artifacts/")


@pytest.mark.asyncio
async def test_unresolvable_rel_path_omits_markdown_uri() -> None:
    row = SimpleNamespace(
        kind="document",
        title="RAG系统综述",
        rel_path="artifacts/RAG系统综述.md",
        path="artifacts/RAG系统综述.md",
        uri="",
        mime="text/markdown",
    )
    runtime = SimpleNamespace(enqueue=AsyncMock(return_value="sub-slides-topic"))
    completion = _completion(
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[row])),
        runtime=runtime,
    )

    notes = await completion._fill_remaining_slides(
        _plan(),
        AgentLoopResult(kind="text", content="PPT 生成完成后取回 .pptx。"),
        [],
        submitted=[],
        task_id=_plan().task_id,
        session_id="s1",
        drain_tasks=False,
        channel="wechat",
    )

    runtime.enqueue.assert_awaited_once()
    params = runtime.enqueue.await_args.args[1]
    assert "markdown_uri" not in params
    assert params["topic"] == _plan().user_message
    assert any("durable URI" in note for note in notes)


@pytest.mark.asyncio
async def test_unresolved_artifact_uri_omits_markdown_uri() -> None:
    row = SimpleNamespace(
        kind="document",
        title="RAG系统综述",
        rel_path="artifacts/RAG系统综述.md",
        path="artifacts/RAG系统综述.md",
        uri="artifact://missing-manuscript",
        mime="text/markdown",
    )

    class Store:
        async def list_by_task(self, task_id: str) -> list[Any]:
            return [row]

        async def resolve_path(self, handle: str) -> Path | None:
            return None

    runtime = SimpleNamespace(enqueue=AsyncMock(return_value="sub-slides-topic"))
    completion = _completion(artifacts=Store(), runtime=runtime)

    notes = await completion._fill_remaining_slides(
        _plan(),
        AgentLoopResult(kind="text", content="PPT 生成完成后取回 .pptx。"),
        [],
        submitted=[],
        task_id=_plan().task_id,
        session_id="s1",
        drain_tasks=False,
        channel="wechat",
    )

    runtime.enqueue.assert_awaited_once()
    assert "markdown_uri" not in runtime.enqueue.await_args.args[1]
    assert any("durable URI" in note for note in notes)


@pytest.mark.asyncio
async def test_host_fill_slides_falls_back_to_absolute_path(tmp_path: Path) -> None:
    paper = tmp_path / "survey.md"
    paper.write_text("# survey\n", encoding="utf-8")
    row = SimpleNamespace(
        kind="report",
        title="survey materials",
        rel_path="artifacts/survey.md",
        path=str(paper),
        uri="",
        mime="text/markdown",
    )
    runtime = SimpleNamespace(enqueue=AsyncMock(return_value="sub-slides-abs"))
    completion = _completion(
        artifacts=SimpleNamespace(list_by_task=AsyncMock(return_value=[row])),
        runtime=runtime,
    )

    await completion._fill_remaining_slides(
        _plan(),
        AgentLoopResult(kind="text", content="PPT 生成完成后取回 .pptx。"),
        [],
        submitted=[],
        task_id=_plan().task_id,
        session_id="s1",
        drain_tasks=False,
        channel="wechat",
    )

    params = runtime.enqueue.await_args.args[1]
    assert params["markdown_uri"] == str(paper)


@pytest.mark.asyncio
async def test_twin_contract_files_fill_the_owed_deck_gap(tmp_path: Any) -> None:

    twin_id = "d6179941" + "1" * 24
    deck_path = tmp_path / "old.pptx"
    deck_path.write_bytes(b"PK")
    this_md = ArtifactRef(
        title="survey materials",
        format="md",
        path="/tmp/survey.md",
        uri="artifact://md-new",
    )
    twin_deck = SimpleNamespace(
        kind="slides",
        title="old deck",
        uri="artifact://pptx-twin",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size_bytes=2,
        rel_path=str(deck_path),
    )

    class Store:
        async def list_by_task(self, task_id: str) -> list[Any]:
            return [twin_deck] if task_id == twin_id else []

        async def resolve_path(self, uri: str) -> Path | None:
            return deck_path if uri == "artifact://pptx-twin" else None

    completion = _completion(artifacts=Store(), runtime=SimpleNamespace())
    merged = await completion._contract_output_artifacts(_plan(twin=twin_id), [this_md], [])
    assert [item.uri for item in merged] == ["artifact://md-new", "artifact://pptx-twin"]


@pytest.mark.asyncio
async def test_apply_settlement_maps_active_children_to_pending_child_task() -> None:
    controller = TaskController(
        SimpleNamespace(
            settlement=AsyncMock(return_value=Settlement("pending", {"active": ["child-1"]}))
        )
    )
    result = TurnResult(text="working", session_id="s1", settlement_status="succeeded")
    await controller.apply_settlement("task-1", result)
    assert result.settlement_status == "pending_child_task"


@pytest.mark.asyncio
async def test_apply_settlement_still_sends_when_only_the_chat_hop_is_pending() -> None:
    controller = TaskController(
        SimpleNamespace(
            settlement=AsyncMock(
                return_value=Settlement("pending", {"awaiting_presentation": ["presentation"]})
            )
        )
    )
    result = TurnResult(text="ready", session_id="s1", settlement_status="succeeded")
    await controller.apply_settlement("task-1", result)
    assert result.settlement_status == "pending"
