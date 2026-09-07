"""Informational expensive-work gate for decks, LiveFigure, posters, and reviews.

Auto mode still runs. This is a ``/task show`` card, not ``awaiting_approval``.
"""

from __future__ import annotations

from typing import Any

PLAN_GATE_EVENT = "research.plan_gate"

_EXPENSIVE: tuple[tuple[frozenset[str], str], ...] = (
    (
        frozenset({"artifact.slides", "slides.generate", "research-pptx"}),
        "Multi-slide deck (research-pptx)",
    ),
    (
        frozenset({"figure.editable.pptx", "artifact.pptx", "livefigure"}),
        "Editable LiveFigure PPTX",
    ),
    (
        frozenset({"artifact.poster", "poster.scientific", "scientific-poster"}),
        "Scientific poster",
    ),
    (
        frozenset({"review.paper", "paper-review", "analysis.paper"}),
        "Paper review",
    ),
)


def expensive_work_from_plan(plan: Any) -> list[dict[str, str]]:
    names = _plan_names(plan)
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for tokens, label in _EXPENSIVE:
        hit = next((name for name in names if name in tokens), "")
        if not hit or label in seen:
            continue
        seen.add(label)
        items.append({"name": hit, "label": label})
    return items


def format_plan_gate(payload: dict[str, Any] | None) -> list[str]:
    data = payload if isinstance(payload, dict) else {}
    raw = data.get("items") if isinstance(data.get("items"), list) else []
    labels = [
        str(item.get("label") or item.get("name") or "").strip()
        for item in raw
        if isinstance(item, dict)
    ]
    labels = [label for label in labels if label]
    if not labels:
        return []
    auto = data.get("auto_ran", True)
    head = (
        "Expensive work (informational — auto mode still ran):"
        if auto
        else "Expensive work (informational):"
    )
    return [head, *labels]


def _plan_names(plan: Any) -> list[str]:
    names: list[str] = []
    names.extend(str(item) for item in (getattr(plan, "outputs", None) or []) if item)
    inputs = getattr(plan, "capability_inputs", None) or {}
    if isinstance(inputs, dict):
        names.extend(str(key) for key in inputs if key)
    verification = getattr(plan, "verification_plan", None)
    names.extend(
        str(item) for item in (getattr(verification, "required_outputs", None) or []) if item
    )
    for selection in getattr(plan, "selected_skills", None) or []:
        if isinstance(selection, str):
            names.append(selection)
            continue
        skill = getattr(selection, "skill", None)
        if skill:
            names.append(str(skill))
        elif isinstance(selection, dict) and selection.get("skill"):
            names.append(str(selection["skill"]))
    return [name for name in names if name]


__all__ = [
    "PLAN_GATE_EVENT",
    "expensive_work_from_plan",
    "format_plan_gate",
]
