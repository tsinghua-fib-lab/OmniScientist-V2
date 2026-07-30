"""Deferring a schema must cost tokens, never reach.

Phase 3 stops advertising 19 usually-idle tools. That is only safe because a
turn keeps three ways back to any of them:

* **A** — call it by name; the loop dispatches from the full list (Phase 2).
* **B** — look its parameters up with ``find_skill`` before calling it.
* **C** — once one has run, its schema is advertised for the rest of the turn.

The prompt also has to *say* the deferred tools exist, or the model routes
around a capability it cannot see. These tests pin all of that, plus the
boundary: policy denial still removes reach, whatever the exposure says.
"""

from __future__ import annotations

from typing import Any

import pytest

from omni.agent.intent_plan import ToolPolicy
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.system_prompt import build_system_prompt, render_tool_catalog
from omni.core.tool_exposure import DEFERRED_TOOLS, apply_default_exposure
from omni.core.tool_policy import filter_tools_for_policy
from omni.skills_runtime.context import Tool
from tests.agent.test_advertised_vs_dispatchable import ToolCapturingLLM

_SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}


def _tool(name: str) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        return {"ran": name, **args}

    return Tool(ToolSpec(name, f"{name} does a thing.", _SCHEMA), handler)


async def _invoker(name: str, args: dict[str, Any]) -> Any:
    return {"ran": name, **args}


# ── the default exposure decision ────────────────────────────────────────────


def test_the_deferred_set_is_a_named_list_not_an_inferred_stage():
    # Keyed to capability families, so it is reviewable and reversible by name.
    assert len(DEFERRED_TOOLS) == 18
    # The recognisable entry point of each deferred family stays advertised.
    assert "schedule_task" not in DEFERRED_TOOLS
    assert "list_schedules" in DEFERRED_TOOLS
    # Nothing the turn leans on is deferred.
    for hot in ("bash", "read_file", "write_file", "update_plan", "run_skill",
                "search_corpus", "search_literature", "find_skill"):
        assert hot not in DEFERRED_TOOLS, hot


def test_a_discretionary_tool_is_not_deferred_however_rare_it_is():
    """Deferral may cost tokens; it may not cost behaviour.

    ``spawn_subagents`` was the strongest candidate on frequency alone — one call
    in 720, and the second-largest schema on the surface — and is deliberately
    excluded. A model delegates because it can see the tool, so withholding the
    schema suppresses delegation rather than deferring its cost. That is a
    behaviour change, which is exactly what deferral promises not to be.
    """
    assert "spawn_subagents" not in DEFERRED_TOOLS


def test_applying_exposure_drops_nothing():
    """Deferral changes advertising only — the list keeps every name and order."""
    tools = [_tool("read_file"), _tool("log_run"), _tool("bash")]
    before = [t.spec.name for t in tools]
    after = apply_default_exposure(tools)
    assert [t.spec.name for t in after] == before
    assert {t.spec.name: t.spec.exposure for t in after} == {
        "read_file": "direct",
        "log_run": "deferred",
        "bash": "direct",
    }


# ── the prompt says what it does not send ────────────────────────────────────


def test_the_catalog_names_deferred_tools_so_the_model_knows_they_exist():
    tools = [t.spec for t in apply_default_exposure([_tool("read_file"), _tool("log_run")])]
    block = render_tool_catalog(tools)
    assert "read_file" in block
    assert "log_run" in block
    assert "find_skill" in block  # how to get the parameters
    # And they are named as a distinct group, not mixed into the advertised list.
    assert block.index("read_file") < block.index("schemas omitted")
    assert block.index("schemas omitted") < block.index("log_run")


def test_a_turn_with_nothing_deferred_says_nothing_about_deferral():
    block = render_tool_catalog([_tool("read_file").spec])
    assert "schemas omitted" not in block


def test_tool_use_rules_permit_the_deferred_tools_they_name():
    """The rules must not contradict the catalog by forbidding unlisted tools."""
    tools = [t.spec for t in apply_default_exposure([_tool("read_file"), _tool("log_run")])]
    prompt = build_system_prompt(role="R", tools=tools, project_name="p")
    assert "schemas omitted" in prompt
    assert "An omitted tool is not permitted" not in prompt


# ── recovery path A: name it and it runs ─────────────────────────────────────


@pytest.mark.asyncio
async def test_path_a_a_deferred_tool_runs_when_named_without_a_lookup():
    tools = [t.spec for t in apply_default_exposure([_tool("read_file"), _tool("log_run")])]
    llm = ToolCapturingLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "log_run", {"x": "seed=1"})]),
        ChatWithToolsResult(content="recorded"),
    ])
    agent = ReActLoopAgent(llm, _invoker, max_iterations=4)
    result = await agent.run(system_prompt="sys", user_message="record it", tools=tools)

    assert "log_run" not in llm.advertised[0]
    assert result.tool_trace[0].status == "succeeded"
    assert result.tool_trace[0].error_code != "unknown_tool"


