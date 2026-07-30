"""Normalize each built-in skill's free-form progress into one shared language.

A skill reports progress as a free string whose wording and granularity is its
own (``querying OpenAlex``, ``render graphviz``, a full English sentence, a
dotted id). ``normalize_progress`` maps those onto the stage contract -- a clean
live *label*, a stable ``stage_id``, and, at a skill's recognized checkpoints, a
completion *milestone* -- so the transcript reads the same whether or not the
skill itself has been retrofitted.

This is a pure, per-event translation. A skill that already speaks the contract
(it set ``stage_id`` or ``milestone`` itself) is passed through untouched, so a
native retrofit always wins over this fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from omni.runtime.stage_contract import MILESTONE_KEY, STAGE_ID_KEY


@dataclass(frozen=True)
class _Stage:
    id: str
    label: str
    milestone: str = ""  # non-empty marks a checkpoint worth a durable line


@dataclass(frozen=True)
class _Rule:
    keys: tuple[str, ...]
    mode: str  # "exact" | "contains"
    stage: _Stage


def _exact(raw: str, stage: _Stage) -> _Rule:
    return _Rule((raw,), "exact", stage)


def _contains(keys: tuple[str, ...], stage: _Stage) -> _Rule:
    return _Rule(keys, "contains", stage)


# Per-skill rules, tried in order; first match wins. Exact rules pin a known
# stage token; ``contains`` rules recognise prose stages by a stable phrase.
_SKILL_RULES: dict[str, tuple[_Rule, ...]] = {
    "openalex-search": (
        _exact("querying openalex", _Stage("literature.search", "searching OpenAlex")),
        _exact("indexing", _Stage("literature.index", "indexing results")),
        _exact("done", _Stage("literature.done", "search complete", "Literature search complete")),
    ),
    "scientific-figure": (
        _exact("plan figure", _Stage("figure.plan", "planning figure")),
        _exact("render graphviz", _Stage("figure.render", "rendering figure")),
        _exact("save artifacts", _Stage("figure.save", "saving artifacts")),
        _exact("record provenance", _Stage("figure.provenance", "recording provenance")),
        _exact("done", _Stage("figure.done", "figure complete", "Figure rendered and checked")),
    ),
    "livefigure": (
        _exact("generate pptx code", _Stage("livefigure.generate", "generating figure code")),
        _exact("validate and build pptx", _Stage("livefigure.build", "building figure")),
        _exact("pptx ready", _Stage("livefigure.done", "figure ready", "Live figure built")),
    ),
    "research-ideation": (
        _contains(("search literature",), _Stage("ideation.search", "surveying literature")),
        _contains(("research gaps",), _Stage("ideation.gaps", "analysing research gaps")),
        _contains(("generate research ideas",), _Stage("ideation.generate", "generating ideas")),
        _contains(("critique",), _Stage("ideation.critique", "critiquing ideas")),
        _contains(("refine",), _Stage("ideation.refine", "refining the idea")),
        # The engine emits the completion milestone natively (with idea/paper
        # counts), so the adapter only normalizes this stage's live label.
        _exact("complete", _Stage("ideation.done", "ideation complete")),
    ),
    "research-pptx": (
        _exact("parsing", _Stage("pptx.parse", "parsing source")),
        _exact("deciding", _Stage("pptx.decide", "deciding strategy")),
        _exact("planning", _Stage("pptx.plan", "planning slides")),
        _exact("rendering", _Stage("pptx.render", "rendering slides")),
        _exact("qa", _Stage("pptx.qa", "checking quality")),
        _exact("critique", _Stage("pptx.critique", "reviewing slides")),
        _exact("upload", _Stage("pptx.done", "finalizing", "Presentation rendered")),
    ),
    "scientific-poster": (
        _exact("poster.prepare-source", _Stage("poster.source", "preparing source")),
        _exact("poster.plan-content", _Stage("poster.plan", "planning content")),
        _exact("poster.plan-ready", _Stage("poster.plan.done", "plan ready", "Poster plan ready")),
        _exact("poster.reference-ready", _Stage("poster.reference.done", "references ready", "References resolved")),
        _exact("poster.design-ready", _Stage("poster.design.done", "design ready", "Poster design ready")),
        _exact("poster.author", _Stage("poster.author", "authoring poster")),
        _exact("poster.author-ready", _Stage("poster.author.done", "content ready", "Author content ready")),
        _exact("poster.inspect", _Stage("poster.inspect", "inspecting in browser")),
        _exact("poster.visual-review", _Stage("poster.review", "visual review")),
        _exact("poster.export-pptx", _Stage("poster.export", "exporting pptx")),
        _exact("poster.ready", _Stage("poster.done", "poster ready", "Poster ready for review")),
    ),
    "paper-review": (
        _contains(("paper input", "paper text"), _Stage("review.parse", "reading manuscript")),
        _contains(("literature",), _Stage("review.literature", "querying literature")),
        _contains(("synthesis", "review synthesis"), _Stage("review.synthesize", "synthesising review")),
        _contains(("validation",), _Stage("review.validate", "validating review")),
        _contains(("revision plan",), _Stage("review.revision", "planning revisions")),
        _contains(("saving", "artifact"), _Stage("review.save", "saving review", "Review complete")),
    ),
    "scientist-kg-distiller": (
        _exact("identity", _Stage("kg.identity", "confirming identity")),
        _exact("collect", _Stage("kg.collect", "collecting materials")),
        _exact("ingest", _Stage("kg.ingest", "ingesting full text")),
        _exact("evidence", _Stage("kg.evidence", "extracting evidence")),
        _exact("l2", _Stage("kg.l2", "inducing judgment patterns")),
        _exact("l3", _Stage("kg.l3", "abstracting stances")),
        _exact("edges", _Stage("kg.edges", "linking the graph")),
        _exact("kg", _Stage("kg.validate", "validating the KG")),
        _exact("capsule", _Stage("kg.capsule", "building capsule", "Soul capsule generated")),
    ),
}


def _match(skill: str, raw: str) -> _Stage | None:
    rules = _SKILL_RULES.get(skill)
    if not rules:
        return None
    norm = " ".join(raw.split()).lower()
    if not norm:
        return None
    for rule in rules:
        if rule.mode == "exact":
            if norm in rule.keys:
                return rule.stage
        elif any(key in norm for key in rule.keys):
            return rule.stage
    return None


def normalize_progress(data: dict[str, Any]) -> dict[str, Any]:
    """Return ``data`` with a normalized stage label/id (+ milestone at a checkpoint).

    Untouched when the event already speaks the contract, names no mappable skill
    stage, or is an internal workflow/tool/skill lifecycle event -- those are the
    display's own vocabulary and must pass through as-is.
    """
    if not isinstance(data, dict):
        return data
    if data.get(STAGE_ID_KEY) or data.get(MILESTONE_KEY):
        return data
    skill = str(data.get("skill") or "")
    stage = _match(skill, str(data.get("stage") or ""))
    if stage is None:
        return data
    out = dict(data)
    out["stage"] = stage.label
    out[STAGE_ID_KEY] = stage.id
    if stage.milestone:
        out[MILESTONE_KEY] = stage.milestone
    return out


__all__ = ["normalize_progress"]
