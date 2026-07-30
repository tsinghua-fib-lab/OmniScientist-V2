"""A run that hits its budget must deliver, disclose, and route.

The "complex tasks fail at max iterations" reports came from a prompt
sub-agent built with progress control switched off wholesale: it could not stop
when its tool calls stopped advancing, it could not force a final answer at the
ceiling, and the bounded stop was then advertised as retryable — so the
workflow layer replayed the same run under the same budget.
"""

from __future__ import annotations

import pytest

from omni.core.llm.client import ChatWithToolsResult, LLMClient, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.termination import (
    BUDGET_EXHAUSTED_REASONS,
    TERMINATION_LABELS,
    terminal_outcome,
    termination_next_action,
)


class _ScriptedLLM(LLMClient):
    """Replays a fixed list of results, repeating the last one forever."""

    model = "scripted"

    def __init__(self, results: list[ChatWithToolsResult]) -> None:
        self._results = results
        self.calls = 0
        self.no_tool_calls = 0

    async def chat_with_tools(self, messages, tools, **_kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if not tools:
            self.no_tool_calls += 1
            return ChatWithToolsResult(content="Best-effort answer from observations.")
        idx = min(self.calls - 1, len(self._results) - 1)
        return self._results[idx]

    async def chat(self, system, user, *, temperature=0.3):  # noqa: ANN001
        return "text"

    async def embed(self, texts):  # noqa: ANN001
        return [[0.0] for _ in texts]


def _unknown_tool_call(idx: int) -> ChatWithToolsResult:
    return ChatWithToolsResult(
        tool_calls=[ToolCall(id=f"c{idx}", name=f"invented_tool_{idx}", arguments={"i": idx})]
    )


async def _noop_invoker(name, arguments):  # noqa: ANN001
    return {"status": "ok"}


@pytest.mark.asyncio
async def test_prompt_subagent_style_loop_forces_a_real_answer_at_the_ceiling():
    """Whatever bound a sub-agent hits, it hands back an answer, not a stub.

    The prompt sub-agent keeps the tool's typed ``needs_input`` payload, which is
    the one thing a caller varies. It does not get to opt out of writing a real
    final answer — a sub-agent that returned "reached the iteration limit" was
    the shape of the eight-iteration paper-review failures.
    """
    llm = _ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall(id="c1", name="echo", arguments={})])
    ])
    agent = ReActLoopAgent(
        llm,
        _noop_invoker,
        max_iterations=2,
        max_tool_calls=10,
        compose_needs_input=False,
    )

    result = await agent.run(
        system_prompt="s",
        user_message="u",
        tools=[ToolSpec(name="echo", description="d", parameters={"type": "object"})],
    )

    assert llm.no_tool_calls == 1, "the ceiling must trigger one tool-free synthesis pass"
    assert result.terminated_reason == "synthesized_max_iterations"
    assert "Best-effort answer" in result.content


@pytest.mark.asyncio
async def test_a_model_inventing_new_tool_names_stops_before_burning_the_budget():
    """Each invented name looks unique, so only a fault-kind ledger catches it."""
    llm = _ScriptedLLM([_unknown_tool_call(i) for i in range(1, 40)])
    agent = ReActLoopAgent(
        llm,
        _noop_invoker,
        max_iterations=30,
        max_tool_calls=60,
    )

    result = await agent.run(
        system_prompt="s",
        user_message="u",
        tools=[ToolSpec(name="echo", description="d", parameters={"type": "object"})],
    )

    assert result.total_iterations < 30, "the run must not consume its whole ceiling"
    assert "no_progress" in result.terminated_reason


@pytest.mark.asyncio
async def test_one_failing_skill_does_not_open_the_breaker_on_the_shared_router():
    """``run_skill`` dispatches every skill; keying the breaker by tool name alone
    would let one broken skill disable all the others."""
    from omni.core.react_agent import _circuit_key

    broken = ToolCall(id="a", name="run_skill", arguments={"skill": "broken-one"})
    healthy = ToolCall(id="b", name="run_skill", arguments={"skill": "healthy-one"})

    assert _circuit_key(broken) != _circuit_key(healthy)
    assert _circuit_key(ToolCall(id="c", name="echo", arguments={})) == "echo"


@pytest.mark.parametrize("reason", sorted(BUDGET_EXHAUSTED_REASONS))
def test_every_budget_stop_names_the_action_that_lifts_it(reason):
    assert termination_next_action(reason), f"{reason} offers no way forward"


@pytest.mark.parametrize("reason", sorted(TERMINATION_LABELS))
def test_terminal_outcome_summary_is_never_empty(reason):
    outcome = terminal_outcome(kind="partial", reason=reason, provider="paper-review")

    assert outcome.label
    assert "paper-review" in outcome.summary()


def test_budget_exhaustion_is_reported_as_degraded_not_failed():
    outcome = terminal_outcome(kind="partial", reason="max_iterations")

    assert outcome.status == "degraded"
    assert outcome.is_bounded
