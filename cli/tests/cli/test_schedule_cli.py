"""`omni schedule list`/`show` observability (offline, via CliRunner).

These pin the operator-facing surface the user asked for: a schedule's set
time, last run, status, result and artifact paths — so a scheduled task is no
longer a black box after it fires.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from omni.cli.main import app

runner = CliRunner()


def _seed(project: str) -> tuple[str, str, str]:
    """Create a schedule and settle one completed run under an owning task.

    A goal schedule now fires as a full *headless orchestrator turn* off-tick, so
    rather than run a live turn (non-deterministic), this seeds exactly what a
    finished run leaves behind: an owning task linked to the schedule and a
    completed subtask under it with a result + artifact, plus the schedule's
    ``last_subtask_id`` binding. This keeps the observability surface (`list`/
    `show`) test focused on rendering, not execution.

    R2: the schedule surface reads *status* from the settled owning task row (the
    same source ``/task show`` and ``/inbox`` use), so the seeded task and subtask
    are kept consistent (both succeeded) — the three surfaces must never disagree.
    """
    from omni.cli.state import AppState, make_agent, run_async
    from omni.storage.models import ScheduleORM, SubtaskORM, TaskORM

    async def _run() -> tuple[str, str]:
        agent = await make_agent(AppState(project=project))
        try:
            now = datetime.now(UTC)
            sid = await agent.scheduler.add(
                "agent-goal", {"input": "make a RAG architecture figure"},
                kind="interval", interval_s=3600,
                first_due=now - timedelta(seconds=1), title="daily figure",
            )
            async with agent.db.session() as s:
                task = TaskORM(
                    project=project, session_id="", channel="cli", status="succeeded",
                    kind="turn", schedule_id=sid, title="daily figure",
                    user_input="make a RAG architecture figure", started_at=now, finished_at=now,
                )
                s.add(task)
                await s.flush()
                sub = SubtaskORM(
                    project=project, task_id=task.id, schedule_id=sid,
                    skill_name="scientific-figure", status="succeeded",
                    finished_at=now,
                    result_json={
                        "summary": "produced a RAG figure",
                        "artifacts": [{"title": "fig", "path": "/tmp/out/rag_figure.png"}],
                    },
                )
                s.add(sub)
                await s.flush()
                sub_id = sub.id
                obj = await s.get(ScheduleORM, sid)
                obj.last_subtask_id = sub_id
                obj.run_count = 1
                obj.last_run_at = now
                await s.commit()
            return sid, sub_id, task.id
        finally:
            await agent.aclose()

    return run_async(_run())


def test_schedule_list_shows_last_run_and_status(monkeypatch):
    project = "sched-list-obs"
    sid, _sub, task_id = _seed(project)

    # CliRunner is a non-tty, so Rich pins width to 80 and folds the 8-column
    # table (splitting words across cell lines). Widen deterministically —
    # Rich uses ``_width`` only when ``_height`` is also set.
    from omni.cli.render import console

    monkeypatch.setattr(console, "_width", 200, raising=False)
    monkeypatch.setattr(console, "_height", 50, raising=False)

    res = runner.invoke(app, ["--project", project, "schedule", "list"])
    assert res.exit_code == 0, res.output
    out = res.stdout
    assert "daily figure" in out
    assert "last-run" in out  # the new column header
    assert "succeeded" in out  # the joined run status
    assert "task" in out  # the task-id column header
    assert task_id[:8] in out  # the owning task id, so the row links to `task show`
    assert "schedule show" in out  # discoverability hint
    assert "task show" in out  # inspect-a-run hint


def test_schedule_clarifications_lists_open_drafts(monkeypatch):
    """The durable ambiguity draft is visible across turns via a read-only list."""
    project = "sched-clarify-obs"
    from omni.cli.render import console
    from omni.cli.state import AppState, make_agent, run_async
    from omni.runtime.action_checkpoints import ActionCheckpointStore

    async def _seed() -> str:
        agent = await make_agent(AppState(project=project))
        try:
            store = ActionCheckpointStore(agent.db)
            rec = await store.open_clarification(
                action_kind="schedule.create",
                contract_version="v1",
                policy_version="temporal-policy-v1",
                channel="wechat",
                session_id="s1",
                actor_principal="local",
                required_decider="local",
                payload={"goal": "prep RAG materials", "title": "RAG", "when": {}},
                resolution={
                    "status": "ambiguous",
                    "raw_expression": "今天7点10分",
                    "unresolved_fields": ["day_period"],
                    "candidates": [
                        {"id": "am", "value": {}, "label": "今天 07:10", "validity": "past"},
                        {"id": "pm", "value": {}, "label": "今天 19:10", "validity": "future"},
                    ],
                },
            )
            return rec.id
        finally:
            await agent.aclose()

    cid = run_async(_seed())
    monkeypatch.setattr(console, "_width", 200, raising=False)
    monkeypatch.setattr(console, "_height", 50, raising=False)

    res = runner.invoke(app, ["--project", project, "schedule", "clarifications"])
    assert res.exit_code == 0, res.output
    out = res.stdout
    assert cid[:8] in out
    assert "今天7点10分" in out  # the asked-about wording
    assert "19:10" in out  # a candidate reading is surfaced
    assert "original requester" in out  # decider-identity hint
    assert "workspace" in out  # catalog column
    assert "decider" in out  # principal surfaced (not hard-coded away)


def test_schedule_show_surfaces_facts_result_and_artifacts():
    project = "sched-show-obs"
    sid, _sub, _task = _seed(project)

    res = runner.invoke(app, ["--project", project, "schedule", "show", sid[:8]])
    assert res.exit_code == 0, res.output
    out = res.stdout
    # schedule facts
    assert "make a RAG architecture figure" in out  # goal
    assert "write_file" in out  # autonomy grant surfaced
    # last-run facts + result + artifact path
    assert "succeeded" in out
    assert "produced a RAG figure" in out
    assert "rag_figure.png" in out


def test_schedule_status_follows_settled_task_not_subtask(monkeypatch):
    """R2: when the settled task row disagrees with its representative subtask, the
    schedule surface reports the *task* status — the same source ``/task show`` and
    ``/inbox`` use — so the three surfaces can never disagree (the reported bug: a
    run that showed ``succeeded`` on ``/schedule`` but ``degraded`` on ``/inbox``).
    """
    project = "sched-status-truth"
    from omni.cli.render import console
    from omni.cli.state import AppState, make_agent, run_async
    from omni.storage.models import ScheduleORM, SubtaskORM, TaskORM

    async def _seed_divergent() -> str:
        agent = await make_agent(AppState(project=project))
        try:
            now = datetime.now(UTC)
            sid = await agent.scheduler.add(
                "agent-goal", {"input": "make a RAG architecture figure"},
                kind="interval", interval_s=3600,
                first_due=now - timedelta(seconds=1), title="daily figure",
            )
            async with agent.db.session() as s:
                # Settled task is degraded (e.g. a verification miss) while the
                # skill subtask itself completed — the divergent state R2 unifies.
                task = TaskORM(
                    project=project, session_id="", channel="cli", status="degraded",
                    kind="turn", schedule_id=sid, title="daily figure",
                    user_input="make a RAG architecture figure", started_at=now, finished_at=now,
                )
                s.add(task)
                await s.flush()
                sub = SubtaskORM(
                    project=project, task_id=task.id, schedule_id=sid,
                    skill_name="scientific-figure", status="succeeded", finished_at=now,
                    result_json={"summary": "produced a RAG figure"},
                )
                s.add(sub)
                await s.flush()
                obj = await s.get(ScheduleORM, sid)
                obj.last_subtask_id = sub.id
                obj.run_count = 1
                obj.last_run_at = now
                await s.commit()
            return sid
        finally:
            await agent.aclose()

    sid = run_async(_seed_divergent())
    monkeypatch.setattr(console, "_width", 200, raising=False)
    monkeypatch.setattr(console, "_height", 50, raising=False)

    res = runner.invoke(app, ["--project", project, "schedule", "show", sid[:8]])
    assert res.exit_code == 0, res.output
    assert "degraded" in res.output  # the task row wins, not the subtask's "succeeded"


def test_schedule_show_unknown_id_exits_nonzero():
    project = "sched-show-missing"
    _seed(project)
    res = runner.invoke(app, ["--project", project, "schedule", "show", "deadbeef"])
    assert res.exit_code == 1


def test_schedule_help_lists_all_subcommands(monkeypatch):
    """`/schedule help` exists and documents the full command surface (the missing
    `help` verb the user reported)."""
    from omni.cli.render import console

    monkeypatch.setattr(console, "_width", 200, raising=False)
    monkeypatch.setattr(console, "_height", 50, raising=False)

    res = runner.invoke(app, ["schedule", "help"])
    assert res.exit_code == 0, res.output
    for sub in (
        "add", "list", "all", "show", "enable", "disable",
        "remove", "proposals", "approve", "deny", "run",
    ):
        assert sub in res.output


def test_schedule_show_resolves_owning_workspace(monkeypatch):
    """A schedule id from `schedule all` opens from any directory: `show` routes to
    the owning workspace, not just the current one (the home/workspace scoping fix
    already applied to `task show`)."""
    owner, other = "sched-route-owner", "sched-route-other"
    sid, _sub, _task = _seed(owner)
    _seed(other)  # a second registered workspace so the CWD workspace != owner

    from omni.cli.render import console

    monkeypatch.setattr(console, "_width", 200, raising=False)
    monkeypatch.setattr(console, "_height", 50, raising=False)

    # From the *other* workspace, the owner's id still resolves and shows its facts.
    res = runner.invoke(app, ["--project", other, "schedule", "show", sid[:8]])
    assert res.exit_code == 0, res.output
    assert sid in res.output  # the owner's exact schedule, resolved cross-workspace


def test_schedule_disable_resolves_owning_workspace():
    """Mutations reach the owning workspace too: `disable <id>` from another
    directory pauses the owner's schedule (task parity for enable/disable/remove)."""
    owner, other = "sched-toggle-owner", "sched-toggle-other"
    sid, _sub, _task = _seed(owner)
    _seed(other)

    res = runner.invoke(app, ["--project", other, "schedule", "disable", sid[:8]])
    assert res.exit_code == 0, res.output

    from omni.cli.state import AppState, make_agent, run_async

    async def _get():
        agent = await make_agent(AppState(project=owner))
        try:
            return await agent.scheduler.get(sid)
        finally:
            await agent.aclose()

    sched = run_async(_get())
    assert sched is not None and sched.enabled is False


