"""REPL slash commands for research verbs (/lit /verify /bench in-process)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from omni.cli.main import _parse_lit_args, _parse_verify_session, _repl_command
from omni.cli.state import AppState, make_agent
from omni.storage.models import SessionORM


def test_parse_lit_args():
    assert _parse_lit_args('"what is rag" --verify --k 8') == ("what is rag", 8, True, False)
    assert _parse_lit_args("plain question here -q") == ("plain question here", 0, False, True)


def test_parse_verify_session():
    assert _parse_verify_session("", "SESS") == ""  # whole workspace
    assert _parse_verify_session("--session", "SESS") == "SESS"  # bare → active
    assert _parse_verify_session("-s abcd1234", "SESS") == "abcd1234"


@pytest.mark.asyncio
async def test_repl_routes_research_verbs_in_process(monkeypatch):
    calls: list[tuple] = []

    async def fake_lit(agent, question, **kw):  # noqa: ANN001
        calls.append(("lit", question, kw))

    async def fake_verify(agent, **kw):  # noqa: ANN001
        calls.append(("verify", kw))

    monkeypatch.setattr("omni.cli.commands.lit_cmd.render_lit", fake_lit)
    monkeypatch.setattr("omni.cli.commands.verify_cmd.render_verify_report", fake_verify)
    # In-process means the external subprocess path must NOT be taken.
    import omni.cli.main as cli_main

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("research verb should run in-process, not via subprocess")

    monkeypatch.setattr(cli_main.subprocess, "run", _boom)

    state = AppState()
    agent = await make_agent(state)
    try:
        sid = await agent.ensure_session(channel="cli")
        await _repl_command(agent, state, '/lit "what is rag" --verify --k 8', sid)
        await _repl_command(agent, state, "/verify --session", sid)
    finally:
        await agent.aclose()
        from omni.storage.db import reset_databases

        await reset_databases()

    assert [c[0] for c in calls] == ["lit", "verify"]
    assert calls[0][1] == "what is rag"
    assert calls[0][2]["session_id"] == sid
    assert calls[0][2]["verify"] is True and calls[0][2]["k"] == 8
    assert calls[1][1]["session"] == sid  # bare --session → active REPL session


@pytest.mark.asyncio
async def test_inprocess_renderers_create_no_stray_sessions():
    from omni.cli.commands.bench_cmd import render_bench
    from omni.cli.commands.lit_cmd import render_lit
    from omni.cli.commands.verify_cmd import render_verify_report

    state = AppState()
    agent = await make_agent(state)
    try:
        sid = await agent.ensure_session(channel="cli")

        async def _count() -> int:
            async with agent.db.session() as s:
                return (await s.execute(select(func.count()).select_from(SessionORM))).scalar_one()

        before = await _count()
        await render_lit(agent, "retrieval augmented generation", session_id=sid)
        await render_verify_report(agent)
        await render_bench(k=3, embed=False)
        after = await _count()
        assert after == before  # the whole point: no session pollution
    finally:
        await agent.aclose()
        from omni.storage.db import reset_databases

        await reset_databases()
