"""ReAct loop: tool dispatch, bounds, escalation."""

from __future__ import annotations

import json

import httpx
import pytest

from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.tool_result import ToolResultEnvelope, _mint_host_tool_rejection
from tests.conftest import ScriptedLLM

ECHO = ToolSpec("echo", "echo back", {"type": "object", "properties": {"x": {"type": "string"}}})


async def _invoker(name, args):
    if name == "echo":
        return {"echoed": args.get("x")}
    raise ValueError("boom")


@pytest.mark.asyncio
async def test_tool_then_final_answer():
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {"x": "hi"})]),
        ChatWithToolsResult(content="final answer"),
    ])
    agent = ReActLoopAgent(llm, _invoker, max_iterations=4)
    res = await agent.run(system_prompt="sys", user_message="hello", tools=[ECHO])
    assert res.kind == "text"
    assert res.content == "final answer"
    assert res.tool_names() == ["echo"]
    assert res.total_tool_calls == 1


@pytest.mark.asyncio
async def test_structured_tool_result_separates_model_observation_from_event_output():
    invocations = 0
    events: list[tuple[str, dict]] = []

    async def command_invoker(_name, _args):  # noqa: ANN001
        nonlocal invocations
        invocations += 1
        return ToolResultEnvelope(
            observation="[exit=1]\n",
            event_output={
                "result_schema": "omni.command-result.v1",
                "command_status": "failed",
                "exit_code": 1,
            },
        )

    async def on_event(phase: str, data: dict) -> None:
        events.append((phase, data))

    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {"x": "false"})]),
        ChatWithToolsResult(content="the command failed"),
    ])
    agent = ReActLoopAgent(llm, command_invoker, max_iterations=4)

    result = await agent.run(
        system_prompt="sys",
        user_message="run false",
        tools=[ECHO],
        on_tool_event=on_event,
    )

    record = result.tool_trace[0]
    done = next(data for phase, data in events if phase == "done")
    assert invocations == 1
    assert record.status == "succeeded"
    assert record.error is None
    assert record.attempts == 1
    assert record.result["command_status"] == "failed"
    assert record.to_observation() == "[exit=1]\n"
    assert done["status"] == "succeeded"
    assert done["result"]["exit_code"] == 1
    assert "ToolResultEnvelope" not in repr(done)


@pytest.mark.parametrize(
    ("marker", "expected_error_code"),
    [
        ("approval_required", "tool_approval_required"),
        ("policy_violation", "tool_policy_rejected"),
    ],
)
@pytest.mark.asyncio
async def test_reason_only_tool_rejection_is_preserved(
    marker,
    expected_error_code,
):
    events: list[tuple[str, dict]] = []

    async def rejecting_invoker(_name, _args):  # noqa: ANN001
        return _mint_host_tool_rejection(
            {marker: True, "reason": "owner denied"}
        )

    async def on_event(phase: str, data: dict) -> None:
        events.append((phase, data))

    llm = ScriptedLLM(
        [
            ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {"x": "secret"})]),
            ChatWithToolsResult(content="the call was denied"),
        ]
    )
    agent = ReActLoopAgent(llm, rejecting_invoker, max_iterations=4)

    result = await agent.run(
        system_prompt="sys",
        user_message="request a protected operation",
        tools=[ECHO],
        on_tool_event=on_event,
    )

    record = result.tool_trace[0]
    done = next(data for phase, data in events if phase == "done")
    assert record.status == "rejected"
    assert record.error == "owner denied"
    assert record.error_code == expected_error_code
    assert done["status"] == "rejected"
    assert done["error"] == "owner denied"


def test_explicit_empty_tool_observation_is_not_replaced_by_event_json():
    from omni.core.react_agent import ToolInvocationRecord

    record = ToolInvocationRecord(
        name="echo",
        arguments={},
        result={"command_status": "succeeded", "output": "event-only"},
        observation="",
    )

    assert record.to_observation() == ""


