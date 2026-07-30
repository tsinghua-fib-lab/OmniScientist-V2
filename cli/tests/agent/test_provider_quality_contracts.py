"""Provider-local quality contracts for built-in research deliverables."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"


def _frontmatter(skill_name: str) -> dict:
    text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])


def _load_module(skill_name: str, module_name: str):
    path = SKILLS_ROOT / skill_name / "engine.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("skill_name", "check"),
    [
        ("research-pptx", "slides_rendered_and_quality_checked"),
        ("scientific-poster", "poster_html_valid"),
        ("paper-review", "review_complete_and_evidence_grounded"),
    ],
)
def test_provider_manifest_owns_its_deliverable_quality_contract(
    skill_name: str,
    check: str,
) -> None:
    helix = _frontmatter(skill_name)["metadata"]["helixforge"]
    contract = helix["quality_contract"]

    assert check in contract["checks"]
    assert contract["assessment_required"] is True
    assert contract["assessment_schema"] == "omni.deliverable-assessment/v1"
    assessment = helix["output_schema"]["properties"]["deliverable_assessment"]
    assert assessment["properties"]["schema"]["const"] == (
        "omni.deliverable-assessment/v1"
    )


@pytest.mark.asyncio
async def test_research_pptx_emits_assessment_from_effective_render_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module(
        "research-pptx",
        "provider_quality_research_pptx",
    )

    async def fake_run(
        _engine: object,
        req: object,
        _progress_callback: object,
    ) -> object:
        return module.PresentationResult(
            status="ok",
            summary="Generated the deck.",
            title="RAG Seminar",
            pptx_uri="artifact://deck",
            slide_count=12,
            artifacts=[{"uri": "artifact://deck"}],
            metadata={
                "qa_warnings": 0,
                "language": vars(req)["language"],
                "talk_type": vars(req)["talk_type"],
            },
        )

    monkeypatch.setattr(module.ResearchPptxEngine, "_run", fake_run)
    engine = module.ResearchPptxEngine()
    engine.ctx = SimpleNamespace(
        provider_authority={"fingerprint": "authority-fingerprint"},
        workflow_step_key="deck.step",
    )

    result = await engine.execute(
        topic="Create a RAG seminar deck.",
        language="en",
        talk_type="seminar",
        deliverable_id="artifact.slides",
        provider_binding_id="provider-binding:deck",
    )

    assessment = result["deliverable_assessment"]
    assert assessment["schema"] == "omni.deliverable-assessment/v1"
    assert assessment["provider"] == "research-pptx"
    assert assessment["provider_binding_id"] == "provider-binding:deck"
    assert assessment["contract_hash"] == "authority-fingerprint"
    assert assessment["step_id"] == "deck.step"
    assert assessment["status"] == "passed"
    assert assessment["retryable"] is False
    assert assessment["effective_inputs"]["talk_type"] == "seminar"
    assert assessment["criteria"] == [
        {
            "criterion_id": "slides_rendered_and_quality_checked",
            "status": "passed",
            "summary": "Rendered 12 slides with no remaining layout warnings.",
            "evidence_refs": ["artifact://deck"],
        }
    ]


def test_scientific_poster_reports_unavailable_render_inspection_as_unknown() -> None:
    module = _load_module(
        "scientific-poster",
        "provider_quality_scientific_poster",
    )
    ctx = SimpleNamespace(
        provider_authority={"fingerprint": "poster-authority"},
        workflow_step_key="poster.step",
    )

    assessment = module._poster_assessment(  # noqa: SLF001
        ctx,
        {
            "action": "draft",
            "input": "Create a poster.",
            "deliverable_id": "artifact.poster",
            "provider_binding_id": "provider-binding:poster",
        },
        inspection={
            "status": "partial",
            "outcome": {"code": "inspection_unavailable"},
        },
        artifacts=[
            {"uri": "artifact://poster"},
            {"uri": "artifact://inspection"},
        ],
    )

    assert assessment["status"] == "unknown"
    assert assessment["retryable"] is False
    assert assessment["criteria"][0]["criterion_id"] == "poster_html_valid"
    assert assessment["criteria"][0]["status"] == "passed"
    assert assessment["criteria"][1]["criterion_id"] == "poster_render_inspected"
    assert assessment["criteria"][1]["status"] == "unknown"
    assert assessment["evidence_refs"] == [
        "artifact://poster",
        "artifact://inspection",
    ]


@pytest.mark.asyncio
async def test_scientific_poster_published_version_carries_provider_assessment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module(
        "scientific-poster",
        "provider_quality_scientific_poster_publish",
    )

    async def fake_store(
        _ctx: object,
        path: Path,
        *,
        kind: str,
        title: str,
        fmt: str,
        mime: str,
    ) -> dict:
        return {
            "title": title,
            "format": fmt,
            "uri": f"artifact://{kind}",
            "path": str(path),
            "mime": mime,
            "size_bytes": path.stat().st_size,
        }

    monkeypatch.setattr(module, "_store_artifact", fake_store)
    source_text = "Abstract Method Evidence Limitations References"
    regions = "".join(
        (
            f'<section data-poster-id="{name}" data-poster-region="{name}" '
            f'data-source-label="Abstract">{name} content</section>'
        )
        for name in ("hero", "method", "evidence", "limitations", "provenance")
    )
    html = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<style>@page { size: 1189mm 841mm; }</style></head><body>"
        f'<main data-poster-id="poster">{regions}</main></body></html>'
    )
    engine = module.ScientificPosterEngine()
    engine.ctx = SimpleNamespace(
        provider_authority={"fingerprint": "poster-authority"},
        workflow_step_key="poster.step",
    )

    result = await engine._publish_version(  # noqa: SLF001
        html_text=html,
        source_text=source_text,
        input_data={
            "action": "draft",
            "input": source_text,
            "deliverable_id": "artifact.poster",
            "provider_binding_id": "provider-binding:poster",
        },
        progress_callback=None,
        workspace=tmp_path,
        parent_html_sha256=None,
        live_html_path=None,
        asset_warnings=[],
        inspection={"status": "ok", "outcome": {"code": "inspection_complete"}},
        source_figure_sha256s=set(),
    )

    assert result["status"] == "ok"
    assert result["deliverable_assessment"]["status"] == "passed"
    assert result["deliverable_assessment"]["provider_binding_id"] == (
        "provider-binding:poster"
    )
    assert {
        item["criterion_id"]
        for item in result["deliverable_assessment"]["criteria"]
    } == {"poster_html_valid", "poster_render_inspected"}


def test_prompt_only_paper_review_requires_truthful_structured_assessment() -> None:
    frontmatter = _frontmatter("paper-review")
    helix = frontmatter["metadata"]["helixforge"]
    text = (SKILLS_ROOT / "paper-review" / "SKILL.md").read_text(encoding="utf-8")

    assert helix["kind"] == "prompt_only"
    assert helix["quality_contract"]["assessment_required"] is True
    assert helix["quality_contract"]["missing_assessment_status"] == "unknown"
    assert "`unknown`" in text
    assert "Never report `passed`" in text
    assert '"criterion_id": "review_complete_and_evidence_grounded"' in text
