"""What a tool budget may bound, and what it must never bound.

A per-tool quota is a cost control. It belongs on tools that *consume* something
scarce — a literature search, a web fetch, a shell command. It does not belong
on the tools that *emit the deliverable*: capping how many files a review may
write caps the review itself, and no number is right because the number is
decided by the content.

Neither Codex, OpenClaw nor OpenCode caps invocations of a specific tool; they
bound context, gate danger, and detect *repetition*. These tests hold Omni to
the same line, and to the three mechanics a quota needs to stay honest when it
does fire: a refused call costs nothing, says what to do instead, and cannot be
retried forever.
"""

from __future__ import annotations

import pytest

from omni.core.execution_budget import ToolExecutionBudget
from omni.core.llm.client import ChatWithToolsResult, LLMClient, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.tool_policy import ToolPolicyGuard, policy_violation
from omni.skills_runtime.manifest import DELIVERABLE_TOOLS, tool_limit_warnings


class _RepeatingLLM(LLMClient):
    """Keeps calling one tool with fresh arguments, as a stuck model does."""

    model = "scripted"

    def __init__(self, tool: str) -> None:
        self._tool = tool
        self.calls = 0
        self.no_tool_calls = 0

    async def chat_with_tools(self, messages, tools, **_kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if not tools:
            self.no_tool_calls += 1
            return ChatWithToolsResult(content="Answer assembled from what was gathered.")
        return ChatWithToolsResult(
            tool_calls=[
                ToolCall(
                    id=f"c{self.calls}",
                    name=self._tool,
                    arguments={"path": f"section-{self.calls}.md"},
                )
            ]
        )

    async def chat(self, system, user, *, temperature=0.3):  # noqa: ANN001
        return "text"

    async def embed(self, texts):  # noqa: ANN001
        return [[0.0] for _ in texts]


# ── what a quota may bound ───────────────────────────────────────────────


def test_capping_a_deliverable_tool_is_an_authoring_defect():
    """The count of output files is decided by the content, not by a manifest."""
    warnings = tool_limit_warnings({"write_file": 10, "cite_source": 8}, "paper-review")

    assert warnings
    assert any("write_file" in w for w in warnings)
    assert any("cite_source" in w for w in warnings)


def test_capping_an_acquisition_tool_is_accepted():
    """Bounding how much a skill searches or fetches is a real cost control."""
    assert tool_limit_warnings({"search_corpus": 4, "web_fetch": 3, "bash": 12}, "s") == []


def test_no_builtin_skill_caps_its_own_deliverable():
    from omni.config import load_settings
    from omni.skills_runtime.registry import SkillRegistry

    registry = SkillRegistry(load_settings())
    registry.build_index()

    offenders: list[str] = []
    for entry in registry.list_all():
        limits = (entry.execution or {}).get("tool_limits") or {}
        offenders.extend(
            f"{entry.name}:{tool}" for tool in limits if tool in DELIVERABLE_TOOLS
        )

    assert offenders == []


# ── how a quota must behave when it does fire ────────────────────────────


def test_unbounded_execution_budget_admits_without_becoming_exhausted():
    budget = ToolExecutionBudget(None)

    assert budget.admit(1000) == 1000
    budget.mark_completed(1000)

    assert budget.remaining is None
    assert budget.exhausted is False
    assert budget.snapshot()["limit"] is None
    assert budget.snapshot()["enforced"] is False


def test_unbounded_child_reports_the_parent_budget_that_actually_constrains_it():
    parent = ToolExecutionBudget(2)
    child = ToolExecutionBudget(None, parent=parent)

    assert child.admit(3) == 2

    assert child.remaining == 0
    assert child.exhausted is True
    assert child.snapshot()["enforced"] is True
    assert child.snapshot()["remaining"] == 0


def test_a_refused_call_does_not_spend_the_budget_it_was_refused_by():
    """Charging for work that never ran lets a model exhaust a run by retrying."""
    guard = ToolPolicyGuard(max_tool_calls=10, per_tool_limits={"web_fetch": 1})

    assert guard.rejection("web_fetch") is None
    for _ in range(5):
        assert guard.rejection("web_fetch") is not None

    # Five refusals later, the shared budget is untouched: nine calls remain.
    for _ in range(9):
        assert guard.rejection("bash") is None
    assert guard.rejection("bash") is not None


@pytest.mark.parametrize(
    "reason",
    [
        "tool_limit_exceeded:3",
        "max_tool_calls_exceeded:40",
        "not_in_allowed_tools",
        "blocked_by_plan",
        "something_new_we_have_not_seen",
    ],
)
def test_every_refusal_says_what_the_model_may_do_instead(reason):
    """A wall with no door is what makes a model re-issue the same call."""
    rejection = policy_violation("web_fetch", reason)

    assert rejection["remedy"]
    # The remedy has to travel to the model on the observation, not sit in a log.
    assert rejection["remedy"] in str(rejection["error"])


def test_an_exhausted_budget_offers_a_way_to_conclude():
    """The only real move left is to deliver what was already gathered."""
    remedy = policy_violation("web_fetch", "tool_limit_exceeded:3")["remedy"]

    assert "finish with the results you have" in remedy


@pytest.mark.asyncio
async def test_repeated_refusals_end_the_run_instead_of_burning_the_ceiling():
    """A host refusal is deterministic: re-issuing it can never start working.

    Each retry carries a different path, so the signature-keyed no-progress
    detector sees fresh work every time. Only a ledger keyed on the *kind* of
    refusal catches this.
    """
    llm = _RepeatingLLM("write_file")
    guard = ToolPolicyGuard(per_tool_limits={"write_file": 1})

    async def invoker(name, arguments):  # noqa: ANN001, ARG001
        rejection = guard.rejection(name)
        return rejection if rejection is not None else {"status": "ok"}

    agent = ReActLoopAgent(llm, invoker, max_iterations=30, max_tool_calls=60)
    result = await agent.run(
        system_prompt="s",
        user_message="u",
        tools=[ToolSpec(name="write_file", description="d", parameters={"type": "object"})],
    )

    assert result.total_iterations < 30, "the run consumed its whole ceiling on refusals"
    assert result.content, "a stopped run must still answer"
