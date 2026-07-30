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
    LLMOutputTruncated,
    LLMProviderError,
    classify_llm_exception,
    from_http_status_error,
)
from omni.core.llm.idle import ActivityHook, emit_activity
from omni.core.llm.native_tool_markup import (
    NativeMarkupSplit,
    NativeToolMarkupFilter,
    split_native_tool_markup,
)
from omni.core.model_catalog import max_output_tokens_for
from omni.core.tool_transcript import normalize_tool_transcript

logger = logging.getLogger(__name__)
_DISABLE_EMBEDDING_STATUSES = {400, 401, 403, 404, 405, 410, 422, 501}
_MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
_MAX_TOOL_ARGUMENT_TRAILING_CHARS = 256


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


def _could_start_json_value(value: str) -> bool:
    if not value:
        return False
    if value[0] in '"-0123456789':
        return True
    word = ""
    for char in value:
        if not char.isalpha():
            break
        word += char.lower()
    return bool(word) and any(literal.startswith(word) for literal in ("true", "false", "null"))


def _parse_tool_arguments(raw: Any) -> tuple[dict[str, Any], str | None, bool, str]:
    """Parse one model tool object, with bounded syntax-only tail recovery."""
    if isinstance(raw, dict):
        return dict(raw), None, False, json.dumps(raw, ensure_ascii=False)
    text = str(raw or "{}")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        if len(text.encode("utf-8")) > _MAX_TOOL_ARGUMENT_BYTES:
            return {}, f"{exc}; arguments exceed repair limit", False, text
        stripped = text.strip()
        if not stripped.startswith("{"):
            return {}, str(exc), False, text
        try:
            value, end = json.JSONDecoder().raw_decode(stripped)
        except json.JSONDecodeError:
            return {}, str(exc), False, text
        trailing = stripped[end:].strip()
        try:
            json.JSONDecoder().raw_decode(trailing)
            trailing_starts_json = True
        except json.JSONDecodeError:
            trailing_starts_json = False
        if (
            not isinstance(value, dict)
            or not trailing
            or len(trailing) > _MAX_TOOL_ARGUMENT_TRAILING_CHARS
            or any(char in trailing for char in "{}[]")
            or trailing.startswith((",", ":"))
            or trailing_starts_json
            or _could_start_json_value(trailing)
        ):
            return {}, str(exc), False, text
        return dict(value), None, True, text
    if not isinstance(value, dict):
        return {}, "tool arguments must be a JSON object", False, text
    return dict(value), None, False, text


def _tool_call_from_raw(
    call_id: str, name: str, raw: Any, *, truncated: bool = False
) -> ToolCall | None:
    """Admit one model tool call, or ``None`` when it is structurally unusable.

    A blank function name is *malformed transport*, not a tool the model chose:
    there is no name to report back, so relaying it as ``unknown tool ''`` gives
    the model nothing to correct and burns an iteration. Both the streaming and
    non-streaming providers route through here so the admission rule cannot
    drift between them.
    """
    if not str(name or "").strip():
        logger.warning("[llm] dropped malformed tool call with blank name id=%s", call_id or "-")
        return None
    args, arg_err, repaired, raw_text = _parse_tool_arguments(raw)
    if repaired:
        digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12]
        logger.warning(
            "[llm] repaired trailing tool-argument text tool=%s raw_length=%d raw_sha256=%s",
            name,
            len(raw_text),
            digest,
        )
    return ToolCall(
        id=call_id,
        name=name,
        arguments=args,
        arguments_error=arg_err,
        raw_arguments=raw_text,
        arguments_repaired=repaired,
        arguments_truncated=bool(truncated and arg_err),
    )


