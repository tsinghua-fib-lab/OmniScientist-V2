"""Workspace identity, registry, daemon liveness, task claim, and resume.

Covers the multi-window hardening: path-keyed workspaces (no `default` bucket,
no `~/.omni` home edge), the workspace registry behind `--all`, the per-workspace
daemon pidfile, atomic task claim (multi-process safety), `omni resume`'s
`resolve_last`, and task visibility across windows.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omni.cli.main import app
from tests.conftest import store_shaped_home

runner = CliRunner()


def _run(coro):
    """Run a coroutine, then dispose cached DB engines in the *same* loop so the
    next ``asyncio.run`` / CLI invocation rebuilds them (avoids loop reuse)."""

    async def _wrap():
        from omni.storage.db import reset_databases

        try:
            return await coro
        finally:
            await reset_databases()

    return asyncio.run(_wrap())


# ── path-keyed resolution ──────────────────────────────────────────────────
def test_get_paths_keys_by_vcs_root(tmp_path):
    from omni.config.paths import get_paths

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)

    p_sub = get_paths(cwd=sub)
    p_root = get_paths(cwd=repo)
    # Every terminal in the repo → one shared, path-keyed store.
    assert p_sub.workspace_root == repo == p_root.workspace_root
    assert p_sub.project_dir == p_root.project_dir
    assert "workspaces" in p_sub.project_dir.parts
    assert p_sub.project_name == "repo"


def test_resolve_project_dir_does_not_treat_a_leaked_workspace_root_as_the_store(tmp_path):
    """A sqlite sitting in the checkout is leftover, not the durable store."""
    from omni.config.paths import get_paths, is_durable_project_dir, resolve_project_dir

    repo = tmp_path / "omniscientist_v2"
    (repo / ".git").mkdir(parents=True)
    leaked = repo / "sessions.sqlite3"
    leaked.write_bytes(b"not-the-store")

    assert is_durable_project_dir(repo) is False
    store = resolve_project_dir(repo)
    assert store == get_paths(cwd=repo).project_dir
    assert store != repo.resolve()
    assert "workspaces" in store.parts
    assert leaked.read_bytes() == b"not-the-store"


def test_resolve_project_dir_keeps_in_place_and_path_keyed_stores(tmp_path):
    from omni.config.paths import get_paths, is_durable_project_dir, resolve_project_dir

    repo = tmp_path / "adopted"
    marker = repo / ".omni"
    marker.mkdir(parents=True)
    (repo / ".git").mkdir()
    assert is_durable_project_dir(marker) is True
    assert resolve_project_dir(marker) == marker.resolve()
    assert resolve_project_dir(repo) == marker.resolve()

    keyed = tmp_path / "hashed"
    (keyed / ".git").mkdir(parents=True)
    store = get_paths(cwd=keyed).project_dir
    assert is_durable_project_dir(store) is True
    assert resolve_project_dir(store) == store.resolve()


def test_home_is_never_a_project(tmp_path):
    from omni.config.paths import find_project_root, get_paths

    home = Path.home()  # tmp/home via isolated_home fixture
    (home / ".omni").mkdir(parents=True, exist_ok=True)
    # ~/.omni must never be treated as an in-place project marker.
    assert find_project_root(home) is None
    p = get_paths(cwd=home)
    assert p.project_dir != home
    assert "workspaces" in p.project_dir.parts


def test_named_project_is_unchanged():
    from omni.config.paths import get_paths, user_home

    p = get_paths(project="alpha")
    assert p.workspace_root is None
    assert p.project_dir == user_home() / "projects" / "alpha"


def test_user_home_persistent_selection_and_reset(tmp_path, monkeypatch):
    from omni.config.paths import (
        configure_user_home,
        default_user_home,
        home_selection_file,
        reset_user_home,
        user_home,
        user_home_resolution,
    )

    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    custom = tmp_path / "research-data"

    assert user_home() == default_user_home()
    assert configure_user_home(custom) == custom.resolve()
    assert home_selection_file().read_text(encoding="utf-8").strip() == str(custom.resolve())
    assert user_home() == custom.resolve()
    assert user_home_resolution()[1].startswith("saved selection")

    assert reset_user_home() == default_user_home()
    assert user_home() == default_user_home()
    assert not home_selection_file().exists()


def test_omni_home_environment_overrides_persistent_selection(tmp_path, monkeypatch):
    from omni.config.paths import configure_user_home, user_home, user_home_resolution

    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    configure_user_home(store_shaped_home(tmp_path, "saved"))
    override = store_shaped_home(tmp_path, "environment")
    monkeypatch.setenv("OMNI_HOME", str(override))

    assert user_home() == override.resolve()
    assert user_home_resolution()[1] == "environment (OMNI_HOME)"


def test_configure_user_home_rejects_unsafe_roots():
    from omni.config.paths import configure_user_home

    for unsafe in (Path.home(), Path("/")):
        with pytest.raises(ValueError):
            configure_user_home(unsafe)


def test_invalid_saved_user_home_falls_back_to_default(tmp_path, monkeypatch):
    from omni.config.paths import default_user_home, home_selection_file, user_home

    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    pointer = home_selection_file()
    pointer.parent.mkdir(parents=True)
    pointer.write_text(f"{Path.home()}\n", encoding="utf-8")

    assert user_home() == default_user_home()


def test_legacy_data_dir_setting_cannot_override_bootstrap_home(tmp_path, monkeypatch):
    from omni.config.paths import configure_user_home, get_paths
    from omni.config.settings import load_settings

    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    selected = configure_user_home(tmp_path / "selected")
    paths = get_paths()
    paths.ensure_dirs()
    paths.config_file.write_text(
        'data_dir = "/tmp/stale-home"\n[model]\nprovider = "mock"\nmodel = "omni-mock"\n',
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.paths.home == selected
    assert settings.data_dir == str(selected)


def test_in_place_omni_wins_over_vcs(tmp_path):
    from omni.config.paths import get_paths

    repo = tmp_path / "repo2"
    (repo / ".git").mkdir(parents=True)
    (repo / ".omni").mkdir(parents=True)
    sub = repo / "x"
    sub.mkdir()
    p = get_paths(cwd=sub)
    assert p.workspace_root == repo
    assert p.project_dir == repo / ".omni"


def test_workspace_key_stable_and_distinct():
    from omni.config.paths import workspace_key

    a, b = Path("/x/y/app"), Path("/p/q/app")
    assert workspace_key(a) == workspace_key(a)
    assert workspace_key(a) != workspace_key(b)  # same basename, different path
    assert workspace_key(a).startswith("app-")


def test_is_within_home_detects_omni_internal_paths():
    from omni.config.paths import is_within_home, user_home

    home = user_home()
    assert is_within_home(home)
    assert is_within_home(home / "workspaces" / "repo-abcd1234")
    assert is_within_home(home / "projects" / "default")
    # A real repo (sibling of the Omni home in tests) is never "within" it.
    assert not is_within_home(home.parent / "some_repo")


def test_get_paths_refuses_omni_home_internal_workspace():
    """A serve/REPL launched from inside ~/.omni must not spawn a nested ghost.

    Keying a workspace off ``~/.omni/workspaces/<x>`` is what produced duplicate
    daemons polling the same IM bot; resolution must fall back to the shared
    named ``default`` project instead.

    ``get_paths`` has always guarded this, but the in-place-project branch runs
    first and used to reach the guard only by luck: the upward walk stopped at
    the OS home, and the store is at the OS home only while ``OMNI_HOME`` keeps
    its default. Relocate the home — a supported, bootstrapped option — and the
    walk found ``<home>/.omni`` as an in-place marker and keyed a workspace off
    the home's parent, with ``project_dir`` set to the store itself.
    """
    from omni.config.paths import get_paths, user_home

    ghost_cwd = user_home() / "workspaces" / "repo-deadbeef"
    ghost_cwd.mkdir(parents=True, exist_ok=True)

    p = get_paths(cwd=ghost_cwd)
    assert p.workspace_root is None  # named project, not a path-keyed workspace
    assert p.project_dir == user_home() / "projects" / "default"
    assert "workspaces" not in p.project_dir.parts


def test_the_store_is_not_an_in_place_project_of_the_directory_holding_it():
    """The store is identified by where it is, not by what it is called.

    ``.omni`` names two different things: the store, and the marker a user drops
    in a repository to adopt it. A walk that only matches the name reads the
    store as an adoption of whatever directory happens to contain it, which
    makes every sibling of the home look like part of an adopted project.
    """
    from omni.config.paths import find_project_root, user_home

    sibling = user_home().parent / "repo"
    sibling.mkdir(parents=True, exist_ok=True)

    assert find_project_root(sibling) is None


def test_nothing_inside_the_store_is_an_in_place_project():
    from omni.config.paths import find_project_root, user_home

    inside = user_home() / "workspaces" / "repo-deadbeef"
    inside.mkdir(parents=True, exist_ok=True)

    assert find_project_root(inside) is None


def test_a_real_in_place_project_is_still_found(tmp_path):  # noqa: ANN001
    """The guard distinguishes the store from a marker; it does not stop
    honouring markers."""
    from omni.config.paths import find_project_root

    repo = tmp_path / "work" / "myrepo"
    (repo / ".omni").mkdir(parents=True, exist_ok=True)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True, exist_ok=True)

    assert find_project_root(repo) == repo.resolve()
    assert find_project_root(nested) == repo.resolve()


# ── registry ────────────────────────────────────────────────────────────────
def test_registry_roundtrip():
    from omni.config.paths import get_paths
    from omni.config.workspaces import list_workspaces, register_workspace

    register_workspace(get_paths(project="r1"))
    register_workspace(get_paths(project="r2"))
    names = {w["name"] for w in list_workspaces()}
    assert {"r1", "r2"} <= names


# ── daemon liveness ───────────────────────────────────────────────────────────
def test_daemon_pidfile_liveness():
    from omni.config.paths import get_paths
    from omni.runtime.daemon import (
        clear_pidfile,
        is_daemon_running,
        pidfile_path,
        write_pidfile,
    )

    paths = get_paths(project="dmn")
    paths.ensure_dirs()
    assert not is_daemon_running(paths)

    write_pidfile(paths)
    assert is_daemon_running(paths)  # our own live pid, fresh heartbeat

    pf = pidfile_path(paths)
    pf.write_text(json.dumps({"pid": os.getpid(), "ts": time.time() - 9999}))
    assert not is_daemon_running(paths)  # stale heartbeat → not an owner

    pf.write_text(json.dumps({"pid": 999_999, "ts": time.time()}))
    assert not is_daemon_running(paths)  # dead pid

    clear_pidfile(paths)
    assert not is_daemon_running(paths)


def test_daemon_pidfile_clear_only_when_current_process_owns_it():
    from omni.config.paths import get_paths
    from omni.runtime.daemon import (
        clear_pidfile_if_owner,
        pidfile_owned_by_current_process,
        pidfile_path,
        write_pidfile,
    )

    paths = get_paths(project="dmn-owner")
    paths.ensure_dirs()
    pf = pidfile_path(paths)

    write_pidfile(paths)
    assert pidfile_owned_by_current_process(paths)
    assert clear_pidfile_if_owner(paths) is True
    assert not pf.exists()

    pf.write_text(json.dumps({"pid": 999_999, "ts": time.time()}), encoding="utf-8")
    assert not pidfile_owned_by_current_process(paths)
    assert clear_pidfile_if_owner(paths) is False
    assert pf.exists()


@pytest.mark.asyncio
async def test_serve_refuses_live_daemon_without_clobbering_pidfile(
    monkeypatch: pytest.MonkeyPatch,
):
    from omni.cli.commands import serve_cmd
    from omni.cli.state import AppState
    from omni.config.paths import get_paths
    from omni.runtime.daemon import pidfile_path, write_pidfile

    project = "serve-dupe"
    paths = get_paths(project=project)
    paths.ensure_dirs()
    write_pidfile(paths)
    before = pidfile_path(paths).read_text(encoding="utf-8")

    async def agent_must_not_start(_state: object) -> None:
        raise AssertionError("duplicate serve initialized an agent before checking its pidfile")

    monkeypatch.setattr(serve_cmd, "make_agent", agent_must_not_start)
    with pytest.raises(serve_cmd.DaemonAlreadyRunning):
        await serve_cmd._run_service(
            AppState(project=project), channels="", workers=1, task_only=True
        )

    assert pidfile_path(paths).read_text(encoding="utf-8") == before


# ── atomic task claim (multi-process safety) ─────────────────────────────────
@pytest.mark.asyncio
async def test_atomic_claim_runs_task_only_once():
    from omni.agent import OmniAgent
    from omni.config import load_settings

    agent = await OmniAgent.create(load_settings())
    try:
        tid = await agent.runtime.enqueue("nope-skill", {"x": 1}, "cli", session_id="s1")
        first = await agent.runtime._claim(tid)
        second = await agent.runtime._claim(tid)
        assert first is not None  # winner gets the task fields
        assert first[0] == "nope-skill"
        assert second is None  # a second worker/process can't re-claim it
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_atomic_claim_concurrent_single_winner():
    from omni.agent import OmniAgent
    from omni.config import load_settings

    agent = await OmniAgent.create(load_settings())
    try:
        tid = await agent.runtime.enqueue("race-skill", {}, "cli", session_id="s2")
        # Simulate a daemon and a REPL drain racing for the same task.
        results = await asyncio.gather(
            agent.runtime._claim(tid), agent.runtime._claim(tid),
        )
        winners = [r for r in results if r is not None]
        assert len(winners) == 1  # exactly one claim succeeds
    finally:
        await agent.aclose()


# ── resume: resolve_last ──────────────────────────────────────────────────────
def test_resume_resolve_last_returns_latest_session():
    from omni.agent import OmniAgent
    from omni.cli.commands.resume_cmd import resolve_last
    from omni.cli.state import AppState
    from omni.config import load_settings

    async def _seed():
        agent = await OmniAgent.create(load_settings())
        try:
            return await agent.ensure_session(channel="cli", title="t")
        finally:
            await agent.aclose()

    sid = _run(_seed())
    assert resolve_last(AppState()) == sid


@pytest.mark.asyncio
async def test_repl_resume_last_switches_to_latest_session():
    from omni.cli.main import _repl_resume
    from omni.cli.state import AppState, make_agent

    agent = await make_agent(AppState(project="repl-resume-last"))
    try:
        first = await agent.ensure_session(channel="cli", title="first")
        await asyncio.sleep(0.01)
        second = await agent.ensure_session(channel="cli", title="second")

        assert await _repl_resume(
            agent,
            AppState(project="repl-resume-last"),
            "--last",
            first,
        ) == second
        assert await _repl_resume(
            agent,
            AppState(project="repl-resume-last"),
            "-l",
            first,
        ) == second
    finally:
        await agent.aclose()


# ── status smoke ──────────────────────────────────────────────────────────────
def test_status_reports_store_path():
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0
    assert "Workspace" in res.stdout
    assert "Daemon" in res.stdout
    assert "Memory store" in res.stdout
    assert "memory_entries" in res.stdout
    assert "memory.sqlite3" not in res.stdout


# ── tasks --all aggregates registered workspaces ─────────────────────────────
def test_tasks_list_all_shows_registered_workspace_task():
    from omni.cli.state import AppState, make_agent

    async def _seed():
        for project, skill, session in (
            ("alpha", "alpha-skill", "sx-alpha"),
            ("beta", "beta-skill", "sx-beta"),
        ):
            agent = await make_agent(AppState(project=project))
            try:
                run = await agent.tasks.create_task(
                    session_id=session,
                    channel="cli",
                    user_input=f"{skill} request",
                    title=f"{skill} request",
                )
                await agent.runtime.enqueue(
                    skill,
                    {"x": project},
                    "cli",
                    session_id=session,
                    task_id=run.id,
                )
            finally:
                await agent.aclose()

    _run(_seed())

    current = runner.invoke(app, ["--project", "alpha", "task", "list"])
    assert current.exit_code == 0
    assert "Tasks (current workspace)" in current.stdout
    assert "beta-skill" not in current.stdout
    assert "Current workspace" in current.stdout
    assert "sessions.sqlite3" in current.stdout.replace("\n", "")

    res = runner.invoke(app, ["--project", "alpha", "task", "list", "--all"], env={"COLUMNS": "200"})
    assert res.exit_code == 0
    assert "alpha-skill" in res.stdout
    assert "beta-skill" in res.stdout

    scoped = runner.invoke(
        app, ["--project", "alpha", "task", "list", "--all", "--session", "sx-beta"], env={"COLUMNS": "200"}
    )
    assert scoped.exit_code == 0
    assert "beta-skill" in scoped.stdout
    assert "alpha-skill" not in scoped.stdout

    empty_filter = runner.invoke(app, ["--project", "alpha", "task", "list", "--session", "missing"])
    assert empty_filter.exit_code == 0
    assert "No tasks" in empty_filter.stdout
    assert "session=missing" in empty_filter.stdout


def test_tasks_empty_list_explains_workspace_and_global_view():
    res = runner.invoke(app, ["task", "list"])
    assert res.exit_code == 0
    assert "Current workspace" in res.stdout
    assert "sessions.sqlite3" in res.stdout.replace("\n", "")
    assert "No tasks" in res.stdout
    assert "/task subtask" in res.stdout


def test_tasks_show_watch_and_attach():
    from sqlalchemy import select

    from omni.cli.state import AppState, make_agent
    from omni.storage.models import SubtaskORM, _utcnow

    project = "task-detail"

    async def _seed():
        agent = await make_agent(AppState(project=project))
        try:
            sid = await agent.ensure_session(channel="cli", title="detail")
            run = await agent.tasks.create_task(
                session_id=sid,
                channel="cli",
                user_input="detail-skill request",
                title="detail-skill request",
            )
            tid = await agent.runtime.enqueue(
                "detail-skill",
                {"prompt": "make a figure"},
                "cli",
                session_id=sid,
                task_id=run.id,
            )
            async with agent.db.session() as session:
                task = (
                    await session.execute(select(SubtaskORM).where(SubtaskORM.id == tid))
                ).scalar_one()
                task.status = "succeeded"
                task.result_json = {
                    "summary": "finished report",
                    "artifacts": [
                        {
                            "title": "Report",
                            "format": "md",
                            "uri": "artifact://report-1",
                            "path": "/tmp/report.md",
                        }
                    ],
                    "research": {
                        "source_ids": ["source-abc"],
                        "claim_ids": ["claim-def"],
                        "evidence_ids": ["evidence-ghi"],
                        "run_id": "run-jkl",
                    },
                }
                task.finished_at = _utcnow()
                await session.commit()
                (agent.paths.home / "skills_deleted.json").write_text(
                    json.dumps({
                        "deleted": [{
                            "name": "detail-skill",
                            "source": "user_omni",
                            "path": str(agent.paths.user_skills_dir / "detail-skill"),
                            "action": "physical_delete",
                            "deleted_at": "2026-06-26T00:00:00+00:00",
                        }]
                    }),
                    encoding="utf-8",
                )
            return sid, tid
        finally:
            await agent.aclose()

    sid, tid = _run(_seed())

    shown = runner.invoke(app, ["--project", project, "task", "show", tid[:8]])
    assert shown.exit_code == 0
    assert "summary" in shown.stdout
    assert "finished report" in shown.stdout
    assert "input" not in shown.stdout
    assert "result" not in shown.stdout
    assert "artifacts" in shown.stdout
    assert "artifact://report-1" not in shown.stdout
    assert "/tmp/report.md" in shown.stdout
    assert "skill deleted" in shown.stdout
    assert "physical_delete" in shown.stdout
    assert f"/task show {tid[:8]} --json" in shown.stdout

    shown_json = runner.invoke(app, ["--project", project, "task", "show", tid[:8], "--json"])
    assert shown_json.exit_code == 0
    assert '"input_json"' in shown_json.stdout
    assert '"result_json"' in shown_json.stdout

    watched = runner.invoke(app, ["--project", project, "task", "watch", "--once"])
    assert watched.exit_code == 0
    assert "Tasks (current workspace)" in watched.stdout

    attached = runner.invoke(app, ["--project", project, "task", "attach", tid[:8], "--session", sid[:8]])
    assert attached.exit_code == 0
    assert "Attached skill_execution" in attached.stdout

    async def _messages():
        agent = await make_agent(AppState(project=project))
        try:
            return [m.content for m in await agent.session_messages(sid)]
        finally:
            await agent.aclose()

    messages = _run(_messages())
    attached_messages = [msg for msg in messages if tid in msg]
    assert attached_messages
    attached = attached_messages[-1]
    assert "finished report" in attached
    assert "/tmp/report.md" in attached
    assert "artifact://report-1" not in attached
    assert "run-jkl" in attached
    assert "/task show" in attached
