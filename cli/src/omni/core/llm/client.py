"""LLM client interface + shared result types.

The :class:`LLMClient` surface is the subset the ReAct loop, memory, and
skills need. Two providers implement it: a deterministic offline ``mock``
provider (so the whole CLI works without an API key) and an
``openai_compatible`` provider (OpenAI / DeepSeek / Ollama / any
``/chat/completions`` endpoint).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# A sink for streamed answer text: each partial delta is handed to it as it
# arrives (may be sync or async — callers wrap accordingly).
TokenSink = Callable[[str], Any]


def _chunk_for_stream(text: str, size: int = 16) -> list[str]:
    """Split ``text`` into progressive pieces for providers without native SSE.

    Fixed-size character slices so ``"".join(chunks) == text`` exactly — this
    streams both whitespace-delimited (English) and script-continuous (CJK) text
    incrementally, giving a faithful progressive render even when we only have
    the full answer in hand.
    """
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)]


async def _emit_delta(sink: TokenSink, piece: str) -> None:
    """Feed one delta to a possibly-async token sink."""
    res = sink(piece)
    if asyncio.iscoroutine(res):
        await res

_RETRYABLE_NAMES = frozenset(
    {
        "TimeoutError", "ConnectError", "ConnectionError", "ReadError", "WriteError",
        "RemoteProtocolError", "HTTPStatusError", "RateLimitError", "ServiceUnavailable",
        "PoolTimeout", "ReadTimeout", "ConnectTimeout",
    }
)


def _is_retryable(exc: Exception) -> bool:
    """A transient error worth a same-provider retry (5xx / 429 / transport)."""
    from omni.core.llm.errors import classify_llm_exception

    classified = classify_llm_exception(exc)
    if classified.category != "unknown":
        return classified.retryable
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    return type(exc).__name__ in _RETRYABLE_NAMES


@dataclass(slots=True)
class RetryPolicy:
    """Transient-error retry budget shared by every LLM client wrapper.

    ``max_retries`` is *extra* attempts after the first. Backoff is exponential
    with symmetric jitter and capped at ``max_delay``; a server ``Retry-After``
    hint overrides the computed delay so we honour rate-limit windows instead of
    hammering. Defaults are deliberately modest so a single 429/5xx/transport
    blip cannot abandon a long research turn, without turning a hard outage into
    a multi-minute stall.
    """

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.1


def _retry_after_seconds(exc: Exception) -> float | None:
    """Parse a ``Retry-After`` header (numeric seconds or HTTP-date) if present."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:  # noqa: BLE001 — never let header quirks break retry
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(when.tzinfo or _dt.UTC)
    return max(0.0, (when - now).total_seconds())


def _retry_delay(policy: RetryPolicy, attempt: int, exc: Exception) -> float:
    """Delay before the next attempt: server hint, else jittered exp backoff."""
    hinted = _retry_after_seconds(exc)
    if hinted is not None:
        return min(hinted, policy.max_delay)
    base = min(policy.base_delay * (2**attempt), policy.max_delay)
    if policy.jitter > 0 and base > 0:
        base += base * policy.jitter * random.uniform(-1.0, 1.0)
    return max(0.0, base)


