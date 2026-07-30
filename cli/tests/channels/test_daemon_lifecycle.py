"""Daemon lifecycle contracts: metadata, restart, and update integration."""

from __future__ import annotations

import os
from types import SimpleNamespace

from typer.testing import CliRunner

from omni.cli.main import app
from omni.config import load_settings
from omni.runtime import daemon
from tests.conftest import store_shaped_home

runner = CliRunner()

# The autouse conftest fixture replaces ``daemon.scan_running_serve_pids`` with an
# empty stub (so no test SIGTERMs a real host process). Capture the real function
# at import time — before any fixture runs — so the two tests that assert the
# scanner's own behaviour can call it directly.
_REAL_SCAN = daemon.scan_running_serve_pids


def _patch_remote_version(monkeypatch, version: str | None) -> None:
    """Stub the update-checker's network fetch so ``omni update`` stays offline."""
    import omni.runtime.update_check as update_check

    monkeypatch.setattr(update_check, "fetch_latest_version", lambda *_a, **_k: version)


def test_pidfile_metadata_and_heartbeat_preserves_launch_context():
    settings = load_settings()
    settings.paths.ensure_dirs()

    daemon.write_pidfile(
        settings.paths,
        metadata={
            "version": "9.9.9-test",
            "executable": "/tmp/python",
            "argv": ["/tmp/python", "-m", "omni.cli.main", "serve", "--channels", "feishu"],
            "cwd": "/tmp/work",
            "channels": ["feishu"],
            "channels_arg": "feishu",
            "workers": 2,
            "mode": "daemon",
        },
    )
    first = daemon.read_pidfile(settings.paths)

    assert first["pid"] == os.getpid()
    assert first["version"] == "9.9.9-test"
    assert first["channels"] == ["feishu"]
    assert first["workers"] == 2
    assert first["started_at"]

    daemon.touch_pidfile(settings.paths)
    second = daemon.read_pidfile(settings.paths)

    assert second["started_at"] == first["started_at"]
    assert second["argv"] == first["argv"]
    assert second["channels_arg"] == "feishu"
    assert second["ts"] >= first["ts"]


def test_restart_reuses_running_daemon_launch_options(monkeypatch):
    import omni.cli.commands.serve_cmd as serve_cmd
    from omni.cli.state import AppState

    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        serve_cmd,
        "daemon_info",
        lambda _paths: {
            "pid": 12345,
            "age": 1,
            "channels": ["feishu", "dingtalk"],
            "channels_arg": "feishu,dingtalk",
            "workers": 3,
        },
    )
    monkeypatch.setattr(serve_cmd, "stop_daemon_process", lambda _state: (True, "stopped"))

    def fake_start(_state, *, channels: str = "", workers: int = 1):
        calls.append((channels, workers))
        return True, "started"

    monkeypatch.setattr(serve_cmd, "start_daemon_process", fake_start)

    ok, detail = serve_cmd.restart_daemon_process(AppState(), channels="", workers=None)

    assert ok is True
    assert "Restarted" in detail
    assert calls == [("feishu,dingtalk", 3)]


def test_restart_preserves_dynamic_channel_mode(monkeypatch):
    import omni.cli.commands.serve_cmd as serve_cmd
    from omni.cli.state import AppState

    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        serve_cmd,
        "daemon_info",
        lambda _paths: {
            "pid": 12345,
            "age": 1,
            "channels_mode": "dynamic",
            "channels": ["cli", "feishu"],
            "channels_arg": "",
            "workers": 2,
        },
    )
    monkeypatch.setattr(serve_cmd, "stop_daemon_process", lambda _state: (True, "stopped"))

    def fake_start(_state, *, channels: str = "", workers: int = 1):
        calls.append((channels, workers))
        return True, "started"

    monkeypatch.setattr(serve_cmd, "start_daemon_process", fake_start)

    ok, detail = serve_cmd.restart_daemon_process(AppState(), channels="", workers=None)

    assert ok is True
    assert "Restarted" in detail
    assert calls == [("", 2)]