def _merge_native_markup(
    split: NativeMarkupSplit,
    calls: list[ToolCall],
    malformed: int,
    *,
    model: str,
) -> tuple[str, list[ToolCall], int]:
    """Fold a tool call encoded into *content* back into the structured result.

    This is the boundary that owns provider wire format, so it is the only layer
    that may know a provider's native encoding exists: everything above consumes
    :class:`ChatWithToolsResult` and would need the same rule duplicated in the
    loop, the recorder, the notebook and the TUI to keep the markup out of all of
    them.

    A structured ``tool_calls`` field stays authoritative — markup that arrives
    beside it is a duplicate of a call already delivered properly, so it is only
    dropped. Markup that could not be read back counts as a malformed call rather
    than as silence, so the loop re-nudges the model instead of finishing the turn
    on the empty string that stripping left behind.
    """
    if not split.stripped:
        return split.content, calls, malformed
    recovered: list[ToolCall] = []
    if not calls:
        for position, candidate in enumerate(split.recovered, start=1):
            admitted = _tool_call_from_raw(
                f"omni_recovered_call_{position}", candidate.name, candidate.arguments
            )
            if admitted is not None:
                recovered.append(admitted)
    logger.warning(
        "[llm] provider-native tool-call markup arrived as assistant content "
        "model=%s structured=%d recovered=%d",
        model, len(calls), len(recovered),
    )
    if not calls and not recovered:
        malformed += 1
    return split.content, [*calls, *recovered], malformed


def _strip_native_markup(text: str, *, model: str) -> str:
    """Drop native tool markup from a plain-text answer, recovering nothing.

    A ``chat`` turn offers no tools, so a call encoded into content cannot be
    honoured here — but the text still becomes a summary, a review or a final
    answer, so the sentinels must not survive into it.
    """
    split = split_native_tool_markup(text)
    if split.stripped:
        logger.warning(
            "[llm] stripped provider-native tool-call markup from a text answer model=%s", model
        )
    return split.content