def test_transport_error_takes_precedence_over_observation_override():
    from omni.core.react_agent import ToolInvocationRecord

    record = ToolInvocationRecord(
        name="echo",
        arguments={},
        result={"command_status": "succeeded"},
        observation="stale successful observation",
        error="policy rejected",
        status="rejected",
        error_code="tool_policy_rejected",
    )

    observation = json.loads(record.to_observation())
    assert observation["status"] == "rejected"
    assert observation["error"] == "policy rejected"
    assert observation["reason"] == "tool_policy_rejected"


@pytest.mark.parametrize("command_status", ["blocked", "invalid", "timed_out"])
def test_controlled_command_outcome_counts_as_unproductive(command_status):
    from omni.core.react_agent import ToolInvocationRecord, _is_unproductive

    record = ToolInvocationRecord(
        name="bash",
        arguments={},
        result={
            "result_schema": "omni.command-result.v1",
            "command_status": command_status,
        },
        observation="ERROR: command did not run",
    )

    assert _is_unproductive(record) is True


def test_nonzero_command_outcome_is_an_informative_observation():
    from omni.core.react_agent import ToolInvocationRecord, _is_unproductive

    record = ToolInvocationRecord(
        name="bash",
        arguments={"command": "false"},
        result={
            "result_schema": "omni.command-result.v1",
            "command_status": "failed",
            "exit_code": 1,
        },
        observation="[exit=1]\n",
    )

    assert _is_unproductive(record) is False


@pytest.mark.asyncio
async def test_repeated_command_failures_do_not_trip_transport_circuit():
    calls = 0

    async def command_invoker(_name, args):  # noqa: ANN001
        nonlocal calls
        calls += 1
        exit_code = int(args["x"])
        return ToolResultEnvelope(
            observation=f"[exit={exit_code}]\n",
            event_output={
                "result_schema": "omni.command-result.v1",
                "command_status": "failed",
                "exit_code": exit_code,
            },
        )

    script = [
        ChatWithToolsResult(tool_calls=[ToolCall(f"c{i}", "echo", {"x": str(i)})])
        for i in range(1, 7)
    ]
    script.append(ChatWithToolsResult(content="all command outcomes inspected"))
    agent = ReActLoopAgent(
        ScriptedLLM(script),
        command_invoker,
        max_iterations=8,
        no_progress_threshold=7,
    )

    result = await agent.run(system_prompt="sys", user_message="run commands", tools=[ECHO])

    assert calls == 6
    assert result.content == "all command outcomes inspected"
    assert all(record.status == "succeeded" for record in result.tool_trace)
    assert all(record.attempts == 1 for record in result.tool_trace)


@pytest.mark.asyncio
async def test_command_failure_can_be_corrected_without_transport_retry():
    calls: list[str] = []

    async def command_invoker(_name, args):  # noqa: ANN001
        command = str(args["x"])
        calls.append(command)
        exit_code = 1 if command == "false" else 0
        return ToolResultEnvelope(
            observation=f"[exit={exit_code}]\n",
            event_output={
                "result_schema": "omni.command-result.v1",
                "command_status": "failed" if exit_code else "succeeded",
                "reason": "nonzero_exit" if exit_code else "ok",
                "exit_code": exit_code,
            },
        )

    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("failed", "echo", {"x": "false"})]
            ),
            ChatWithToolsResult(
                tool_calls=[ToolCall("corrected", "echo", {"x": "true"})]
            ),
            ChatWithToolsResult(content="corrected command succeeded"),
        ]
    )
    agent = ReActLoopAgent(llm, command_invoker, max_iterations=4)

    result = await agent.run(
        system_prompt="sys",
        user_message="recover from a failed command",
        tools=[ECHO],
    )

    assert calls == ["false", "true"]
    assert [record.attempts for record in result.tool_trace] == [1, 1]
    assert [
        record.result["command_status"] for record in result.tool_trace
    ] == ["failed", "succeeded"]
    assert result.content == "corrected command succeeded"


