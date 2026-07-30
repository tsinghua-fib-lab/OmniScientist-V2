"""Per-skill progress normalization: one shared vocabulary, native wins.

``stage_map`` translates each built-in skill's own progress wording into the
stage contract so the CLI reads uniformly. These tests pin representative
mappings, the checkpoint milestones, and the rule that a skill already speaking
the contract is never overwritten.
"""

from __future__ import annotations

import pytest

from omni.runtime.stage_map import normalize_progress


@pytest.mark.parametrize(
    ("skill", "raw", "label", "milestone"),
    [
        ("openalex-search", "querying OpenAlex", "searching OpenAlex", ""),
        ("openalex-search", "done", "search complete", "Literature search complete"),
        ("scientific-figure", "render graphviz", "rendering figure", ""),
        ("scientific-figure", "done", "figure complete", "Figure rendered and checked"),
        ("livefigure", "pptx ready", "figure ready", "Live figure built"),
        ("research-pptx", "rendering", "rendering slides", ""),
        ("research-pptx", "upload", "finalizing", "Presentation rendered"),
        ("scientific-poster", "poster.design-ready", "design ready", "Poster design ready"),
        ("scientist-kg-distiller", "capsule", "building capsule", "Soul capsule generated"),
    ],
)
def test_known_stages_map_to_labels_and_checkpoints(skill, raw, label, milestone) -> None:
    out = normalize_progress({"skill": skill, "stage": raw, "pct": 0.5})
    assert out["stage"] == label
    assert out["stage_id"]
    assert out.get("milestone", "") == milestone


def test_prose_stages_match_by_phrase() -> None:
    out = normalize_progress(
        {"skill": "research-ideation",
         "stage": "Analyze research gaps across the corpus", "pct": 0.45}
    )
    assert out["stage"] == "analysing research gaps"
    assert out["stage_id"] == "ideation.gaps"


def test_a_native_contract_event_is_never_overwritten() -> None:
    native = {
        "skill": "openalex-search",
        "stage": "done",
        "stage_id": "custom.id",
        "milestone": "My own milestone",
    }
    assert normalize_progress(native) == native


def test_unmapped_skill_and_internal_stages_pass_through() -> None:
    workflow = {"subtask_id": "t", "stage": "workflow.step.start", "skill": "workflow"}
    assert normalize_progress(workflow) == workflow

    unknown = {"skill": "totally-unknown", "stage": "doing a thing"}
    assert normalize_progress(unknown) == unknown


def test_pct_only_events_are_left_alone() -> None:
    event = {"skill": "openalex-search", "stage": "", "pct": 0.2}
    assert normalize_progress(event) == event
