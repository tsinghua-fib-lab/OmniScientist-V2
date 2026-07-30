"""Public access to Omni's owner-configured embedding runtime.

Skill-side index builders need the exact embedding configuration that will be
used at query time, but should not import configuration or provider internals.
This adapter is embedding-only: it never falls back to the chat provider or to
the offline mock model, and it never exposes API keys.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

SPECTER2_DEFAULT_MODEL = "allenai/specter2-proximity"
SPECTER2_DIMENSION = 768
_SPECTER2_WORKER_PATH = Path(__file__).with_name("specter2_worker.py")
_SPECTER2_SPACE_POLICY = "specter2-local-cls-title-abstract-v1"


class EmbeddingRuntimeError(RuntimeError):
    """An actionable embedding failure that contains no endpoint or credential."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _canonical_endpoint(value: str) -> str:
    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    scheme = parts.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not parts.hostname
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        return ""
    host = parts.hostname.casefold()
    try:
        port = parts.port
    except ValueError:
        return ""
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = (
        rendered_host
        if port in {None, default_port}
        else f"{rendered_host}:{port}"
    )
    path = "/" + "/".join(segment for segment in parts.path.split("/") if segment)
    return urlunsplit((scheme, netloc, path.rstrip("/"), "", ""))


def _same_origin(left: str, right: str) -> bool:
    left_parts = urlsplit(_canonical_endpoint(left))
    right_parts = urlsplit(_canonical_endpoint(right))
    return bool(
        left_parts.scheme
        and left_parts.scheme == right_parts.scheme
        and left_parts.hostname == right_parts.hostname
        and left_parts.port == right_parts.port
    )


def embedding_space_id(*, provider: str, base_url: str, model: str) -> str:
    """Fingerprint an embedding space without storing its service URL."""

    canonical = _canonical_endpoint(base_url)
    normalized_model = str(model or "").strip()
    if not canonical or not normalized_model:
        return ""
    payload = (
        f"embedding-space-v1\0{str(provider or 'openai_compatible').casefold()}\0"
        f"{canonical}\0{normalized_model}"
    )
    return f"emb-v1:{hashlib.sha256(payload.encode()).hexdigest()}"


