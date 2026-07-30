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
    words = [word for word in query.split() if word]
    phrases = " ".join(entry.trigger.get("phrases", [])) if isinstance(getattr(entry, "trigger", None), dict) else ""
    haystack = f"{name} {getattr(entry, 'description', '')} {getattr(entry, 'when_to_use', '')} {phrases}".lower()
    if not words or not all(word in haystack for word in words):
        return 0.0
    if any(word in name for word in words if len(word) > 2):
        return 80.0
    return 15.0


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
