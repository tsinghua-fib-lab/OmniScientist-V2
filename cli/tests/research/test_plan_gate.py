from __future__ import annotations

from types import SimpleNamespace

from omni.research.plan_gate import expensive_work_from_plan, format_plan_gate


def test_expensive_work_from_slides_and_livefigure() -> None:
    plan = SimpleNamespace(
        outputs=["artifact.slides", "draft.manuscript"],
        capability_inputs={"figure.editable.pptx": {}},
        verification_plan=SimpleNamespace(required_outputs=["artifact.slides"]),
        selected_skills=[],
    )
    items = expensive_work_from_plan(plan)
    labels = {item["label"] for item in items}
    assert any("deck" in label for label in labels)
    assert any("LiveFigure" in label or "editable" in label for label in labels)
    lines = format_plan_gate({"items": items})
    assert lines
    assert "informational" in lines[0]


def test_plain_survey_is_not_expensive_work() -> None:
    plan = SimpleNamespace(
        outputs=["draft.section", "sources"],
        capability_inputs={"literature.survey": {}},
        verification_plan=None,
        selected_skills=[],
    )
    assert expensive_work_from_plan(plan) == []
