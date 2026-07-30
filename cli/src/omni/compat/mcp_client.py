"""MCP client — consume external MCP servers as OmniScientist tools.

This is the "their tools → our agent" direction: any MCP server configured
under ``[mcp_servers.<name>]`` becomes callable inside the ReAct loop. Each
configured server is introspected for its tool list; each tool is wrapped so
a call spins up a short-lived stdio (or SSE) session, invokes it, and returns
the text result. Connection-per-call keeps this robust and stateless.
"""

from __future__ import annotations

import logging
from typing import Any

from omni.config.settings import MCPServerCfg, OmniSettings
from omni.core.react_agent import ToolSpec
from omni.skills_runtime.context import Tool

logger = logging.getLogger(__name__)


async def _with_session(cfg: MCPServerCfg, fn):
    """Open a session to ``cfg`` (stdio or SSE), run ``fn(session)``, close."""
    from mcp import ClientSession

    if cfg.url:
        from mcp.client.sse import sse_client

        async with sse_client(cfg.url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)
    else:
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env or None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)


async def load_mcp_tools(settings: OmniSettings) -> list[Tool]:
    tools: list[Tool] = []
    for server_name, cfg in settings.mcp_servers.items():
        if not cfg.enabled or (not cfg.command and not cfg.url):
            continue
        try:
            listed = await _with_session(cfg, lambda s: s.list_tools())
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP server '%s' unreachable: %s", server_name, exc)
            continue
        for mt in getattr(listed, "tools", []) or []:
            tools.append(_wrap_remote_tool(server_name, cfg, mt))
    return tools


def _wrap_remote_tool(server_name: str, cfg: MCPServerCfg, mt: Any) -> Tool:
    qualified = f"{server_name}__{mt.name}"

    async def handler(args: dict) -> Any:
        async def _call(session):
            return await session.call_tool(mt.name, args)

        result = await _with_session(cfg, _call)
        parts = []
        for c in getattr(result, "content", []) or []:
            text = getattr(c, "text", None)
            parts.append(text if text is not None else str(c))
        return "\n".join(parts) if parts else "(empty result)"

    schema = getattr(mt, "inputSchema", None) or {"type": "object", "properties": {}}
    annotations = getattr(mt, "annotations", None)
    read_only = bool(
        annotations.get("readOnlyHint", False)
        if isinstance(annotations, dict)
        else getattr(annotations, "readOnlyHint", False)
    )
    spec = ToolSpec(
        name=qualified,
        description=f"[MCP:{server_name}] {getattr(mt, 'description', '') or mt.name}",
        parameters=schema,
        replay_safe=read_only,
    )
    return Tool(spec, handler, sensitive=not read_only, replay_safe=read_only)
