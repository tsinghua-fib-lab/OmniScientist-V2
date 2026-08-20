"""Rank skill catalog hits and return an executable input contract.

``find_skill`` is the coordinator's Codex-style stage-2 lookup: the turn catalog
stays a name list, and the model asks for a parameter list only when it needs
one. The card must be enough to call ``run_skill`` — metadata without fields is
what sent a slides request into ``docs_search`` / ``glob`` / ``search_tasks``.
"""

from __future__ import annotations

from typing import Any

from omni.core.field_contract import instruction_field

_CARD_PROPERTY_LIMIT = 16
_CARD_DESCRIPTION_LIMIT = 160
_FIGURE_HINTS = frozenset(
    {
        "figure",
        "diagram",
        "architecture",
        "chart",
        "svg",
        "png",
        "graphviz",
        "\u67b6\u6784\u56fe",
        "\u793a\u610f\u56fe",
        "\u7ed8\u56fe",
    }
)
_SLIDE_HINTS = frozenset(
    {
        "pptx",
        "slides",
        "slide",
        "presentation",
        "ppt",
        "deck",
        "\u5e7b\u706f",
        "\u6f14\u793a",
        "\u7ec4\u4f1a",
    }
)

FIND_SKILL_NEXT_ACTION = (
    "Call run_skill now with this input_schema. Do not keep searching docs, "
    "files, or tasks for the same contract."
)


def rank_skill_matches(entries: list[Any], query: str, *, limit: int = 15) -> list[Any]:
    """Return catalog hits with an exact name before a mention-only neighbour.

    ``livefigure`` documents ``research-pptx`` in ``when_to_use``. A query for
    that name must not surface the neighbour first, or the model reads the
    wrong card and keeps exploring.
    """
    selectable = list(entries)
    normalized = str(query or "").lower().strip()
    if not normalized:
        return selectable[: max(0, limit)]
    scored: list[tuple[float, int, str, Any]] = []
    for entry in selectable:
        score = _match_score(entry, normalized)
        if score <= 0:
            continue
        scored.append((score, -int(getattr(entry, "priority", 0) or 0), str(entry.name or ""), entry))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [entry for *_rank, entry in scored[: max(0, limit)]]


def skill_contract_card(entry: Any) -> dict[str, Any]:
    """Compact, callable contract for one catalog skill."""
    schema = getattr(entry, "input_schema", None)
    schema_object = schema if isinstance(schema, dict) else {}
    slot = instruction_field(schema_object)
    example: dict[str, Any] = {slot: "<user request>"} if slot else {}
    return {
        "name": str(getattr(entry, "name", "") or ""),
        "description": entry.short_desc(160) if hasattr(entry, "short_desc") else str(getattr(entry, "description", "") or "")[:160],
        "delivery": _enum_value(getattr(entry, "delivery_mode", "")),
        "kind": _enum_value(getattr(entry, "kind", "")),
        "status": str(getattr(entry, "status", "") or ""),
        "replaced_by": str(getattr(entry, "replaced_by", "") or ""),
        "when_to_use": str(getattr(entry, "when_to_use", "") or "")[:160],
        "instruction_field": slot or None,
        "input_schema": {
            "type": "object",
            "required": [str(name) for name in (schema_object.get("required") or []) if str(name)],
            "properties": _compact_properties(schema_object, instruction_slot=slot),
        },
        "call": {
            "skill_name": str(getattr(entry, "name", "") or ""),
            "input": example,
        },
        "next_action": FIND_SKILL_NEXT_ACTION,
    }


def _significant_words(text: str) -> list[str]:
    return [word for word in text.replace("-", " ").split() if len(word) > 2]


def _match_score(entry: Any, query: str) -> float:
    name = str(getattr(entry, "name", "") or "").lower()
    if not name:
        return 0.0
    folded = query.replace("-", " ")
    named = name.replace("-", " ")
    if name == query or named == folded:
        return 1000.0
    if query in name or name in query:
        return 500.0
    words = _significant_words(query)
    wordset = set(words)
    for phrase in getattr(entry, "default_for", None) or []:
        phrase_words = _significant_words(str(phrase).lower())
        if phrase_words and all(word in folded for word in phrase_words):
            return 400.0
    tags = {
        str(item).lower()
        for item in (
            *(getattr(entry, "capabilities", None) or []),
            *(getattr(entry, "deliverables", None) or []),
        )
        if item
    }
    if wordset & _FIGURE_HINTS and (
        "figure" in name
        or name == "livefigure"
        or any(tag == "artifact.figure" or tag.startswith("figure.") for tag in tags)
    ):
        return 200.0
    if wordset & _SLIDE_HINTS and (
        "pptx" in name
        or "slides" in name
        or any(tag == "artifact.slides" or tag.startswith("slides.") for tag in tags)
    ):
        return 200.0
    phrases = " ".join(entry.trigger.get("phrases", [])) if isinstance(getattr(entry, "trigger", None), dict) else ""
    defaults = " ".join(str(item) for item in (getattr(entry, "default_for", None) or []))
    haystack = (
        f"{name} {getattr(entry, 'description', '')} {getattr(entry, 'when_to_use', '')} "
        f"{phrases} {defaults}"
    ).lower()
    hits = sum(1 for word in words if word in haystack)
    if hits >= 2:
        return 40.0 + hits
    if any(word in name for word in words):
        return 80.0
    return 0.0


def _compact_properties(schema: dict[str, Any], *, instruction_slot: str) -> dict[str, Any]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    required = [str(name) for name in (schema.get("required") or []) if str(name)]
    order: list[str] = []
    for name in (instruction_slot, *required, *properties):
        key = str(name or "")
        if key and key in properties and key not in order:
            order.append(key)
    compact: dict[str, Any] = {}
    for name in order[:_CARD_PROPERTY_LIMIT]:
        spec = properties.get(name)
        if not isinstance(spec, dict):
            continue
        item: dict[str, Any] = {"type": spec.get("type") or "string"}
        if spec.get("enum"):
            item["enum"] = list(spec["enum"])
        description = " ".join(str(spec.get("description") or "").split())
        if description:
            item["description"] = description[:_CARD_DESCRIPTION_LIMIT]
        compact[name] = item
    return compact


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


__all__ = [
    "FIND_SKILL_NEXT_ACTION",
    "rank_skill_matches",
    "skill_contract_card",
]