def test_serve_status_warns_when_legacy_daemon_running():
    # After convergence, `omni serve status` reports the home service. A still
    # running legacy per-workspace daemon is surfaced as a migration warning so
    # it is never invisible.
    settings = load_settings()
    settings.paths.ensure_dirs()
    daemon.write_pidfile(
        settings.paths,
        metadata={
            "version": "9.9.9-test",
            "mode": "daemon",
            "channels_mode": "dynamic",
            "channels": ["cli", "feishu", "wechat"],
            "workers": 1,
        },
    )

    res = runner.invoke(app, ["serve", "status"])

    assert res.exit_code == 0
    assert "legacy per-workspace daemon" in res.stdout


def test_serve_status_all_lists_legacy_daemons():
    settings = load_settings()
    settings.paths.ensure_dirs()
    daemon.write_pidfile(
        settings.paths,
        metadata={
            "mode": "daemon",
            "channels_mode": "dynamic",
            "channels": ["cli"],
            "workers": 1,
            "model_provider": "openai_compatible",
            "model_name": "deepseek-chat",
            "model_base_url": "https://api.deepseek.com/v1",
        },
    )

    res = runner.invoke(app, ["serve", "status", "--all"])

    assert res.exit_code == 0, res.stdout
    # The `--all` view lists legacy daemons for migration hygiene; header presence
    # implies at least one row (the empty case prints an info line instead).
    assert "Legacy per-workspace daemons" in res.stdout


def test_update_retires_legacy_daemons(monkeypatch):
    """Legacy per-workspace daemons are *retired* on update (never restarted): the
    single home service takes over channels + schedules."""
    import omni.cli.commands.update_cmd as update_cmd
    from omni.runtime import daemon as daemon_mod

    retired: list[str] = []

    _patch_remote_version(monkeypatch, "9.9.9")  # offline: a newer version exists
    monkeypatch.setattr(
        update_cmd, "_plan", lambda **_kw: ("pip", ["python", "-m", "pip"], "pip fake")
    )
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda _cmd, check=False: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        update_cmd,
        "list_running_daemons",
        lambda _home: [{"pid": 12345, "project_name": "paper", "channels_arg": "feishu"}],
    )
    monkeypatch.setattr(
        daemon_mod, "stop_legacy_daemons", lambda home: retired.append(str(home)) or [12345]
    )

    res = runner.invoke(app, ["update", "--yes"])

    assert res.exit_code == 0, res.stdout
    assert retired  # legacy daemons were retired
    assert "retired 1 legacy per-workspace daemon(s)" in res.stdout


def test_update_can_skip_serve_work(monkeypatch):
    import omni.cli.commands.update_cmd as update_cmd
    from omni.runtime import daemon as daemon_mod

    retired: list[str] = []

    _patch_remote_version(monkeypatch, "9.9.9")  # offline: a newer version exists
    monkeypatch.setattr(
        update_cmd, "_plan", lambda **_kw: ("pip", ["python", "-m", "pip"], "pip fake")
    )
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda _cmd, check=False: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        update_cmd,
        "list_running_daemons",
        lambda _home: [{"pid": 12345, "project_name": "paper", "channels_arg": "feishu"}],
    )
    monkeypatch.setattr(
        daemon_mod, "stop_legacy_daemons", lambda home: retired.append(str(home)) or [12345]
    )

    res = runner.invoke(app, ["update", "--yes", "--no-restart-serve"])

    assert res.exit_code == 0, res.stdout
    assert retired == []  # --no-restart-serve leaves the background service untouched
    assert "restart was skipped" in res.stdout


# ── ghost hardening: restart re-keys from workspace_root, never from ~/.omni ──
def test_restart_cwd_prefers_workspace_root(tmp_path):
    from omni.cli.commands.serve_cmd import _restart_cwd_from_record

    repo = tmp_path / "repo"
    repo.mkdir()
    rec = {"workspace_root": str(repo), "cwd": str(tmp_path / "stale")}
    assert _restart_cwd_from_record(rec) == str(repo)


def test_restart_cwd_refuses_omni_home_workspace():
    from omni.cli.commands.serve_cmd import _restart_cwd_from_record
    from omni.config.paths import user_home

    ghost = user_home() / "workspaces" / "repo-dead"
    ghost.mkdir(parents=True, exist_ok=True)
    # A workspace keyed off ~/.omni is a ghost → never resurrect it.
    assert _restart_cwd_from_record({"workspace_root": str(ghost), "cwd": str(ghost)}) is None


