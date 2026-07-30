"""Human-review queue for self-evolution proposals.

The evolution loop no longer silently writes skills to disk (the old bare
``--install``). Instead it *proposes*: new-skill candidates (from successes) and
improvement patches (from failures) are appended to a durable review queue at
``~/.omni/skill_proposals.jsonl``. The owner reviews with ``omni skills
proposals``, then **approves** (applied to disk) or **rejects**. This is the
The improvement loop keeps a human in control: capability changes are always
consented to, mirroring the P0 tool-approval gate.

A proposal is one JSONL line with a stable ``id``, ``status`` (pending →
approved/applied | rejected), and a ``payload`` carrying exactly what ``apply``
needs (a rendered ``SKILL.md`` for new skills, or a lesson section to append for
improvements). Nothing here calls an LLM; distillation already happened upstream.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omni.skills_runtime.evolution import (
    LESSON_HEADING,
    CandidateSkill,
    ImprovementProposal,
    render_skill_md,
)
from omni.skills_runtime.registry import SkillRegistry

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
APPLIED = "applied"

NEW_SKILL = "new_skill"
IMPROVE_SKILL = "improve_skill"


@dataclass(slots=True)
class Proposal:
    """One reviewable self-evolution proposal."""

    id: str
    kind: str          # new_skill | improve_skill
    skill_name: str
    status: str        # pending | approved | rejected | applied
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    decided_at: str = ""
    applied_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "skill_name": self.skill_name,
            "status": self.status,
            "reasons": self.reasons,
            "metrics": self.metrics,
            "payload": self.payload,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "applied_path": self.applied_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Proposal:
        return cls(
            id=str(data.get("id") or ""),
            kind=str(data.get("kind") or ""),
            skill_name=str(data.get("skill_name") or ""),
            status=str(data.get("status") or PENDING),
            reasons=list(data.get("reasons") or []),
            metrics=dict(data.get("metrics") or {}),
            payload=dict(data.get("payload") or {}),
            created_at=str(data.get("created_at") or ""),
            decided_at=str(data.get("decided_at") or ""),
            applied_path=str(data.get("applied_path") or ""),
        )


def default_proposals_path(paths: Any = None) -> Path:
    """Where proposals are queued under the active Omni data directory."""
    if paths is not None and getattr(paths, "home", None):
        return Path(paths.home) / "skill_proposals.jsonl"
    from omni.config.paths import user_home

    return user_home() / "skill_proposals.jsonl"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ── construction ─────────────────────────────────────────────────────────────
def proposal_from_candidate(candidate: CandidateSkill) -> Proposal:
    """A ``new_skill`` proposal from a distilled success cluster."""
    return Proposal(
        id=_new_id(),
        kind=NEW_SKILL,
        skill_name=candidate.name,
        status=PENDING,
        reasons=[f"support={candidate.support}", *candidate.capabilities[:1]],
        metrics={"support": candidate.support, "source_task_ids": candidate.source_task_ids},
        payload={"skill_md": render_skill_md(candidate)},
        created_at=_now(),
    )


def proposal_from_improvement(improvement: ImprovementProposal) -> Proposal:
    """An ``improve_skill`` proposal from recurring failures of an existing skill."""
    return Proposal(
        id=_new_id(),
        kind=IMPROVE_SKILL,
        skill_name=improvement.skill_name,
        status=PENDING,
        reasons=list(improvement.reasons),
        metrics={
            "failures": improvement.failures,
            "total": improvement.total,
            "failure_rate": round(improvement.failure_rate, 3),
            "error_signatures": improvement.error_signatures,
        },
        payload={"lesson": improvement.lesson, "heading": LESSON_HEADING},
        created_at=_now(),
    )


# ── storage ──────────────────────────────────────────────────────────────────
def load_proposals(path: Path, *, status: str | None = None) -> list[Proposal]:
    """Read all proposals (optionally filtered by status), skipping bad lines."""
    if not path.is_file():
        return []
    out: list[Proposal] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        prop = Proposal.from_dict(data)
        if status is None or prop.status == status:
            out.append(prop)
    return out


def _write_all(path: Path, proposals: list[Proposal]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for prop in proposals:
            fh.write(json.dumps(prop.to_dict(), ensure_ascii=False) + "\n")


def enqueue(path: Path, proposals: list[Proposal]) -> list[Proposal]:
    """Append new proposals, skipping any whose ``(kind, skill_name)`` is already
    pending. Returns the proposals actually added."""
    existing = load_proposals(path)
    pending_keys = {(p.kind, p.skill_name) for p in existing if p.status == PENDING}
    added: list[Proposal] = []
    for prop in proposals:
        key = (prop.kind, prop.skill_name)
        if key in pending_keys:
            continue
        pending_keys.add(key)
        added.append(prop)
    if added:
        _write_all(path, existing + added)
    return added


def get(path: Path, pid: str) -> Proposal | None:
    """Resolve a proposal by full id or unambiguous id prefix (git-style)."""
    pid = (pid or "").strip()
    if not pid:
        return None
    matches = [p for p in load_proposals(path) if p.id == pid or p.id.startswith(pid)]
    return matches[0] if len(matches) == 1 else None


def _set_status(path: Path, pid: str, status: str, *, applied_path: str = "") -> Proposal | None:
    proposals = load_proposals(path)
    target: Proposal | None = None
    for prop in proposals:
        if prop.id == pid or prop.id.startswith(pid):
            if target is not None:
                return None  # ambiguous prefix
            target = prop
    if target is None:
        return None
    target.status = status
    target.decided_at = _now()
    if applied_path:
        target.applied_path = applied_path
    _write_all(path, proposals)
    return target


# ── application ──────────────────────────────────────────────────────────────
def _resolve_skill_md(registry: SkillRegistry, skill_name: str) -> Path | None:
    entry = registry.get(skill_name)
    if entry is None or entry.path is None:
        return None
    src = entry.path
    return (src / "SKILL.md") if src.is_dir() else src


def _append_lesson(md_text: str, heading: str, lesson: str) -> str:
    """Append a stamped lesson section to a skill's markdown (idempotent-ish)."""
    stamped = f"{heading}（{datetime.now(UTC).date().isoformat()}）\n\n{lesson.strip()}\n"
    return md_text.rstrip() + "\n\n" + stamped


