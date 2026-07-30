"""`omni schedule` — recurring / one-shot scheduled jobs (cron/interval).

A schedule fires a skill (capability) as a background task when it comes due,
reusing the task runtime's durability, auto-retry and notifications. Use it for
recurring work like a daily literature digest or a periodic corpus refresh.
"""

from __future__ import annotations

import json
from typing import Any

import typer

from omni.cli.render import console, data_table, error, info, kv_table, success
from omni.cli.state import AppState, make_agent, make_agent_for_schedule, run_async
from omni.cli.timefmt import format_local_time
from omni.scheduling.contracts import (
    GOAL_SKILL,
    STATUS_CREATED,
    STATUS_NEEDS_INPUT,
    ScheduleActor,
    ScheduleCreateRequest,
    cron_trigger,
    interval_trigger,
    once_trigger,
)
from omni.scheduling.service import ScheduleService

app = typer.Typer(help="Schedule skills by interval, cron expression, or one-time trigger.", no_args_is_help=True)


def _spec_label(s: Any) -> str:  # noqa: ANN401 - ScheduleORM row
    """Humanised trigger description (e.g. 'every 3600s', 'cron 0 9 * * *', 'once')."""
    if s.kind == "interval":
        return f"every {s.interval_s}s"
    if s.kind == "cron":
        return f"cron {s.cron_expr}"
    return "once"


def _goal_text(s: Any) -> str:  # noqa: ANN401
    return str((s.input_json or {}).get("input") or "").strip()


def _artifact_paths(result_json: Any) -> list[str]:  # noqa: ANN401
    """User-facing artifact paths recorded by a run (best-effort across shapes)."""
    arts = (result_json or {}).get("artifacts") if isinstance(result_json, dict) else None
    out: list[str] = []
    if isinstance(arts, list):
        for a in arts:
            if isinstance(a, dict):
                path = a.get("path") or a.get("display_path") or a.get("uri")
                if path:
                    out.append(str(path))
            elif isinstance(a, str) and a:
                out.append(a)
    return out


def _result_summary(result_json: Any) -> str:  # noqa: ANN401
    if not isinstance(result_json, dict):
        return ""
    for key in ("summary", "message", "answer", "title"):
        val = result_json.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:200]
    return ""


def _parse_input(raw: str) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        error(f"--input is not valid JSON: {exc}")
        raise typer.Exit(1) from exc
    if not isinstance(data, dict):
        error("--input must be a JSON object")
        raise typer.Exit(1)
    return data


