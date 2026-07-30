#!/usr/bin/env python3
"""Build the paper-review Arena preference FAISS index."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SKILL_DIR = Path(__file__).resolve().parents[1]


class _PortableEmbeddingError(RuntimeError):
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


def _canonical_embedding_endpoint(base_url: str) -> str:
    try:
        parts = urlsplit(str(base_url or "").strip())
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
    default_port = 443 if scheme == "https" else 80
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if port in {None, default_port} else f"{rendered_host}:{port}"
    path = "/" + "/".join(segment for segment in parts.path.split("/") if segment)
    return urlunsplit((scheme, netloc, path.rstrip("/"), "", ""))


def _embedding_space_id(base_url: str, model: str) -> str:
    canonical = _canonical_embedding_endpoint(base_url)
    if not canonical or not str(model or "").strip():
        return ""
    payload = f"embedding-space-v1\0openai_compatible\0{canonical}\0{model.strip()}"
    return f"emb-v1:{hashlib.sha256(payload.encode()).hexdigest()}"


def _load_preference_memory() -> Any:
    spec = importlib.util.spec_from_file_location(
        "paper_review_preference_memory_builder", SKILL_DIR / "preference_memory.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("could not load paper-review preference_memory.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _OpenAICompatibleEmbedding:
    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self._base_url = _canonical_embedding_endpoint(base_url)
        if not self._base_url:
            raise ValueError(
                "embedding base URL must be absolute HTTP(S) without credentials, "
                "query parameters, or fragments"
            )
        self._api_key = api_key
        self.model = model
        self.space_id = _embedding_space_id(base_url, model)
        self._client: Any | None = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                json={"model": self.model, "input": texts},
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _PortableEmbeddingError(
                f"embedding endpoint returned HTTP {exc.response.status_code}",
                code="embedding_http_error",
                http_status=int(exc.response.status_code),
            ) from None
        except httpx.TimeoutException:
            raise _PortableEmbeddingError(
                "embedding request timed out", code="embedding_timeout"
            ) from None
        except httpx.RequestError as exc:
            raise _PortableEmbeddingError(
                f"embedding transport failed ({type(exc).__name__})",
                code="embedding_transport_error",
            ) from None
        try:
            payload = response.json()
        except ValueError:
            raise _PortableEmbeddingError(
                "embedding endpoint returned invalid JSON",
                code="embedding_invalid_response",
            ) from None
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise _PortableEmbeddingError(
                "embedding endpoint returned the wrong number of rows",
                code="embedding_invalid_response",
            )
        by_index: dict[int, list[float]] = {}
        for fallback, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("embedding"), list):
                raise _PortableEmbeddingError(
                    "embedding endpoint returned a malformed row",
                    code="embedding_invalid_response",
                )
            try:
                index = int(row.get("index", fallback))
                vector = [float(value) for value in row["embedding"]]
            except (TypeError, ValueError):
                raise _PortableEmbeddingError(
                    "embedding endpoint returned a malformed vector",
                    code="embedding_invalid_response",
                ) from None
            if index in by_index:
                raise _PortableEmbeddingError(
                    "embedding endpoint returned duplicate indices",
                    code="embedding_invalid_response",
                )
            by_index[index] = vector
        if set(by_index) != set(range(len(texts))):
            raise _PortableEmbeddingError(
                "embedding endpoint returned non-contiguous indices",
                code="embedding_invalid_response",
            )
        return [by_index[index] for index in range(len(texts))]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a paper-level exact-cosine FAISS index over anonymous Review "
            "Arena preferences. By default this uses Omni's configured SPECTER2 "
            "embedding runtime."
        )
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--index",
        type=Path,
        required=True,
        help=(
            "owned output directory containing index.json and an immutable "
            "vectors.faiss/papers.jsonl/preferences.pack generation"
        ),
    )
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--limit", type=int, help="optional battle limit for smoke tests")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--embedding-base-url", default="")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--api-key-env", default="OMNI_EMBEDDING_API_KEY")
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def _embedding_runtime(args: argparse.Namespace) -> tuple[Any, str]:
    if args.embedding_base_url:
        model = str(args.embedding_model or "").strip()
        if not model:
            raise ValueError("--embedding-model is required with --embedding-base-url")
        runtime = _OpenAICompatibleEmbedding(
            base_url=str(args.embedding_base_url),
            api_key=os.environ.get(str(args.api_key_env), ""),
            model=model,
        )
        return runtime, model
    try:
        from omni.research import configured_embedding_runtime
    except ImportError as exc:
        raise RuntimeError(
            "Omni is not importable. Supply --embedding-base-url and "
            f"--embedding-model, with the key in {args.api_key_env}."
        ) from exc
    runtime = configured_embedding_runtime()
    if not runtime.enabled:
        raise RuntimeError(
            "Omni embeddings are disabled. Configure the local SPECTER2 proximity "
            "runtime with `omni config embeddings --help`."
        )
    configured_model = str(runtime.model or "").strip()
    requested_model = str(args.embedding_model or "").strip()
    if requested_model and requested_model != configured_model:
        raise ValueError(
            f"--embedding-model {requested_model} does not match Omni's configured "
            f"model {configured_model}"
        )
    return runtime, configured_model


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    memory = _load_preference_memory()
    if args.report is not None:
        report = args.report.expanduser().resolve()
        dataset = args.dataset.expanduser().resolve()
        index = args.index.expanduser().resolve()
        if (
            report == dataset
            or dataset in report.parents
            or report == index
            or index in report.parents
        ):
            raise ValueError("--report must remain outside --dataset and --index")
    runtime, model = _embedding_runtime(args)

    def progress(update: dict[str, Any]) -> None:
        print(json.dumps(update, ensure_ascii=False), file=sys.stderr, flush=True)

    try:
        return await memory.build_preference_index(
            args.dataset,
            args.index,
            embedder=runtime.embed,
            embedding_model=model,
            embedding_space_id=str(getattr(runtime, "space_id", "") or ""),
            batch_size=args.batch_size,
            rebuild=args.rebuild,
            limit=args.limit,
            progress=progress,
        )
    finally:
        close = getattr(runtime, "aclose", None)
        if callable(close):
            await close()


def _safe_cli_error(exc: Exception) -> str:
    if isinstance(exc, (FileNotFoundError, ValueError, RuntimeError)):
        return str(exc)
    return f"index build failed ({type(exc).__name__})"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps({"status": "error", "error": _safe_cli_error(exc)}),
            file=sys.stderr,
        )
        return 1
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report is not None:
        report = args.report.expanduser().resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
