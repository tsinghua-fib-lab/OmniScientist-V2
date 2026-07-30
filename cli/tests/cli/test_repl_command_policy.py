"""REPL command I/O contracts stay visible, interactive, and secret-safe.

User journeys:
- A user running a non-interactive slash command keeps its result in transcript.
- A user running a prompt, pager, editor, or foreground service gets a real TTY.
- A user entering credentials never leaves the raw value in transcript or history.
"""

from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from omni.cli.repl_input import ReplInputBox
from omni.cli.repl_output import use_output_sink
from omni.cli.repl_tui import ReplTui


@pytest.mark.parametrize(
    "tokens",
    [
        ["config", "model", "-k", "secret"],
        ["config", "embeddings"],
        ["config", "home"],
        ["channel", "help"],
        ["channel", "list"],
        ["channel", "add", "feishu"],
        ["channel", "remove", "feishu"],
        ["channel", "test", "cli"],
        ["init", "--non-interactive"],
        ["init", "--help"],
        ["update", "--check"],
        ["update", "status"],
        ["uninstall", "--dry-run"],
        ["uninstall", "--yes"],
        ["terminal", "help"],
        ["terminal", "setup", "--check"],
        ["terminal", "setup", "--dry-run"],
        ["terminal", "setup", "--yes"],
        ["task", "watch", "--once"],
        ["skills", "trust", "demo", "--yes"],
        ["serve", "prune", "--yes"],
    ],
)
def test_noninteractive_variants_use_captured_transcript(tokens: list[str]) -> None:
    from omni.cli.repl_command_policy import ReplCommandMode, classify_repl_command

    assert classify_repl_command(tokens).mode is ReplCommandMode.CAPTURED


@pytest.mark.parametrize(
    ("tokens", "expected_mode"),
    [
        (["init"], "interactive_tty"),
        (["channel", "login", "wechat"], "interactive_tty"),
        (["uninstall"], "interactive_tty"),
        (["terminal", "setup"], "interactive_tty"),
        (["memory", "edit"], "interactive_tty"),
        (["memory", "list", "--pager"], "interactive_tty"),
        (["skills", "list", "--pager"], "interactive_tty"),
        (["skills", "trust", "demo"], "interactive_tty"),
        (["serve", "prune"], "interactive_tty"),
        (["task", "watch"], "foreground_tty"),
        (["serve"], "foreground_tty"),
        (["serve", "daemon"], "foreground_tty"),
        (["serve", "poller"], "foreground_tty"),
        (["mcp", "serve"], "foreground_tty"),
    ],
)
def test_interactive_variants_get_the_required_terminal_mode(
    tokens: list[str],
    expected_mode: str,
) -> None:
    from omni.cli.repl_command_policy import classify_repl_command

    assert classify_repl_command(tokens).mode.value == expected_mode


@pytest.mark.asyncio
async def test_successful_tty_command_publishes_a_redacted_durable_summary(monkeypatch) -> None:
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    tui = ReplTui(commands=())
    raw_secret = "super-secret-app-token"

    @asynccontextmanager
    async def fake_suspended():
        yield

    def fake_run(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(tui, "suspended", fake_suspended)
    monkeypatch.setattr(cli_main.subprocess, "run", fake_run)
    with use_output_sink(tui):
        returncode = await cli_main._run_repl_external_command(
            AppState(),
            f"/channel login feishu --app-id demo --app-secret {raw_secret}",
        )

    assert returncode == 0
    assert "Interactive command finished" in tui.transcript.text
    assert "/channel login feishu" in tui.transcript.text
    assert "REDACTED" in tui.transcript.text
    assert raw_secret not in tui.transcript.text


@pytest.mark.asyncio
async def test_failed_tty_command_records_a_durable_warn_note(monkeypatch) -> None:
    """A non-zero interactive child still leaves a note in the durable transcript
    (not the input row), so the dock stays clean after it returns."""
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    tui = ReplTui(commands=())

    @asynccontextmanager
    async def fake_suspended():
        yield

    monkeypatch.setattr(tui, "suspended", fake_suspended)
    monkeypatch.setattr(
        cli_main.subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 3)
    )
    with use_output_sink(tui):
        returncode = await cli_main._run_repl_external_command(
            AppState(), "/channel login feishu --app-id demo"
        )

    assert returncode == 3
    assert "Interactive command exited with code 3" in tui.transcript.text


def test_note_after_interactive_records_note_when_app_is_idle() -> None:
    """Outside a running dock (classic mode / tests) the note is still recorded in
    the durable transcript rather than dropped."""
    tui = ReplTui(commands=())
    tui.note_after_interactive("Interactive command finished: /channel login feishu.")
    tui.note_after_interactive("Interactive command exited with code 2.", style="warn")
    text = tui.transcript.text
    assert "Interactive command finished: /channel login feishu." in text
    assert "Interactive command exited with code 2." in text


@pytest.mark.asyncio
async def test_tui_displays_redacted_command_but_dispatches_the_original_secret() -> None:
    tui = ReplTui(commands=())
    raw = "/config model -k super-secret-model-token"

    assert tui.accept_text(raw) is True

    submission = await tui.read_submission_async()
    assert submission.text == raw
    assert submission.turn_id
    assert "super-secret-model-token" not in tui.transcript.text
    assert "REDACTED" in tui.transcript.text


