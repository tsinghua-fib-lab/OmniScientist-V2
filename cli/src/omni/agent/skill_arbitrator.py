"""Skill/provider selection from capability contracts."""

from __future__ import annotations

from omni.agent.intent_plan import SkillCandidateRejection, SkillSelection
from omni.skills_runtime.manifest import SkillEntry
from omni.skills_runtime.registry import SkillRegistry


class SkillArbitrator:
    """Resolve explicit skills and capabilities into auditable selections."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> SkillRegistry:
        """The backing skill registry (read-only helper for plan builders)."""
        return self._registry

    def select_explicit(
        self, skill_name: str, *, reason: str, confidence: float = 0.99, source: str = ""
    ) -> SkillSelection | None:
        """Build an explicit selection.

        ``source`` is set when the user forced a specific discovery source via
        ``$<scope>:<name>`` — it is threaded through so a shadowed skill is both
        validated and executed against the intended source, not the winner.
        """
        entry = self._registry.resolve_ref(skill_name, source)
        if entry is None:
            return None
        return SkillSelection(
            skill=entry.name,
            reason=reason,
            matched_capabilities=matched_capabilities(entry, reason),
            selection_source="explicit",
            confidence=confidence,
            candidate_score=float(entry.priority or 0),
            contract_level=contract_level(entry),
            rejected_candidates=[],
            skill_source=source,
        )

    def select_capability(
        self,
        capability: str,
        *,
        message: str,
        reason: str,
        confidence: float,
    ) -> SkillSelection | None:
        entry, rejected = self._registry.resolve_capability(capability, request=message)
        if entry is None:
            return None
        return SkillSelection(
            skill=entry.name,
            reason=reason,
            matched_capabilities=matched_capabilities(entry, reason, capability=capability),
            selection_source="capability",
            confidence=confidence,
            candidate_score=float(entry.priority or 0),
            contract_level=contract_level(entry),
            rejected_candidates=[SkillCandidateRejection(skill=item.name, reason=why) for item, why in rejected[:3]],
        )


def contract_level(entry: SkillEntry) -> str:
    return getattr(entry, "contract_level", "none") or "none"


def matched_capabilities(entry: SkillEntry, reason: str, *, capability: str = "") -> list[str]:
    caps = list(entry.capabilities or [])
    if capability and capability not in caps:
        caps.insert(0, capability)
    return caps[:8]
