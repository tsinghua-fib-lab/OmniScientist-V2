"""Codex-style input routing while a foreground turn is active."""

from __future__ import annotations

import asyncio
import os
from collections import deque
from types import SimpleNamespace

import pytest
from prompt_toolkit.keys import Keys
from sqlalchemy import select, update

from omni.agent import OmniAgent
from omni.cli.repl_tui import ReplSubmission, ReplTui
from omni.config import load_settings
from omni.runtime import task_recorder as task_recorder_module
from omni.storage.models import TaskControlORM


def test_current_process_liveness_probe_is_non_destructive() -> None:
    """Windows CI guards against accidentally routing PID probes to TerminateProcess."""
    assert task_recorder_module._process_is_alive(os.getpid()) is True


async def _open_react_steering(agent: OmniAgent, task_id: str) -> None:
    await agent.tasks.record_plan(
        task_id,
        {
            "intent_type": "react_fallback",
            "user_message": "test semantic loop",
        },
        status="validated",
        emit_event=False,
    )


@pytest.mark.asyncio
async def test_busy_enter_steers_and_explicit_queue_targets_next_turn() -> None:
    tui = ReplTui(commands=())

    assert tui.accept_text("first turn")
    initial = await tui.read_submission_async()
    assert initial.disposition == "submit"

    tui.set_busy(True)
    assert tui.accept_text("use only primary sources")
    steer = await tui.read_submission_async()
    assert steer.disposition == "steer"

    assert tui.accept_text("summarize next", disposition="queue")
    queued = await tui.read_submission_async()
    assert queued.disposition == "queue"

    assert tui.accept_text("/queue audit the citations next")
    slash_queued = await tui.read_submission_async()
    assert slash_queued.text == "audit the citations next"
    assert slash_queued.disposition == "queue"


@pytest.mark.asyncio
async def test_busy_stop_is_a_control_submission() -> None:
    tui = ReplTui(commands=("/stop",))
    tui.set_busy(True)

    assert tui.request_stop()
    submitted = await tui.read_submission_async()

    assert submitted.text == "/stop"
    assert submitted.disposition == "control"


@pytest.mark.asyncio
async def test_repeated_stop_forces_only_the_current_turn_and_resets_next_turn() -> None:
    from omni.cli.main import _monitor_foreground_turn, _ReplControls

    controls: list[tuple[str, str]] = []
    second_cooperative_stop = asyncio.Event()

    class Tasks:
        async def request_control(
            self,
            task_id: str,
            *,
            action: str,
            instruction: str = "",
        ) -> None:
            del instruction
            controls.append((task_id, action))
            if len(controls) == 2:
                second_cooperative_stop.set()

    async def first_turn() -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "first turn force-cancelled"

    async def second_turn() -> str:
        await second_cooperative_stop.wait()
        return "second turn cooperatively cancelled"

    tui = ReplTui(commands=("/stop",))
    tui.set_busy(True)
    try:
        first_monitor = asyncio.create_task(
            _monitor_foreground_turn(
                asyncio.create_task(first_turn()),
                tui=tui,
                agent=SimpleNamespace(tasks=Tasks()),
                task_ref={"task_id": "task-first"},
                state=SimpleNamespace(),
                session_id="session-1",
                controls=_ReplControls(
                    interaction_mode="auto",
                    display_verbosity="normal",
                ),
            )
        )
        await asyncio.sleep(0)
        assert tui.request_stop()
        for _ in range(10):
            await asyncio.sleep(0)
            if controls:
                break
        assert controls == [("task-first", "cancel")]
        assert not first_monitor.done()

        assert tui.request_stop()
        first_outcome = await asyncio.wait_for(first_monitor, timeout=1.0)
        assert first_outcome.turn == "first turn force-cancelled"
        assert tui._runtime_status == "forcing stop"

        second_monitor = asyncio.create_task(
            _monitor_foreground_turn(
                asyncio.create_task(second_turn()),
                tui=tui,
                agent=SimpleNamespace(tasks=Tasks()),
                task_ref={"task_id": "task-second"},
                state=SimpleNamespace(),
                session_id="session-1",
                controls=_ReplControls(
                    interaction_mode="auto",
                    display_verbosity="normal",
                ),
            )
        )
        await asyncio.sleep(0)
        assert tui.request_stop()
        second_outcome = await asyncio.wait_for(second_monitor, timeout=1.0)

        assert second_outcome.turn == "second turn cooperatively cancelled"
        assert controls == [
            ("task-first", "cancel"),
            ("task-second", "cancel"),
        ]
        assert tui._runtime_status == "stopping"
    finally:
        tui.set_busy(False)


