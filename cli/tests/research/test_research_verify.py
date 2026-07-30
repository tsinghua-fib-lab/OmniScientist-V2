"""M5 verification: verify_session logic + `omni verify` CLI surface."""

from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

from omni.cli.main import app
from omni.config import load_settings
from omni.research.store import ResearchStore
from omni.research.verify import verify_session
from omni.storage.db import get_database

runner = CliRunner()


def _run(coro):
    async def _wrap():
        from omni.storage.db import reset_databases

        try:
            return await coro
        finally:
            await reset_databases()

    return asyncio.run(_wrap())


async def _store() -> ResearchStore:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ResearchStore(db)


@pytest.mark.asyncio
async def test_verify_flags_unsupported_contradicted_overconfident():
    store = await _store()
    src = await store.add_source({"title": "S"})
    # supported claim
    c_ok = await store.add_claim("supported", confidence=0.6)
    await store.add_evidence(c_ok.id, source_id=src.id, stance="supports")
    # unsupported + overconfident
    await store.add_claim("thin but confident", confidence=0.9)
    # contradicted
    c_bad = await store.add_claim("disputed", confidence=0.5)
    await store.add_evidence(c_bad.id, source_id=src.id, stance="contradicts")

    report = await verify_session(store)
    assert report.total_claims == 3
    assert report.supported == 1
    # "disputed" has only contradicting evidence → both unsupported and contradicted.
    assert len(report.unsupported) == 2
    assert len(report.overconfident) == 1
    assert len(report.contradicted) == 1
    assert report.grounding_rate == pytest.approx(1 / 3)
    assert report.issues == 4


def test_verify_cli_empty_message():
    res = runner.invoke(app, ["verify"])
    assert res.exit_code == 0
    assert "no claims to verify" in res.stdout


def test_verify_cli_reports_issues():
    async def _seed():
        store = await _store()
        await store.add_claim("unsupported assertion", confidence=0.95)

    _run(_seed())
    res = runner.invoke(app, ["verify"])
    assert res.exit_code == 0
    assert "Unsupported claims" in res.stdout


def test_verify_cli_accepts_session_prefix():
    async def _seed():
        from omni.cli.state import AppState, make_agent

        agent = await make_agent(AppState())
        try:
            sid = await agent.ensure_session(channel="cli")
            store = ResearchStore(agent.db)
            src = await store.add_source({"title": "Attention Is All You Need"})
            claim = await store.add_claim(
                "Transformer uses encoder-decoder attention",
                confidence=0.7,
                session_id=sid,
            )
            await store.add_evidence(claim.id, source_id=src.id, stance="supports")
            await store.add_claim(
                "Different session unsupported claim",
                confidence=0.95,
                session_id="other-session",
            )
            return sid
        finally:
            await agent.aclose()

    sid = _run(_seed())

    res = runner.invoke(app, ["verify", "--session", sid[:8]])

    assert res.exit_code == 0, res.stdout
    assert "Verification scope: session" in res.stdout
    assert "Claims 1" in res.stdout
    assert "grounding rate 100%" in res.stdout
    assert "Unsupported claims" not in res.stdout


def test_verify_cli_unknown_session_prefix_fails_clearly():
    res = runner.invoke(app, ["verify", "--session", "deadbeef"])

    assert res.exit_code == 1
    assert "Session deadbeef was not found" in res.stderr
