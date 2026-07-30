"""The transport must report an output-cap cut as its own, not as model error.

Incident 599a725b: a ``write_file`` carrying a whole paper exceeded the output
token cap, so the response stopped mid-string. ``finish_reason`` was read
nowhere in the codebase, so the half-written arguments surfaced as "the model
sent invalid JSON" and the model was asked to re-send them — which produced the
same oversized call, cut at the same place, until the run's token budget ran out.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omni.core.llm.providers import OpenAICompatibleProvider

_CUT_ARGUMENTS = '{"path": "papers/rag_survey.md", "contents": "# RAG survey\\n\\nIntro'


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch):
    """An OpenAI-compatible provider whose HTTP calls are served by a handler."""

    def build(handler) -> OpenAICompatibleProvider:  # noqa: ANN001
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)
        return OpenAICompatibleProvider(
            base_url="https://example.invalid/v1", api_key="k", model="test-model"
        )

    return build


def _json_handler(payload: dict[str, Any]):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


@pytest.mark.asyncio
async def test_non_streaming_marks_a_length_capped_tool_call_as_truncated(provider) -> None:  # noqa: ANN001
    client = provider(_json_handler({
        "choices": [{
            "finish_reason": "length",
            "message": {"tool_calls": [{
                "id": "c1",
                "function": {"name": "write_file", "arguments": _CUT_ARGUMENTS},
            }]},
        }],
        "usage": {"total_tokens": 4096},
    }))

    result = await client.chat_with_tools([{"role": "user", "content": "write"}], [])

    assert result.finish_reason == "length"
    assert result.truncated_by_output_cap is True
    call = result.tool_calls[0]
    assert call.arguments_error is not None
    assert call.arguments_truncated is True


@pytest.mark.asyncio
async def test_streaming_marks_a_length_capped_tool_call_as_truncated(provider) -> None:  # noqa: ANN001
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "c1",
            "function": {"name": "write_file", "arguments": _CUT_ARGUMENTS},
        }]}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = provider(handler)
    result = await client.chat_with_tools_stream([{"role": "user", "content": "write"}], [])

    assert result.finish_reason == "length"
    assert result.tool_calls[0].arguments_truncated is True


@pytest.mark.asyncio
async def test_a_normally_finished_call_with_bad_json_is_not_called_truncated(provider) -> None:  # noqa: ANN001
    """Only the cap makes a call truncated. Genuine malformed JSON keeps its own
    diagnosis, because the two failures need opposite instructions: one says
    re-send it correctly, the other says never send it this way again."""
    client = provider(_json_handler({
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {"tool_calls": [{
                "id": "c1",
                "function": {"name": "write_file", "arguments": "{not json at all"},
            }]},
        }],
    }))

    result = await client.chat_with_tools([{"role": "user", "content": "write"}], [])

    assert result.truncated_by_output_cap is False
    call = result.tool_calls[0]
    assert call.arguments_error is not None
    assert call.arguments_truncated is False


@pytest.mark.asyncio
async def test_only_the_last_call_in_a_capped_response_is_the_truncated_one(provider) -> None:  # noqa: ANN001
    """An earlier call in the same batch closed before the cap was reached, so
    its parse failure is the model's and must not be excused as truncation."""
    client = provider(_json_handler({
        "choices": [{
            "finish_reason": "length",
            "message": {"tool_calls": [
                {"id": "c1", "function": {"name": "read_file", "arguments": "{bad"}},
                {"id": "c2", "function": {"name": "write_file", "arguments": _CUT_ARGUMENTS}},
            ]},
        }],
    }))

    result = await client.chat_with_tools([{"role": "user", "content": "go"}], [])

    first, last = result.tool_calls
    assert first.arguments_truncated is False
    assert last.arguments_truncated is True