# ── recovery path C: running one advertises it from then on ──────────────────


@pytest.mark.asyncio
async def test_path_c_a_deferred_tool_that_runs_is_advertised_afterwards():
    tools = [t.spec for t in apply_default_exposure([_tool("read_file"), _tool("log_run")])]
    llm = ToolCapturingLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "log_run", {"x": "1"})]),
        ChatWithToolsResult(tool_calls=[ToolCall("c2", "log_run", {"x": "2"})]),
        ChatWithToolsResult(content="done"),
    ])
    agent = ReActLoopAgent(llm, _invoker, max_iterations=5)
    await agent.run(system_prompt="sys", user_message="go", tools=tools)

    assert len(llm.advertised) >= 2
    assert "log_run" not in llm.advertised[0], "should start withheld"
    assert "log_run" in llm.advertised[1], "should be advertised once it has run"


@pytest.mark.asyncio
async def test_a_rejected_name_is_never_promoted_into_the_advertised_set():
    """Promotion follows execution, so an invented name cannot buy itself a slot."""
    tools = [t.spec for t in apply_default_exposure([_tool("read_file")])]
    llm = ToolCapturingLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "not_a_tool", {})]),
        ChatWithToolsResult(tool_calls=[ToolCall("c2", "read_file", {"x": "a"})]),
        ChatWithToolsResult(content="done"),
    ])
    agent = ReActLoopAgent(llm, _invoker, max_iterations=5)
    await agent.run(system_prompt="sys", user_message="go", tools=tools)
    for advertised in llm.advertised:
        assert "not_a_tool" not in advertised


# ── the boundary survives deferral ───────────────────────────────────────────


def test_policy_denial_still_wins_over_the_default_deferred_set():
    """A denied tool is gone from the list; exposure never argues with that."""
    tools = apply_default_exposure([_tool("read_file"), _tool("run_compute")])
    assert {t.spec.name: t.spec.exposure for t in tools}["run_compute"] == "deferred"

    visible = filter_tools_for_policy(tools, ToolPolicy(blocked_tools=["run_compute"]))
    assert {t.spec.name for t in visible} == {"read_file"}


@pytest.mark.asyncio
async def test_an_allowlist_still_bounds_the_turn_even_though_tools_are_deferred():
    tools = apply_default_exposure(
        [_tool("read_file"), _tool("log_run"), _tool("cite_source")]
    )
    visible = filter_tools_for_policy(tools, ToolPolicy(allowed_tools=["read_file"]))
    assert {t.spec.name for t in visible} == {"read_file"}


# ── recovery path B: find_skill returns the withheld parameters ───────────────


@pytest.mark.asyncio
async def test_path_b_find_skill_returns_the_schema_of_a_withheld_tool(tmp_path):
    from omni.agent.tool_surface import ToolSurfaceBuilder
    from omni.config import load_settings
    from omni.skills_runtime.context import ExecContext
    from omni.skills_runtime.registry import SkillRegistry

    settings = load_settings()
    registry = SkillRegistry(settings)
    registry.build_index()

    async def _no_mcp(_ctx: ExecContext) -> list[Tool]:
        return []

    builder = ToolSurfaceBuilder(runtime=None, tasks=None, registry=registry, mcp_loader=_no_mcp)
    ctx = ExecContext(settings=settings, paths=settings.paths, channel="cli", db=None)
    tools = await builder.build(ctx, wait_for_tasks=True)

    by_name = {t.spec.name: t for t in tools}
    # The surface really does defer something, and keeps it reachable.
    deferred = {name for name, t in by_name.items() if t.spec.exposure == "deferred"}
    assert deferred, "nothing was deferred on the coordinator surface"

    target = sorted(deferred)[0]
    found = await by_name["find_skill"].handler({"query": target})
    unlisted = {entry["name"]: entry for entry in found.get("unlisted_tools", [])}
    assert target in unlisted
    assert unlisted[target]["parameters"] == by_name[target].spec.parameters


@pytest.mark.asyncio
async def test_find_skill_still_answers_skill_queries_without_leaking_tools():
    from omni.agent.tool_surface import ToolSurfaceBuilder
    from omni.config import load_settings
    from omni.skills_runtime.context import ExecContext
    from omni.skills_runtime.registry import SkillRegistry

    settings = load_settings()
    registry = SkillRegistry(settings)
    registry.build_index()

    async def _no_mcp(_ctx: ExecContext) -> list[Tool]:
        return []

    builder = ToolSurfaceBuilder(runtime=None, tasks=None, registry=registry, mcp_loader=_no_mcp)
    ctx = ExecContext(settings=settings, paths=settings.paths, channel="cli", db=None)
    tools = await builder.build(ctx, wait_for_tasks=True)
    find_skill = {t.spec.name: t for t in tools}["find_skill"]

    found = await find_skill.handler({"query": "zzz_no_such_thing_zzz"})
    assert found["matches"] == []
    assert not found.get("unlisted_tools")
