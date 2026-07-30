"""Attachments belong to the immutable turn input.

Without this wiring a ``task retry`` replays the original text with its ``@``
mentions stripped of any grant, so the retried attempt silently answers a
different question than the one that was asked.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omni.agent.task_controller import TaskController


class _RecordingRecorder:
    """Captures the kwargs ``create_turn_task`` forwards to the recorder."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def create_task(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return SimpleNamespace(id="task-1")


@pytest.mark.asyncio
async def test_create_turn_task_forwards_attachments() -> None:
    recorder = _RecordingRecorder()
    controller = TaskController(recorder)  # type: ignore[arg-type]

    task_id = await controller.create_turn_task(
        session_id="s1",
        channel="cli",
        user_input="review @/abs/paper.md",
        file_uris=["/abs/paper.md"],
    )

    assert task_id == "task-1"
    assert recorder.kwargs["file_uris"] == ["/abs/paper.md"]


@pytest.mark.asyncio
async def test_turn_without_attachments_stays_unchanged() -> None:
    recorder = _RecordingRecorder()
    controller = TaskController(recorder)  # type: ignore[arg-type]

    await controller.create_turn_task(session_id="s1", channel="cli", user_input="hello")

    assert recorder.kwargs["file_uris"] is None


@pytest.mark.asyncio
async def test_ack_still_reports_the_task_id() -> None:
    recorder = _RecordingRecorder()
    controller = TaskController(recorder)  # type: ignore[arg-type]
    seen: list[dict] = []

    await controller.create_turn_task(
        session_id="s1",
        channel="cli",
        user_input="review @a.md",
        file_uris=["/abs/a.md"],
        on_task_ack=seen.append,
    )

    assert seen and seen[0]["task_id"] == "task-1"
