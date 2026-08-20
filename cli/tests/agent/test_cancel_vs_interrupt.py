"""User cancel stays cancel; process teardown is interrupted."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import OperationalError

from omni.agent.turn_execution import TurnExecution, TurnResult


class _FakeTask:
    def __init__(self, task_id: str) -> None:
        self.id = task_id
        self.status = "running"


class _FakeTasks:
    def __init__(self, *, controls: list[dict[str, str]] | None = None) -> None:
        self.task = _FakeTask("task-042bea37")
        self.events: list[dict[str, object]] = []
        self.settled_cancels: list[str] = []
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

    async def settle_open_children_for_cancel(self, task_id: str) -> None:
        self.settled_cancels.append(task_id)


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
    assert [event["event_type"] for event in tasks.events] == [
        "execution.interrupted",
        "react.finished",
    ]
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
    assert [event["event_type"] for event in tasks.events] == [
        "execution.cancelled",
        "react.finished",
    ]
    assert controller.finished[0]["task_status"] == "cancelled"
    assert tasks.settled_cancels == [tasks.task.id]


async def _return_cancelled(user_message: str, **kwargs: object) -> TurnResult:
    del user_message, kwargs
    return TurnResult(
        text="stopped",
        session_id="sess",
        task_id="task-042bea37",
        kind="partial",
        terminated_reason="cancelled",
    )


@pytest.mark.asyncio
async def test_cancelled_react_result_settles_open_children() -> None:
    tasks = _FakeTasks()
    controller = _FakeController()
    execution = TurnExecution(tasks, controller, _persist)

    result = await execution.run(
        execute=_return_cancelled,
        user_message="stop this turn",
        session_id="sess",
        existing_task_id=tasks.task.id,
        on_task_ack=None,
        execute_kwargs={},
    )

    assert result.terminated_reason == "cancelled"
    assert tasks.settled_cancels == [tasks.task.id]


class _SlowSettleTasks(_FakeTasks):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def settle_open_children_for_cancel(self, task_id: str) -> None:
        self.started.set()
        await asyncio.sleep(0.2)
        await super().settle_open_children_for_cancel(task_id)


@pytest.mark.asyncio
async def test_stopped_result_survives_wait_for_cancelling_the_turn() -> None:
    """Windows release wait_for(8) cancels handle_turn mid-settle."""
    tasks = _SlowSettleTasks()
    controller = _FakeController()
    execution = TurnExecution(tasks, controller, _persist)

    running = asyncio.create_task(
        execution._stopped_result(
            tasks.task.id,
            "sess",
            "stop this turn",
            reason="cancelled",
        )
    )
    await tasks.started.wait()
    running.cancel()
    result = await running

    assert result.terminated_reason == "cancelled"
    assert tasks.settled_cancels == [tasks.task.id]
    assert [event["event_type"] for event in tasks.events] == [
        "execution.cancelled",
        "react.finished",
    ]
    assert controller.finished[0]["task_status"] == "cancelled"


@pytest.mark.asyncio
async def test_returned_cancel_settle_survives_wait_for_cancelling_the_turn() -> None:
    tasks = _SlowSettleTasks()
    controller = _FakeController()
    execution = TurnExecution(tasks, controller, _persist)

    running = asyncio.create_task(
        execution.run(
            execute=_return_cancelled,
            user_message="stop this turn",
            session_id="sess",
            existing_task_id=tasks.task.id,
            on_task_ack=None,
            execute_kwargs={},
        )
    )
    await tasks.started.wait()
    running.cancel()
    result = await running

    assert result.terminated_reason == "cancelled"
    assert tasks.settled_cancels == [tasks.task.id]


class _BusyChildTasks(_FakeTasks):
    async def settle_open_children_for_cancel(self, task_id: str) -> None:
        await super().settle_open_children_for_cancel(task_id)
        raise OperationalError(
            "UPDATE subtasks", {}, Exception("database is locked")
        )


@pytest.mark.asyncio
async def test_busy_child_settle_does_not_fail_a_cancelled_turn() -> None:
    tasks = _BusyChildTasks()
    controller = _FakeController()
    execution = TurnExecution(tasks, controller, _persist)

    result = await execution.run(
        execute=_return_cancelled,
        user_message="stop this turn",
        session_id="sess",
        existing_task_id=tasks.task.id,
        on_task_ack=None,
        execute_kwargs={},
    )

    assert result.terminated_reason == "cancelled"
    assert tasks.settled_cancels == [tasks.task.id]


class _OrderTasks(_FakeTasks):
    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []

    async def settle_open_children_for_cancel(self, task_id: str) -> None:
        self.order.append("settle")
        await super().settle_open_children_for_cancel(task_id)

    async def append_event(self, task_id: str, **payload: object) -> None:
        self.order.append("event")
        await super().append_event(task_id, **payload)

    async def finish_task(self, task_id: str, **payload: object) -> None:
        del task_id, payload
        self.order.append("finish")


@pytest.mark.asyncio
async def test_finish_turn_checkpoints_children_before_advisory_events() -> None:
    from omni.agent.task_controller import TaskController

    tasks = _OrderTasks()
    controller = TaskController(tasks)

    await controller.finish_turn(
        tasks.task.id,
        kind="partial",
        text="stopped",
        task_status="cancelled",
    )

    assert tasks.order == ["settle", "event", "finish"]
