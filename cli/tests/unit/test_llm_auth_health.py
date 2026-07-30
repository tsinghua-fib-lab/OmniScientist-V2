from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from omni.core.llm.client import check_connectivity
from omni.core.llm.errors import LLMProviderError
from omni.core.llm.providers import OpenAICompatibleProvider


class _UnauthorizedAsyncClient:
    calls = 0

    def __init__(self, **_kwargs) -> None:  # noqa: ANN003
        pass

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args) -> None:  # noqa: ANN002
        return None

    async def post(self, url: str, **_kwargs) -> httpx.Response:  # noqa: ANN003
        type(self).calls += 1
        request = httpx.Request("POST", url)
        return httpx.Response(
            401,
            request=request,
            json={"error": {"message": "invalid API key; status=401"}},
        )


@pytest.mark.asyncio
async def test_provider_caches_terminal_auth_failure_for_its_config_lifetime(monkeypatch):
    from omni.core.llm import providers

    _UnauthorizedAsyncClient.calls = 0
    monkeypatch.setattr(providers.httpx, "AsyncClient", _UnauthorizedAsyncClient)
    provider = OpenAICompatibleProvider(
        base_url="https://models.invalid/v1",
        api_key="invalid",
        model="test-model",
    )

    with pytest.raises(LLMProviderError):
        await provider.chat("system", "first")
    with pytest.raises(LLMProviderError):
        await provider.chat("system", "second")

    assert _UnauthorizedAsyncClient.calls == 1


@pytest.mark.asyncio
async def test_connectivity_check_returns_normalized_authentication_message(monkeypatch):
    from omni.core.llm import client as client_module

    class _UnauthorizedClient:
        async def chat(self, *_args, **_kwargs) -> str:  # noqa: ANN002, ANN003
            request = httpx.Request("POST", "https://models.invalid/v1/chat/completions")
            response = httpx.Response(
                401,
                request=request,
                json={"error": {"message": "invalid API key; status=401"}},
            )
            exc = httpx.HTTPStatusError("unauthorized", request=request, response=response)
            from omni.core.llm.errors import from_http_status_error

            raise from_http_status_error(exc)

    monkeypatch.setattr(client_module, "create_llm_client", lambda _settings: _UnauthorizedClient())
    settings = SimpleNamespace(
        model=SimpleNamespace(
            provider="openai",
            base_url="https://models.invalid/v1",
            model="test-model",
        )
    )

    ok, detail = await check_connectivity(settings)

    assert ok is False
    assert detail == (
        "Model authentication failed. Check the configured provider API key "
        "and access permissions."
    )
    assert "401" not in detail
    assert "LLMProviderError" not in detail