def test_schedule_all_aggregates_registered_workspaces(monkeypatch):
    """`schedule all` gathers schedules from every workspace (tagged), while a bare
    `schedule list` stays scoped — the cross-workspace parity with `task all`."""
    from omni.cli.state import AppState, make_agent, run_async

    async def _seed_sched(project: str, title: str) -> None:
        agent = await make_agent(AppState(project=project))
        try:
            await agent.scheduler.add(
                "agent-goal", {"input": f"{title} goal"},
                kind="interval", interval_s=3600,
                first_due=datetime.now(UTC) + timedelta(hours=1), title=title,
            )
        finally:
            await agent.aclose()

    run_async(_seed_sched("sched-all-alpha", "alpha-digest"))
    run_async(_seed_sched("sched-all-beta", "beta-digest"))

    from omni.cli.render import console

    monkeypatch.setattr(console, "_width", 200, raising=False)
    monkeypatch.setattr(console, "_height", 50, raising=False)

    scoped = runner.invoke(app, ["--project", "sched-all-alpha", "schedule", "list"])
    assert scoped.exit_code == 0, scoped.output
    assert "alpha-digest" in scoped.stdout
    assert "beta-digest" not in scoped.stdout  # a bare list is workspace-scoped

    res = runner.invoke(app, ["--project", "sched-all-alpha", "schedule", "all"])
    assert res.exit_code == 0, res.output
    assert "Schedules across workspaces" in res.stdout
    assert "workspace" in res.stdout  # the tagging column
    assert "alpha-digest" in res.stdout
    assert "beta-digest" in res.stdout  # aggregated across registered workspaces