@pytest.mark.asyncio
async def test_malformed_tool_args_yield_retryable_error_observation():
    """P0.2: unparseable tool arguments become a model-visible *failed*
    observation (retryable) instead of silently invoking the tool with ``{}``."""
    invoked: list[dict] = []

    async def recording_invoker(name, args):
        invoked.append({"name": name, "args": args})
        return {"echoed": args.get("x")}

    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[
            ToolCall("c1", "echo", {}, arguments_error="Expecting value: line 1 column 1 (char 0)")
        ]),
        ChatWithToolsResult(content="recovered with valid json"),
    ])
    agent = ReActLoopAgent(llm, recording_invoker, max_iterations=4)
    res = await agent.run(system_prompt="sys", user_message="hi", tools=[ECHO])

    assert res.kind == "text"
    assert res.content == "recovered with valid json"
    assert invoked == []  # the malformed call was rejected, never executed
    rec = res.tool_trace[0]
    assert rec.status == "rejected"
    assert rec.error_code == "tool_arguments_invalid"
    assert rec.retryable is True
    obs = json.loads(rec.to_observation())
    assert obs["retryable"] is True and obs["reason"] == "tool_arguments_invalid"


@pytest.mark.asyncio
async def test_tool_event_callback_may_be_async():
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {"x": "hi"})]),
        ChatWithToolsResult(content="final answer"),
    ])
    events: list[tuple[str, str]] = []

    async def on_tool_event(phase: str, data: dict) -> None:
        events.append((phase, data.get("name", "")))

    agent = ReActLoopAgent(llm, _invoker, max_iterations=4)
    res = await agent.run(
        system_prompt="sys",
        user_message="hello",
        tools=[ECHO],
        on_tool_event=on_tool_event,
    )

    assert res.kind == "text"
    assert events == [("start", "echo"), ("done", "echo")]


@pytest.mark.asyncio
async def test_malformed_history_is_repaired_and_emits_auditable_event():
    llm = ScriptedLLM([ChatWithToolsResult(content="recovered")])
    events: list[tuple[str, dict]] = []

    async def on_tool_event(phase: str, data: dict) -> None:
        events.append((phase, data))

    agent = ReActLoopAgent(llm, _invoker, max_iterations=2)
    result = await agent.run(
        system_prompt="sys",
        user_message="continue",
        tools=[ECHO],
        history=[{
            "role": "assistant",
            "content": "",
            "tool_calls": [ToolCall("old-call", "echo", {"x": "old"}).to_message_fragment()],
        }],
        on_tool_event=on_tool_event,
    )

    assert result.content == "recovered"
    assert "missing_tool_result:old-call" in result.transcript_repairs
    assert any(phase == "transcript" and data["status"] == "repaired" for phase, data in events)


@pytest.mark.asyncio
async def test_unknown_tool_is_reported_not_crash():
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "nope", {})]),
        ChatWithToolsResult(content="recovered"),
    ])
    agent = ReActLoopAgent(llm, _invoker, max_iterations=4)
    res = await agent.run(system_prompt="s", user_message="u", tools=[ECHO])
    assert res.kind == "text"
    assert res.tool_trace[0].error is not None


@pytest.mark.asyncio
async def test_max_iterations_bound():
    # Always returns a distinct, productive tool call → no synthesis trigger;
    # must terminate at the bound with a salvage stub (synthesis disabled).
    never_ending = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall(f"c{i}", "echo", {"x": str(i)})]) for i in range(20)
    ])
    agent = ReActLoopAgent(never_ending, _invoker, max_iterations=3, no_progress_synthesis=False)
    res = await agent.run(system_prompt="s", user_message="u", tools=[ECHO])
    assert res.kind in {"error", "partial"}
    assert res.terminated_reason in ("max_iterations", "max_tool_calls")
    if res.terminated_reason == "max_iterations":
        assert "Partial result" in res.content
        assert "iteration limit" in res.content


