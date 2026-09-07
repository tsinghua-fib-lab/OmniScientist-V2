"""Owner model/VLM reload: write-through, last-good, same-generation pairing."""

from __future__ import annotations

from omni.config.live_settings import (
    OWNER_CONNECTION_RELOAD_NOTICE,
    connection_token,
    format_vlm_identity,
    is_owner_connection_key,
    owner_source_stamp,
    public_connection_identity,
    request_settings_reload,
    settings_reload_path,
    try_load_owner_settings,
)
from omni.config.paths import get_paths
from omni.config.settings import load_settings
from omni.config.user_edits import apply_vlm_config


def test_connection_keys_are_model_and_vlm_only() -> None:
    assert is_owner_connection_key("vlm.endpoint")
    assert is_owner_connection_key("model.api_key")
    assert not is_owner_connection_key("research.semantic_scholar_api_key")
    assert not is_owner_connection_key("display.verbosity")


def test_apply_vlm_touches_reload_sentinel_and_pairs_endpoint_with_key() -> None:
    paths = get_paths()
    apply_vlm_config(
        paths,
        endpoint="https://vision.example/v1/chat/completions",
        model="gpt-image-2",
        api_key="vlm-secret",
    )
    assert settings_reload_path(paths.home).is_file()
    settings = load_settings()
    token = connection_token(settings)
    assert "gpt-image-2" in token
    assert "https://vision.example/v1/chat/completions" in token
    assert "vlm-secret" in token
    identity = public_connection_identity(settings)
    assert identity["vlm_enabled"] is True
    assert identity["vlm_model"] == "gpt-image-2"
    assert "vlm-secret" not in str(identity)
    assert "next turn" in OWNER_CONNECTION_RELOAD_NOTICE


def test_corrupt_owner_file_returns_none_instead_of_defaults() -> None:
    paths = get_paths()
    apply_vlm_config(
        paths,
        endpoint="https://vision.example/v1/chat/completions",
        model="vision-model",
        api_key="vlm-secret",
    )
    paths.config_file.write_text("{not toml", encoding="utf-8")
    assert try_load_owner_settings(paths) is None


def test_source_stamp_changes_when_sentinel_is_touched() -> None:
    paths = get_paths()
    before = owner_source_stamp(paths)
    request_settings_reload(paths.home)
    after = owner_source_stamp(paths)
    assert after != before


def test_format_vlm_identity_is_non_secret() -> None:
    assert format_vlm_identity(False, "gpt-image-2", "https://example") == "disabled"
    assert format_vlm_identity(True, "gpt-image-2", "https://example") == (
        "gpt-image-2 @ https://example"
    )
