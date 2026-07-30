"""Offline contract tests for the thin AutoSOTA launcher integration."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from omni.autosota import integration
from omni.autosota.integration import (
    ReleaseAsset,
    WorkspaceConfiguration,
    active_install,
    autosota_root,
    configure_workspace,
    install_release,
    materialized_workspace_secrets,
    metadata_path,
    prepare_native_paper_config,
    run_native,
)
from omni.cli.commands.autosota_cmd import _code_model_base_url
from omni.cli.main import app

runner = CliRunner()


def _paths(tmp_path: Path) -> SimpleNamespace:
    home = tmp_path / "omni-home"
    return SimpleNamespace(cache_dir=home / "cache", secrets_file=home / "secrets.toml")


def _fake_which(name: str) -> str | None:
    return f"/usr/bin/{name}" if name in {"node", "npm", "git", "bash"} else None


def _fake_active_install(paths: SimpleNamespace) -> Path:
    name = "autosota.cmd" if os.name == "nt" else "autosota"
    executable = autosota_root(paths) / "versions" / "v0" / "node_modules" / ".bin" / name
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "@exit /b 0\r\n" if os.name == "nt" else "#!/bin/sh\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        executable.chmod(0o755)
    metadata_path(paths).parent.mkdir(parents=True, exist_ok=True)
    metadata_path(paths).write_text(
        json.dumps({"version": "v0", "runtime_dir": str(executable.parents[2]), "executable": str(executable)}),
        encoding="utf-8",
    )
    return executable


def test_official_release_asset_requires_exactly_one_tgz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        integration,
        "_fetch_json",
        lambda _url: {
            "tag_name": "v0.3.0",
            "assets": [
                {"name": "notes.txt", "browser_download_url": "https://example.test/notes.txt"},
                {
                    "name": "autosota-0.3.0.tgz",
                    "browser_download_url": "https://example.test/autosota-0.3.0.tgz",
                    "digest": "sha256:" + "a" * 64,
                },
            ],
        },
    )

    asset = integration.official_release_asset("0.3.0")

    assert asset == ReleaseAsset("v0.3.0", "https://example.test/autosota-0.3.0.tgz", "a" * 64)


def test_official_release_asset_falls_back_when_github_api_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_rate_limit(_url: str) -> dict[str, object]:
        raise integration.AutosotaError("Could not query the official AutoSOTA release: HTTP Error 403")

    monkeypatch.setattr(integration, "_fetch_json", raise_rate_limit)
    monkeypatch.setattr(integration, "_latest_release_tag_from_page", lambda: "v0.3.0")

    asset = integration.official_release_asset()

    assert asset == ReleaseAsset(
        "v0.3.0",
        "https://github.com/tsinghua-fib-lab/AutoSOTA/releases/download/v0.3.0/autosota-0.3.0.tgz",
    )


def test_use_omni_model_translates_deepseek_to_its_anthropic_endpoint() -> None:
    assert _code_model_base_url("deepseek", "https://api.deepseek.com/v1") == "https://api.deepseek.com/anthropic"
    assert _code_model_base_url("openai", "https://api.openai.com/v1") == "https://api.openai.com/v1"


def test_install_is_isolated_verified_and_idempotent(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    calls: list[list[str]] = []

    def fake_download(_url: str, destination: Path) -> str:
        destination.write_bytes(b"fake-autosota-package")
        return integration._file_sha256(destination)

    def fake_run(argv, **_kwargs):  # noqa: ANN001, ANN202
        args = [str(arg) for arg in argv]
        calls.append(args)
        if args[0] == "/usr/bin/node":
            return SimpleNamespace(returncode=0, stdout="v20.20.2\n", stderr="")
        if args[0] == "/usr/bin/npm":
            prefix = Path(args[args.index("--prefix") + 1])
            name = "autosota.cmd" if os.name == "nt" else "autosota"
            executable = prefix / "node_modules" / ".bin" / name
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text(
                "@exit /b 0\r\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n",
                encoding="utf-8",
            )
            if os.name != "nt":
                executable.chmod(0o755)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert args[-1] == "--version"
        return SimpleNamespace(returncode=0, stdout="AutoSOTA 0.3.0\n", stderr="")

    result = install_release(
        paths,
        release_resolver=lambda _version: ReleaseAsset("v0.3.0", "https://example.test/autosota.tgz"),
        downloader=fake_download,
        run=fake_run,
        which=_fake_which,
    )

    assert result.changed is True
    assert result.executable.is_file()
    assert result.runtime_dir.is_relative_to(autosota_root(paths))
    assert active_install(paths) == result.__class__(result.version, result.runtime_dir, result.executable, False)
    payload = json.loads(metadata_path(paths).read_text(encoding="utf-8"))
    assert payload["version"] == "v0.3.0"
    assert payload["source_url"] == "https://example.test/autosota.tgz"
    assert calls[1][0] == "/usr/bin/npm"
    assert "--global" not in calls[1]

    calls_before = len(calls)
    unchanged = install_release(
        paths,
        release_resolver=lambda _version: ReleaseAsset("v0.3.0", "https://example.test/autosota.tgz"),
        downloader=fake_download,
        run=fake_run,
        which=_fake_which,
    )

    assert unchanged.changed is False
    assert len(calls) == calls_before + 1  # Node.js requirement check only; npm is not rerun.


def test_workspace_config_keeps_api_keys_outside_workspace(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = tmp_path / "workspace"
    repo = tmp_path / "target-repo"
    repo.mkdir()

    result = configure_workspace(
        paths,
        WorkspaceConfiguration(
            workspace=workspace,
            repo_path=repo,
            devices="0,1",
            primary_metric="accuracy",
            metric_direction="maximize",
            claude_base_url="https://models.example/v1",
            claude_model="code-model",
        ),
        secrets={"claude_api_key": "test-secret-value"},
    )

    workspace_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (result.profile_path, result.config_path)
    )
    assert "test-secret-value" not in workspace_text
    assert yaml.safe_load(result.config_path.read_text(encoding="utf-8"))["claude_model"] == "code-model"
    profile = tomllib.loads(result.profile_path.read_text(encoding="utf-8"))
    assert profile["launcher"]["repo_path"] == str(repo.resolve())
    secrets = tomllib.loads(paths.secrets_file.read_text(encoding="utf-8"))
    assert secrets["autosota"]["workspaces"]
    assert "test-secret-value" in paths.secrets_file.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(paths.secrets_file.stat().st_mode) == 0o600


def test_workspace_config_preserves_existing_native_yaml_without_force(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace.mkdir()
    native_config = workspace / "config.yaml"
    original = "# AutoSOTA-owned comment\nclaude_model: native-model\n"
    native_config.write_text(original, encoding="utf-8")

    result = configure_workspace(
        paths,
        WorkspaceConfiguration(workspace=workspace, repo_path=repo, claude_model="replacement-model"),
    )

    assert result.config_updated is False
    assert native_config.read_text(encoding="utf-8") == original


def test_prepare_native_paper_config_applies_protection_without_credentials(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    configure_workspace(
        paths,
        WorkspaceConfiguration(
            workspace=workspace,
            repo_path=repo,
            devices="0,1,2,3",
            eval_command="bash run_eval.sh",
            primary_metric="validation_accuracy",
            metric_direction="maximize",
            baseline="0.25",
            max_iterations=2,
            protected_paths=("run_eval.sh", "tests"),
            claude_model="model",
        ),
        secrets={"claude_api_key": "test-secret-value"},
    )

    config_path = prepare_native_paper_config(workspace, "gpu-demo")
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert payload["repo_path"] == str(repo.resolve())
    assert payload["gpu_devices"] == "0,1,2,3"
    assert payload["eval_command"] == "bash run_eval.sh"
    assert payload["eval_command_file"] == "run_eval.sh"
    assert payload["baseline_metrics"]["validation_accuracy"] == 0.25
    assert payload["metric_direction"] == "higher"
    assert payload["max_iterations"] == 2
    assert payload["protected_paths"] == ["run_eval.sh", "tests"]
    assert all(not payload[key] for key in integration._SECRET_KEYS)
    assert "test-secret-value" not in config_path.read_text(encoding="utf-8")


def test_run_refuses_secret_backed_native_onboarding_until_prepared(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    configure_workspace(
        paths,
        WorkspaceConfiguration(
            workspace=workspace,
            repo_path=repo,
            eval_command="bash run_eval.sh",
            primary_metric="accuracy",
            metric_direction="maximize",
            baseline="0.5",
            protected_paths=("run_eval.sh",),
            claude_model="model",
        ),
        secrets={"claude_api_key": "test-secret-value"},
    )
    _fake_active_install(paths)

    with pytest.raises(integration.AutosotaError, match="autosota prepare gpu-demo"):
        run_native(paths, workspace=workspace, args=["gpu-demo"], run=lambda *_args, **_kwargs: None)


def test_run_syncs_policy_and_scrubs_native_paper_secrets(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    configure_workspace(
        paths,
        WorkspaceConfiguration(
            workspace=workspace,
            repo_path=repo,
            devices="0,1",
            eval_command="bash run_eval.sh",
            primary_metric="accuracy",
            metric_direction="maximize",
            baseline="0.5",
            protected_paths=("run_eval.sh", "tests"),
            claude_model="model",
        ),
        secrets={"claude_api_key": "test-secret-value"},
    )
    config_path = prepare_native_paper_config(workspace, "gpu-demo")
    _fake_active_install(paths)

    def fake_run(_argv, **_kwargs):  # noqa: ANN001, ANN202
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert payload["protected_paths"] == ["run_eval.sh", "tests"]
        payload["openrouter_api_key"] = "native-persisted-secret"
        config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    assert run_native(
        paths,
        workspace=workspace,
        args=["gpu-demo", "--skip-onboard"],
        run=fake_run,
    ) == 0
    cleaned = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert cleaned["openrouter_api_key"] == ""
    assert "native-persisted-secret" not in config_path.read_text(encoding="utf-8")


def test_native_dry_run_does_not_materialize_keys(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    configure_workspace(
        paths,
        WorkspaceConfiguration(workspace=workspace, repo_path=repo, claude_model="model"),
        secrets={"claude_api_key": "test-secret-value"},
    )
    _fake_active_install(paths)

    def fake_run(_argv, **_kwargs):  # noqa: ANN001, ANN202
        assert "test-secret-value" not in (workspace / "config.yaml").read_text(encoding="utf-8")
        return SimpleNamespace(returncode=0)

    assert run_native(paths, workspace=workspace, args=["gpu-demo", "--dry-run"], run=fake_run) == 0


def test_materialized_secrets_are_restored_after_native_process(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    configure_workspace(
        paths,
        WorkspaceConfiguration(workspace=workspace, repo_path=repo, claude_model="model"),
        secrets={"claude_api_key": "test-secret-value"},
    )
    config_path = workspace / "config.yaml"
    original = config_path.read_bytes()
    if os.name != "nt":
        config_path.chmod(0o640)

    executable = _fake_active_install(paths)
    observed: dict[str, str] = {}

    def fake_run(_argv, **kwargs):  # noqa: ANN001, ANN202
        observed.update(yaml.safe_load(config_path.read_text(encoding="utf-8")))
        assert kwargs["env"]["PATH"].split(os.pathsep)[0] == str(executable.parent)
        return SimpleNamespace(returncode=0)

    assert run_native(paths, workspace=workspace, args=["doctor"], run=fake_run) == 0
    assert observed["claude_api_key"] == "test-secret-value"
    assert config_path.read_bytes() == original
    if os.name != "nt":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o640


def test_cli_config_redacts_key_and_run_dry_does_not_start_autosota(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()

    configured = runner.invoke(
        app,
        [
            "autosota", "config", "--workspace", str(workspace), "--repo", str(repo),
            "--devices", "0", "--max-iterations", "3", "--max-total-hours", "1.5",
            "--claude-model", "code-model", "--claude-api-key", "test-secret-value",
        ],
    )

    assert configured.exit_code == 0, configured.output
    assert "test-secret-value" not in configured.output
    dry_run = runner.invoke(app, ["autosota", "run", "--workspace", str(workspace), "--dry-run"])
    assert dry_run.exit_code == 0, dry_run.output
    assert "autosota --repo" in dry_run.output
    assert "--max-iter 3" in dry_run.output
    assert "--max-total-minutes 90" in dry_run.output
    assert "No AutoSOTA process" in dry_run.output


def test_cli_real_run_requires_prepared_paper_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = runner.invoke(app, ["autosota", "run", "--workspace", str(workspace)])

    assert result.exit_code == 1
    assert "prepared paper name is required" in result.output


def test_secret_materialization_does_not_create_config_when_native_workspace_has_none(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    integration._save_workspace_secrets(paths, workspace, {"claude_api_key": "test-secret-value"})

    with materialized_workspace_secrets(paths, workspace):
        assert not (workspace / "config.yaml").exists()
