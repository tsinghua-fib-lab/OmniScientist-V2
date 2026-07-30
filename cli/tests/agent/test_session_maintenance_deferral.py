"""Session end owes memory maintenance; it does not perform it.

Consolidation costs several model round trips (fact extraction, profile merge,
memory-file compaction) under a cross-process lock. Doing that while the user
waits to leave means the pass reliably outlives any shutdown budget, gets
cancelled halfway, and leaves its own run open forever — the cost is paid every
time and the result almost never lands. So session end parks the work and a
later drain claims and runs it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from omni.agent.orchestrator import OmniAgent
from omni.config.settings import load_settings
from omni.memory.service import MemoryLayer
from tests.conftest import ScriptedLLM

_TERMINAL = {"succeeded", "degraded", "failed", "cancelled", "interrupted"}


async def _agent_with_a_real_looking_provider() -> tuple[OmniAgent, ScriptedLLM]:
    """An agent whose memory passes would really call a model if asked."""
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = ScriptedLLM()
    agent.llm = llm
    agent.memory._llm = llm
    agent.turn_memory._llm = llm
    settings.model.provider = "openai_compatible"
    return agent, llm


async def _a_preference_worth_folding(agent: OmniAgent) -> None:
    """Give the profile merge something to do, so it would call the model."""
    await agent.memory.record(
        layer=MemoryLayer.SEMANTIC,
        scope="user",
        scope_id="local",
        summary="I prefer polars over pandas",
        memory_type="preference",
        importance=0.8,
    )


@pytest.mark.asyncio
async def test_ending_a_session_parks_the_work_instead_of_paying_for_it() -> None:
    """Enqueueing must cost no model call — that is the whole point of it."""
    agent, llm = await _agent_with_a_real_looking_provider()
    try:
        session_id = await agent.ensure_session()
        await _a_preference_worth_folding(agent)
        calls_before = llm.calls

        task_id = await agent.enqueue_session_maintenance(session_id)

        assert task_id, "parking the work must name the run that owes it"
        assert llm.calls == calls_before, "session end called the model"
        parked = await agent.tasks.get_task(task_id)
        assert parked is not None
        assert parked.status == "pending"
        assert parked.kind == "maintenance"
        assert parked.session_id == session_id
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_a_parked_pass_is_not_swept_away_as_a_dead_worker() -> None:
    """Parked work waits for a drain, however long that takes.

    The stale sweep settles tasks whose worker looks dead. Parked maintenance
    has no worker yet, so it must not look dead to the sweep — otherwise the
    queue empties itself into ``interrupted`` before anyone runs it.
    """
    agent, _ = await _agent_with_a_real_looking_provider()
    try:
        session_id = await agent.ensure_session()
        task_id = await agent.enqueue_session_maintenance(session_id)

        await agent.tasks.reconcile_stale_tasks(stale_after_s=0.001)

        still_parked = await agent.tasks.get_task(task_id)
        assert still_parked is not None
        assert still_parked.status == "pending"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_draining_runs_the_pass_and_settles_the_run() -> None:
    """What exit stopped doing, the drain does — and it finishes the run."""
    agent, _ = await _agent_with_a_real_looking_provider()
    try:
        session_id = await agent.ensure_session()
        await _a_preference_worth_folding(agent)
        task_id = await agent.enqueue_session_maintenance(session_id)

        drained = await agent.drain_pending_maintenance(limit=5)

        assert drained == 1
        settled = await agent.tasks.get_task(task_id)
        assert settled is not None
        assert settled.status in _TERMINAL
        events = await agent.tasks.list_events(task_id)
        assert any(event.event_type == "maintenance.completed" for event in events)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_two_drainers_do_not_run_the_same_pass_twice() -> None:
    """Concurrent windows share one queue, so a claim has to be exclusive."""
    agent, _ = await _agent_with_a_real_looking_provider()
    try:
        session_id = await agent.ensure_session()
        await agent.enqueue_session_maintenance(session_id)

        first, second = await asyncio.gather(
            agent.tasks.claim_pending_maintenance(limit=5),
            agent.tasks.claim_pending_maintenance(limit=5),
        )

        assert len(first) + len(second) == 1, "the same parked pass was claimed twice"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_draining_clears_the_wreckage_of_earlier_drains() -> None:
    """Passes killed mid-flight must not stay ``running`` forever.

    The general stale sweep only runs inside a service, so an interactive-only
    workspace accumulated them: fifty of fifty-nine maintenance runs in a real
    one had never reached a terminal status.
    """
    agent, _ = await _agent_with_a_real_looking_provider()
    try:
        session_id = await agent.ensure_session()
        orphan = await agent.tasks.create_task(
            session_id=session_id,
            channel="maintenance",
            user_input="session memory maintenance",
            kind="maintenance",
        )
        assert orphan.status == "running"

        await agent.drain_pending_maintenance(limit=5, stale_after_s=0.001)

        settled = await agent.tasks.get_task(orphan.id)
        assert settled is not None
        assert settled.status in _TERMINAL, f"orphan left as {settled.status!r}"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_something_actually_drains_the_queue() -> None:
    """Parking is only safe because two consumers pick the work back up.

    A window drains beside its prompt; a service drains on the runtime tick. If
    neither is wired up, deferral is just a leak with better manners.
    """
    from omni.runtime.memory_maintenance import maintenance_tick, spawn_maintenance_drain

    agent, _ = await _agent_with_a_real_looking_provider()
    try:
        session_id = await agent.ensure_session()
        parked_for_window = await agent.enqueue_session_maintenance(session_id)
        handle = spawn_maintenance_drain(agent, delay_s=0.0, interval_s=3600)
        assert handle is not None
        for _ in range(200):
            await asyncio.sleep(0.02)
            row = await agent.tasks.get_task(parked_for_window)
            if row is not None and row.status in _TERMINAL:
                break
        handle.cancel()
        window_drained = await agent.tasks.get_task(parked_for_window)
        assert window_drained is not None
        assert window_drained.status in _TERMINAL, "the window never drained the queue"

        parked_for_service = await agent.enqueue_session_maintenance(session_id)
        assert await maintenance_tick(agent, interval_s=0.0)() == 1
        service_drained = await agent.tasks.get_task(parked_for_service)
        assert service_drained is not None
        assert service_drained.status in _TERMINAL
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_the_service_tick_keeps_its_own_clock() -> None:
    """The runtime ticks far more often than this work needs to run."""
    from omni.runtime.memory_maintenance import maintenance_tick

    agent, _ = await _agent_with_a_real_looking_provider()
    try:
        session_id = await agent.ensure_session()
        await agent.enqueue_session_maintenance(session_id)
        tick = maintenance_tick(agent, interval_s=3600)

        assert await tick() == 1
        await agent.enqueue_session_maintenance(session_id)
        assert await tick() == 0, "the tick drained twice inside its own interval"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_the_first_tick_drains_on_a_freshly_booted_host(monkeypatch) -> None:
    """``time.monotonic`` counts from boot, so uptime must not seed the clock.

    Starting the tick's clock at a plain zero made "time since the last drain"
    equal to the host's uptime, so on any machine up for less than the interval
    the first tick concluded it had already run and drained nothing. Every CI
    runner is such a machine, and so is any service that starts at boot — which
    is exactly where the parked queue has the most waiting for it. The sibling
    test above cannot catch this: on a developer's long-running machine the
    uptime dwarfs the interval and the bug is invisible.
    """
    from omni.runtime import memory_maintenance

    monkeypatch.setattr(
        memory_maintenance, "time", SimpleNamespace(monotonic=lambda: 12.0)
    )

    agent, _ = await _agent_with_a_real_looking_provider()
    try:
        session_id = await agent.ensure_session()
        await agent.enqueue_session_maintenance(session_id)
        tick = memory_maintenance.maintenance_tick(agent, interval_s=3600)

        assert await tick() == 1, "a host booted 12s ago never drained its queue"
        await agent.enqueue_session_maintenance(session_id)
        assert await tick() == 0, "the tick drained twice inside its own interval"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_a_cancelled_pass_still_settles_its_run() -> None:
    """A drain that is cut off must not leave the run open forever.

    This is the defect that filled real workspaces with maintenance runs stuck
    in ``running``: settlement sat after the expensive part, so cancellation
    skipped it.
    """
    agent, _ = await _agent_with_a_real_looking_provider()
    try:
        session_id = await agent.ensure_session()
        task_id = await agent.enqueue_session_maintenance(session_id)

        async def never_finishes(*_args, **_kwargs) -> list[str]:
            await asyncio.sleep(3600)
            return []

        agent.turn_memory.consolidate = never_finishes  # type: ignore[method-assign]

        running = asyncio.create_task(
            agent.turn_memory.run_session_maintenance(
                session_id, maintenance_task_id=task_id
            )
        )
        await asyncio.sleep(0.05)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(0.05)

        settled = await agent.tasks.get_task(task_id)
        assert settled is not None
        assert settled.status in _TERMINAL, f"left open as {settled.status!r}"
    finally:
        await agent.aclose()
