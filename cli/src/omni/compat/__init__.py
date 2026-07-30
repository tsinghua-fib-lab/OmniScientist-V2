"""Claude Code / Codex / MCP compatibility layer.

- ``mcp_server``: expose OmniScientist skills + ``omni_ask`` as MCP tools so
  Claude Code / Codex can call them (our skills → their agents).
- ``mcp_client``: consume external MCP servers as OmniScientist tools
  (their tools → our agent).
- ``integrations``: write the ``omni`` MCP server into Codex/Claude config,
  and emit ``AGENTS.md`` for shared project guidance.
"""

__all__ = ["mcp_server", "mcp_client", "integrations"]