@dataclass(slots=True)
class ToolCall:
    """A single tool call requested by the model.

    ``arguments_error`` is set when the provider could not parse the model's raw
    argument string as JSON. The ReAct loop turns it into a model-visible failed
    observation (rather than silently invoking with ``{}``) so the model can
    re-issue the call with valid JSON instead of executing with wrong/empty args.
    """

    id: str
    name: str
    arguments: dict[str, Any]
    arguments_error: str | None = None

    def to_message_fragment(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(slots=True)
class ChatWithToolsResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning_content: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(ABC):
    """Minimal async LLM surface used across OmniScientist."""

    model: str = ""

    @abstractmethod
    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> ChatWithToolsResult: ...

    @abstractmethod
    async def chat(self, system: str, user: str, *, temperature: float = 0.3) -> str: ...

    async def chat_stream(
        self, system: str, user: str, *, temperature: float = 0.3
    ) -> AsyncIterator[str]:
        """Default: non-streaming fallback yielding the full answer once."""
        yield await self.chat(system, user, temperature=temperature)

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
        """Streaming variant of :meth:`chat_with_tools`.

        Default (providers without native SSE, and the offline mock): run the
        normal call, then, when the turn is a *text answer* (no tool calls),
        emit the content progressively through ``on_delta`` so the UI renders it
        as it "arrives". Providers override this for true token-by-token
        streaming; the ReAct loop uses whichever is available transparently.
        """
        result = await self.chat_with_tools(
            messages, tools, tool_choice=tool_choice,
            temperature=temperature, max_tokens=max_tokens,
        )
        if on_delta is not None and result.content and not result.tool_calls:
            for piece in _chunk_for_stream(result.content):
                await _emit_delta(on_delta, piece)
        return result

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings; providers without embeddings should raise."""
        raise NotImplementedError("embeddings not supported by this provider")


class FallbackLLMClient(LLMClient):
    """Try a primary client, then a chain of fallbacks on failure.

    Each client gets ``max_retries`` same-provider retries with exponential
    backoff on *transient* errors (5xx / 429 / transport), then falls back only
    for failures that a user-configured fallback may recover from. Structurally
    invalid tool transcripts fail closed because replaying them cannot repair the
    local protocol violation.
    """

    def __init__(
        self,
        primary: LLMClient,
        fallbacks: list[LLMClient],
        *,
        max_retries: int = 2,
        base_delay: float = 0.5,
    ) -> None:
        self._clients: list[LLMClient] = [primary, *fallbacks]
        self.model = primary.model
        self._max_retries = max(0, max_retries)
        self._base_delay = max(0.0, base_delay)
        self._retry_policy = RetryPolicy(max_retries=self._max_retries, base_delay=self._base_delay)

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for idx, client in enumerate(self._clients):
            for attempt in range(1 + self._max_retries):
                try:
                    return await getattr(client, method)(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — classify then retry/fail over
                    last_exc = exc
                    from omni.core.llm.errors import classify_llm_exception

                    info = classify_llm_exception(exc)
                    retryable = info.retryable if info.category != "unknown" else _is_retryable(exc)
                    if retryable and attempt < self._max_retries:
                        await asyncio.sleep(_retry_delay(self._retry_policy, attempt, exc))
                        continue
                    if not info.fallback_allowed and info.category != "unknown":
                        raise
                    if idx < len(self._clients) - 1:
                        logger.warning(
                            "[llm] provider #%d %s() failed category=%s; failing over to next provider",
                            idx, method, info.category,
                        )
                    break
        assert last_exc is not None
        raise last_exc

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> ChatWithToolsResult:
        return await self._call(
            "chat_with_tools", messages, tools,
            tool_choice=tool_choice, temperature=temperature, max_tokens=max_tokens,
        )

    async def chat(self, system: str, user: str, *, temperature: float = 0.3) -> str:
        return await self._call("chat", system, user, temperature=temperature)

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
        return await self._call(
            "chat_with_tools_stream", messages, tools,
            tool_choice=tool_choice, temperature=temperature, max_tokens=max_tokens,
            on_delta=on_delta,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._call("embed", texts)

    async def chat_stream(
        self, system: str, user: str, *, temperature: float = 0.3
    ) -> AsyncIterator[str]:
        last_exc: Exception | None = None
        for client in self._clients:
            started = False
            try:
                async for chunk in client.chat_stream(system, user, temperature=temperature):
                    started = True
                    yield chunk
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if started:  # can't cleanly restart a partially-streamed answer
                    raise
                from omni.core.llm.errors import classify_llm_exception

                info = classify_llm_exception(exc)
                if not info.fallback_allowed and info.category != "unknown":
                    raise
                continue
        if last_exc is not None:
            raise last_exc


class RetryingLLMClient(LLMClient):
    """Wrap one client with transient-error retry (backoff + jitter + Retry-After).

    Applied to *every* configured provider — independent of whether a fallback is
    set — so a single 429 / 5xx / transport blip does not abandon a long research
    turn. Non-transient errors (auth, invalid transcript, bad request) are not
    retried. A stream that has already emitted output is never retried, because a
    partially rendered answer cannot be cleanly restarted.
    """

    def __init__(self, inner: LLMClient, *, policy: RetryPolicy | None = None) -> None:
        self._inner = inner
        self._policy = policy or RetryPolicy()
        self.model = inner.model

    def __getattr__(self, name: str) -> Any:
        # Transparent decorator: proxy provider-specific attributes (embedding
        # config, health-check internals, etc.) to the wrapped client. Defined
        # methods and ``model`` / ``_inner`` / ``_policy`` resolve normally and
        # never reach here; guard ``_inner`` to avoid recursion before __init__.
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)

    async def _retry(self, method: str, *args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1 + self._policy.max_retries):
            try:
                return await getattr(self._inner, method)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — classify, then retry or re-raise
                last_exc = exc
                if attempt < self._policy.max_retries and _is_retryable(exc):
                    delay = _retry_delay(self._policy, attempt, exc)
                    logger.warning(
                        "[llm] %s failed (attempt %d/%d); retrying in %.2fs",
                        method, attempt + 1, self._policy.max_retries + 1, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> ChatWithToolsResult:
        return await self._retry(
            "chat_with_tools", messages, tools,
            tool_choice=tool_choice, temperature=temperature, max_tokens=max_tokens,
        )

    async def chat(self, system: str, user: str, *, temperature: float = 0.3) -> str:
        return await self._retry("chat", system, user, temperature=temperature)

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
        if on_delta is None:
            return await self._retry(
                "chat_with_tools_stream", messages, tools,
                tool_choice=tool_choice, temperature=temperature, max_tokens=max_tokens,
                on_delta=None,
            )
        # Guard against re-emitting a partially streamed answer on retry.
        state = {"started": False}

        def guarded(piece: str) -> Any:
            state["started"] = True
            return on_delta(piece)

        last_exc: Exception | None = None
        for attempt in range(1 + self._policy.max_retries):
            try:
                return await self._inner.chat_with_tools_stream(
                    messages, tools,
                    tool_choice=tool_choice, temperature=temperature, max_tokens=max_tokens,
                    on_delta=guarded,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if state["started"]:
                    raise  # cannot cleanly restart a partially-streamed answer
                if attempt < self._policy.max_retries and _is_retryable(exc):
                    await asyncio.sleep(_retry_delay(self._policy, attempt, exc))
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._retry("embed", texts)

    async def chat_stream(
        self, system: str, user: str, *, temperature: float = 0.3
    ) -> AsyncIterator[str]:
        attempt = 0
        while True:
            started = False
            try:
                async for chunk in self._inner.chat_stream(system, user, temperature=temperature):
                    started = True
                    yield chunk
                return
            except Exception as exc:  # noqa: BLE001
                if not started and attempt < self._policy.max_retries and _is_retryable(exc):
                    await asyncio.sleep(_retry_delay(self._policy, attempt, exc))
                    attempt += 1
                    continue
                raise


def _make_provider(
    provider: str,
    *,
    base_url: str,
    api_key: str,
    model_name: str,
    timeout_s: float,
    emb_on: bool,
    mem: Any,
) -> LLMClient:
    """Build one concrete provider from resolved connection params."""
    from omni.core.llm.providers import MockProvider, OpenAICompatibleProvider

    p = (provider or "mock").lower()
    if p in ("mock", "", "offline"):
        return MockProvider(model_name=model_name or "omni-mock", embeddings_enabled=emb_on)
    if p in ("openai_compatible", "openai", "deepseek", "ollama", "anthropic"):
        # Remote/OpenAI-compatible clients require an explicit embedding base.
        # Never guess that a chat endpoint also supports /embeddings: known
        # chat-only services (for example DeepSeek) would otherwise 404.
        emb_ready = bool(emb_on and mem.embedding_base_url)
        return OpenAICompatibleProvider(
            base_url=base_url,
            api_key=api_key,
            model=model_name,
            timeout_s=timeout_s,
            embedding_base_url=mem.embedding_base_url if emb_ready else "",
            embedding_api_key=(mem.embedding_api_key or api_key) if emb_ready else "",
            embedding_model=mem.embedding_model if emb_ready else "",
        )
    # Unknown provider → safe fallback to mock so the CLI still runs.
    return MockProvider(model_name=model_name or "omni-mock", embeddings_enabled=emb_on)


def create_llm_client(settings: Any) -> LLMClient:
    """Factory: build the client for the active model config.

    Every client is wrapped so a transient 429/5xx/transport error is retried
    (backoff + jitter + ``Retry-After``) rather than abandoning the turn — this
    holds *whether or not* a fallback is configured. When
    ``model.fallback_provider`` is set the primary is additionally wrapped in a
    :class:`FallbackLLMClient` so a hard primary outage degrades to the fallback
    provider; otherwise the primary is wrapped in :class:`RetryingLLMClient`.
    """
    model = settings.model
    mem = settings.memory
    # ``embeddings_enabled`` is the master switch. Endpoint/provider fields may
    # remain stored while disabled so users can re-enable later, but they must
    # never override an explicit opt-out or trigger a capability probe.
    emb_on = bool(mem.embeddings_enabled)

    primary = _make_provider(
        model.provider,
        base_url=model.base_url,
        api_key=model.api_key,
        model_name=model.model,
        timeout_s=model.request_timeout_s,
        emb_on=emb_on,
        mem=mem,
    )
    if not (model.fallback_provider or "").strip():
        return RetryingLLMClient(primary)
    fallback = _make_provider(
        model.fallback_provider,
        base_url=model.fallback_base_url or model.base_url,
        api_key=model.fallback_api_key or model.api_key,
        model_name=model.fallback_model or model.model,
        timeout_s=model.request_timeout_s,
        emb_on=emb_on,
        mem=mem,
    )
    return FallbackLLMClient(primary, [fallback])


async def check_connectivity(settings: Any, *, timeout_s: float = 20.0) -> tuple[bool, str]:
    """Best-effort live check of the configured model. Returns (ok, detail)."""
    model = settings.model
    provider = (model.provider or "mock").lower()
    if provider in ("mock", "", "offline"):
        return True, "mock model (offline; no network required)"
    if not model.base_url:
        return False, "model.base_url is not configured"
    client = create_llm_client(settings)
    try:
        reply = await client.chat("You are a health check.", "ping", temperature=0.0)
    except Exception as exc:  # noqa: BLE001 - normalize every provider failure for users.
        from omni.core.llm.errors import classify_llm_exception

        return False, classify_llm_exception(exc).user_message
    snippet = (reply or "").strip().replace("\n", " ")[:60]
    return True, f"{model.provider}/{model.model} is available; response: {snippet or '(empty)'}"
