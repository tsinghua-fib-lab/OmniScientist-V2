"""Evolution signals: mine durable stores for what's *working* and *failing*.

The self-evolution loop grows new capability from **successful** trajectories
(:mod:`omni.skills_runtime.evolution`). This module supplies the other half —
the *corrective* signal — by aggregating what recently went wrong so the loop can
propose **improvements to existing skills** and surface health to the owner:

* **failure signal** — per-skill success/failure counts + recurring error
  signatures, from ``skill_tasks`` (durable, skill-keyed).
* **verification signal** — count of ``verification.failed`` run events (the
  plan-contract layer said a run didn't meet its acceptance checks).
* **reviewer signal** — counts of ``reviewer.{pass,revise,reject}`` run events
  (LLM-as-judge verdicts on subagent output; see :mod:`omni.agent.subagents`).

Nothing here writes; it only reads what the runtime already records. That keeps
the signal a *read model* the evolution loop and ``omni skills`` can both trust.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from omni.storage.db import Database
from omni.storage.models import SubtaskORM, TaskEventORM

# Normalise volatile bits out of an error string so "file /a/b/1.txt not found"
# and "file /c/d/2.txt not found" collapse to one recurring signature.
_NUM_RE = re.compile(r"\d+")
_PATH_RE = re.compile(r"(/[^\s'\"]+|[A-Za-z]:\\[^\s'\"]+)")
_HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b")
_WS_RE = re.compile(r"\s+")


def error_signature(error: str, *, limit: int = 160) -> str:
    """Collapse a raw error into a stable, groupable signature."""
    text = (error or "").strip()
    if not text:
        return ""
    text = _PATH_RE.sub("<path>", text)
    text = _HEX_RE.sub("<hex>", text)
    text = _NUM_RE.sub("<n>", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


@dataclass(slots=True)
class SkillOutcomeSignal:
    """Aggregated recent outcomes for one skill."""

    skill_name: str
    succeeded: int = 0
    failed: int = 0
    error_signatures: dict[str, int] = field(default_factory=dict)
    sample_goals: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.succeeded + self.failed

    @property
    def failure_rate(self) -> float:
        return (self.failed / self.total) if self.total else 0.0

    def top_signatures(self, n: int = 5) -> list[tuple[str, int]]:
        return sorted(self.error_signatures.items(), key=lambda kv: (-kv[1], kv[0]))[:n]

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "failure_rate": round(self.failure_rate, 3),
            "top_signatures": self.top_signatures(),
            "sample_goals": self.sample_goals[:5],
        }


def _goal_of(task: SubtaskORM) -> str:
    data = task.input_json or {}
    if isinstance(data, dict):
        for key in ("goal", "input", "query", "question", "prompt"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


async def collect_skill_signals(
    db: Database, *, limit: int = 500
) -> dict[str, SkillOutcomeSignal]:
    """Aggregate recent (non-archived) skill-task outcomes keyed by skill name.

    Both successes and failures are counted so a skill's *failure rate* is
    meaningful (a skill that fails 2/2 is worse than one that fails 2/50).
    """
    async with db.session() as s:
        rows = (
            await s.execute(
                select(SubtaskORM)
                .where(SubtaskORM.archived_at.is_(None))
                .where(SubtaskORM.status.in_(("succeeded", "failed")))
                .order_by(SubtaskORM.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()

    signals: dict[str, SkillOutcomeSignal] = {}
    for t in rows:
        name = (t.skill_name or "").strip()
        if not name:
            continue
        sig = signals.setdefault(name, SkillOutcomeSignal(skill_name=name))
        if t.status == "succeeded":
            sig.succeeded += 1
            continue
        sig.failed += 1
        signature = error_signature(t.error or "")
        if signature:
            sig.error_signatures[signature] = sig.error_signatures.get(signature, 0) + 1
        goal = _goal_of(t)
        if goal and goal not in sig.sample_goals:
            sig.sample_goals.append(goal)
    return signals


async def _count_events_by_prefix(db: Database, prefix: str, *, limit: int) -> dict[str, int]:
    async with db.session() as s:
        rows = (
            await s.execute(
                select(TaskEventORM.event_type, func.count())
                .where(TaskEventORM.event_type.like(f"{prefix}%"))
                .group_by(TaskEventORM.event_type)
                .limit(limit)
            )
        ).all()
    return {str(et): int(n) for et, n in rows}


async def collect_reviewer_signals(db: Database, *, limit: int = 100) -> dict[str, int]:
    """Counts of ``reviewer.{pass,revise,reject}`` run events (verdict → count)."""
    counts = await _count_events_by_prefix(db, "reviewer.", limit=limit)
    return {et.split(".", 1)[1]: n for et, n in counts.items() if "." in et}


async def collect_verification_signals(db: Database, *, limit: int = 100) -> dict[str, int]:
    """Counts of ``verification.{passed,failed,pending,...}`` run events."""
    counts = await _count_events_by_prefix(db, "verification.", limit=limit)
    return {et.split(".", 1)[1]: n for et, n in counts.items() if "." in et}


@dataclass(slots=True)
class SignalDigest:
    """A single read model of recent agent health for the owner + evolve loop."""

    skills: dict[str, SkillOutcomeSignal]
    reviewer: dict[str, int]
    verification: dict[str, int]

    def failing_skills(self, *, min_failures: int = 2, min_rate: float = 0.34) -> list[SkillOutcomeSignal]:
        """Skills worth proposing an improvement for (enough failures + rate)."""
        out = [
            sig for sig in self.skills.values()
            if sig.failed >= min_failures and sig.failure_rate >= min_rate
        ]
        out.sort(key=lambda s: (-s.failed, -s.failure_rate, s.skill_name))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "skills": {k: v.to_dict() for k, v in self.skills.items()},
            "reviewer": self.reviewer,
            "verification": self.verification,
        }


async def collect_signal_digest(db: Database, *, limit: int = 500) -> SignalDigest:
    """One-shot: gather skill / reviewer / verification signals into a digest."""
    return SignalDigest(
        skills=await collect_skill_signals(db, limit=limit),
        reviewer=await collect_reviewer_signals(db),
        verification=await collect_verification_signals(db),
    )


__all__ = [
    "SkillOutcomeSignal",
    "SignalDigest",
    "error_signature",
    "collect_skill_signals",
    "collect_reviewer_signals",
    "collect_verification_signals",
    "collect_signal_digest",
]