@app.command("add")
def add_cmd(
    ctx: typer.Context,
    target: str = typer.Argument(
        "", help="Skill/capability to trigger, or free-form goal text for the built-in agent."
    ),
    goal: str = typer.Option(
        "", "--goal", help="Free-form goal for the built-in agent (schedules the agent-goal sub-agent)."
    ),
    every: int = typer.Option(0, "--every", help="Trigger every N seconds."),
    cron: str = typer.Option("", "--cron", help='Five-field cron expression, for example "0 9 * * *".'),
    once: str = typer.Option("", "--once", help="One-time ISO timestamp, for example 2026-07-11T09:00."),
    at: str = typer.Option(
        "", "--at", help="One-time ISO timestamp (alias for --once). Naive values are local wall-clock."
    ),
    timezone: str = typer.Option(
        "", "--timezone", help="IANA timezone for a naive --at/--once (e.g. Asia/Shanghai); default is local."
    ),
    input_json: str = typer.Option("", "--input", help="Skill input as a JSON object (skill mode)."),
    title: str = typer.Option("", "--title", help="Human-readable title."),
    session: str = typer.Option("", "--session", help="Owning session ID, if any."),
    allow_tool: list[str] = typer.Option(  # noqa: B008 - Typer option factory
        None,
        "--allow-tool",
        help=(
            "Sensitive tool this schedule may run unattended (repeatable), e.g. "
            "--allow-tool write_file --allow-tool run_compute. Omit to use the "
            "schedules.autonomy default; pass --allow-tool '' for none (fail-closed)."
        ),
    ),
) -> None:
    """Create a schedule using exactly one trigger mode.

    Provide the goal/skill either as the positional argument or via ``--goal``,
    and exactly one trigger: ``--every`` seconds, ``--cron``, or a one-time
    ``--at`` (``--once`` is an alias). A naive ``--at`` is the operator's local
    wall-clock — the same semantics the ``schedule_task`` agent tool uses.
    """
    state: AppState = ctx.obj
    once_value = at or once
    if at and once and at != once:
        error("Pass only one of --at or --once (they are the same one-time trigger).")
        raise typer.Exit(1)
    given = [bool(every), bool(cron), bool(once_value)]
    if sum(given) != 1:
        error("Specify exactly one trigger: --every, --cron, or --at.")
        raise typer.Exit(1)
    if goal and target:
        error("Pass the goal via the positional argument OR --goal, not both.")
        raise typer.Exit(1)
    payload = _parse_input(input_json)
    approved_tools = None if allow_tool is None else [t for t in allow_tool if t.strip()]
    if every:
        trigger = interval_trigger(every)
    elif cron:
        trigger = cron_trigger(cron)
    else:
        trigger = once_trigger(once_value, timezone)

    async def _run():
        agent = await make_agent(state)
        try:
            goal_text = goal.strip()
            skill_name = GOAL_SKILL
            skill_input = payload
            # Positional target: a registered skill runs in skill mode; anything
            # else is treated as free-form goal text (the agent-goal sub-agent).
            if not goal_text and target.strip():
                if agent.registry.get(target.strip()) is not None:
                    skill_name = target.strip()
                else:
                    goal_text = target.strip()
            if not goal_text and skill_name == GOAL_SKILL and not skill_input:
                error("Provide a goal (positional text or --goal) or a known skill name.")
                raise typer.Exit(1)
            request = ScheduleCreateRequest(
                trigger=trigger,
                goal=goal_text,
                skill_name=skill_name,
                input=skill_input,
                title=title,
                actor=ScheduleActor(channel="cli", session_id=session, principal="local"),
                requested_grants=approved_tools,
            )
            service = ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)
            return await service.create(request)
        finally:
            await agent.aclose()

    result = run_async(_run())
    if result.status == STATUS_CREATED:
        success(f"Created schedule {result.schedule_id[:8]} ({result.spec}).")
        info(result.summary)
        _lazy_enable_home_service(state, result)
    elif result.status == STATUS_NEEDS_INPUT:
        error(result.summary or result.error)
        raise typer.Exit(1)
    else:
        error(result.summary or result.error or f"Could not create schedule ({result.status}).")
        raise typer.Exit(1)