@pytest.mark.asyncio
async def test_max_tool_calls_returns_partial_salvage_not_bare_error():
    # With synthesis disabled the tool-budget path must still return a useful
    # salvage stub rather than a bare error.
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {"x": "one"})]),
        ChatWithToolsResult(tool_calls=[ToolCall("c2", "echo", {"x": "two"})]),
        ChatWithToolsResult(content="should not need this"),
    ])
    agent = ReActLoopAgent(llm, _invoker, max_iterations=4, max_tool_calls=1, no_progress_synthesis=False)

    res = await agent.run(system_prompt="sys", user_message="RAG 如何降低幻觉", tools=[ECHO])

    assert res.kind == "partial"
    assert res.terminated_reason == "max_tool_calls"
    assert "Partial result" in res.content
    assert "echo" in res.content


@pytest.mark.asyncio
async def test_optional_token_limit_rejects_tools_before_further_execution():
    invoked: list[str] = []

    async def counting_invoker(name, args):  # noqa: ANN001, ARG001
        invoked.append(name)
        return "unexpected"

    llm = ScriptedLLM([
        ChatWithToolsResult(
            tool_calls=[ToolCall("c1", "echo", {"x": "one"})],
            usage={"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
        ),
    ])
    agent = ReActLoopAgent(
        llm,
        counting_invoker,
        max_iterations=4,
        max_total_tokens=50,
    )

    result = await agent.run(system_prompt="s", user_message="u", tools=[ECHO])

    assert result.kind == "partial"
    assert result.terminated_reason == "max_total_tokens"
    assert invoked == []
    assert result.tool_trace[0].status == "rejected"
    assert result.tool_trace[0].error_code == "run_token_budget_exhausted"


@pytest.mark.asyncio
async def test_optional_cost_limit_rejects_tools_before_further_execution():
    invoked: list[str] = []

    async def counting_invoker(name, args):  # noqa: ANN001, ARG001
        invoked.append(name)
        return "unexpected"

    llm = ScriptedLLM([
        ChatWithToolsResult(
            tool_calls=[ToolCall("c1", "echo", {"x": "one"})],
            usage={"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100},
        ),
    ])
    agent = ReActLoopAgent(
        llm,
        counting_invoker,
        max_iterations=4,
        max_cost_usd=0.05,
        input_cost_per_mtok=1000.0,
    )

    result = await agent.run(system_prompt="s", user_message="u", tools=[ECHO])

    assert result.kind == "partial"
    assert result.terminated_reason == "max_cost"
    assert invoked == []
    assert result.tool_trace[0].error_code == "run_cost_budget_exhausted"


@pytest.mark.asyncio
async def test_provider_bad_request_is_classified_without_raw_transport_details():
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    response = httpx.Response(
        400,
        request=request,
        json={"error": {"message": "No tool output found for function call call-1"}},
    )
    error = httpx.HTTPStatusError("400 Bad Request", request=request, response=response)

    class BrokenLLM(ScriptedLLM):
        async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
            raise error

    agent = ReActLoopAgent(BrokenLLM(), _invoker, max_iterations=2)
    res = await agent.run(system_prompt="s", user_message="u", tools=[ECHO])

    assert res.kind == "error"
    assert res.terminated_reason == "llm_transcript_invalid"
    assert "rejected the tool-call transcript" in res.content
    assert "provider.invalid" not in res.content
    assert "HTTPStatusError" not in res.content


@pytest.mark.asyncio
async def test_final_synthesis_400_falls_back_to_safe_partial_without_traceback(caplog):  # noqa: ANN001
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    response = httpx.Response(
        400,
        request=request,
        json={"error": {"message": "No tool output found for function call call-2"}},
    )
    error = httpx.HTTPStatusError("400 Bad Request", request=request, response=response)

    class FinalizationFails(ScriptedLLM):
        async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                return ChatWithToolsResult(
                    tool_calls=[
                        ToolCall("call-1", "echo", {"x": "one"}),
                        ToolCall("call-2", "echo", {"x": "two"}),
                    ]
                )
            raise error

    caplog.set_level("WARNING")
    agent = ReActLoopAgent(
        FinalizationFails(),
        _invoker,
        max_iterations=2,
        max_tool_calls=1,
    )

    result = await agent.run(system_prompt="s", user_message="u", tools=[ECHO])

    assert result.kind == "partial"
    assert result.terminated_reason == "max_tool_calls"
    assert "Partial result" in result.content
    assert "provider.invalid" not in result.content
    assert "HTTPStatusError" not in result.content
    assert "Traceback" not in caplog.text


# ── Layer A: progress detection + forced knowledge-terminal synthesis ──

_BOOM = ToolSpec("boom", "always fails", {"type": "object", "properties": {}})


async def _always_fail(name, args):  # noqa: ANN001, ARG001
    raise ValueError("nope")


@pytest.mark.asyncio
async def test_no_progress_forces_synthesis_answer():
    # Two failing tool calls → after the threshold, one tool-free synthesis turn
    # returns a real answer instead of riding the budget to a non-answer.
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "boom", {})]),
        ChatWithToolsResult(tool_calls=[ToolCall("c2", "boom", {})]),
        ChatWithToolsResult(content="综合已知信息作答（部分未经工具验证）"),
    ])
    agent = ReActLoopAgent(llm, _always_fail, max_iterations=6, no_progress_threshold=2)
    res = await agent.run(system_prompt="s", user_message="你的存储架构是如何设计的", tools=[_BOOM])
    assert res.kind == "text"
    assert res.terminated_reason == "synthesized_no_progress"
    assert "综合已知信息" in res.content