@pytest.mark.asyncio
async def test_busy_tab_queues_and_escape_requests_stop() -> None:
    tui = ReplTui(commands=())
    tui.set_busy(True)

    tui._input_buffer.insert_text("run this after the current turn")
    tab = tui._app.key_bindings.get_bindings_for_keys((Keys.ControlI,))[-1]
    tab.handler(SimpleNamespace(current_buffer=tui._input_buffer))
    queued = await tui.read_submission_async()

    assert queued.text == "run this after the current turn"
    assert queued.disposition == "queue"

    escape = tui._app.key_bindings.get_bindings_for_keys((Keys.Escape,))[-1]
    escape.handler(SimpleNamespace(current_buffer=tui._input_buffer))
    stopped = await tui.read_submission_async()

    assert stopped.text == "/stop"
    assert stopped.disposition == "control"
    assert "Enter steer" in tui.footer_text()
    assert "Tab queue" in tui.footer_text()
    assert "Esc stop" in tui.footer_text()


@pytest.mark.asyncio
async def test_terminal_task_atomically_rejects_new_control(tmp_path) -> None:
    agent = await OmniAgent.create(load_settings(cwd=tmp_path))
    try:
        task = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="finish immediately",
        )
        await agent.tasks.finish_task(task.id, status="cancelled")

        rejected = await agent.tasks.try_request_control(
            task.id,
            action="steer",
            instruction="too late",
        )

        assert rejected is None
        assert await agent.tasks.consume_controls(task.id) == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_unapplied_steer_is_durably_requeued_and_old_task_cannot_reconsume(
    tmp_path,
) -> None:
    agent = await OmniAgent.create(load_settings(cwd=tmp_path))
    try:
        task = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="finish while steer is pending",
        )
        await _open_react_steering(agent, task.id)
        control = await agent.tasks.request_control(
            task.id,
            action="steer",
            instruction="compare the ablation",
        )
        claimed = await agent.tasks.consume_controls(task.id)
        assert [item["id"] for item in claimed] == [control.id]

        assert await agent.tasks.requeue_unapplied_control(control.id) is True
        assert await agent.tasks.control_status(control.id) == "requeued"
        assert await agent.tasks.consume_controls(task.id) == []
        assert await agent.tasks.requeue_unapplied_control(control.id) is False
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_resumed_task_recovers_only_unacknowledged_consumed_steer(
    tmp_path,
) -> None:
    agent = await OmniAgent.create(load_settings(cwd=tmp_path))
    try:
        task = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="resume after worker interruption",
        )
        await _open_react_steering(agent, task.id)
        control = await agent.tasks.request_control(
            task.id,
            action="steer",
            instruction="use the newer evidence",
        )
        assert len(await agent.tasks.consume_controls(task.id)) == 1
        assert await agent.tasks.control_status(control.id) == "consumed"

        assert (
            await agent.tasks.recover_consumed_controls(
                task.id,
                stale_after_s=0,
            )
            == 1
        )
        replay = await agent.tasks.consume_controls(task.id)
        assert [item["id"] for item in replay] == [control.id]
        await agent.tasks.mark_controls_applied([control.id])

        assert await agent.tasks.recover_consumed_controls(task.id) == 0
        assert await agent.tasks.consume_controls(task.id) == []
        assert await agent.tasks.control_status(control.id) == "applied"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_resumed_task_does_not_recover_consumed_cancel(
    tmp_path,
) -> None:
    agent = await OmniAgent.create(load_settings(cwd=tmp_path))
    try:
        task = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="resume must not replay cancel",
        )
        await _open_react_steering(agent, task.id)
        control = await agent.tasks.request_control(task.id, action="cancel")
        assert len(await agent.tasks.consume_controls(task.id, actions={"cancel"})) == 1
        assert await agent.tasks.control_status(control.id) == "consumed"

        async with agent.db.session() as session:
            await session.execute(
                update(TaskControlORM)
                .where(TaskControlORM.id == control.id)
                .values(consumer_pid=2_147_483_647)
            )
            await session.commit()

        assert await agent.tasks.recover_consumed_controls(task.id, stale_after_s=0) == 0
        assert await agent.tasks.control_status(control.id) == "consumed"
        assert await agent.tasks.consume_controls(task.id, actions={"cancel"}) == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_control_ownership_and_audit_event_commit_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    agent = await OmniAgent.create(load_settings(cwd=tmp_path))

    def fail_event(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        del args, kwargs
        raise RuntimeError("injected event persistence failure")

    try:
        task = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="exercise control transaction rollback",
        )
        await _open_react_steering(agent, task.id)
        with monkeypatch.context() as patch:
            patch.setattr(task_recorder_module, "_event_row", fail_event)
            with pytest.raises(
                RuntimeError,
                match="injected event persistence failure",
            ):
                await agent.tasks.request_control(
                    task.id,
                    action="steer",
                    instruction="must not outlive a failed audit write",
                )
        async with agent.db.session() as session:
            orphan = (
                await session.execute(
                    select(TaskControlORM).where(
                        TaskControlORM.task_id == task.id
                    )
                )
            ).scalar_one_or_none()
        assert orphan is None

        control = await agent.tasks.request_control(
            task.id,
            action="steer",
            instruction="remain pending if consume audit fails",
        )
        with monkeypatch.context() as patch:
            patch.setattr(task_recorder_module, "_event_row", fail_event)
            with pytest.raises(
                RuntimeError,
                match="injected event persistence failure",
            ):
                await agent.tasks.consume_controls(task.id)
        assert await agent.tasks.control_status(control.id) == "pending"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_control_settlement_rolls_back_with_audit_and_dead_owner_recovers(
    tmp_path,
    monkeypatch,
) -> None:
    agent = await OmniAgent.create(load_settings(cwd=tmp_path))

    def fail_event(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        del args, kwargs
        raise RuntimeError("injected event persistence failure")

    try:
        task = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="recover a dead control consumer",
        )
        await _open_react_steering(agent, task.id)
        control = await agent.tasks.request_control(
            task.id,
            action="steer",
            instruction="survive every settlement boundary",
        )
        assert len(await agent.tasks.consume_controls(task.id)) == 1

        with monkeypatch.context() as patch:
            patch.setattr(task_recorder_module, "_event_row", fail_event)
            with pytest.raises(RuntimeError):
                await agent.tasks.mark_controls_applied([control.id])
        assert await agent.tasks.control_status(control.id) == "consumed"

        with monkeypatch.context() as patch:
            patch.setattr(task_recorder_module, "_event_row", fail_event)
            with pytest.raises(RuntimeError):
                await agent.tasks.requeue_unapplied_control(control.id)
        assert await agent.tasks.control_status(control.id) == "consumed"

        # The current process still owns the fresh claim, so another live
        # execution cannot steal it before the lease expires.
        assert await agent.tasks.recover_consumed_controls(task.id) == 0
        async with agent.db.session() as session:
            await session.execute(
                update(TaskControlORM)
                .where(TaskControlORM.id == control.id)
                .values(consumer_pid=2_147_483_647)
            )
            await session.commit()

        with monkeypatch.context() as patch:
            patch.setattr(task_recorder_module, "_event_row", fail_event)
            with pytest.raises(RuntimeError):
                await agent.tasks.recover_consumed_controls(task.id)
        assert await agent.tasks.control_status(control.id) == "consumed"

        assert await agent.tasks.recover_consumed_controls(task.id) == 1
        replay = await agent.tasks.consume_controls(task.id)
        assert [item["id"] for item in replay] == [control.id]
        await agent.tasks.mark_controls_applied([control.id])
        assert await agent.tasks.recover_consumed_controls(task.id) == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_deterministic_control_poll_claims_cancel_but_leaves_steer_pending(
    tmp_path,
) -> None:
    agent = await OmniAgent.create(load_settings(cwd=tmp_path))
    try:
        task = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="run a deterministic workflow",
        )
        await _open_react_steering(agent, task.id)
        steer = await agent.tasks.request_control(
            task.id,
            action="steer",
            instruction="change the interpretation",
        )
        await agent.tasks.record_plan(
            task.id,
            {
                "intent_type": "workflow",
                "user_message": task.user_input,
            },
            status="validated",
            emit_event=False,
        )
        cancel = await agent.tasks.request_control(
            task.id,
            action="cancel",
        )

        claimed = await agent.tasks.consume_controls(
            task.id,
            actions={"cancel"},
        )

        assert [item["id"] for item in claimed] == [cancel.id]
        assert await agent.tasks.control_status(cancel.id) == "consumed"
        assert await agent.tasks.control_status(steer.id) == "pending"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_final_steering_boundary_is_monotonic_and_insert_is_gated(
    tmp_path,
) -> None:
    agent = await OmniAgent.create(load_settings(cwd=tmp_path))
    try:
        task = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="close steering before later audit events",
        )
        await _open_react_steering(agent, task.id)
        assert await agent.tasks.steer_rejection_reason(task.id) == ""

        await agent.tasks.append_event(
            task.id,
            event_type="execution.finished",
            status="succeeded",
            name="react",
            summary="semantic loop ended",
        )
        await agent.tasks.append_event(
            task.id,
            event_type="cost.usage",
            status="succeeded",
            name="cost",
            output_json={"total_tokens": 42},
            summary="cost recorded after execution",
        )
        # A late plan/audit write cannot reopen a sealed execution epoch.
        await _open_react_steering(agent, task.id)

        assert "passed its final steering boundary" in (
            await agent.tasks.steer_rejection_reason(task.id)
        )
        assert (
            await agent.tasks.try_request_control(
                task.id,
                action="steer",
                instruction="must become a follow-up",
            )
            is None
        )
        async with agent.db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(TaskControlORM).where(
                            TaskControlORM.task_id == task.id
                        )
                    )
                ).scalars()
            )
        assert rows == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_detached_steer_rejects_runtimes_without_semantic_boundary(
    tmp_path,
) -> None:
    agent = await OmniAgent.create(load_settings(cwd=tmp_path))
    try:
        task = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="run a deterministic workflow",
        )
        assert "still planning" in (
            await agent.tasks.steer_rejection_reason(task.id)
        )

        await agent.tasks.record_plan(
            task.id,
            {
                "intent_type": "workflow",
                "user_message": task.user_input,
            },
            status="validated",
            emit_event=False,
        )
        assert "no model steering boundary" in (
            await agent.tasks.steer_rejection_reason(task.id)
        )

        await agent.tasks.record_plan(
            task.id,
            {
                "intent_type": "react_fallback",
                "user_message": task.user_input,
            },
            status="validated",
            emit_event=False,
        )
        assert await agent.tasks.steer_rejection_reason(task.id) == ""
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_rejected_same_tick_steer_falls_back_to_next_queue_once() -> None:
    from omni.cli.main import _monitor_foreground_turn, _ReplControls

    finished = asyncio.Event()
    attempts: list[tuple[str, str]] = []

    class Tasks:
        async def try_request_control(
            self,
            task_id: str,
            *,
            action: str,
            instruction: str = "",
        ) -> object | None:
            attempts.append((action, instruction))
            finished.set()
            return None

    async def running_turn() -> str:
        await finished.wait()
        return "completed"

    tui = ReplTui(commands=())
    tui.set_busy(True)
    tui._submissions.put_nowait(
        ReplSubmission(
            turn_id="steer-1",
            text="also compare the ablation",
            disposition="steer",
        )
    )
    outcome = await _monitor_foreground_turn(
        asyncio.create_task(running_turn()),
        tui=tui,
        agent=SimpleNamespace(tasks=Tasks()),
        task_ref={"task_id": "task-1"},
        state=SimpleNamespace(),
        session_id="session-1",
        controls=_ReplControls(
            interaction_mode="auto",
            display_verbosity="normal",
        ),
    )

    assert attempts == [("steer", "also compare the ablation")]
    assert [item.text for item in outcome.queued_lines] == [
        "also compare the ablation"
    ]


