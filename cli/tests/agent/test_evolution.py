"""Self-evolution loop (P1-C): collect → cluster → distill → gate → install."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.config import load_settings
from omni.skills_runtime.evolution import (
    collect_trajectories,
    evolve_skills,
    gate_candidate,
    install_candidate,
    propose_candidates,
    render_skill_md,
)
from omni.skills_runtime.manifest import SkillEntry, SkillKind, parse_skill_text
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM

_LIT = [
    "write a literature review synthesis on retrieval augmented generation",
    "write a literature review synthesis of retrieval augmented generation methods",
    "literature review synthesis for retrieval augmented generation systems",
]
_FIG = [
    "generate a scientific architecture figure for the transformer model",
    "generate a scientific architecture figure for the RAG pipeline",
]


async def _db():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return s, db


async def _seed(db, goals, *, status="succeeded", skill="", tools=None, start=0):
    base = datetime.now(UTC)
    async with db.session() as sess:
        for i, g in enumerate(goals):
            sess.add(SubtaskORM(
                skill_name=skill, status=status,
                input_json={"goal": g},
                result_json={"summary": f"done: {g[:20]}"},
                trace_log=[{"tool": t} for t in (tools or [])],
                created_at=base + timedelta(seconds=start + i),
            ))
        await sess.commit()


def _registry(s) -> SkillRegistry:
    reg = SkillRegistry(s)
    reg.build_index()
    return reg


# ── collection ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_collect_only_succeeded():
    s, db = await _db()
    await _seed(db, _LIT, status="succeeded")
    await _seed(db, ["a failed one that should be ignored entirely here"], status="failed")
    trajs = await collect_trajectories(db)
    assert len(trajs) == 3
    assert all(t.signature for t in trajs)


# ── clustering + proposal ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_propose_two_clusters():
    s, db = await _db()
    await _seed(db, _LIT, tools=["search_corpus", "cite_source"], start=0)
    await _seed(db, _FIG, tools=["find_skill"], start=10)
    trajs = await collect_trajectories(db)
    cands = await propose_candidates(trajs, registry=_registry(s), min_support=2)
    assert len(cands) == 2
    supports = sorted(c.support for c in cands)
    assert supports == [2, 3]
    assert len({c.name for c in cands}) == 2  # distinct names
    lit = next(c for c in cands if c.support == 3)
    assert lit.name.startswith("evolved-")
    assert "search_corpus" in lit.allowed_tools


@pytest.mark.asyncio
async def test_min_support_filters_singletons():
    s, db = await _db()
    await _seed(db, ["one unique task that never repeats verbatim here alpha"], start=0)
    await _seed(db, ["another totally different unique task beta gamma delta"], start=5)
    trajs = await collect_trajectories(db)
    cands = await propose_candidates(trajs, registry=_registry(s), min_support=2)
    assert cands == []


# ── render + gate ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_render_parses_back_to_prompt_only():
    s, db = await _db()
    await _seed(db, _LIT, tools=["search_corpus"])
    cands = await propose_candidates(await collect_trajectories(db), registry=_registry(s))
    text = render_skill_md(cands[0])
    entry = parse_skill_text(text, default_name=cands[0].name, source="user_omni")
    assert entry.kind is SkillKind.PROMPT_ONLY
    assert entry.trigger.get("phrases")
    assert entry.contract_level == "full"


@pytest.mark.asyncio
async def test_gate_accepts_fresh_and_rejects_duplicate():
    s, db = await _db()
    await _seed(db, _LIT, tools=["search_corpus"])
    reg = _registry(s)
    cand = (await propose_candidates(await collect_trajectories(db), registry=reg))[0]
    ok, reasons = gate_candidate(cand, registry=reg)
    assert ok, reasons

    reg.register(SkillEntry(name=cand.name, description="pre-existing"))
    ok2, reasons2 = gate_candidate(cand, registry=reg)
    assert not ok2
    assert any("already exists" in r for r in reasons2)


# ── install + full loop ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_install_writes_and_indexes():
    s, db = await _db()
    await _seed(db, _LIT, tools=["search_corpus"])
    reg = _registry(s)
    cand = (await propose_candidates(await collect_trajectories(db), registry=reg))[0]
    path = install_candidate(cand, s.paths)
    assert (s.paths.user_skills_dir / cand.name / "SKILL.md").exists()
    assert path.endswith("SKILL.md")

    reg2 = _registry(s)
    entry = reg2.get(cand.name)
    assert entry is not None
    assert entry.kind is SkillKind.PROMPT_ONLY
    assert entry.source == "user_omni"


@pytest.mark.asyncio
async def test_evolve_dry_run_then_install():
    s, db = await _db()
    await _seed(db, _LIT, tools=["search_corpus"], start=0)
    await _seed(db, _FIG, start=10)

    dry = await evolve_skills(db, _registry(s), s.paths, None, install=False)
    assert dry.considered == 5
    assert {o.action for o in dry.outcomes} == {"proposed"}
    # nothing landed on disk
    assert not any(s.paths.user_skills_dir.glob("evolved-*/SKILL.md"))

    done = await evolve_skills(db, _registry(s), s.paths, None, install=True)
    assert done.installed == 2
    assert all(o.action == "installed" for o in done.outcomes)
    assert len(list(s.paths.user_skills_dir.glob("evolved-*/SKILL.md"))) == 2


# ── distillation source ──────────────────────────────────────────────────────
class _FakeLLM:
    async def chat(self, system: str, user: str) -> str:
        return "步骤如下：\n1. 先检索文献。\n2. 再综合成小节并标注引用。\n3. 输出可溯源结果。"


@pytest.mark.asyncio
async def test_distill_prefers_llm_body_when_usable():
    s, db = await _db()
    await _seed(db, _LIT, tools=["search_corpus"])
    trajs = await collect_trajectories(db)
    observed: list[str] = []

    async def observe(component: str, system: str, user: str, output: str) -> None:
        assert system and user and output
        observed.append(component)

    cands = await propose_candidates(
        trajs,
        registry=_registry(s),
        llm=_FakeLLM(),
        on_llm_call=observe,
    )
    assert "先检索文献" in cands[0].body
    assert observed == ["evolution:candidate_distill"]


@pytest.mark.asyncio
async def test_distill_falls_back_to_heuristic_without_llm():
    s, db = await _db()
    await _seed(db, _LIT, tools=["search_corpus", "cite_source"])
    cands = await propose_candidates(await collect_trajectories(db), registry=_registry(s), llm=None)
    assert "## Procedure" in cands[0].body
    assert "search_corpus" in cands[0].body  # common tool surfaced in heuristic


@pytest.mark.asyncio
async def test_evolution_cli_operation_persists_verified_maintenance_run():
    from omni.cli.commands.skills_cmd import _run_evolve
    from omni.runtime.task_recorder import TaskRecorder

    settings, db = await _db()
    report = await _run_evolve(settings, install=False, limit=1, min_support=2)
    runs = TaskRecorder(db, project=settings.paths.project_name)
    maintenance = next(run for run in await runs.list_tasks() if run.kind == "maintenance")
    events = await runs.list_events(maintenance.id)

    assert report.considered == 0
    assert maintenance.status == "succeeded"
    assert any(event.event_type == "evolution.completed" for event in events)
    assert any(event.event_type == "verification.passed" for event in events)


# ── CJK goals still yield a valid ascii skill ────────────────────────────────
@pytest.mark.asyncio
async def test_cjk_goals_install_cleanly():
    s, db = await _db()
    await _seed(db, ["写一篇关于检索增强生成的文献综述小节",
                     "写检索增强生成方向的文献综述",
                     "整理检索增强生成的文献综述"], tools=["search_corpus"])
    reg = _registry(s)
    cands = await propose_candidates(await collect_trajectories(db), registry=reg)
    assert cands, "CJK goals should still cluster"
    cand = cands[0]
    assert cand.name.startswith("evolved-")
    assert cand.name.isascii()
    ok, reasons = gate_candidate(cand, registry=reg)
    assert ok, reasons
