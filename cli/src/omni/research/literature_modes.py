"""Literature capability modes (Crow / Falcon / Owl) as plan facts.

These are capabilities, not a second runtime. The planner binds them; settlement
and the reviewer check the corresponding structural contract.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from omni.agent.capabilities import (
    CAPABILITY_GROUNDED_QA,
    CAPABILITY_SYNTHESIS_FINAL,
    DELIVERABLE_DRAFT_MANUSCRIPT,
    DELIVERABLE_DRAFT_SECTION,
)

CAPABILITY_LITERATURE_QA = "literature.qa"
CAPABILITY_LITERATURE_SURVEY = "literature.survey"
CAPABILITY_LITERATURE_PRECEDENT = "literature.precedent"

PRECEDENT_ANSWERS = ("yes", "no", "unclear")
_LEADING_VERDICT_RE = re.compile(r"^(yes|no|unclear)\b", re.IGNORECASE)
_PHRASE_RE = re.compile(r"\b(not found|no prior work|uncertain)\b", re.IGNORECASE)
_PRECEDENT_ASK_RE = re.compile(
    r"(has anyone|have people|anyone (?:ever )?done|prior work on|有没有人|是否有人做过)",
    re.IGNORECASE,
)
CITE_SOURCE_EVENT = "cite_source"


def utterance_asks_precedent(text: str) -> bool:
    """Owl-style 'has anyone done X' questions, language-neutral enough for CN/EN."""
    return bool(_PRECEDENT_ASK_RE.search(str(text or "")))


def literature_mode(capabilities: Sequence[str], outputs: Sequence[str] | None = None) -> str:
    """Return the active literature mode, or ``""``."""
    named = {
        str(item).strip()
        for item in (*capabilities, *(outputs or []))
        if str(item).strip()
    }
    if CAPABILITY_LITERATURE_PRECEDENT in named:
        return CAPABILITY_LITERATURE_PRECEDENT
    if CAPABILITY_LITERATURE_SURVEY in named:
        return CAPABILITY_LITERATURE_SURVEY
    if CAPABILITY_LITERATURE_QA in named or CAPABILITY_GROUNDED_QA in named:
        return CAPABILITY_LITERATURE_QA
    return ""


def is_survey_mode(capabilities: Sequence[str], outputs: Sequence[str] | None = None) -> bool:
    from omni.agent.capabilities import is_survey_pair

    if literature_mode(capabilities, outputs) == CAPABILITY_LITERATURE_SURVEY:
        return True
    return is_survey_pair(list(capabilities), list(outputs or []))


def is_precedent_mode(capabilities: Sequence[str], outputs: Sequence[str] | None = None) -> bool:
    return literature_mode(capabilities, outputs) == CAPABILITY_LITERATURE_PRECEDENT


def precedent_verdict(text: str) -> str:
    """yes / no / unclear from a short precedent answer, else ``""``."""
    body = str(text or "").strip()
    if not body:
        return ""
    head = body.splitlines()[0].strip()
    leading = _LEADING_VERDICT_RE.match(head)
    if leading:
        token = leading.group(1).lower()
        return token if token in PRECEDENT_ANSWERS else "unclear"
    phrase = _PHRASE_RE.search(body)
    if phrase is None:
        return ""
    token = phrase.group(1).lower()
    if token in {"not found", "no prior work"}:
        return "no"
    return "unclear"


def survey_required_events(existing: Sequence[str] | None = None) -> list[str]:
    events = [str(item) for item in (existing or []) if str(item).strip()]
    if CITE_SOURCE_EVENT not in events:
        events.append(CITE_SOURCE_EVENT)
    return events


def precedent_required_events(existing: Sequence[str] | None = None) -> list[str]:
    """Owl-style precedent still owes at least one cite_source row."""
    return survey_required_events(existing)


def writing_names() -> frozenset[str]:
    return frozenset(
        {
            CAPABILITY_SYNTHESIS_FINAL,
            DELIVERABLE_DRAFT_SECTION,
            DELIVERABLE_DRAFT_MANUSCRIPT,
        }
    )


def plan_has_writing(plan: Any) -> bool:
    names = [str(item) for item in (getattr(plan, "outputs", None) or []) if item]
    verification = getattr(plan, "verification_plan", None)
    names.extend(
        str(item) for item in (getattr(verification, "required_outputs", None) or []) if item
    )
    caps = [str(item) for item in (getattr(plan, "capability_inputs", None) or {}) if item]
    return bool(set(names) & writing_names()) or bool(set(caps) & writing_names())


def plan_is_grounded_qa(plan: Any) -> bool:
    caps = plan_capabilities_of(plan)
    return literature_mode(caps) == CAPABILITY_LITERATURE_QA


def plan_capabilities_of(plan: Any) -> list[str]:
    caps = [str(item) for item in (getattr(plan, "capability_inputs", None) or {}) if item]
    caps.extend(str(item) for item in (getattr(plan, "outputs", None) or []) if item)
    for selection in getattr(plan, "selected_skills", None) or []:
        caps.extend(str(item) for item in (getattr(selection, "matched_capabilities", None) or []) if item)
    verification = getattr(plan, "verification_plan", None)
    caps.extend(
        str(item) for item in (getattr(verification, "required_outputs", None) or []) if item
    )
    return list(dict.fromkeys(caps))


__all__ = [
    "CAPABILITY_LITERATURE_PRECEDENT",
    "CAPABILITY_LITERATURE_QA",
    "CAPABILITY_LITERATURE_SURVEY",
    "CITE_SOURCE_EVENT",
    "is_precedent_mode",
    "is_survey_mode",
    "literature_mode",
    "plan_capabilities_of",
    "plan_has_writing",
    "plan_is_grounded_qa",
    "precedent_required_events",
    "precedent_verdict",
    "survey_required_events",
    "utterance_asks_precedent",
    "writing_names",
]
