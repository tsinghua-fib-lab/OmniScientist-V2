"""A plain-text answer cut off by the output cap must not settle as ``done``.

Incident 599a725b was fixed for tool calls only. The same cap cuts a plain
answer, and that half was left unhandled: the provider sent no ``max_tokens``
on the text paths and threw the choice object away, so a response ending
``finish_reason: "length"`` reached the user as ``kind: text /
terminated_reason: done`` — a sentence stopping mid-word, delivered under a
success reason, with nothing anywhere recording that it was incomplete.

The finalisation turn is the likeliest producer: it runs with ``tools=[]`` and
``tool_choice="none"``, so everything the turn still has to say has to fit in
one response.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.llm.errors import LLMOutputTruncated
from omni.core.llm.providers import OpenAICompatibleProvider
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.termination import execution_outcome_status
from omni.memory.compaction import summarize_messages
from tests.conftest import ScriptedLLM

# The reviewer's repro: a survey that stops mid-word in the first paragraph.
_CUT_ANSWER = (
    "# RAG survey\n\n## 1 Introduction\nRetrieval-augmented generation combi"
)

ECHO = ToolSpec("echo", "echo back", {"type": "object", "properties": {}})


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
            base_url="https://example.invalid/v1", api_key="k", model="gpt-4o"
        )

    return build


def _recording_handler(payload: dict[str, Any], seen: list[dict[str, Any]]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    return handler


def _sse_handler(chunks: list[dict[str, Any]], seen: list[dict[str, Any]]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    return handler


async def _drain(stream) -> list[str]:  # noqa: ANN001
    return [piece async for piece in stream]


# ── transport: the cap has to be visible before anything can react to it ──


@pytest.mark.asyncio
async def test_a_text_answer_stopped_at_the_cap_reports_why_it_stopped(provider) -> None:  # noqa: ANN001
    seen: list[dict[str, Any]] = []
    client = provider(_recording_handler(
        {
            "choices": [{"finish_reason": "length", "message": {"content": _CUT_ANSWER}}],
            "usage": {"total_tokens": 16384},
        },
        seen,
    ))

    result = await client.chat_result("write a survey", "go")

    assert result.content == _CUT_ANSWER
    assert result.truncated_by_output_cap is True
    assert result.usage["total_tokens"] == 16384


@pytest.mark.asyncio
async def test_a_text_request_asks_for_the_output_budget_its_model_allows(provider) -> None:  # noqa: ANN001
    """Sending no ``max_tokens`` does not mean "unlimited"; it means the
    provider's own default, which is the invisible ceiling that did the cutting."""
    seen: list[dict[str, Any]] = []
    client = provider(_recording_handler(
        {"choices": [{"finish_reason": "stop", "message": {"content": "hi"}}]}, seen
    ))

    await client.chat("sys", "user")

    assert seen[0]["max_tokens"] == 16_384  # gpt-4o's cap, from the model catalog


