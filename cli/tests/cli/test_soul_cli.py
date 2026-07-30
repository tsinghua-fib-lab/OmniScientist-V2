"""CLI coverage for the scientist-persona command wrapper."""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from omni.cli.commands import soul_cmd
from omni.cli.main import app

runner = CliRunner()


def _write_persona(root: Path, scientist_id: str, name: str) -> None:
    target = root / scientist_id
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(
        json.dumps({"scientist_id": scientist_id}), encoding="utf-8"
    )
    (target / "identity.json").write_text(
        json.dumps(
            {
                "scientist_id": scientist_id,
                "scientist_name": name,
                "aliases": [name, f"{name} alias"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_soul_list_uses_project_scanner_before_home(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "研究项目 (测试)"
    project.mkdir()
    _write_persona(project / "scientist-kg", "local-scientist", "Local Scientist")
    _write_persona(
        Path(os.environ["OMNI_HOME"]) / "scientist-kg",
        "home-scientist",
        "Home Scientist",
    )
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["soul", "list", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["project_root"] == str(project.resolve())
    assert payload["kg_root"] == str((project / "scientist-kg").resolve())
    assert [row["scientist_id"] for row in payload["available"]] == [
        "local-scientist"
    ]


def test_soul_list_falls_back_to_home_scanner(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "plain-project"
    project.mkdir()
    home_kg = Path(os.environ["OMNI_HOME"]) / "scientist-kg"
    _write_persona(home_kg, "home-scientist", "Home Scientist")
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["soul", "list", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["kg_root"] == str(home_kg.resolve())
    assert payload["available"][0]["scientist_name"] == "Home Scientist"


def test_soul_status_reports_ready_active_persona(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "active-project"
    lock = project / ".soulagent" / "lock"
    lock.mkdir(parents=True)
    (lock / "ready").touch()
    (project / ".soulagent" / "state.json").write_text(
        json.dumps(
            {
                "host": "omniscientist",
                "scientist_id": "kaiming-he",
                "scientist_name": "Kaiming He",
            }
        ),
        encoding="utf-8",
    )
    (project / "role.md").write_text("Residual thinking.", encoding="utf-8")
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["soul", "status", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["active"] is True
    assert payload["scientist_id"] == "kaiming-he"
    assert payload["scientist_name"] == "Kaiming He"


def test_soul_create_dry_run_resolves_unicode_paths_without_writing(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "研究项目 (测试)"
    project.mkdir()
    monkeypatch.chdir(project)

    result = runner.invoke(
        app,
        [
            "soul",
            "create",
            "Geoffrey Hinton",
            "--field",
            "machine learning",
            "--dry-run",
        ],
        env={"COLUMNS": "300"},
    )

    assert result.exit_code == 0, result.stdout
    assert "Geoffrey Hinton" in result.stdout
    assert str(project.resolve()) in result.stdout
    assert str((project / "scientist-distillations").resolve()) in result.stdout
    assert not (project / "scientist-distillations").exists()


def test_soul_create_submits_a_focused_distiller_turn(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "create-project"
    project.mkdir()
    monkeypatch.chdir(project)
    captured: dict[str, object] = {}

    async def _fake_run_one_shot(state, prompt: str, **kwargs):  # noqa: ANN001
        captured["state"] = state
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return None

    monkeypatch.setattr(soul_cmd, "run_one_shot", _fake_run_one_shot)

    result = runner.invoke(
        app,
        [
            "soul",
            "create",
            "Geoffrey Hinton",
            "--institution",
            "University of Toronto",
            "--max-sources",
            "25",
            "--detach",
        ],
    )

    assert result.exit_code == 0, result.stdout
    prompt = str(captured["prompt"])
    assert "$scientist-kg-distiller" in prompt
    assert "Geoffrey Hinton" in prompt
    assert "University of Toronto" in prompt
    assert "Maximum source candidates: 25" in prompt
    assert "Creating the KG must not activate it" in prompt
    assert captured["kwargs"] == {"quiet": False, "verbose": False, "detach": True}


def test_soul_group_help_exposes_status_list_and_create() -> None:
    result = runner.invoke(app, ["soul", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "status" in result.stdout
    assert "list" in result.stdout
    assert "create" in result.stdout


def test_soul_help_explains_lifecycle_and_create_does_not_activate() -> None:
    result = runner.invoke(app, ["soul", "help"], env={"COLUMNS": "220"})

    assert result.exit_code == 0, result.stdout
    for label in ("1. Discover", "2. Create", "3. Activate", "4. Check", "5. Unload"):
        assert label in result.stdout
    assert "creation may be a long" in result.stdout
    assert "Create and activate are deliberately separate" in result.stdout
    assert "/soul create \"Geoffrey Hinton\"" in result.stdout
    assert "Use `/soul ...` in the interactive CLI" in result.stdout
    assert "Shell equivalents use `omni soul ...`" in result.stdout


def test_bare_soul_uses_human_status_view(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "bare-status"
    project.mkdir()
    home_kg = Path(os.environ["OMNI_HOME"]) / "scientist-kg"
    _write_persona(home_kg, "home-scientist", "Home Scientist")
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["soul"])

    assert result.exit_code == 0, result.stdout
    assert "Scientist personas available (home-scientist)" in result.stdout
    assert not result.stdout.lstrip().startswith("{")


def test_soul_create_is_registered_for_repl_external_dispatch() -> None:
    from omni.cli.main import _REPL_EXTERNAL_COMMANDS, _external_repl_command_args
    from omni.cli.state import AppState

    assert "soul" in _REPL_EXTERNAL_COMMANDS
    assert _external_repl_command_args(AppState(), "/soul help") == ["soul", "help"]
    assert _external_repl_command_args(
        AppState(profile="dev"), '/soul create "Geoffrey Hinton" --dry-run'
    ) == [
        "--profile",
        "dev",
        "soul",
        "create",
        "Geoffrey Hinton",
        "--dry-run",
    ]
