"""Offline contracts for paper-review's complete concurrent engine."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from omni.core.tool_contracts import skill_input_contract_error

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "paper-review"


def _load_engine() -> Any:
    name = "paper_review_complete_pipeline_engine"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / "engine.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _ReviewLLM:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        fail_revision_plan: bool = False,
    ) -> None:
        self.payload = payload
        self.fail_revision_plan = fail_revision_plan
        self.calls = 0
        self.synthesis_saw_manuscript_analysis = False
        self.revision_prompt = ""

    async def chat(self, system: str, user: str, **_kwargs: Any) -> str:
        self.calls += 1
        if "literature-search queries" in system:
            await __import__("asyncio").sleep(0.005)
            return json.dumps(
                {
                    "queries": [
                        "DeepReview LLM paper review novelty verification",
                        "automated peer review evidence verification large language model",
                        "multi-stage scholarly review retrieval augmented evaluation",
                    ]
                }
            )
        if "semantic understanding" in system:
            await __import__("asyncio").sleep(0.04)
            return json.dumps(
                {
                    "paper_outline": "Introduction, method, experiments, conclusion.",
                    "research_problem_and_scope": "Automated evidence-grounded review.",
                    "contributions": [
                        {
                            "claim": "A staged review framework.",
                            "location": "Method",
                            "support_in_manuscript": "Algorithm and experiments.",
                        }
                    ],
                    "methodology": [],
                    "experiments_and_results": [],
                    "claim_evidence_map": [],
                    "strength_candidates": ["Explicit reasoning stages."],
                    "risk_candidates": [],
                    "reproducibility_ethics_and_limitations": [],
                    "questions_for_visual_or_literature_evidence": [],
                }
            )
        if "author revision strategist" in system:
            self.revision_prompt = user
            if self.fail_revision_plan:
                return r'{"revision_plan":{"revision_strategy":"invalid\_json"}}'
            return json.dumps(_revision_plan_payload())
        if "Repair a malformed detailed author revision plan" in system:
            if self.fail_revision_plan:
                return r'{"revision_plan":{"revision_strategy":"still\_bad"}}'
            return json.dumps(_revision_plan_payload())
        self.synthesis_saw_manuscript_analysis = (
            self.synthesis_saw_manuscript_analysis
            or "Earlier full-manuscript structured understanding" in user
        )
        return json.dumps(self.payload)


class _GroupedRefinementLLM:
    def __init__(self, fields: tuple[str, ...]) -> None:
        self.fields = fields
        self.calls = 0
        self.purposes: set[str] = set()
        self.group_prompts: list[str] = []
        self.revision_prompt = ""

    async def chat(self, _system: str, user: str, **_kwargs: Any) -> str:
        self.calls += 1
        if "Exact output schema:" in user and '"revision_plan"' in user:
            self.revision_prompt = user
            return json.dumps(_revision_plan_payload())
        if "Group purpose:" not in user:
            base = _payload(self.fields)
            base["review_fields"] = {
                field: "Concise initial assessment." for field in self.fields
            }
            base["review_fields"]["Overall Assessment"] = "2.5/5 — Borderline."
            return json.dumps(base)

        purpose = user.split("Group purpose:", 1)[1].splitlines()[0].strip()
        self.purposes.add(purpose)
        self.group_prompts.append(user)
        if purpose == "paper overview":
            fields = {
                "Paper Summary": (
                    "The paper proposes a staged LLM review process, evaluates it on "
                    "the tasks described in Sections 3 and 4, and reports improved "
                    "agreement with expert assessments within the stated benchmark scope."
                )
            }
        elif purpose == "evidence-based strengths and weaknesses":
            fields = {
                "Summary Of Strengths": (
                    "Section 3 clearly separates evidence collection from final judgment, "
                    "which makes the reported review process easier to inspect."
                ),
                "Summary Of Weaknesses": (
                    "Major: Table 4 does not isolate the contribution of each reasoning "
                    "stage. This weakens the attribution claim; add a component-wise "
                    "ablation with uncertainty estimates."
                ),
            }
        elif purpose == "venue-native author feedback and questions":
            fields = {
                "Comments Suggestions And Typos": (
                    "1. Add the Table 4 component ablation and report confidence intervals.\n"
                    "\n### Potentially Missing Related Work\n"
                    "2. Compare the staged design with TreeReview "
                    "(https://www.semanticscholar.org/paper/tree-review), which overlaps "
                    "in hierarchical review decomposition; position the distinction in "
                    "Related Work and the discussion of the method."
                )
            }
        else:
            fields = {
                field: (
                    f"Venue-scale judgment for {field}, grounded in the supplied "
                    "manuscript evidence and stated evidence boundaries."
                )
                for field in self.fields
            }
            fields["Overall Assessment"] = (
                "2.5/5 — Borderline findings. The staged design is promising, but the "
                "missing component ablation prevents confident attribution of the gains."
            )
        result: dict[str, Any] = {"review_fields": fields}
        if purpose.startswith("venue scores"):
            result.update(
                {
                    "target_venue": "ACL · 2025 · Main Conference — Long Papers",
                    "reviewed_as": "ACL 2025 Main Conference — Long Papers",
                    "desk_rejection": {
                        field: "Evidence-bounded desk assessment."
                        for field in (
                            "Paper Length",
                            "Topic Compatibility",
                            "Minimum Quality",
                            "Prompt Injection and Hidden Manipulation Detection",
                        )
                    },
                    "disclaimer": "Simulated author-facing review.",
                }
            )
        return json.dumps(result)


class _MalformedGroupJSONLLM:
    def __init__(self, *, repair_succeeds: bool) -> None:
        self.repair_succeeds = repair_succeeds
        self.calls = 0
        self.repair_prompt = ""

    async def chat(self, system: str, user: str, **_kwargs: Any) -> str:
        self.calls += 1
        if self.calls == 1:
            return (
                r'{"review_fields":{"Comments Suggestions And Typos":"### Potentially '
                r'Missing Related Work\nTreeReview overlaps DeepReview\_Bench"}}'
            )
        self.repair_prompt = f"{system}\n{user}"
        if not self.repair_succeeds:
            return r'{"review_fields":{"Comments Suggestions And Typos":"still bad\_"}}'
        return json.dumps(
            {
                "review_fields": {
                    "Comments Suggestions And Typos": (
                        "### Potentially Missing Related Work\n"
                        "[TreeReview](https://www.semanticscholar.org/paper/tree-review) "
                        "uses hierarchical review decomposition; compare it in Related "
                        "Work and distinguish the evidence-verification stage."
                    )
                }
            }
        )


class _MalformedRevisionPlanLLM:
    def __init__(self, *, repair_succeeds: bool) -> None:
        self.repair_succeeds = repair_succeeds
        self.calls = 0
        self.repair_prompt = ""

    async def chat(self, system: str, user: str, **_kwargs: Any) -> str:
        self.calls += 1
        if self.calls == 1:
            return r'{"revision_plan":{"revision_strategy":"invalid\_json"}}'
        self.repair_prompt = f"{system}\n{user}"
        if self.repair_succeeds:
            return json.dumps(_revision_plan_payload())
        return r'{"revision_plan":{"revision_strategy":"still\_invalid"}}'


class _NestedVenueFieldLLM:
    def __init__(self, *, repair_succeeds: bool = True) -> None:
        self.repair_succeeds = repair_succeeds
        self.calls = 0
        self.first_prompt = ""
        self.repair_prompt = ""

    async def chat(self, system: str, user: str, **_kwargs: Any) -> str:
        self.calls += 1
        if self.calls == 1:
            self.first_prompt = f"{system}\n{user}"
            return json.dumps(
                {
                    "review_fields": {
                        "Overall Assessment": (
                            "4 — Conference.\n\n"
                            "### Paper Summary\n"
                            "A duplicated copy of the paper summary.\n\n"
                            "### Summary Of Strengths\n"
                            "A duplicated copy of the strengths."
                        )
                    }
                }
            )
        self.repair_prompt = f"{system}\n{user}"
        content = (
            "4 — Conference. The contribution is promising and empirically strong, "
            "although independent human validation remains important."
            if self.repair_succeeds
            else (
                "4 — Conference.\n\n### Paper Summary\n"
                "The duplicated summary is still present."
            )
        )
        return json.dumps({"review_fields": {"Overall Assessment": content}})


def _revision_plan_payload() -> dict[str, Any]:
    return {
        "revision_plan": {
            "revision_strategy": (
                "Resolve the central attribution risk first, then align the manuscript "
                "claims, related work, and presentation with the new evidence."
            ),
            "prioritized_actions": [
                {
                    "priority": "Critical",
                    "title": "Isolate the contribution of each review stage",
                    "review_concern": (
                        "The completed review finds that Table 4 cannot attribute gains "
                        "to individual reasoning stages."
                    ),
                    "paper_location": "Section 4 and Table 4",
                    "required_change": (
                        "Because the central mechanism claim is under-supported, add a "
                        "component-wise ablation and calibrate the attribution claim. "
                        "Evaluate each stage removal under the same data split, judge, and "
                        "decoding budget, then report paired effects, confidence intervals, "
                        "and a robustness check. Treat the action as complete only when "
                        "every claimed stage-level gain is supported or narrowed "
                        "by the new table and corresponding text; complete this analysis "
                        "before rewriting the abstract and conclusion claims."
                    ),
                }
            ],
            "experiments_and_analysis": (
                "Add the Table 4 ablation, uncertainty estimates, and judge-sensitivity "
                "analysis using the existing evaluation split."
            ),
            "manuscript_and_related_work_edits": (
                "Treat TreeReview (https://www.semanticscholar.org/paper/tree-review) "
                "as possible later context; compare it only if its publication date "
                "predates the submission deadline."
            ),
            "figures_tables_formulas_writing_and_typos": (
                "Update the Table 4 caption after the ablation and correct the identified "
                "model-name typo."
            ),
            "final_verification": (
                "Recheck that the abstract, contribution list, tables, and conclusion all "
                "make the same bounded attribution claim."
            ),
        }
    }


def _payload(fields: tuple[str, ...]) -> dict[str, Any]:
    review_fields = {
        field: (f"Detailed evidence and rationale for {field}. " * 10).strip()
        for field in fields
    }
    review_fields["Paper Summary"] = "Summary of methods, data, and results. " * 35
    review_fields["Summary Of Strengths"] = (
        "- Evidence-grounded strength with a precise manuscript locator and impact.\n"
        * 30
    )
    review_fields["Summary Of Weaknesses"] = (
        "- Major weakness with location, acceptance impact, and a concrete remedy.\n"
        * 52
    )
    review_fields["Comments Suggestions And Typos"] = (
        "- Actionable revision with a specific verification request.\n" * 24
    )
    review_fields["Overall Assessment"] = (
        "2.5/5 — Borderline findings. The evidence is promising but the central "
        "evaluation needs stronger independent validation."
    )
    return {
        "target_venue": "ACL · 2025 · Main Conference — Long Papers",
        "reviewed_as": "ACL  \\n2025  \\nMain Conference — Long Papers",
        "desk_rejection": {
            "Paper Length": "Pass with the supplied evidence boundary.",
            "Topic Compatibility": "Pass; the topic is relevant to ACL.",
            "Minimum Quality": "Pass; the manuscript is reviewable.",
            "Prompt Injection and Hidden Manipulation Detection": "No hidden manipulation was found in extracted text.",
        },
        "review_fields": review_fields,
        "disclaimer": "This is an author-facing simulated review, not an official ACL review.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_revision_plan", "expected_status", "expected_calls"),
    ((False, "partial", 7), (True, "partial", 8)),
)
async def test_engine_starts_mineru_first_and_overlaps_semantic_scholar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fail_revision_plan: bool,
    expected_status: str,
    expected_calls: int,
) -> None:
    module = _load_engine()
    venue = module._core.resolve_venue(
        "ACL 2025 Main Conference — Long Papers",
        SKILL_DIR / "references" / "venues",
    )
    llm = _ReviewLLM(
        _payload(venue.fields),
        fail_revision_plan=fail_revision_plan,
    )
    visual_started = False
    literature_started = False
    stage_starts: list[str] = []
    visual_options: dict[str, Any] = {}

    monkeypatch.setattr(
        module,
        "resolve_connector",
        lambda _ctx, _name: SimpleNamespace(
            secrets={"semantic_scholar_api_key": "test-s2-key"}
        ),
    )

    async def fake_extract(
        _source_path: Path | None,
        _supplied_text: str,
        timings: dict[str, float],
        started: float,
    ) -> dict[str, Any]:
        stage_starts.append("text")
        timings["text_start_offset_seconds"] = time.monotonic() - started
        await __import__("asyncio").sleep(0.01)
        timings["text_end_offset_seconds"] = time.monotonic() - started
        timings["text_extraction_seconds"] = (
            timings["text_end_offset_seconds"]
            - timings["text_start_offset_seconds"]
        )
        return {
            "source": "paper.pdf",
            "title": "DeepReview",
            "abstract": "A staged LLM paper-review framework.",
            "sections": {"introduction": 10},
            "text": "DeepReview manuscript evidence. " * 100,
        }

    async def fake_visual(
        _self: Any,
        _pdf_path: Path,
        *,
        timings: dict[str, float],
        started: float,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal visual_started
        visual_started = True
        visual_options.update(_kwargs)
        stage_starts.append("visual")
        timings["visual_start_offset_seconds"] = time.monotonic() - started
        while not literature_started:
            await __import__("asyncio").sleep(0.001)
        while "manuscript_analysis_start_offset_seconds" not in timings:
            await __import__("asyncio").sleep(0.001)
        await __import__("asyncio").sleep(0.02)
        timings["visual_end_offset_seconds"] = time.monotonic() - started
        timings["visual_seconds"] = (
            timings["visual_end_offset_seconds"]
            - timings["visual_start_offset_seconds"]
        )
        visual_report = tmp_path / "visual-evidence.md"
        visual_report.write_text("# Visual evidence", encoding="utf-8")
        mineru_log = tmp_path / "mineru.stderr.log"
        mineru_log.write_text("diagnostic", encoding="utf-8")
        return {
            "status": "ok",
            "summary": "one figure reviewed",
            "selected_count": 1,
            "reviewed_count": 1,
            "severity_counts": {"major": 0, "minor": 0},
            "visual_evidence": [],
            "warnings": [],
            "artifacts": [
                {
                    "title": "Paper-review visual evidence",
                    "format": "md",
                    "path": str(visual_report),
                    "mime": "text/markdown",
                },
                {
                    "title": "MinerU error output",
                    "format": "log",
                    "path": str(mineru_log),
                    "mime": "text/plain",
                },
            ],
        }

    async def fake_literature(
        queries: list[str],
        *,
        timings: dict[str, float],
        started: float,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal literature_started
        assert visual_started is True
        literature_started = True
        timings["literature_start_offset_seconds"] = time.monotonic() - started
        while "manuscript_analysis_start_offset_seconds" not in timings:
            await __import__("asyncio").sleep(0.001)
        await __import__("asyncio").sleep(0.02)
        timings["literature_end_offset_seconds"] = time.monotonic() - started
        timings["literature_seconds"] = (
            timings["literature_end_offset_seconds"]
            - timings["literature_start_offset_seconds"]
        )
        return {
            "status": "ok",
            "queries": queries,
            "raw_count": 1,
            "candidate_count": 1,
            "candidates": [
                {
                    "title": "Related Reviewer",
                    "year": "2024",
                    "url": "https://example.test/paper",
                    "summary": "Related abstract.",
                }
            ],
            "errors": [],
            "retrieval_limited": True,
            "source": "Semantic Scholar",
        }

    monkeypatch.setattr(module, "_extract_structure", fake_extract)
    monkeypatch.setattr(module.PaperReviewEngine, "_run_visual_stage", fake_visual)
    monkeypatch.setattr(module, "_retrieve_semantic_scholar", fake_literature)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    engine = module.PaperReviewEngine()
    engine.ctx = SimpleNamespace(
        llm=llm,
        settings=SimpleNamespace(),
        working_dir=tmp_path,
        artifacts=None,
        db=None,
    )

    result = await engine.execute(
        input=str(pdf),
        venue="ACL 2025 Main Conference — Long Papers",
        mineru_command="/opt/mineru-venv/bin/mineru",
        mineru_backend="pipeline",
        mineru_timeout_s=37.5,
        mineru_device="cuda:3",
    )

    assert result["status"] == expected_status
    # Query generation + manuscript understanding + integrated draft + three
    # displayed-field refinements + the sequential revision plan, plus one
    # repair in the failure case.
    assert llm.calls == expected_calls
    assert llm.synthesis_saw_manuscript_analysis is True
    assert result["outcome"]["refinement_status"] == "ok"
    assert result["outcome"]["failed_refinement_groups"] == []
    assert result["outcome"]["revision_plan_status"] == (
        "partial" if fail_revision_plan else "ok"
    )
    assert result["outcome"]["effective_inputs"] == {
        "input": str(pdf.resolve()),
        "venue": "ACL 2025 Main Conference — Long Papers",
        "mode": "standard",
        "output_language": "English",
        "skip_visual": False,
    }
    if fail_revision_plan:
        assert "after one repair" in result["warning"]
    assert visual_options["mineru_command"] == "/opt/mineru-venv/bin/mineru"
    assert visual_options["mineru_backend"] == "pipeline"
    assert visual_options["mineru_timeout_s"] == 37.5
    assert visual_options["mineru_device"] == "cuda:3"
    assert stage_starts[:2] == ["visual", "text"]
    assert result["timings"]["visual_start_offset_seconds"] <= result["timings"][
        "text_start_offset_seconds"
    ]
    assert result["timings"]["evidence_overlap_seconds"] > 0
    assert result["timings"]["visual_manuscript_overlap_seconds"] > 0
    assert result["timings"]["visual_literature_overlap_seconds"] > 0
    assert result["timings"]["three_way_overlap_seconds"] > 0
    assert (
        result["timings"]["manuscript_analysis_start_offset_seconds"]
        >= result["timings"]["text_end_offset_seconds"]
    )
    assert (
        result["timings"]["literature_start_offset_seconds"]
        < result["timings"]["manuscript_analysis_end_offset_seconds"]
    )
    assert result["manuscript_understanding"]["status"] == "ok"
    assert result["manuscript_understanding"]["coverage"]["complete"] is True
    assert result["literature_review"]["queries"]
    report = result["text"]
    assert report.count("# Target Venue") == 1
    assert report.count("# Reviewed as if submitted to") == 1
    assert report.count("# Desk Rejection Assessment") == 1
    assert report.count("# Expected Review Outcome") == 1
    assert report.count("# Disclaimer") == 1
    displayed_fields = module._displayed_review_fields(venue.fields)
    for field in displayed_fields:
        assert report.count(f"## {field}\n") == 1
    assert "## Comments Suggestions And Typos\n" not in report
    assert report.count("# Detailed Revision Plan\n") == 1
    assert report.rfind("# Detailed Revision Plan") > report.rfind("# Disclaimer")
    if fail_revision_plan:
        assert result["suggestions"] == []
    else:
        assert result["suggestions"]
        assert "1. **Critical — Isolate the contribution of each review stage**" in report
        assert "**Review concern:**" in report
        assert "**Paper location:**" in report
        assert "**Required change:**" in report
        for removed_label in (
            "**Why it matters:**",
            "**Implementation details:**",
            "**New evidence or validation:**",
            "**Completion criterion:**",
            "**Dependencies or trade-offs:**",
        ):
            assert removed_label not in report
        assert "### 1. Critical" not in report
        assert "# Expected Review Outcome" in llm.revision_prompt
        assert "DeepReview manuscript evidence" in llm.revision_prompt
    assert Path(result["artifacts"][0]["path"]).is_file()
    assert Path(result["output_path"]).is_file()
    assert result["presentation"]["completion_mode"] == "artifact_links"
    assert result["presentation"]["summary"].endswith("paper review.")
    assert result["presentation"]["artifacts"][0]["path"] == result["output_path"]
    assert len(result["presentation"]["artifacts"]) == 1
    assert result["recoverable"] is False


@pytest.mark.asyncio
async def test_a_keyless_semantic_scholar_yields_a_thinner_review_not_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The registry calls this connector degraded, and retrieval already says so.

    ``_retrieve_semantic_scholar`` reports a thin batch as ``partial`` with
    ``retrieval_limited`` set, so a public-tier key costs citations, not the
    review. Refusing in front of it contradicted the registry and threw away the
    manuscript analysis — which never needed the connector at all.
    """
    module = _load_engine()
    monkeypatch.setattr(
        module,
        "resolve_connector",
        lambda _ctx, _name: SimpleNamespace(
            secrets={"semantic_scholar_api_key": ""}
        ),
    )
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    engine = module.PaperReviewEngine()
    engine.ctx = SimpleNamespace(
        llm=_ReviewLLM({}),
        settings=SimpleNamespace(),
        working_dir=tmp_path,
    )

    result = await engine.execute(input=str(pdf), venue="ACL 2025")

    assert result.get("error_info", {}).get("code") != "semantic_scholar_api_key_missing"


