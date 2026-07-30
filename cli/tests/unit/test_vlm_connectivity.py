"""Offline contracts for VLM endpoint validation and connectivity checks."""

from __future__ import annotations

import json

import httpx
import pytest

from omni.config.settings import VlmCfg
from omni.core.vlm import VlmGateway, check_vlm_connectivity, validate_vlm_endpoint


def _config(**overrides: object) -> VlmCfg:
    values: dict[str, object] = {
        "enabled": True,
        "model": "vision-test-model",
        "endpoint": "https://vision.example/v1/chat/completions",
        "api_key": "vlm-secret-value",
        "protocol": "openai_compatible_chat",
        "timeout_s": 5.0,
    }
    values.update(overrides)
    return VlmCfg(**values)


def test_endpoint_policy_requires_https_except_for_loopback() -> None:
    assert validate_vlm_endpoint("https://vision.example/v1/chat/completions") is None
    assert validate_vlm_endpoint("http://localhost:11434/v1/chat/completions") is None
    assert validate_vlm_endpoint("http://127.0.0.1:8080/v1/chat/completions") is None
    assert validate_vlm_endpoint("http://[::1]:8080/v1/chat/completions") is None

    with pytest.raises(ValueError, match="HTTPS"):
        validate_vlm_endpoint("http://vision.example/v1/chat/completions")
    with pytest.raises(ValueError, match="complete"):
        validate_vlm_endpoint("vision.example/v1/chat/completions")


@pytest.mark.asyncio
async def test_vlm_gateway_exposes_generation_without_raw_owner_config() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "code"}}]})

    service = VlmGateway(_config(), transport=httpx.MockTransport(handler))

    assert not hasattr(service, "config")
    assert await service.generate_text("make a figure") == "code"
    assert requests[0].headers["authorization"] == "Bearer vlm-secret-value"
    assert "vlm-secret-value" not in repr(service)


@pytest.mark.asyncio
async def test_connectivity_probe_uses_redacted_openai_compatible_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    ok, detail = await check_vlm_connectivity(
        _config(), transport=httpx.MockTransport(handler)
    )

    assert ok is True
    assert "verified" in detail.lower()
    assert "vlm-secret-value" not in detail
    assert len(requests) == 1
    request = requests[0]
    assert request.headers["authorization"] == "Bearer vlm-secret-value"
    payload = json.loads(request.content)
    assert payload["model"] == "vision-test-model"
    assert payload["messages"][0]["content"][0]["type"] == "text"


@pytest.mark.asyncio
async def test_connectivity_probe_omits_provider_body_and_secret_on_auth_failure() -> None:
    secret = "provider-echoed-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"credential {secret} rejected")

    ok, detail = await check_vlm_connectivity(
        _config(api_key=secret), transport=httpx.MockTransport(handler)
    )

    assert ok is False
    assert "401" in detail
    assert secret not in detail
    assert "credential" not in detail


@pytest.mark.asyncio
async def test_connectivity_probe_rejects_incomplete_or_unsupported_configuration() -> None:
    ok, detail = await check_vlm_connectivity(_config(model=""))
    assert ok is False
    assert "model" in detail.lower()

    ok, detail = await check_vlm_connectivity(_config(protocol="unknown"))
    assert ok is False
    assert "protocol" in detail.lower()
