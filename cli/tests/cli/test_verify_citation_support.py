from __future__ import annotations

from omni.cli.runner import render_verify
from omni.research.citation_support import CitationSupportReport
from omni.research.verify import VerifyReport


def test_render_verify_prints_citation_support_warning_tier(capsys) -> None:  # noqa: ANN001
    report = VerifyReport(
        total_claims=1,
        supported=1,
        citation_support=CitationSupportReport(
            checked=1,
            supported=0,
            weak=[("claim001abcdef", "Activation steering always works.", 0.22)],
        ),
    )
    render_verify(report)
    out = capsys.readouterr().out
    assert "Citation support (warning only — does not change task status)" in out
    assert "weak 1" in out


def test_render_verify_prints_citation_support_when_no_claims(capsys) -> None:  # noqa: ANN001
    render_verify(VerifyReport(citation_support=CitationSupportReport()))
    out = capsys.readouterr().out
    assert "Citation support (warning only — does not change task status)" in out
