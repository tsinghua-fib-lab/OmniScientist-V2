"""User-layer edits are the shared write path for CLI and web."""

from __future__ import annotations

import tomllib

from omni.config.paths import get_paths
from omni.config.settings import load_settings
from omni.config.user_edits import apply_config_value, apply_model_config, setup_required


def test_apply_config_value_writes_user_toml() -> None:
    paths = get_paths()
    apply_config_value(paths, "react.max_iterations", "11")
    raw = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
    assert raw["react"]["max_iterations"] == 11
    assert load_settings().react.max_iterations == 11


def test_apply_model_auto_promotes_mock_when_url_supplied() -> None:
    paths = get_paths()
    changed = apply_model_config(
        paths,
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        current_provider="mock",
    )
    assert any("openai_compatible" in item for item in changed)
    settings = load_settings()
    assert settings.model.provider == "openai_compatible"
    assert settings.model.base_url == "https://api.deepseek.com/v1"


def test_setup_required_clears_after_user_file_exists() -> None:
    assert setup_required(load_settings()) is True
    apply_model_config(get_paths(), provider="mock", model="omni-mock")
    assert setup_required(load_settings()) is False
