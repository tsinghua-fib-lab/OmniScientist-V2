"""End-to-end self-evolution journey: mine signal → propose → human approves.

This is the "以人为本" journey the P0 self-evolution epic promises, exercised the
way an owner would: repeated failures/successes accumulate, ``omni skills
proposals scan`` distills reviewable proposals into a queue (nothing auto-lands),
and only an explicit approval writes to ``~/.omni/skills``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.config import load_settings
from omni.skills_runtime.proposals import (
    IMPROVE_SKILL,
    NEW_SKILL,
    PENDING,
    approve,
    default_proposals_path,
    generate_and_enqueue,
    load_proposals,
)
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM


async def _db():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return s, db


async def _seed(db, skill, status, goals, *, error="", start=0):
    base = datetime.now(UTC)
    async with db.session() as sess:
        for i, g in enumerate(goals):
            sess.add(SubtaskORM(
                skill_name=skill, status=status,
                input_json={"goal": g}, error=error,
                created_at=base + timedelta(seconds=start + i),
            ))
        await sess.commit()


def _write_user_skill(paths, name: str) -> None:
    d = paths.user_skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: analyze data\n---\n\n# {name}\n\n原始流程正文。\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_journey_failures_to_approved_improvement():
    s, db = await _db()
    _write_user_skill(s.paths, "analyzer")
    reg = SkillRegistry(s)
    reg.build_index()
    assert reg.get("analyzer") is not None

    # a research skill keeps failing the same way
    await _seed(db, "analyzer", "failed",
                ["分析实验数据给显著性"] * 3, error="KeyError: 'pvalue'")

    summary = await generate_and_enqueue(db, reg, s.paths, llm=None)
    assert summary["improvements"] >= 1
    assert summary["queued"] >= 1

    ppath = default_proposals_path(s.paths)
    pending = load_proposals(ppath, status=PENDING)
    improve = next(p for p in pending if p.kind == IMPROVE_SKILL and p.skill_name == "analyzer")

    # nothing landed on disk yet — human review still owns the decision
    before = (s.paths.user_skills_dir / "analyzer" / "SKILL.md").read_text(encoding="utf-8")
    assert "Known pitfalls" not in before

    updated, applied_path = approve(ppath, improve.id, s.paths, reg)
    assert updated.status == "applied"
    after = (s.paths.user_skills_dir / "analyzer" / "SKILL.md").read_text(encoding="utf-8")
    assert "原始流程正文" in after          # original preserved
    assert "Known pitfalls and learned safeguards" in after  # improvement applied
    # the improved skill still resolves cleanly
    reg2 = SkillRegistry(s)
    reg2.build_index()
    assert reg2.get("analyzer") is not None


@pytest.mark.asyncio
async def test_journey_successes_to_new_skill_candidate():
    s, db = await _db()
    reg = SkillRegistry(s)
    reg.build_index()

    await _seed(db, "", "succeeded", [
        "write a literature review synthesis on retrieval augmented generation",
        "write a literature review synthesis of retrieval augmented generation methods",
        "literature review synthesis for retrieval augmented generation systems",
    ])
    summary = await generate_and_enqueue(db, reg, s.paths, llm=None)
    assert summary["candidates"] >= 1

    ppath = default_proposals_path(s.paths)
    new_props = [p for p in load_proposals(ppath, status=PENDING) if p.kind == NEW_SKILL]
    assert new_props, "a reusable new-skill candidate should be queued"

    updated, applied_path = approve(ppath, new_props[0].id, s.paths, reg)
    assert updated.status == "applied"
    assert applied_path.endswith("SKILL.md")
    reg2 = SkillRegistry(s)
    reg2.build_index()
    assert reg2.get(new_props[0].skill_name) is not None


@pytest.mark.asyncio
async def test_journey_single_failure_queues_nothing():
    s, db = await _db()
    _write_user_skill(s.paths, "analyzer")
    reg = SkillRegistry(s)
    reg.build_index()
    await _seed(db, "analyzer", "failed", ["一次性任务"], error="transient")
    summary = await generate_and_enqueue(db, reg, s.paths, llm=None)
    assert summary["queued"] == 0
    assert load_proposals(default_proposals_path(s.paths)) == []
