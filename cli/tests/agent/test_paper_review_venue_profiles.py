"""Regression checks for the bundled paper-review venue contracts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "skills" / "paper-review"
VENUE_DIR = SKILL_DIR / "references" / "venues"


def _load_core() -> ModuleType:
    """Load the portable skill core without importing CLI internals."""

    module_name = "paper_review_venue_profile_core"
    spec = importlib.util.spec_from_file_location(module_name, SKILL_DIR / "core.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("venue", "expected_fields"),
    (
        (
            "ICLR 2026",
            (
                "Summary",
                "Soundness",
                "Presentation",
                "Contribution",
                "Strengths",
                "Weaknesses",
                "Questions",
                "Flag For Ethics Review",
                "Rating",
                "Confidence",
            ),
        ),
        (
            "CVPR 2026",
            (
                "Paper Summary",
                "Paper Strengths",
                "Major Weaknesses",
                "Minor Weaknesses",
                "Overall Recommendation",
                "Justification For Recommendation And Suggestions For Rebuttal",
                "Confidence Level",
            ),
        ),
        (
            "NeurIPS 2026",
            (
                "Summary",
                "Contribution Type Confirmation",
                "Strengths And Weaknesses",
                "Quality",
                "Clarity",
                "Significance",
                "Originality",
                "Questions",
                "Limitations",
                "Overall",
                "Confidence",
                "Ethical Concerns",
                "Paper Formatting Concerns",
            ),
        ),
        (
            "ICML 2025",
            (
                "Summary",
                "Claims And Evidence",
                "Relation To Prior Works",
                "Other Aspects",
                "Questions For Authors",
                "Ethical Issues",
                "Overall Recommendation",
            ),
        ),
    ),
)
def test_year_specific_venue_contracts(
    venue: str,
    expected_fields: tuple[str, ...],
) -> None:
    """Keep historically distinct public forms from collapsing into one template."""

    core = _load_core()
    selection = core.resolve_venue(venue, VENUE_DIR)
    assert selection.fields == expected_fields


def test_cvpr_2026_profile_rejects_obsolete_six_point_scale() -> None:
    """CVPR 2026 official training uses five qualitative recommendation labels."""

    profile = (VENUE_DIR / "cvpr.md").read_text(encoding="utf-8")
    assert "`5 — Strong Accept`" in profile
    assert "`3 — Borderline`" in profile
    assert "six-point split" in profile
    assert "`6` — Accept" not in profile


def test_upcoming_forms_are_marked_as_pending() -> None:
    """Do not present a historical fallback as unpublished 2027 form metadata."""

    iclr = (VENUE_DIR / "iclr.md").read_text(encoding="utf-8")
    aaai = (VENUE_DIR / "aaai.md").read_text(encoding="utf-8")
    assert "ICLR 2027 Current Guidance — Form Pending" in iclr
    assert "AAAI-27 Current Main-Track Guidance — Form Pending" in aaai
    assert "Pending official AAAI-27 form" in aaai
