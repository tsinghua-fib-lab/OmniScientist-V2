"""Named scientific outputs are a settlement contract, not a caption."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omni.agent.capabilities import contract_outputs, contract_write_target
from omni.agent.intent_plan import IntentType
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.core.react_agent import ToolInvocationRecord
from omni.runtime.remaining import (
    failed_canonical_file_debts,
    incoming_plan_is_retrieve_only,
    remaining_contract_files,
    remaining_deliverables,
    remaining_figure,
    remaining_slides,
    remaining_typed_refs,
    remaining_writing,
)
from omni.skills_runtime.registry import SkillRegistry


def test_ledger_tokens_are_not_write_basenames() -> None:
    assert contract_write_target("draft.section") == ("report", ".md")
    assert contract_write_target("draft.manuscript") == ("report", ".md")
    assert contract_write_target("synthesis.final") == ("report", ".md")
    assert contract_write_target("artifact.figure") == ("figure", ".png")
    assert contract_write_target("artifact.slides") == ("slides", ".pptx")
    assert contract_write_target("notes.md") is None
    assert contract_write_target("survey.md") is None


def test_sources_debt_clears_only_when_source_ids_exist() -> None:
    assert remaining_typed_refs(["answer", "sources"], source_ids=None) == ["sources"]
    assert remaining_typed_refs(["sources"], source_ids=[]) == ["sources"]
    assert remaining_typed_refs(["sources"], source_ids=["", "  "]) == ["sources"]
    assert remaining_typed_refs(["sources"], source_ids=["openalex:W1"]) == []
    assert remaining_typed_refs(["answer"], source_ids=None) == []


def test_figure_does_not_satisfy_a_manuscript_debt() -> None:
    figure = SimpleNamespace(
        kind="figure",
        title="RAG architecture",
        rel_path="figures/rag.png",
        mime="image/png",
        uri="artifact://fig",
    )
    remaining = remaining_deliverables(
        ["answer", "artifact.figure", "draft.manuscript"],
        [figure],
    )
    assert remaining == ["draft.manuscript"]
    assert remaining_writing(remaining) == ["draft.manuscript"]
    assert remaining_figure(remaining) == []


def test_dot_sidecar_does_not_count_as_the_figure() -> None:
    dot = SimpleNamespace(
        kind="figure",
        title="RAG",
        rel_path="figures/rag.dot",
        mime="text/vnd.graphviz",
        uri="artifact://dot",
    )
    remaining = remaining_deliverables(["artifact.figure"], [dot])
    assert remaining == ["artifact.figure"]
    assert remaining_figure(remaining) == ["artifact.figure"]


def test_markdown_report_does_not_pay_a_paper_review_debt() -> None:
    report = SimpleNamespace(
        kind="document",
        title="review",
        rel_path="outputs/review.md",
        mime="text/markdown",
        uri="artifact://md",
    )
    assert remaining_deliverables(["review"], [report]) == ["review"]


def test_kind_review_pays_the_paper_review_debt() -> None:
    review = SimpleNamespace(
        kind="review",
        title="NeurIPS review",
        rel_path="outputs/review.md",
        mime="text/markdown",
        uri="artifact://review",
    )
    assert remaining_deliverables(["review"], [review]) == []


def test_harvested_deck_does_not_pay_editable_figure() -> None:
    deck = SimpleNamespace(
        kind="slides",
        title="deck",
        rel_path="outputs/deck.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        uri="artifact://deck",
    )
    assert remaining_deliverables(["artifact.pptx"], [deck]) == ["artifact.pptx"]
    assert remaining_deliverables(["artifact.slides"], [deck]) == []


def test_figure_kind_pptx_pays_editable_figure() -> None:
    slide = SimpleNamespace(
        kind="figure",
        title="RAG architecture PPTX",
        rel_path="outputs/rag.pptx",
        format="pptx",
        uri="artifact://live",
    )
    assert remaining_deliverables(["artifact.pptx"], [slide]) == []


def test_failed_livefigure_keeps_editable_figure_debt() -> None:
    record = ToolInvocationRecord(
        name="run_skill",
        arguments={"skill_name": "livefigure"},
        result={"status": "error", "skill_name": "livefigure", "error": "dunder"},
        status="succeeded",
    )
    assert failed_canonical_file_debts([record], []) == ["artifact.pptx"]


def test_markdown_report_satisfies_manuscript() -> None:
    report = SimpleNamespace(
        kind="report",
        title="Survey",
        rel_path="reports/survey.md",
        mime="text/markdown",
        uri="artifact://paper",
    )
    assert remaining_deliverables(["draft.manuscript", "draft.section"], [report]) == []


def test_this_task_utf8_file_pays_writing_debt(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "draft.section"
    path.write_text("# latent-space intervention survey\n", encoding="utf-8")
    artifact = SimpleNamespace(
        kind="file",
        title="draft",
        rel_path=str(path),
        path=str(path),
        mime="application/octet-stream",
        uri="artifact://draft",
    )
    assert remaining_deliverables(["draft.section", "draft.manuscript"], [artifact]) == []


def test_this_task_png_bytes_do_not_pay_writing_debt(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "draft.section"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
    artifact = SimpleNamespace(
        kind="file",
        title="draft",
        rel_path=str(path),
        path=str(path),
        mime="application/octet-stream",
        uri="artifact://bin",
    )
    assert remaining_deliverables(["draft.section"], [artifact]) == ["draft.section"]


def test_document_txt_pays_writing_debt() -> None:
    notes = SimpleNamespace(
        kind="document",
        title="notes",
        rel_path="notes.txt",
        mime="text/plain",
        uri="artifact://txt",
    )
    assert remaining_deliverables(["draft.section"], [notes]) == []


def test_text_mime_file_pays_writing_debt() -> None:
    artifact = SimpleNamespace(
        kind="file",
        title="draft",
        rel_path="draft.section",
        mime="text/plain",
        uri="artifact://text",
    )
    assert remaining_deliverables(["draft.manuscript"], [artifact]) == []


def test_answer_alone_is_not_an_artifact_debt() -> None:
    assert contract_outputs(["answer"]) == []
    assert remaining_deliverables(["answer"], []) == []


def test_remaining_contract_files_omit_sources_and_answer() -> None:
    remaining = ["sources", "answer", "draft.section", "artifact.figure"]
    assert remaining_contract_files(remaining) == ["draft.section", "artifact.figure"]


def test_figure_debt_is_host_fillable() -> None:
    remaining = remaining_deliverables(["artifact.figure", "draft.manuscript"], [])
    assert remaining_figure(remaining) == ["artifact.figure"]
    assert remaining_writing(remaining) == ["draft.manuscript"]


def test_pptx_plus_manuscript_is_not_writing_only() -> None:
    remaining = remaining_deliverables(["artifact.pptx", "draft.manuscript"], [])
    assert remaining == ["artifact.pptx", "draft.manuscript"]
    writing = remaining_writing(remaining)
    assert writing == ["draft.manuscript"]
    assert set(writing) != set(remaining)


def test_slide_debt_is_host_fillable_and_editable_pptx_is_not() -> None:
    remaining = remaining_deliverables(
        ["artifact.slides", "artifact.pptx", "draft.manuscript"], []
    )
    assert remaining_slides(remaining) == ["artifact.slides"]
    assert remaining_writing(remaining) == ["draft.manuscript"]


def test_react_fallback_copies_named_outputs_onto_verification() -> None:
    planner = IntentPlanner(SkillRegistry(load_settings()))
    proposal = ModelPlanProposal.from_payload(
        {
            "intent_type": "workflow",
            "required_capabilities": [
                "paper.fetch.arxiv",
                "artifact.figure",
                "draft.manuscript",
            ],
            "outputs": ["artifact.figure", "draft.manuscript", "answer"],
            "workflow_steps": [
                {"id": "fetch", "capability": "paper.fetch.arxiv"},
                {"id": "fig", "capability": "artifact.figure"},
                {"id": "paper", "capability": "draft.manuscript"},
            ],
            "confidence": 0.8,
            "rationale": "survey materials sequenced live",
        }
    )
    plan = planner.plan_from_proposal(
        "Attention abstract, RAG figure, and a survey paper",
        proposal,
        task_id="task-829bfee2",
    )
    assert plan.intent_type == IntentType.REACT_FALLBACK
    assert "artifact.figure" in plan.outputs
    assert "draft.manuscript" in plan.outputs
    required = plan.verification_plan.required_outputs
    assert "artifact.figure" in required
    assert "draft.manuscript" in required
    assert "react.finished" in plan.verification_plan.required_events


def test_answer_only_proposal_still_binds_named_capabilities() -> None:
    """Incident 6978342b: ReAct floor with outputs=answer dropped the paper debt."""
    planner = IntentPlanner(SkillRegistry(load_settings()))
    proposal = ModelPlanProposal.from_payload(
        {
            "intent_type": "workflow",
            "required_capabilities": ["artifact.figure", "draft.manuscript"],
            "outputs": ["answer"],
            "workflow_steps": [
                {"id": "fig", "capability": "artifact.figure"},
                {"id": "paper", "capability": "draft.manuscript"},
            ],
            "confidence": 0.8,
            "rationale": "summary, figure, and paper sequenced live",
        }
    )
    plan = planner.plan_from_proposal(
        "Attention abstract, RAG figure, and a survey paper",
        proposal,
        task_id="task-6978342b",
    )
    assert plan.intent_type == IntentType.REACT_FALLBACK
    assert "artifact.figure" in plan.outputs
    assert "draft.manuscript" in plan.outputs
    required = plan.verification_plan.required_outputs
    assert "artifact.figure" in required
    assert "draft.manuscript" in required
    assert plan.tool_policy.allows("write_file")
    assert not plan.tool_policy.allows("bash")


def test_analysis_report_wording_binds_a_manuscript() -> None:
    from omni.runtime.remaining import infer_analysis_report_outputs

    prompt = (
        "请仔细分析你的存储、记忆系统架构，对标codex 的源码"
        "（源码目录 /Users/antonio/work/sourcecode ）实现"
    )
    assert infer_analysis_report_outputs(prompt) == ["draft.manuscript"]
    assert infer_analysis_report_outputs("请仔细分析这段代码为什么报错") == []
    assert infer_analysis_report_outputs("写一篇综述") == []
    assert infer_analysis_report_outputs("请做一组会PPT对标一下") == []
    assert infer_analysis_report_outputs(
        "仔细review 今天 push 到master 上的代码，实现是对标了 codex 源码设计的。不做代码改动"
    ) == []


def test_analysis_report_bind_unblocks_write_file() -> None:
    from omni.agent.plan_factory import build_assistant_plan
    from omni.runtime.remaining import bind_contract_outputs

    plan = build_assistant_plan(
        "请仔细分析你的存储、记忆系统架构，对标codex 的源码实现",
        task_id="analysis-bind",
        rationale="readonly analysis",
    )
    bound = bind_contract_outputs(plan)
    assert "draft.manuscript" in bound.outputs
    assert "draft.manuscript" in bound.verification_plan.required_outputs
    assert bound.tool_policy.allows("write_file")


def test_figure_and_paper_wording_binds_the_contract_without_capabilities() -> None:
    from omni.runtime.remaining import infer_figure_and_paper_outputs, infer_slide_outputs

    assert infer_figure_and_paper_outputs(
        "请总结 Attention，画一张 RAG 架构图，并输出一篇论文"
    ) == ["artifact.figure", "draft.manuscript"]
    assert infer_figure_and_paper_outputs("请画一张架构图") == []
    assert infer_figure_and_paper_outputs("写一篇综述") == []
    assert infer_figure_and_paper_outputs(
        "我想了解我前面问的 RAG 系统综述 & Transformer 架构图，看看这个任务的产出是什么"
    ) == []
    assert infer_slide_outputs("请做一组会PPT") == ["artifact.slides"]
    assert infer_slide_outputs("生成 PPT 和综述材料") == ["artifact.slides"]
    assert infer_slide_outputs("Make one editable single-slide figure") == []
    assert infer_figure_and_paper_outputs("生成 PPT 和综述材料") == []
    assert infer_figure_and_paper_outputs(
        "为 loop engineering 系统综述准备材料，并生成架构图，并输出一份详细的介绍ppt"
    ) == ["artifact.figure"]
    assert infer_figure_and_paper_outputs(
        "为 RAG 系统综述准备材料，生成架构图，并输出一篇论文和一份PPT"
    ) == ["artifact.figure", "draft.manuscript"]


def test_retrieve_window_binds_produce_debts_without_write() -> None:
    from omni.agent.plan_factory import build_named_native_tool_plan
    from omni.runtime.remaining import bind_contract_outputs

    plan = build_named_native_tool_plan(
        "调用 search_literature 再写一篇综述、画架构图、做一组会PPT。",
        tool="search_literature",
        task_id="x8-bind",
    )
    assert incoming_plan_is_retrieve_only(plan)
    bound = bind_contract_outputs(plan)
    assert not incoming_plan_is_retrieve_only(bound)
    assert not bound.tool_policy.allows("write_file")
    assert "artifact.figure" in bound.outputs
    assert "artifact.slides" in bound.verification_plan.required_outputs


def test_source_id_only_scope_skips_produce_inference() -> None:
    from omni.agent.plan_factory import build_named_native_tool_plan
    from omni.runtime.remaining import bind_contract_outputs

    plan = build_named_native_tool_plan(
        "调用 search_literature 再写一篇综述，只列出 source_id。",
        tool="search_literature",
        task_id="x8-ids",
    )
    bound = bind_contract_outputs(plan)
    assert incoming_plan_is_retrieve_only(bound)
    assert "artifact.figure" not in bound.outputs
    assert not bound.tool_policy.allows("write_file")


def test_skip_completed_skills_note_names_the_figure_and_the_paper_debt() -> None:
    from omni.runtime.remaining import skip_completed_skills_note

    figure = SimpleNamespace(
        kind="figure", title="RAG architecture", rel_path="figures/rag.png", mime="image/png",
    )
    note = skip_completed_skills_note(["draft.manuscript"], [figure])
    assert "RAG architecture" in note
    assert "draft.manuscript" in note
    assert "do not rerun" in note.lower()
    assert "this task" in note.lower()
    assert "another task" in note.lower()


def test_host_remaining_summary_reports_missing_manuscript() -> None:
    from omni.cli.commands.tasks_cmd import _host_remaining_summary

    plan = {
        "outputs": ["artifact.figure", "draft.manuscript"],
        "verification_plan": {"required_outputs": ["artifact.figure", "draft.manuscript"]},
    }
    # Production tuples are (title, path, uri), not (kind, title, target).
    missing = _host_remaining_summary(
        plan,
        [("RAG architecture", "figures/rag.png", "artifact://fig")],
    )
    assert missing == "missing draft.manuscript"
    filled = _host_remaining_summary(
        plan,
        [
            ("RAG architecture", "figures/rag.png", "artifact://fig"),
            ("Survey", "reports/survey.md", "artifact://md"),
        ],
    )
    assert filled == "all named deliverables present"


def test_p01_host_remaining_clears_when_display_paths_pay_debts() -> None:
    """P-01: figure + manuscript + slides on disk clear remaining in task show."""
    from omni.cli.commands.tasks_cmd import _host_remaining_summary

    plan = {
        "outputs": ["artifact.figure", "draft.manuscript", "artifact.slides"],
        "verification_plan": {
            "required_outputs": ["artifact.figure", "draft.manuscript", "artifact.slides"],
        },
    }
    assert _host_remaining_summary(plan, []) == (
        "missing artifact.figure, draft.manuscript, artifact.slides"
    )
    filled = _host_remaining_summary(
        plan,
        [
            ("RAG architecture", "/tmp/out/RAG.png", "artifact://fig"),
            ("RAG survey", "/tmp/out/RAG系统综述.md", "artifact://md"),
            ("RAG slides", "/tmp/out/deck.pptx", "artifact://pptx"),
        ],
    )
    assert filled == "all named deliverables present"


@pytest.mark.asyncio
async def test_remaining_retry_context_says_other_tasks_do_not_count() -> None:
    from omni.runtime.remaining import remaining_retry_context

    original = SimpleNamespace(
        plan_json={
            "outputs": ["artifact.figure"],
            "verification_plan": {"required_outputs": ["artifact.figure"]},
        }
    )
    current = SimpleNamespace(retry_of_task_id="orig-1")
    tasks = SimpleNamespace(
        get_task=AsyncMock(side_effect=lambda tid: current if tid == "retry-1" else original)
    )
    artifacts = SimpleNamespace(list_by_task=AsyncMock(return_value=[]))

    note = await remaining_retry_context(tasks, artifacts, "retry-1")
    assert "another task" in note.lower()
    assert "this task" in note.lower()
    assert "artifact.figure" in note
