"""Stable capability and deliverable identifiers used by planner/runtime."""

from __future__ import annotations

CAPABILITY_LITERATURE_SEARCH = "literature.search"
CAPABILITY_GROUNDED_QA = "qa.grounded"
CAPABILITY_FIGURE = "artifact.figure"
CAPABILITY_EDITABLE_PPTX_FIGURE = "figure.editable.pptx"
CAPABILITY_SLIDES_GENERATE = "slides.generate"
# In-place, minor restyle/recolor/relabel of an *existing* attached figure. The
# model proposes this capability; the runtime executes the deterministic,
# contract-validated patch (or escalates to a full ``artifact.figure`` redraw
# when the target cannot be grounded). It is a single-turn edit, not a workflow
# step, so it is intentionally kept out of ``WORKFLOW_CAPABILITIES``.
CAPABILITY_ARTIFACT_REVISE = "artifact.revise"
CAPABILITY_SYNTHESIS_FINAL = "synthesis.final"
DELIVERABLE_DRAFT_SECTION = "draft.section"
DELIVERABLE_DRAFT_MANUSCRIPT = "draft.manuscript"
CAPABILITY_PAPER_ANALYSIS = "analysis.paper"
CAPABILITY_REVIEW = "review.paper"
CAPABILITY_REVIEW_RESPONSE = "review.response"
CAPABILITY_SCIENTIFIC_POSTER = "poster.scientific"
CAPABILITY_RESEARCH_IDEATION = "research.ideation"
CAPABILITY_CONTRADICTION = "evidence.contradiction_scan"
CAPABILITY_ARXIV_FETCH = "paper.fetch.arxiv"

WORKFLOW_CAPABILITIES = frozenset(
    {
        CAPABILITY_LITERATURE_SEARCH,
        CAPABILITY_FIGURE,
        CAPABILITY_EDITABLE_PPTX_FIGURE,
        CAPABILITY_SLIDES_GENERATE,
        CAPABILITY_SYNTHESIS_FINAL,
        DELIVERABLE_DRAFT_SECTION,
        DELIVERABLE_DRAFT_MANUSCRIPT,
        CAPABILITY_RESEARCH_IDEATION,
        CAPABILITY_ARXIV_FETCH,
        CAPABILITY_REVIEW,
        CAPABILITY_REVIEW_RESPONSE,
        CAPABILITY_SCIENTIFIC_POSTER,
    }
)


def is_native_synthesis_capability(capability: str) -> bool:
    return capability in {CAPABILITY_SYNTHESIS_FINAL, DELIVERABLE_DRAFT_SECTION, DELIVERABLE_DRAFT_MANUSCRIPT}


def capabilities_from_steps(steps: list[dict[str, object]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for step in steps:
        capability = str(step.get("capability") or "").strip()
        if capability and capability not in seen:
            seen.add(capability)
            out.append(capability)
    return out


def deliverables_from_capabilities(capabilities: list[str]) -> list[str]:
    out: list[str] = []
    for capability in capabilities:
        if capability == CAPABILITY_FIGURE:
            out.append("artifact.figure")
        elif capability == CAPABILITY_EDITABLE_PPTX_FIGURE:
            out.append("artifact.pptx")
        elif capability == CAPABILITY_SLIDES_GENERATE:
            out.append("artifact.slides")
        elif capability == CAPABILITY_GROUNDED_QA:
            out.append("answer")
        elif capability == CAPABILITY_LITERATURE_SEARCH:
            out.append("sources")
        elif is_native_synthesis_capability(capability):
            out.append(DELIVERABLE_DRAFT_SECTION)
        elif capability == CAPABILITY_REVIEW:
            out.append("review")
        elif capability == CAPABILITY_REVIEW_RESPONSE:
            out.append("response_letter")
        elif capability == CAPABILITY_SCIENTIFIC_POSTER:
            out.append("artifact.poster")
    return list(dict.fromkeys(out)) or ["workflow"]
