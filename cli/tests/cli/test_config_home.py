"""Persistent Omni data-directory selection tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from omni.cli.main import app
from omni.cli.state import AppState

runner = CliRunner()


def test_bare_omni_first_launch_can_choose_custom_home(tmp_path, monkeypatch):
    from omni.cli import main as main_module
    from omni.config.paths import home_selection_file, user_home

    custom = tmp_path / "research-home"
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(main_module, "_terminal_is_interactive", lambda: True)
    monkeypatch.setattr(main_module, "_repl", lambda _state, **_kwargs: None)

    # Inputs: custom home, provider 4 (mock), then Enter through embeddings,
    # the optional Semantic Scholar key, skill export, and MCP registration.
    result = runner.invoke(app, [], input=f"{custom}\n4\n\n\n\n\n")

    assert result.exit_code == 0
    assert user_home() == custom.resolve()
    assert (custom / "config.toml").is_file()
    assert home_selection_file().read_text(encoding="utf-8").strip() == str(custom.resolve())


def test_init_non_interactive_can_persist_custom_home(tmp_path, monkeypatch):
    from omni.config.paths import home_selection_file, user_home

    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    custom = tmp_path / "omni-data"

    result = runner.invoke(app, ["init", "--non-interactive", "--home", str(custom)])

    assert result.exit_code == 0
    assert user_home() == custom.resolve()
    assert (custom / "config.toml").is_file()
    assert home_selection_file().read_text(encoding="utf-8").strip() == str(custom.resolve())
    assert custom.name in result.stdout


def test_config_home_switch_and_reset(tmp_path, monkeypatch):
    from omni.config.paths import default_user_home, home_selection_file, user_home

    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    original = default_user_home()
    original.mkdir(parents=True, exist_ok=True)
    marker = original / "preserved.txt"
    marker.write_text("keep", encoding="utf-8")
    custom = tmp_path / "custom-omni"

    changed = runner.invoke(app, ["config", "home", str(custom)])
    assert changed.exit_code == 0
    assert user_home() == custom.resolve()
    assert custom.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep"
    shown = runner.invoke(app, ["config", "home"])
    assert shown.exit_code == 0
    compact_output = "".join(shown.stdout.split())
    assert str(custom.resolve()) in compact_output
    assert str(home_selection_file()) in compact_output
    custom_marker = custom / "also-preserved.txt"
    custom_marker.write_text("keep custom", encoding="utf-8")

    reset = runner.invoke(app, ["config", "home", "--reset"])
    assert reset.exit_code == 0
    assert user_home() == original
    assert marker.read_text(encoding="utf-8") == "keep"
    assert custom_marker.read_text(encoding="utf-8") == "keep custom"
    assert not home_selection_file().exists()


def test_config_home_refuses_to_override_environment(tmp_path, monkeypatch):
    selected = tmp_path / "environment-home"
    monkeypatch.setenv("OMNI_HOME", str(selected))

    result = runner.invoke(app, ["config", "home", str(tmp_path / "different")])

    assert result.exit_code == 2
    assert "Unset OMNI_HOME" in result.stdout + result.stderr


def test_config_data_dir_is_read_only():
    result = runner.invoke(app, ["config", "set", "data_dir", "/tmp/ignored"])

    assert result.exit_code == 2
    assert "config home" in result.stdout + result.stderr


@pytest.mark.asyncio
async def test_repl_config_home_requests_a_full_restart(tmp_path, monkeypatch):
    import omni.cli.main as main

    dispatched: list[str] = []

    async def fake_external(_state, command):  # noqa: ANN001
        dispatched.append(command)
        return 0

    monkeypatch.setattr(main, "_run_repl_external_command", fake_external)

    result = await main._repl_command(
        SimpleNamespace(),
        AppState(),
        f"/config home {tmp_path / 'new-home'}",
        "session-123",
    )

    assert dispatched == [f"config home {tmp_path / 'new-home'}"]
    assert result.restart is True
    assert result.resume_after_restart is False
