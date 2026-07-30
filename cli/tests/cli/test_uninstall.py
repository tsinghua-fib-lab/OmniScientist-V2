"""Ownership and safety contracts for ``omni uninstall``."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace

import tomli_w
from typer.testing import CliRunner

from omni.cli.main import app
from omni.compat import integrations
from omni.config.paths import get_paths
from omni.data import BUILTIN_SKILLS_DIR
from omni.runtime import uninstall
from omni.skills_runtime.discovery import skill_dirs_in

runner = CliRunner()


def _paths(tmp_path: Path):  # noqa: ANN202
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return get_paths(cwd=workspace)


def test_uninstall_plan_is_pure_and_dry_run_json_creates_no_home(tmp_path):
    paths = _paths(tmp_path)
    assert not paths.home.exists()

    plan = uninstall.build_uninstall_plan(
        paths,
        purge=False,
        all_project_data=False,
        all_installations=False,
        remove_program=False,
        remove_untracked_exports=False,
    )

    assert not paths.home.exists()
    assert any(action.category == "service" for action in plan.actions)

    result = runner.invoke(
        app,
        ["uninstall", "--dry-run", "--keep-program", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["purge"] is False
    assert payload["remove_program"] is False
    assert not paths.home.exists()


def test_all_project_data_requires_explicit_purge():
    result = runner.invoke(
        app,
        ["uninstall", "--all-project-data", "--dry-run", "--keep-program"],
    )
    assert result.exit_code == 2
    assert "requires --purge" in result.output


def test_json_execution_requires_non_interactive_confirmation():
    result = runner.invoke(
        app,
        ["uninstall", "--keep-program", "--json"],
    )
    assert result.exit_code == 2
    assert "requires --yes" in result.output


def test_default_execution_preserves_data_and_unrelated_integrations(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    paths.home.mkdir(parents=True)
    paths.config_file.write_text("[model]\nprovider = 'mock'\n", encoding="utf-8")

    exported = paths.codex_user_skills / "managed-example"
    exported.mkdir(parents=True)
    (exported / "SKILL.md").write_text("managed", encoding="utf-8")
    (paths.home / "skills_install.json").write_text(
        json.dumps(
            {
                "owned": [
                    {
                        "name": "managed-example",
                        "target": "codex",
                        "dest": str(exported),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    codex_config = paths.codex_user_skills.parent / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)
    with codex_config.open("wb") as handle:
        tomli_w.dump(
            {
                "mcp_servers": {
                    "omniscientist": {"command": "omni", "args": ["mcp", "serve"]},
                    "keep-me": {"command": "other"},
                }
            },
            handle,
        )
    claude_config = Path.home() / ".claude.json"
    claude_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "omniscientist": {"command": "omni"},
                    "keep-me": {"command": "other"},
                },
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(uninstall, "scan_running_serve_pids", lambda **_kwargs: [])

    plan = uninstall.build_uninstall_plan(
        paths,
        purge=False,
        all_project_data=False,
        all_installations=False,
        remove_program=False,
        remove_untracked_exports=False,
    )
    report = uninstall.execute_uninstall_plan(paths, plan)

    assert report.errors == []
    assert paths.config_file.exists()
    assert not exported.exists()
    codex_payload = tomllib.loads(codex_config.read_text(encoding="utf-8"))
    assert set(codex_payload["mcp_servers"]) == {"keep-me"}
    claude_payload = json.loads(claude_config.read_text(encoding="utf-8"))
    assert set(claude_payload["mcpServers"]) == {"keep-me"}
    assert claude_payload["theme"] == "dark"


def test_purge_removes_home_and_registered_in_place_projects(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    paths.home.mkdir(parents=True)
    paths.config_file.write_text("[model]\nprovider = 'mock'\n", encoding="utf-8")
    in_place = tmp_path / "research-project" / ".omni"
    in_place.mkdir(parents=True)
    db = in_place / "sessions.sqlite3"
    db.write_text("local research data", encoding="utf-8")
    (paths.home / "workspaces.json").write_text(
        json.dumps(
            {
                str(in_place): {
                    "name": "research-project",
                    "project_dir": str(in_place),
                    "db": str(db),
                    "kind": "in-place",
                    "last_seen": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(uninstall, "scan_running_serve_pids", lambda **_kwargs: [])
    from omni.channels import credentials

    monkeypatch.setattr(credentials, "purge_known_channel_secrets", lambda: [])

    plan = uninstall.build_uninstall_plan(
        paths,
        purge=True,
        all_project_data=True,
        all_installations=False,
        remove_program=False,
        remove_untracked_exports=False,
    )
    report = uninstall.execute_uninstall_plan(paths, plan)

    assert report.errors == []
    assert not paths.home.exists()
    assert not in_place.exists()


def test_everything_only_removes_untracked_exports_that_still_match(tmp_path):
    paths = _paths(tmp_path)
    source = next(iter(skill_dirs_in(BUILTIN_SKILLS_DIR)))
    target = paths.codex_user_skills / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)

    conservative = uninstall.build_uninstall_plan(
        paths,
        purge=False,
        all_project_data=False,
        all_installations=False,
        remove_program=False,
        remove_untracked_exports=False,
    )
    assert target not in conservative.untracked_export_targets
    assert any("identical to built-ins" in warning for warning in conservative.warnings)

    complete = uninstall.build_uninstall_plan(
        paths,
        purge=False,
        all_project_data=False,
        all_installations=False,
        remove_program=False,
        remove_untracked_exports=True,
    )
    assert target in complete.untracked_export_targets

    (target / "user-note.txt").write_text("keep my edits", encoding="utf-8")
    changed = uninstall.build_uninstall_plan(
        paths,
        purge=False,
        all_project_data=False,
        all_installations=False,
        remove_program=False,
        remove_untracked_exports=True,
    )
    assert target not in changed.untracked_export_targets
    assert any("different content" in warning for warning in changed.warnings)


def test_corrupt_skill_export_manifest_cannot_delete_arbitrary_directory(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    paths.home.mkdir(parents=True)
    victim = tmp_path / "important-user-directory"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    (paths.home / "skills_install.json").write_text(
        json.dumps(
            {
                "owned": [
                    {
                        "name": victim.name,
                        "target": "codex",
                        "dest": str(victim),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(uninstall, "scan_running_serve_pids", lambda **_kwargs: [])

    plan = uninstall.build_uninstall_plan(
        paths,
        purge=False,
        all_project_data=False,
        all_installations=False,
        remove_program=False,
        remove_untracked_exports=False,
    )
    report = uninstall.execute_uninstall_plan(paths, plan)

    assert plan.tracked_export_targets == []
    assert any("unsafe paths" in warning for warning in plan.warnings)
    assert report.errors == []
    assert (victim / "keep.txt").is_file()


def test_uninstall_does_not_kill_unverified_runtime_or_update_holder(
    tmp_path, monkeypatch
):
    from omni.runtime import service_state

    paths = _paths(tmp_path)
    monkeypatch.setattr(uninstall, "_daemon_pidfiles", lambda *_args: [])
    monkeypatch.setattr(
        uninstall, "scan_running_serve_pids", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        service_state,
        "service_runtime_info",
        lambda _paths: {
            "pid": 4242,
            "service_id": service_state.service_instance_id(paths),
        },
    )
    monkeypatch.setattr(
        service_state,
        "singleton_holder_info",
        lambda _paths: {"pid": 4343, "role": "update"},
        raising=False,
    )
    stopped: list[int] = []
    monkeypatch.setattr(
        uninstall,
        "_stop_pid",
        lambda pid: (stopped.append(pid) or True),
    )

    count, errors = uninstall._stop_all_daemons(paths, [])

    assert count == 0
    assert errors == []
    assert stopped == []


def test_installation_manifest_records_reversible_owner(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(uninstall, "_current_entrypoint", lambda: "/tmp/bin/omni")

    manifest = uninstall.record_installation(
        paths,
        method="uv",
        source="omniscientist @ git+https://user:secret@example.test/repo?token=abc",
        editable=False,
    )

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["installations"][0]["method"] == "uv"
    source = payload["installations"][0]["source"]
    assert source == "omniscientist @ git+https://***@example.test/repo?token=***"
    assert "secret" not in source and "abc" not in source


def test_installation_manifest_prunes_missing_owner_records(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    paths.home.mkdir(parents=True)
    stale = tmp_path / "removed-conda" / "bin" / "omni"
    uninstall.install_manifest_path(paths).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "installations": [
                    {
                        "method": "env",
                        "executable": str(stale),
                        "python": str(stale.parent / "python"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    current = tmp_path / "uv-bin" / "omni"
    current.parent.mkdir(parents=True)
    current.write_text("launcher", encoding="utf-8")
    monkeypatch.setattr(uninstall, "_current_entrypoint", lambda: str(current))

    uninstall.record_installation(paths, method="uv", source="omniscientist")

    payload = json.loads(uninstall.install_manifest_path(paths).read_text(encoding="utf-8"))
    assert [row["executable"] for row in payload["installations"]] == [str(current)]


def test_hidden_record_install_hook_is_a_real_command_not_a_chat_prompt():
    result = runner.invoke(
        app,
        ["_record-install", "--method", "uv", "--source", "omniscientist"],
    )

    assert result.exit_code == 0, result.output
    assert "Recorded Omni installation ownership" in result.stdout
    assert uninstall.install_manifest_path(get_paths()).is_file()


def test_program_removal_is_deferred_until_the_cli_process_exits(monkeypatch):
    installation = uninstall.InstallationRecord(
        method="uv",
        executable="/tmp/uv-tools/omniscientist/bin/omni",
        python="/tmp/uv-tools/omniscientist/bin/python",
        current=True,
    )
    report = uninstall.UninstallReport()
    scheduled: list[list[list[str]]] = []

    def fake_defer(commands, _operation_dir):  # noqa: ANN001, ANN202
        scheduled.append(commands)
        return True

    def fail_if_run(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("the running Omni environment must not be removed synchronously")

    monkeypatch.setattr(uninstall, "_defer_program_removal", fake_defer)
    monkeypatch.setattr(uninstall.subprocess, "run", fail_if_run)

    uninstall._remove_programs([installation], report)

    assert scheduled == [[['uv', 'tool', 'uninstall', 'omniscientist']]]
    assert report.program_removal_deferred is True
    assert report.errors == []
    assert report.completed == ["scheduled program removal after this process exits"]


def test_posix_deferred_removal_publishes_an_install_operation_marker(tmp_path, monkeypatch):
    launched: list[list[str]] = []

    class FakeProcess:
        pass

    def fake_popen(command, **_kwargs):  # noqa: ANN001, ANN202
        launched.append(command)
        return FakeProcess()

    monkeypatch.setattr(uninstall.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(uninstall.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(uninstall.shutil, "which", lambda _name: "/bin/sh")
    operation_dir = tmp_path / "install-state"

    scheduled = uninstall._defer_posix_program_removal(
        [["uv", "tool", "uninstall", "omniscientist"]],
        operation_dir,
    )

    assert scheduled is True
    pending = operation_dir / "uninstall.pending"
    assert pending.is_file()
    payload = json.loads(pending.read_text(encoding="utf-8"))
    assert payload["status"] == "pending"
    helper = Path(launched[0][1])
    helper_text = helper.read_text(encoding="utf-8")
    assert str(pending) in helper_text
    assert str(operation_dir / "uninstall.failed") in helper_text


def test_default_plan_exposes_other_installations_without_removing_them(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    current = uninstall.InstallationRecord(
        method="uv",
        executable="/home/user/.local/bin/omni",
        python="/home/user/.local/share/uv/tools/omniscientist/bin/python",
        current=True,
    )
    other = uninstall.InstallationRecord(
        method="env",
        executable="/opt/conda/bin/omni",
        python="/opt/conda/bin/python",
    )

    def fake_detect(_paths, *, all_installations):  # noqa: ANN001, ANN202
        return [other, current] if all_installations else [current]

    monkeypatch.setattr(uninstall, "detect_installations", fake_detect)
    monkeypatch.setattr(uninstall, "_export_inventory", lambda _paths: ([], [], [], []))
    monkeypatch.setattr(integrations, "mcp_registration_status", lambda: {})

    plan = uninstall.build_uninstall_plan(
        paths,
        purge=False,
        all_project_data=False,
        all_installations=False,
        remove_program=True,
        remove_untracked_exports=False,
    )

    assert plan.installations == [current]
    assert plan.preserved_installations == [other]
    assert any(
        action.action == "preserve other install" and action.target == other.executable
        for action in plan.actions
    )
    assert any("--all-installations" in warning for warning in plan.warnings)


def test_mcp_unregister_helpers_preserve_other_servers():
    integrations.register_with_codex()
    integrations.register_with_codex("other")
    integrations.register_with_claude()
    integrations.register_with_claude("other")

    _, codex_changed = integrations.unregister_with_codex()
    _, claude_changed = integrations.unregister_with_claude()

    assert codex_changed is True
    assert claude_changed is True
    assert integrations.mcp_registration_status("omniscientist") == {
        "codex": False,
        "claude": False,
    }
    assert integrations.mcp_registration_status("other") == {
        "codex": True,
        "claude": True,
    }


def test_keychain_purge_targets_only_known_omni_accounts(monkeypatch):
    from omni.channels import credentials

    calls: list[list[str]] = []
    monkeypatch.setattr(credentials, "_has_macos_keychain", lambda: True)

    def fake_run(command, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(credentials.subprocess, "run", fake_run)
    removed = credentials.purge_known_channel_secrets()

    assert set(removed) == {
        "channel:feishu:app_secret",
        "channel:dingtalk:client_secret",
        "channel:wechat:bot_token",
    }
    assert all(call[:4] == ["security", "delete-generic-password", "-s", "omniscientist"] for call in calls)
    assert {call[-1] for call in calls} == set(removed)