def test_restart_cwd_named_project_uses_neutral_home():
    from pathlib import Path

    from omni.cli.commands.serve_cmd import _restart_cwd_from_record

    # Named --project daemons carry their selector in argv; cwd is irrelevant.
    assert _restart_cwd_from_record({"workspace_root": "", "cwd": ""}) == str(Path.home())


def test_is_ghost_record():
    from omni.cli.commands.serve_cmd import _is_ghost_record
    from omni.config.paths import user_home

    assert _is_ghost_record({"workspace_root": str(user_home() / "workspaces" / "x")}) is True
    assert _is_ghost_record({"workspace_root": "/Users/me/work/repo"}) is False
    assert _is_ghost_record({"workspace_root": ""}) is False  # named project


def test_restart_records_stop_but_skip_ghosts_without_failing(monkeypatch):
    from omni.cli.commands import serve_cmd
    from omni.config.paths import user_home

    ghost_root = user_home() / "workspaces" / "repo-dead"
    ghost_root.mkdir(parents=True, exist_ok=True)
    good_root = user_home().parent / "good_repo"
    good_root.mkdir(parents=True, exist_ok=True)

    stopped: list[int] = []
    started: list[int] = []
    monkeypatch.setattr(
        serve_cmd, "_stop_daemon_record",
        lambda rec, **kw: (stopped.append(int(rec.get("pid", 0))) or (True, "stopped")),
    )
    monkeypatch.setattr(
        serve_cmd, "_start_daemon_record",
        lambda rec: (started.append(int(rec.get("pid", 0))) or (True, "started")),
    )

    records = [
        {"pid": 1, "workspace_root": str(ghost_root), "project_name": "ghost"},
        {"pid": 2, "workspace_root": str(good_root), "project_name": "real"},
    ]
    ok, detail = serve_cmd.restart_daemon_records(records)

    assert ok is True              # a stopped+skipped ghost is non-fatal for update
    assert stopped == [1, 2]       # both daemons were stopped
    assert started == [2]          # only the real workspace was restarted
    assert "skipped" in detail


# ── untracked-daemon discovery: `omni update` must surface serve processes it
#    can't auto-restart (no pidfile metadata), e.g. ghosts from an older build ──
def test_scan_running_serve_pids_matches_only_omni_serve(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    fake_ps = (
        f"{os.getpid()} /usr/bin/python -m omni.cli.main serve --workers 1\n"  # self → excluded
        "2991 /venv/bin/python /work/cli/src/omni/cli/main.py serve --workers 1\n"
        "17774 /venv/bin/python -m omni.cli.main serve\n"
        "555 /usr/bin/some-daemon --serve\n"  # no omni marker → excluded
        "666 /venv/bin/python -m omni.cli.main chat hi\n"  # omni but not serve → excluded
    )
    monkeypatch.setattr(
        daemon.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=fake_ps),
    )
    assert _REAL_SCAN() == [2991, 17774]


def test_scan_running_serve_pids_empty_on_windows(monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    assert _REAL_SCAN() == []


def test_scan_running_serve_pids_can_scope_to_one_home_service(monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    fake_ps = (
        "111 /venv/bin/python -X omni_service_id=home-a -m omni.cli.main serve run\n"
        "112 /venv/bin/python -Xomni_service_id=home-a -m omni.cli.main serve run\n"
        "222 /venv/bin/python -X omni_service_id=home-b -m omni.cli.main serve run\n"
        "333 /venv/bin/python -X omni_service_id=home-c -m omni.cli.main serve run\n"
        "444 /venv/bin/python -m omni.cli.main serve run\n"
        "556 /venv/bin/python -X omni_service_id=prefix-home-a -m omni.cli.main serve run\n"
        "557 /venv/bin/python -X omni_service_id=home-a-extra -m omni.cli.main serve run\n"
    )
    monkeypatch.setattr(
        daemon.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=fake_ps),
    )

    assert _REAL_SCAN(service_id="home-a") == [111, 112]
    assert _REAL_SCAN(service_id="home-b") == [222]


def test_untracked_serve_pids_excludes_tracked(monkeypatch):
    monkeypatch.setattr(daemon, "scan_running_serve_pids", lambda: [111, 222, 333])
    assert daemon.untracked_serve_pids([{"pid": 222}, {"pid": 999}]) == [111, 333]


def test_untracked_serve_pids_excludes_extra_pids(monkeypatch):
    """The home service's own pid (tracked under <home>/service, not a serve.pid)
    is passed via extra_pids so it is never misreported as an orphan."""
    monkeypatch.setattr(daemon, "scan_running_serve_pids", lambda: [111, 222, 333])
    assert daemon.untracked_serve_pids([{"pid": 222}], extra_pids=[333]) == [111]


def test_reap_serve_processes_terminates_all_and_reports(monkeypatch):
    """Reap SIGTERMs every serve pid and reports the ones confirmed gone."""
    alive = {101, 102}
    killed: list[int] = []
    monkeypatch.setattr(daemon, "_terminate", lambda pid: (killed.append(pid), alive.discard(pid), True)[-1])
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: pid in alive)
    reaped = daemon.reap_serve_processes([101, 102], grace_s=1.0)
    assert sorted(killed) == [101, 102]
    assert reaped == [101, 102]


