"""Embedding capability detection should degrade once, then stay quiet."""

from __future__ import annotations

import httpx
import pytest

from omni.core.llm import providers
from omni.core.llm.providers import OpenAICompatibleProvider


class _FakeEmbeddingClient:
    calls = 0

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:  # noqa: ANN002
        return None

    async def post(self, url: str, **kwargs):  # noqa: ANN003
        type(self).calls += 1
        request = httpx.Request("POST", url)
        response = httpx.Response(404, request=request)
        return response


@pytest.mark.asyncio
async def test_embedding_404_disables_future_embedding_calls(monkeypatch) -> None:
    _FakeEmbeddingClient.calls = 0
    monkeypatch.setattr(providers.httpx, "AsyncClient", _FakeEmbeddingClient)
    provider = OpenAICompatibleProvider(
        base_url="https://api.deepseek.com",
        api_key="sk-test",
        model="deepseek-v4-pro",
    )

    with pytest.raises(NotImplementedError):
        await provider.embed(["first"])
    with pytest.raises(NotImplementedError):
        await provider.embed(["second"])

    assert _FakeEmbeddingClient.calls == 1
