"""A provider's own tool-call encoding must never be read as an answer.

Incident c60c4c85: on the tool-free finalisation turn DeepSeek still wanted to
call ``update_plan``, and with no structured ``tool_calls`` field to put it in it
wrote the call into assistant *content* using its native sentinels. Nothing
between the provider and the screen knew that vocabulary, so the whole block —
``<｜｜DSML｜｜tool_calls>`` and all — was persisted as the final answer and
rendered to the user as prose.

The sentinels below use FULL-WIDTH VERTICAL LINE (U+FF5C) and LOWER ONE EIGHTH
BLOCK (U+2581); the ASCII-pipe spellings that appear in the negative case are
deliberately different characters.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omni.core.llm.providers import OpenAICompatibleProvider

# Verbatim assistant content recorded for task c60c4c85, shortened to two steps.
_INCIDENT_CONTENT = (
    '<｜｜DSML｜｜tool_calls>\n'
    '<｜｜DSML｜｜invoke name="update_plan">\n'
    '<｜｜DSML｜｜parameter name="plan" string="false">'
    '[{"status":"completed","step":"review commit 1"},'
    '{"status":"in_progress","step":"summarise findings"}]'
    '</｜｜DSML｜｜parameter>\n'
    '</｜｜DSML｜｜invoke>\n'
    '</｜｜DSML｜｜tool_calls>'
)
_V3_CONTENT = (
    "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>read_file\n"
    '```json\n{"path": "NOTEBOOK.md"}\n```'
    "<｜tool▁call▁end｜><｜tool▁calls▁end｜>"
)
_SENTINEL_FRAGMENTS = ("DSML", "\uff5c", "\u2581")


def _assert_no_sentinel(text: str) -> None:
    for fragment in _SENTINEL_FRAGMENTS:
        assert fragment not in text, f"{fragment!r} leaked into {text!r}"


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
            base_url="https://example.invalid/v1", api_key="k", model="deepseek-chat"
        )

    return build


def _content_handler(content: str, **message: Any):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": content, **message}}]
        })

    return handler


def _sse_handler(deltas: list[str]):
    """Serve one content delta per SSE frame, exactly as split by the caller."""

    def handler(_request: httpx.Request) -> httpx.Response:
        frames = [{"choices": [{"delta": {"content": piece}}]} for piece in deltas]
        frames.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    return handler


@pytest.mark.asyncio
async def test_a_provider_sentinel_never_reaches_the_user(provider) -> None:  # noqa: ANN001
    client = provider(_content_handler("Here is the plan.\n" + _INCIDENT_CONTENT))

    result = await client.chat_with_tools([{"role": "user", "content": "go"}], [])

    _assert_no_sentinel(result.content)
    assert result.content == "Here is the plan.\n"


@pytest.mark.asyncio
async def test_a_tool_call_encoded_as_content_is_recovered_as_a_real_call(provider) -> None:  # noqa: ANN001
    """A complete block is the call the model meant to make, so the loop should
    get to run it rather than end the turn on markup that reads as an answer."""
    client = provider(_content_handler(_INCIDENT_CONTENT))

    result = await client.chat_with_tools([{"role": "user", "content": "go"}], [])

    assert result.content == ""
    assert [call.name for call in result.tool_calls] == ["update_plan"]
    plan = result.tool_calls[0].arguments["plan"]
    assert [step["status"] for step in plan] == ["completed", "in_progress"]
    assert result.tool_calls[0].arguments_error is None
    assert result.malformed_tool_calls == 0


@pytest.mark.asyncio
async def test_a_deepseek_v3_tool_call_block_is_recovered_too(provider) -> None:  # noqa: ANN001
    """The ``tool▁calls`` sentinel family is the same failure in another build."""
    client = provider(_content_handler("Reading it now.\n" + _V3_CONTENT))

    result = await client.chat_with_tools([{"role": "user", "content": "read"}], [])

    assert result.content == "Reading it now.\n"
    assert [call.name for call in result.tool_calls] == ["read_file"]
    assert result.tool_calls[0].arguments == {"path": "NOTEBOOK.md"}


@pytest.mark.asyncio
async def test_a_sentinel_split_across_streaming_deltas_is_never_emitted(provider) -> None:  # noqa: ANN001
    """Rendering a sentinel and un-rendering it later is not an option, so a
    delta whose tail could still start one is held back until it cannot."""
    streamed = "Here is the plan.\n" + _INCIDENT_CONTENT
    client = provider(_sse_handler([streamed[i : i + 3] for i in range(0, len(streamed), 3)]))
    emitted: list[str] = []

    result = await client.chat_with_tools_stream(
        [{"role": "user", "content": "go"}], [], on_delta=emitted.append
    )

    for piece in emitted:
        _assert_no_sentinel(piece)
    assert "".join(emitted) == "Here is the plan.\n"
    assert result.content == "Here is the plan.\n"
    assert [call.name for call in result.tool_calls] == ["update_plan"]


@pytest.mark.asyncio
async def test_prose_that_only_looks_like_a_sentinel_is_left_exactly_as_written(provider) -> None:  # noqa: ANN001
    """The ASCII spellings are what a user writing *about* this bug would type,
    and an unbalanced ``<`` in ordinary prose must survive untouched."""
    prose = (
        "DeepSeek writes <||DSML||tool_calls> with full-width bars, and "
        "<|tool_calls_begin|> is the ASCII form. Note 3 < 4 and update_plan > nothing."
    )
    client = provider(_content_handler(prose))

    result = await client.chat_with_tools([{"role": "user", "content": "explain"}], [])

    assert result.content == prose
    assert result.tool_calls == []
    assert result.malformed_tool_calls == 0


@pytest.mark.asyncio
async def test_markup_cut_off_mid_call_is_hidden_without_inventing_a_call(provider) -> None:  # noqa: ANN001
    """Half-written arguments would invoke the tool with the wrong input, so the
    block is dropped and reported as a malformed call — which is what makes the
    loop re-nudge instead of finishing on the empty string left behind."""
    cut = (
        '<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name="update_plan">\n'
        '<｜｜DSML｜｜parameter name="plan" string="false">[{"st'
    )
    client = provider(_content_handler("Working.\n" + cut))

    result = await client.chat_with_tools([{"role": "user", "content": "go"}], [])

    assert result.content == "Working.\n"
    assert result.tool_calls == []
    assert result.malformed_tool_calls == 1


@pytest.mark.asyncio
async def test_a_structured_tool_call_wins_over_the_same_call_echoed_as_markup(provider) -> None:  # noqa: ANN001
    """When both channels carry the call, the structured field is authoritative:
    admitting the echo too would run the same tool twice."""
    client = provider(_content_handler(
        _INCIDENT_CONTENT,
        tool_calls=[{"id": "c1", "function": {"name": "update_plan", "arguments": "{}"}}],
    ))

    result = await client.chat_with_tools([{"role": "user", "content": "go"}], [])

    assert result.content == ""
    assert [call.id for call in result.tool_calls] == ["c1"]
    assert result.malformed_tool_calls == 0


@pytest.mark.asyncio
async def test_a_plain_text_answer_is_kept_free_of_sentinels_as_well(provider) -> None:  # noqa: ANN001
    """``chat`` offers no tools, so nothing can be recovered — but its output
    becomes summaries and final answers, so the markup still cannot survive."""
    client = provider(_content_handler("Summary follows.\n" + _INCIDENT_CONTENT))

    answer = await client.chat("system", "user")

    _assert_no_sentinel(answer)
    assert answer == "Summary follows.\n"
