from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from omni.cli.main import app
from omni.cli.terminal_harness import (
    MODIFY_OTHER_KEYS_DISABLE,
    MODIFY_OTHER_KEYS_ENABLE,
    TerminalKeyboardProtocol,
    apply_tmux_setup,
    inspect_terminal,
    plan_tmux_setup,
)

runner = CliRunner()


class _RawOutput:
    def __init__(self) -> None:
        self.raw: list[str] = []
        self.flushes = 0

    def write_raw(self, value: str) -> None:
        self.raw.append(value)

    def flush(self) -> None:
        self.flushes += 1


def test_keyboard_protocol_negotiates_and_restores_terminal_state() -> None:
    output = _RawOutput()
    protocol = TerminalKeyboardProtocol(output, enabled=True)

    protocol.start()
    protocol.start()
    protocol.stop()
    protocol.stop()

    assert output.raw == [MODIFY_OTHER_KEYS_ENABLE, MODIFY_OTHER_KEYS_DISABLE]
    assert output.flushes == 2


def test_terminal_report_explains_tmux_extended_key_blocker() -> None:
    replies = {
        ("tmux", "-V"): "tmux 3.6b\n",
        ("tmux", "show-options", "-gqv", "extended-keys"): "off\n",
        ("tmux", "show-options", "-gqv", "allow-passthrough"): "off\n",
        ("tmux", "show-options", "-gqv", "terminal-features"): "xterm*:clipboard\n",
        ("tmux", "show-options", "-sqv", "extended-keys-format"): "xterm\n",
    }

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, replies.get(tuple(command), ""), "")

    report = inspect_terminal(
        environ={
            "TERM": "screen-256color",
            "TERM_PROGRAM": "iTerm.app",
            "TMUX": "/tmp/tmux-501/default,1,0",
        },
        interactive=True,
        command_runner=run,
    )

    assert report.host_terminal == "iTerm.app"
    assert report.tmux.active is True
    assert report.tmux.extended_keys == "off"
    assert report.tmux.allow_passthrough == "off"
    assert report.shift_enter_ready is False
    assert "omni terminal setup" in report.repair_command
    assert any("extended-keys" in issue for issue in report.issues)


def test_tmux_setup_is_preserving_backed_up_and_idempotent(tmp_path: Path) -> None:
    config = tmp_path / ".tmux.conf"
    original = "set -g mouse on\n"
    config.write_text(original, encoding="utf-8")

    first = plan_tmux_setup(config)
    assert first.changed is True
    assert original in first.updated
    assert "set -s extended-keys on" in first.updated
    assert "set -g allow-passthrough on" in first.updated
    assert "xterm*:extkeys" in first.updated

    applied = apply_tmux_setup(first)
    assert applied.changed is True
    assert applied.backup_path is not None
    assert applied.backup_path.read_text(encoding="utf-8") == original
    assert config.read_text(encoding="utf-8") == first.updated

    second = plan_tmux_setup(config)
    assert second.changed is False
    reapplied = apply_tmux_setup(second)
    assert reapplied.changed is False
    assert reapplied.backup_path is None


def test_terminal_setup_requires_confirmation_before_writing(tmp_path: Path) -> None:
    config = tmp_path / ".tmux.conf"
    config.write_text("set -g mouse on\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["terminal", "setup", "--tmux-config", str(config)],
        input="n\n",
    )

    assert result.exit_code == 0
    assert "No terminal configuration was changed" in result.stdout
    assert config.read_text(encoding="utf-8") == "set -g mouse on\n"
    assert list(tmp_path.glob(".tmux.conf.omni-backup-*")) == []


def test_terminal_setup_has_shell_and_repl_compatible_entrypoints(tmp_path: Path) -> None:
    config = tmp_path / ".tmux.conf"

    group = runner.invoke(
        app,
        ["terminal", "setup", "--tmux-config", str(config), "--yes"],
    )
    alias = runner.invoke(app, ["terminal-setup", "--check"])

    assert group.exit_code == 0, group.stdout
    assert config.exists()
    assert alias.exit_code == 0, alias.stdout
    assert "Shift+Enter" in alias.stdout


def test_terminal_report_is_cross_platform_without_tmux() -> None:
    report = inspect_terminal(
        environ={"TERM": "xterm-256color", "WT_SESSION": "session"},
        interactive=True,
        platform_name="win32",
        command_runner=lambda command: subprocess.CompletedProcess(command, 1, "", "missing"),
    )

    assert report.platform == "windows"
    assert report.host_terminal == "Windows Terminal"
    assert report.tmux.active is False
    assert report.fallback_shortcut == "Ctrl+J"


def test_doctor_includes_terminal_keyboard_diagnostics(monkeypatch) -> None:
    from omni.cli.commands import doctor_cmd

    report = inspect_terminal(
        environ={"TERM": "xterm-256color"},
        interactive=True,
        platform_name="linux",
        command_runner=lambda command: subprocess.CompletedProcess(command, 1, "", ""),
    )
    monkeypatch.setattr(doctor_cmd, "inspect_terminal", lambda: report)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.stdout
    assert "Host terminal" in result.stdout
    assert "Extended keyboard" in result.stdout
    assert "omni terminal setup" in result.stdout
