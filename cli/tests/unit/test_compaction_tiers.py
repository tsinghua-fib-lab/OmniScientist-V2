"""Compaction has two tiers, and they must not fire at the same point.

Trimming the content of old tool observations is cheap and loses little. Folding
the whole transcript into a bridge summary costs a model call and discards the
detail a long research run is built on. Omni declared both tiers but derived
both thresholds from one percentage, so the expensive one always fired at the
same moment as the cheap one — the transcript was summarised at 70% of the
window while 30% still sat unused.

Codex compacts at ~90% of the context window against a 95% usable ceiling. The
tiers below restore that ordering: trim first, summarise only near the wall.

The percentages measure against a *ceiling*, and the window is only one of the
two ceilings a transcript lives under; the run's token budget is the other. Both
tiers read the same ceiling so the ordering survives whichever one binds — the
regression guarded in tests/memory/test_compaction_threshold.py.
"""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.config.settings import (
    microcompact_token_budget,
    resolve_max_input_tokens,
    session_compact_token_budget,
    transcript_ceiling_tokens,
)


def test_the_cheap_tier_runs_before_the_expensive_one():
    settings = load_settings()

    assert microcompact_token_budget(settings) < session_compact_token_budget(settings)


@pytest.mark.parametrize("window", [4_000, 8_192, 32_768, 128_000, 1_000_000])
@pytest.mark.parametrize("budget", [0, 5_000, 60_000, 250_000, 4_000_000])
def test_both_tiers_measure_the_same_ceiling_whichever_one_binds(
    window: int, budget: int
):
    """Two ceilings, either of which may bind, must still yield one escalation.

    Sharing the ceiling is what keeps the escalation an escalation: a tier that
    answers to a ceiling the other one cannot see does not move nearer to it,
    it moves away from the other tier.
    """
    settings = load_settings()
    settings.memory.context_window_tokens = window
    settings.cost.max_total_tokens = budget
    ceiling = transcript_ceiling_tokens(settings)

    assert microcompact_token_budget(settings) == int(ceiling * 0.70)
    assert session_compact_token_budget(settings) == int(ceiling * 0.90)
    assert microcompact_token_budget(settings) < session_compact_token_budget(settings)


def test_percentages_configured_the_wrong_way_round_still_trim_before_folding():
    """Both fractions are owner-settable, and a pair that inverts them would
    otherwise pay for a summary on first contact and never trim at all."""
    settings = load_settings()
    settings.memory.microcompact_pct = 0.95
    settings.memory.autocompact_pct = 0.20

    assert microcompact_token_budget(settings) < session_compact_token_budget(settings)


def test_summarising_waits_until_the_transcript_ceiling_is_nearly_full():
    """Folding at 70% throws away detail while a third of the ceiling is free."""
    settings = load_settings()
    ceiling = transcript_ceiling_tokens(settings)

    assert ceiling > 0
    assert session_compact_token_budget(settings) >= int(ceiling * 0.85)


def test_a_margin_is_always_left_below_the_ceiling():
    """Token counts are estimates, so neither tier may sit at the ceiling."""
    settings = load_settings()

    assert session_compact_token_budget(settings) < transcript_ceiling_tokens(settings)
    assert session_compact_token_budget(settings) < resolve_max_input_tokens(settings)


def test_the_tiers_track_a_window_tighter_than_the_run_budget():
    """A window below what the budget can sustain is what both tiers measure —
    less the 16,384 tokens of reply gpt-4o is asked for, which share the window
    with the prompt and so cannot also be spent on transcript."""
    settings = load_settings()
    settings.model.model = "gpt-4o"
    settings.memory.context_window_tokens = 40_000

    assert session_compact_token_budget(settings) == 21_254
    assert microcompact_token_budget(settings) == 16_531


def test_the_tiers_track_the_request_window_not_cumulative_run_spend():
    """Compaction protects one request; owner spend policy is independent."""
    settings = load_settings()
    settings.model.model = "gpt-4o"
    settings.memory.context_window_tokens = 60_000

    assert session_compact_token_budget(settings) == 39_254
    assert microcompact_token_budget(settings) == 30_531