def test_init_semantic_scholar_key_is_redacted_from_repl_display_and_history() -> None:
    from omni.cli.repl_command_policy import (
        command_contains_sensitive_data,
        redact_repl_command,
    )

    secret = "s2-repl-secret-789"
    raw = f"/init --non-interactive --semantic-scholar-api-key {secret}"

    redacted = redact_repl_command(raw)

    assert secret not in redacted
    assert "--semantic-scholar-api-key REDACTED" in redacted
    assert command_contains_sensitive_data(raw) is True

    tui_history = ReplTui(commands=())._input_buffer.history
    tui_history.append_string(raw)
    classic_history = ReplInputBox(enabled=False)._ensure_session().history
    classic_history.append_string(raw)

    assert tui_history.get_strings() == []
    assert classic_history.get_strings() == []


def test_tui_and_classic_histories_omit_sensitive_commands() -> None:
    tui = ReplTui(commands=())
    tui_history = tui._input_buffer.history
    tui_history.append_string("normal question")
    tui_history.append_string("/config model -k secret-value")

    classic = ReplInputBox(enabled=False)
    classic_history = classic._ensure_session().history
    classic_history.append_string("another normal question")
    classic_history.append_string("/channel login feishu --app-secret secret-value")

    assert tui_history.get_strings() == ["normal question"]
    assert classic_history.get_strings() == ["another normal question"]


@pytest.mark.asyncio
async def test_update_check_streams_into_transcript_without_suspending_tui(monkeypatch) -> None:
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    tui = ReplTui(commands=())
    calls: list[list[str]] = []

    async def fake_stream(_tui, args):  # noqa: ANN001
        calls.append(args)
        _tui.append_output("update check result\n")
        return 0

    def unexpected_terminal(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("--check must not suspend the managed TUI")

    monkeypatch.setattr(cli_main, "_stream_repl_external_command", fake_stream)
    monkeypatch.setattr(cli_main, "_repl_update_in_terminal", unexpected_terminal)
    with use_output_sink(tui):
        restart = await cli_main._repl_update(AppState(), "/update --check")

    assert restart is False
    assert calls == [["update", "--check"]]
    assert "update check result" in tui.transcript.text


@pytest.mark.asyncio
async def test_update_status_streams_without_restarting_the_repl(monkeypatch) -> None:
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    tui = ReplTui(commands=())
    calls: list[list[str]] = []

    async def fake_stream(_tui, args):  # noqa: ANN001
        calls.append(args)
        return 0

    def unexpected_terminal(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("read-only update status must not suspend or restart")

    monkeypatch.setattr(cli_main, "_stream_repl_external_command", fake_stream)
    monkeypatch.setattr(cli_main, "_repl_update_in_terminal", unexpected_terminal)
    with use_output_sink(tui):
        restart = await cli_main._repl_update(AppState(), "/update status")

    assert restart is False
    assert calls == [["update", "status"]]


@pytest.mark.asyncio
async def test_successful_repl_update_requests_session_continuity(monkeypatch) -> None:
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    async def successful_update(_state, _line):  # noqa: ANN001
        return True

    monkeypatch.setattr(cli_main, "_repl_update", successful_update)
    agent = object()

    result = await cli_main._repl_command(
        agent,
        AppState(),
        "/update",
        "session-123",
    )

    assert result.restart is True
    assert result.resume_after_restart is True


def test_classic_update_status_with_global_options_does_not_restart(monkeypatch) -> None:
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):  # noqa: ANN001
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli_main.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cli_main.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )

    restart = cli_main._repl_update_in_terminal(
        AppState(profile="dev", model="test-model"),
        "/update status",
    )

    assert restart is False
    assert calls
    assert calls[0][-2:] == ["update", "status"]
    assert "--profile" in calls[0]
    assert "--model" in calls[0]


def test_tasks_watch_once_does_not_clear_the_managed_transcript(monkeypatch) -> None:
    from omni.cli.commands import tasks_cmd
    from omni.cli.state import AppState

    class Agent:
        paths = SimpleNamespace()
        tasks = SimpleNamespace()

        async def aclose(self) -> None:
            return None

    async def fake_make_agent(_state):  # noqa: ANN001
        agent = Agent()

        async def list_tasks(**_kwargs):  # noqa: ANN003
            return []

        agent.tasks.list_tasks = list_tasks
        return agent

    monkeypatch.setattr(tasks_cmd, "make_agent", fake_make_agent)
    monkeypatch.setattr(tasks_cmd, "render_task_list", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tasks_cmd.console,
        "clear",
        lambda: (_ for _ in ()).throw(AssertionError("--once must not clear the TUI")),
    )

    tasks_cmd.watch_cmd(
        SimpleNamespace(obj=AppState()),
        task_id="",
        status="",
        kind="turn",
        limit=20,
        show_all=False,
        session="",
        archived=False,
        interval=2.0,
        once=True,
    )


@pytest.mark.asyncio
async def test_real_config_success_is_captured_and_redacted(tmp_path, monkeypatch) -> None:
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni-home"))
    tui = ReplTui(commands=())
    raw_secret = "integration-secret-token"

    with use_output_sink(tui):
        returncode = await cli_main._run_repl_external_command(
            AppState(),
            f"/config model -k {raw_secret}",
        )

    assert returncode == 0
    assert "Updated model configuration" in tui.transcript.text
    assert "redacted" in tui.transcript.text.lower()
    assert raw_secret not in tui.transcript.text


def test_restart_notice_crosses_one_exec_boundary_without_repeating() -> None:
    from omni.cli.repl_command_policy import consume_restart_notice, remember_restart_notice

    environ: dict[str, str] = {}
    remember_restart_notice(environ)

    assert consume_restart_notice(environ) == (
        "Previous command completed; Omni restarted to load the new runtime state."
    )
    assert consume_restart_notice(environ) == ""
