"""Host-side LLM usage accumulation that portable skills do not have to know about.

Engine adapters speak the public ``LLMClient`` surface and drop provider ``usage``
on the floor (a portable OpenAI-shaped port only needs ``choices``). The host
wraps ``ctx.llm`` for the duration of one python-engine run so every real
``chat`` / ``chat_result`` / ``chat_with_tools`` call is counted, then the
executor writes a single ``cost.usage`` event. Skills stay free of CLI internals.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from omni.core.llm.client import ChatWithToolsResult, LLMClient

_CHARS_PER_TOKEN = 4
UsageProgress = Callable[[dict[str, Any]], Awaitable[None] | None]


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _usage_counts(usage: dict[str, Any] | None) -> tuple[int, int, int]:
    payload = usage if isinstance(usage, dict) else {}
    prompt = int(payload.get("prompt_tokens") or 0)
    completion = int(payload.get("completion_tokens") or 0)
    total = int(payload.get("total_tokens") or 0)
    if total <= 0:
        total = prompt + completion
    return prompt, completion, total


@dataclass
class UsageMeter:
    """Accumulate provider usage (or a char-based estimate) across one engine run."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    estimated: bool = False
    _text_in: list[str] = field(default_factory=list)
    _text_out: list[str] = field(default_factory=list)

    def add(
        self,
        usage: dict[str, Any] | None,
        *,
        text_in: str = "",
        text_out: str = "",
    ) -> None:
        self.calls += 1
        prompt, completion, total = _usage_counts(usage)
        if prompt or completion or total:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.total_tokens += total
            return
        self.estimated = True
        if text_in:
            self._text_in.append(text_in)
        if text_out:
            self._text_out.append(text_out)

    def as_usage(self) -> dict[str, int]:
        """Return OpenAI-shaped counts, adding char estimates for unmetered calls."""
        extra_prompt = sum(_estimate_tokens(text) for text in self._text_in)
        extra_completion = sum(_estimate_tokens(text) for text in self._text_out)
        reported = self.total_tokens or (self.prompt_tokens + self.completion_tokens)
        return {
            "prompt_tokens": self.prompt_tokens + extra_prompt,
            "completion_tokens": self.completion_tokens + extra_completion,
            "total_tokens": reported + extra_prompt + extra_completion,
        }


def _messages_text(messages: list[dict[str, Any]] | None) -> str:
    """Flatten chat messages for a char-based estimate when the provider omits usage."""
    parts: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = str(message.get("content") or "")
        if content:
            parts.append(content)
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(function.get("name") or call.get("name") or "")
            arguments = str(function.get("arguments") or "")
            if name or arguments:
                parts.append(f"{name} {arguments}".strip())
    return "\n".join(parts)


class UsageTrackingLLM(LLMClient):
    """Transparent decorator that notes usage on every successful model call."""

    def __init__(
        self,
        inner: Any,
        meter: UsageMeter,
        *,
        on_usage: UsageProgress | None = None,
    ) -> None:
        self._inner = inner
        self._meter = meter
        self._on_usage = on_usage
        self.model = getattr(inner, "model", "") or ""

    def __getattr__(self, name: str) -> Any:
        if name in {"_inner", "_meter", "_on_usage"}:
            raise AttributeError(name)
        return getattr(self._inner, name)

    def _note(self, result: Any, *, text_in: str = "", text_out: str = "") -> Any:
        usage = getattr(result, "usage", None)
        content = text_out or str(getattr(result, "content", "") or "")
        self._meter.add(usage if isinstance(usage, dict) else None, text_in=text_in, text_out=content)
        return self._emit_usage()

    def _emit_usage(self) -> Any:
        callback = self._on_usage
        if callback is None:
            return None
        snapshot = {
            **self._meter.as_usage(),
            "calls": self._meter.calls,
            "estimated": self._meter.estimated,
        }
        try:
            return callback(snapshot)
        except Exception:  # noqa: BLE001 - live metering must never fail a model call
            return None

    async def _await_usage(self, pending: Any) -> None:
        if inspect.isawaitable(pending):
            try:
                await pending
            except Exception:  # noqa: BLE001
                return

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> ChatWithToolsResult:
        result = await self._inner.chat_with_tools(
            messages,
            tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        await self._await_usage(self._note(result, text_in=_messages_text(messages)))
        return result

    async def chat_result(
        self, system: str, user: str, *, temperature: float = 0.3
    ) -> ChatWithToolsResult:
        result = await self._inner.chat_result(system, user, temperature=temperature)
        await self._await_usage(self._note(result, text_in=f"{system}\n{user}"))
        return result

    async def chat(self, system: str, user: str, *, temperature: float = 0.3, **kwargs: Any) -> str:
        # Prefer ``chat_result`` so a text-only skill path (research-pptx) still
        # yields provider usage. Fall back to ``chat`` for doubles that only
        # implement the string surface.
        inner_result = getattr(self._inner, "chat_result", None)
        if callable(inner_result):
            try:
                result = await inner_result(system, user, temperature=temperature)
            except TypeError:
                result = None
            else:
                await self._await_usage(self._note(result, text_in=f"{system}\n{user}"))
                return str(getattr(result, "content", "") or "")
        text = await self._inner.chat(system, user, temperature=temperature, **kwargs)
        self._meter.add(None, text_in=f"{system}\n{user}", text_out=str(text or ""))
        await self._await_usage(self._emit_usage())
        return text

    async def chat_with_tools_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
        on_delta: Any = None,
        on_activity: Any = None,
    ) -> ChatWithToolsResult:
        method = getattr(self._inner, "chat_with_tools_stream", None)
        if callable(method):
            result = await method(
                messages,
                tools,
                tool_choice=tool_choice,
                temperature=temperature,
                max_tokens=max_tokens,
                on_delta=on_delta,
                on_activity=on_activity,
            )
        else:
            result = await self._inner.chat_with_tools(
                messages,
                tools,
                tool_choice=tool_choice,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        await self._await_usage(self._note(result, text_in=_messages_text(messages)))
        return result


__all__ = ["UsageMeter", "UsageTrackingLLM"]