@lru_cache(maxsize=64)
def _sha256_file_cached(
    path: str,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
) -> str:
    """Hash an immutable-looking model asset, keyed by its filesystem identity."""

    del size, mtime_ns, ctime_ns
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _local_model_identity(value: str, *, adapter: bool) -> str | None:
    """Return a path-independent content identity for a local model directory."""

    try:
        root = Path(value).expanduser().resolve(strict=True)
        if not root.is_dir():
            return None
        exact_names = {
            "adapter_config.json",
            "config.json",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocab.txt",
        }
        assets = [
            path
            for path in root.iterdir()
            if path.is_file()
            and (
                path.name in exact_names
                or path.suffix == ".safetensors"
                or (path.suffix == ".bin" and "model" in path.name)
                or (path.suffix == ".bin" and "adapter" in path.name)
                or path.name.endswith(".index.json")
            )
        ]
        weight_assets = [
            path
            for path in assets
            if path.suffix in {".bin", ".safetensors"}
        ]
        if not weight_assets:
            return None
        if adapter and not any(
            "adapter" in path.name or path.name == "pytorch_model.bin"
            for path in weight_assets
        ):
            return None
        digest = hashlib.sha256()
        for path in sorted(assets, key=lambda item: item.name):
            stat = path.stat()
            content_hash = _sha256_file_cached(
                str(path.resolve(strict=True)),
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
            digest.update(f"{path.name}\0{stat.st_size}\0{content_hash}\n".encode())
        return digest.hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None


def specter2_embedding_space_id(
    *,
    provider: str,
    model: str,
    base_model_path: str,
    adapter_path: str,
) -> str:
    """Fingerprint a local SPECTER2 space from model assets, not install paths.

    The returned digest never contains a filesystem path. Device and Python
    executable are intentionally omitted because they do not define the model
    space; the base/tokenizer and adapter content do. Identical assets copied
    to another machine therefore retain the same embedding-space identity.
    """

    normalized_provider = str(provider or "").strip().casefold()
    normalized_model = str(model or "").strip()
    if normalized_provider != "specter2" or not normalized_model:
        return ""
    base_identity = _local_model_identity(base_model_path, adapter=False)
    adapter_identity = _local_model_identity(adapter_path, adapter=True)
    if base_identity is None or adapter_identity is None:
        return ""
    payload = "\0".join(
        (
            "specter2-content-space-v2",
            _SPECTER2_SPACE_POLICY,
            normalized_provider,
            normalized_model,
            base_identity,
            adapter_identity,
        )
    )
    return f"emb-v2:{hashlib.sha256(payload.encode()).hexdigest()}"


class _OpenAIEmbeddingClient:
    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self._base_url = _canonical_endpoint(base_url)
        self._api_key = api_key
        self._model = model
        self._client: httpx.AsyncClient | None = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._base_url or not self._model:
            raise NotImplementedError("embedding endpoint and model are not configured")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": texts},
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = int(exc.response.status_code)
            raise EmbeddingRuntimeError(
                f"embedding endpoint returned HTTP {status}",
                code="embedding_http_error",
                http_status=status,
            ) from None
        except httpx.TimeoutException:
            raise EmbeddingRuntimeError(
                "embedding request timed out",
                code="embedding_timeout",
            ) from None
        except httpx.RequestError as exc:
            raise EmbeddingRuntimeError(
                f"embedding transport failed ({type(exc).__name__})",
                code="embedding_transport_error",
            ) from None
        try:
            payload = response.json()
        except ValueError:
            raise EmbeddingRuntimeError(
                "embedding endpoint returned invalid JSON",
                code="embedding_invalid_response",
            ) from None
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise EmbeddingRuntimeError(
                "embedding endpoint returned the wrong number of rows",
                code="embedding_invalid_response",
            )
        by_index: dict[int, list[float]] = {}
        for fallback_index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise EmbeddingRuntimeError(
                    "embedding endpoint returned a malformed row",
                    code="embedding_invalid_response",
                )
            try:
                index = int(row.get("index", fallback_index))
            except (TypeError, ValueError):
                raise EmbeddingRuntimeError(
                    "embedding endpoint returned a malformed row index",
                    code="embedding_invalid_response",
                ) from None
            vector = row.get("embedding")
            if index in by_index or not isinstance(vector, list):
                raise EmbeddingRuntimeError(
                    "embedding endpoint returned duplicate or malformed indices",
                    code="embedding_invalid_response",
                )
            try:
                by_index[index] = [float(value) for value in vector]
            except (TypeError, ValueError):
                raise EmbeddingRuntimeError(
                    "embedding endpoint returned a non-numeric vector",
                    code="embedding_invalid_response",
                ) from None
        if set(by_index) != set(range(len(texts))):
            raise EmbeddingRuntimeError(
                "embedding endpoint returned non-contiguous indices",
                code="embedding_invalid_response",
            )
        return [by_index[index] for index in range(len(texts))]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class _UnavailableEmbeddingClient:
    """A safe failure object for an unsupported or incomplete provider."""

    def __init__(self, *, code: str, message: str) -> None:
        self._code = code
        self._message = message

    async def embed(self, _texts: list[str]) -> list[list[float]]:
        raise EmbeddingRuntimeError(self._message, code=self._code)

    async def aclose(self) -> None:
        return None


class _Specter2EmbeddingClient:
    """Persistent JSONL bridge to a dedicated, offline SPECTER2 Python."""

    _START_TIMEOUT_S = 300.0
    _REQUEST_TIMEOUT_S = 300.0
    _MAX_REQUEST_TEXTS = 64
    _MAX_PROTOCOL_LINE = 32 * 1024 * 1024

    def __init__(
        self,
        *,
        python: str,
        base_model: str,
        adapter: str,
        device: str,
    ) -> None:
        self._python = python
        self._base_model = base_model
        self._adapter = adapter
        self._device = device
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_request_id = 1

    def _configuration_is_usable(self) -> bool:
        try:
            return bool(
                Path(self._python).expanduser().is_file()
                and Path(self._base_model).expanduser().is_dir()
                and Path(self._adapter).expanduser().is_dir()
                and _SPECTER2_WORKER_PATH.is_file()
            )
        except OSError:
            return False

    async def _discard_process(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    async def _read_payload(self, *, timeout: float) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise EmbeddingRuntimeError(
                "local embedding worker is unavailable",
                code="specter2_worker_unavailable",
            )
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        except TimeoutError:
            await self._discard_process()
            raise EmbeddingRuntimeError(
                "local embedding worker timed out",
                code="specter2_worker_timeout",
            ) from None
        except (OSError, ValueError):
            await self._discard_process()
            raise EmbeddingRuntimeError(
                "local embedding worker communication failed",
                code="specter2_worker_protocol_error",
            ) from None
        if not line:
            await self._discard_process()
            raise EmbeddingRuntimeError(
                "local embedding worker stopped unexpectedly",
                code="specter2_worker_stopped",
            )
        try:
            payload = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            await self._discard_process()
            raise EmbeddingRuntimeError(
                "local embedding worker returned an invalid response",
                code="specter2_worker_protocol_error",
            ) from None
        if not isinstance(payload, dict):
            await self._discard_process()
            raise EmbeddingRuntimeError(
                "local embedding worker returned an invalid response",
                code="specter2_worker_protocol_error",
            )
        return payload

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process
        if not self._configuration_is_usable():
            raise EmbeddingRuntimeError(
                "local SPECTER2 configuration is incomplete",
                code="specter2_not_configured",
            )
        environment = dict(os.environ)
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "TOKENIZERS_PARALLELISM": "false",
            }
        )
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._python,
                "-I",
                "-u",
                str(_SPECTER2_WORKER_PATH),
                "--base-model",
                self._base_model,
                "--adapter",
                self._adapter,
                "--device",
                self._device,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                limit=self._MAX_PROTOCOL_LINE,
            )
        except (OSError, ValueError):
            self._process = None
            raise EmbeddingRuntimeError(
                "local embedding worker could not start",
                code="specter2_worker_start_failed",
            ) from None
        ready = await self._read_payload(timeout=self._START_TIMEOUT_S)
        if (
            ready.get("type") != "ready"
            or ready.get("dimension") != SPECTER2_DIMENSION
        ):
            await self._discard_process()
            raise EmbeddingRuntimeError(
                "local embedding worker could not initialize",
                code="specter2_worker_initialization_failed",
            )
        return self._process

    async def _embed_chunk(self, texts: list[str]) -> list[list[float]]:
        async with self._lock:
            process = await self._ensure_process()
            if process.stdin is None:
                await self._discard_process()
                raise EmbeddingRuntimeError(
                    "local embedding worker is unavailable",
                    code="specter2_worker_unavailable",
                )
            request_id = self._next_request_id
            self._next_request_id += 1
            request = json.dumps(
                {"type": "embed", "id": request_id, "texts": texts},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            try:
                process.stdin.write(request)
                await process.stdin.drain()
                response = await self._read_payload(timeout=self._REQUEST_TIMEOUT_S)
            except asyncio.CancelledError:
                await self._discard_process()
                raise
            except (BrokenPipeError, ConnectionError, OSError):
                await self._discard_process()
                raise EmbeddingRuntimeError(
                    "local embedding worker communication failed",
                    code="specter2_worker_protocol_error",
                ) from None
            if response.get("type") == "error":
                raise EmbeddingRuntimeError(
                    "local SPECTER2 embedding failed",
                    code="specter2_embedding_failed",
                )
            if response.get("type") != "result" or response.get("id") != request_id:
                await self._discard_process()
                raise EmbeddingRuntimeError(
                    "local embedding worker returned an invalid response",
                    code="specter2_worker_protocol_error",
                )
            raw_vectors = response.get("vectors")
            if not isinstance(raw_vectors, list) or len(raw_vectors) != len(texts):
                await self._discard_process()
                raise EmbeddingRuntimeError(
                    "local embedding worker returned the wrong number of rows",
                    code="specter2_invalid_response",
                )
            vectors: list[list[float]] = []
            try:
                for raw_vector in raw_vectors:
                    if not isinstance(raw_vector, list):
                        raise ValueError
                    vector = [float(value) for value in raw_vector]
                    if len(vector) != SPECTER2_DIMENSION or not all(
                        math.isfinite(value) for value in vector
                    ):
                        raise ValueError
                    vectors.append(vector)
            except (TypeError, ValueError):
                await self._discard_process()
                raise EmbeddingRuntimeError(
                    "local embedding worker returned a malformed vector",
                    code="specter2_invalid_response",
                ) from None
            return vectors

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(text, str) for text in texts):
            raise EmbeddingRuntimeError(
                "local embeddings require text inputs",
                code="specter2_invalid_input",
            )
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._MAX_REQUEST_TEXTS):
            vectors.extend(
                await self._embed_chunk(texts[start : start + self._MAX_REQUEST_TEXTS])
            )
        return vectors

    async def aclose(self) -> None:
        async with self._lock:
            process = self._process
            if process is None:
                return
            if process.returncode is None and process.stdin is not None:
                with contextlib.suppress(BrokenPipeError, ConnectionError, OSError):
                    process.stdin.write(b'{"type":"close"}\n')
                    await process.stdin.drain()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    pass
            await self._discard_process()