@pytest.mark.asyncio
async def test_queued_input_survives_a_failing_current_turn_exactly_once() -> None:
    from omni.cli.main import _monitor_foreground_turn, _ReplControls

    async def failing_turn() -> None:
        await asyncio.sleep(0.01)
        raise RuntimeError("current turn failed")

    tui = ReplTui(commands=())
    tui.set_busy(True)
    tui._submissions.put_nowait(
        ReplSubmission(
            turn_id="queue-1",
            text="run after the failure",
            disposition="queue",
        )
    )

    outcome = await _monitor_foreground_turn(
        asyncio.create_task(failing_turn()),
        tui=tui,
        agent=SimpleNamespace(tasks=SimpleNamespace()),
        task_ref={"task_id": "task-1"},
        state=SimpleNamespace(),
        session_id="session-1",
        controls=_ReplControls(
            interaction_mode="auto",
            display_verbosity="normal",
        ),
    )

    assert isinstance(outcome.turn_error, RuntimeError)
    assert [item.text for item in outcome.queued_lines] == ["run after the failure"]


@pytest.mark.asyncio
async def test_unapplied_steer_survives_a_failing_current_turn_exactly_once() -> None:
    from omni.cli.main import _monitor_foreground_turn, _ReplControls

    accepted = asyncio.Event()

    class Tasks:
        async def try_request_control(
            self,
            task_id: str,
            *,
            action: str,
            instruction: str = "",
        ) -> object:
            del task_id, action, instruction
            accepted.set()
            return SimpleNamespace(id="control-1")

        async def control_status(self, control_id: str) -> str:
            assert control_id == "control-1"
            return "consumed"

    async def failing_turn() -> None:
        await accepted.wait()
        await asyncio.sleep(0)
        raise RuntimeError("current turn failed")

    tui = ReplTui(commands=())
    tui.set_busy(True)
    tui._submissions.put_nowait(
        ReplSubmission(
            turn_id="steer-1",
            text="preserve this instruction",
            disposition="steer",
        )
    )

    outcome = await _monitor_foreground_turn(
        asyncio.create_task(failing_turn()),
        tui=tui,
        agent=SimpleNamespace(tasks=Tasks()),
        task_ref={"task_id": "task-1"},
        state=SimpleNamespace(),
        session_id="session-1",
        controls=_ReplControls(
            interaction_mode="auto",
            display_verbosity="normal",
        ),
    )

    assert isinstance(outcome.turn_error, RuntimeError)
    assert [item.text for item in outcome.queued_lines] == [
        "preserve this instruction"
    ]


