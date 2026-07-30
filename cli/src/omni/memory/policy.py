"""Type-aware staleness & decay policy for long-term memory (P2.5).

Different kinds of knowledge age differently. A *methodology preference* should
not go stale; an empirical finding must be re-checked after a while so
it can't calcify into a hallucinated "fact". Dead-ends / negative results stay
retrievable (they're valuable to avoid repeating work) but don't dominate recall.

Centralising the tables here keeps ``MemoryService`` lean and the rules testable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from omni.core.timefmt import ensure_aware

# Types whose truth doesn't expire with time — never annotate "stale", never decay.
NON_DECAYING_TYPES: frozenset[str] = frozenset({
    "preference", "user_preference", "user_profile",
    "decision", "methodology", "constraint",
})

# Research-native types introduced in P2 (recognised, retrievable, weighted).
RESEARCH_TYPES: frozenset[str] = frozenset({
    "finding", "research_finding", "dead_end", "negative_result", "idea_evolution",
})

# Per-type staleness window (days). ``None`` → never stale. Anything not listed
# uses ``default_days`` from the caller (MemoryCfg.staleness_days).
_STALE_DAYS: dict[str, int | None] = {
    "finding": 45,
    "research_finding": 45,
    "episode": 30,
    "note": 60,
    "user_note": 120,
    # negative knowledge is long-lived: keep it findable so we don't redo dead ends
    "dead_end": 365,
    "negative_result": 365,
    "idea_evolution": 180,
}


def _aware(dt: datetime) -> datetime:
    return ensure_aware(dt)


def age_days(entry: Any, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    created = getattr(entry, "created_at", None)
    if not created:
        return 0.0
    return max(0.0, (now - _aware(created)).total_seconds() / 86400.0)


def is_non_decaying(entry: Any) -> bool:
    return (getattr(entry, "memory_type", "") or "") in NON_DECAYING_TYPES


def stale_window_days(entry: Any, *, default_days: int) -> int | None:
    mtype = getattr(entry, "memory_type", "") or ""
    if mtype in NON_DECAYING_TYPES:
        return None
    return _STALE_DAYS.get(mtype, default_days)


def is_stale(entry: Any, *, default_days: int, now: datetime | None = None) -> bool:
    """True when a *decaying* entry is older than its type's window and not pinned."""
    if getattr(entry, "pinned", 0):
        return False
    window = stale_window_days(entry, default_days=default_days)
    if window is None:
        return False
    return age_days(entry, now) > window


def decayed_importance(entry: Any, *, factor: float) -> float | None:
    """New importance after one decay step, or ``None`` if the entry shouldn't decay.

    Pinned and non-decaying types are exempt. Importance floors at 0.1 so a fact
    never disappears entirely from keyword recall — it just stops outranking
    fresh, verified knowledge.
    """
    if getattr(entry, "pinned", 0) or is_non_decaying(entry):
        return None
    cur = float(getattr(entry, "importance", 0.5) or 0.5)
    new = round(max(0.1, cur * factor), 4)
    return new if new < cur else None
