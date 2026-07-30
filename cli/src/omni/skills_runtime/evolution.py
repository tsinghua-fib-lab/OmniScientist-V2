"""Self-evolution loop (P1-C): successful trajectories → candidate skills.

The closed loop the design docs deferred (HelixForge's ``eval_evolution``): mine
**successful** ``SubtaskORM`` trajectories, cluster the recurring ones, distill
each cluster into a *candidate* prompt-only ``SKILL.md`` (LLM when available,
deterministic heuristic otherwise), **gate** it (name dedupe + manifest re-parse +
contract check + registry resolvability), and only then write it to
``~/.omni/skills`` and re-index. Dry-run by default — nothing lands on disk
unless ``install=True``.

This grows new capability, distinct from memory distillation, which only grows
the runtime's understanding of the user.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import yaml
from sqlalchemy import select

from omni.skills_runtime.manifest import SkillKind, parse_skill_text
from omni.skills_runtime.registry import SkillRegistry
from omni.skills_runtime.signals import SignalDigest, SkillOutcomeSignal, collect_signal_digest
from omni.storage.db import Database
from omni.storage.models import SubtaskORM

LLMCallObserver = Callable[[str, str, str, str], Awaitable[None]]
logger = logging.getLogger(__name__)


async def _observe_llm_call(
    observer: LLMCallObserver | None,
    component: str,
    system: str,
    user: str,
    output: str,
) -> None:
    """Keep optional telemetry from changing evolution output."""
    if observer is None:
        return
    try:
        await observer(component, system, user, output)
    except Exception:  # noqa: BLE001
        logger.debug("evolution LLM observer failed", exc_info=True)

_LATIN_RE = re.compile(r"[a-z0-9]{3,}")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True)
class Trajectory:
    subtask_id: str
    skill_name: str
    goal: str
    summary: str
    tools: list[str]
    signature: frozenset[str]


@dataclass(slots=True)
class CandidateSkill:
    name: str
    description: str
    when_to_use: str
    trigger_phrases: list[str]
    capabilities: list[str]
    allowed_tools: list[str]
    body: str
    support: int
    source_task_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "support": self.support, "capabilities": self.capabilities,
            "trigger_phrases": self.trigger_phrases,
            "source_task_ids": self.source_task_ids,
        }


@dataclass(slots=True)
class ImprovementProposal:
    """A proposed *fix* to an existing skill, distilled from its recent failures.

    Unlike :class:`CandidateSkill` (a brand-new capability from successes), this
    appends a distilled "known pitfalls / lessons" section to an already-installed
    skill so it fails less next time — the corrective half of self-evolution.
    """

    skill_name: str
    failures: int
    total: int
    failure_rate: float
    lesson: str  # markdown section body appended to the skill
    error_signatures: list[str] = field(default_factory=list)
    sample_goals: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "failures": self.failures,
            "total": self.total,
            "failure_rate": round(self.failure_rate, 3),
            "lesson": self.lesson,
            "error_signatures": self.error_signatures,
            "sample_goals": self.sample_goals,
            "reasons": self.reasons,
        }


@dataclass(slots=True)
class EvolutionOutcome:
    name: str
    support: int
    action: str            # proposed | installed | rejected
    reasons: list[str]
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "support": self.support, "action": self.action,
                "reasons": self.reasons, "path": self.path}


@dataclass(slots=True)
class EvolutionReport:
    outcomes: list[EvolutionOutcome]
    considered: int
    installed: int

    def to_dict(self) -> dict[str, Any]:
        return {"considered": self.considered, "installed": self.installed,
                "outcomes": [o.to_dict() for o in self.outcomes]}


# ── tokenization / clustering ────────────────────────────────────────────────
def _tokens(text: str) -> list[str]:
    """Return script-neutral terms for lightweight trajectory clustering."""
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    words = [word for word in _WORD_RE.findall(normalized) if len(word) >= 2]
    compact = "".join(char for char in normalized if char.isalnum())
    grams = [compact[index:index + 3] for index in range(max(0, len(compact) - 2))]
    return list(dict.fromkeys([*words, *grams]))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0


def _slug(tokens: list[str], fallback: str) -> str:
    latin = [t for t in tokens if _LATIN_RE.fullmatch(t)]
    base = "-".join(latin[:3]) if latin else ""
    base = _SLUG_RE.sub("-", base).strip("-")
    return f"evolved-{base}" if base else f"evolved-{fallback}"


# ── collection ───────────────────────────────────────────────────────────────
def _goal_of(task: SubtaskORM) -> str:
    data = task.input_json or {}
    if isinstance(data, dict):
        for key in ("goal", "input", "query", "question", "prompt"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return (task.skill_name or "").strip()


def _summary_of(task: SubtaskORM) -> str:
    res = task.result_json or {}
    if isinstance(res, dict):
        for key in ("summary", "answer", "text", "outcome"):
            v = res.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:400]
    return ""


def _tools_of(task: SubtaskORM) -> list[str]:
    tools: list[str] = []
    for entry in task.trace_log or []:
        if isinstance(entry, dict):
            name = entry.get("tool") or entry.get("tool_name")
            if isinstance(name, str) and name and name not in tools:
                tools.append(name)
    return tools


async def collect_trajectories(db: Database, *, limit: int = 200) -> list[Trajectory]:
    """Load recent *succeeded*, non-archived skill tasks as trajectories."""
    async with db.session() as s:
        rows = (
            await s.execute(
                select(SubtaskORM)
                .where(SubtaskORM.status == "succeeded")
                .where(SubtaskORM.archived_at.is_(None))
                .order_by(SubtaskORM.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    out: list[Trajectory] = []
    for t in rows:
        goal = _goal_of(t)
        if not goal:
            continue
        out.append(Trajectory(
            subtask_id=t.id, skill_name=t.skill_name or "", goal=goal,
            summary=_summary_of(t), tools=_tools_of(t),
            signature=frozenset(_tokens(goal)),
        ))
    return out


def _cluster(trajs: list[Trajectory], *, threshold: float) -> list[list[Trajectory]]:
    """Single-linkage clustering: join a cluster if *any* member is similar."""
    clusters: list[list[Trajectory]] = []
    for t in trajs:
        if not t.signature:
            continue
        placed = False
        for cluster in clusters:
            if any(_jaccard(t.signature, m.signature) >= threshold for m in cluster):
                cluster.append(t)
                placed = True
                break
        if not placed:
            clusters.append([t])
    return clusters


# ── distillation ─────────────────────────────────────────────────────────────
_DISTILL_SYSTEM = (
    "You distill reusable skills from successful trajectories. Extract the shared method from each "
    "goal, tool trace, and result summary. Return only an English Markdown procedure for a prompt-only "
    "skill, with no YAML or commentary."
)


def _heuristic_body(cluster: list[Trajectory], *, common_tools: list[str]) -> str:
    goals = [t.goal for t in cluster]
    lines = [
        "A reusable procedure distilled from recurring successful tasks.",
        "",
        "## When to use",
        "",
    ]
    lines += [f"- Example: {g}" for g in _dedup(goals)[:5]]
    lines += ["", "## Procedure", ""]
    if common_tools:
        for i, tool in enumerate(common_tools[:8], 1):
            lines.append(f"{i}. Use `{tool}` for the corresponding step.")
    else:
        lines += [
            "1. Establish the objective and output contract.",
            "2. Retrieve or read the required inputs and execute the work incrementally.",
            "3. Synthesize a traceable final result and state any unresolved gaps.",
        ]
    lines += ["", "## Output", "", "Return a structured, traceable result. Identify sources by id, DOI, or URL."]
    return "\n".join(lines) + "\n"


async def _distill_body(
    llm: Any,
    cluster: list[Trajectory],
    *,
    common_tools: list[str],
    on_llm_call: LLMCallObserver | None = None,
) -> str:
    heuristic = _heuristic_body(cluster, common_tools=common_tools)
    if llm is None:
        return heuristic
    prompt_lines = ["Successful trajectories from the same cluster:"]
    for t in cluster[:8]:
        prompt_lines.append(f"- Goal: {t.goal}")
        if t.tools:
            prompt_lines.append(f"  Tools: {', '.join(t.tools[:8])}")
        if t.summary:
            prompt_lines.append(f"  Result: {t.summary[:160]}")
    prompt_lines.append("\nDistill these into a reusable step-by-step procedure.")
    user = "\n".join(prompt_lines)
    try:
        body = await llm.chat(_DISTILL_SYSTEM, user)
    except Exception:  # noqa: BLE001 — distillation is best-effort; fall back
        return heuristic
    await _observe_llm_call(
        on_llm_call,
        "evolution:candidate_distill",
        _DISTILL_SYSTEM,
        user,
        body,
    )
    body = (body or "").strip()
    # Guard against degenerate/echo replies (e.g. mock "summary:...").
    if len(body) < 40 or "\n" not in body:
        return heuristic
    return body + "\n"


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        k = it.strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def _common_tools(cluster: list[Trajectory]) -> list[str]:
    counts: dict[str, int] = {}
    for t in cluster:
        for tool in t.tools:
            counts[tool] = counts.get(tool, 0) + 1
    return [tool for tool, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


async def propose_candidates(
    trajs: list[Trajectory],
    *,
    registry: SkillRegistry,
    llm: Any = None,
    min_support: int = 2,
    threshold: float = 0.34,
    max_candidates: int = 5,
    on_llm_call: LLMCallObserver | None = None,
) -> list[CandidateSkill]:
    """Cluster trajectories and distill candidate skills (name-deduped)."""
    clusters = [c for c in _cluster(trajs, threshold=threshold) if len(c) >= min_support]
    clusters.sort(key=len, reverse=True)
    candidates: list[CandidateSkill] = []
    used_names: set[str] = set()
    for idx, cluster in enumerate(clusters):
        tokens: list[str] = []
        for t in cluster:
            tokens.extend(_tokens(t.goal))
        name = _slug(tokens, fallback=f"{idx + 1}")
        if name in used_names or registry.get(name) is not None:
            continue
        used_names.add(name)
        goals = _dedup([t.goal for t in cluster])
        common = _common_tools(cluster)
        body = await _distill_body(
            llm,
            cluster,
            common_tools=common,
            on_llm_call=on_llm_call,
        )
        candidates.append(CandidateSkill(
            name=name,
            description=f"Evolved procedure for a recurring successful task: {goals[0][:120]}",
            when_to_use=f"Use for tasks similar to: {goals[0][:80]}.",
            trigger_phrases=goals[:6],
            capabilities=[f"evolved.{name.removeprefix('evolved-') or 'skill'}"],
            allowed_tools=common[:8],
            body=body,
            support=len(cluster),
            source_task_ids=[t.subtask_id for t in cluster],
        ))
        if len(candidates) >= max_candidates:
            break
    return candidates


# ── rendering ────────────────────────────────────────────────────────────────
def render_skill_md(candidate: CandidateSkill) -> str:
    """Serialize a candidate to a Claude-Code-compatible ``SKILL.md`` string."""
    hf: dict[str, Any] = {
        "tier": "agent",
        "role": "task",
        "status": "experimental",
        "kind": "prompt_only",
        "delivery_mode": "sync_tool",
        "priority": 10,
        "capabilities": candidate.capabilities,
        "trigger": {
            "phrases": candidate.trigger_phrases,
            "when_to_use": candidate.when_to_use,
        },
        "input_schema": {
            "type": "object",
            "properties": {"input": {"type": "string", "description": "Task input"}},
            "required": ["input"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}, "answer": {"type": "string"}},
            "required": ["status"],
        },
        "evolution": {
            "origin": "self_evolved",
            "created_at": datetime.now(UTC).isoformat(),
            "support": candidate.support,
            "source_task_ids": candidate.source_task_ids,
        },
    }
    meta: dict[str, Any] = {
        "name": candidate.name,
        "description": candidate.description,
        "version": "0.1",
        "metadata": {"helixforge": hf},
    }
    if candidate.allowed_tools:
        meta["allowed-tools"] = candidate.allowed_tools
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front}\n---\n\n# {candidate.name}\n\n{candidate.body}"


# ── gating ───────────────────────────────────────────────────────────────────
def gate_candidate(candidate: CandidateSkill, *, registry: SkillRegistry) -> tuple[bool, list[str]]:
    """Validate a candidate before install. Returns ``(ok, reasons)``."""
    reasons: list[str] = []
    if registry.get(candidate.name) is not None:
        return False, [f"name '{candidate.name}' already exists"]
    if not candidate.trigger_phrases:
        reasons.append("no trigger phrases")
    if len((candidate.body or "").strip()) < 40:
        reasons.append("body too short")
    text = render_skill_md(candidate)
    try:
        entry = parse_skill_text(text, default_name=candidate.name, source="user_omni")
    except Exception as exc:  # noqa: BLE001 — a candidate that won't parse is rejected
        return False, [f"manifest parse failed: {exc}"]
    if entry.name != candidate.name:
        reasons.append("name mismatch after parse")
    if entry.kind is not SkillKind.PROMPT_ONLY:
        reasons.append(f"unexpected kind {entry.kind.value}")
    if entry.is_deprecated or entry.is_disabled:
        reasons.append("parsed status not usable")
    if entry.contract_level == "none":
        reasons.append("no declared contract")
    if not entry.trigger.get("phrases"):
        reasons.append("trigger phrases dropped after parse")
    ok = not reasons
    if ok:
        reasons = ["manifest valid", f"contract={entry.contract_level}", f"support={candidate.support}"]
    return ok, reasons


# ── install ──────────────────────────────────────────────────────────────────
def install_candidate(candidate: CandidateSkill, paths: Any, *, force: bool = False) -> str:
    """Write the candidate to ``~/.omni/skills/<name>/SKILL.md``; return the path."""
    dest_dir = paths.user_skills_dir / candidate.name
    skill_md = dest_dir / "SKILL.md"
    if skill_md.exists() and not force:
        raise FileExistsError(f"skill '{candidate.name}' already installed at {skill_md}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(render_skill_md(candidate), encoding="utf-8")
    return str(skill_md)


# ── improvement (failure → fix an existing skill) ────────────────────────────
LESSON_HEADING = "## Known pitfalls and learned safeguards"

_LESSON_SYSTEM = (
    "You improve a skill from failed trajectories. Given the skill name, failed goals, and recurring "
    "errors, summarize the pitfalls and concrete safeguards for the next run. Return only an English "
    "Markdown bullet list suitable for appending to SKILL.md; omit YAML and the heading."
)


def _heuristic_lesson(sig: SkillOutcomeSignal) -> str:
    lines = [
        f"Distilled from {sig.failed} failures among {sig.total} recent runs.",
        "",
        "Recurring failures and mitigations:",
    ]
    tops = sig.top_signatures(5)
    if tops:
        for signature, count in tops:
            lines.append(f"- Repeated {count} times: {signature}")
        lines += [
            "",
            "Safeguards:",
            "- Validate inputs and preconditions before execution; ask or degrade when data is missing.",
            "- Preflight recurring failure modes and retry only when safe; otherwise return a traceable failure.",
        ]
    else:
        lines += [
            "- Recent failures lack a stable signature; tighten input validation and record intermediate state.",
        ]
    return "\n".join(lines)


async def _distill_lesson(
    llm: Any,
    sig: SkillOutcomeSignal,
    *,
    on_llm_call: LLMCallObserver | None = None,
) -> str:
    heuristic = _heuristic_lesson(sig)
    if llm is None:
        return heuristic
    prompt_lines = [f"Skill: {sig.skill_name}", "", "Failed goals:"]
    prompt_lines += [f"- {g}" for g in sig.sample_goals[:6]] or ["- No recorded goal"]
    prompt_lines.append("Recurring errors:")
    prompt_lines += [f"- {s} (x{n})" for s, n in sig.top_signatures(5)] or ["- No stable signature"]
    prompt_lines.append("\nSummarize the pitfalls and safeguards.")
    user = "\n".join(prompt_lines)
    try:
        body = await llm.chat(_LESSON_SYSTEM, user)
    except Exception:  # noqa: BLE001 — distillation is best-effort; fall back
        return heuristic
    await _observe_llm_call(
        on_llm_call,
        "evolution:lesson_distill",
        _LESSON_SYSTEM,
        user,
        body,
    )
    body = (body or "").strip()
    if len(body) < 40 or "\n" not in body:  # guard degenerate/echo replies
        return heuristic
    return body


async def propose_improvements(
    digest: SignalDigest | dict[str, SkillOutcomeSignal],
    *,
    registry: SkillRegistry,
    llm: Any = None,
    min_failures: int = 2,
    min_rate: float = 0.34,
    max_proposals: int = 5,
    on_llm_call: LLMCallObserver | None = None,
) -> list[ImprovementProposal]:
    """Turn recurring per-skill failures into improvement proposals.

    Only skills that (a) exist in the registry and (b) crossed the failure
    thresholds are proposed — we never fabricate a fix for a one-off failure or a
    skill we can't locate. Human review + apply lives in
    :mod:`omni.skills_runtime.proposals`.
    """
    if isinstance(digest, SignalDigest):
        failing = digest.failing_skills(min_failures=min_failures, min_rate=min_rate)
    else:
        failing = [
            sig for sig in digest.values()
            if sig.failed >= min_failures and sig.failure_rate >= min_rate
        ]
        failing.sort(key=lambda s: (-s.failed, -s.failure_rate, s.skill_name))

    proposals: list[ImprovementProposal] = []
    for sig in failing:
        if registry.get(sig.skill_name) is None:
            continue  # can't improve a skill we can't resolve
        lesson = await _distill_lesson(llm, sig, on_llm_call=on_llm_call)
        proposals.append(ImprovementProposal(
            skill_name=sig.skill_name,
            failures=sig.failed,
            total=sig.total,
            failure_rate=sig.failure_rate,
            lesson=lesson,
            error_signatures=[s for s, _ in sig.top_signatures(5)],
            sample_goals=sig.sample_goals[:5],
            reasons=[f"failed {sig.failed}/{sig.total}", f"rate={sig.failure_rate:.0%}"],
        ))
        if len(proposals) >= max_proposals:
            break
    return proposals


# ── orchestration ────────────────────────────────────────────────────────────
async def evolve_skills(
    db: Database,
    registry: SkillRegistry,
    paths: Any,
    llm: Any = None,
    *,
    install: bool = False,
    limit: int = 200,
    min_support: int = 2,
    threshold: float = 0.34,
    max_candidates: int = 5,
    on_llm_call: LLMCallObserver | None = None,
) -> EvolutionReport:
    """Run the full loop: collect → propose → gate → (install|dry-run) → report."""
    trajs = await collect_trajectories(db, limit=limit)
    candidates = await propose_candidates(
        trajs, registry=registry, llm=llm, min_support=min_support,
        threshold=threshold, max_candidates=max_candidates, on_llm_call=on_llm_call,
    )
    outcomes: list[EvolutionOutcome] = []
    installed = 0
    for c in candidates:
        ok, reasons = gate_candidate(c, registry=registry)
        if not ok:
            outcomes.append(EvolutionOutcome(c.name, c.support, "rejected", reasons))
            continue
        if install:
            try:
                path = install_candidate(c, paths)
            except OSError as exc:
                outcomes.append(EvolutionOutcome(c.name, c.support, "rejected", [f"install failed: {exc}"]))
                continue
            installed += 1
            outcomes.append(EvolutionOutcome(c.name, c.support, "installed", reasons, path))
        else:
            outcomes.append(EvolutionOutcome(c.name, c.support, "proposed", reasons))
    if installed:
        registry.build_index()
    return EvolutionReport(outcomes=outcomes, considered=len(trajs), installed=installed)


async def collect_improvements(
    db: Database,
    registry: SkillRegistry,
    *,
    llm: Any = None,
    limit: int = 500,
    min_failures: int = 2,
    min_rate: float = 0.34,
    max_proposals: int = 5,
    on_llm_call: LLMCallObserver | None = None,
) -> list[ImprovementProposal]:
    """Convenience: gather the signal digest and distill improvement proposals."""
    digest = await collect_signal_digest(db, limit=limit)
    return await propose_improvements(
        digest, registry=registry, llm=llm,
        min_failures=min_failures, min_rate=min_rate, max_proposals=max_proposals,
        on_llm_call=on_llm_call,
    )


__all__ = [
    "Trajectory",
    "CandidateSkill",
    "ImprovementProposal",
    "EvolutionOutcome",
    "EvolutionReport",
    "LESSON_HEADING",
    "collect_trajectories",
    "propose_candidates",
    "propose_improvements",
    "collect_improvements",
    "render_skill_md",
    "gate_candidate",
    "install_candidate",
    "evolve_skills",
]
