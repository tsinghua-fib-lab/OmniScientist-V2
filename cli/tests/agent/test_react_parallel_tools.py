"""P1-D′b: multiple tool calls in one turn run concurrently, in order."""

from __future__ import annotations

import asyncio
import time

import pytest

from omni.core.llm.client import ChatWithToolsResult, LLMClient, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from tests.conftest import ScriptedLLM

SLEEP = ToolSpec("sleep", "sleep then echo", {"type": "object", "properties": {"i": {"type": "string"}}})
_DELAY = 0.05


async def _slow_invoker(name, args):  # noqa: ANN001
    await asyncio.sleep(_DELAY)
    return {"i": args.get("i")}


def _three_call_llm() -> ScriptedLLM:
    return ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall(f"c{i}", "sleep", {"i": str(i)}) for i in range(3)]
            ),
            ChatWithToolsResult(content="done"),
        ]
    )


@pytest.mark.asyncio
async def test_batch_runs_concurrently_and_preserves_order():
    agent = ReActLoopAgent(_three_call_llm(), _slow_invoker, max_iterations=4, parallel_tools=True)
    started = time.monotonic()
    res = await agent.run(system_prompt="s", user_message="u", tools=[SLEEP])
    elapsed = time.monotonic() - started

    assert res.kind == "text" and res.content == "done"
    assert res.total_tool_calls == 3
    # results kept in request order despite concurrent dispatch
    assert [r.arguments["i"] for r in res.tool_trace] == ["0", "1", "2"]
    # 3 × 0.05s serial ≈ 0.15s; concurrent ≈ 0.05s. Allow generous slack.
    assert elapsed < 0.12, f"expected concurrent dispatch, took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_serial_mode_is_slower_than_parallel():
    agent = ReActLoopAgent(_three_call_llm(), _slow_invoker, max_iterations=4, parallel_tools=False)
    started = time.monotonic()
    res = await agent.run(system_prompt="s", user_message="u", tools=[SLEEP])
    elapsed = time.monotonic() - started
    assert res.kind == "text"
    assert elapsed >= 3 * _DELAY * 0.8, f"serial should take ≥3×delay, took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_batch_respects_tool_budget_split():
    # A 3-call batch with a budget of 2: dispatch 2, then trip max_tool_calls.
    agent = ReActLoopAgent(
        _three_call_llm(), _slow_invoker,
        max_iterations=4, max_tool_calls=2, no_progress_synthesis=False,
    )
    res = await agent.run(system_prompt="s", user_message="u", tools=[SLEEP])
    assert res.terminated_reason == "max_tool_calls"
    # Only actually executed calls count as tool calls. The rejected call stays
    # in the auditable trace and receives a protocol-closing result.
    assert res.total_tool_calls == 2
    assert [r.arguments["i"] for r in res.tool_trace if r.status != "rejected"] == ["0", "1"]
    assert [r.arguments["i"] for r in res.tool_trace if r.status == "rejected"] == ["2"]


class _BudgetBoundaryLLM(LLMClient):
    """Assert that every requested call is closed before final synthesis."""

    model = "budget-boundary"

    def __init__(self, *, final: str = "best effort") -> None:
        self.calls = 0
        self.final = final
        self.final_messages: list[dict] = []

    async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return ChatWithToolsResult(
                tool_calls=[ToolCall(f"c{i}", "sleep", {"i": str(i)}) for i in range(3)]
            )
        self.final_messages = list(messages)
        return ChatWithToolsResult(content=self.final)

    async def chat(self, system, user, **kwargs):  # noqa: ANN001
        return self.final


