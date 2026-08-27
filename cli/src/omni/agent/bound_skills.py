"""Host-injected skill contracts for file debts this turn already named.

Codex injects ``SKILL.md`` when the user mentions a skill. Omni also knows
the settlement slot (``required_outputs``), so the coordinator can surface
the admitted producer without another ``find_skill``. Selection stays on
registry admission and slot routing — not an utterance parser.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from omni.agent.capabilities import CAPABILITY_EDITABLE_PPTX_FIGURE, CAPABILITY_FIGURE
from omni.agent.skill_lookup import skill_contract_card
from omni.core.scientific_progress import (
    bound_skill_steer,
    leftover_produce_signal,
    leftover_skill_pressure,
)
from omni.runtime.unpayable import capability_for_deliverable
from omni.skills_runtime.slot_routing import explicit_figure_skill, figure_slot_for_remaining

INJECTABLE_SLOTS = frozenset(
    {
        CAPABILITY_FIGURE,
        CAPABILITY_EDITABLE_PPTX_FIGURE,
        "artifact.pptx",
        "artifact.slides",
        "artifact.poster",
    }
)
_FIGURE_SLOTS = frozenset(
    {CAPABILITY_FIGURE, CAPABILITY_EDITABLE_PPTX_FIGURE, "artifact.pptx"}
)
_SLOT_DEFAULTS = {
    CAPABILITY_FIGURE: "scientific-figure",
    CAPABILITY_EDITABLE_PPTX_FIGURE: "livefigure",
    "artifact.pptx": "livefigure",
    "artifact.slides": "research-pptx",
    "artifact.poster": "research-poster",
}


@dataclass(frozen=True, slots=True)
class BoundSkill:
    skill: str
    output: str
    card: dict[str, Any]


def resolve_bound_skills(
    plan: Any,
    registry: Any,
    *,
    ctx: Any | None = None,
    services: dict[str, Any] | None = None,
) -> list[BoundSkill]:
    """Admitted (or explicitly named) producers for this turn's file debts."""
    owed = [name for name in _owed_file_slots(plan) if name in INJECTABLE_SLOTS]
    if not owed or registry is None:
        return []
    planned = _planned_skill_names(plan)
    user_message = str(getattr(plan, "user_message", "") or "")
    explicit = explicit_figure_skill(
        user_message, getattr(plan, "selected_skills", None)
    )
    figure_owed = [name for name in owed if name in _FIGURE_SLOTS]
    figure_slot = (
        figure_slot_for_remaining(figure_owed, explicit_skill=explicit)
        if figure_owed
        else ""
    )
    probed = services
    if probed is None:
        probe = getattr(registry, "admission_services", None)
        if callable(probe):
            probed = probe(ctx=ctx)
    bindings: list[BoundSkill] = []
    seen: set[str] = set()
    for output in owed:
        skill = _skill_for_output(
            output,
            registry=registry,
            planned=planned,
            explicit=explicit,
            figure_slot=figure_slot,
            services=probed,
            ctx=ctx,
        )
        if not skill or skill in seen:
            continue
        getter = getattr(registry, "get", None)
        entry = getter(skill) if callable(getter) else None
        if entry is None:
            continue
        seen.add(skill)
        bindings.append(
            BoundSkill(
                skill=skill,
                output=output,
                card=skill_contract_card(entry, services=probed, ctx=ctx),
            )
        )
    return bindings


def render_bound_skill_block(bindings: Iterable[BoundSkill]) -> str:
    """Compact Codex-style injection: name, availability, and the run_skill call."""
    items = [item for item in bindings if item.skill]
    if not items:
        return ""
    lines = [
        "[Bound skills]",
        "This turn already owes these file deliverables. Call run_skill with "
        "the input_schema below. Do not find_skill the same name again, and "
        "do not bash-write a leftover file of the same kind.",
    ]
    for item in items:
        card = item.card if isinstance(item.card, dict) else {}
        availability = str(card.get("availability") or "available")
        reason = str(card.get("unavailable_reason") or "").strip()
        status = f"{availability}: {reason}" if reason else availability
        lines.append(f"- {item.skill} pays {item.output} ({status})")
        call = card.get("call")
        if isinstance(call, dict) and call:
            lines.append(f"  call: run_skill {json.dumps(call, ensure_ascii=False)}")
        next_action = str(card.get("next_action") or "").strip()
        if next_action:
            lines.append(f"  {next_action}")
        fallback = str(card.get("fallback") or "").strip()
        if fallback and availability != "available":
            lines.append(f"  fallback: {fallback}")
    return "\n".join(lines)


def bound_skill_names(bindings: Iterable[BoundSkill]) -> frozenset[str]:
    return frozenset(item.skill for item in bindings if item.skill)


def _skill_for_output(
    output: str,
    *,
    registry: Any,
    planned: list[str],
    explicit: str,
    figure_slot: str,
    services: dict[str, Any] | None,
    ctx: Any | None,
) -> str:
    capability = capability_for_deliverable(output) or output
    for name in planned:
        if _entry_pays(registry, name, capability):
            return name
    if output in _FIGURE_SLOTS:
        if explicit:
            return explicit
        resolved = _resolve_capability(
            registry, figure_slot or capability, services=services, ctx=ctx
        )
        return resolved or _SLOT_DEFAULTS.get(figure_slot or output, "")
    return _resolve_capability(
        registry, capability, services=services, ctx=ctx
    ) or _SLOT_DEFAULTS.get(output, "")


def _resolve_capability(
    registry: Any,
    slot: str,
    *,
    services: dict[str, Any] | None,
    ctx: Any | None,
) -> str:
    resolve = getattr(registry, "resolve_capability", None)
    if not callable(resolve) or not slot:
        return ""
    try:
        entry, _rejected = resolve(slot, services=services, ctx=ctx)
    except TypeError:
        entry, _rejected = resolve(slot)
    return str(getattr(entry, "name", "") or "")


def _entry_pays(registry: Any, name: str, capability: str) -> bool:
    from omni.runtime.unpayable import entry_pays_capability

    getter = getattr(registry, "get", None)
    entry = getter(name) if callable(getter) else None
    return bool(entry is not None and entry_pays_capability(entry, capability))


def _owed_file_slots(plan: Any) -> list[str]:
    verification = getattr(plan, "verification_plan", None)
    names = [str(item) for item in (getattr(verification, "required_outputs", None) or [])]
    names.extend(str(item) for item in (getattr(plan, "outputs", None) or []) if item)
    return list(dict.fromkeys(name for name in names if name))


def _planned_skill_names(plan: Any) -> list[str]:
    names: list[str] = []
    for selection in getattr(plan, "selected_skills", None) or []:
        name = str(getattr(selection, "skill", "") or "").strip()
        if name and name not in names:
            names.append(name)
    for step in getattr(plan, "workflow_steps", None) or []:
        if not isinstance(step, dict):
            continue
        name = str(step.get("skill") or step.get("skill_name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


__all__ = [
    "BoundSkill",
    "INJECTABLE_SLOTS",
    "bound_skill_names",
    "bound_skill_steer",
    "leftover_produce_signal",
    "leftover_skill_pressure",
    "render_bound_skill_block",
    "resolve_bound_skills",
]
