"""Parse language-neutral, machine-readable boundaries before planning.

Natural-language intent belongs to the semantic planner. This module recognizes
only explicit provider syntax whose meaning does not depend on the user's
language: ``$name``, ``skill:name``, ``run_skill name``, ``use_skill name``, and
``/skills run name``. Commands, identifiers, paths, and permissions are handled
by their dedicated parsers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from omni.skills_runtime.registry import SkillRegistry, scope_sources


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    kind: str
    reason: str = ""
    skill: str = ""
    missing_field: str = ""
    # Forced discovery source when the user wrote ``$<scope>:<name>``; empty for
    # a bare ``$name`` (which resolves to the winning skill for that name).
    skill_source: str = ""


_EXPLICIT_SKILL_PATTERNS = (
    re.compile(r"(?:^|\s)\$(?P<name>[A-Za-z0-9][\w.+:/-]*)\b"),
    re.compile(r"(?:^|\s)skill:(?P<name>[A-Za-z0-9][\w.+:/-]*)\b", re.IGNORECASE),
    re.compile(
        r"(?:^|\s)(?:run_skill|use_skill)\s+(?P<name>[A-Za-z0-9][\w.+:/-]*)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|\s)/skills\s+run\s+(?P<name>[A-Za-z0-9][\w.+:/-]*)\b",
        re.IGNORECASE,
    ),
)


class BoundaryRouter:
    """Evaluate explicit protocol boundaries before semantic planning."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def route(self, text: str) -> BoundaryDecision | None:
        name, source = explicit_skill_ref(text or "", self._registry)
        if not name:
            return None
        return BoundaryDecision(
            "explicit_skill",
            "user explicitly selected a skill/provider",
            skill=name,
            skill_source=source,
        )


def _explicit_token(text: str) -> str:
    """The raw ``$name`` / ``$<scope>:<name>`` token the user typed, or ``""``."""
    for pattern in _EXPLICIT_SKILL_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group("name")
    return ""


def explicit_skill_ref(text: str, registry: SkillRegistry) -> tuple[str, str]:
    """Resolve an explicit selection to ``(canonical_name, source)``.

    A bare ``$name`` returns ``(name, "")`` (the winning skill for that name). A
    ``$<scope>:<name>`` escape returns the concrete source (e.g. ``user_omni``)
    so a skill shadowed by a same-named built-in is still reachable end to end.
    Returns ``("", "")`` when nothing matches an installed skill.
    """
    token = _explicit_token(text)
    if not token:
        return "", ""
    entry = registry.resolve_explicit(token)
    if entry is None:
        return "", ""
    # A recognised scope prefix means the user forced this specific source.
    scope, sep, rest = token.partition(":")
    forced = bool(sep and rest and scope_sources(scope) is not None)
    return entry.name, (entry.source if forced else "")


def explicit_skill_name(text: str, registry: SkillRegistry) -> str:
    """Backwards-compatible helper returning only the canonical skill name."""
    return explicit_skill_ref(text, registry)[0]
