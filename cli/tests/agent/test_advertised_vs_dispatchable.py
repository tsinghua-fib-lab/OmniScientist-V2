"""What a turn advertises is not what a turn can run.

This file exists because of a real incident: to save per-iteration tokens
``write_file`` was withheld from the tool catalog, and because one list served
both as the advertised schemas and as the dispatch index, withholding it also
removed it. The model did an entire research turn and was then told
``unknown tool 'write_file'`` when it tried to save the paper.

The fix is the separation Codex already makes — ``build_model_visible_specs``
filters advertised specs by exposure while ``ToolRegistry::tool`` dispatches by
name without consulting it. These tests pin that separation at both gates that
can reject a name in this codebase:

* :meth:`ReActLoopAgent._dispatch_tool`, which checks its own ``tools_by_name``
  before invoking anything;
* :meth:`ToolGateway.invoker`, which checks the gateway's index.

They also pin the boundary that keeps the separation safe: *deferral* hides a
schema, *denial* removes reach, and a tool denied by policy must stay
unreachable however it is named.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omni.agent.intent_plan import ToolPolicy
from omni.core.llm.client import ChatWithToolsResult, LLMClient, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.tool_policy import filter_tools_for_policy
from omni.runtime.tool_gateway import ToolGateway
from omni.skills_runtime.context import Tool

_SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}}

# The incident, as data: a tool the turn would rather not pay to advertise on
# every iteration, and which the model nevertheless needs before it finishes.
WRITE_FILE = ToolSpec("write_file", "Write a file.", _SCHEMA, exposure="deferred")
READ_FILE = ToolSpec("read_file", "Read a file.", _SCHEMA)


class ToolCapturingLLM(LLMClient):
    """Records the ``tools`` array of every request, then replies from a script."""

    def __init__(self, script: list[ChatWithToolsResult]) -> None:
        self.model = "capturing"
        self._script = list(script)
        self.advertised: list[list[str]] = []

    async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatWithToolsResult:
        self.advertised.append([t["function"]["name"] for t in tools])
        if self._script:
            return self._script.pop(0)
        return ChatWithToolsResult(content="done")

    async def chat(self, system: str, user: str, **kwargs: Any) -> str:
        return "summary"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


async def _invoker(name: str, args: dict[str, Any]) -> Any:
    return {"wrote": args.get("path"), "tool": name}


# ── gate 1: the ReAct loop's own name check ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_deferred_tool_is_not_advertised_but_still_executes_when_named():
    """The write_file incident, reproduced and then made impossible.

    Fails before the split: the loop derives both the advertised array and its
    dispatch index from the same ``tools`` argument, so a tool it does not
    advertise is a tool it cannot run, and this returns ``unknown_tool``.
    """
    llm = ToolCapturingLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "write_file", {"path": "paper.md"})]),
        ChatWithToolsResult(content="saved"),
    ])
    agent = ReActLoopAgent(llm, _invoker, max_iterations=4)
    result = await agent.run(
        system_prompt="sys", user_message="write the paper", tools=[READ_FILE, WRITE_FILE]
    )

    # Not advertised: its schema never reached the provider.
    assert llm.advertised, "the loop made no request"
    assert "write_file" not in llm.advertised[0]
    assert "read_file" in llm.advertised[0]

    # Still ran: naming it was enough.
    assert result.kind == "text"
    assert result.tool_names() == ["write_file"]
    record = result.tool_trace[0]
    assert record.status == "succeeded", record.error
    assert record.error_code != "unknown_tool"


@pytest.mark.asyncio
async def test_an_unknown_name_is_still_rejected_and_lists_only_advertised_tools():
    """Deferral must not turn the loop into a tool that accepts any name."""
    llm = ToolCapturingLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "no_such_tool", {})]),
        ChatWithToolsResult(content="gave up"),
    ])
    agent = ReActLoopAgent(llm, _invoker, max_iterations=4)
    result = await agent.run(
        system_prompt="sys", user_message="go", tools=[READ_FILE, WRITE_FILE]
    )
    record = result.tool_trace[0]
    assert record.status == "rejected"
    assert record.error_code == "unknown_tool"


# ── gate 2: the gateway's dispatch index ─────────────────────────────────────


def _tool(spec: ToolSpec) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        return {"ran": spec.name, **args}

    return Tool(spec, handler)


@pytest.mark.asyncio
async def test_the_gateway_dispatches_a_tool_it_does_not_advertise():
    """Fails before the split: ``tool_specs`` was the only view of the tool list,
    so there was no way to advertise less than the gateway could run.
    """
    gateway = ToolGateway(
        task_id="t1", tools=[_tool(READ_FILE), _tool(WRITE_FILE)], event_family="react"
    )

    advertised = {spec.name for spec in gateway.model_visible_specs()}
    assert advertised == {"read_file"}

    # Everything that passed policy stays reachable, advertised or not.
    dispatchable = {spec.name for spec in gateway.tool_specs}
    assert dispatchable == {"read_file", "write_file"}
    assert advertised <= dispatchable

    result = await gateway.invoker()("write_file", {"path": "paper.md"})
    assert result["ran"] == "write_file"


# ── the boundary: deferral hides, denial removes ─────────────────────────────


@pytest.mark.asyncio
async def test_a_policy_blocked_tool_cannot_be_reached_by_naming_it():
    """Denial and deferral must stay two mechanisms.

    ``filter_tools_for_policy`` keeps meaning *blocked and unreachable*. Marking
    a blocked tool ``deferred`` must not smuggle it back into reach.
    """
    blocked_but_deferred = ToolSpec(
        "bash", "Run a shell command.", _SCHEMA, exposure="deferred"
    )
    policy = ToolPolicy(blocked_tools=["bash"])
    visible = filter_tools_for_policy([_tool(READ_FILE), _tool(blocked_but_deferred)], policy)
    assert {t.spec.name for t in visible} == {"read_file"}

    gateway = ToolGateway(
        task_id="t1", tools=visible, policy=policy, event_family="react"
    )
    assert "bash" not in {spec.name for spec in gateway.tool_specs}
    assert "bash" not in {spec.name for spec in gateway.model_visible_specs()}

    rejection = await gateway.invoker()("bash", {"command": "rm -rf /"})
    payload = rejection if isinstance(rejection, dict) else json.loads(str(rejection))
    assert payload.get("status") == "rejected" or payload.get("error")


@pytest.mark.asyncio
async def test_the_react_loop_also_refuses_a_policy_denied_tool_it_never_received():
    """The loop cannot dispatch what policy removed before it was handed over."""
    llm = ToolCapturingLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "bash", {"command": "ls"})]),
        ChatWithToolsResult(content="refused"),
    ])
    agent = ReActLoopAgent(llm, _invoker, max_iterations=4)
    result = await agent.run(system_prompt="sys", user_message="go", tools=[READ_FILE])
    record = result.tool_trace[0]
    assert record.status == "rejected"
    assert record.error_code == "unknown_tool"


# ── Phase 2 ships with nothing deferred: the refactor changes no behaviour ────


def test_every_shipped_tool_is_advertised_until_a_phase_marks_it_deferred():
    """The split lands inert: production surfaces still advertise everything."""
    from omni.config import load_settings
    from omni.skills_runtime.builtin_tools import build_builtin_tools
    from omni.skills_runtime.context import ExecContext

    settings = load_settings()
    ctx = ExecContext(settings=settings, paths=settings.paths, channel="cli", db=None)
    specs = [t.spec for t in build_builtin_tools(ctx)]
    assert specs, "no builtin tools were built"
    assert all(spec.exposure == "direct" for spec in specs)