# ── `omni schedule add` — the incident surface (unified contract) ──


def _future_local_iso(hours: int = 3) -> str:
    return (datetime.now().astimezone().replace(tzinfo=None) + timedelta(hours=hours)).isoformat(
        timespec="minutes"
    )


def test_schedule_add_at_creates_one_time_schedule():
    """The exact shape from the incident — ``--at`` + a trailing goal — now works.

    The original failure was ``No such option: --at``; ``add`` learned the
    one-time flag (aliasing ``--once``) and routes free-form goal text to the
    agent-goal sub-agent.
    """
    project = "sched-add-at"
    res = runner.invoke(
        app,
        [
            "--project", project, "schedule", "add",
            "--at", _future_local_iso(),
            "--title", "RAG review prep",
            "get the Attention Is All You Need abstract and draw a RAG architecture figure",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "No such option" not in res.output
    assert "Created schedule" in res.output

    from omni.cli.state import AppState, make_agent, run_async

    async def _rows():
        agent = await make_agent(AppState(project=project))
        try:
            return await agent.scheduler.list()
        finally:
            await agent.aclose()

    rows = run_async(_rows())
    assert len(rows) == 1 and rows[0].kind == "once"
    assert "Attention Is All You Need" in str((rows[0].input_json or {}).get("input") or "")


def test_schedule_add_at_in_the_past_is_a_clean_error_not_a_parse_error():
    """A past one-time time is refused with guidance (time-confusion bug), and it
    is emphatically *not* the old ``No such option: --at`` parser failure."""
    project = "sched-add-past"
    past = (datetime.now().astimezone().replace(tzinfo=None) - timedelta(days=1)).isoformat()
    res = runner.invoke(app, ["--project", project, "schedule", "add", "--at", past, "past digest"])
    assert res.exit_code == 1
    assert "No such option" not in res.output
    assert "past" in res.output.lower()


def test_schedule_add_requires_exactly_one_trigger():
    project = "sched-add-notrigger"
    res = runner.invoke(app, ["--project", project, "schedule", "add", "some goal"])
    assert res.exit_code == 1
    assert "exactly one trigger" in res.output.lower()


def test_schedule_add_at_and_once_are_the_same_flag():
    """``--once`` still works as the historical alias of ``--at``."""
    project = "sched-add-once-alias"
    res = runner.invoke(
        app, ["--project", project, "schedule", "add", "--once", _future_local_iso(), "goal text"]
    )
    assert res.exit_code == 0, res.output


# ── durable approval commands (proposals / approve / deny) ──


def _seed_im_proposal(project: str) -> str:
    """Persist an IM-originated proposal via the service and return its id."""
    from omni.cli.state import AppState, make_agent, run_async
    from omni.scheduling.contracts import ScheduleActor, ScheduleCreateRequest, cron_trigger
    from omni.scheduling.service import ScheduleService

    async def _run() -> str:
        agent = await make_agent(AppState(project=project))
        try:
            service = ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)
            result = await service.create(
                ScheduleCreateRequest(
                    trigger=cron_trigger("0 18 * * *"),
                    goal="daily research digest",
                    actor=ScheduleActor(channel="wechat", session_id="s1", principal="wechat:peer"),
                )
            )
            return result.proposal_id
        finally:
            await agent.aclose()

    return run_async(_run())


def test_schedule_proposals_lists_pending_and_approve_creates_it(monkeypatch):
    project = "sched-proposals"
    pid = _seed_im_proposal(project)
    assert pid

    from omni.cli.render import console

    monkeypatch.setattr(console, "_width", 200, raising=False)
    monkeypatch.setattr(console, "_height", 50, raising=False)

    listed = runner.invoke(app, ["--project", project, "schedule", "proposals"])
    assert listed.exit_code == 0, listed.output
    assert pid[:8] in listed.output
    assert "approve" in listed.output.lower()

    approved = runner.invoke(app, ["--project", project, "schedule", "approve", pid[:8]])
    assert approved.exit_code == 0, approved.output
    assert "schedule" in approved.output.lower()

    from omni.cli.state import AppState, make_agent, run_async

    async def _rows():
        agent = await make_agent(AppState(project=project))
        try:
            return await agent.scheduler.list()
        finally:
            await agent.aclose()

    assert len(run_async(_rows())) == 1


def test_schedule_deny_rejects_without_creating():
    project = "sched-deny"
    pid = _seed_im_proposal(project)
    res = runner.invoke(app, ["--project", project, "schedule", "deny", pid[:8]])
    assert res.exit_code == 0, res.output

    from omni.cli.state import AppState, make_agent, run_async

    async def _rows():
        agent = await make_agent(AppState(project=project))
        try:
            return await agent.scheduler.list()
        finally:
            await agent.aclose()

    assert run_async(_rows()) == []