def test_update_warns_about_stray_serve_processes(monkeypatch):
    import omni.cli.commands.update_cmd as update_cmd
    from omni.runtime import service_control

    _patch_remote_version(monkeypatch, "9.9.9")
    monkeypatch.setattr(
        update_cmd, "_plan", lambda **_kw: ("pip", ["python", "-m", "pip"], "pip fake")
    )
    monkeypatch.setattr(
        update_cmd.subprocess, "run", lambda _cmd, check=False: SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(update_cmd, "list_running_daemons", lambda _home: [])
    inside_guard = False

    class _Guard:
        def __enter__(self):
            nonlocal inside_guard
            inside_guard = True
            return self

        def restore(self):
            return ""

        def __exit__(self, *_exc):
            nonlocal inside_guard
            inside_guard = False
            return False

    def _orphans(_tracked, **_kwargs):
        assert inside_guard is True
        return [2991, 17774]

    monkeypatch.setattr(service_control, "update_guard", lambda *_a, **_k: _Guard())
    monkeypatch.setattr(update_cmd, "untracked_serve_pids", _orphans)

    res = runner.invoke(app, ["update", "--yes"])

    assert res.exit_code == 0, res.stdout
    assert "stray omni serve process(es)" in res.stdout
    assert "2991" in res.stdout


def test_update_ignores_a_transient_pre_singleton_sibling(monkeypatch):
    """A duplicate that disappears while imports finish is not a persistent stray."""
    import omni.cli.commands.update_cmd as update_cmd

    settings = load_settings()
    scans = iter([[64955], []])
    monkeypatch.setattr(
        update_cmd,
        "untracked_serve_pids",
        lambda *_args, **_kwargs: next(scans, []),
    )
    monkeypatch.setattr(update_cmd.time, "sleep", lambda _seconds: None)

    assert update_cmd._stable_untracked_serve_pids(settings) == []


def test_update_reports_only_pids_persistent_across_settle_polls(monkeypatch):
    import omni.cli.commands.update_cmd as update_cmd

    settings = load_settings()
    scans = iter([[100, 200], [100, 300], [100]])
    monkeypatch.setattr(
        update_cmd,
        "untracked_serve_pids",
        lambda *_args, **_kwargs: next(scans),
    )
    monkeypatch.setattr(update_cmd.time, "sleep", lambda _seconds: None)

    assert update_cmd._stable_untracked_serve_pids(settings) == [100]


def test_update_does_not_flag_home_service_as_orphan(monkeypatch):
    """Regression: the home service records its pid under <home>/service (not a
    serve.pid), so the orphan check must exclude it — else every update falsely
    warns about the very service it just (re)started."""
    import omni.cli.commands.update_cmd as update_cmd
    from omni.runtime import daemon as daemon_mod
    from omni.runtime import service_control, service_state
    from omni.runtime.service_state import ServiceDesiredState

    _patch_remote_version(monkeypatch, "9.9.9")
    monkeypatch.setattr(
        update_cmd, "_plan", lambda **_kw: ("pip", ["python", "-m", "pip"], "pip fake")
    )
    monkeypatch.setattr(
        update_cmd.subprocess, "run", lambda _cmd, check=False: SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(update_cmd, "list_running_daemons", lambda _home: [])

    settings = load_settings()
    service_state.write_desired(settings.paths, ServiceDesiredState(enabled=True, configured=True))
    service_state.write_runtime(settings.paths, {"ready": True})  # home service pid = this process
    # `ps` sees exactly the home service process (our pid); the orphan check must
    # exclude it via extra_pids.
    monkeypatch.setattr(
        daemon_mod, "scan_running_serve_pids", lambda **_kwargs: [os.getpid()]
    )

    class _UpdateGuard:
        def __enter__(self):
            return self

        def restore(self):
            return "restarted"

        def __exit__(self, *_exc):
            return False

    # Running + enabled would be quiesced and restored; isolate this orphan
    # accounting test from real process control.
    monkeypatch.setattr(
        service_control, "update_guard", lambda *_a, **_k: _UpdateGuard()
    )

    res = runner.invoke(app, ["update", "--yes"])

    assert res.exit_code == 0, res.stdout
    assert "stray omni serve" not in res.stdout  # the home service is NOT an orphan
    assert "Home service: restarted" in res.stdout


def test_update_is_not_reported_complete_when_restored_service_never_ready(monkeypatch):
    import omni.cli.commands.update_cmd as update_cmd
    from omni.runtime import service_control

    _patch_remote_version(monkeypatch, "9.9.9")
    monkeypatch.setattr(
        update_cmd, "_plan", lambda **_kw: ("pip", ["python"], "pip fake")
    )
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda _cmd, check=False: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(update_cmd, "list_running_daemons", lambda _home: [])

    class _UpdateGuard:
        def __enter__(self):
            return self

        def restore(self):
            raise RuntimeError(
                "Update installed, but the restarted home service did not become ready."
            )

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        service_control, "update_guard", lambda *_a, **_k: _UpdateGuard()
    )

    res = runner.invoke(app, ["update", "--yes"])
    output = res.stdout + res.stderr

    assert res.exit_code == 1
    assert "did not become ready" in output
    assert "Update completed" not in output


def test_update_is_reported_complete_when_restored_service_still_starting(monkeypatch):
    import omni.cli.commands.update_cmd as update_cmd
    from omni.runtime import service_control

    _patch_remote_version(monkeypatch, "9.9.9")
    monkeypatch.setattr(
        update_cmd, "_plan", lambda **_kw: ("pip", ["python"], "pip fake")
    )
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda _cmd, check=False: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(update_cmd, "list_running_daemons", lambda _home: [])

    class _UpdateGuard:
        def __enter__(self):
            return self

        def restore(self):
            return (
                "restarted; still becoming ready (phase=starting). "
                "Channels will reconnect. See `omni serve status`."
            )

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        service_control, "update_guard", lambda *_a, **_k: _UpdateGuard()
    )

    res = runner.invoke(app, ["update", "--yes"])
    output = res.stdout + res.stderr

    assert res.exit_code == 0, output
    assert "Update completed" in output
    assert "still becoming ready" in output
    assert "Home service:" in output