@pytest.mark.asyncio
async def test_locally_delivered_steer_is_not_duplicated_when_ack_store_lags() -> None:
    from omni.cli.main import _monitor_foreground_turn, _ReplControls

    accepted = asyncio.Event()

    class Tasks:
        async def try_request_control(
            self,
            task_id: str,
            *,
            action: str,
            instruction: str = "",
        ) -> object:
            del task_id, action, instruction
            accepted.set()
            return SimpleNamespace(id="control-1")

        async def control_status(self, control_id: str) -> str:
            assert control_id == "control-1"
            return "consumed"

    async def delivered_turn() -> object:
        await accepted.wait()
        await asyncio.sleep(0)
        return SimpleNamespace(_delivered_control_ids=("control-1",))

    tui = ReplTui(commands=())
    tui.set_busy(True)
    tui._submissions.put_nowait(
        ReplSubmission(
            turn_id="steer-1",
            text="do not run me twice",
            disposition="steer",
        )
    )

    outcome = await _monitor_foreground_turn(
        asyncio.create_task(delivered_turn()),
        tui=tui,
        agent=SimpleNamespace(tasks=Tasks()),
        task_ref={"task_id": "task-1"},
        state=SimpleNamespace(),
        session_id="session-1",
        controls=_ReplControls(
            interaction_mode="auto",
            display_verbosity="normal",
        ),
    )

    assert outcome.turn is not None
    assert outcome.queued_lines == []