@pytest.mark.asyncio
async def test_a_streamed_answer_reports_the_cap_after_delivering_what_arrived(provider) -> None:  # noqa: ANN001
    """A generator has no return value to carry a finish reason, so the only way
    an ``async for`` can learn its answer was cut is to raise once it ends."""
    seen: list[dict[str, Any]] = []
    client = provider(_sse_handler(
        [
            {"choices": [{"delta": {"content": "# RAG survey\n\nRetrieval-augmented "}}]},
            {"choices": [{"delta": {"content": "generation combi"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ],
        seen,
    ))

    delivered: list[str] = []
    with pytest.raises(LLMOutputTruncated) as caught:
        async for piece in client.chat_stream("sys", "write a survey"):
            delivered.append(piece)

    assert "".join(delivered).endswith("combi")
    assert caught.value.info.terminated_reason == "output_cap_truncated"
    assert seen[0]["max_tokens"] == 16_384


@pytest.mark.asyncio
async def test_a_streamed_answer_that_finished_normally_raises_nothing(provider) -> None:  # noqa: ANN001
    seen: list[dict[str, Any]] = []
    client = provider(_sse_handler(
        [
            {"choices": [{"delta": {"content": "a complete answer."}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ],
        seen,
    ))

    assert await _drain(client.chat_stream("sys", "ask")) == ["a complete answer."]


@pytest.mark.asyncio
async def test_a_text_answer_that_finished_normally_is_not_called_truncated(provider) -> None:  # noqa: ANN001
    """Negative case. Only the cap makes an answer incomplete; a short reply that
    simply ended is a complete reply and must travel untouched."""
    seen: list[dict[str, Any]] = []
    client = provider(_recording_handler(
        {"choices": [{"finish_reason": "stop", "message": {"content": "Yes — see §3."}}]},
        seen,
    ))

    result = await client.chat_result("sys", "is it covered?")

    assert result.truncated_by_output_cap is False
    assert result.content == "Yes — see §3."


# ── the loop: a fragment is delivered, but never as a finished answer ──


@pytest.mark.asyncio
async def test_an_answer_cut_off_by_the_cap_is_not_handed_back_as_done() -> None:
    llm = ScriptedLLM([ChatWithToolsResult(content=_CUT_ANSWER, finish_reason="length")])
    agent = ReActLoopAgent(llm, _unreachable_invoker, max_iterations=4)

    res = await agent.run(system_prompt="sys", user_message="write a survey", tools=[ECHO])

    assert res.terminated_reason == "output_cap_truncated"
    assert res.kind == "partial"
    # The work that did arrive is still delivered — it answers most of the ask.
    assert res.content.startswith(_CUT_ANSWER)
    assert "output-token limit" in res.content
    assert execution_outcome_status(res.kind, res.terminated_reason) == "degraded"


@pytest.mark.asyncio
async def test_a_truncated_answer_is_never_re_asked_under_the_same_cap() -> None:
    """Re-asking is the trap the tool-call half already fell into: the same
    prompt under the same ceiling stops at the same word, and the turn spends
    its budget discovering that."""
    llm = ScriptedLLM([
        ChatWithToolsResult(content=_CUT_ANSWER, finish_reason="length"),
        ChatWithToolsResult(content="a second, equally doomed attempt"),
    ])
    agent = ReActLoopAgent(llm, _unreachable_invoker, max_iterations=6)

    res = await agent.run(system_prompt="sys", user_message="write a survey", tools=[ECHO])

    assert llm.calls == 1
    assert res.content.startswith(_CUT_ANSWER)


@pytest.mark.asyncio
async def test_a_finalisation_answer_cut_off_by_the_cap_reports_the_cap_not_the_bound() -> None:
    """The wrap-up turn runs with no tools, so it is the likeliest place for a
    long inline answer to hit the cap. Reporting ``synthesized_max_iterations``
    would name the bound that stopped the tools and hide the one that cut the
    text."""
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {})]),
        ChatWithToolsResult(content=_CUT_ANSWER, finish_reason="length"),
    ])
    agent = ReActLoopAgent(llm, _echo_invoker, max_iterations=1)

    res = await agent.run(system_prompt="sys", user_message="write a survey", tools=[ECHO])

    assert res.terminated_reason == "output_cap_truncated"
    assert res.kind == "partial"
    assert "output-token limit" in res.content


@pytest.mark.asyncio
async def test_a_finalisation_answer_that_finished_normally_keeps_its_bound() -> None:
    """Negative case: the existing ``synthesized_<bound>`` reporting must not be
    displaced for every wrap-up, only for the ones the cap cut."""
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {})]),
        ChatWithToolsResult(content="the complete wrap-up answer"),
    ])
    agent = ReActLoopAgent(llm, _echo_invoker, max_iterations=1)

    res = await agent.run(system_prompt="sys", user_message="write a survey", tools=[ECHO])

    assert res.terminated_reason == "synthesized_max_iterations"
    assert res.kind == "text"
    assert res.content == "the complete wrap-up answer"


@pytest.mark.asyncio
async def test_a_clarifying_question_cut_off_by_the_cap_falls_back_to_the_tool_payload() -> None:
    """A question exists to be answered, so half of one is worse than none — and
    unlike a long answer there is a complete alternative already in hand."""
    async def clarifying_invoker(_name, _args):  # noqa: ANN001
        return {
            "outcome": "needs_input",
            "message": "Do you mean the 2024 or the 2025 review?",
        }

    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {})]),
        ChatWithToolsResult(content="Which of the two revi", finish_reason="length"),
    ])
    agent = ReActLoopAgent(llm, clarifying_invoker, max_iterations=4)

    res = await agent.run(system_prompt="sys", user_message="summarise the review", tools=[ECHO])

    assert res.kind == "needs_input"
    assert res.content == "Do you mean the 2024 or the 2025 review?"


# ── durable storage: a half summary is worse than a blunt complete one ──


class _TruncatingLLM(ScriptedLLM):
    """A client whose every tool-free answer stops at the output cap."""

    def __init__(self, content: str) -> None:
        super().__init__()
        self._content = content

    async def chat_result(self, system: str, user: str, **kwargs: Any) -> ChatWithToolsResult:
        return ChatWithToolsResult(content=self._content, finish_reason="length")


@pytest.mark.asyncio
async def test_a_compaction_summary_cut_off_by_the_cap_is_not_written_to_the_session() -> None:
    """The bridge note replaces the turns it summarises, so a fragment is not
    merely unreadable — it becomes the only record of what those turns decided,
    and everything after the cut is gone for every later turn."""
    settings = load_settings()
    settings.model.provider = "openai_compatible"
    messages = [
        {"role": "user", "content": "survey retrieval-augmented generation"},
        {"role": "assistant", "content": "produced artifact://survey1"},
    ]

    out = await summarize_messages(
        _TruncatingLLM("- goal: survey RAG\n- decided to use artifact://sur"),
        settings,
        messages,
    )

    assert "artifact://sur\n" not in out
    assert "artifact://survey1" in out  # the deterministic summary kept the whole reference
    assert out.startswith("Earlier conversation summary")


@pytest.mark.asyncio
async def test_a_complete_compaction_summary_is_still_used() -> None:
    """Negative case: rejecting truncation must not reject summarisation."""
    settings = load_settings()
    settings.model.provider = "openai_compatible"
    messages = [{"role": "user", "content": "survey retrieval-augmented generation"}]

    out = await summarize_messages(ScriptedLLM(), settings, messages)

    assert out.startswith("summary:")


async def _unreachable_invoker(name, args):  # noqa: ANN001, ARG001
    raise AssertionError("a text-only turn must not dispatch a tool")


async def _echo_invoker(_name, _args):  # noqa: ANN001
    return {"echoed": True}
