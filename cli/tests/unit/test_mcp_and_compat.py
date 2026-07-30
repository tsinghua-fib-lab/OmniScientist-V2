"""MCP server dispatch + Codex/Claude integration writers."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from omni.agent import OmniAgent
from omni.compat import integrations
from omni.compat.mcp_server import _dispatch
from omni.config import load_settings
from omni.skills_runtime.manifest import ExecSpec, SkillEntry, SkillKind


@pytest.mark.asyncio
async def test_mcp_dispatch_list_skills_and_skill():
    agent = await OmniAgent.create(load_settings())
    script = "import sys,json;d=json.load(sys.stdin);print(json.dumps({'status':'ok','n':d.get('n')}))"
    agent.registry.register(SkillEntry(
        name="echo_skill", description="d", kind=SkillKind.CLI_EXEC,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    ))
    listed = await _dispatch(agent, "omni_list_skills", {})
    assert any(s["name"] == "echo_skill" for s in listed["skills"])

    out = await _dispatch(agent, "echo_skill", {"n": 5})
    assert out == {"status": "ok", "n": 5}
    await agent.aclose()


@pytest.mark.asyncio
async def test_mcp_skill_dispatch_enforces_concrete_output_contract():
    agent = await OmniAgent.create(load_settings())
    script = "print('{\"status\":\"ok\",\"count\":\"wrong\"}')"
    agent.registry.register(SkillEntry(
        name="bad_contract_skill",
        description="d",
        kind=SkillKind.CLI_EXEC,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={
            "type": "object",
            "properties": {"status": {"const": "ok"}, "count": {"type": "integer"}},
            "required": ["status", "count"],
            "additionalProperties": False,
        },
    ))

    out = await _dispatch(agent, "bad_contract_skill", {})

    assert out["reason"] == "output_contract_violation"
    assert out["execution_started"] is True
    await agent.aclose()


@pytest.mark.asyncio
async def test_mcp_omni_ask_runs_turn():
    agent = await OmniAgent.create(load_settings())
    out = await _dispatch(agent, "omni_ask", {"prompt": "hi"})
    assert "answer" in out
    await agent.aclose()


@pytest.mark.asyncio
async def test_mcp_omni_ask_preserves_needs_input_boundary():
    class Agent:
        async def handle_turn(self, *_args, **_kwargs):
            return SimpleNamespace(
                text="Configure VLM with `omni config vlm`.",
                kind="needs_input",
                terminated_reason="vlm_not_configured",
                tool_trace=[],
                submitted_workflow_ids=[],
                submitted_subtask_ids=[],
                drained_results=[],
            )

    out = await _dispatch(Agent(), "omni_ask", {"prompt": "make an editable figure"})

    assert out["kind"] == "needs_input"
    assert out["terminated_reason"] == "vlm_not_configured"


def test_register_with_codex_and_claude_idempotent():
    p1 = integrations.register_with_codex()
    p1b = integrations.register_with_codex()
    assert p1 == p1b and p1.is_file()
    data = p1.read_text()
    assert "omniscientist" in data and "mcp" in data

    p2 = integrations.register_with_claude()
    payload = json.loads(p2.read_text())
    assert payload["mcpServers"]["omniscientist"]["command"] == "omni"


def test_emit_agents_md(tmp_path):
    path = integrations.emit_agents_md(tmp_path)
    assert path.is_file()
    assert "omni_ask" in path.read_text()


def test_discovery_report_shape():
    rep = integrations.discovery_report(load_settings())
    assert "user_claude (Claude Code)" in rep
    assert all("path" in v and "exists" in v for v in rep.values())
