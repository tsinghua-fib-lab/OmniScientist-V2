"""Throwaway homes must not persist a user-level home pointer or KeepAlive."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from omni.cli.main import app
from omni.config.paths import OmniPaths, home_selection_file, user_home
from omni.runtime import update_state

runner = CliRunner()


def test_isolated_home_is_marked_converged_without_writing_model_config(omni_home: Path) -> None:
    paths = OmniPaths(
        home=omni_home,
        project_name="default",
        project_dir=omni_home / "projects" / "default",
    )
    assert not (omni_home / "config.toml").is_file()
    assert not update_state.convergence_needed(paths)


def test_prepared_home_disables_keepalive_and_uses_mock_model(prepared_home: Path) -> None:
    text = (prepared_home / "config.toml").read_text(encoding="utf-8")
    assert "ensure_on_launch = false" in text
    assert 'provider = "mock"' in text
    paths = OmniPaths(
        home=prepared_home,
        project_name="default",
        project_dir=prepared_home / "projects" / "default",
    )
    assert not update_state.convergence_needed(paths)


def test_isolated_home_remaps_xdg_so_init_home_cannot_leak(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OMNI_HOME", raising=False)
    custom = tmp_path / "walkthrough-home"
    result = runner.invoke(app, ["init", "--non-interactive", "--home", str(custom), "--provider", "mock"])
    assert result.exit_code == 0
    pointer = home_selection_file()
    assert pointer.is_file()
    assert pointer.read_text(encoding="utf-8").strip() == str(custom.resolve())
    assert Path.home().joinpath(".config") in pointer.parents
    assert user_home() == custom.resolve()
