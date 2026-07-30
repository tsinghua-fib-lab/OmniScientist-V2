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
# Inspect the durable record for an earlier user-request task. The semantic
# planner names the information need; the host maps it to the read-only
# ``get_task`` tool so the model never chooses or grants a concrete tool.
CAPABILITY_TASK_INSPECT = "task.inspect"
# Review *several* prior tasks — a time window ("what did we do in the last N
# days") or a cross-project retrospective ("what have I not handled well").
# Unlike ``task.inspect`` (one task, host-projected status), this is a capable
# read-only turn: the model narrates, and the host appends an authoritative
# per-task status footer so no failed/degraded task is narrated as a success.
CAPABILITY_TASK_REVIEW = "task.review"
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


# Named scientific outputs the durable record can prove with an artifact. Prose
# names ("answer", "workflow", "sources") stay descriptive: the turn's text is
# the answer, and literature hits live in the research object model, not a file.
CONTRACT_DELIVERABLES = frozenset(
    {
        CAPABILITY_FIGURE,
        "artifact.pptx",
        "artifact.slides",
        "artifact.poster",
        DELIVERABLE_DRAFT_MANUSCRIPT,
        DELIVERABLE_DRAFT_SECTION,
        "review",
        "response_letter",
    }
)
WRITING_DELIVERABLES = frozenset(
    {
        DELIVERABLE_DRAFT_MANUSCRIPT,
        DELIVERABLE_DRAFT_SECTION,
        CAPABILITY_SYNTHESIS_FINAL,
    }
)


def contract_outputs(outputs: list[str]) -> list[str]:
    """Return the subset of ``outputs`` that settlement can verify from artifacts."""
    return [name for name in outputs if name in CONTRACT_DELIVERABLES]


def contract_outputs_from_capabilities(capabilities: list[str]) -> list[str]:
    """Map capability names onto the artifact debts settlement can prove.

    ``draft.manuscript`` is already a contract name; passing it through
    :func:`deliverables_from_capabilities` would collapse it to ``draft.section``.
    """
    names: list[str] = []
    for capability in capabilities:
        cap = str(capability or "").strip()
        if not cap:
            continue
        if cap in CONTRACT_DELIVERABLES:
            names.append(cap)
        else:
            names.extend(contract_outputs(deliverables_from_capabilities([cap])))
    return list(dict.fromkeys(names))


def writing_outputs(outputs: list[str]) -> list[str]:
    """Return remaining writing deliverables native synthesis can fill."""
    return [name for name in outputs if name in WRITING_DELIVERABLES]


_SURVEY_RETRIEVAL = frozenset({CAPABILITY_LITERATURE_SEARCH})
_SURVEY_WRITING = frozenset(
    {
        CAPABILITY_SYNTHESIS_FINAL,
        DELIVERABLE_DRAFT_SECTION,
        DELIVERABLE_DRAFT_MANUSCRIPT,
    }
)
_SURVEY_IGNORABLE = frozenset({"sources", "answer", "workflow"})


def is_survey_pair(
    capabilities: list[str],
    outputs: list[str] | None = None,
) -> bool:
    """Literature retrieval plus a manuscript, and nothing else.

    The host already has runners for both sides. Demoting this pair to ReAct
    hid the produce path behind orientation tools — the same hole
    ``_qa_figure_pair`` already carved out for answer-plus-figure.
    """
    named = {
        str(item).strip()
        for item in (*capabilities, *(outputs or []))
        if str(item).strip()
    }
    extra = named - _SURVEY_RETRIEVAL - _SURVEY_WRITING - _SURVEY_IGNORABLE
    return bool(named & _SURVEY_RETRIEVAL) and bool(named & _SURVEY_WRITING) and not extra
