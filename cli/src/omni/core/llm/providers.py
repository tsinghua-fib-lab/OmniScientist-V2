"""LLM providers: deterministic ``mock`` + ``openai_compatible`` HTTP."""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from collections.abc import AsyncIterator
from typing import Any

import httpx

from omni.core.llm.client import (
    ChatWithToolsResult,
    LLMClient,
    TokenSink,
    ToolCall,
    _emit_delta,
)
from omni.core.llm.errors import (
    LLMErrorInfo,
    LLMProviderError,
    classify_llm_exception,
    from_http_status_error,
)
from omni.core.tool_transcript import normalize_tool_transcript

logger = logging.getLogger(__name__)
_DISABLE_EMBEDDING_STATUSES = {400, 401, 403, 404, 405, 410, 422, 501}


def _merge_tool_call_fragment(frags: dict[int, dict[str, Any]], tc: dict[str, Any]) -> None:
    """Accumulate a streamed ``tool_calls`` delta by its index (OpenAI SSE)."""
    idx = tc.get("index", 0)
    slot = frags.setdefault(idx, {"id": "", "name": "", "arguments": ""})
    if tc.get("id"):
        slot["id"] = tc["id"]
    fn = tc.get("function") or {}
    if fn.get("name"):
        slot["name"] = fn["name"]
    if fn.get("arguments"):
        slot["arguments"] += fn["arguments"]


def _finalize_tool_calls(frags: dict[int, dict[str, Any]]) -> list[ToolCall]:
    """Turn accumulated tool-call fragments into :class:`ToolCall`s (parsing args)."""
    calls: list[ToolCall] = []
    for _idx, slot in sorted(frags.items()):
        if not slot.get("name"):
            continue
        try:
            args = json.loads(slot.get("arguments") or "{}")
            arg_err = None
        except json.JSONDecodeError as exc:
            args, arg_err = {}, str(exc)
        calls.append(
            ToolCall(
                id=slot.get("id", ""), name=slot["name"], arguments=args, arguments_error=arg_err
            )
        )
    return calls


class MockProvider(LLMClient):
    """Offline, deterministic provider — an honest placeholder, not a brain.

    Lets every command run without an API key. It never infers intent or
    fabricates a tool call from the message (that is the model's job); with no
    model configured it returns a placeholder answer — optionally grounded in a
    tool observation already present in the transcript — and asks the user to
    configure a real model. This mirrors Claude Code / Codex, which also decline
    to act semantically without a model rather than keyword-guessing.
    """

    def __init__(self, model_name: str = "omni-mock", *, embeddings_enabled: bool = True) -> None:
        self.model = model_name
        self._embeddings_enabled = embeddings_enabled

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> ChatWithToolsResult:
        # No model → no intent to route: never fabricate a tool call from the
        # message. Return an honest placeholder, grounded in a tool observation
        # only if one is already present in the transcript.
        observation = _last_tool_text(messages)
        if observation:
            obs = observation.strip().replace("\n", " ")
            if len(obs) > 400:
                obs = obs[:400] + "…"
            content = (
                f"[Offline mock model] Tool observation: {obs}\n\n"
                "Configure a real model for a complete, substantive answer."
            )
        else:
            last_user = _last_user_text(messages)
            content = (
                "[Offline mock model] Request received:\n"
                f"  “{last_user.strip()[:300]}”\n\n"
                "This is an offline placeholder. The mock model does not invoke tools automatically. "
                "Configure a real provider, for example `omni config set model.provider openai`; "
                "the API key is stored in secrets.toml under the active Omni data directory."
            )
        return ChatWithToolsResult(content=content)

    async def chat(self, system: str, user: str, *, temperature: float = 0.3) -> str:
        return f"【mock】{user.strip()[:500]}"

    async def chat_stream(
        self, system: str, user: str, *, temperature: float = 0.3
    ) -> AsyncIterator[str]:
        for chunk in (await self.chat(system, user)).split(" "):
            yield chunk + " "

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._embeddings_enabled:
            raise NotImplementedError("embeddings disabled by configuration")
        return [_hash_embedding(t, dim=256) for t in texts]


