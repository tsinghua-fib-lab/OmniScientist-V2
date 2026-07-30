"""The stage/milestone vocabulary and its transport-compatibility guarantee.

The whole redesign rests on one fact: a skill can attach a ``milestone`` (or a
``current`` item) to its existing progress callback and have it reach the CLI
*without* any change to the event bus. These tests pin the vocabulary and, most
importantly, that the reserved fields survive a workflow relay.
"""

from __future__ import annotations

from omni.runtime.stage_contract import (
    CURRENT_KEY,
    InfoTier,
    Milestone,
    StageEvent,
    current_item,
    format_stats,
    milestone_from_progress,
    tier_visible,
)
from omni.runtime.workflow_progress import RELAY_OWNED_KEYS, relayed_fields


def test_stage_event_round_trips_through_progress_data() -> None:
    event = StageEvent(
        stage_id="analyze",
        label="analyzing papers",
        detail="Zero-shot prediction of cellular responses",
        pct=0.6,
    )
    data = event.to_progress_data()
    assert data["stage"] == "analyzing papers"
    assert data["stage_id"] == "analyze"
    assert current_item(data) == "Zero-shot prediction of cellular responses"
    assert data["pct"] == 0.6


def test_milestone_renders_summary_then_stats() -> None:
    milestone = Milestone(
        stage_id="search",
        summary="Literature search complete",
        stats={"found": 126, "kept": 20},
    )
    assert milestone.render() == "Literature search complete · found 126 · kept 20"


def test_milestone_is_read_back_from_progress_data() -> None:
    data = Milestone("search", "Search complete", {"found": 126}).to_progress_data()
    restored = milestone_from_progress(data)
    assert restored is not None
    assert restored.summary == "Search complete"
    assert restored.stats == {"found": 126}


def test_absent_milestone_reads_as_none() -> None:
    assert milestone_from_progress({"stage": "rendering", "pct": 0.3}) is None
    assert current_item({"stage": "rendering"}) == ""


def test_format_stats_handles_unlabelled_and_missing() -> None:
    assert format_stats({"pages": 16}) == ["pages 16"]
    assert format_stats(None) == []
    assert format_stats({"": "already phrased"}) == ["already phrased"]


def test_reserved_fields_survive_a_workflow_relay() -> None:
    """The core guarantee: milestones cross a workflow boundary untouched.

    ``relayed_fields`` re-stamps the keys the workflow owns and forwards the
    rest. If any stage-contract key were relay-owned, a nested skill's milestone
    would be stripped on its way to the terminal.
    """
    for key in ("milestone", "stats", "current", "stage_id"):
        assert key not in RELAY_OWNED_KEYS

    child = Milestone("search", "Search complete", {"found": 126}).to_progress_data()
    child[CURRENT_KEY] = "a paper title"
    forwarded = relayed_fields(child)
    assert forwarded["milestone"] == "Search complete"
    assert forwarded["stats"] == {"found": 126}
    assert forwarded[CURRENT_KEY] == "a paper title"


def test_tier_visibility_hides_only_diagnostics_by_default() -> None:
    assert tier_visible(InfoTier.STAGE, "normal") is True
    assert tier_visible(InfoTier.DIAGNOSTIC, "normal") is False
    assert tier_visible(InfoTier.DIAGNOSTIC, "normal", debug=True) is True
    assert tier_visible(InfoTier.DIAGNOSTIC, "verbose") is True
    # Quiet keeps the task and its deliverable; it drops the stage narrative.
    assert tier_visible(InfoTier.TASK, "quiet") is True
    assert tier_visible(InfoTier.DELIVERABLE, "quiet") is True
    assert tier_visible(InfoTier.STAGE, "quiet") is False