@pytest.mark.asyncio
async def test_hard_budget_rejection_closes_every_tool_call_before_synthesis():
    llm = _BudgetBoundaryLLM()
    agent = ReActLoopAgent(
        llm,
        _slow_invoker,
        max_iterations=4,
        max_tool_calls=2,
    )

    res = await agent.run(system_prompt="s", user_message="u", tools=[SLEEP])

    assert res.kind == "text"
    assert res.content == "best effort"
    assert res.terminated_reason == "synthesized_max_tool_calls"
    assistant = next(message for message in llm.final_messages if message.get("tool_calls"))
    expected_ids = [item["id"] for item in assistant["tool_calls"]]
    result_ids = [
        message["tool_call_id"]
        for message in llm.final_messages
        if message.get("role") == "tool"
    ]
    assert result_ids == expected_ids
    assert res.total_tool_calls == 2
    assert res.tool_budget == {
        "limit": 2,
        "requested": 3,
        "admitted": 2,
        "completed": 2,
        "rejected": 1,
        "exhausted": True,
    }


@pytest.mark.asyncio
async def test_thirteen_requested_calls_execute_only_twelve_and_keep_valid_transcript() -> None:
    script: list[ChatWithToolsResult] = []
    cursor = 0
    for size in (2, 4, 4, 3):
        script.append(
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(f"c{i}", "sleep", {"i": str(i)})
                    for i in range(cursor, cursor + size)
                ]
            )
        )
        cursor += size
    script.append(ChatWithToolsResult(content="13-call synthesis completed"))

    class CapturingScriptedLLM(ScriptedLLM):
        def __init__(self) -> None:
            super().__init__(script)
            self.last_messages: list[dict] = []

        async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
            self.last_messages = list(messages)
            return await super().chat_with_tools(messages, tools, **kwargs)

    llm = CapturingScriptedLLM()
    agent = ReActLoopAgent(
        llm,
        _slow_invoker,
        max_iterations=5,
        max_tool_calls=12,
    )

    result = await agent.run(system_prompt="s", user_message="inspect prior work", tools=[SLEEP])

    assert result.kind == "text"
    assert result.content == "13-call synthesis completed"
    assert result.total_tool_calls == 12
    assert result.tool_budget["limit"] == 12
    assert result.tool_budget["rejected"] == 1
    assert [record.status for record in result.tool_trace].count("rejected") == 1
    call_ids = [
        call["id"]
        for message in llm.last_messages
        for call in message.get("tool_calls", [])
    ]
    result_ids = [
        message["tool_call_id"]
        for message in llm.last_messages
        if message.get("role") == "tool"
    ]
    assert result_ids == call_ids


@pytest.mark.asyncio
async def test_single_call_path_unchanged():
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "sleep", {"i": "solo"})]),
        ChatWithToolsResult(content="ok"),
    ])
    agent = ReActLoopAgent(llm, _slow_invoker, max_iterations=4, parallel_tools=True)
    res = await agent.run(system_prompt="s", user_message="u", tools=[SLEEP])
    assert res.kind == "text" and res.tool_names() == ["sleep"]


@pytest.mark.asyncio
async def test_cancel_closes_every_inflight_parallel_tool_call() -> None:
    started = 0
    both_started = asyncio.Event()
    cancelled: list[str] = []
    events: list[tuple[str, dict]] = []

    async def blocking_invoker(name, args):  # noqa: ANN001
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(str(args["i"]))
            raise

    llm = ScriptedLLM([
        ChatWithToolsResult(
            tool_calls=[ToolCall(f"c{i}", "sleep", {"i": str(i)}) for i in range(2)]
        )
    ])

    async def controls() -> list[dict[str, str]]:
        return [{"action": "cancel", "instruction": ""}] if both_started.is_set() else []

    async def on_event(phase: str, data: dict) -> None:
        events.append((phase, data))

    agent = ReActLoopAgent(llm, blocking_invoker, max_iterations=3)
    result = await asyncio.wait_for(
        agent.run(
            system_prompt="s",
            user_message="u",
            tools=[SLEEP],
            on_control=controls,
            on_tool_event=on_event,
        ),
        timeout=1,
    )

    assert result.kind == "partial"
    assert result.terminated_reason == "cancelled"
    assert [record.status for record in result.tool_trace] == ["cancelled", "cancelled"]
    assert sorted(cancelled) == ["0", "1"]
    assert [data["status"] for phase, data in events if phase == "done"] == [
        "cancelled",
        "cancelled",
    ]
