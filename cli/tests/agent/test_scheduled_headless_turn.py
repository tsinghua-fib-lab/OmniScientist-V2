"""Headless scheduled goals: "one brain, headless door".

A due goal schedule runs the *same* planner→workflow→verification pipeline an
interactive turn uses, unattended (``origin="schedule"``): no interactive
approver, and it cannot recursively schedule more work. A run that finishes short
of its full goal (degraded/failed) triggers a bounded auto-continuation that
finishes the missing deliverables, and the verified outcome — or an honest error
— is always delivered to the origin inbox. These pin those contracts without
running a live model turn (the fire→turn→inbox chain end-to-end lives in
``test_schedule_tools.py``).
"""

from __future__ import annotations

import pytest

from omni.agent import OmniAgent
from omni.agent.turn_execution import TurnResult
from omni.config import load_settings
from omni.runtime.presentation import ArtifactRef


@pytest.mark.asyncio
async def test_scheduled_ctx_is_autonomous_and_cannot_self_schedule():
    agent = await OmniAgent.create(load_settings())
    try:
        sched = agent._make_ctx("", "cli", origin="schedule")
        assert sched.origin == "schedule"
        assert sched.autonomous is True
        # A headless run must not spawn more schedules (recursion guard) …
        assert sched.allow_scheduling is False

        interactive = agent._make_ctx("", "cli", origin="interactive")
        assert interactive.autonomous is False
        assert interactive.allow_scheduling is True
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_autonomous_turn_surface_hides_the_schedule_tool():
    """The same coordinator surface, minus scheduling, so a scheduled run can do
    real work (find/use/run skills, workflows) but cannot create schedules."""
    agent = await OmniAgent.create(load_settings())
    try:
        sched_ctx = agent._make_ctx("", "cli", origin="schedule")
        sched_tools = {t.spec.name for t in await agent._build_tools(sched_ctx, wait_for_tasks=False)}
        assert "schedule_task" not in sched_tools
        assert "run_skill" in sched_tools  # it still has the real working surface

        live_ctx = agent._make_ctx("", "cli", origin="interactive")
        live_tools = {t.spec.name for t in await agent._build_tools(live_ctx, wait_for_tasks=False)}
        assert "schedule_task" in live_tools
    finally:
        await agent.aclose()


class _ScriptedTurns:
    """Return pre-scripted TurnResults for successive headless turns."""

    def __init__(self, results: list[TurnResult]) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    async def __call__(self, *, goal: str, task_id: str, channel: str, session_id: str, grant: list[str]):
        self.calls.append({"goal": goal, "task_id": task_id, "grant": list(grant)})
        return self._results[min(len(self.calls) - 1, len(self._results) - 1)]


def _turn(
    status: str,
    *,
    task_id: str = "t",
    warnings: list[str] | None = None,
    kind: str = "text",
    artifacts: list[ArtifactRef] | None = None,
) -> TurnResult:
    return TurnResult(
        text=f"turn:{status}",
        session_id="s",
        task_id=task_id,
        kind=kind,
        degraded_warnings=list(warnings or []),
        settlement_status=status,
        artifacts=list(artifacts or []),
    )


