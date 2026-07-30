"""MCP server bridge — exposes OmniScientist to Claude Code / Codex.

Run with ``omni mcp serve`` (stdio transport). Registered tools:

- one tool per discovered skill (sync or async), executed to completion and
  returned synchronously (MCP consumers expect a synchronous result);
- ``omni_ask`` — run a full OmniScientist research turn for a free-form prompt;
- ``omni_list_skills`` — introspection.

This is the "our skills → their agents" direction of bi-directional
compatibility. Requires the ``mcp`` extra (``pip install OmniScientist-V2[mcp]``).
"""

from __future__ import annotations

import json
import logging
import traceback
from typing import Any

from omni.agent.orchestrator import OmniAgent
from omni.config.settings import OmniSettings
from omni.skills_runtime.executor import execute_skill

logger = logging.getLogger(__name__)

_GENERIC_SCHEMA = {
    "type": "object",
    "properties": {"input": {"type": "string", "description": "Task input or query"}},
}


async def serve_stdio(settings: OmniSettings) -> None:
    try:
        import mcp.types as types
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError(
            "MCP support requires the 'mcp' package. Install with: pip install 'OmniScientist-V2[mcp]'"
        ) from exc

    agent = await OmniAgent.create(settings)
    server = Server("omniscientist")

    def _skill_tools() -> list[types.Tool]:
        tools: list[types.Tool] = []
        for entry in agent.registry.list_selectable():
            schema = entry.input_schema if entry.input_schema.get("properties") else _GENERIC_SCHEMA
            tools.append(types.Tool(
                name=entry.name,
                description=(entry.short_desc(400) or entry.name),
                inputSchema=schema,
            ))
        return tools

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:  # noqa: ANN202
        extra = [
            types.Tool(
                name="omni_ask",
                description="Ask the OmniScientist research agent a free-form question with planning, skills, and memory.",
                inputSchema={
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                    "required": ["prompt"],
                },
            ),
            types.Tool(
                name="omni_list_skills",
                description="List research skills available to OmniScientist.",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]
        return extra + _skill_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:  # noqa: ANN202
        arguments = arguments or {}
        try:
            result = await _dispatch(agent, name, arguments)
        except Exception as exc:  # noqa: BLE001
            result = _safe_mcp_failure(name, exc)
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        return [types.TextContent(type="text", text=text)]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


async def _dispatch(agent: OmniAgent, name: str, arguments: dict[str, Any]) -> Any:
    if name == "omni_ask":
        turn = await agent.handle_turn(str(arguments.get("prompt", "")), channel="mcp")
        out: dict[str, Any] = {
            "answer": turn.text,
            "kind": turn.kind,
            "terminated_reason": turn.terminated_reason,
            "tools_used": [record.name for record in turn.tool_trace],
        }
        if turn.drained_results:
            out["task_results"] = turn.drained_results
        artifacts = [
            artifact.to_dict()
            for artifact in (getattr(turn, "artifacts", []) or [])
            if hasattr(artifact, "to_dict")
        ]
        if artifacts:
            out["artifacts"] = artifacts
        return out
    if name == "omni_list_skills":
        return {
            "skills": [
                {
                    "name": entry.name,
                    "description": entry.short_desc(160),
                    "delivery": entry.delivery_mode.value,
                }
                for entry in agent.registry.list_selectable()
            ]
        }
    entry = agent.registry.get(name)
    if entry is None:
        return {"status": "error", "error": f"unknown tool '{name}'"}
    ctx = agent._make_ctx(session_id="", channel="mcp")
    return await execute_skill(entry, arguments, ctx)


def _safe_mcp_failure(name: str, exc: BaseException) -> dict[str, Any]:
    """Log useful local location data without exposing exception text to MCP."""
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    location = "unknown"
    if frames:
        frame = frames[-1]
        location = f"{frame.name}@{frame.filename}:{frame.lineno}"
    logger.error(
        "mcp call_tool failed: tool=%s type=%s location=%s",
        name,
        type(exc).__name__,
        location,
    )
    return {
        "status": "error",
        "error": "Omni MCP tool execution failed; inspect owner-local diagnostics.",
        "error_info": {
            "code": "mcp_dispatch_failed",
            "category": "internal",
            "retryable": False,
        },
    }
