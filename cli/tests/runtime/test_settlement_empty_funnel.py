"""An empty-funnel child must not paint the parent after later retrieve."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omni.runtime.settlement import settlement_for


def _task(
    *,
    task_id: str = "parent",
    subtask_ids: list[str],
    outputs: list[str] | None = None,
) -> SimpleNamespace:
    plan: dict = {}
    if outputs:
        plan["verification_plan"] = {"required_outputs": outputs, "required_events": []}
    return SimpleNamespace(
        id=task_id,
        status="running",
        channel="cli",
        submitted_subtask_ids=subtask_ids,
        submitted_workflow_ids=[],
        plan_json=plan,
    )


def _event(event_type: str, *, status: str = "succeeded", **payload: object) -> SimpleNamespace:
    return SimpleNamespace(event_type=event_type, status=status, output_json=payload)


def _child(
    child_id: str,
    *,
    status: str,
    result: dict,
) -> SimpleNamespace:
    return SimpleNamespace(id=child_id, status=status, result_json=result, retry_of="")


class _Store:
    def __init__(
        self,
        task: SimpleNamespace,
        *,
        events: list[SimpleNamespace],
        children: list[SimpleNamespace],
        artifacts: list[object] | None = None,
    ) -> None:
        self._task = task
        self._events = events
        self._children = {item.id: item for item in children}
        self._artifacts = artifacts or []

    async def get_task(self, task_id: str) -> SimpleNamespace | None:
        return self._task if task_id == self._task.id else None

    async def list_events(self, task_id: str) -> list[SimpleNamespace]:
        return list(self._events) if task_id == self._task.id else []

    async def list_subtasks_by_ids(self, subtask_ids: list[str]) -> list[SimpleNamespace]:
        return [self._children[item] for item in subtask_ids if item in self._children]

    async def list_workflows_by_ids(self, workflow_ids: list[str]) -> list:
        return []

    async def list_subtasks_by_workflow_ids(self, workflow_ids: list[str]) -> list:
        return []

    async def list_artifacts_by_task(self, task_id: str) -> list[object]:
        return list(self._artifacts)


_EMPTY_IDEATION = {
    "status": "partial",
    "warning": "Literature search returned zero relevant papers for the generated queries.",
    "steps": {"search": {"queries": ["latent space"], "paper_count": 0, "papers": []}},
}


@pytest.mark.asyncio
async def test_empty_funnel_child_alone_keeps_parent_degraded() -> None:
    store = _Store(
        _task(subtask_ids=["idea-1"]),
        events=[_event("react.finished", kind="text", terminated_reason="done", tool_names=["run_skill"])],
        children=[_child("idea-1", status="degraded", result=_EMPTY_IDEATION)],
    )
    settled = await settlement_for(store, "parent")
    assert settled.status == "degraded"
    assert settled.detail["degraded"] == ["idea-1"]


@pytest.mark.asyncio
async def test_later_arxiv_fetch_supersedes_empty_funnel_child() -> None:
    store = _Store(
        _task(subtask_ids=["idea-1", "arxiv-1"]),
        events=[
            _event(
                "react.finished",
                kind="text",
                terminated_reason="done",
                tool_names=["run_skill", "run_skill"],
            )
        ],
        children=[
            _child("idea-1", status="degraded", result=_EMPTY_IDEATION),
            _child(
                "arxiv-1",
                status="succeeded",
                result={
                    "status": "ok",
                    "title": "Inference-Time Intervention",
                    "arxiv_id": "2306.03341",
                },
            ),
        ],
    )
    settled = await settlement_for(store, "parent")
    assert settled.status == "succeeded"
    assert "degraded" not in settled.detail


@pytest.mark.asyncio
async def test_search_literature_in_react_supersedes_empty_funnel_child() -> None:
    store = _Store(
        _task(subtask_ids=["idea-1"]),
        events=[
            _event(
                "react.finished",
                kind="text",
                terminated_reason="done",
                tool_names=["run_skill", "search_literature"],
            )
        ],
        children=[_child("idea-1", status="degraded", result=_EMPTY_IDEATION)],
    )
    settled = await settlement_for(store, "parent")
    assert settled.status == "succeeded"
    assert "degraded" not in settled.detail


@pytest.mark.asyncio
async def test_failed_child_still_wins_over_empty_funnel_leftover() -> None:
    store = _Store(
        _task(subtask_ids=["idea-1", "boom-1"]),
        events=[
            _event(
                "react.finished",
                kind="text",
                terminated_reason="done",
                tool_names=["run_skill", "search_literature"],
            )
        ],
        children=[
            _child("idea-1", status="degraded", result=_EMPTY_IDEATION),
            _child("boom-1", status="failed", result={"status": "error", "error": "boom"}),
        ],
    )
    settled = await settlement_for(store, "parent")
    assert settled.status == "failed"
    assert settled.detail["lost"] == ["boom-1"]


@pytest.mark.asyncio
async def test_figure_partial_is_not_empty_funnel_leftover() -> None:
    store = _Store(
        _task(subtask_ids=["fig-1"]),
        events=[
            _event(
                "react.finished",
                kind="text",
                terminated_reason="done",
                tool_names=["run_skill", "search_literature"],
            )
        ],
        children=[
            _child(
                "fig-1",
                status="degraded",
                result={
                    "status": "partial",
                    "warning": "This is a weaker Graphviz schematic.",
                    "figure_kind": "generic",
                    "artifacts": [{"format": "svg", "path": "/tmp/x.svg"}],
                },
            )
        ],
    )
    settled = await settlement_for(store, "parent")
    assert settled.status == "degraded"
    assert settled.detail["degraded"] == ["fig-1"]