def _lazy_enable_home_service(state: AppState, result: Any) -> None:  # noqa: ANN401 - ScheduleCreateResult
    """Confirm the always-on home service is up so a just-created schedule can fire.

    In the always-on model a bare `omni` already brings the service up, and even a
    transient `omni serve stop` is undone on the next launch. This is a
    belt-and-suspenders ensure for the CLI path (e.g. `omni schedule add` run in a
    script with no prior interactive launch): the schedule dispatcher only fires
    unattended when the service is running, so we enable + start it here if it
    isn't. If a runner is already live nothing happens. This never runs from the
    agent tool path (which cannot install an OS unit mid-turn) — only this local CLI.
    """
    if getattr(result, "runner_ready", None) is True:
        return
    from omni.runtime import service_control

    outcome = service_control.lazy_enable(state.settings(), reason="schedule")
    if outcome.ok:
        info("The always-on home service is running; this schedule will fire on time.")
    else:
        info(
            "Could not confirm the home service is running "
            f"({outcome.detail}). Run `omni serve start`, or `omni schedule run` to fire due jobs now."
        )


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    limit: int = typer.Option(30, help="Maximum schedules to show."),
    all_: bool = typer.Option(True, "--all/--enabled-only", help="Include disabled schedules."),
) -> None:
    """List schedules ordered by next trigger time."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            rows = await agent.scheduler.list(include_disabled=all_, limit=limit)
            # Join each schedule's most recent run so the list can show real
            # execution status, not just the static trigger. ``last_run`` resolves
            # the representative subtask, or falls back to the owning task for a
            # headless goal run that produced no subtask.
            last: dict[str, Any] = {}
            for s in rows:
                view = await agent.scheduler.last_run(s)
                if view is not None:
                    last[s.id] = view
            return rows, last
        finally:
            await agent.aclose()

    rows, last = run_async(_run())

    data_table(
        "Schedules",
        ["id", "title", "spec", "next", "last-run", "status", "task", "runs", "on"],
        [
            [
                s.id[:8], (s.title or "-")[:22], _spec_label(s),
                format_local_time(s.next_due_at) if s.next_due_at else "-",
                format_local_time(s.last_run_at) if s.last_run_at else "-",
                (last[s.id].status if s.id in last else ("-" if not s.last_subtask_id else "?")),
                # The last run's owning task, so the row links straight to
                # `omni task show <task>` / `omni inbox` for its result + artifacts.
                (last[s.id].task_id[:8] if s.id in last and last[s.id].task_id else "-"),
                s.run_count, "on" if s.enabled else "off",
            ]
            for s in rows
        ]
        or [["(none)", "", "", "", "", "", "", "", ""]],
    )
    if rows:
        info(
            "Details, last-run status, and artifact paths: `omni schedule show <id>`; "
            "inspect a run with `omni task show <task>`. IM / other workspaces: "
            "`omni schedule all`."
        )
    else:
        info(
            "No schedules in this workspace. Cross-workspace (incl. IM channel anchor): "
            "`omni schedule all`."
        )


@app.command("all")
def all_cmd(
    ctx: typer.Context,
    limit: int = typer.Option(30, help="Maximum schedules per workspace to show."),
    all_: bool = typer.Option(True, "--all/--enabled-only", help="Include disabled schedules."),
) -> None:
    """List schedules across the workspace catalog (cross-workspace `schedule list`).

    A bare `schedule list` is scoped to the current workspace, but the always-on
    home service fires *every* workspace's schedules — including the IM channel
    anchor. This is the one place to see them all, tagged with their workspace,
    exactly like `task all`.
    """
    from omni.runtime.aggregate import list_schedules_all_workspaces

    state: AppState = ctx.obj
    rows = run_async(
        list_schedules_all_workspaces(
            limit_per=limit, include_disabled=all_, home=state.settings().paths.home
        )
    )
    data_table(
        "Schedules across workspaces",
        ["workspace", "id", "title", "spec", "next", "last-run", "runs", "on"],
        [
            [
                (r.workspace or "-")[:18], r.id[:8], (r.title or "-")[:22], _spec_label(r),
                format_local_time(r.next_due_at) if r.next_due_at else "-",
                format_local_time(r.last_run_at) if r.last_run_at else "-",
                r.run_count, "on" if r.enabled else "off",
            ]
            for r in rows
        ]
        or [["(none)", "", "", "", "", "", "", ""]],
    )
    if rows:
        info(
            "Scoped to this workspace: `omni schedule list`. Inspect any id above with "
            "`omni schedule show <id>` — show/enable/disable/remove resolve the owning workspace automatically."
        )


@app.command("show")
def show_cmd(
    ctx: typer.Context,
    schedule: str = typer.Argument(..., help="Schedule id (a unique prefix is accepted)."),
    runs: int = typer.Option(5, "--runs", help="How many recent runs to list."),
) -> None:
    """Show a schedule's full definition, its last run, artifacts, and history."""
    state: AppState = ctx.obj

    async def _run():
        # Route to the workspace that owns this schedule id (schedule all is
        # cross-workspace); falls back to the local workspace for a local id.
        agent, _ = await make_agent_for_schedule(state, schedule)
        try:
            sched = await agent.scheduler.get(schedule)
            if sched is None:
                return None
            # Resolve the representative last run (subtask, or owning-task fallback
            # for a headless goal run that produced no subtask).
            last = await agent.scheduler.last_run(sched)
            history = await agent.scheduler.runs(sched.id, limit=max(1, runs))
            serve_running = False
            try:
                from omni.runtime.daemon import daemon_info

                serve_running = bool(daemon_info(agent.paths))
            except Exception:  # noqa: BLE001
                serve_running = False
            return sched, last, history, serve_running
        finally:
            await agent.aclose()

    res = run_async(_run())
    if res is None:
        error(f"Schedule {schedule} was not found.")
        raise typer.Exit(1)
    sched, last, history, serve_running = res

    grants = list(sched.approved_tools or [])
    autonomy = ", ".join(grants) if grants else "off (no unattended sensitive tools)"
    facts: list[tuple[str, Any]] = [
        ("id", sched.id),
        ("title", sched.title or "-"),
        ("goal", (_goal_text(sched) or "-")[:200]),
        ("skill", sched.skill_name),
        ("trigger", _spec_label(sched)),
        ("enabled", "yes" if sched.enabled else "no"),
        ("channel", sched.channel or "cli"),
        ("created", format_local_time(sched.created_at) if sched.created_at else "-"),
        ("next due", format_local_time(sched.next_due_at) if sched.next_due_at else "-"),
        ("last run", format_local_time(sched.last_run_at) if sched.last_run_at else "never"),
        ("runs", str(sched.run_count or 0)),
        ("autonomy", autonomy),
    ]
    kv_table(f"Schedule {sched.id[:8]}", facts)

    if last is not None:
        started = format_local_time(last.started_at) if last.started_at else "-"
        finished = format_local_time(last.finished_at) if last.finished_at else "-"
        detail: list[tuple[str, Any]] = [
            ("run id", last.id[:8]),
            ("task id", (last.task_id or "-")[:8]),
            ("status", last.status),
            ("started", started),
            ("finished", finished),
        ]
        summary = _result_summary(last.result_json)
        if summary:
            detail.append(("result", summary))
        if last.error:
            detail.append(("error", str(last.error)[:200]))
        kv_table("Last run", detail)

        artifacts = _artifact_paths(last.result_json)
        if artifacts:
            console.print("[bold]Artifacts[/bold]")
            for path in artifacts:
                console.print(f"  • {path}")
        elif last.status in {"succeeded", "degraded"}:
            console.print(
                "[dim]No artifacts recorded for the last run. If autonomy is off, the run "
                "cannot write files — see `autonomy` above and schedules.autonomy in config.[/dim]"
            )
    else:
        info("This schedule has not run yet.")

    if len(history) > 1:
        data_table(
            "Recent runs",
            ["run", "status", "when", "artifacts"],
            [
                [
                    h.id[:8], h.status,
                    format_local_time(h.finished_at or h.started_at) if (h.finished_at or h.started_at) else "-",
                    str(len(_artifact_paths(h.result_json))),
                ]
                for h in history
            ],
        )

    if sched.enabled and sched.next_due_at and not serve_running:
        info("Start `omni serve` so this fires unattended, or run `omni schedule run` to fire due jobs now.")
    if last is not None and last.task_id:
        info(f"Inspect the run's task with `omni task show {last.task_id[:8]}`; see all results in `omni inbox`.")


