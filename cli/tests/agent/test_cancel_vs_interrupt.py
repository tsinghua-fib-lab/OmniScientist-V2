"""User cancel stays cancel; process teardown is interrupted."""

from __future__ import annotations

import asyncio

import pytest

from omni.agent.turn_execution import TurnExecution, TurnResult


class _FakeTask:
    def __init__(self, task_id: str) -> None:
        self.id = task_id
        self.status = "running"


class _FakeTasks:
    def __init__(self, *, controls: list[dict[str, str]] | None = None) -> None:
        self.task = _FakeTask("task-042bea37")
        self.events: list[dict[str, object]] = []
        self._controls = list(controls or [])

    async def recover_consumed_controls(self, task_id: str, **kwargs: object) -> int:
        del task_id, kwargs
        return 0

    async def consume_controls(self, task_id: str, **kwargs: object) -> list[dict[str, str]]:
        del task_id, kwargs
        claimed = list(self._controls)
        self._controls.clear()
        return claimed

    async def mark_controls_applied(self, control_ids: list[str]) -> None:
        del control_ids

    async def get_task(self, task_id: str) -> _FakeTask | None:
        return self.task if task_id == self.task.id else None

    async def append_event(self, task_id: str, **payload: object) -> None:
        del task_id
        self.events.append(payload)


class _FakeController:
    def __init__(self) -> None:
        self.finished: list[dict[str, object]] = []

    async def finish_turn(self, task_id: str, **payload: object) -> None:
        del task_id
        self.finished.append(payload)


async def _persist(*args: object, **kwargs: object) -> None:
    del args, kwargs


async def _hang(user_message: str, **kwargs: object) -> TurnResult:
    del user_message, kwargs
    await asyncio.sleep(30)
    raise AssertionError("turn should have been stopped")


async def _torn_down(user_message: str, **kwargs: object) -> TurnResult:
    del user_message, kwargs
    raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_teardown_cancelled_error_settles_interrupted() -> None:
    tasks = _FakeTasks()
    controller = _FakeController()
    execution = TurnExecution(tasks, controller, _persist)

    result = await execution.run(
        execute=_torn_down,
        user_message="continue the scheduled goal",
        session_id="sess",
        existing_task_id=tasks.task.id,
        on_task_ack=None,
        execute_kwargs={},
    )

    assert result.terminated_reason == "interrupted"
    assert "owning process exited" in result.text
    assert "cancelled by user" not in result.text
    assert tasks.events[0]["event_type"] == "execution.interrupted"
    assert controller.finished[0]["task_status"] == "interrupted"


@pytest.mark.asyncio
async def test_durable_cancel_still_settles_cancelled() -> None:
    tasks = _FakeTasks(
        controls=[{"id": "ctl-1", "action": "cancel", "instruction": ""}],
    )
    controller = _FakeController()
    execution = TurnExecution(tasks, controller, _persist)

    result = await execution.run(
        execute=_hang,
        user_message="stop this turn",
        session_id="sess",
        existing_task_id=tasks.task.id,
        on_task_ack=None,
        execute_kwargs={},
    )

    assert result.terminated_reason == "cancelled"
    assert result.text.startswith("Execution cancelled.")
    assert tasks.events[0]["event_type"] == "execution.cancelled"
    assert controller.finished[0]["task_status"] == "cancelled"
