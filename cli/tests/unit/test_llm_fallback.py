"""P1-D′c: model fallback chain — retry then fail over across providers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

from omni.core.llm.client import (
    ChatWithToolsResult,
    FallbackLLMClient,
    LLMClient,
    RetryingLLMClient,
    RetryPolicy,
    _retry_after_seconds,
    create_llm_client,
)


class _Boom(LLMClient):
    """A client that fails a fixed number of times, then succeeds."""

    def __init__(self, *, model: str, fail_times: int, exc: Exception, tag: str) -> None:
        self.model = model
        self._fail_times = fail_times
        self._exc = exc
        self._tag = tag
        self.calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatWithToolsResult:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return ChatWithToolsResult(content=self._tag)

    async def chat(self, system: str, user: str, **kwargs: Any) -> str:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return self._tag


def _http_500() -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://x/chat/completions")
    resp = httpx.Response(503, request=req)
    return httpx.HTTPStatusError("server error", request=req, response=resp)


@pytest.mark.asyncio
async def test_retryable_error_retries_same_provider_then_succeeds():
    primary = _Boom(model="p", fail_times=1, exc=_http_500(), tag="primary-ok")
    fb = _Boom(model="f", fail_times=0, exc=ValueError(), tag="fallback")
    client = FallbackLLMClient(primary, [fb], max_retries=1, base_delay=0.0)

    res = await client.chat_with_tools([{"role": "user", "content": "hi"}], [])
    assert res.content == "primary-ok"  # recovered on retry, never hit fallback
    assert primary.calls == 2 and fb.calls == 0


@pytest.mark.asyncio
async def test_generic_bad_request_can_use_explicit_fallback():
    req = httpx.Request("POST", "http://x")
    bad = httpx.HTTPStatusError("bad", request=req, response=httpx.Response(400, request=req))
    primary = _Boom(model="p", fail_times=99, exc=bad, tag="never")
    fb = _Boom(model="f", fail_times=0, exc=ValueError(), tag="fallback-ok")
    client = FallbackLLMClient(primary, [fb], max_retries=2, base_delay=0.0)

    result = await client.chat_with_tools([{"role": "user", "content": "hi"}], [])
    assert result.content == "fallback-ok"
    assert primary.calls == 1
    assert fb.calls == 1


@pytest.mark.asyncio
async def test_transcript_invalid_request_does_not_replay_to_fallback():
    req = httpx.Request("POST", "http://x")
    bad = httpx.HTTPStatusError(
        "bad",
        request=req,
        response=httpx.Response(
            400,
            request=req,
            json={"error": {"message": "No tool output found for function call c1"}},
        ),
    )
    primary = _Boom(model="p", fail_times=99, exc=bad, tag="never")
    fb = _Boom(model="f", fail_times=0, exc=ValueError(), tag="fallback-ok")
    client = FallbackLLMClient(primary, [fb], max_retries=1, base_delay=0.0)

    with pytest.raises(httpx.HTTPStatusError):
        await client.chat_with_tools([{"role": "user", "content": "hi"}], [])
    assert primary.calls == 1
    assert fb.calls == 0


@pytest.mark.asyncio
async def test_all_providers_exhausted_raises_last_error():
    primary = _Boom(model="p", fail_times=99, exc=_http_500(), tag="x")
    fb = _Boom(model="f", fail_times=99, exc=ValueError("fb-dead"), tag="y")
    client = FallbackLLMClient(primary, [fb], max_retries=1, base_delay=0.0)

    with pytest.raises(ValueError, match="fb-dead"):
        await client.chat("s", "u")


def test_factory_always_retry_wraps_and_adds_fallback_when_configured(settings):
    # No fallback → primary is still retry-wrapped (retry is decoupled from
    # fallback), so a transient blip no longer abandons the turn.
    settings.model.provider = "mock"
    plain = create_llm_client(settings)
    assert isinstance(plain, RetryingLLMClient)
    assert not isinstance(plain, FallbackLLMClient)

    # Fallback configured → wrapped in the fallback chain.
    settings.model.provider = "openai"
    settings.model.base_url = "http://primary/v1"
    settings.model.api_key = "k"
    settings.model.model = "gpt-x"
    settings.model.fallback_provider = "mock"
    wrapped = create_llm_client(settings)
    assert isinstance(wrapped, FallbackLLMClient)
    assert wrapped.model == "gpt-x"


@pytest.mark.asyncio
async def test_retry_without_fallback_recovers_from_transient_error():
    # The whole point of P0.1: retry works even with NO fallback provider.
    primary = _Boom(model="p", fail_times=2, exc=_http_500(), tag="ok-after-retry")
    client = RetryingLLMClient(primary, policy=RetryPolicy(max_retries=2, base_delay=0.0, jitter=0.0))

    res = await client.chat_with_tools([{"role": "user", "content": "hi"}], [])
    assert res.content == "ok-after-retry"
    assert primary.calls == 3  # 1 initial + 2 retries


@pytest.mark.asyncio
async def test_retry_gives_up_and_raises_after_budget():
    primary = _Boom(model="p", fail_times=99, exc=_http_500(), tag="never")
    client = RetryingLLMClient(primary, policy=RetryPolicy(max_retries=1, base_delay=0.0, jitter=0.0))

    with pytest.raises(httpx.HTTPStatusError):
        await client.chat_with_tools([{"role": "user", "content": "hi"}], [])
    assert primary.calls == 2  # 1 initial + 1 retry, then re-raised


@pytest.mark.asyncio
async def test_retry_does_not_retry_non_transient_error():
    req = httpx.Request("POST", "http://x")
    bad = httpx.HTTPStatusError("bad", request=req, response=httpx.Response(400, request=req))
    primary = _Boom(model="p", fail_times=99, exc=bad, tag="never")
    client = RetryingLLMClient(primary, policy=RetryPolicy(max_retries=3, base_delay=0.0))

    with pytest.raises(httpx.HTTPStatusError):
        await client.chat_with_tools([{"role": "user", "content": "hi"}], [])
    assert primary.calls == 1  # 400 is not transient → no retry


def test_retry_after_header_numeric_and_absent():
    req = httpx.Request("POST", "http://x/chat/completions")
    with_hdr = httpx.HTTPStatusError(
        "rate", request=req,
        response=httpx.Response(429, request=req, headers={"Retry-After": "2"}),
    )
    assert _retry_after_seconds(with_hdr) == 2.0

    no_hdr = httpx.HTTPStatusError(
        "server", request=req, response=httpx.Response(503, request=req)
    )
    assert _retry_after_seconds(no_hdr) is None


def test_provider_records_arguments_parse_error_instead_of_silent_empty():
    # P0.2: a tool-call whose arguments are not valid JSON must carry the parse
    # error so the ReAct loop can surface it, not silently degrade to ``{}``.
    from omni.core.llm.providers import _finalize_tool_calls

    frags = {0: {"id": "c1", "name": "echo", "arguments": "{not json"}}
    calls = _finalize_tool_calls(frags)
    assert len(calls) == 1
    assert calls[0].arguments == {}
    assert calls[0].arguments_error is not None
    assert calls[0].raw_arguments == "{not json"
    assert calls[0].to_message_fragment()["function"]["arguments"] == "{not json"

    ok = _finalize_tool_calls({0: {"id": "c2", "name": "echo", "arguments": '{"x": 1}'}})
    assert ok[0].arguments == {"x": 1}
    assert ok[0].arguments_error is None
    assert ok[0].arguments_repaired is False


def test_provider_bounded_repair_accepts_one_object_with_non_json_trailing_text(
    caplog,
):
    from omni.core.llm.providers import _finalize_tool_calls

    raw = '{"goal":"总结科研","cron":"0 18 * * *"} trailing explanation'
    call = _finalize_tool_calls(
        {0: {"id": "c1", "name": "schedule_task", "arguments": raw}}
    )[0]

    assert call.arguments == {"goal": "总结科研", "cron": "0 18 * * *"}
    assert call.arguments_error is None
    assert call.arguments_repaired is True
    assert call.raw_arguments == raw
    assert call.to_message_fragment()["function"]["arguments"] == json.dumps(
        call.arguments,
        ensure_ascii=False,
    )
    assert hashlib.sha256(raw.encode()).hexdigest()[:12] in caplog.text
    assert "总结科研" not in caplog.text


@pytest.mark.parametrize(
    "raw",
    [
        '{"x":1}{"x":2}',
        '{"x":1} true',
        '{"x":1} 2',
        '{"x":1} "second"',
        '{"x":1} tru',
        '{"x":1} "second',
        '{"x":1} -',
    ],
)
def test_provider_bounded_repair_rejects_multiple_json_values(raw):
    from omni.core.llm.providers import _finalize_tool_calls

    call = _finalize_tool_calls({0: {"id": "c1", "name": "echo", "arguments": raw}})[0]

    assert call.arguments == {}
    assert call.arguments_error is not None
    assert call.arguments_repaired is False
    assert call.raw_arguments == raw


class _BlankToolNameClient:
    """Returns one usable tool call plus one with a blank function name."""

    def __init__(self, **_kwargs) -> None:  # noqa: ANN003
        pass

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args) -> None:  # noqa: ANN002
        return None

    async def post(self, url: str, **_kwargs) -> httpx.Response:  # noqa: ANN003
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {"id": "c1", "function": {"name": "", "arguments": "{}"}},
                                {"id": "c2", "function": {"name": "echo", "arguments": '{"x":1}'}},
                            ],
                        }
                    }
                ]
            },
        )


@pytest.mark.asyncio
async def test_blank_tool_name_is_dropped_on_both_streaming_and_non_streaming_paths(
    monkeypatch,
):
    """A blank function name is malformed transport and never reaches the loop.

    The streaming and non-streaming providers must agree: a nameless call has no
    identity to report back, so relaying it would render as ``?`` and burn an
    iteration on an ``unknown tool ''`` the model cannot correct. Prompt
    sub-agents always take the non-streaming path (no token sink), which is
    exactly where the asymmetry used to let these through.
    """
    from omni.core.llm import providers
    from omni.core.llm.providers import OpenAICompatibleProvider, _finalize_tool_calls

    streamed = _finalize_tool_calls({
        0: {"id": "c1", "name": "", "arguments": "{}"},
        1: {"id": "c2", "name": "echo", "arguments": '{"x":1}'},
    })
    assert [c.name for c in streamed] == ["echo"]

    monkeypatch.setattr(providers.httpx, "AsyncClient", _BlankToolNameClient)
    provider = OpenAICompatibleProvider(
        base_url="https://models.invalid/v1",
        api_key="k",
        model="test-model",
    )
    result = await provider.chat_with_tools([{"role": "user", "content": "hi"}], [])

    assert [c.name for c in result.tool_calls] == ["echo"]
