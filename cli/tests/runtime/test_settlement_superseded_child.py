"""A leftover sibling must not fail the parent once this task has the files."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omni.runtime.settlement import settlement_for
from tests.runtime.test_settlement_empty_funnel import _child, _event, _Store, _task


def _figure(**fields: object) -> SimpleNamespace:
    payload = {
        "kind": "figure",
        "format": "png",
        "path": "x.png",
        "mime": "image/png",
        "title": "fig",
    }
    payload.update(fields)
    return SimpleNamespace(**payload)


def _manuscript(**fields: object) -> SimpleNamespace:
    payload = {
        "kind": "report",
        "format": "md",
        "path": "survey.md",
        "mime": "text/markdown",
        "title": "survey",
    }
    payload.update(fields)
    return SimpleNamespace(**payload)


@pytest.mark.asyncio
async def test_failed_livefigure_is_leftover_when_figure_and_paper_exist() -> None:
    store = _Store(
        _task(
            subtask_ids=["live-1", "fig-1", "paper-1"],
            outputs=["artifact.figure", "draft.manuscript"],
        ),
        events=[
            _event(
                "react.finished",
                kind="text",
                terminated_reason="done",
                tool_names=["run_skill", "run_skill", "run_skill"],
            )
        ],
        children=[
            _child(
                "live-1",
                status="failed",
                result={"status": "error", "error": "vlm timeout"},
            ),
            _child(
                "fig-1",
                status="succeeded",
                result={"status": "ok", "artifacts": [{"path": "x.png"}]},
            ),
            _child(
                "paper-1",
                status="succeeded",
                result={"status": "ok", "artifacts": [{"path": "survey.md"}]},
            ),
        ],
        artifacts=[_figure(), _manuscript()],
    )
    settled = await settlement_for(store, "parent")
    assert settled.status == "succeeded"
    assert "lost" not in settled.detail
    assert "degraded" not in settled.detail
    assert settled.detail["superseded_failures"] == ["live-1"]


@pytest.mark.asyncio
async def test_topology_partial_is_leftover_when_the_figure_is_on_this_task() -> None:
    store = _Store(
        _task(subtask_ids=["fig-1"], outputs=["artifact.figure"]),
        events=[
            _event(
                "react.finished",
                kind="text",
                terminated_reason="done",
                tool_names=["run_skill"],
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
                    "artifacts": [{"format": "png", "path": "x.png"}],
                },
            )
        ],
        artifacts=[_figure()],
    )
    settled = await settlement_for(store, "parent")
    assert settled.status == "succeeded"
    assert "degraded" not in settled.detail
    assert "lost" not in settled.detail


@pytest.mark.asyncio
async def test_failed_child_still_fails_when_the_manuscript_is_missing() -> None:
    store = _Store(
        _task(
            subtask_ids=["live-1", "fig-1"],
            outputs=["artifact.figure", "draft.manuscript"],
        ),
        events=[
            _event(
                "react.finished",
                kind="text",
                terminated_reason="done",
                tool_names=["run_skill", "run_skill"],
            )
        ],
        children=[
            _child(
                "live-1",
                status="failed",
                result={"status": "error", "error": "vlm timeout"},
            ),
            _child(
                "fig-1",
                status="succeeded",
                result={"status": "ok", "artifacts": [{"path": "x.png"}]},
            ),
        ],
        artifacts=[_figure()],
    )
    settled = await settlement_for(store, "parent")
    assert settled.status == "failed"
    assert settled.detail["lost"] == ["live-1"]
    assert settled.detail["undelivered_outputs"] == ["draft.manuscript"]
    assert "superseded_failures" not in settled.detail


def _pptx(**fields: object) -> SimpleNamespace:
    payload = {
        "kind": "slides",
        "format": "pptx",
        "path": "deck.pptx",
        "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "title": "deck",
    }
    payload.update(fields)
    return SimpleNamespace(**payload)


@pytest.mark.asyncio
async def test_failed_livefigure_does_not_succeed_on_a_harvested_deck() -> None:
    store = _Store(
        _task(subtask_ids=["live-1"], outputs=["artifact.pptx"]),
        events=[
            _event(
                "react.finished",
                kind="text",
                terminated_reason="done",
                tool_names=["run_skill", "bash"],
            )
        ],
        children=[
            _child(
                "live-1",
                status="failed",
                result={"status": "error", "error": "forbidden dunder"},
            ),
        ],
        artifacts=[_pptx()],
    )
    settled = await settlement_for(store, "parent")
    assert settled.status == "failed"
    assert settled.detail["lost"] == ["live-1"]
    assert settled.detail["undelivered_outputs"] == ["artifact.pptx"]