def apply_proposal(proposal: Proposal, paths: Any, registry: SkillRegistry) -> str:
    """Write a proposal to ``~/.omni/skills`` and return the SKILL.md path.

    ``new_skill`` writes the rendered manifest. ``improve_skill`` patches the
    skill's own SKILL.md when it already lives under the user root, else copies
    the resolved manifest into the user root and appends the lesson there
    (copy-on-write override — shipped/builtin skills are never mutated in place).
    """
    dest_dir = Path(paths.user_skills_dir) / proposal.skill_name
    dest_md = dest_dir / "SKILL.md"
    dest_dir.mkdir(parents=True, exist_ok=True)

    if proposal.kind == NEW_SKILL:
        skill_md = str(proposal.payload.get("skill_md") or "")
        if not skill_md.strip():
            raise ValueError("new_skill proposal has no rendered SKILL.md")
        dest_md.write_text(skill_md, encoding="utf-8")
        return str(dest_md)

    if proposal.kind == IMPROVE_SKILL:
        lesson = str(proposal.payload.get("lesson") or "")
        heading = str(proposal.payload.get("heading") or LESSON_HEADING)
        if not lesson.strip():
            raise ValueError("improve_skill proposal has no lesson")
        src_md = _resolve_skill_md(registry, proposal.skill_name)
        base = src_md.read_text(encoding="utf-8") if (src_md and src_md.is_file()) else ""
        if not base.strip():
            raise ValueError(f"cannot resolve SKILL.md for '{proposal.skill_name}'")
        dest_md.write_text(_append_lesson(base, heading, lesson), encoding="utf-8")
        return str(dest_md)

    raise ValueError(f"unknown proposal kind '{proposal.kind}'")


def approve(path: Path, pid: str, paths: Any, registry: SkillRegistry) -> tuple[Proposal | None, str]:
    """Approve + apply a proposal. Returns ``(proposal, applied_path)``.

    On apply failure the proposal is left ``pending`` and the error is raised, so
    a bad write never leaves a half-approved record.
    """
    prop = get(path, pid)
    if prop is None:
        return None, ""
    if prop.status in (REJECTED, APPLIED):
        return prop, prop.applied_path
    applied_path = apply_proposal(prop, paths, registry)
    updated = _set_status(path, prop.id, APPLIED, applied_path=applied_path)
    return updated, applied_path


def reject(path: Path, pid: str) -> Proposal | None:
    """Mark a proposal rejected (never applied)."""
    return _set_status(path, pid, REJECTED)


# ── orchestration ────────────────────────────────────────────────────────────
async def generate_and_enqueue(
    db: Any,
    registry: SkillRegistry,
    paths: Any,
    *,
    llm: Any = None,
    limit: int = 200,
    min_support: int = 2,
    min_failures: int = 2,
    min_rate: float = 0.34,
    max_candidates: int = 5,
    max_improvements: int = 5,
    on_llm_call: Any = None,
) -> dict[str, Any]:
    """Run both halves of self-evolution and enqueue the reviewable proposals.

    New-skill candidates are **gated** (manifest/contract validity) before they
    enter the queue; improvement proposals target existing, resolvable skills.
    Returns a small summary dict (counts + the proposals actually added).
    """
    from omni.skills_runtime.evolution import (
        collect_improvements,
        collect_trajectories,
        gate_candidate,
        propose_candidates,
    )

    trajs = await collect_trajectories(db, limit=limit)
    candidates = await propose_candidates(
        trajs, registry=registry, llm=llm, min_support=min_support, max_candidates=max_candidates,
        on_llm_call=on_llm_call,
    )
    gated = [c for c in candidates if gate_candidate(c, registry=registry)[0]]
    improvements = await collect_improvements(
        db, registry, llm=llm, min_failures=min_failures, min_rate=min_rate, max_proposals=max_improvements,
        on_llm_call=on_llm_call,
    )

    props = [proposal_from_candidate(c) for c in gated]
    props += [proposal_from_improvement(i) for i in improvements]
    path = default_proposals_path(paths)
    added = enqueue(path, props)
    return {
        "considered": len(trajs),
        "candidates": len(gated),
        "improvements": len(improvements),
        "queued": len(added),
        "path": str(path),
        "added": added,
    }


__all__ = [
    "Proposal",
    "PENDING",
    "APPROVED",
    "REJECTED",
    "APPLIED",
    "NEW_SKILL",
    "IMPROVE_SKILL",
    "default_proposals_path",
    "proposal_from_candidate",
    "proposal_from_improvement",
    "load_proposals",
    "enqueue",
    "get",
    "apply_proposal",
    "approve",
    "reject",
    "generate_and_enqueue",
]