@pytest.mark.asyncio
async def test_a_disabled_semantic_scholar_still_yields_a_complete_thin_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_engine()
    venue = module._core.resolve_venue(
        "ACL 2025 Main Conference — Long Papers",
        SKILL_DIR / "references" / "venues",
    )
    llm = _ReviewLLM(_payload(venue.fields))
    connector_calls = 0
    progress: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(module, "resolve_connector", lambda _ctx, _name: None)

    async def forbidden_search(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal connector_calls
        connector_calls += 1
        raise AssertionError("a disabled connector must not be called")

    monkeypatch.setattr(
        module.connectors,
        "semanticscholar_search",
        forbidden_search,
    )
    manuscript = (
        "DeepReview Evidence-grounded Automated Peer Review\n\n"
        "Abstract\nWe introduce a staged review framework and evaluate its "
        "agreement with expert assessments.\n\n1 Introduction\n"
        + "Complete manuscript evidence for the staged framework. " * 80
    )
    engine = module.PaperReviewEngine()
    engine.ctx = SimpleNamespace(
        llm=llm,
        settings=SimpleNamespace(),
        working_dir=tmp_path,
        artifacts=None,
        db=None,
    )

    async def capture_progress(
        stage: str,
        _fraction: float,
        **data: Any,
    ) -> None:
        progress.append((stage, data))

    result = await engine.execute(
        input=manuscript,
        venue="ACL 2025 Main Conference — Long Papers",
        review_rag="off",
        preference_rag="off",
        progress_callback=capture_progress,
    )

    assert connector_calls == 0
    assert result["status"] == "partial", result
    assert result["outcome"]["code"] == "review_complete_with_evidence_gaps"
    assert result["literature_review"]["status"] == "unavailable"
    assert result["literature_review"]["outcome"]["code"] == (
        "semantic_scholar_disabled"
    )
    assert "Semantic Scholar is disabled" in result["warning"]
    assert result["text"].startswith("# Target Venue")
    assert "# Detailed Revision Plan" in result["text"]
    assert Path(result["output_path"]).is_file()
    warning_events = [
        (stage, data)
        for stage, data in progress
        if data.get("severity") == "warning"
    ]
    assert warning_events == [
        (
            "WARNING: Semantic Scholar is disabled. Paper Review will continue, "
            "but related-work evidence will be incomplete.",
            {
                "stage_id": "paper-review.literature.warning",
                "severity": "warning",
            },
        )
    ]
    assert module._SEMANTIC_SCHOLAR_ENABLE_COMMAND in result["next_actions"]


def test_engine_resolves_at_attachment_with_spaces_and_trailing_instruction(
    tmp_path: Path,
) -> None:
    module = _load_engine()
    pdf = tmp_path / "Worldlines in the Mean Field Real Town.pdf"
    pdf.write_bytes(b"%PDF-test")

    source, supplied_text = module._resolve_input(f"@{pdf} 请审稿")

    assert source == pdf.resolve()
    assert supplied_text == ""


@pytest.mark.asyncio
async def test_text_extraction_failure_saves_recovery_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_engine()
    monkeypatch.setattr(
        module,
        "resolve_connector",
        lambda _ctx, _name: SimpleNamespace(
            secrets={"semantic_scholar_api_key": "test-s2-key"}
        ),
    )

    async def broken_extract(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("parser stopped on a damaged object stream")

    monkeypatch.setattr(module, "_extract_structure", broken_extract)
    pdf = tmp_path / "paper with spaces.pdf"
    pdf.write_bytes(b"%PDF-test")
    engine = module.PaperReviewEngine()
    engine.ctx = SimpleNamespace(
        llm=_ReviewLLM({}),
        settings=SimpleNamespace(),
        working_dir=tmp_path,
        artifacts=None,
    )

    result = await engine.execute(
        input=f"@{pdf} 请审稿",
        venue="ACL 2025",
        skip_visual=True,
    )

    assert result["status"] == "error"
    assert result["outcome"]["code"] == "paper_text_extraction_failed"
    assert result["checkpoint"]["stage"] == "paper text extraction"
    checkpoint = Path(result["artifacts"][0]["path"])
    assert checkpoint.is_file()
    content = checkpoint.read_text(encoding="utf-8")
    assert "not a complete peer review" in content
    assert "parser stopped on a damaged object stream" in content


@pytest.mark.asyncio
async def test_failed_pdf_parser_auto_install_emits_a_warning_and_repair_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_engine()
    monkeypatch.setattr(
        module,
        "resolve_connector",
        lambda _ctx, _name: SimpleNamespace(
            secrets={"semantic_scholar_api_key": "test-s2-key"}
        ),
    )

    async def missing_parser(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise module._extractor.PdfParserUnavailableError("no parser")

    monkeypatch.setattr(module, "_extract_structure", missing_parser)

    def failed_install(_cache_dir: Path) -> dict[str, Any]:
        raise module._pdf_runtime.PdfRuntimeInstallError("package index offline")

    monkeypatch.setattr(
        module._pdf_runtime,
        "ensure_pypdf_runtime",
        failed_install,
    )
    progress: list[tuple[str, dict[str, Any]]] = []

    async def capture_progress(
        stage: str,
        _fraction: float,
        **data: Any,
    ) -> None:
        progress.append((stage, data))

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    engine = module.PaperReviewEngine()
    engine.ctx = SimpleNamespace(
        llm=_ReviewLLM({}),
        settings=SimpleNamespace(),
        working_dir=tmp_path,
        artifacts=None,
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
    )

    result = await engine.execute(
        input=str(pdf),
        venue="ACL 2025",
        skip_visual=True,
        progress_callback=capture_progress,
    )

    assert result["status"] == "error"
    assert result["outcome"]["code"] == "pdf_parser_auto_install_failed"
    assert result["setup_command"] == "omni update"
    assert result["action_required"] == {
        "kind": "install",
        "command": "omni update",
    }
    assert any(
        data.get("severity") == "warning"
        and "installing pinned pypdf into its private cache" in stage
        for stage, data in progress
    )
    assert any(
        data.get("severity") == "warning"
        and "Automatic installation" in stage
        and "package index offline" in stage
        for stage, data in progress
    )


@pytest.mark.asyncio
async def test_missing_pdf_parser_auto_installs_and_retries_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_engine()
    extraction_calls = 0
    install_calls: list[Path] = []
    progress: list[tuple[str, dict[str, Any]]] = []

    async def extract_after_repair(
        _source_path: Path | None,
        _supplied_text: str,
        timings: dict[str, float],
        started: float,
    ) -> dict[str, Any]:
        nonlocal extraction_calls
        extraction_calls += 1
        if extraction_calls == 1:
            raise module._extractor.PdfFallbackUnavailableError(
                "primary failed and fallback is absent"
            )
        timings["text_start_offset_seconds"] = time.monotonic() - started
        timings["text_end_offset_seconds"] = time.monotonic() - started
        timings["text_extraction_seconds"] = 0.0
        return {
            "source": "paper.pdf",
            "title": "DeepReview",
            "abstract": "A staged review framework.",
            "sections": {"introduction": 10},
            "text": "Complete manuscript evidence. " * 100,
        }

    def successful_install(cache_dir: Path) -> dict[str, Any]:
        install_calls.append(cache_dir)
        return {
            "installed": True,
            "package": "pypdf==6.14.2",
            "runtime_dir": str(cache_dir / "runtime"),
        }

    async def capture_progress(
        stage: str,
        _fraction: float,
        **data: Any,
    ) -> None:
        progress.append((stage, data))

    monkeypatch.setattr(module, "_extract_structure", extract_after_repair)
    monkeypatch.setattr(
        module._pdf_runtime,
        "ensure_pypdf_runtime",
        successful_install,
    )
    cache_dir = tmp_path / "cache"
    structure = await module._extract_structure_with_pdf_repair(
        tmp_path / "paper.pdf",
        "",
        {},
        time.monotonic(),
        ctx=SimpleNamespace(paths=SimpleNamespace(cache_dir=cache_dir)),
        progress_callback=capture_progress,
    )

    assert structure["title"] == "DeepReview"
    assert extraction_calls == 2
    assert install_calls == [cache_dir]
    assert any(data.get("severity") == "warning" for _, data in progress)
    assert [data.get("milestone") for _, data in progress if data.get("milestone")] == [
        "PDF fallback parser ready"
    ]


@pytest.mark.asyncio
async def test_engine_immediately_surfaces_missing_vlm_and_continues_text_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_engine()
    venue = module._core.resolve_venue(
        "ACL 2025 Main Conference — Long Papers",
        SKILL_DIR / "references" / "venues",
    )
    llm = _ReviewLLM(_payload(venue.fields))
    progress: list[str] = []
    monkeypatch.setattr(
        module,
        "resolve_connector",
        lambda _ctx, _name: SimpleNamespace(
            secrets={"semantic_scholar_api_key": "test-s2-key"}
        ),
    )

    async def fake_extract(
        source_path: Path | None,
        _supplied_text: str,
        timings: dict[str, float],
        started: float,
    ) -> dict[str, Any]:
        timings["text_start_offset_seconds"] = time.monotonic() - started
        timings["text_end_offset_seconds"] = time.monotonic() - started
        timings["text_extraction_seconds"] = 0.0
        return {
            "source": str(source_path),
            "title": "Text-only DeepReview test",
            "abstract": "A paper reviewed by a text-only primary model.",
            "sections": {"Introduction": "..."},
            "text": "Complete manuscript evidence. " * 500,
        }

    async def fake_visual(
        _self: Any,
        _pdf_path: Path,
        *,
        timings: dict[str, float],
        started: float,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        timings["visual_start_offset_seconds"] = time.monotonic() - started
        timings["visual_end_offset_seconds"] = time.monotonic() - started
        timings["visual_seconds"] = 0.0
        return {
            "status": "partial",
            "summary": "MinerU crops extracted without visual interpretation.",
            "visual_evidence": [],
            "artifacts": [],
            "warnings": ["No vision-language model is configured."],
            "outcome": {"code": "vlm_not_configured"},
            "configuration_notice": "No separate VLM is configured.",
            "setup_command": (
                "omni config vlm -u <ENDPOINT> -m <VISION_MODEL> "
                "-k <API_KEY> --test"
            ),
            "next_actions": [
                "configure a vision-capable model",
                "or set `skip_visual=true`",
            ],
            "recoverable": True,
            "blocking": False,
        }

    async def fake_literature(
        queries: list[str],
        *,
        timings: dict[str, float],
        started: float,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        timings["literature_start_offset_seconds"] = time.monotonic() - started
        timings["literature_end_offset_seconds"] = time.monotonic() - started
        timings["literature_seconds"] = 0.0
        return {
            "status": "ok",
            "queries": queries,
            "raw_count": 0,
            "candidate_count": 0,
            "candidates": [],
            "errors": [],
            "retrieval_limited": False,
            "source": "Semantic Scholar",
        }

    monkeypatch.setattr(module, "_extract_structure", fake_extract)
    monkeypatch.setattr(module.PaperReviewEngine, "_run_visual_stage", fake_visual)
    monkeypatch.setattr(module, "_retrieve_semantic_scholar", fake_literature)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    engine = module.PaperReviewEngine()
    engine.ctx = SimpleNamespace(
        llm=llm,
        vlm=None,
        settings=SimpleNamespace(),
        working_dir=tmp_path,
        artifacts=None,
        db=None,
    )

    async def capture(message: str, _fraction: float) -> None:
        progress.append(message)

    result = await engine.execute(
        input=str(pdf),
        venue="ACL 2025 Main Conference — Long Papers",
        progress_callback=capture,
    )

    assert result["status"] == "partial"
    assert "No separate VLM was configured" in result["warning"]
    assert result["setup_command"].startswith("omni config vlm")
    assert any("skip_visual=true" in item for item in result["next_actions"])
    assert result["visual_review"]["outcome"]["code"] == "vlm_not_configured"
    assert any(message.startswith("No VLM is configured") for message in progress)
    assert "# Expected Review Outcome" in result["text"]


@pytest.mark.asyncio
async def test_engine_emits_a_native_completion_milestone_with_venue_and_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """paper-review reports a typed completion milestone the CLI can compress to
    one durable ✓ line — with the venue it settled on and the source count only
    the engine knows. Legacy 2-arg callbacks still work via _emit's fallback."""
    module = _load_engine()
    venue = module._core.resolve_venue(
        "ACL 2025 Main Conference — Long Papers",
        SKILL_DIR / "references" / "venues",
    )
    llm = _ReviewLLM(_payload(venue.fields))
    monkeypatch.setattr(
        module,
        "resolve_connector",
        lambda _ctx, _name: SimpleNamespace(
            secrets={"semantic_scholar_api_key": "test-s2-key"}
        ),
    )

    async def fake_extract(
        source_path: Path | None,
        _supplied_text: str,
        timings: dict[str, float],
        started: float,
    ) -> dict[str, Any]:
        timings["text_extraction_seconds"] = 0.0
        return {
            "source": str(source_path),
            "title": "Native milestone test",
            "abstract": "A paper whose review reports a native milestone.",
            "sections": {"Introduction": "..."},
            "text": "Complete manuscript evidence. " * 500,
        }

    async def fake_visual(
        _self: Any, _pdf_path: Path, *, timings: dict[str, float], started: float, **_kw: Any
    ) -> dict[str, Any]:
        timings["visual_seconds"] = 0.0
        return {"status": "ok", "summary": "", "visual_evidence": [], "artifacts": []}

    async def fake_literature(
        queries: list[str], *, timings: dict[str, float], started: float, **_kw: Any
    ) -> dict[str, Any]:
        timings["literature_seconds"] = 0.0
        return {
            "status": "ok",
            "queries": queries,
            "raw_count": 3,
            "candidate_count": 3,
            "candidates": [],
            "errors": [],
            "retrieval_limited": False,
            "source": "Semantic Scholar",
        }

    monkeypatch.setattr(module, "_extract_structure", fake_extract)
    monkeypatch.setattr(module.PaperReviewEngine, "_run_visual_stage", fake_visual)
    monkeypatch.setattr(module, "_retrieve_semantic_scholar", fake_literature)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    engine = module.PaperReviewEngine()
    engine.ctx = SimpleNamespace(
        llm=llm, vlm=None, settings=SimpleNamespace(),
        working_dir=tmp_path, artifacts=None, db=None,
    )

    events: list[dict[str, Any]] = []

    async def capture(message: str, fraction: float, **data: Any) -> None:
        events.append({"message": message, **data})

    await engine.execute(
        input=str(pdf),
        venue="ACL 2025 Main Conference — Long Papers",
        skip_visual=True,
        progress_callback=capture,
    )

    milestone_event = next((e for e in events if e.get("milestone")), None)
    assert milestone_event is not None
    assert milestone_event["milestone"] == "Paper review complete"
    assert milestone_event.get("stage_id") == "review.done"
    stats = milestone_event.get("stats") or {}
    assert stats.get("sources") == 3
    assert "ACL 2025" in str(stats.get("venue", ""))


@pytest.mark.asyncio
async def test_a_legacy_two_arg_progress_callback_still_receives_completion() -> None:
    """The milestone retrofit must not break a callback that accepts only
    (message, fraction): _emit falls back to the positional call."""
    module = _load_engine()
    received: list[str] = []

    async def legacy(message: str, _fraction: float) -> None:
        received.append(message)

    await module._emit(
        legacy,
        "Complete paper review saved",
        1.0,
        stage_id="review.done",
        milestone="Paper review complete",
        stats={"sources": 3},
    )
    assert received == ["Complete paper review saved"]


@pytest.mark.asyncio
async def test_synthesis_refines_complete_review_without_a_length_gate() -> None:
    module = _load_engine()
    venue = module._core.resolve_venue(
        "ACL 2025 Main Conference — Long Papers",
        SKILL_DIR / "references" / "venues",
    )
    displayed_fields = module._displayed_review_fields(venue.fields)
    llm = _GroupedRefinementLLM(displayed_fields)
    profile = (SKILL_DIR / "references" / "venues" / venue.profile_filename).read_text()
    structure = {
        "source": "published-paper.pdf",
        "title": "DeepReview",
        "abstract": "A staged LLM paper-review framework.",
        "sections": {"Introduction": "...", "Experiments": "..."},
        "text": (
            "Proceedings of the 63rd Annual Meeting of the Association for "
            "Computational Linguistics. Manuscript evidence. "
            * 500
        ),
    }

    manuscript_analysis = {
        "status": "ok",
        "summary": "Full manuscript analyzed.",
        "coverage": {"complete": True, "analysis_call_count": 1},
        "analysis": {"paper_outline": "Complete outline."},
        "warnings": [],
    }
    visual_result = {"status": "ok", "visual_evidence": []}
    literature_result = {
        "queries": ["hierarchical LLM paper review"],
        "candidates": [
            {
                "title": "TreeReview",
                "url": "https://www.semanticscholar.org/paper/tree-review",
                "year": 2025,
            }
        ],
        "errors": [],
    }
    payload, warnings = await module._synthesize_review(
        llm,
        structure=structure,
        venue=venue,
        review_fields=displayed_fields,
        profile_text=profile,
        mode="standard",
        language="English",
        manuscript_analysis=manuscript_analysis,
        visual_result=visual_result,
        literature_result=literature_result,
    )

    assert llm.calls == 4
    assert llm.purposes == {
        "paper overview",
        "evidence-based strengths and weaknesses",
        "venue scores, responsible-review checks, and form metadata",
    }
    assert warnings == []
    assert payload["review_fields"]["Paper Summary"].startswith("The paper proposes")
    assert "Section 3" in payload["review_fields"]["Summary Of Strengths"]
    assert "Table 4" in payload["review_fields"]["Summary Of Weaknesses"]
    assert "Comments Suggestions And Typos" not in payload["review_fields"]
    assert "missing component ablation" in payload["review_fields"][
        "Overall Assessment"
    ]
    group_instructions = "\n".join(llm.group_prompts)
    for hard_quota in (
        "300-450",
        "5-7",
        "6-10",
        "8-12",
        "1,500-2,300",
        "900-1,400",
        "1,000-1,600",
        "words across",
        "Aim for",
    ):
        assert hard_quota not in group_instructions
    assert "final implementation plan" not in group_instructions
    assert "cannot be verified from this copy alone" in payload["desk_rejection"][
        "Paper Length"
    ]
    assert "selected visual crops only" in payload["desk_rejection"][
        "Prompt Injection and Hidden Manipulation Detection"
    ]
    assert "did not download, open, or execute" in payload["review_fields"]["Software"]
    assert "were not executed or independently verified" in payload["disclaimer"]

    completed_review = module._core.render_review(
        payload,
        displayed_fields,
        requested_venue=venue.requested,
    )
    plan, plan_warnings, plan_status = await module._synthesize_revision_plan(
        llm,
        structure=structure,
        venue=venue,
        mode="standard",
        language="English",
        completed_review=completed_review,
        manuscript_analysis=manuscript_analysis,
        visual_result=visual_result,
        literature_result=literature_result,
    )

    assert plan_status == "ok"
    assert plan_warnings == []
    assert llm.calls == 5
    assert "Table 4" in llm.revision_prompt
    assert "Original untrusted manuscript text" in llm.revision_prompt
    assert "TreeReview" in llm.revision_prompt
    assert "completed first-stage review" in llm.revision_prompt.casefold()
    assert "frozen" not in llm.revision_prompt.casefold()
    assert "issue boundary" not in llm.revision_prompt.casefold()
    for required_detail in (
        "review concern",
        "paper location",
        "required change",
        "rationale",
        "execution steps",
        "evidence or validation",
        "completion criterion",
        "dependency or trade-off",
    ):
        assert required_detail in llm.revision_prompt.casefold()
    assert (
        "do not impose a word, character, or fixed item-count quota"
        in llm.revision_prompt.casefold()
    )
    for evidence_boundary in (
        "keep evidence and proposed study design separate",
        "clearly label an example illustrative",
        "treat mineru/vlm observations as crop-level evidence",
        "check the original pdf instead of declaring",
    ):
        assert evidence_boundary in llm.revision_prompt.casefold()
    payload["revision_plan"] = plan
    final = module._core.render_review(
        payload,
        displayed_fields,
        requested_venue=venue.requested,
    )
    assert "## Comments Suggestions And Typos" not in final
    assert final.count("# Detailed Revision Plan") == 1
    assert final.rfind("# Detailed Revision Plan") > final.rfind("# Disclaimer")
    assert "https://www.semanticscholar.org/paper/tree-review" in final


@pytest.mark.asyncio
async def test_detailed_revision_plan_repairs_structure_once() -> None:
    module = _load_engine()
    llm = _MalformedRevisionPlanLLM(repair_succeeds=True)
    venue = SimpleNamespace(requested="ACL 2025 Main Conference — Long Papers")

    plan, warnings, status = await module._synthesize_revision_plan(
        llm,
        structure={
            "source": "paper.pdf",
            "title": "DeepReview",
            "abstract": "Paper review.",
            "sections": {"Experiments": 4},
            "text": "Complete current-paper evidence.",
        },
        venue=venue,
        mode="standard",
        language="English",
        completed_review=(
            "# Expected Review Outcome\n\n## Summary Of Weaknesses\n\n"
            "The main experiment lacks a controlled ablation."
        ),
        manuscript_analysis={"status": "ok", "analysis": {}},
        visual_result={"status": "ok", "visual_evidence": []},
        literature_result={"queries": [], "candidates": [], "errors": []},
    )

    assert llm.calls == 2
    assert status == "ok"
    assert plan["status"] == "ok"
    assert warnings == [
        "The detailed revision-plan response was malformed or incomplete; Omni "
        "repaired its structure once."
    ]
    assert "Repair a malformed detailed author revision plan" in llm.repair_prompt
    assert (
        "the main experiment lacks a controlled ablation"
        in llm.repair_prompt.casefold()
    )


def test_revision_plan_parser_drops_extra_fields_and_renderer_owned_heading() -> None:
    module = _load_engine()
    payload = _revision_plan_payload()
    payload["revision_plan"]["score"] = "10 — Accept"
    raw_action = payload["revision_plan"]["prioritized_actions"][0]
    raw_action["why_it_matters"] = "Legacy rationale that must be dropped."
    raw_action["implementation_details"] = "Legacy execution detail."
    raw_action["new_evidence_or_validation"] = "Legacy evidence request."
    raw_action["completion_criterion"] = "Legacy definition of done."
    raw_action["dependencies_or_tradeoffs"] = "Legacy dependency."
    plan = module._parse_revision_plan(json.dumps(payload))

    assert "score" not in plan
    action = plan["prioritized_actions"][0]
    assert set(action) == {
        "priority",
        "title",
        "review_concern",
        "paper_location",
        "required_change",
    }
    for legacy_detail in (
        "Legacy rationale that must be dropped.",
        "Legacy execution detail.",
        "Legacy evidence request.",
        "Legacy definition of done.",
        "Legacy dependency.",
    ):
        assert legacy_detail not in module._core.render_revision_plan(plan)
    rendered = module._core.render_revision_plan(plan)
    assert rendered.count("**Review concern:**") == 1
    assert rendered.count("**Paper location:**") == 1
    assert rendered.count("**Required change:**") == 1
    for removed_label in (
        "**Why it matters:**",
        "**Implementation details:**",
        "**New evidence or validation:**",
        "**Completion criterion:**",
        "**Dependencies or trade-offs:**",
    ):
        assert removed_label not in rendered
    bad = _revision_plan_payload()
    bad["revision_plan"]["final_verification"] = (
        "## Comments Suggestions And Typos\nDo not render this heading."
    )
    with pytest.raises(ValueError, match="renderer-owned or absorbed heading"):
        module._parse_revision_plan(json.dumps(bad))


@pytest.mark.asyncio
async def test_group_refinement_repairs_invalid_json_once() -> None:
    module = _load_engine()
    field = "Comments Suggestions And Typos"
    llm = _MalformedGroupJSONLLM(repair_succeeds=True)

    overlay, warning = await module._refine_one_group(
        llm,
        group={
            "purpose": "author revision plan and literature positioning",
            "fields": [field],
            "instructions": "Identify confidently missing related work.",
        },
        payload={"review_fields": {field: "Existing integrated-draft feedback."}},
        structure={"title": "DeepReview", "abstract": "Paper review."},
        venue=SimpleNamespace(requested="ACL 2025 Main Conference — Long Papers"),
        profile_text="ACL review contract",
        mode="standard",
        language="English",
        manuscript_evidence={"status": "ok"},
        visual_evidence={"status": "ok"},
        literature_evidence={
            "candidates": [
                {
                    "title": "TreeReview",
                    "url": "https://www.semanticscholar.org/paper/tree-review",
                    "year": 2025,
                }
            ]
        },
        paper_text="DeepReview manuscript.",
    )

    assert llm.calls == 2
    assert "Repair one malformed conference-review group response" in llm.repair_prompt
    assert "Omni repaired it once" in warning
    assert module._failed_refinement_groups([warning]) == []
    content = overlay["review_fields"][field]
    assert "Potentially Missing Related Work" in content
    assert "https://www.semanticscholar.org/paper/tree-review" in content


@pytest.mark.asyncio
async def test_unrepaired_group_json_is_reported_as_a_failed_group() -> None:
    module = _load_engine()
    field = "Comments Suggestions And Typos"
    purpose = "author revision plan and literature positioning"
    llm = _MalformedGroupJSONLLM(repair_succeeds=False)

    overlay, warning = await module._refine_one_group(
        llm,
        group={
            "purpose": purpose,
            "fields": [field],
            "instructions": "Identify confidently missing related work.",
        },
        payload={"review_fields": {field: "Existing integrated-draft feedback."}},
        structure={"title": "DeepReview", "abstract": "Paper review."},
        venue=SimpleNamespace(requested="ACL 2025 Main Conference — Long Papers"),
        profile_text="ACL review contract",
        mode="standard",
        language="English",
        manuscript_evidence={"status": "ok"},
        visual_evidence={"status": "ok"},
        literature_evidence={"candidates": []},
        paper_text="DeepReview manuscript.",
    )

    assert llm.calls == 2
    assert overlay == {}
    assert "after one repair" in warning
    assert module._failed_refinement_groups([warning]) == [purpose]


@pytest.mark.asyncio
async def test_assessment_refinement_isolated_and_repairs_nested_venue_fields() -> None:
    module = _load_engine()
    fields = (
        "Paper Summary",
        "Summary Of Strengths",
        "Overall Assessment",
    )
    llm = _NestedVenueFieldLLM()
    sentinel = "SIBLING_DRAFT_MUST_NOT_REACH_ASSESSMENT_GROUP"

    overlay, warning = await module._refine_one_group(
        llm,
        group={
            "purpose": "venue scores, responsible-review checks, and form metadata",
            "fields": ["Overall Assessment"],
            "instructions": "Give the venue score and its evidence-based rationale.",
        },
        payload={
            "review_fields": {
                "Paper Summary": sentinel,
                "Summary Of Strengths": "A strong empirical comparison.",
                "Overall Assessment": "3.5 — Borderline Conference.",
            }
        },
        structure={"title": "DeepReview", "abstract": "Paper review."},
        venue=SimpleNamespace(
            requested="ACL 2025 Main Conference — Long Papers",
            fields=fields,
        ),
        profile_text=(
            "- `Overall Assessment`: 1 Do Not Resubmit, 5 Consider For Award."
        ),
        mode="standard",
        language="English",
        manuscript_evidence={"status": "ok"},
        visual_evidence={"status": "ok"},
        literature_evidence={"candidates": []},
        paper_text="DeepReview manuscript.",
    )

    assert llm.calls == 2
    assert sentinel not in llm.first_prompt
    assert "Current assigned draft fields" in llm.first_prompt
    assert "nested venue field heading" in llm.repair_prompt
    assert "Omni repaired it once" in warning
    assessment = overlay["review_fields"]["Overall Assessment"]
    assert assessment.startswith("4 — Conference.")
    assert "### Paper Summary" not in assessment
    assert "### Summary Of Strengths" not in assessment


@pytest.mark.asyncio
async def test_unrepaired_nested_venue_fields_do_not_replace_the_base_review() -> None:
    module = _load_engine()
    purpose = "venue scores, responsible-review checks, and form metadata"
    llm = _NestedVenueFieldLLM(repair_succeeds=False)

    overlay, warning = await module._refine_one_group(
        llm,
        group={
            "purpose": purpose,
            "fields": ["Overall Assessment"],
            "instructions": "Give the venue score and its evidence-based rationale.",
        },
        payload={
            "review_fields": {
                "Paper Summary": "A clean summary.",
                "Overall Assessment": "3.5 — Borderline Conference.",
            }
        },
        structure={"title": "DeepReview", "abstract": "Paper review."},
        venue=SimpleNamespace(
            requested="ACL 2025 Main Conference — Long Papers",
            fields=("Paper Summary", "Overall Assessment"),
        ),
        profile_text="- `Overall Assessment`: ACL scale.",
        mode="standard",
        language="English",
        manuscript_evidence={"status": "ok"},
        visual_evidence={"status": "ok"},
        literature_evidence={"candidates": []},
        paper_text="DeepReview manuscript.",
    )

    assert llm.calls == 2
    assert overlay == {}
    assert "after one repair" in warning
    assert module._failed_refinement_groups([warning]) == [purpose]


def test_rendered_review_rejects_nested_venue_field_headings() -> None:
    module = _load_engine()
    fields = ("Paper Summary", "Overall Assessment")
    payload = {
        "target_venue": "ACL 2025",
        "reviewed_as": "ACL 2025 Main Conference",
        "desk_rejection": {
            field: "Evidence-bounded assessment."
            for field in module._core.DESK_FIELDS
        },
        "review_fields": {
            "Paper Summary": "A clean summary.",
            "Overall Assessment": (
                "4 — Conference.\n\n### Paper Summary\nA duplicated summary."
            ),
        },
        "disclaimer": "Simulated author-facing review.",
    }

    isolation_failures = module._core.review_field_isolation_failures(
        payload,
        fields,
    )
    assert isolation_failures == [
        "review_fields.Overall Assessment contains nested venue field heading: "
        "Paper Summary"
    ]
    markdown = module._core.render_review(
        payload,
        fields,
        requested_venue="ACL 2025",
    )
    assert (
        "nested venue field heading: Paper Summary inside Overall Assessment"
        in module._core.validate_rendered_review(markdown, fields)
    )


@pytest.mark.asyncio
async def test_manuscript_analysis_sends_complete_88k_text_in_one_call() -> None:
    module = _load_engine()

    class _CaptureLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.user = ""

        async def chat(self, _system: str, user: str, **_kwargs: Any) -> str:
            self.calls += 1
            self.user = user
            return json.dumps(
                {
                    "paper_outline": "Complete-paper outline.",
                    "research_problem_and_scope": "Whole-paper scope.",
                    "contributions": [],
                    "methodology": [],
                    "experiments_and_results": [],
                    "claim_evidence_map": [],
                    "strength_candidates": [],
                    "risk_candidates": [],
                    "reproducibility_ethics_and_limitations": [],
                    "questions_for_visual_or_literature_evidence": [],
                }
            )

    start_marker = "FULL_MANUSCRIPT_START_SENTINEL"
    middle_marker = "FULL_MANUSCRIPT_MIDDLE_SENTINEL"
    end_marker = "FULL_MANUSCRIPT_END_SENTINEL"
    text = start_marker + ("A" * 44_000) + middle_marker + ("B" * 44_000) + end_marker
    llm = _CaptureLLM()
    timings: dict[str, float] = {}

    result = await module._analyze_manuscript(
        llm,
        {
            "title": "Long Paper",
            "abstract": "An abstract.",
            "text": text,
        },
        language="English",
        timings=timings,
        started=time.monotonic(),
    )

    assert llm.calls == 1
    assert llm.user.count(start_marker) == 1
    assert llm.user.count(middle_marker) == 1
    assert llm.user.count(end_marker) == 1
    assert "Chunk:" not in llm.user
    assert "<paper>" in llm.user and "</paper>" in llm.user
    assert result["status"] == "ok"
    assert result["coverage"] == {
        "total_characters": len(text),
        "analysis_call_count": 1,
        "complete": True,
    }
    assert result["analysis"]["paper_outline"] == "Complete-paper outline."
    assert "chunk_analyses" not in result


@pytest.mark.asyncio
async def test_manuscript_analysis_repairs_invalid_json_without_model_retry() -> None:
    module = _load_engine()

    class _InvalidJSONLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, _system: str, _user: str, **_kwargs: Any) -> str:
            self.calls += 1
            return (
                '{paper_outline: "Recovered outline", '
                'research_problem_and_scope: "Recovered scope",}'
            )

    llm = _InvalidJSONLLM()
    text = "Complete manuscript evidence. " * 4_000
    result = await module._analyze_manuscript(
        llm,
        {"title": "Paper", "abstract": "Abstract", "text": text},
        language="English",
        timings={},
        started=time.monotonic(),
    )

    assert llm.calls == 1
    assert result["status"] == "ok"
    assert result["coverage"] == {
        "total_characters": len(text),
        "analysis_call_count": 1,
        "complete": True,
    }
    assert result["analysis"] == {
        "paper_outline": "Recovered outline",
        "research_problem_and_scope": "Recovered scope",
    }
    assert len(result["warnings"]) == 1
    assert "repaired locally with json_repair" in result["warnings"][0]
    assert "no additional model call" in result["warnings"][0]
    assert "chunk" not in result["warnings"][0].casefold()


@pytest.mark.asyncio
async def test_manuscript_analysis_unrepairable_json_stays_partial() -> None:
    module = _load_engine()

    class _UnrepairableLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, _system: str, _user: str, **_kwargs: Any) -> str:
            self.calls += 1
            return "not a JSON object"

    llm = _UnrepairableLLM()
    text = "Complete manuscript evidence. " * 4_000
    result = await module._analyze_manuscript(
        llm,
        {"title": "Paper", "abstract": "Abstract", "text": text},
        language="English",
        timings={},
        started=time.monotonic(),
    )

    assert llm.calls == 1
    assert result["status"] == "partial"
    assert result["coverage"]["complete"] is False
    assert result["analysis"] == {"analysis_unavailable": True}
    assert len(result["warnings"]) == 1
    assert result["warnings"][0].startswith("Full-manuscript analysis failed:")
    assert "local json_repair fallback failed" in result["warnings"][0]


def test_default_report_path_uses_venue_title_and_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_engine()
    monkeypatch.setattr(module, "_report_timestamp", lambda: "20260726-091623")
    venue = SimpleNamespace(requested="ACL 2025 Main Conference — Long Papers")

    path = module._report_path(
        SimpleNamespace(working_dir=tmp_path),
        structure={
            "title": (
                "DeepReview: Improving LLM-based Paper Review with "
                "Human-like Deep Thinking Process"
            )
        },
        venue=venue,
        output_path=None,
    )

    assert path == (
        tmp_path
        / "reviews"
        / (
            "omni-review-acl-2025-main-conference-long-papers-"
            "deepreview-improving-llm-based-paper-review-with-human-like-"
            "deep-thinking-process-20260726-091623.md"
        )
    )


def test_paper_review_input_aliases_normalize_to_canonical_fields() -> None:
    module = _load_engine()

    normalized = module._normalize_input_data(
        {
            "paper_path": "/tmp/paper.pdf",
            "target_venue": "ICLR 2026",
            "review_mode": "strict",
            "language": "Chinese",
        }
    )

    assert normalized == {
        "input": "/tmp/paper.pdf",
        "venue": "ICLR 2026",
        "mode": "strict",
        "output_language": "Chinese",
    }
    assert module.PaperReviewEngine.validate_params(
        arguments={"paper_path": "/tmp/paper.pdf"}
    ) is None


def test_paper_review_contract_accepts_observed_react_compatibility_input() -> None:
    frontmatter = yaml.safe_load(
        (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
    )
    schema = frontmatter["metadata"]["helixforge"]["input_schema"]
    assert schema["properties"]["output_language"]["default"] == "English"
    entry = SimpleNamespace(
        input_schema=schema,
        input_schema_declared=True,
        contract_level="full",
    )

    assert skill_input_contract_error(
        entry,
        {
            "paper_path": "/tmp/paper.pdf",
            "target_venue": "ICLR 2026",
            "review_mode": "standard",
            "output_language": "中文",
            "review_purpose": "作者投稿前评审",
            "run_full_workflow": True,
            "save_markdown": True,
            "constraints": "只调用一次 paper-review",
        },
    ) == {}


def test_iclr_rating_is_the_overall_verdict_and_numeric_score() -> None:
    module = _load_engine()
    rating = "4 — Marginally Below The Acceptance Threshold"

    assert module._overall_text({"Rating": rating, "Confidence": "4"}) == rating
    assert module._score_from_text(rating) == 4.0


@pytest.mark.asyncio
async def test_managed_report_path_honors_host_output_without_a_source_copy(
    tmp_path: Path,
) -> None:
    module = _load_engine()

    class _Artifacts:
        def __init__(self) -> None:
            self.registered: list[Path] = []

        async def task_output_path(self, filename: str, *, kind: str) -> Path:
            assert kind == "report"
            destination = tmp_path / "cli-out" / "reports" / "task" / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            return destination

        async def register_existing(self, path: Path, **_kwargs: Any) -> Any:
            self.registered.append(path)
            return SimpleNamespace(
                uri="artifact://review",
                path=path,
                mime="text/markdown",
            )

    artifacts = _Artifacts()
    fallback = tmp_path / "repo" / "reviews" / "review.md"
    managed = await module._managed_report_path(
        SimpleNamespace(artifacts=artifacts),
        fallback,
        explicit_output=False,
    )
    managed.write_text("# Review", encoding="utf-8")
    stored = await module._store_markdown_artifact(
        SimpleNamespace(artifacts=artifacts),
        managed,
        title="Paper review",
    )

    assert managed == tmp_path / "cli-out" / "reports" / "task" / "review.md"
    assert not fallback.exists()
    assert artifacts.registered == [managed]
    assert stored["path"] == str(managed)

    explicit = await module._managed_report_path(
        SimpleNamespace(artifacts=artifacts),
        fallback,
        explicit_output=True,
    )
    assert explicit == fallback
