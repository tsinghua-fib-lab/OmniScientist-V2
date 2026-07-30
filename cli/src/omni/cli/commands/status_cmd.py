"""`omni status` — show the active workspace, its store, and daemon/task state.

The fast way to answer "why do my two terminals see different tasks?": it prints
the resolved workspace root and the exact DB file backing this window.
"""

from __future__ import annotations

from collections import Counter

import typer

from omni.cli.render import console, data_table
from omni.cli.state import AppState, make_agent, run_async
from omni.runtime.daemon import daemon_info

app = typer.Typer(help="Show workspace, storage, task, and daemon status.")


def _fmt_size(path) -> str:  # noqa: ANN001
    try:
        return f"{path.stat().st_size / 1024:.0f} KB"
    except OSError:
        return "—"


def _approval_status(settings, trusted: bool | None) -> str:  # noqa: ANN001
    from omni.config.security_preset import security_preset_label

    return security_preset_label(settings, trusted=trusted)


@app.callback(invoke_without_command=True)
def status(ctx: typer.Context) -> None:
    state: AppState = ctx.obj or AppState()

    async def _run():
        from omni.research.store import ResearchStore

        agent = await make_agent(state)
        try:
            tasks = [task for task in await agent.runtime.list_subtasks(limit=10_000) if task.task_id]
            sessions = await agent.list_sessions(limit=10_000)
            rom = await ResearchStore(agent.db).counts()
            mirror_dir = str(agent.artifacts._mirror_dir or "")
            schedules = await agent.scheduler.list(include_disabled=True, limit=10_000)
            return agent.paths, tasks, sessions, rom, mirror_dir, schedules, agent.settings
        finally:
            await agent.aclose()

    paths, tasks, sessions, rom, mirror_dir, schedules, settings = run_async(_run())
    from omni.memory.files import user_memory_file

    by_status = Counter(t.status for t in tasks)
    info_d = daemon_info(paths)
    daemon_txt = (
        f"running (pid={info_d['pid']}, heartbeat {info_d['age']:.0f}s ago)"
        if info_d else "not running (tasks execute in the current process)"
    )
    from omni.cli.timefmt import format_local_time

    enabled_schedules = [s for s in schedules if s.enabled]
    next_due = min((s.next_due_at for s in enabled_schedules if s.next_due_at), default=None)
    last_run = max((s.last_run_at for s in schedules if s.last_run_at), default=None)
    if not schedules:
        schedules_txt = "none"
    else:
        parts = [f"{len(enabled_schedules)}/{len(schedules)} enabled"]
        if next_due:
            parts.append(f"next {format_local_time(next_due)}")
        if last_run:
            parts.append(f"last {format_local_time(last_run)}")
        if enabled_schedules and not info_d:
            parts.append("⚠ start `omni serve` to fire")
        schedules_txt = "; ".join(parts)

    rows = [
        ["Data directory", str(paths.home)],
        ["Workspace", paths.project_name],
        ["Root", str(paths.workspace_root) if paths.workspace_root else "(named project via -P)"],
        ["Tool working dir", str(paths.local_ops_dir)],
        ["Storage", str(paths.project_dir)],
        ["Session DB", f"{paths.project_db}  ({_fmt_size(paths.project_db)})"],
        ["Memory store", f"{paths.project_db}  (memory_entries table)"],
        ["User MEMORY.md", str(user_memory_file(paths))],
        ["Trusted dir", "yes" if state.trusted else "no (read-only; run `omni trust`)"],
        ["Approval", _approval_status(settings, state.trusted)],
        ["Output files", mirror_dir if mirror_dir else "(durable store only)"],
        ["Daemon", daemon_txt],
        ["Schedules", schedules_txt],
        ["Sessions", str(len(sessions))],
        ["Tasks", str(len(tasks))],
    ]
    data_table("OmniScientist workspace status", ["field", "value"], rows)

    from omni.config.workspaces import iter_catalog_workspaces

    catalog = iter_catalog_workspaces(paths.home)
    if catalog:
        data_table(
            "Known workspaces (`/task all` lists their tasks; each folder path keys its own store)",
            ["name", "root", "store"],
            [
                [
                    str(rec.get("name") or ""),
                    str(rec.get("root") or "—"),
                    str(rec.get("project_dir") or ""),
                ]
                for rec in catalog
            ],
        )

    if tasks:
        parts = [f"{k}={by_status[k]}" for k in ("pending", "running", "succeeded", "failed")
                 if by_status.get(k)]
        if parts:
            console.print(f"  Task status: {'  '.join(parts)}", style="dim")

    if any(rom.values()):
        console.print(
            "  Research objects: "
            + "  ".join(f"{label}={rom[key]}" for key, label in (
                ("sources", "sources"), ("chunks", "chunks"), ("hypotheses", "hypotheses"),
                ("claims", "claims"), ("evidence", "evidence"), ("runs", "runs"),
            ) if rom.get(key)),
            style="dim",
        )
