"""The eval corpus preserves the missing-VLM user journey."""

from __future__ import annotations

from omni.eval.scenarios import load_scenarios


def test_eval_corpus_contains_livefigure_missing_configuration_journey():
    scenario = next(
        item for item in load_scenarios() if item.id == "livefigure_missing_vlm_configuration"
    )
    turn = scenario.turns[0]

    assert turn.drain is True
    assert {"livefigure", "scientific-figure"} <= set(
        turn.expect["skills_executed_exclude"]
    )
    assert "artifact.pptx" in turn.expect["text_contains"]
