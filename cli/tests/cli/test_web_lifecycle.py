"""REPL ``/web`` is a managed background service; shell ``omni web`` stays foreground."""

from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from omni.cli.main import app
from omni.cli.state import AppState
from omni.config import load_settings


def test_cli_web_help_lists_manage_commands() -> None:
    result = CliRunner().invoke(app, ["web", "--help"])
    assert result.exit_code == 0
    shown = result.stdout
    assert "1088" in shown
    for name in ("start", "stop", "status", "restart", "port"):
        assert name in shown


def test_cli_web_start_help_is_background() -> None:
    result = CliRunner().invoke(app, ["web", "start", "--help"])
    assert result.exit_code == 0
    assert "background" in result.stdout.lower()


def test_stop_without_pidfile_is_idle(tmp_path, monkeypatch) -> None:
    from omni.cli.commands import web_cmd

    monkeypatch.chdir(tmp_path)
    state = AppState()
    monkeypatch.setattr(state, "settings", lambda: load_settings(cwd=tmp_path, trusted=True))
    ok, detail = web_cmd.stop_web_process(state)
    assert ok is True
    assert "not running" in detail


def test_start_reports_already_running(tmp_path, monkeypatch) -> None:
    from omni.cli.commands import web_cmd

    monkeypatch.chdir(tmp_path)
    settings = load_settings(cwd=tmp_path, trusted=True)
    state = AppState()
    monkeypatch.setattr(state, "settings", lambda: settings)
    monkeypatch.setattr(
        web_cmd,
        "web_info",
        lambda _paths: {"pid": 4242, "url": "http://127.0.0.1:1088", "host": "127.0.0.1", "port": 1088},
    )
    ok, detail = web_cmd.start_web_process(state, host="127.0.0.1", port=1088)
    assert ok is True
    assert "already running" in detail
    assert "4242" in detail


def test_start_detaches_and_returns_when_port_listens(tmp_path, monkeypatch) -> None:
    from omni.cli.commands import web_cmd

    monkeypatch.chdir(tmp_path)
    settings = load_settings(cwd=tmp_path, trusted=True)
    state = AppState()
    monkeypatch.setattr(state, "settings", lambda: settings)

    popped: list[list[str]] = []

    class _Proc:
        pid = 5555

        def poll(self) -> None:
            return None

    def fake_popen(argv, **_kwargs):  # noqa: ANN001, ANN003
        popped.append(list(argv))
        return _Proc()

    monkeypatch.setattr(web_cmd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(web_cmd, "port_listening", lambda host, port: host == "127.0.0.1" and port == 1290)

    ok, detail = web_cmd.start_web_process(state, host="127.0.0.1", port=1290)
    assert ok is True
    assert "http://127.0.0.1:1290" in detail
    assert "background" in detail
    assert popped and popped[0][-4:] == ["web", "--host", "127.0.0.1", "--port", "1290"][-4:]
    assert "start" not in popped[0]


def test_start_child_argv_is_foreground_omni_web(tmp_path, monkeypatch) -> None:
    from omni.cli.commands import web_cmd

    state = AppState(project="demo")
    argv = web_cmd._foreground_argv(state, host="127.0.0.1", port=1088)
    assert argv[-5:] == ["web", "--host", "127.0.0.1", "--port", "1088"]
    assert "--project" in argv


def test_port_restarts_when_already_running_elsewhere(tmp_path, monkeypatch) -> None:
    from omni.cli.commands import web_cmd

    settings = load_settings(cwd=tmp_path, trusted=True)
    state = AppState()
    monkeypatch.setattr(state, "settings", lambda: settings)
    restarts: list[tuple[str, int]] = []
    monkeypatch.setattr(
        web_cmd,
        "web_info",
        lambda _paths: {
            "pid": 7,
            "url": "http://127.0.0.1:1088",
            "host": "127.0.0.1",
            "port": 1088,
        },
    )
    monkeypatch.setattr(
        web_cmd,
        "restart_web_process",
        lambda _state, host, port: restarts.append((host, port)) or (True, "moved"),
    )
    monkeypatch.setattr(web_cmd, "_report", lambda ok, detail: None)
    web_cmd.port_cmd(SimpleNamespace(obj=state), port=1290, host="127.0.0.1")
    assert restarts == [("127.0.0.1", 1290)]