@pytest.mark.asyncio
async def test_degraded_run_triggers_one_bounded_continuation_then_delivers_success(monkeypatch):
    settings = load_settings()
    settings.schedules.auto_continue = True
    settings.schedules.max_continuations = 1
    agent = await OmniAgent.create(settings)
    try:
        # First turn finishes short (degraded, one deliverable missing); the
        # continuation finishes the job (passed).
        scripted = _ScriptedTurns([
            _turn("degraded", warnings=["paper not written"]),
            _turn("passed"),
        ])
        monkeypatch.setattr(agent._scheduled_goals, "_run_turn", scripted)

        session_id = await agent.ensure_session(channel="cli")
        result = await agent.run_scheduled_goal(
            goal="fetch an abstract, draw a figure, and write a paper",
            task_id="",  # no pre-created owning task → the scripted turn stands in
            channel="cli",
            session_id=session_id,
            schedule_id="",  # skip observability binding
        )
        assert result is not None
        assert result.settlement_status == "passed"
        # Exactly one continuation was enqueued (initial + 1).
        assert len(scripted.calls) == 2
        # The continuation goal nudges "finish only what's missing" and carries
        # the outstanding deliverable forward.
        assert "continuation" in scripted.calls[1]["goal"].lower()
        assert "paper not written" in scripted.calls[1]["goal"]

        inbox = agent.notifier.read_all()
        last = inbox[-1]
        assert last["object_kind"] == "task"
        assert last["object_id"] == "t"
        assert last["task_id"] == "t"
        assert last["status"] == "succeeded"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_no_continuation_when_auto_continue_disabled(monkeypatch):
    settings = load_settings()
    settings.schedules.auto_continue = False
    agent = await OmniAgent.create(settings)
    try:
        scripted = _ScriptedTurns([_turn("degraded", warnings=["still missing"])])
        monkeypatch.setattr(agent._scheduled_goals, "_run_turn", scripted)

        result = await agent.run_scheduled_goal(
            goal="do a big multi-part job", task_id="", channel="cli", session_id="s", schedule_id="",
        )
        assert len(scripted.calls) == 1  # no continuation
        assert result.settlement_status == "degraded"
        assert agent.notifier.read_all()[-1]["status"] == "degraded"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_passed_run_is_not_continued(monkeypatch):
    settings = load_settings()
    settings.schedules.auto_continue = True
    settings.schedules.max_continuations = 2
    agent = await OmniAgent.create(settings)
    try:
        scripted = _ScriptedTurns([_turn("passed")])
        monkeypatch.setattr(agent._scheduled_goals, "_run_turn", scripted)
        result = await agent.run_scheduled_goal(
            goal="one clean deliverable", task_id="", channel="cli", session_id="s", schedule_id="",
        )
        assert len(scripted.calls) == 1
        assert result.settlement_status == "passed"
        assert agent.notifier.read_all()[-1]["status"] == "succeeded"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_scheduled_notification_preserves_artifacts_across_continuation(monkeypatch):
    settings = load_settings()
    settings.schedules.auto_continue = True
    settings.schedules.max_continuations = 1
    agent = await OmniAgent.create(settings)
    first = ArtifactRef(
        title="Figure",
        format="png",
        uri="artifact://figure",
        path="/workspace/figures/figure.png",
    )
    second = ArtifactRef(
        title="Paper",
        format="md",
        uri="artifact://paper",
        path="/workspace/reports/paper.md",
    )
    try:
        scripted = _ScriptedTurns(
            [
                _turn("degraded", warnings=["paper missing"], artifacts=[first]),
                _turn("passed", artifacts=[second]),
            ]
        )
        monkeypatch.setattr(agent._scheduled_goals, "_run_turn", scripted)

        session_id = await agent.ensure_session(channel="cli")
        await agent.run_scheduled_goal(
            goal="draw a figure and write a paper",
            task_id="",
            channel="cli",
            session_id=session_id,
            schedule_id="",
        )

        note = agent.notifier.read_all()[-1]
        assert note["artifacts"] == ["artifact://figure", "artifact://paper"]
        assert [item["uri"] for item in note["payload"]["artifacts"]] == [
            "artifact://figure",
            "artifact://paper",
        ]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_needs_input_run_is_not_continued(monkeypatch):
    """Nobody is there to answer an unattended run, so a clarification is not
    retried — it is delivered as-is (a retry would only stall again)."""
    settings = load_settings()
    settings.schedules.auto_continue = True
    settings.schedules.max_continuations = 2
    agent = await OmniAgent.create(settings)
    try:
        scripted = _ScriptedTurns([_turn("needs_input")])
        monkeypatch.setattr(agent._scheduled_goals, "_run_turn", scripted)
        await agent.run_scheduled_goal(
            goal="ambiguous request", task_id="", channel="cli", session_id="s", schedule_id="",
        )
        assert len(scripted.calls) == 1
        assert agent.notifier.read_all()[-1]["status"] == "needs_input"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_pending_turn_is_delivered_from_the_settled_task_row_not_degraded(monkeypatch):
    """R1: a headless turn can hand back a transient ``pending`` verification
    captured before the verifier settled. Delivering that (a workflow run) used to
    map to ``degraded`` in the inbox even though the durable task row settled
    ``succeeded`` — the exact reported divergence. The deliverer must re-read the
    settled row and report its authoritative status.
    """
    from omni.storage.models import TaskORM

    agent = await OmniAgent.create(load_settings())
    try:
        task = await agent.tasks.create_task(
            session_id="s", channel="cli", user_input="settled work",
            title="settled work", kind="turn",
        )
        async with agent.db.session() as s:
            row = await s.get(TaskORM, task.id)
            row.status = "succeeded"  # the verifier settled it after the turn returned
            await s.commit()

        # The turn result still carries the pre-settlement ``pending`` state, and it
        # is a workflow (whose stale kind-based fallback would have been degraded).
        scripted = _ScriptedTurns([_turn("pending", task_id=task.id, kind="workflow")])
        monkeypatch.setattr(agent._scheduled_goals, "_run_turn", scripted)

        result = await agent.run_scheduled_goal(
            goal="settled work", task_id=task.id, channel="cli", session_id="s", schedule_id="",
        )
        assert result is not None and result.settlement_status == "pending"
        note = agent.notifier.read_all()[-1]
        assert note["status"] == "succeeded"  # the settled row wins over the fallback
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_crashing_run_delivers_an_honest_error_note_never_raises(monkeypatch):
    agent = await OmniAgent.create(load_settings())
    try:
        async def boom(*, goal, task_id, channel, session_id, grant):  # noqa: ANN001, ANN202
            raise RuntimeError("planner exploded")

        monkeypatch.setattr(agent._scheduled_goals, "_run_turn", boom)
        result = await agent.run_scheduled_goal(
            goal="whatever", task_id="", channel="cli", session_id="s", schedule_id="sch1",
        )
        # An unattended run is always accounted for — the exception is swallowed
        # into a delivered error note, not propagated to the scheduler tick.
        assert result is None
        last = agent.notifier.read_all()[-1]
        assert last["object_kind"] == "scheduled_goal"
        assert last["status"] == "failed"
        assert "planner exploded" in last["summary"]
    finally:
        await agent.aclose()
