from __future__ import annotations

from types import SimpleNamespace

from omni.research.survey_stages import detect_survey_stages, format_survey_stages


def test_detects_retrieve_evidence_outline_and_write() -> None:
    report = detect_survey_stages(
        tool_trace=[
            SimpleNamespace(name="search_literature"),
            SimpleNamespace(name="cite_source"),
            SimpleNamespace(name="update_plan"),
            SimpleNamespace(name="write_file"),
        ],
        draft="# Survey\n\n## Related work\n\nText [S1].",
    )
    assert report["complete"] is True
    assert report["present"] == ["retrieve", "evidence", "outline", "write"]
    assert report["missing"] == []


def test_missing_evidence_is_visible_but_not_a_hard_gate() -> None:
    report = detect_survey_stages(
        tool_trace=[SimpleNamespace(name="search_literature")],
        draft="",
    )
    assert "retrieve" in report["present"]
    assert "evidence" in report["missing"]
    assert report["complete"] is False
    lines = format_survey_stages(report)
    assert lines
    assert "still open" in lines[0]
