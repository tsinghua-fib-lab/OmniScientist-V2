from __future__ import annotations

from types import SimpleNamespace

from omni.research.citation_anchors import (
    citation_anchor_gate,
    collect_source_ids,
    extract_visible_anchors,
    is_scholarly_citation_key,
    uncovered_citation_event,
)
from omni.runtime.final_synthesis import execute_final_synthesis, run_native_synthesis
from omni.runtime.settlement import settlement_for


def test_extracts_s_anchors_and_source_ids() -> None:
    text = "RAG reduces hallucination [S1] via source_id abcdef12xyz."
    anchors = extract_visible_anchors(text, source_ids=["abcdef12xyz"])
    assert "[S1]" in anchors
    assert "abcdef12xyz" in anchors


def test_scholarly_keys_are_arxiv_and_doi() -> None:
    assert is_scholarly_citation_key("1706.03762") is True
    assert is_scholarly_citation_key("arxiv:2005.11401") is True
    assert is_scholarly_citation_key("10.18653/v1/2020.emnlp-main.550") is True
    assert is_scholarly_citation_key("fb3930049cda40a88ea0b75a80785df9") is False
    assert is_scholarly_citation_key("[S1]") is False


def test_gate_uncovered_when_sources_exist_and_draft_has_no_anchors() -> None:
    report = citation_anchor_gate("A survey with no citations.", ["source123456"])
    assert report.uncovered is True
    assert report.grounded_count == 1
    assert report.anchor_count == 0
    assert "model-visible" in report.notice


def test_gate_passes_when_no_sources() -> None:
    report = citation_anchor_gate("Opinion without retrieval.", [])
    assert report.uncovered is False


def test_gate_passes_when_source_and_anchor() -> None:
    report = citation_anchor_gate("Findings hold [S1].", ["source123456"])
    assert report.uncovered is False
    assert report.anchor_count == 1


def test_collect_source_ids_walks_nested_payloads() -> None:
    ids = collect_source_ids(
        {"source_ids": ["aaa111"]},
        {"result": {"source_id": "bbb222", "nested": {"source_ids": ["ccc333"]}}},
    )
    assert ids == ["aaa111", "bbb222", "ccc333"]


def test_final_synthesis_degrades_when_sources_have_no_anchors() -> None:
    result = execute_final_synthesis(
        "写一个 RAG 研究小节",
        {"id": "final", "deliverable": "draft.section", "depends_on": ["qa"]},
        {
            "qa": {
                "summary": "RAG uses retrieval to ground generation.",
                "source_ids": ["source123456"],
            }
        },
    )
    assert result["status"] == "partial"
    assert result["citation_anchors"]["uncovered"] is True
    assert result["provenance"]["source_ids"] == ["source123456"]


def test_final_synthesis_does_not_punish_contextual_draft() -> None:
    result = execute_final_synthesis(
        "写一个没有上游依据的小节",
        {"id": "final", "deliverable": "draft.section", "depends_on": []},
        {},
    )
    assert result["citation_anchors"]["uncovered"] is False


async def test_native_synthesis_llm_draft_with_anchor_stays_ok() -> None:
    class _Draft:
        async def chat(self, system: str, user: str, **_kwargs: object) -> str:
            return "# RAG\n\nThe result is grounded [S1]. " + ("body " * 40)

    result = await run_native_synthesis(
        "写综述",
        {"id": "final", "deliverable": "draft.section", "depends_on": ["qa"]},
        {"qa": {"summary": "RAG grounds generation.", "source_ids": ["source123456"]}},
        llm=_Draft(),
    )
    assert result["status"] == "ok"
    assert result["citation_anchors"]["uncovered"] is False
    assert result["deliverable_assessment"]["status"] == "passed"


async def test_native_synthesis_llm_draft_without_anchor_is_partial() -> None:
    class _Draft:
        async def chat(self, system: str, user: str, **_kwargs: object) -> str:
            return "# RAG\n\nA long draft with no citations at all. " + ("body " * 40)

    result = await run_native_synthesis(
        "写综述",
        {"id": "final", "deliverable": "draft.section", "depends_on": ["qa"]},
        {"qa": {"summary": "RAG grounds generation.", "source_ids": ["source123456"]}},
        llm=_Draft(),
    )
    assert result["status"] == "partial"
    assert result["citation_anchors"]["uncovered"] is True
    assert result["deliverable_assessment"]["status"] == "degraded"


def test_uncovered_citation_event() -> None:
    events = [
        SimpleNamespace(event_type="react.finished", status="succeeded", output_json={}),
        SimpleNamespace(
            event_type="citation.anchors",
            status="degraded",
            output_json={"uncovered": True, "grounded_count": 3, "anchor_count": 0},
        ),
    ]
    assert uncovered_citation_event(events) is True


class _Store:
    def __init__(self, events: list[object], task: object) -> None:
        self.events = events
        self.task = task

    async def get_task(self, task_id: str) -> object:
        return self.task

    async def list_events(self, task_id: str) -> list[object]:
        return self.events

    async def list_subtasks_by_ids(self, ids: list[str]) -> list[object]:
        return []

    async def list_workflows_by_ids(self, ids: list[str]) -> list[object]:
        return []

    async def list_subtasks_by_workflow_ids(self, ids: list[str]) -> list[object]:
        return []

    async def list_artifacts_by_task(self, task_id: str) -> list[object]:
        return [
            SimpleNamespace(
                kind="report",
                title="survey",
                path="/tmp/survey.md",
                mime="text/markdown",
                format="md",
                uri="artifact://survey1",
            )
        ]


async def test_settlement_degrades_on_uncovered_citation_event() -> None:
    task = SimpleNamespace(
        id="task1",
        status="succeeded",
        submitted_subtask_ids=[],
        submitted_workflow_ids=[],
        source_ids=["source123456"],
        plan_json={
            "outputs": ["draft.manuscript"],
            "verification_plan": {
                "required_outputs": ["draft.manuscript"],
                "required_events": ["react.finished"],
            },
        },
        channel="cli",
    )
    events = [
        SimpleNamespace(
            event_type="react.finished",
            status="succeeded",
            name="react",
            output_json={"kind": "text", "terminated_reason": "done"},
        ),
        SimpleNamespace(
            event_type="citation.anchors",
            status="degraded",
            name="citation.anchors",
            output_json={"uncovered": True, "grounded_count": 1, "anchor_count": 0},
        ),
    ]
    settled = await settlement_for(_Store(events, task), "task1")
    assert settled.status == "degraded"
    assert settled.detail.get("uncovered_citation_anchors") is True