def _finalize_tool_calls(
    frags: dict[int, dict[str, Any]], *, truncated: bool = False
) -> list[ToolCall]:
    """Turn accumulated tool-call fragments into :class:`ToolCall`s (parsing args).

    Only the last call in a response can be the one the output cap cut off; an
    earlier call in the same batch was written and closed before the cap was
    reached, so blaming truncation for its parse failure would misdirect the fix.
    """
    calls: list[ToolCall] = []
    ordered = sorted(frags.items())
    for position, (_idx, slot) in enumerate(ordered):
        call = _tool_call_from_raw(
            str(slot.get("id", "")),
            str(slot.get("name") or ""),
            slot.get("arguments") or "{}",
            truncated=truncated and position == len(ordered) - 1,
        )
        if call is not None:
            calls.append(call)
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
                "Configure a real provider, for example `/config set model.provider openai`; "
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
        timeout_s: float | httpx.Timeout = 120.0,
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
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        finish_reason = str(choice.get("finish_reason") or "")
        raw_calls = msg.get("tool_calls") or []
        calls: list[ToolCall] = []
        for position, tc in enumerate(raw_calls):
            fn = tc.get("function", {})
            call = _tool_call_from_raw(
                str(tc.get("id", "")),
                str(fn.get("name", "")),
                fn.get("arguments") or "{}",
                truncated=finish_reason == "length" and position == len(raw_calls) - 1,
            )
            if call is not None:
                calls.append(call)
        content, calls, malformed = _merge_native_markup(
            split_native_tool_markup(msg.get("content") or ""),
            calls,
            len(raw_calls) - len(calls),
            model=self.model,
        )
        return ChatWithToolsResult(
            content=content,
            tool_calls=calls,
            reasoning_content=msg.get("reasoning_content") or "",
            usage=data.get("usage") or {},
            malformed_tool_calls=malformed,
            finish_reason=finish_reason,
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
        on_activity: ActivityHook | None = None,
    ) -> ChatWithToolsResult:
        """Native SSE streaming: forward content deltas to ``on_delta`` while
        accumulating tool-call fragments and usage. Any streaming/transport
        failure degrades to the non-streaming chunked fallback (so a flaky SSE
        never breaks a turn). ``on_activity`` ticks on every ``data:`` line so
        an idle watchdog sees reasoning and tool-call fragments, not only
        visible answer text."""
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
        finish_reason = ""
        # A sentinel can straddle two deltas, so deltas are filtered before they
        # are forwarded: what ``on_delta`` never receives cannot be rendered and
        # then have to be un-rendered.
        markup_filter = NativeToolMarkupFilter()
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
                        await emit_activity(on_activity)
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
                        if choices[0].get("finish_reason"):
                            finish_reason = str(choices[0]["finish_reason"])
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            showable = markup_filter.push(piece)
                            if showable:
                                content_parts.append(showable)
                                if on_delta is not None:
                                    await _emit_delta(on_delta, showable)
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
                on_activity=on_activity,
            )
        tail = markup_filter.finish()
        if tail:
            content_parts.append(tail)
            if on_delta is not None:
                await _emit_delta(on_delta, tail)
        admitted = _finalize_tool_calls(tool_frags, truncated=finish_reason == "length")
        content, calls, malformed = _merge_native_markup(
            markup_filter.split("".join(content_parts)),
            admitted,
            len(tool_frags) - len(admitted),
            model=self.model,
        )
        return ChatWithToolsResult(
            content=content,
            tool_calls=calls,
            malformed_tool_calls=malformed,
            reasoning_content="".join(reasoning_parts),
            usage=usage,
            finish_reason=finish_reason,
        )

    def _text_payload(self, system: str, user: str, temperature: float) -> dict[str, Any]:
        """One request body for both text paths, sized by the model's own cap.

        Omitting ``max_tokens`` does not mean "no limit" — it means the
        provider's default, which is invisible from here and is what quietly cut
        a long answer in half. Asking for the catalog value both raises the
        ceiling to what the model actually allows and makes the resulting
        ``finish_reason`` mean something we chose.
        """
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_output_tokens_for(self.model),
        }

    async def chat_result(
        self, system: str, user: str, *, temperature: float = 0.3
    ) -> ChatWithToolsResult:
        self._ensure_chat_available()
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._base}/chat/completions",
                json=self._text_payload(system, user, temperature),
                headers=self._headers(),
            )
            self._check_chat_status(resp)
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        raw = choice.get("message", {}).get("content") or ""
        finish_reason = str(choice.get("finish_reason") or "")
        if finish_reason == "length":
            logger.warning(
                "[llm] text answer stopped at the output cap model=%s chars=%d",
                self.model, len(raw),
            )
        return ChatWithToolsResult(
            content=_strip_native_markup(raw, model=self.model),
            usage=data.get("usage") or {},
            finish_reason=finish_reason,
        )

    async def chat(self, system: str, user: str, *, temperature: float = 0.3) -> str:
        return (await self.chat_result(system, user, temperature=temperature)).content

    async def chat_stream(
        self, system: str, user: str, *, temperature: float = 0.3
    ) -> AsyncIterator[str]:
        """Stream a tool-free answer, raising once it turns out to be a fragment.

        A generator hands its consumer every delta and then ends; there is no
        later value to carry a finish reason in. So the deltas are delivered
        first — what arrived is real and worth showing — and the cap is reported
        by raising afterwards, which is the only way an ``async for`` can learn
        the answer it just consumed was cut off rather than finished.
        """
        self._ensure_chat_available()
        payload = {**self._text_payload(system, user, temperature), "stream": True}
        markup_filter = NativeToolMarkupFilter()
        finish_reason = ""
        produced = 0
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
                        choice = json.loads(chunk)["choices"][0]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        showable = markup_filter.push(delta)
                        if showable:
                            produced += len(showable)
                            yield showable
        tail = markup_filter.finish()
        if tail:
            produced += len(tail)
            yield tail
        if finish_reason == "length":
            logger.warning(
                "[llm] streamed answer stopped at the output cap model=%s chars=%d",
                self.model, produced,
            )
            raise LLMOutputTruncated(model=self.model, produced_chars=produced)

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
