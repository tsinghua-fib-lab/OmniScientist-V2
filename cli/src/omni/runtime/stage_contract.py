"""Typed vocabulary for skill progress: stages, milestones, and info tiers.

The live display consumes an untyped ``progress_callback(stage, pct, **data)``
stream today, where the stage is a free-form string whose naming varies per
skill. This module gives that stream a small, explicit vocabulary so a renderer
can tell an in-progress *stage* (the transient thing a status line shows) from a
completed *milestone* (the one durable line kept in the persistent log), and can
gate detail by an information *tier* (L1-L4 of the readability redesign).

It changes no transport. The extra fields defined here ride the same progress
``data`` dict as ``stage``/``pct``; ``omni.runtime.workflow_progress.relayed_fields``
already forwards any key it does not own, so a skill that attaches ``milestone``
or ``current`` reaches the CLI without a single change to the event bus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

# Fields the stage contract reserves on a progress ``data`` dict. Producers and
# the reader agree on these names; they are deliberately outside
# ``RELAY_OWNED_KEYS`` so a nested skill's milestone survives a workflow relay.
STAGE_ID_KEY = "stage_id"  # normalized stage id, when a producer emits one
MILESTONE_KEY = "milestone"  # a completed-stage summary line for the log
STATS_KEY = "stats"  # structured counters backing a milestone
CURRENT_KEY = "current"  # the item being processed right now (status line 2)


class InfoTier(IntEnum):
    """The four information layers of the readability redesign.

    L1/L2/L3 are the user's story (what was asked, what is running, what was
    delivered); L4 is the engineering detail the default view hides so raw JSON,
    tokens, and internal ids never bury a result.
    """

    TASK = 1  # user input, task type, core constraints — persistent
    STAGE = 2  # retrieval / analysis / render — live, then compressed to L3
    DELIVERABLE = 3  # conclusions, artifacts, quality status — persistent
    DIAGNOSTIC = 4  # params, raw JSON, tokens, internal ids — hidden by default


def tier_visible(tier: InfoTier, verbosity: str, *, debug: bool = False) -> bool:
    """Whether a tier is shown at ``verbosity`` (``--debug`` reveals L4).

    The redesign keeps L1/L3 visible even under ``--quiet`` (the task and its
    deliverable are the point), lets ``normal`` add the L2 stage narrative, and
    reserves L4 for ``--debug`` or ``verbose``.
    """
    if tier == InfoTier.DIAGNOSTIC:
        return bool(debug) or verbosity == "verbose"
    if verbosity == "quiet":
        return tier in (InfoTier.TASK, InfoTier.DELIVERABLE)
    return True


def format_stats(stats: dict[str, Any] | None) -> list[str]:
    """Render stat pairs as ``label value`` fragments for a milestone line.

    Mapping keys are humanised only lightly (underscores to spaces) so a counter
    named ``kept`` reads as a word rather than an identifier. A value with no key
    is rendered alone, which lets a producer pass an already-phrased fragment.
    """
    if not isinstance(stats, dict):
        return []
    fragments: list[str] = []
    for key, value in stats.items():
        label = str(key).replace("_", " ").strip()
        text = f"{label} {value}".strip() if label else str(value).strip()
        if text:
            fragments.append(text)
    return fragments


@dataclass(frozen=True)
class StageEvent:
    """A stage in progress — the transient thing the dynamic status line shows.

    ``label`` is the human phrase for the status line (``analyzing papers``);
    ``detail`` is the concrete item under work right now (the paper's title),
    surfaced as the "current" line of the status region.
    """

    stage_id: str
    label: str
    detail: str = ""
    pct: float | None = None

    def to_progress_data(self) -> dict[str, Any]:
        """Serialize onto a progress ``data`` dict (transport-compatible)."""
        data: dict[str, Any] = {"stage": self.label}
        if self.stage_id:
            data[STAGE_ID_KEY] = self.stage_id
        if self.detail:
            data[CURRENT_KEY] = self.detail
        if self.pct is not None:
            data["pct"] = self.pct
        return data


@dataclass(frozen=True)
class Milestone:
    """A completed stage — the one durable line kept in the persistent log.

    ``summary`` is the human sentence (``Literature search complete``) and
    ``stats`` the counters the redesign appends after it (``found 126``,
    ``kept 20``). The display owns the leading glyph and colour.
    """

    stage_id: str
    summary: str
    stats: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        """One line: ``summary · stat · stat`` using the redesign separator."""
        parts: list[str] = []
        if self.summary.strip():
            parts.append(self.summary.strip())
        parts.extend(format_stats(self.stats))
        return " · ".join(parts)

    def to_progress_data(self) -> dict[str, Any]:
        """Serialize onto a progress ``data`` dict (transport-compatible)."""
        data: dict[str, Any] = {
            "stage": self.stage_id or self.summary,
            MILESTONE_KEY: self.summary,
        }
        if self.stage_id:
            data[STAGE_ID_KEY] = self.stage_id
        if self.stats:
            data[STATS_KEY] = dict(self.stats)
        return data


def milestone_from_progress(data: dict[str, Any]) -> Milestone | None:
    """Read a milestone off a progress ``data`` dict, or ``None`` if absent."""
    if not isinstance(data, dict):
        return None
    summary = str(data.get(MILESTONE_KEY) or "").strip()
    if not summary:
        return None
    stats = data.get(STATS_KEY)
    return Milestone(
        stage_id=str(data.get(STAGE_ID_KEY) or data.get("stage") or ""),
        summary=summary,
        stats=dict(stats) if isinstance(stats, dict) else {},
    )


def current_item(data: dict[str, Any]) -> str:
    """The 'currently processing' detail for status line 2, if a producer set it."""
    if not isinstance(data, dict):
        return ""
    return str(data.get(CURRENT_KEY) or "").strip()


__all__ = [
    "CURRENT_KEY",
    "InfoTier",
    "MILESTONE_KEY",
    "Milestone",
    "STAGE_ID_KEY",
    "STATS_KEY",
    "StageEvent",
    "current_item",
    "format_stats",
    "milestone_from_progress",
    "tier_visible",
]
