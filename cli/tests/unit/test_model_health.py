from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from omni.cli.main import app

runner = CliRunner()


def _model(*, api_key: str = "sk-first-secret") -> SimpleNamespace:
    return SimpleNamespace(
        provider="openai",
        base_url="https://models.example/v1",
        model="test-model",
        api_key=api_key,
    )


def test_model_health_is_bound_to_config_fingerprint_without_persisting_secret(tmp_path):
    from omni.core.llm.health import load_model_health, record_model_health

    paths = SimpleNamespace(cache_dir=tmp_path / "cache")
    model = _model()

    initial = load_model_health(paths, model)
    assert initial.status == "unverified"

    record_model_health(paths, model, status="verified", message="model is available")
    verified = load_model_health(paths, model)
    assert verified.status == "verified"
    assert verified.message == "model is available"

    persisted = (paths.cache_dir / "model-health.json").read_text(encoding="utf-8")
    assert model.api_key not in persisted

    changed = load_model_health(paths, _model(api_key="sk-second-secret"))
    assert changed.status == "unverified"
    assert changed.message == "Model configuration changed and has not been tested."


def test_failed_model_health_keeps_only_safe_user_message(tmp_path):
    from omni.core.llm.health import load_model_health, record_model_health

    paths = SimpleNamespace(cache_dir=tmp_path / "cache")
    model = _model()
    record_model_health(
        paths,
        model,
        status="failed",
        message=(
            "Model authentication failed. Check the configured provider API key "
            "and access permissions."
        ),
    )

    failed = load_model_health(paths, model)
    assert failed.status == "failed"
    assert "401" not in failed.message
    assert model.api_key not in failed.message


def test_config_model_marks_new_configuration_unverified():
    from omni.config import load_settings
    from omni.core.llm.health import load_model_health

    result = runner.invoke(
        app,
        [
            "config",
            "model",
            "-p",
            "openai",
            "-u",
            "https://models.example/v1",
            "-m",
            "test-model",
            "-k",
            "sk-new-secret",
        ],
    )

    assert result.exit_code == 0
    settings = load_settings()
    assert load_model_health(settings.paths, settings.model).status == "unverified"


def test_config_test_persists_normalized_authentication_failure(monkeypatch):
    from omni.config import load_settings
    from omni.core.llm import client as llm_client
    from omni.core.llm.health import load_model_health

    configured = runner.invoke(
        app,
        [
            "config",
            "model",
            "-p",
            "openai",
            "-u",
            "https://models.example/v1",
            "-m",
            "test-model",
            "-k",
            "sk-invalid-secret",
        ],
    )
    assert configured.exit_code == 0

    async def reject_authentication(_settings):  # noqa: ANN001
        return False, "Model authentication failed. Check the configured provider API key."

    monkeypatch.setattr(llm_client, "check_connectivity", reject_authentication)
    result = runner.invoke(app, ["config", "test"])

    assert result.exit_code == 0
    output = result.stdout + result.stderr
    assert "Model authentication failed" in output
    assert "sk-invalid-secret" not in output
    settings = load_settings()
    health = load_model_health(settings.paths, settings.model)
    assert health.status == "failed"
    assert "sk-invalid-secret" not in health.message
