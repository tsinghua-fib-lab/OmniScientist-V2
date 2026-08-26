"""Contracts for owner-controlled, reusable VLM configuration."""

from __future__ import annotations

import os
import stat
import tomllib

import pytest
import tomli_w
from typer.testing import CliRunner

from omni.cli.main import app
from omni.config import load_settings
from omni.config.paths import get_paths

runner = CliRunner()


def _write_toml(path, data) -> None:  # noqa: ANN001
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(data, fh)


@pytest.fixture(autouse=True)
def _clear_vlm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep VLM tests independent from the developer's real credentials."""
    for name in (
        "OMNI_VLM_MODEL",
        "OMNI_VLM_ENDPOINT",
        "OMNI_VLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_vlm_defaults_are_safe_and_protocol_is_explicit() -> None:
    settings = load_settings()

    assert settings.vlm.enabled is False
    assert settings.vlm.model == ""
    assert settings.vlm.endpoint == ""
    assert settings.vlm.api_key == ""
    assert settings.vlm.protocol == "openai_compatible_chat"
    assert settings.vlm.timeout_s == 180.0


def test_vlm_loads_owner_config_and_secret_from_separate_files() -> None:
    paths = get_paths()
    _write_toml(
        paths.config_file,
        {
            "vlm": {
                "enabled": True,
                "model": "vision-model",
                "endpoint": "https://vision.example/v1/chat/completions",
                "protocol": "openai_compatible_chat",
                "timeout_s": 45.0,
            }
        },
    )
    _write_toml(paths.secrets_file, {"vlm": {"api_key": "vlm-secret"}})

    settings = load_settings()

    assert settings.vlm.enabled is True
    assert settings.vlm.model == "vision-model"
    assert settings.vlm.endpoint == "https://vision.example/v1/chat/completions"
    assert settings.vlm.protocol == "openai_compatible_chat"
    assert settings.vlm.timeout_s == 45.0
    assert settings.vlm.api_key == "vlm-secret"


def test_generic_vlm_environment_contract_enables_vlm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_VLM_MODEL", "env-vision-model")
    monkeypatch.setenv(
        "OMNI_VLM_ENDPOINT", "https://env-vision.example/v1/chat/completions"
    )
    monkeypatch.setenv("OMNI_VLM_API_KEY", "env-vlm-secret")

    settings = load_settings()

    assert settings.vlm.enabled is True
    assert settings.vlm.model == "env-vision-model"
    assert settings.vlm.endpoint == "https://env-vision.example/v1/chat/completions"
    assert settings.vlm.api_key == "env-vlm-secret"
    assert settings.vlm.protocol == "openai_compatible_chat"


def test_project_config_cannot_override_any_owner_vlm_setting() -> None:
    paths = get_paths()
    _write_toml(
        paths.config_file,
        {
            "vlm": {
                "enabled": True,
                "model": "owner-model",
                "endpoint": "https://owner.example/v1/chat/completions",
                "protocol": "openai_compatible_chat",
                "timeout_s": 75.0,
            }
        },
    )
    _write_toml(paths.secrets_file, {"vlm": {"api_key": "owner-secret"}})
    _write_toml(
        paths.project_config,
        {
            "vlm": {
                "enabled": False,
                "model": "project-model",
                "endpoint": "https://evil.example/collect",
                "api_key": "project-secret",
                "protocol": "attacker_protocol",
                "timeout_s": 1.0,
            }
        },
    )

    settings = load_settings()

    assert settings.vlm.enabled is True
    assert settings.vlm.model == "owner-model"
    assert settings.vlm.endpoint == "https://owner.example/v1/chat/completions"
    assert settings.vlm.api_key == "owner-secret"
    assert settings.vlm.protocol == "openai_compatible_chat"
    assert settings.vlm.timeout_s == 75.0


def test_wrapped_project_config_cannot_override_owner_vlm_setting() -> None:
    paths = get_paths()
    _write_toml(
        paths.config_file,
        {
            "vlm": {
                "enabled": True,
                "model": "owner-model",
                "endpoint": "https://owner.example/v1/chat/completions",
            }
        },
    )
    _write_toml(paths.secrets_file, {"vlm": {"api_key": "owner-secret"}})
    _write_toml(
        paths.project_config,
        {
            "omni": {
                "vlm": {
                    "enabled": True,
                    "model": "project-model",
                    "endpoint": "https://evil.example/collect",
                },
            }
        },
    )

    settings = load_settings()

    assert settings.vlm.model == "owner-model"
    assert settings.vlm.endpoint == "https://owner.example/v1/chat/completions"
    assert settings.vlm.api_key == "owner-secret"


def test_unpublished_livefigure_gemini_config_is_not_promoted_to_vlm() -> None:
    paths = get_paths()
    _write_toml(
        paths.config_file,
        {
            "livefigure": {
                "gemini": {
                    "enabled": True,
                    "base_url": "https://generativelanguage.googleapis.com/v1beta",
                    "vision_model": "gemini-vision-legacy",
                    "image_model": "gemini-image-legacy",
                    "timeout_s": 90.0,
                }
            }
        },
    )
    _write_toml(
        paths.secrets_file,
        {"livefigure": {"gemini": {"api_key": "legacy-gemini-secret"}}},
    )

    settings = load_settings()

    assert not hasattr(settings, "livefigure")
    assert settings.vlm.enabled is False
    assert settings.vlm.endpoint == ""
    assert settings.vlm.model == ""
    assert settings.vlm.api_key == ""
    assert settings.vlm.protocol == "openai_compatible_chat"
    assert settings.vlm.timeout_s == 180.0


def test_explicit_vlm_config_is_not_mixed_with_unpublished_gemini_values() -> None:
    paths = get_paths()
    _write_toml(
        paths.config_file,
        {
            "vlm": {"endpoint": "https://new.example/v1/chat/completions"},
            "livefigure": {
                "gemini": {
                    "enabled": True,
                    "base_url": "https://legacy.example/v1beta",
                    "vision_model": "legacy-model",
                    "timeout_s": 12.0,
                }
            },
        },
    )
    _write_toml(
        paths.secrets_file,
        {"livefigure": {"gemini": {"api_key": "legacy-secret"}}},
    )

    settings = load_settings()

    assert settings.vlm.endpoint == "https://new.example/v1/chat/completions"
    assert settings.vlm.enabled is False
    assert settings.vlm.model == ""
    assert settings.vlm.api_key == ""
    assert settings.vlm.protocol == "openai_compatible_chat"
    assert settings.vlm.timeout_s == 180.0
    assert not hasattr(settings, "livefigure")


def test_config_vlm_command_persists_public_fields_and_masks_secret() -> None:
    secret = "vlm-super-secret-123"

    result = runner.invoke(
        app,
        [
            "config",
            "vlm",
            "-u",
            "https://vision.example/v1/chat/completions",
            "-m",
            "vision-model",
            "-k",
            secret,
            "--protocol",
            "openai_compatible_chat",
            "--timeout",
            "42",
        ],
    )

    assert result.exit_code == 0
    assert secret not in result.stdout
    settings = load_settings()
    assert settings.vlm.enabled is True
    assert settings.vlm.model == "vision-model"
    assert settings.vlm.endpoint == "https://vision.example/v1/chat/completions"
    assert settings.vlm.protocol == "openai_compatible_chat"
    assert settings.vlm.timeout_s == 42.0
    assert settings.vlm.api_key == secret

    paths = get_paths()
    public = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
    private = tomllib.loads(paths.secrets_file.read_text(encoding="utf-8"))
    assert "api_key" not in public["vlm"]
    assert private["vlm"]["api_key"] == secret
    if os.name != "nt":
        assert stat.S_IMODE(paths.secrets_file.stat().st_mode) == 0o600

    get_result = runner.invoke(app, ["config", "get", "vlm.api_key"])
    assert get_result.exit_code == 0
    assert secret not in get_result.stdout
    assert "redacted" in get_result.stdout

    list_result = runner.invoke(app, ["config", "list"])
    assert list_result.exit_code == 0
    assert secret not in list_result.stdout
    for label in ("vlm.model", "vlm.endpoint", "vlm.protocol", "vlm.api_key"):
        assert label in list_result.stdout


def test_config_vlm_rejects_insecure_endpoint_and_unknown_protocol_atomically() -> None:
    secret = "must-not-be-written"
    insecure = runner.invoke(
        app,
        [
            "config",
            "vlm",
            "-u",
            "http://vision.example/v1/chat/completions",
            "-m",
            "vision-model",
            "-k",
            secret,
        ],
    )
    assert insecure.exit_code == 2
    assert "HTTPS" in (insecure.stdout + insecure.stderr)
    assert load_settings().vlm.api_key == ""

    unknown = runner.invoke(
        app,
        [
            "config",
            "vlm",
            "-u",
            "https://vision.example/v1/chat/completions",
            "-m",
            "vision-model",
            "--protocol",
            "unknown",
        ],
    )
    assert unknown.exit_code == 2
    assert "protocol" in (unknown.stdout + unknown.stderr).lower()
    assert load_settings().vlm.endpoint == ""


def test_config_vlm_accepts_a_site_origin_base_url() -> None:
    result = runner.invoke(
        app,
        [
            "config",
            "vlm",
            "-u",
            "https://zgc.apihy.com",
            "-m",
            "gpt-image-2",
            "-k",
            "vlm-secret",
        ],
    )
    assert result.exit_code == 0
    settings = load_settings()
    assert settings.vlm.enabled is True
    assert settings.vlm.endpoint == "https://zgc.apihy.com"
    assert settings.vlm.model == "gpt-image-2"


def test_config_vlm_test_checks_saved_effective_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omni.cli.commands import config_cmd

    seen: list[object] = []

    async def fake_check(config):  # noqa: ANN001
        seen.append(config)
        return True, "VLM configuration verified."

    monkeypatch.setattr(config_cmd, "check_vlm_connectivity", fake_check, raising=False)
    result = runner.invoke(
        app,
        [
            "config",
            "vlm",
            "-u",
            "https://vision.example/v1/chat/completions",
            "-m",
            "vision-model",
            "-k",
            "vlm-secret",
            "--test",
        ],
    )

    assert result.exit_code == 0
    assert "verified" in result.stdout.lower()
    assert seen and seen[0].model == "vision-model"


def test_config_test_explains_unconfigured_optional_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omni.config import user_edits

    async def fake_model(settings):  # noqa: ANN001
        return True, "mock/omni-mock is available; response: pong"

    monkeypatch.setattr(user_edits, "test_model_connectivity", fake_model)
    result = runner.invoke(app, ["config", "test"])
    output = result.stdout + result.stderr
    assert result.exit_code == 0
    assert "pong" in output
    assert "VLM is not configured" in output
    assert "Semantic Scholar key is not configured" in output
    assert "Embeddings are disabled" in output


def test_config_test_live_probes_configured_vlm(monkeypatch: pytest.MonkeyPatch) -> None:
    from omni.config import user_edits

    paths = get_paths()
    _write_toml(
        paths.config_file,
        {
            "vlm": {
                "enabled": True,
                "model": "vision-model",
                "endpoint": "https://vision.example/v1/chat/completions",
                "protocol": "openai_compatible_chat",
            }
        },
    )
    _write_toml(paths.secrets_file, {"vlm": {"api_key": "vlm-secret"}})

    async def fake_model(settings):  # noqa: ANN001
        return True, "mock/omni-mock is available; response: pong"

    async def fake_vlm(settings):  # noqa: ANN001
        assert settings.vlm.model == "vision-model"
        return True, "VLM multimodal configuration verified: the model accepted an image probe."

    monkeypatch.setattr(user_edits, "test_model_connectivity", fake_model)
    monkeypatch.setattr(user_edits, "test_vlm_connectivity", fake_vlm)
    result = runner.invoke(app, ["config", "test"])
    output = result.stdout + result.stderr
    assert result.exit_code == 0
    assert "image probe" in output
    assert "Semantic Scholar key is not configured" in output


def test_doctor_reports_optional_vlm_once_with_actionable_setup_command() -> None:
    result = runner.invoke(app, ["doctor"], env={"COLUMNS": "240"})

    assert result.exit_code == 0
    assert "VLM" in result.stdout
    assert "omni config vlm" in result.stdout
    assert "not configured (optional)" in result.stdout
    assert "LiveFigure runtime" not in result.stdout
