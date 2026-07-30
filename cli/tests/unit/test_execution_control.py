from __future__ import annotations

import asyncio

import pytest

from omni.core.execution_control import (
    CancellationEscalator,
    ExecutionCancelled,
    ExecutionControl,
)


def test_cancellation_escalator_is_cooperative_once_then_forces_until_reset() -> None:
    escalation = CancellationEscalator()

    assert escalation.request() == "cooperative"
    assert escalation.request() == "force"
    assert escalation.request() == "force"

    escalation.reset()

    assert escalation.request() == "cooperative"


@pytest.mark.asyncio
async def test_execution_control_interrupts_an_inflight_await() -> None:
    started = asyncio.Event()
    controls: list[list[dict[str, str]]] = [[], [{"action": "cancel", "instruction": ""}]]

    async def read_controls() -> list[dict[str, str]]:
        return controls.pop(0) if controls else []

    async def work() -> str:
        started.set()
        await asyncio.sleep(30)
        return "late"

    control = ExecutionControl(read_controls, poll_interval=0.01)

    with pytest.raises(ExecutionCancelled):
        await asyncio.wait_for(control.run(work()), timeout=0.5)

    assert control.cancel_requested is True


@pytest.mark.asyncio
async def test_execution_control_preserves_steering_until_runtime_boundary() -> None:
    controls = [[{"action": "steer", "instruction": "prioritize cited evidence"}]]

    async def read_controls() -> list[dict[str, str]]:
        return controls.pop(0) if controls else []

    control = ExecutionControl(read_controls, poll_interval=0.01)
    result = await control.run(asyncio.sleep(0.03, result="done"))

    assert result == "done"
    assert control.take_steering() == ["prioritize cited evidence"]
    assert control.take_steering() == []


@pytest.mark.asyncio
async def test_nested_execution_control_has_one_owner_and_propagates_cancel() -> None:
    reads = 0

    async def read_controls() -> list[dict[str, str]]:
        nonlocal reads
        reads += 1
        return [{"action": "cancel", "instruction": ""}] if reads >= 2 else []

    control = ExecutionControl(read_controls, poll_interval=0.01)

    async def nested() -> str:
        return await control.run(asyncio.sleep(30, result="late"))

    with pytest.raises(ExecutionCancelled):
        await asyncio.wait_for(control.run(nested()), timeout=0.5)

    assert reads < 10


@pytest.mark.asyncio
async def test_terminal_steer_acknowledgement_retries_after_transient_failure() -> None:
    controls = [
        [{"id": "steer-1", "action": "steer", "instruction": "use primary sources"}]
    ]
    acknowledgements: list[list[str]] = []

    async def read_controls() -> list[dict[str, str]]:
        return controls.pop(0) if controls else []

    async def acknowledge(control_ids: list[str]) -> None:
        acknowledgements.append(control_ids)
        if len(acknowledgements) == 1:
            raise RuntimeError("transient database lock")

    control = ExecutionControl(
        read_controls,
        acknowledge_controls=acknowledge,
        poll_interval=0.01,
    )

    async def work() -> list[str]:
        await asyncio.sleep(0.02)
        return control.take_steering()

    assert await control.run(work()) == ["use primary sources"]
    assert acknowledgements == [["steer-1"], ["steer-1"]]
    assert control.delivered_control_ids == ("steer-1",)


@pytest.mark.asyncio
async def test_delivered_steer_receipt_survives_persistent_ack_store_failure() -> None:
    controls = [
        [{"id": "steer-1", "action": "steer", "instruction": "use primary sources"}]
    ]

    async def read_controls() -> list[dict[str, str]]:
        return controls.pop(0) if controls else []

    async def acknowledge(_control_ids: list[str]) -> None:
        raise RuntimeError("database unavailable")

    control = ExecutionControl(
        read_controls,
        acknowledge_controls=acknowledge,
        poll_interval=0.01,
    )

    async def work() -> list[str]:
        await asyncio.sleep(0.02)
        return control.take_steering()

    assert await control.run(work()) == ["use primary sources"]
    assert control.delivered_control_ids == ("steer-1",)