@app.command("remove")
def remove_cmd(ctx: typer.Context, schedule: str) -> None:
    """Delete a schedule."""
    state: AppState = ctx.obj

    async def _run():
        agent, _ = await make_agent_for_schedule(state, schedule)
        try:
            return await agent.scheduler.remove(schedule)
        finally:
            await agent.aclose()

    if run_async(_run()):
        success(f"Deleted schedule {schedule}")
    else:
        error(f"Schedule {schedule} was not found.")
        raise typer.Exit(1)


@app.command("enable")
def enable_cmd(ctx: typer.Context, schedule: str) -> None:
    """Enable a schedule."""
    _set_enabled(ctx.obj, schedule, True)


@app.command("disable")
def disable_cmd(ctx: typer.Context, schedule: str) -> None:
    """Disable a schedule without deleting it."""
    _set_enabled(ctx.obj, schedule, False)


def _set_enabled(state: AppState, schedule: str, enabled: bool) -> None:
    async def _run():
        agent, _ = await make_agent_for_schedule(state, schedule)
        try:
            return await agent.scheduler.set_enabled(schedule, enabled)
        finally:
            await agent.aclose()

    if run_async(_run()):
        success(f"Schedule {schedule} {'enabled' if enabled else 'disabled'}")
    else:
        error(f"Schedule {schedule} was not found.")
        raise typer.Exit(1)