def test_settling_failed_turn_preserves_queue_for_next_loop_iteration() -> None:
    from omni.cli.main import (
        _ForegroundTurnOutcome,
        _settle_foreground_outcome,
    )

    tui = ReplTui(commands=())
    pending = deque()
    queued = ReplSubmission(
        turn_id="queue-1",
        text="execute exactly once next",
        disposition="queue",
    )

    turn, turn_error = _settle_foreground_outcome(
        pending,
        tui,
        _ForegroundTurnOutcome(
            turn=None,
            queued_lines=[queued],
            turn_error=RuntimeError("current turn failed"),
        ),
    )

    assert turn is None
    assert isinstance(turn_error, RuntimeError)
    assert pending.popleft().text == "execute exactly once next"
    assert not pending


def test_settling_failed_turn_without_queue_keeps_repl_recoverable() -> None:
    from omni.cli.main import (
        _ForegroundTurnOutcome,
        _settle_foreground_outcome,
    )

    turn, turn_error = _settle_foreground_outcome(
        deque(),
        ReplTui(commands=()),
        _ForegroundTurnOutcome(
            turn=None,
            queued_lines=[],
            turn_error=RuntimeError("current turn failed"),
        ),
    )

    assert turn is None
    assert isinstance(turn_error, RuntimeError)


def test_settling_cancelled_turn_propagates_after_committing_queue() -> None:
    from omni.cli.main import (
        _ForegroundTurnOutcome,
        _settle_foreground_outcome,
    )

    pending = deque()
    queued = ReplSubmission(
        turn_id="queue-1",
        text="preserved before cancellation propagates",
        disposition="queue",
    )

    with pytest.raises(asyncio.CancelledError):
        _settle_foreground_outcome(
            pending,
            ReplTui(commands=()),
            _ForegroundTurnOutcome(
                turn=None,
                queued_lines=[queued],
                turn_error=asyncio.CancelledError(),
            ),
        )

    assert [item.text for item in pending] == [
        "preserved before cancellation propagates"
    ]
