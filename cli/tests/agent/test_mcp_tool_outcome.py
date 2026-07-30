from __future__ import annotations

from types import SimpleNamespace

import pytest

from omni.compat import mcp_client
from omni.core.tool_result import ToolResultEnvelope, tool_result_failure


@pytest.mark.asyncio
async def test_mcp_is_error_controls_outcome_without_parsing_content(monkeypatch) -> None:
    async def fake_with_session(_cfg, _fn):  # noqa: ANN001
        return SimpleNamespace(
            content=[SimpleNamespace(text='{"status":"ok","detail":"remote failed"}')],
            isError=True,
        )

    monkeypatch.setattr(mcp_client, "_with_session", fake_with_session)
    remote = SimpleNamespace(
        name="lookup",
        description="remote lookup",
        inputSchema={"type": "object", "properties": {}},
        annotations={"readOnlyHint": True},
    )
    tool = mcp_client._wrap_remote_tool(  # noqa: SLF001
        "server",
        SimpleNamespace(),
        remote,
    )

    result = await tool.handler({})

    assert isinstance(result, ToolResultEnvelope)
    assert result.event_output["is_error"] is True
    assert tool_result_failure(result) == (
        "failed",
        '{"status":"ok","detail":"remote failed"}',
    )