@app.command("proposals")
def proposals_cmd(
    ctx: typer.Context,
    all_: bool = typer.Option(False, "--all/--pending-only", help="Include decided/expired proposals."),
    limit: int = typer.Option(30, help="Maximum proposals to show."),
) -> None:
    """List schedule requests awaiting the owner's approval.

    An IM-originated ``schedule_task`` with no local approver is held here as a
    durable, resumable proposal (Codex request/response-by-id, persisted) instead
    of being silently created or dead-ended. Approve one to execute the exact
    stored request.
    """
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            service = ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)
            return await service.list_proposals(include_all=all_, limit=limit)
        finally:
            await agent.aclose()

    rows = run_async(_run())
    data_table(
        "Schedule approval proposals",
        ["id", "state", "title", "requested-by", "channel", "created"],
        [
            [
                r.id[:8], r.state, (r.title or "-")[:28], (r.actor_principal or "-")[:22],
                r.channel or "-", format_local_time(r.created_at) if r.created_at else "-",
            ]
            for r in rows
        ]
        or [["(none)", "", "", "", "", ""]],
    )
    if any(r.state == "pending" for r in rows):
        info("Approve with `omni schedule approve <id>` or reject with `omni schedule deny <id>`.")


@app.command("clarifications")
def clarifications_cmd(
    ctx: typer.Context,
    limit: int = typer.Option(30, help="Maximum open clarifications to show."),
) -> None:
    """List open schedule-time clarifications across the workspace catalog.

    When a worded time is ambiguous (classically a bare hour with no AM/PM), the
    grounded readings are held as a durable draft in the owning workspace DB
    (often the IM channel anchor) instead of being silently guessed. Only the
    *original requester* (``decider``) can answer — reply in the same conversation
    (e.g. "evening" / "the second one" / "cancel"); this list is owner visibility
    across workspaces, channels, turns, and daemon restarts.
    """
    from omni.runtime.aggregate import list_open_clarifications_all_workspaces

    state: AppState = ctx.obj
    home = state.settings().paths.home
    rows = run_async(list_open_clarifications_all_workspaces(limit=limit, home=home))

    def _raw(rec: Any) -> str:  # noqa: ANN401
        return str((rec.resolution or {}).get("raw_expression", "")).strip() or "-"

    def _cands(rec: Any) -> str:  # noqa: ANN401
        labels = [str(c.get("label", "")) for c in (rec.resolution or {}).get("candidates", [])]
        return "; ".join(label for label in labels if label) or "-"

    data_table(
        "Open schedule clarifications",
        ["workspace", "id", "asked", "options", "channel", "decider", "expires"],
        [
            [
                (row.workspace or "-")[:16],
                row.record.id[:8],
                _raw(row.record)[:24],
                _cands(row.record)[:36],
                row.record.channel or "-",
                (row.record.required_decider or "-")[:20],
                format_local_time(row.record.expires_at) if row.record.expires_at else "-",
            ]
            for row in rows
        ]
        or [["(none)", "", "", "", "", "", ""]],
    )
    if rows:
        info(
            "Only the original requester (decider) can answer — reply in the same conversation "
            "(e.g. “evening” / “the second one” / “cancel”). They lapse automatically after their TTL."
        )
    else:
        info(
            "No open clarifications in the workspace catalog (registry + channel anchor + "
            "named projects). Ambiguous IM times draft on the channel anchor, not only CWD."
        )


@app.command("approve")
def approve_cmd(ctx: typer.Context, proposal: str = typer.Argument(..., help="Proposal id (a unique prefix is accepted).")) -> None:
    """Approve a pending schedule request, creating the exact stored schedule."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            service = ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)
            return await service.approve(proposal, decided_by="local")
        finally:
            await agent.aclose()

    result = run_async(_run())
    if result.status == STATUS_CREATED:
        success(f"Approved proposal {proposal[:8]} → schedule {result.schedule_id[:8]} ({result.spec}).")
        info(result.summary)
        # An approved schedule is only useful if something fires it; bring the
        # always-on home service up (same lazy trigger as `schedule add`).
        _lazy_enable_home_service(state, result)
    else:
        error(result.summary or result.error or f"Could not approve proposal ({result.status}).")
        raise typer.Exit(1)


@app.command("deny")
def deny_cmd(ctx: typer.Context, proposal: str = typer.Argument(..., help="Proposal id (a unique prefix is accepted).")) -> None:
    """Reject a pending schedule request without creating anything."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            service = ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)
            return await service.deny(proposal, decided_by="local")
        finally:
            await agent.aclose()

    result = run_async(_run())
    if result.proposal_id:
        success(result.summary or f"Denied proposal {proposal[:8]}.")
    else:
        error(result.error or f"Could not deny proposal ({result.status}).")
        raise typer.Exit(1)