@dataclass(frozen=True)
class ConfiguredEmbeddingRuntime:
    """A key-free description and callable view of configured embeddings."""

    enabled: bool
    model: str
    base_url: str
    _client: Any = field(repr=False)
    space_id: str = ""
    provider: str = "openai_compatible"
    dimension: int | None = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` with the active owner-scoped embedding client."""

        if not self.enabled:
            raise NotImplementedError(
                "embeddings are disabled; run `omni config embeddings --enable ...`"
            )
        return await self._client.embed(texts)

    async def aclose(self) -> None:
        """Close the reusable provider client, when the implementation owns one."""

        close = getattr(self._client, "aclose", None)
        if callable(close):
            await close()

    async def __aenter__(self) -> ConfiguredEmbeddingRuntime:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.aclose()


def configured_embedding_runtime(settings: Any | None = None) -> ConfiguredEmbeddingRuntime:
    """Return the active embedding runtime without exposing its secret.

    ``settings`` is accepted so an executing skill can reuse its already
    resolved workspace settings. Omitting it loads the active Omni settings,
    which is convenient for standalone index builders.
    """

    if settings is None:
        from omni.config import load_settings

        settings = load_settings()
    memory = settings.memory
    model_settings = settings.model
    provider = str(memory.embedding_provider or "openai_compatible").strip().casefold()
    model = str(memory.embedding_model or "").strip()
    if provider == "specter2":
        python = str(getattr(memory, "embedding_specter2_python", "") or "")
        base_model = str(
            getattr(memory, "embedding_specter2_base_model", "") or ""
        )
        adapter = str(getattr(memory, "embedding_specter2_adapter", "") or "")
        device = str(
            getattr(memory, "embedding_specter2_device", "cpu") or "cpu"
        )
        client = _Specter2EmbeddingClient(
            python=python,
            base_model=base_model,
            adapter=adapter,
            device=device,
        )
        return ConfiguredEmbeddingRuntime(
            enabled=bool(memory.embeddings_enabled),
            model=model,
            base_url="",
            _client=client,
            space_id=specter2_embedding_space_id(
                provider=provider,
                model=model,
                base_model_path=base_model,
                adapter_path=adapter,
            ),
            provider=provider,
            dimension=SPECTER2_DIMENSION,
        )
    if provider not in {"openai", "openai_compatible"}:
        return ConfiguredEmbeddingRuntime(
            enabled=bool(memory.embeddings_enabled),
            model=model,
            base_url="",
            _client=_UnavailableEmbeddingClient(
                code="embedding_provider_unsupported",
                message="configured embedding provider is unsupported",
            ),
            provider=provider,
        )
    configured_base_url = str(memory.embedding_base_url or "")
    base_url = _canonical_endpoint(configured_base_url)
    embedding_key = str(memory.embedding_api_key or "")
    if (
        not embedding_key
        and _same_origin(
            configured_base_url,
            str(getattr(model_settings, "base_url", "") or ""),
        )
    ):
        embedding_key = str(model_settings.api_key or "")
    client = _OpenAIEmbeddingClient(
        base_url=base_url,
        api_key=embedding_key,
        model=model,
    )
    return ConfiguredEmbeddingRuntime(
        enabled=bool(memory.embeddings_enabled),
        model=model,
        base_url=base_url,
        _client=client,
        space_id=embedding_space_id(
            provider=provider,
            base_url=base_url,
            model=model,
        ),
        provider=provider,
        dimension=int(getattr(memory, "embedding_dim", 0) or 0) or None,
    )


def configured_embedding_space_id(settings: Any | None = None) -> str:
    """Return the active provider-aware embedding-space fingerprint.

    This is safe for orchestration code to call before embedding: constructing
    the runtime does not start the local worker or issue a network request.
    """

    return configured_embedding_runtime(settings).space_id


__all__ = [
    "ConfiguredEmbeddingRuntime",
    "EmbeddingRuntimeError",
    "configured_embedding_runtime",
    "configured_embedding_space_id",
    "embedding_space_id",
    "specter2_embedding_space_id",
]
