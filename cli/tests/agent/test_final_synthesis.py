from __future__ import annotations

import inspect
from typing import Any

import pytest

from omni.runtime import final_synthesis
from omni.runtime.final_synthesis import execute_final_synthesis, run_native_synthesis


class _DraftLLM:
    """Deterministic writing double: returns a fixed draft and records prompts."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.system = ""
        self.user = ""

    async def chat(self, system: str, user: str, **_kwargs: Any) -> str:
        self.system = system
        self.user = user
        return self.text


class _FakeStored:
    def __init__(self) -> None:
        self.uri = "artifact://draft123456"
        self.path = "/tmp/report/draft123456.md"
        self.mime = "text/markdown"
        self.size_bytes = 512


class _FakeArtifacts:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def put_bytes(self, data: bytes, **kwargs: Any) -> _FakeStored:
        self.calls.append({"data": data, **kwargs})
        return _FakeStored()


def test_final_synthesis_carries_provenance_objects():
    result = execute_final_synthesis(
        "写一个 RAG 研究小节",
        {"id": "final", "deliverable": "draft.section", "depends_on": ["qa", "figure"]},
        {
            "qa": {
                "summary": "RAG uses retrieval to ground generation.",
                "source_ids": ["source123456"],
                "claim_ids": ["claim123456"],
                "evidence_ids": ["evidence123456"],
            },
            "figure": {"artifacts": [{"uri": "artifact://artifact123456", "title": "RAG Figure"}]},
        },
    )

    assert result["status"] == "ok"
    assert result["evidence_level"] == "grounded"
    assert result["provenance"]["source_ids"] == ["source123456"]
    assert result["provenance"]["claim_ids"] == ["claim123456"]
    assert result["provenance"]["evidence_ids"] == ["evidence123456"]
    assert result["provenance"]["artifact_ids"] == ["artifact123456"]
    assert "Evidence status: grounded" in result["draft_markdown"]
    assert "the audience is `researcher`" in result["draft_markdown"]


def test_final_synthesis_labels_conclusions_by_support():
    result = execute_final_synthesis(
        "写一个 RAG 研究小节",
        {"id": "final", "deliverable": "draft.section", "depends_on": ["qa", "guess"]},
        {
            # sourced conclusion: carries structured evidence
            "qa": {
                "summary": "RAG grounds generation in retrieved passages.",
                "source_ids": ["source123456"],
                "evidence_ids": ["evidence123456"],
            },
            # inferred conclusion: has a summary but no source/claim/evidence
            "guess": {"summary": "Larger context windows may reduce the need for retrieval."},
        },
    )

    labels = {item["id"]: item for item in result["provenance_labels"]}
    assert labels["qa"]["support"] == "sourced"
    assert labels["qa"]["label"] == "sourced"
    assert labels["guess"]["support"] == "inferred"
    assert labels["guess"]["label"] == "inferred"
    # The labelled comparison surfaces in the rendered draft.
    assert "Conclusion support" in result["draft_markdown"]
    assert "[sourced]" in result["draft_markdown"]
    assert "[inferred]" in result["draft_markdown"]


def test_final_synthesis_marks_insufficient_when_no_upstream_conclusions():
    result = execute_final_synthesis(
        "写一个没有上游依据的小节",
        {"id": "final", "deliverable": "draft.section", "depends_on": []},
        {},
    )

    assert result["status"] == "partial"
    assert result["evidence_level"] == "degraded"
    labels = result["provenance_labels"]
    assert len(labels) == 1
    assert labels[0]["support"] == "insufficient"
    assert labels[0]["label"] == "insufficient evidence"
    assert "[insufficient evidence]" in result["draft_markdown"]


def test_final_synthesis_template_lives_outside_runtime_code():
    src = inspect.getsource(final_synthesis)

    assert "synthesis_templates" in src
    assert "Target audience" not in src


def test_final_synthesis_topic_prefers_step_title_over_goal_truncation():
    result = execute_final_synthesis(
        "为 RAG 系统综述准备材料：获取 Attention Is All You Need 摘要，并生成科研架构图。并输出一篇论文",
        {"id": "final", "input": {"title": "Retrieval-Augmented Generation 系统综述"}},
        {"qa": {"summary": "RAG grounds generation."}},
    )

    assert result["topic"] == "Retrieval-Augmented Generation 系统综述"
    assert result["draft_markdown"].startswith("## Retrieval-Augmented Generation 系统综述")


@pytest.mark.asyncio
async def test_native_synthesis_uses_llm_draft_with_full_upstream_material():
    long_abstract = (
        "The dominant sequence transduction models are based on complex recurrent "
        "or convolutional neural networks in an encoder-decoder configuration. "
        "The best performing models also connect the encoder and decoder through "
        "an attention mechanism. We propose a new simple network architecture, "
        "the Transformer, based solely on attention mechanisms. UNIQUE-TAIL-MARKER"
    )
    assert len(long_abstract) > 240  # would be cut by the 240-char UI summary
    llm = _DraftLLM("# RAG 系统综述\n\n" + "论文正文。" * 60)

    result = await run_native_synthesis(
        "为 RAG 系统综述准备材料，并输出一篇论文",
        {"id": "final", "deliverable": "draft.section", "depends_on": ["paper"]},
        {"paper": {"title": "Attention Is All You Need", "summary": long_abstract}},
        llm=llm,
    )

    assert result["synthesis_mode"] == "llm"
    assert result["status"] == "ok"
    assert result["draft_markdown"].startswith("# RAG 系统综述")
    assessment = result["deliverable_assessment"]
    assert assessment["deliverable_id"] == "draft.section"
    assert assessment["status"] == "passed"
    assert assessment["criteria"][0]["criterion_id"] == "draft_content_present"
    assert assessment["criteria"][0]["status"] == "passed"
    # The writing prompt received the full abstract, not the 240-char teaser.
    assert "UNIQUE-TAIL-MARKER" in llm.user
    assert "research writing executor" in llm.system


@pytest.mark.asyncio
async def test_native_synthesis_without_llm_degrades_honestly():
    result = await run_native_synthesis(
        "写一个 RAG 研究小节",
        {"id": "final", "deliverable": "draft.section", "depends_on": ["qa"]},
        {"qa": {"summary": "RAG grounds generation."}},
        llm=None,
    )

    assert result["synthesis_mode"] == "template_fallback"
    assert result["status"] == "partial"  # → workflow records the step degraded
    assert "degraded" in result["warning"]
    assert result["draft_markdown"]  # the template draft is still delivered
    assert result["deliverable_assessment"]["status"] == "degraded"
    assert (
        result["deliverable_assessment"]["criteria"][0]["status"]
        == "degraded"
    )
    # Running without a model is the expected offline rung, not a failure.
    assert "synthesis_error" not in result


@pytest.mark.asyncio
async def test_native_synthesis_rejects_stub_llm_output():
    result = await run_native_synthesis(
        "写一个 RAG 研究小节",
        {"id": "final", "deliverable": "draft.section", "depends_on": ["qa"]},
        {"qa": {"summary": "RAG grounds generation."}},
        llm=_DraftLLM("summary:too short"),
    )

    assert result["synthesis_mode"] == "template_fallback"
    assert result["status"] == "partial"
    assert "too short" in result["synthesis_error"]


@pytest.mark.asyncio
async def test_native_synthesis_records_model_failure_reason():
    class _FailingLLM:
        async def chat(self, system: str, user: str, **_kwargs: Any) -> str:
            raise RuntimeError("provider unavailable (503)")

    result = await run_native_synthesis(
        "写一个 RAG 研究小节",
        {"id": "final", "deliverable": "draft.section", "depends_on": ["qa"]},
        {"qa": {"summary": "RAG grounds generation."}},
        llm=_FailingLLM(),
    )

    # The degrade behavior is unchanged; the failure cause is now auditable.
    assert result["synthesis_mode"] == "template_fallback"
    assert result["status"] == "partial"
    assert result["synthesis_error"] == "RuntimeError: provider unavailable (503)"


@pytest.mark.asyncio
async def test_native_synthesis_persists_report_artifact():
    store = _FakeArtifacts()
    result = await run_native_synthesis(
        "写一个 RAG 研究小节",
        {"id": "final", "deliverable": "draft.section", "input": {"title": "RAG 综述"}, "depends_on": ["qa"]},
        {"qa": {"summary": "RAG grounds generation."}},
        llm=_DraftLLM("# RAG 综述\n\n" + "grounded prose. " * 30),
        artifacts=store,
        session_id="sess-1",
        subtask_id="task-1",
    )

    assert result["report_uri"] == "artifact://draft123456"
    assert result["artifacts"][0]["format"] == "md"
    assert result["artifacts"][0]["mime"] == "text/markdown"
    call = store.calls[0]
    assert call["kind"] == "report"
    assert call["ext"] == "md"
    assert call["session_id"] == "sess-1"
    assert call["subtask_id"] == "task-1"
    assert call["meta"]["synthesis_mode"] == "llm"