def test_pid_alive_handles_invalid_and_current_pid():
    assert daemon.pid_alive(0) is False
    assert daemon.pid_alive(-1) is False
    assert daemon.pid_alive(os.getpid()) is True


def test_pid_alive_routes_to_windows_branch_without_os_kill(monkeypatch):
    # On Windows os.kill(pid, 0) terminates the process, so the daemon liveness
    # probe must never reach os.kill there. Force the Windows path and assert it
    # delegates to the handle-based check instead.
    monkeypatch.setattr(daemon.sys, "platform", "win32")

    def _boom(*_args, **_kwargs):  # pragma: no cover - must not be called on win32
        raise AssertionError("os.kill must not be used for liveness on Windows")

    monkeypatch.setattr(daemon.os, "kill", _boom)
    monkeypatch.setattr(daemon, "_pid_alive_windows", lambda pid: pid == 4321)

    assert daemon.pid_alive(4321) is True
    assert daemon.pid_alive(9999) is False


def test_detached_popen_kwargs_per_platform(monkeypatch):
    import omni.cli.commands.serve_cmd as serve_cmd

    monkeypatch.setattr(serve_cmd.sys, "platform", "linux")
    assert serve_cmd._detached_popen_kwargs() == {"start_new_session": True}

    monkeypatch.setattr(serve_cmd.sys, "platform", "win32")
    win_kwargs = serve_cmd._detached_popen_kwargs()
    assert "creationflags" in win_kwargs
    assert "start_new_session" not in win_kwargs
    assert win_kwargs["creationflags"] != 0


