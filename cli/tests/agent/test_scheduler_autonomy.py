"""Scheduled-run autonomy + traceability.

A schedule fires under ``omni serve`` where there is *no* interactive approver,
so without a grant every sensitive tool fails closed and the run produces
nothing (the deployed regression the user hit). The fix makes each fire a
first-class owning task carrying the schedule's ``approved_tools`` grant, so the
approval gate clears exactly those tools through its preauthorizer — reusing the
same ``TaskORM.approved_tools`` path a human uses to "approve for this task" —
and links every run back to its schedule for ``/schedule show`` history.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.agent import OmniAgent
from omni.agent.interaction_lifecycle import build_approval_gate
from omni.config import load_settings
from omni.runtime.scheduler import autonomy_tools


def test_autonomy_tools_maps_modes_and_falls_back_to_standard():
    settings = load_settings()
    settings.schedules.autonomy = "off"
    assert autonomy_tools(settings) == []
    settings.schedules.autonomy = "standard"
    assert autonomy_tools(settings) == ["write_file", "edit_file", "run_compute"]
    settings.schedules.autonomy = "full"
    assert autonomy_tools(settings) == ["write_file", "edit_file", "run_compute", "bash"]
    settings.schedules.autonomy = "not-a-mode"  # unknown → safe standard grant
    assert autonomy_tools(settings) == ["write_file", "edit_file", "run_compute"]


def test_schema_exposes_scheduling_columns():
    from omni.storage.models import ScheduleORM, SubtaskORM

    assert "approved_tools" in ScheduleORM.__table__.columns
    assert "schedule_id" in SubtaskORM.__table__.columns


@pytest.mark.asyncio
async def test_add_seeds_autonomy_default_and_honours_explicit_grant():
    agent = await OmniAgent.create(load_settings())
    try:
        sid = await agent.scheduler.add("agent-goal", {"input": "x"}, kind="interval", interval_s=3600)
        sched = await agent.scheduler.get(sid)
        assert set(sched.approved_tools) == {"write_file", "edit_file", "run_compute"}

        # An explicit list overrides the default…
        sid2 = await agent.scheduler.add(
            "agent-goal", {"input": "y"}, kind="interval", interval_s=3600, approved_tools=["bash"]
        )
        assert (await agent.scheduler.get(sid2)).approved_tools == ["bash"]

        # …and an explicit empty list means fail-closed for that schedule.
        sid3 = await agent.scheduler.add(
            "agent-goal", {"input": "z"}, kind="interval", interval_s=3600, approved_tools=[]
        )
        assert (await agent.scheduler.get(sid3)).approved_tools == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_run_due_materialises_owning_task_with_grant_and_schedule_link():
    # This isolates the grant/traceability mechanics of a fire's owning task,
    # which are identical whether the fire runs as a full headless turn or a
    # direct skill. Pin the direct-skill path so ``fired[0]`` is the run's
    # subtask (the headless-turn end-to-end chain is covered in
    # ``test_schedule_tools.py``).
    settings = load_settings()
    settings.schedules.execution_mode = "skill"
    agent = await OmniAgent.create(settings)
    try:
        now = datetime.now(UTC)
        sid = await agent.scheduler.add(
            "agent-goal", {"input": "go"}, kind="interval", interval_s=3600,
            first_due=now - timedelta(seconds=1),
        )
        fired = await agent.scheduler.run_due(now=now)
        assert len(fired) == 1

        sub = await agent.runtime.get_subtask(fired[0])
        assert sub is not None
        # The fire is traceable back to its schedule and to a first-class task.
        assert sub.schedule_id == sid
        assert sub.task_id

        task = await agent.tasks.get_task(sub.task_id)
        assert task is not None
        assert set(task.approved_tools) == {"write_file", "edit_file", "run_compute"}

        # run history surfaces the fired run for `/schedule show`.
        runs = await agent.scheduler.runs(sid)
        assert [r.id for r in runs] == [sub.id]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_scheduled_grant_clears_gate_without_an_approver():
    """The exact daemon condition: approver=None, but the schedule's owning task
    pre-authorises write_file — so the gate clears it while a non-granted
    sensitive tool (bash) still fails closed."""
    settings = load_settings()
    settings.schedules.execution_mode = "skill"  # isolate the gate/grant mechanics
    agent = await OmniAgent.create(settings)
    try:
        now = datetime.now(UTC)
        await agent.scheduler.add(
            "agent-goal", {"input": "go"}, kind="interval", interval_s=3600,
            first_due=now - timedelta(seconds=1),
        )
        fired = await agent.scheduler.run_due(now=now)
        sub = await agent.runtime.get_subtask(fired[0])

        gate = build_approval_gate(
            settings=agent.settings,
            tasks=agent.tasks,
            approver=None,  # daemon / non-interactive: no owner to ask
            session_allow={},
            task_id=sub.task_id,
            channel="cli",
            session_id="",
        )

        calls = {"n": 0}

        async def invoker():
            calls["n"] += 1
            return {"ok": True}

        granted = await gate.invoke("write_file", {"path": "out.txt", "content": "hi"}, invoker, sensitive=True)
        assert calls["n"] == 1  # invoked → the grant cleared the gate
        assert granted == {"ok": True}

        # bash was NOT granted by the standard autonomy default → still blocked.
        await gate.invoke("bash", {"command": "ls"}, invoker, sensitive=True)
        assert calls["n"] == 1  # invoker not called again
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_autonomy_off_leaves_scheduled_run_fail_closed():
    settings = load_settings()
    settings.schedules.autonomy = "off"
    settings.schedules.execution_mode = "skill"  # isolate the grant path
    agent = await OmniAgent.create(settings)
    try:
        now = datetime.now(UTC)
        sid = await agent.scheduler.add(
            "agent-goal", {"input": "go"}, kind="interval", interval_s=3600,
            first_due=now - timedelta(seconds=1),
        )
        assert (await agent.scheduler.get(sid)).approved_tools == []
        fired = await agent.scheduler.run_due(now=now)
        sub = await agent.runtime.get_subtask(fired[0])
        task = await agent.tasks.get_task(sub.task_id)
        # A first-class task still exists (visible in /task) but grants nothing.
        assert task is not None
        assert list(task.approved_tools or []) == []
    finally:
        await agent.aclose()