@pytest.mark.asyncio
async def test_repeated_identical_call_counts_as_no_progress():
    # Even a "productive" result, if the model repeats the identical call, is a
    # loop → forces synthesis.
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {"x": "hi"})]),
        ChatWithToolsResult(tool_calls=[ToolCall("c2", "echo", {"x": "hi"})]),
        ChatWithToolsResult(content="answer from knowledge"),
    ])
    agent = ReActLoopAgent(llm, _invoker, max_iterations=6, no_progress_threshold=1)
    res = await agent.run(system_prompt="s", user_message="u", tools=[ECHO])
    assert res.kind == "text"
    assert res.terminated_reason == "synthesized_no_progress"
    assert res.content == "answer from knowledge"


@pytest.mark.asyncio
async def test_repeated_call_with_changing_observation_keeps_running():
    values = iter([{"page": 1}, {"page": 2}])

    async def changing_invoker(name, args):  # noqa: ANN001, ARG001
        return next(values)

    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {"x": "same"})]),
        ChatWithToolsResult(tool_calls=[ToolCall("c2", "echo", {"x": "same"})]),
        ChatWithToolsResult(content="complete after page 2"),
    ])
    agent = ReActLoopAgent(llm, changing_invoker, max_iterations=6, no_progress_threshold=1)

    res = await agent.run(system_prompt="s", user_message="u", tools=[ECHO])

    assert res.terminated_reason == "done"
    assert res.content == "complete after page 2"


@pytest.mark.asyncio
async def test_distinct_failed_retrieval_sources_do_not_form_one_no_progress_loop():
    tools = [
        ToolSpec(name, f"search {name}", {"type": "object", "properties": {"query": {"type": "string"}}})
        for name in ("search_arxiv", "search_openalex", "search_crossref")
    ]

    async def multi_source_invoker(name, args):  # noqa: ANN001, ARG001
        if name == "search_arxiv":
            return {"status": "empty", "results": []}
        if name == "search_openalex":
            return {"status": "error", "error": "temporary source failure"}
        return {"status": "ok", "results": [{"title": "Grounded RAG survey"}]}

    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("a1", "search_arxiv", {"query": "RAG hallucination"})]),
        ChatWithToolsResult(tool_calls=[ToolCall("o1", "search_openalex", {"query": "RAG hallucination"})]),
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "search_crossref", {"query": "RAG hallucination"})]),
        ChatWithToolsResult(content="Crossref returned a usable paper; synthesis complete."),
    ])
    agent = ReActLoopAgent(llm, multi_source_invoker, max_iterations=6, no_progress_threshold=2)

    result = await agent.run(
        system_prompt="Search multiple scholarly sources before answering.",
        user_message="Compare evidence on whether RAG reduces hallucination.",
        tools=tools,
    )

    assert result.terminated_reason == "done"
    assert result.content == "Crossref returned a usable paper; synthesis complete."
    assert result.tool_names() == ["search_arxiv", "search_openalex", "search_crossref"]
    assert result.tool_trace[1].status == "failed"
    assert result.tool_trace[1].error == "temporary source failure"


