from __future__ import annotations

from omni.research.review_findings import (
    finding_from_judge_notes,
    findings_from_structural,
    format_review_findings,
)


def test_structural_findings_become_priority_cards() -> None:
    cards = findings_from_structural(
        dangling_anchors=["[S99]"],
        unanchored_absolutes=["SOTA"],
        figure_gaps=["/tmp/lonely.png"],
    )
    kinds = {card.kind for card in cards}
    assert kinds == {"dangling_anchor", "unanchored_absolute", "figure_gap"}
    assert any(card.priority == "high" for card in cards)


def test_format_review_findings_lists_cards_and_decks() -> None:
    lines = format_review_findings(
        {
            "findings": [
                {
                    "title": "Dangling [S#] anchors",
                    "body": "[S99] is not mapped",
                    "priority": "high",
                }
            ],
            "slide_paths": ["/tmp/deck.pptx"],
        }
    )
    assert any("[high] Dangling" in line for line in lines)
    assert any("deck.pptx" in line for line in lines)


def test_judge_notes_become_a_low_priority_card() -> None:
    card = finding_from_judge_notes("The outline is thin.")
    assert card is not None
    assert card.kind == "judge"
    assert card.priority == "low"


def test_format_review_findings_shows_informational_verdict() -> None:
    lines = format_review_findings({"verdict": "pass", "findings": []})
    assert any("Review verdict: pass" in line for line in lines)
    assert any("does not change task status" in line for line in lines)