class OpenAICompatibleProvider(LLMClient):
    """Talks to any OpenAI ``/chat/completions`` compatible endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 120.0,
        embedding_base_url: str = "",
        embedding_api_key: str = "",
        embedding_model: str = "text-embedding-3-small",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self.model = model
        self._timeout = timeout_s
        self._emb_base = (embedding_base_url or base_url).rstrip("/")
        self._emb_key = embedding_api_key or api_key
        self._emb_model = embedding_model
        self._emb_available: bool | None = None
        self._emb_failure = ""
        self._terminal_chat_failure: LLMErrorInfo | None = None

    def _ensure_chat_available(self) -> None:
        if self._terminal_chat_failure is not None:
            raise LLMProviderError(self._terminal_chat_failure)

    def _check_chat_status(self, response: httpx.Response) -> None:
        try:
            _raise_chat_status(response)
        except LLMProviderError as exc:
            if exc.info.category == "authentication":
                self._terminal_chat_failure = exc.info
            raise

    async def _check_stream_status(self, response: httpx.Response) -> None:
        if response.is_error:
            await response.aread()
        self._check_chat_status(response)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._key:
            h["Authorization"] = f"Bearer {self._key}"
        return h

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> ChatWithToolsResult:
        self._ensure_chat_available()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _sanitize_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._base}/chat/completions", json=payload, headers=self._headers()
            )
            self._check_chat_status(resp)
            data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message", {})
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
                arg_err = None
            except json.JSONDecodeError as exc:
                args, arg_err = {}, str(exc)
            calls.append(
                ToolCall(
                    id=tc.get("id", ""), name=fn.get("name", ""),
                    arguments=args, arguments_error=arg_err,
                )
            )
        return ChatWithToolsResult(
            content=msg.get("content") or "",
            tool_calls=calls,
            reasoning_content=msg.get("reasoning_content") or "",
            usage=data.get("usage") or {},
        )

    async def chat_with_tools_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
        on_delta: TokenSink | None = None,
    ) -> ChatWithToolsResult:
        """Native SSE streaming: forward content deltas to ``on_delta`` while
        accumulating tool-call fragments and usage. Any streaming/transport
        failure degrades to the non-streaming chunked fallback (so a flaky SSE
        never breaks a turn)."""
        self._ensure_chat_available()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _sanitize_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_frags: dict[int, dict[str, Any]] = {}
        usage: dict[str, int] = {}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                async with c.stream(
                    "POST", f"{self._base}/chat/completions",
                    json=payload, headers=self._headers(),
                ) as resp:
                    await self._check_stream_status(resp)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        chunk = line[len("data:"):].strip()
                        if chunk in ("", "[DONE]"):
                            continue
                        try:
                            data = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        if data.get("usage"):
                            usage = data["usage"]
                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            content_parts.append(piece)
                            if on_delta is not None:
                                await _emit_delta(on_delta, piece)
                        if delta.get("reasoning_content"):
                            reasoning_parts.append(delta["reasoning_content"])
                        for tc in delta.get("tool_calls") or []:
                            _merge_tool_call_fragment(tool_frags, tc)
        except Exception as exc:  # noqa: BLE001 - degrade compatible stream failures
            info = classify_llm_exception(exc)
            if info.category != "unknown":
                raise
            logger.warning("[llm] streaming failed; falling back to non-streaming", exc_info=True)
            return await super().chat_with_tools_stream(
                messages, tools, tool_choice=tool_choice,
                temperature=temperature, max_tokens=max_tokens, on_delta=on_delta,
            )
        return ChatWithToolsResult(
            content="".join(content_parts),
            tool_calls=_finalize_tool_calls(tool_frags),
            reasoning_content="".join(reasoning_parts),
            usage=usage,
        )

    async def chat(self, system: str, user: str, *, temperature: float = 0.3) -> str:
        self._ensure_chat_available()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._base}/chat/completions", json=payload, headers=self._headers()
            )
            self._check_chat_status(resp)
            data = resp.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    async def chat_stream(
        self, system: str, user: str, *, temperature: float = 0.3
    ) -> AsyncIterator[str]:
        self._ensure_chat_available()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            async with c.stream(
                "POST", f"{self._base}/chat/completions", json=payload, headers=self._headers()
            ) as resp:
                await self._check_stream_status(resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[len("data:") :].strip()
                    if chunk in ("", "[DONE]"):
                        continue
                    try:
                        delta = json.loads(chunk)["choices"][0]["delta"].get("content")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta:
                        yield delta

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._emb_available is False:
            raise NotImplementedError(self._emb_failure or "embeddings unavailable")
        if not self._emb_base or not self._emb_key or not self._emb_model:
            self._emb_available = False
            self._emb_failure = "embedding endpoint is not configured"
            raise NotImplementedError(self._emb_failure)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            try:
                resp = await c.post(
                    f"{self._emb_base}/embeddings",
                    json={"model": self._emb_model, "input": texts},
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._emb_key}"},
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in _DISABLE_EMBEDDING_STATUSES:
                    self._emb_available = False
                    self._emb_failure = f"embedding endpoint unavailable (HTTP {status})"
                    logger.warning(
                        "Embedding endpoint %s/embeddings returned HTTP %s; this endpoint "
                        "likely has no embeddings API. Falling back to keyword recall for this "
                        "process. To silence this, set memory.embeddings_enabled=false, or point "
                        "memory.embedding_base_url at an endpoint that serves /embeddings.",
                        self._emb_base,
                        status,
                    )
                    raise NotImplementedError(self._emb_failure) from exc
                raise
            data = resp.json()
        self._emb_available = True
        return [row["embedding"] for row in data.get("data", [])]


# ── helpers ──────────────────────────────────────────────────────────────


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def _last_tool_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "tool":
            return str(m.get("content") or "")
    return ""


def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a provider-valid transcript, repairing interrupted tool turns."""
    return normalize_tool_transcript(messages).messages


def _raise_chat_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise from_http_status_error(exc) from exc


async def _raise_stream_status(response: httpx.Response) -> None:
    """Read an SSE error body before classification, then raise a safe error."""
    if response.is_error:
        await response.aread()
    _raise_chat_status(response)


def _hash_embedding(text: str, dim: int = 256) -> list[float]:
    """Deterministic pseudo-embedding for offline/mock use."""
    vec: list[float] = []
    counter = 0
    while len(vec) < dim:
        h = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        for i in range(0, len(h), 4):
            (val,) = struct.unpack("<I", h[i : i + 4])
            vec.append((val / 0xFFFFFFFF) * 2.0 - 1.0)
            if len(vec) >= dim:
                break
        counter += 1
    # L2 normalise
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]