@pytest.mark.asyncio
async def test_synthesis_disabled_falls_back_to_salvage():
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall(f"c{i}", "boom", {})]) for i in range(6)
    ])
    agent = ReActLoopAgent(llm, _always_fail, max_iterations=3, no_progress_synthesis=False)
    res = await agent.run(system_prompt="s", user_message="u", tools=[_BOOM])
    assert res.kind == "partial"
    assert res.terminated_reason == "max_iterations"
    assert "Partial result" in res.content


@pytest.mark.asyncio
async def test_empty_synthesis_falls_back_to_salvage():
    # Synthesis that yields no text (an empty tool-call turn) degrades to the
    # salvage stub rather than an empty answer.
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "boom", {})]),
        ChatWithToolsResult(tool_calls=[ToolCall("c2", "boom", {})]),
        ChatWithToolsResult(tool_calls=[ToolCall("c3", "boom", {})]),  # synth attempt: empty content
    ])
    agent = ReActLoopAgent(llm, _always_fail, max_iterations=6, no_progress_threshold=2)
    res = await agent.run(system_prompt="s", user_message="u", tools=[_BOOM])
    assert res.kind == "partial"
    assert res.terminated_reason == "no_progress"
    assert "Partial result" in res.content


@pytest.mark.asyncio
async def test_circuit_breaker_is_per_instance_not_process_global():
    # A fresh agent must not inherit another agent's tripped circuit breaker.
    fail_llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall(f"c{i}", "boom", {})]) for i in range(12)
    ])
    doomed = ReActLoopAgent(fail_llm, _always_fail, max_iterations=8, no_progress_synthesis=False)
    await doomed.run(system_prompt="s", user_message="u", tools=[_BOOM])

    ok_llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {"x": "hi"})]),
        ChatWithToolsResult(content="fresh"),
    ])
    fresh = ReActLoopAgent(ok_llm, _invoker, max_iterations=4)
    res = await fresh.run(system_prompt="s", user_message="u", tools=[ECHO])
    assert res.kind == "text"
    assert res.tool_trace[0].error is None


@pytest.mark.asyncio
async def test_escalation():
    from omni.core.react_agent import ESCALATE_RUN_TOOL_NAME
    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", ESCALATE_RUN_TOOL_NAME,
                                                 {"goal_summary": "big goal"})]),
    ])
    agent = ReActLoopAgent(llm, _invoker)
    res = await agent.run(system_prompt="s", user_message="u", tools=[ECHO], allow_escalation=True)
    assert res.kind == "escalated"
    assert res.escalated_goal == "big goal"


@pytest.mark.asyncio
async def test_terminal_tool_result_stops_without_extra_llm_roundtrip():
    async def invoker(name, args):  # noqa: ANN001
        return {
            "status": "failed",
            "message": "工作流部分失败，但已保留可恢复结果。",
            "_omni_control": {"terminal": True},
        }

    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {"x": "hi"})]),
        ChatWithToolsResult(content="should not be used"),
    ])
    agent = ReActLoopAgent(llm, invoker, max_iterations=4)

    res = await agent.run(system_prompt="sys", user_message="hello", tools=[ECHO])

    assert res.kind == "text"
    assert res.content == "工作流部分失败，但已保留可恢复结果。"
    assert llm.calls == 1
