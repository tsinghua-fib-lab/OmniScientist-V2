"""`omni task show <id>` routes across workspaces via the global index.

Reproduces the reported bug: ``tasks --all`` lists a task owned by workspace A,
but ``task show <id>`` — run from workspace B — used to report "not found"
because it only queried B's database. With the global task index, B resolves the
id back to A and renders it.
"""

from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from omni.cli.main import app
from omni.config import load_settings
from omni.config.workspaces import register_workspace
from omni.storage.db import get_database
from omni.storage.models import TaskORM

runner = CliRunner()


def _seed_task(project: str, task_id: str, *, title: str) -> None:
    async def _seed() -> None:
        s = load_settings(project=project)
        s.paths.ensure_dirs()
        db = get_database(s.paths.project_db)
        await db.init()
        async with db.session() as sess:
            sess.add(
                TaskORM(
                    id=task_id,
                    status="succeeded",
                    title=title,
                    kind="turn",
                    channel="cli",
                    user_input="do the thing",
                    project=s.paths.project_name,
                )
            )
            await sess.commit()
        register_workspace(s.paths)

    asyncio.run(_seed())


def test_show_resolves_a_task_owned_by_another_workspace():
    _seed_task("alpha", "a856f342cafef00d", title="cross-workspace task")

    res = runner.invoke(app, ["--project", "beta", "task", "show", "a856f342"])

    assert res.exit_code == 0, res.output
    # The full owning-workspace row was resolved and rendered (not "not found").
    assert "a856f342cafef00d" in res.output
    assert "was not found" not in res.output


def test_show_still_reports_missing_for_a_truly_unknown_id():
    _seed_task("alpha", "b0000000feedface", title="only alpha")

    res = runner.invoke(app, ["--project", "beta", "task", "show", "deadbeef"])

    assert res.exit_code == 1
    assert "was not found" in res.output


def test_watch_once_follows_a_task_owned_by_another_workspace():
    """`task watch <id> --once` renders one task's detail, routing cross-workspace.

    Guards the reported ``code 2`` regression: passing an id to ``watch`` used to
    be an "unexpected extra argument" usage error; now it follows that task.
    """
    _seed_task("alpha", "c17a5c0011223344", title="watch me")

    res = runner.invoke(app, ["--project", "beta", "task", "watch", "c17a5c00", "--once"])

    assert res.exit_code == 0, res.output
    assert "c17a5c0011223344" in res.output
    assert "was not found" not in res.output


def test_watch_once_reports_missing_for_unknown_id():
    res = runner.invoke(app, ["--project", "beta", "task", "watch", "0ff1ce00deadbeef", "--once"])

    assert res.exit_code == 1
    assert "was not found" in res.output
