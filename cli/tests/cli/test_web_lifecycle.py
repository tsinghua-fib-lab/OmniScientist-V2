"""REPL ``/web`` is a managed background service; shell ``omni web`` stays foreground."""

from __future__ import annotations

import logging
import os
import subprocess
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
    popen_kwargs: list[dict[str, object]] = []

    class _Proc:
        pid = 5555

        def poll(self) -> None:
            return None

    def fake_popen(argv, **_kwargs):  # noqa: ANN001, ANN003
        popped.append(list(argv))
        popen_kwargs.append(dict(_kwargs))
        return _Proc()

    monkeypatch.setattr(web_cmd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(web_cmd, "port_listening", lambda host, port: host == "127.0.0.1" and port == 1290)

    ok, detail = web_cmd.start_web_process(state, host="127.0.0.1", port=1290)
    assert ok is True
    assert "http://127.0.0.1:1290" in detail
    assert "background" in detail
    assert popped and popped[0][-4:] == ["web", "--host", "127.0.0.1", "--port", "1290"][-4:]
    assert "start" not in popped[0]
    assert popen_kwargs[0]["env"].get("OMNI_WEB_MANAGED_LOG_STREAM") != "1"
    assert popen_kwargs[0]["stdout"] is subprocess.DEVNULL
    assert popen_kwargs[0]["stderr"] is subprocess.DEVNULL
    log_path = settings.paths.logs_dir / f"web-{settings.paths.project_name}.log"
    if os.name == "posix":
        assert log_path.stat().st_mode & 0o777 == 0o600


def test_start_child_argv_is_foreground_omni_web(tmp_path, monkeypatch) -> None:
    from omni.cli.commands import web_cmd

    state = AppState(project="demo")
    argv = web_cmd._foreground_argv(state, host="127.0.0.1", port=1088)
    assert argv[-5:] == ["web", "--host", "127.0.0.1", "--port", "1088"]
    assert "--project" in argv


def test_foreground_web_keeps_diagnostics_out_of_terminal(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    from omni.cli.commands import web_cmd
    from omni.web import app as web_app

    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "home"))
    settings = load_settings(cwd=tmp_path, trusted=True)
    state = AppState()
    monkeypatch.setattr(state, "settings", lambda: settings)
    monkeypatch.setattr(web_cmd, "ensure_web_ui", lambda: None)
    monkeypatch.setattr(web_cmd, "web_info", lambda _paths: None)
    monkeypatch.setattr(web_cmd, "write_pidfile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_cmd, "clear_pidfile_if_owner", lambda _paths: None)
    monkeypatch.setattr(web_cmd, "spa_version", lambda _dist: "2.0.0rc6")
    monkeypatch.setattr(web_cmd, "package_version", lambda: "2.0.0rc6")
    monkeypatch.setattr(
        web_app,
        "create_app",
        lambda **_kwargs: SimpleNamespace(
            state=SimpleNamespace(hub=SimpleNamespace(begin_shutdown=lambda: None))
        ),
    )

    def _run(_app, *, host, port, on_ready, log_level):  # noqa: ANN001
        assert (host, port, log_level) == ("127.0.0.1", 1088, logging.INFO)
        logging.getLogger("omni.web.test").warning(
            "request failed api_key=sk-1234567890abcdef",
            extra={"event": "request.failed"},
        )
        on_ready()

    monkeypatch.setattr(web_cmd, "run_web_server", _run)

    web_cmd._run_foreground(state, host="127.0.0.1", port=1088)

    captured = capsys.readouterr()
    assert "omni web: http://127.0.0.1:1088" in captured.out
    assert "stopped" not in captured.out
    assert "request failed" not in captured.out + captured.err
    diagnostics = (
        settings.paths.logs_dir / f"web-{settings.paths.project_name}.log"
    ).read_text(encoding="utf-8")
    assert "component=web" in diagnostics
    assert "event=request.failed" in diagnostics
    assert "sk-1234567890abcdef" not in diagnostics
    assert "[REDACTED]" in diagnostics


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
