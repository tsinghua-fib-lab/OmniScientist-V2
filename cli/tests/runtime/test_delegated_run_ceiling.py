"""Cumulative quotas are explicit owner policy, and hitting one is visible.

Incident dc787efa: one misread turn routed to ``paper-review`` and spent 407k
tokens before settling ``degraded`` at the iteration limit. A sibling run,
0792bf0a, spent 446k the same way. Those were the only two delegated executions
in the whole local history that recorded usage, and both were runaways: measured
against every recorded run, the median is ~6.6k tokens and the p90 is ~69k, with
one healthy outlier at 196k.

The enforcement path remains available on every run, but the default is zero:
accounting stays enabled while a productive long task is governed by context
rollover and progress rather than a guessed cumulative token count.
"""

from __future__ import annotations

import pytest

from omni.agent.cost import react_usage_limits
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, LLMClient, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.termination import (
    execution_outcome_status,
    is_bounded_termination,
    termination_next_action,
)


class _ExpensiveLLM(LLMClient):
    """Reports heavy usage per turn, as a long research run does."""

    model = "scripted"

    def __init__(self, tokens_per_turn: int) -> None:
        self._tokens = tokens_per_turn
        self.calls = 0

    async def chat_with_tools(self, messages, tools, **_kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        usage = {
            "prompt_tokens": self._tokens,
            "completion_tokens": 0,
            "total_tokens": self._tokens,
        }
        if not tools:
            return ChatWithToolsResult(content="partial answer", usage=usage)
        return ChatWithToolsResult(
            tool_calls=[
                ToolCall(id=f"c{self.calls}", name="read_file", arguments={"path": "p"})
            ],
            usage=usage,
        )

    async def chat(self, system, user, *, temperature=0.3):  # noqa: ANN001
        return "text"

    async def embed(self, texts):  # noqa: ANN001
        return [[0.0] for _ in texts]


def test_accounting_is_enabled_without_a_default_cumulative_ceiling():
    cost = load_settings().cost

    assert cost.enabled
    assert cost.max_total_tokens == 0


def test_disabled_ceiling_reaches_the_loop_as_disabled():
    settings = load_settings()
    limits = react_usage_limits(settings, _ExpensiveLLM(1))

    assert limits["max_total_tokens"] == 0


@pytest.mark.asyncio
async def test_a_runaway_stops_at_the_ceiling_rather_than_the_iteration_limit():
    llm = _ExpensiveLLM(tokens_per_turn=40_000)

    async def invoker(name, arguments):  # noqa: ANN001, ARG001
        return {"status": "ok"}

    agent = ReActLoopAgent(
        llm,
        invoker,
        max_iterations=100,
        max_tool_calls=200,
        max_total_tokens=100_000,
    )
    result = await agent.run(
        system_prompt="s",
        user_message="u",
        tools=[ToolSpec(name="read_file", description="d", parameters={"type": "object"})],
    )

    assert result.terminated_reason.endswith("max_total_tokens")
    assert result.total_iterations < 100
    assert result.usage_budget["enforced"] is True


def test_reaching_the_ceiling_is_degraded_and_says_how_to_lift_it():
    """A spent budget is a bounded outcome, not a failure, and it has an exit."""
    assert is_bounded_termination("max_total_tokens")
    assert execution_outcome_status("partial", "max_total_tokens") == "degraded"
    assert "token budget" in termination_next_action("max_total_tokens")
