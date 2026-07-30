"""AcceptanceEngine — research-fact acceptance over a VerifyReport (P2.2).

The engine is additive on top of the structural verification gate: ``warn``
(default) annotates without changing acceptance; ``strict`` fails closed so a
caller can gate/downgrade; ``off`` disables it. These tests build VerifyReport
values directly (no DB / no model) so acceptance stays deterministic and offline.
"""

from __future__ import annotations

import asyncio

import pytest
import typer

from omni.research.acceptance import AcceptanceEngine
from omni.research.citation_support import CitationSupportReport
from omni.research.verify import VerifyReport


def _report(
    *,
    total: int = 0,
    supported: int = 0,
    unsupported: int = 0,
    contradicted: int = 0,
    citation: CitationSupportReport | None = None,
) -> VerifyReport:
    r = VerifyReport(total_claims=total, supported=supported)
    # unsupported/contradicted only need length for acceptance; use light stand-ins.
    r.unsupported = [object() for _ in range(unsupported)]  # type: ignore[list-item]
    r.contradicted = [(object(), 1) for _ in range(contradicted)]  # type: ignore[list-item]
    r.citation_support = citation
    return r


def test_off_mode_accepts_and_is_silent():
    report = _report(total=2, supported=0, unsupported=2)
    verdict = AcceptanceEngine("off").evaluate(report)
    assert verdict.accepted is True
    assert verdict.has_findings is False
    assert verdict.annotation() == ""


def test_clean_report_accepts_with_no_findings_in_all_modes():
    report = _report(total=2, supported=2)
    for mode in ("off", "warn", "strict"):
        verdict = AcceptanceEngine(mode).evaluate(report)
        assert verdict.accepted is True
        assert verdict.has_findings is False


def test_warn_mode_annotates_but_still_accepts():
    report = _report(total=4, supported=1, unsupported=3)  # grounding 25% < 50%
    verdict = AcceptanceEngine("warn").evaluate(report)
    assert verdict.accepted is True
    assert verdict.has_findings is True
    assert "grounding" in verdict.annotation()
    assert "⚠ Acceptance" in verdict.annotation()


def test_strict_mode_fails_on_thin_grounding():
    report = _report(total=4, supported=1, unsupported=3)
    verdict = AcceptanceEngine("strict").evaluate(report)
    assert verdict.accepted is False
    assert "✗ Not accepted" in verdict.annotation()


def test_strict_mode_fails_on_contradictions():
    report = _report(total=2, supported=2, contradicted=1)
    verdict = AcceptanceEngine("strict").evaluate(report)
    assert verdict.accepted is False
    assert any("contradicting" in n for n in verdict.notes)


def test_strict_mode_fails_on_weak_citation_support():
    cs = CitationSupportReport(checked=4, supported=1)
    cs.unsupported = [("c1", "claim", 0.1), ("c2", "claim", 0.2)]
    report = _report(total=4, supported=4, citation=cs)  # structurally fine
    verdict = AcceptanceEngine("strict", min_citation_support=0.6).evaluate(report)
    assert verdict.accepted is False
    assert any("citation support" in n for n in verdict.notes)


def test_citation_support_not_faulted_when_nothing_checked():
    cs = CitationSupportReport(checked=0, supported=0)
    report = _report(total=1, supported=1, citation=cs)
    verdict = AcceptanceEngine("strict").evaluate(report)
    assert verdict.accepted is True  # support_rate is 1.0 when nothing checked


def test_invalid_mode_falls_back_to_warn():
    assert AcceptanceEngine("bogus").mode == "warn"


def test_from_settings_reads_research_knobs():
    from omni.config import load_settings

    settings = load_settings()
    settings.research.acceptance_mode = "strict"
    settings.research.acceptance_min_grounding = 0.9
    engine = AcceptanceEngine.from_settings(settings)
    assert engine.mode == "strict"
    assert engine.min_grounding == 0.9


def _run(coro):
    async def _wrap():
        from omni.storage.db import reset_databases

        try:
            return await coro
        finally:
            await reset_databases()

    return asyncio.run(_wrap())


async def _seed_thin_claim(agent) -> None:  # noqa: ANN001
    from omni.research.store import ResearchStore

    await ResearchStore(agent.db).add_claim("thin but confident", confidence=0.95)


def test_verify_strict_gate_exits_nonzero_on_thin_graph():
    """``strict`` mode turns a thin/ungrounded graph into a non-zero exit."""
    from omni.cli.commands.verify_cmd import _run_verify
    from omni.cli.state import AppState, make_agent

    async def _seed_and_verify():
        agent = await make_agent(AppState())
        try:
            await _seed_thin_claim(agent)
        finally:
            await agent.aclose()
        state = AppState(overrides={"research": {"acceptance_mode": "strict"}})
        await _run_verify(state, session="")

    with pytest.raises(typer.Exit) as exc:
        _run(_seed_and_verify())
    assert exc.value.exit_code == 2


def test_verify_warn_default_accepts_but_flags():
    """The default (``warn``) never fails the exit code but does annotate."""
    from omni.cli.commands.verify_cmd import render_verify_report
    from omni.cli.state import AppState, make_agent

    async def _seed_and_verify():
        agent = await make_agent(AppState())
        try:
            await _seed_thin_claim(agent)
            return await render_verify_report(agent)
        finally:
            await agent.aclose()

    verdict = _run(_seed_and_verify())
    assert verdict.accepted is True
    assert verdict.has_findings is True


def test_verify_strict_render_marks_not_accepted():
    from omni.cli.commands.verify_cmd import render_verify_report
    from omni.cli.state import AppState, make_agent

    async def _seed_and_verify():
        agent = await make_agent(AppState(overrides={"research": {"acceptance_mode": "strict"}}))
        try:
            await _seed_thin_claim(agent)
            return await render_verify_report(agent)
        finally:
            await agent.aclose()

    verdict = _run(_seed_and_verify())
    assert verdict.accepted is False
