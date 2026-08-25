"""Figure slots: admitted producer, explicit name, visible skip — no utterance parser.

A slot names the object the plan owes (``artifact.figure`` vs editable PPTX).
Host facts are: the bound capability, an explicit ``$skill`` / catalog name,
admission, and a real ``.dot`` file the caller already decided to pass.
Codex binds skills only from explicit mentions. Omni adds settlement and
admission; it does not classify ``one slide`` / ``just svg`` / ``.dot`` in prose.

Sibling fallback is admission-only and only for the format-neutral figure
slot. A named livefigure / ``figure.editable.pptx`` debt never switches.
"""

from __future__ import annotations

import re
from typing import Any

from omni.agent.capabilities import CAPABILITY_EDITABLE_PPTX_FIGURE, CAPABILITY_FIGURE
from omni.skills_runtime.admission import skill_admission_rejection

# Preferred skill first. A sibling may pay the same format-neutral slot when
# the preferred provider is blocked — never when the user named it.
SLOT_SIBLINGS: dict[str, dict[str, str]] = {
    "livefigure": {CAPABILITY_FIGURE: "scientific-figure"},
}

_FIGURE_SKILLS = frozenset({"livefigure", "scientific-figure"})
_EDITABLE_SLOTS = frozenset({CAPABILITY_EDITABLE_PPTX_FIGURE, "artifact.pptx"})


def normalize_request(text: str) -> str:
    return str(text or "").casefold().replace("-", " ")


def user_named_skill(message: str, skill: str) -> bool:
    """True when the user named this catalog skill (``$name`` or a whole token).

    Codex ``collect_explicit_skill_mentions``: ``$SkillName`` or an unambiguous
    plain name. Not a semantic hint list.
    """
    name = str(skill or "").strip().casefold()
    if not name:
        return False
    text = str(message or "")
    if re.search(rf"(?:^|\s)\${re.escape(name)}\b", text, flags=re.IGNORECASE):
        return True
    folded = text.casefold()
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", folded) is not None


def explicit_figure_skill(message: str, selected: Any = None) -> str:
    """Plan-selected figure skill, else an explicit catalog name in the utterance."""
    for item in selected or ():
        name = str(getattr(item, "skill", "") or getattr(item, "name", "") or "").strip()
        if name in _FIGURE_SKILLS:
            return name
    for name in ("livefigure", "scientific-figure"):
        if user_named_skill(message, name):
            return name
    return ""


def figure_slot_for_remaining(remaining: list[str], *, explicit_skill: str = "") -> str:
    """Stricter editable debt wins; otherwise the format-neutral figure slot."""
    names = {str(item) for item in remaining if item}
    if explicit_skill == "livefigure" or names & _EDITABLE_SLOTS:
        return CAPABILITY_EDITABLE_PPTX_FIGURE
    return CAPABILITY_FIGURE


def allow_slot_fallback(
    *,
    preferred: str,
    slot: str,
    named_preferred: bool = False,
    user_message: str = "",
) -> bool:
    """Named / editable paths do not switch. Neutral ``artifact.figure`` may.

    ``user_message`` is only consulted for an explicit catalog / ``$`` name.
    """
    if not fallback_skill(preferred, slot):
        return False
    named = named_preferred or user_named_skill(user_message, preferred)
    if named:
        return False
    if str(slot or "") in _EDITABLE_SLOTS:
        return False
    return str(slot or "") == CAPABILITY_FIGURE


def fallback_skill(preferred: str, slot: str) -> str:
    """Sibling that can pay ``slot`` after ``preferred`` is skipped."""
    return str(SLOT_SIBLINGS.get(str(preferred or ""), {}).get(str(slot or ""), "") or "")


def admission_reason_code(rejection: dict[str, Any] | None) -> str:
    if not isinstance(rejection, dict):
        return ""
    info = rejection.get("error_info") if isinstance(rejection.get("error_info"), dict) else {}
    return str((info or {}).get("code") or "").strip()


def skill_availability(
    entry: Any,
    *,
    services: dict[str, Any] | None = None,
    ctx: Any | None = None,
) -> tuple[str, str]:
    """Return ``(available|unavailable, reason_code)`` for a catalog card."""
    try:
        rejection = skill_admission_rejection(entry, services=services, ctx=ctx)
    except Exception:  # noqa: BLE001 — catalog ranking must not fail closed
        return "available", ""
    if rejection is None:
        return "available", ""
    return "unavailable", admission_reason_code(rejection) or "admission_rejected"


def skip_observation(
    *,
    skipped: str,
    fallback: str,
    reason: str,
    setup_command: str = "omni config vlm",
) -> str:
    """Visible degrade / skip line. Must not pretend the fallback was first choice."""
    code = str(reason or "preferred_unavailable").strip()
    skipped_name = str(skipped or "preferred skill").strip()
    fallback_name = str(fallback or "").strip()
    command = str(setup_command or "").strip()
    lines = [f"reason={code}", f"{skipped_name} skipped: {code}."]
    if command and code in {"vlm_not_configured", "vlm_invalid_configuration"}:
        lines.append(f"Run `{command}` so {skipped_name} can produce an editable PPTX.")
    if fallback_name:
        lines.append(f"Using {fallback_name} instead.")
    return " ".join(lines)


__all__ = [
    "SLOT_SIBLINGS",
    "admission_reason_code",
    "allow_slot_fallback",
    "explicit_figure_skill",
    "fallback_skill",
    "figure_slot_for_remaining",
    "normalize_request",
    "skip_observation",
    "skill_availability",
    "user_named_skill",
]