def test_serve_status_all_marks_ghost(monkeypatch):
    from omni.cli.commands import serve_cmd
    from omni.config.paths import user_home

    rec = {
        "pid": 5,
        "age": 2.0,
        "project_name": "repo",
        "workspace_root": str(user_home() / "workspaces" / "repo-x"),
        "project_dir": str(user_home() / "workspaces" / "repo-x-y"),
        "version": "2.0.0.dev0",
        "channels": ["cli"],
        "channels_mode": "dynamic",
    }
    monkeypatch.setattr(serve_cmd, "list_running_daemons", lambda _home: [rec])

    res = runner.invoke(app, ["serve", "status", "--all"])
    assert res.exit_code == 0, res.stdout
    assert "ghost" in res.stdout


def test_serve_prune_stops_and_removes_ghost(monkeypatch):
    from omni.cli.commands import serve_cmd
    from omni.config.paths import user_home

    ghost_dir = user_home() / "workspaces" / "repo-dead-1234"
    ghost_dir.mkdir(parents=True, exist_ok=True)
    (ghost_dir / "serve.pid").write_text("{}", encoding="utf-8")
    rec = {
        "pid": 4242,
        "age": 1.0,
        "workspace_root": str(ghost_dir),
        "project_dir": str(ghost_dir),
        "pidfile": str(ghost_dir / "serve.pid"),
    }
    monkeypatch.setattr(serve_cmd, "list_running_daemons", lambda _home: [rec])
    monkeypatch.setattr(serve_cmd, "_stop_daemon_record", lambda _r, **_kw: (True, "stopped"))

    res = runner.invoke(app, ["serve", "prune", "--yes"])
    assert res.exit_code == 0, res.stdout
    assert not ghost_dir.exists()  # data dir removed


def test_serve_prune_keep_data_only_stops(monkeypatch):
    from omni.cli.commands import serve_cmd
    from omni.config.paths import user_home

    ghost_dir = user_home() / "workspaces" / "repo-dead-5678"
    ghost_dir.mkdir(parents=True, exist_ok=True)
    rec = {"pid": 9, "age": 1.0, "workspace_root": str(ghost_dir), "project_dir": str(ghost_dir)}
    monkeypatch.setattr(serve_cmd, "list_running_daemons", lambda _home: [rec])
    monkeypatch.setattr(serve_cmd, "_stop_daemon_record", lambda _r, **_kw: (True, "stopped"))

    res = runner.invoke(app, ["serve", "prune", "--yes", "--keep-data"])
    assert res.exit_code == 0, res.stdout
    assert ghost_dir.exists()  # preserved with --keep-data


# ── restart reliability: stop must escalate so a fresh-config daemon can start ──
def test_terminate_and_wait_graceful_stop_sends_only_sigterm(monkeypatch):
    import signal

    from omni.cli.commands import serve_cmd

    state = {"alive": True, "signals": []}

    def fake_kill(_pid, sig):
        state["signals"].append(sig)
        if sig == signal.SIGTERM:
            state["alive"] = False

    monkeypatch.setattr(serve_cmd.os, "kill", fake_kill)
    monkeypatch.setattr(serve_cmd, "_pid_alive", lambda _pid: state["alive"])

    assert serve_cmd._terminate_and_wait(4321, timeout=0.0, kill_grace=0.0) is True
    assert state["signals"] == [signal.SIGTERM]  # graceful exit → no SIGKILL


