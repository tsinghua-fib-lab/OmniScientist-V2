"""Idle-on-activity vs whole-call wall clock for the ReAct model wait."""

from __future__ import annotations

import asyncio
import time

import pytest

from omni.core.llm.client import ChatWithToolsResult, LLMClient, RetryingLLMClient, RetryPolicy
from omni.core.llm.idle import IdleWatchdog, StreamIdleTimeout, await_with_idle
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.termination import base_termination_reason

ECHO = ToolSpec("echo", "echo back", {"type": "object", "properties": {"x": {"type": "string"}}})


async def _invoker(name, args):  # noqa: ANN001, ARG001
    return {"echoed": args.get("x")}


class _StreamingBusyLLM(LLMClient):
    """Emits SSE-like deltas over a span longer than the stall window."""

    def __init__(self, *, pieces: list[str], gap_s: float, answer: str) -> None:
        self.model = "busy-stream"
        self._pieces = pieces
        self._gap_s = gap_s
        self._answer = answer
        self.stream_calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001, ARG002
        raise AssertionError("idle-armed turns must stream so activity is visible")

    async def chat_with_tools_stream(
        self,
        messages,  # noqa: ANN001, ARG002
        tools,  # noqa: ANN001, ARG002
        **kwargs,
    ) -> ChatWithToolsResult:
        self.stream_calls += 1
        on_delta = kwargs.get("on_delta")
        on_activity = kwargs.get("on_activity")
        for piece in self._pieces:
            if on_activity is not None:
                on_activity()
            if on_delta is not None:
                result = on_delta(piece)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(self._gap_s)
        return ChatWithToolsResult(content=self._answer)

    async def chat(self, system: str, user: str, **kwargs):  # noqa: ANN001, ARG002
        return self._answer


class _IdleThenOkLLM(LLMClient):
    """Fails with StreamIdleTimeout a fixed number of times, then answers."""

    def __init__(self, *, fail_times: int, answer: str) -> None:
        self.model = "idle-retry"
        self._fail_times = fail_times
        self._answer = answer
        self.calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        if self.calls <= self._fail_times:
            raise StreamIdleTimeout
        return ChatWithToolsResult(content=self._answer)

    async def chat_with_tools_stream(self, messages, tools, **kwargs):  # noqa: ANN001
        return await self.chat_with_tools(messages, tools, **kwargs)

    async def chat(self, system: str, user: str, **kwargs):  # noqa: ANN001, ARG002
        return self._answer


class _HangThenOkLLM(LLMClient):
    """Hangs without raising, then answers — idle must be detected per attempt."""

    def __init__(self, *, hang_times: int, answer: str) -> None:
        self.model = "hang-retry"
        self._hang_times = hang_times
        self._answer = answer
        self.calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        if self.calls <= self._hang_times:
            await asyncio.sleep(30.0)
        return ChatWithToolsResult(content=self._answer)

    async def chat(self, system: str, user: str, **kwargs):  # noqa: ANN001, ARG002
        return self._answer


@pytest.mark.asyncio
async def test_await_with_idle_completes_when_activity_resets_the_window():
    watchdog = IdleWatchdog()

    async def busy() -> str:
        for _ in range(4):
            await asyncio.sleep(0.05)
            watchdog.tick()
        return "ok"

    result = await await_with_idle(
        busy(), stall_s=0.12, deadline=time.monotonic() + 5.0, watchdog=watchdog,
    )
    assert result == "ok"


@pytest.mark.asyncio
async def test_await_with_idle_raises_when_the_call_goes_quiet():
    watchdog = IdleWatchdog()

    async def silent() -> str:
        await asyncio.sleep(1.0)
        return "late"

    with pytest.raises(StreamIdleTimeout):
        await await_with_idle(
            silent(),
            stall_s=0.08,
            deadline=time.monotonic() + 5.0,
            watchdog=watchdog,
        )


@pytest.mark.asyncio
async def test_streaming_longer_than_stall_window_is_not_stalled():
    # The 829bfee2 shape: tokens keep arriving past the old whole-call wait_for.
    # Stall is 0.12s; gaps are 0.05s; total span ~0.25s > stall.
    llm = _StreamingBusyLLM(
        pieces=["The ", "survey ", "continues. "],
        gap_s=0.05,
        answer="The survey continues.",
    )
    agent = ReActLoopAgent(
        llm, _invoker, max_iterations=4, max_seconds=30.0, stall_timeout_s=0.12,
    )
    res = await agent.run(system_prompt="s", user_message="write the paper", tools=[ECHO])
    assert res.kind == "text"
    assert res.content == "The survey continues."
    assert base_termination_reason(res.terminated_reason) == "done"
    assert llm.stream_calls == 1


@pytest.mark.asyncio
async def test_stream_idle_retries_then_succeeds_and_emits_reconnect():
    events: list[tuple[str, dict]] = []

    async def on_event(phase: str, data: dict) -> None:
        events.append((phase, data))

    inner = _IdleThenOkLLM(fail_times=2, answer="recovered")
    llm = RetryingLLMClient(
        inner, policy=RetryPolicy(max_retries=5, base_delay=0.0, jitter=0.0),
    )
    agent = ReActLoopAgent(
        llm, _invoker, max_iterations=4, max_seconds=30.0, stall_timeout_s=0.0,
    )
    res = await agent.run(
        system_prompt="s", user_message="u", tools=[ECHO], on_tool_event=on_event,
    )
    assert res.content == "recovered"
    reconnects = [d for phase, d in events if phase == "notice" and d.get("kind") == "reconnect"]
    assert len(reconnects) == 2
    assert reconnects[0]["attempt"] == 1
    assert reconnects[-1]["max"] == 5
    assert inner.calls == 3


@pytest.mark.asyncio
async def test_stream_idle_exhausts_retries_then_errors_without_progress():
    inner = _IdleThenOkLLM(fail_times=99, answer="never")
    llm = RetryingLLMClient(
        inner, policy=RetryPolicy(max_retries=2, base_delay=0.0, jitter=0.0),
    )
    agent = ReActLoopAgent(
        llm, _invoker, max_iterations=4, max_seconds=30.0, stall_timeout_s=0.0,
    )
    res = await agent.run(system_prompt="s", user_message="u", tools=[ECHO])
    assert res.kind == "error"
    assert res.terminated_reason == "llm_timeout"
    assert inner.calls == 3


@pytest.mark.asyncio
async def test_retrying_client_reconnects_on_idle_hang_without_outer_cancel():
    """Production stack: RetryingLLMClient owns idle; ReAct must not cancel it.

    A quiet first attempt used to trip ReAct's await_with_idle and abort the
    reconnect loop. The wrapper now retries; ReAct only holds the wall clock.
    """
    events: list[tuple[str, dict]] = []

    async def on_event(phase: str, data: dict) -> None:
        events.append((phase, data))

    inner = _HangThenOkLLM(hang_times=2, answer="recovered after idle")
    llm = RetryingLLMClient(
        inner,
        policy=RetryPolicy(max_retries=5, base_delay=0.0, jitter=0.0),
        idle_s=0.08,
    )
    agent = ReActLoopAgent(
        llm, _invoker, max_iterations=4, max_seconds=30.0, stall_timeout_s=0.08,
    )
    res = await agent.run(
        system_prompt="s", user_message="u", tools=[ECHO], on_tool_event=on_event,
    )
    assert res.content == "recovered after idle"
    reconnects = [d for phase, d in events if phase == "notice" and d.get("kind") == "reconnect"]
    assert len(reconnects) == 2
    assert inner.calls == 3