def render_schedule_usage_help() -> None:
    """Print schedule subcommands and common examples (`/schedule help` in the REPL)."""
    data_table(
        "Schedule subcommands",
        ["command", "purpose", "example"],
        [
            ["add <goal|skill>", "Create a schedule with exactly one trigger (--every / --cron / --at)", '/schedule add "daily arXiv digest" --cron "0 9 * * *"'],
            ["list", "List this workspace's schedules by next trigger time", "/schedule list"],
            ["all", "List schedules across catalog workspaces (incl. IM channel anchor)", "/schedule all"],
            ["show <id>", "Show a schedule's definition, last run, artifacts, and history", "/schedule show 3f3d600d"],
            ["enable <id>", "Re-enable a disabled schedule", "/schedule enable 3f3d600d"],
            ["disable <id>", "Pause a schedule without deleting it", "/schedule disable 3f3d600d"],
            ["remove <id>", "Delete a schedule", "/schedule remove 3f3d600d"],
            ["clarifications", "List open ambiguous-time drafts across catalog workspaces", "/schedule clarifications"],
            ["proposals", "List IM-originated schedule requests awaiting approval", "/schedule proposals"],
            ["approve <id>", "Approve a pending proposal, creating the stored schedule", "/schedule approve 9a8b7c6d"],
            ["deny <id>", "Reject a pending proposal without creating anything", "/schedule deny 9a8b7c6d"],
            ["run", "Trigger all due schedules now (fires this workspace's due jobs)", "/schedule run"],
            ["help", "Show this schedule command reference", "/schedule help"],
        ],
    )
    data_table(
        "Important schedule options",
        ["option", "commands", "example"],
        [
            ["--every N", "add", "/schedule add refresh_corpus --every 3600"],
            ["--cron <expr>", "add", '/schedule add "weekly review" --cron "0 9 * * 1"'],
            ["--at / --once <iso>", "add", "/schedule add \"one-off report\" --at 2026-07-11T09:00"],
            ["--timezone <IANA>", "add", "/schedule add ... --at 2026-07-11T09:00 --timezone Asia/Shanghai"],
            ["--goal <text>", "add", '/schedule add --goal "summarize new papers" --every 86400'],
            ["--input <json>", "add", '/schedule add my_skill --input \'{"topic":"RAG"}\' --cron "0 8 * * *"'],
            ["--allow-tool <tool>", "add", "/schedule add ... --allow-tool write_file"],
            ["--runs N", "show", "/schedule show 3f3d600d --runs 10"],
            ["--all / --enabled-only", "list / all / proposals", "/schedule list --enabled-only"],
        ],
    )
    info("Schedules fire from the always-on home service (`omni serve`); `run` fires due jobs immediately in this workspace.")
    info("`list` is scoped to this workspace and `all` spans every workspace; `show/enable/disable/remove` accept an id from either and resolve the owning workspace.")
    info("IM-created requests without a local approver wait in `proposals` — approve or deny them to act on the exact stored request.")


@app.command("run")
def run_cmd(
    ctx: typer.Context,
    drain: bool = typer.Option(True, "--drain/--no-drain", help="Run enqueued tasks immediately."),
) -> None:
    """Trigger all due schedules immediately."""
    state: AppState = ctx.obj

    async def _run():
        agent = await make_agent(state)
        try:
            fired = await agent.scheduler.run_due()
            if fired and drain:
                # A headless goal fire runs as a detached orchestrator turn; await
                # those first so their workflow subtasks exist before we drain.
                await agent.scheduler.drain_fires()
                await agent.runtime.drain()
            return fired
        finally:
            await agent.aclose()

    fired = run_async(_run())
    if fired:
        success(f"Triggered {len(fired)} due tasks: {', '.join(t[:8] for t in fired)}")
    else:
        info("No schedules are due.")


@app.command("help")
def help_cmd() -> None:
    """Show schedule subcommands and common examples (`/schedule help` in the REPL)."""
    render_schedule_usage_help()
