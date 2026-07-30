"""Public embedding adapter used by portable skill index builders."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omni.research import embedding_runtime as embedding_runtime_module
from omni.research.embedding_runtime import (
    ConfiguredEmbeddingRuntime,
    EmbeddingRuntimeError,
    configured_embedding_runtime,
    configured_embedding_space_id,
    embedding_space_id,
    specter2_embedding_space_id,
)


class _EmbeddingResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "data": [
                {"index": 1, "embedding": [2.0]},
                {"index": 0, "embedding": [1.0]},
            ]
        }


class _EmbeddingHTTPClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def post(self, url: str, **kwargs: Any) -> _EmbeddingResponse:
        self.calls.append({"url": url, **kwargs})
        return _EmbeddingResponse()

    async def aclose(self) -> None:
        return None


class _UnauthorizedResponse:
    status_code = 401

    def raise_for_status(self) -> None:
        request = httpx.Request("POST", "https://embedding.example/v1/embeddings")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unsafe provider detail", request=request, response=response)


class _UnauthorizedHTTPClient(_EmbeddingHTTPClient):
    async def post(self, _url: str, **_kwargs: Any) -> _UnauthorizedResponse:
        return _UnauthorizedResponse()


@pytest.mark.asyncio
async def test_runtime_uses_embedding_config_even_when_chat_is_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        model=SimpleNamespace(
            provider="mock",
            api_key="chat-secret",
            base_url="https://chat.example/v1",
        ),
        memory=SimpleNamespace(
            embeddings_enabled=True,
            embedding_provider="openai_compatible",
            embedding_model="bge-m3",
            embedding_base_url="https://embedding.example/v1",
            embedding_api_key="embedding-secret",
        ),
    )
    _EmbeddingHTTPClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _EmbeddingHTTPClient)

    runtime = configured_embedding_runtime(settings)
    vectors = await runtime.embed(["paper one", "paper two"])
    await runtime.aclose()

    assert runtime.enabled is True
    assert runtime.model == "bge-m3"
    assert runtime.space_id.startswith("emb-v1:")
    assert vectors == [[1.0], [2.0]]
    assert _EmbeddingHTTPClient.calls[0]["json"]["model"] == "bge-m3"
    assert _EmbeddingHTTPClient.calls[0]["headers"]["Authorization"] == (
        "Bearer embedding-secret"
    )
    assert "embedding-secret" not in repr(runtime)
    assert "chat-secret" not in repr(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_base_url", "expects_authorization"),
    [
        ("https://embedding.example/v1/chat", True),
        ("https://different.example/v1", False),
    ],
)
async def test_chat_key_is_reused_only_for_same_origin(
    monkeypatch: pytest.MonkeyPatch,
    chat_base_url: str,
    expects_authorization: bool,
) -> None:
    settings = SimpleNamespace(
        model=SimpleNamespace(
            api_key="chat-secret",
            base_url=chat_base_url,
        ),
        memory=SimpleNamespace(
            embeddings_enabled=True,
            embedding_provider="openai_compatible",
            embedding_model="bge-m3",
            embedding_base_url="https://embedding.example/v1",
            embedding_api_key="",
        ),
    )
    _EmbeddingHTTPClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _EmbeddingHTTPClient)

    runtime = configured_embedding_runtime(settings)
    await runtime.embed(["paper one", "paper two"])
    await runtime.aclose()

    headers = _EmbeddingHTTPClient.calls[0]["headers"]
    assert ("Authorization" in headers) is expects_authorization


@pytest.mark.asyncio
async def test_http_failure_preserves_safe_status_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        model=SimpleNamespace(api_key="", base_url=""),
        memory=SimpleNamespace(
            embeddings_enabled=True,
            embedding_provider="openai_compatible",
            embedding_model="bge-m3",
            embedding_base_url="https://embedding.example/v1",
            embedding_api_key="embedding-secret",
        ),
    )
    monkeypatch.setattr(httpx, "AsyncClient", _UnauthorizedHTTPClient)
    runtime = configured_embedding_runtime(settings)

    with pytest.raises(EmbeddingRuntimeError) as captured:
        await runtime.embed(["paper"])
    await runtime.aclose()

    assert captured.value.code == "embedding_http_error"
    assert captured.value.http_status == 401
    assert str(captured.value) == "embedding endpoint returned HTTP 401"
    assert "embedding-secret" not in str(captured.value)


class _EmbeddingClient:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text))] for text in texts]


@pytest.mark.asyncio
async def test_disabled_runtime_rejects_embedding() -> None:
    runtime = ConfiguredEmbeddingRuntime(
        enabled=False,
        model="bge-m3",
        base_url="",
        _client=_EmbeddingClient(),
    )

    with pytest.raises(NotImplementedError, match="embeddings are disabled"):
        await runtime.embed(["paper"])


def test_embedding_space_id_binds_endpoint_and_model_without_exposing_url() -> None:
    first = embedding_space_id(
        provider="openai_compatible",
        base_url="HTTPS://Embedding.Example:443/v1/",
        model="bge-m3",
    )
    normalized = embedding_space_id(
        provider="openai_compatible",
        base_url="https://embedding.example/v1",
        model="bge-m3",
    )
    different = embedding_space_id(
        provider="openai_compatible",
        base_url="https://other.example/v1",
        model="bge-m3",
    )

    assert first == normalized
    assert first != different
    assert "embedding.example" not in first
    assert "secret" not in first
    assert (
        embedding_space_id(
            provider="openai_compatible",
            base_url="https://embedding.example/v1?token=secret",
            model="bge-m3",
        )
        == ""
    )


def test_embedding_endpoint_handles_ipv6_and_malformed_ipv6() -> None:
    explicit_default_port = embedding_space_id(
        provider="openai_compatible",
        base_url="http://[::1]:80/v1/",
        model="bge-m3",
    )
    implicit_default_port = embedding_space_id(
        provider="openai_compatible",
        base_url="http://[::1]/v1",
        model="bge-m3",
    )
    settings = SimpleNamespace(
        model=SimpleNamespace(api_key="", base_url=""),
        memory=SimpleNamespace(
            embeddings_enabled=True,
            embedding_provider="openai_compatible",
            embedding_model="bge-m3",
            embedding_base_url="http://[::1]:8000/v1/",
            embedding_api_key="",
        ),
    )

    runtime = configured_embedding_runtime(settings)

    assert explicit_default_port == implicit_default_port
    assert explicit_default_port.startswith("emb-v1:")
    assert runtime.base_url == "http://[::1]:8000/v1"
    assert (
        embedding_space_id(
            provider="openai_compatible",
            base_url="http://[::1/v1",
            model="bge-m3",
        )
        == ""
    )


@pytest.mark.asyncio
async def test_runtime_rejects_endpoint_query_instead_of_misplacing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        model=SimpleNamespace(api_key="chat-secret", base_url=""),
        memory=SimpleNamespace(
            embeddings_enabled=True,
            embedding_provider="openai_compatible",
            embedding_model="bge-m3",
            embedding_base_url="https://embedding.example/v1?token=secret",
            embedding_api_key="embedding-secret",
        ),
    )
    _EmbeddingHTTPClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _EmbeddingHTTPClient)
    runtime = configured_embedding_runtime(settings)

    with pytest.raises(NotImplementedError, match="endpoint and model"):
        await runtime.embed(["paper"])

    assert runtime.base_url == ""
    assert runtime.space_id == ""
    assert _EmbeddingHTTPClient.calls == []


def _write_specter2_assets(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    base = tmp_path / "private-base-model"
    adapter = tmp_path / "private-adapter"
    base.mkdir()
    adapter.mkdir()
    (base / "config.json").write_text('{"hidden_size":768}', encoding="utf-8")
    (base / "tokenizer.json").write_text('{"model":"test"}', encoding="utf-8")
    (base / "pytorch_model.bin").write_bytes(b"base-weights-v1")
    (adapter / "adapter_config.json").write_text(
        '{"name":"proximity"}', encoding="utf-8"
    )
    (adapter / "pytorch_adapter.bin").write_bytes(b"adapter-weights-v1")
    return base, adapter


def _specter_settings(
    *,
    base: Path,
    adapter: Path,
    python: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        model=SimpleNamespace(api_key="chat-secret", base_url="https://chat.invalid"),
        memory=SimpleNamespace(
            embeddings_enabled=True,
            embedding_provider="specter2",
            embedding_model="allenai/specter2-proximity",
            embedding_base_url="https://must-not-be-used.invalid/v1",
            embedding_api_key="embedding-secret",
            embedding_dim=768,
            embedding_specter2_python=python,
            embedding_specter2_base_model=str(base),
            embedding_specter2_adapter=str(adapter),
            embedding_specter2_device="cpu",
        ),
    )


def test_specter2_space_is_portable_and_binds_weight_content(tmp_path: Path) -> None:
    first_base, first_adapter = _write_specter2_assets(tmp_path / "first")
    second_root = tmp_path / "second"
    second_root.mkdir()
    second_base, second_adapter = _write_specter2_assets(second_root)
    first = specter2_embedding_space_id(
        provider="specter2",
        model="allenai/specter2-proximity",
        base_model_path=str(first_base),
        adapter_path=str(first_adapter),
    )
    same_content_different_paths = specter2_embedding_space_id(
        provider="specter2",
        model="allenai/specter2-proximity",
        base_model_path=str(second_base),
        adapter_path=str(second_adapter),
    )
    (first_adapter / "pytorch_adapter.bin").write_bytes(b"adapter-weights-v2")
    changed_weights = specter2_embedding_space_id(
        provider="specter2",
        model="allenai/specter2-proximity",
        base_model_path=str(first_base),
        adapter_path=str(first_adapter),
    )

    assert first.startswith("emb-v2:")
    assert first == same_content_different_paths
    assert first != changed_weights
    assert str(first_base) not in first
    assert str(first_adapter) not in first


@pytest.mark.asyncio
async def test_specter2_runtime_uses_persistent_jsonl_worker_and_aclose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, adapter = _write_specter2_assets(tmp_path)
    worker = tmp_path / "fake_worker.py"
    worker.write_text(
        "import json, sys\n"
        "print(json.dumps({'type':'ready','dimension':768}), flush=True)\n"
        "counter = 0\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if request.get('type') == 'close': break\n"
        "    counter += 1\n"
        "    rows = [[float(counter)] + [0.0] * 767 for _ in request['texts']]\n"
        "    print(json.dumps({'type':'result','id':request['id'],'vectors':rows}), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(embedding_runtime_module, "_SPECTER2_WORKER_PATH", worker)
    settings = _specter_settings(base=base, adapter=adapter, python=sys.executable)

    runtime = configured_embedding_runtime(settings)
    first = await runtime.embed(["Title: First\nAbstract: body"])
    process = runtime._client._process
    second = await runtime.embed(["Title: Second\nAbstract: body"])
    await runtime.aclose()

    assert runtime.provider == "specter2"
    assert runtime.model == "allenai/specter2-proximity"
    assert runtime.base_url == ""
    assert runtime.dimension == 768
    assert runtime.space_id == configured_embedding_space_id(settings)
    assert first[0][0] == 1.0
    assert second[0][0] == 2.0
    assert process is not None and process.returncode == 0
    assert str(base) not in repr(runtime)
    assert str(adapter) not in repr(runtime)
    assert "embedding-secret" not in repr(runtime)


@pytest.mark.asyncio
async def test_specter2_worker_error_does_not_leak_input_or_local_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, adapter = _write_specter2_assets(tmp_path)
    worker = tmp_path / "unsafe_fake_worker.py"
    secret_input = "PRIVATE PAPER CONTENT"
    worker.write_text(
        "import json, sys\n"
        "print(json.dumps({'type':'ready','dimension':768}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if request.get('type') == 'close': break\n"
        "    print(json.dumps({'type':'error','message':request['texts'][0] + sys.argv[-3]}), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(embedding_runtime_module, "_SPECTER2_WORKER_PATH", worker)
    runtime = configured_embedding_runtime(
        _specter_settings(base=base, adapter=adapter, python=sys.executable)
    )

    with pytest.raises(EmbeddingRuntimeError) as captured:
        await runtime.embed([secret_input])
    await runtime.aclose()

    assert captured.value.code == "specter2_embedding_failed"
    assert str(captured.value) == "local SPECTER2 embedding failed"
    assert secret_input not in str(captured.value)
    assert str(base) not in str(captured.value)
    assert str(adapter) not in str(captured.value)


@pytest.mark.asyncio
async def test_incomplete_specter2_runtime_fails_without_exposing_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "private-missing-model"
    settings = _specter_settings(
        base=missing,
        adapter=missing,
        python=sys.executable,
    )
    runtime = configured_embedding_runtime(settings)

    with pytest.raises(EmbeddingRuntimeError) as captured:
        await runtime.embed(["PRIVATE INPUT"])

    assert captured.value.code == "specter2_not_configured"
    assert str(missing) not in str(captured.value)
    assert "PRIVATE INPUT" not in str(captured.value)