def test_terminate_and_wait_escalates_to_sigkill_when_sigterm_ignored(monkeypatch):
    import signal

    import pytest

    from omni.cli.commands import serve_cmd

    if not hasattr(signal, "SIGKILL"):
        pytest.skip("SIGKILL escalation is POSIX-only")

    # A daemon that ignores SIGTERM (e.g. blocked in slow channel teardown) must
    # be force-killed so restart can proceed rather than abort and leave the
    # stale-config daemon answering.
    state = {"alive": True, "signals": []}

    def fake_kill(_pid, sig):
        state["signals"].append(sig)
        if sig == signal.SIGKILL:
            state["alive"] = False

    monkeypatch.setattr(serve_cmd.os, "kill", fake_kill)
    monkeypatch.setattr(serve_cmd, "_pid_alive", lambda _pid: state["alive"])

    assert serve_cmd._terminate_and_wait(4321, timeout=0.0, kill_grace=0.0) is True
    assert state["signals"] == [signal.SIGTERM, signal.SIGKILL]


def test_stop_daemon_process_reports_failure_when_process_survives(monkeypatch):
    from omni.cli.commands import serve_cmd
    from omni.cli.state import AppState

    settings = load_settings()
    settings.paths.ensure_dirs()
    daemon.write_pidfile(settings.paths, metadata={"mode": "daemon"})

    monkeypatch.setattr(serve_cmd, "_pid_alive", lambda _pid: True)
    # Simulate an un-killable process so stop reports failure (rather than
    # silently returning success and letting restart start a duplicate).
    monkeypatch.setattr(serve_cmd, "_terminate_and_wait", lambda _pid, **_kw: False)

    ok, detail = serve_cmd.stop_daemon_process(AppState())
    assert ok is False
    assert "SIGKILL" in detail
    daemon.clear_pidfile(settings.paths)


def test_serve_child_argv_pins_launch_cwd(monkeypatch, tmp_path):
    """The detached daemon inherits the exact launch dir as ``--out``.

    Without this the child is re-homed to ``cwd=workspace_root`` (the git root)
    and would mirror deliverables there instead of where the operator started
    ``omni serve`` (and scanned the login QR).
    """
    from omni.cli.commands.serve_cmd import _serve_child_argv
    from omni.cli.state import AppState

    launch = tmp_path / "launch_here"
    launch.mkdir()
    monkeypatch.chdir(launch)

    argv = _serve_child_argv(AppState(), channels="", workers=1)

    assert "--out" in argv
    assert argv[argv.index("--out") + 1] == str(launch)
    # ``--out`` is a root option → must precede the ``serve`` subcommand.
    assert argv.index("--out") < argv.index("serve")


def test_serve_child_argv_preserves_explicit_out(monkeypatch, tmp_path):
    from omni.cli.commands.serve_cmd import _serve_child_argv
    from omni.cli.state import AppState

    monkeypatch.chdir(tmp_path)
    state = AppState(overrides={"artifacts": {"output_dir": "/custom/out"}})

    argv = _serve_child_argv(state, channels="feishu", workers=2)

    # Operator-provided --out wins and is not duplicated with the launch CWD.
    assert argv.count("--out") == 1
    assert argv[argv.index("--out") + 1] == "/custom/out"
    assert "--channels" in argv and argv[argv.index("--channels") + 1] == "feishu"
    assert argv[argv.index("--workers") + 1] == "2"


def test_serve_child_argv_carries_project_and_model(monkeypatch, tmp_path):
    from omni.cli.commands.serve_cmd import _serve_child_argv
    from omni.cli.state import AppState

    monkeypatch.chdir(tmp_path)
    argv = _serve_child_argv(AppState(project="lab", model="gpt-x"), channels="", workers=1)

    assert argv[argv.index("--project") + 1] == "lab"
    assert argv[argv.index("--model") + 1] == "gpt-x"
    # Even a --project daemon still pins its launch dir for output mirroring.
    assert argv[argv.index("--out") + 1] == str(tmp_path)


def test_adopt_launch_dir_marks_serve_dir_trusted(monkeypatch, tmp_path):
    from omni.cli.commands.serve_cmd import _adopt_launch_dir_for_serve
    from omni.config import trust as trustmod

    home = store_shaped_home(tmp_path, "trust")
    launch = tmp_path / "svc"
    launch.mkdir(exist_ok=True)
    monkeypatch.setenv("OMNI_HOME", str(home))
    monkeypatch.chdir(launch)

    assert trustmod.is_trusted(launch, home=home) is False
    _adopt_launch_dir_for_serve()
    # Launching the daemon here is an explicit local act of trust → mirror on.
    assert trustmod.is_trusted(launch, home=home) is True
