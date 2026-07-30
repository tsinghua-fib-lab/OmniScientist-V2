"""Named-model shortcuts and isolated-home seeds must not change live layers."""

from __future__ import annotations

import os
from pathlib import Path

import tomli_w

from omni.config import resolve_settings
from omni.config.model_discovery import (
    discover_host_seed,
    discover_init_seed,
    infer_preset_for_model_name,
    is_complete_main_model,
    process_environment_seed,
    resolve_named_main_model,
)
from omni.config.model_stack import ModelRole, resolve_model_stack
from omni.config.paths import default_user_home


def _write_toml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        tomli_w.dump(data, stream)


def test_named_model_on_mock_applies_the_matching_preset() -> None:
    current = resolve_model_stack(resolve_settings()).for_role(ModelRole.MAIN)
    choice = resolve_named_main_model("deepseek-chat", current)

    assert choice is not None
    assert choice.provider == "deepseek"
    assert choice.model == "deepseek-chat"
    assert choice.keep_existing_endpoint is False
    assert "deepseek.com" in choice.base_url


def test_named_provider_key_applies_that_preset() -> None:
    current = resolve_model_stack(resolve_settings()).for_role(ModelRole.MAIN)
    choice = resolve_named_main_model("ollama", current)

    assert choice is not None
    assert choice.provider == "ollama"
    assert choice.model == "llama3.1"
    assert choice.inferred_from == "preset-key"


def test_named_model_keeps_the_current_endpoint_for_the_same_vendor() -> None:
    current = resolve_model_stack(
        resolve_settings(
            overrides={
                "model": {
                    "provider": "openai",
                    "base_url": "https://already-configured.example/v1",
                    "model": "gpt-4o-mini",
                }
            }
        )
    ).for_role(ModelRole.MAIN)

    choice = resolve_named_main_model("gpt-4.1", current)

    assert choice is not None
    assert choice.keep_existing_endpoint is True
    assert choice.model == "gpt-4.1"
    assert choice.provider == "openai"


def test_named_model_switches_preset_when_the_vendor_changes() -> None:
    current = resolve_model_stack(
        resolve_settings(
            overrides={
                "model": {
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini",
                }
            }
        )
    ).for_role(ModelRole.MAIN)

    choice = resolve_named_main_model("deepseek-chat", current)

    assert choice is not None
    assert choice.provider == "deepseek"
    assert choice.keep_existing_endpoint is False


def test_unknown_name_on_mock_is_refused() -> None:
    current = resolve_model_stack(resolve_settings()).for_role(ModelRole.MAIN)
    assert resolve_named_main_model("totally-custom-finetune", current) is None


def test_unknown_name_on_a_real_provider_only_changes_the_model_id() -> None:
    current = resolve_model_stack(
        resolve_settings(
            overrides={
                "model": {
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini",
                }
            }
        )
    ).for_role(ModelRole.MAIN)

    choice = resolve_named_main_model("my-finetune", current)

    assert choice is not None
    assert choice.model == "my-finetune"
    assert choice.keep_existing_endpoint is True


def test_llama_name_maps_to_the_ollama_preset() -> None:
    assert infer_preset_for_model_name("llama3.2").key == "ollama"


def test_complete_main_model_rejects_mock() -> None:
    assert is_complete_main_model("openai", "https://api.openai.com/v1", "gpt-4o")
    assert not is_complete_main_model("mock", "", "omni-mock")


def test_process_environment_seed_completes_a_known_provider(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNI_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OMNI_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "env-secret")

    seed = process_environment_seed(resolve_settings())

    assert seed is not None
    assert seed.provider == "openai"
    assert seed.model == "gpt-4o"
    assert seed.base_url == "https://api.openai.com/v1"
    assert seed.api_key == "env-secret"
    assert seed.origin == "process environment"


def test_host_home_is_discoverable_but_not_a_runtime_layer(
    tmp_path: Path, monkeypatch
) -> None:
    # Isolated HOME from conftest — never the machine's real ~/.omni.
    host = default_user_home()
    assert host.is_relative_to(Path.home())
    assert host.resolve() != Path(os.environ["OMNI_HOME"]).resolve()
    _write_toml(
        host / "config.toml",
        {
            "model": {
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "model": "host-deepseek",
            }
        },
    )
    _write_toml(host / "secrets.toml", {"model": {"api_key": "host-secret"}})

    live = resolve_settings()
    assert live.settings.model.model == "omni-mock"
    assert live.settings.model.provider == "mock"

    seed = discover_host_seed(live.settings.paths.home)
    assert seed is not None
    assert seed.model == "host-deepseek"
    assert seed.api_key == "host-secret"
    assert "host home" in seed.origin

    interactive = discover_init_seed(live, allow_host=True)
    noninteractive = discover_init_seed(live, allow_host=False)
    assert interactive is not None and interactive.model == "host-deepseek"
    assert noninteractive is None
