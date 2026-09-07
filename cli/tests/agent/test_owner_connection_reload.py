"""Long-running agents re-read owner model/VLM without rebuilding the tree."""

from __future__ import annotations

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.config.paths import get_paths
from omni.config.settings import VlmCfg
from omni.config.user_edits import apply_vlm_config
from omni.core.vlm import VlmGateway


def test_refresh_replaces_gateway_and_keeps_the_in_flight_object() -> None:
    agent = OmniAgent(load_settings())
    frozen = agent.vlm
    assert frozen.available is False
    apply_vlm_config(
        get_paths(),
        endpoint="https://vision.example/v1/chat/completions",
        model="gpt-image-2",
        api_key="vlm-secret",
    )
    assert agent.refresh_owner_connection() is True
    assert agent.vlm is not frozen
    assert agent.vlm.available is True
    assert agent.settings.vlm.model == "gpt-image-2"
    assert frozen.available is False
    assert frozen._config.model == ""  # noqa: SLF001 — in-flight snapshot
    services = agent.registry.admission_services()
    assert services["vlm"].available is True
    assert services["vlm"]._config.model == "gpt-image-2"  # noqa: SLF001


def test_in_memory_override_survives_when_owner_files_did_not_change() -> None:
    settings = load_settings()
    settings.vlm = VlmCfg(
        enabled=True,
        model="in-memory-vision",
        endpoint="https://memory.example/v1/chat/completions",
        api_key="memory-secret",
    )
    agent = OmniAgent(settings)
    agent.vlm = VlmGateway(settings.vlm)
    assert agent.refresh_owner_connection() is False
    assert agent.settings.vlm.model == "in-memory-vision"
    assert agent.vlm.available is True


def test_corrupt_toml_keeps_last_good_gateway() -> None:
    agent = OmniAgent(load_settings())
    apply_vlm_config(
        get_paths(),
        endpoint="https://vision.example/v1/chat/completions",
        model="vision-model",
        api_key="vlm-secret",
    )
    assert agent.refresh_owner_connection() is True
    good = agent.vlm
    get_paths().config_file.write_text("{not toml", encoding="utf-8")
    assert agent.refresh_owner_connection() is False
    assert agent.vlm is good
    assert agent.vlm.available is True


def test_second_refresh_is_a_no_op_until_files_change() -> None:
    agent = OmniAgent(load_settings())
    apply_vlm_config(
        get_paths(),
        endpoint="https://vision.example/v1/chat/completions",
        model="vision-model",
        api_key="vlm-secret",
    )
    assert agent.refresh_owner_connection() is True
    first = agent.vlm
    assert agent.refresh_owner_connection() is False
    assert agent.vlm is first
