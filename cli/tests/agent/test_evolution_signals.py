"""Corrective self-evolution: failure/verification/reviewer signals → improvements."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.config import load_settings
from omni.skills_runtime.evolution import collect_improvements, propose_improvements
from omni.skills_runtime.manifest import SkillEntry
from omni.skills_runtime.registry import SkillRegistry
from omni.skills_runtime.signals import (
    collect_reviewer_signals,
    collect_signal_digest,
    collect_skill_signals,
    error_signature,
)
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM, TaskEventORM, TaskORM


async def _db():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return s, db


async def _seed_tasks(db, skill, *, succeeded=0, failed=0, error="", goal="do the thing"):
    base = datetime.now(UTC)
    idx = 0
    async with db.session() as sess:
        for _ in range(succeeded):
            sess.add(SubtaskORM(skill_name=skill, status="succeeded",
                                  input_json={"goal": goal},
                                  created_at=base + timedelta(seconds=idx)))
            idx += 1
        for _ in range(failed):
            sess.add(SubtaskORM(skill_name=skill, status="failed",
                                  input_json={"goal": goal}, error=error,
                                  created_at=base + timedelta(seconds=idx)))
            idx += 1
        await sess.commit()


def _registry(s, *names) -> SkillRegistry:
    reg = SkillRegistry(s)
    reg.build_index()
    for name in names:
        reg.register(SkillEntry(name=name, description=f"seeded {name}"))
    return reg


# ── error signatures ─────────────────────────────────────────────────────────
def test_error_signature_collapses_volatile_bits():
    a = error_signature("file /tmp/run/42/out.txt not found (code 17)")
    b = error_signature("file /var/x/99/out.txt not found (code 3)")
    assert a == b
    assert "<path>" in a and "<n>" in a


def test_error_signature_empty():
    assert error_signature("") == ""


# ── skill outcome signals ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_collect_skill_signals_counts_and_rates():
    s, db = await _db()
    await _seed_tasks(db, "analyze", succeeded=8, failed=2, error="KeyError: 'pvalue' at 42")
    signals = await collect_skill_signals(db)
    sig = signals["analyze"]
    assert sig.succeeded == 8
    assert sig.failed == 2
    assert sig.total == 10
    assert abs(sig.failure_rate - 0.2) < 1e-9
    # errors are grouped by signature
    assert sig.top_signatures()[0][1] == 2


@pytest.mark.asyncio
async def test_signals_skip_blank_skill_names():
    s, db = await _db()
    await _seed_tasks(db, "", succeeded=3)
    assert await collect_skill_signals(db) == {}


@pytest.mark.asyncio
async def test_digest_failing_skills_thresholds():
    s, db = await _db()
    await _seed_tasks(db, "flaky", failed=3, error="boom")
    await _seed_tasks(db, "solid", succeeded=10, failed=1, error="rare")
    digest = await collect_signal_digest(db)
    failing = {sig.skill_name for sig in digest.failing_skills(min_failures=2, min_rate=0.34)}
    assert "flaky" in failing        # 3/3 failures
    assert "solid" not in failing    # only 1/11 → below rate


# ── reviewer signals from run events ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_collect_reviewer_signals_from_events():
    s, db = await _db()
    async with db.session() as sess:
        sess.add(TaskORM(id="r1"))
        sess.add(TaskORM(id="r2"))
        await sess.flush()
        sess.add(TaskEventORM(task_id="r1", seq=1, event_type="reviewer.reject"))
        sess.add(TaskEventORM(task_id="r1", seq=2, event_type="reviewer.pass"))
        sess.add(TaskEventORM(task_id="r2", seq=1, event_type="reviewer.pass"))
        await sess.commit()
    counts = await collect_reviewer_signals(db)
    assert counts.get("pass") == 2
    assert counts.get("reject") == 1


# ── improvement proposals ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_propose_improvements_only_for_resolvable_failing_skills():
    s, db = await _db()
    await _seed_tasks(db, "known", failed=3, error="KeyError: 'x'")
    await _seed_tasks(db, "unknown", failed=3, error="KeyError: 'y'")
    digest = await collect_signal_digest(db)
    reg = _registry(s, "known")  # only "known" is registered
    props = await propose_improvements(digest, registry=reg, llm=None)
    names = {p.skill_name for p in props}
    assert "known" in names
    assert "unknown" not in names  # can't improve a skill we can't resolve


@pytest.mark.asyncio
async def test_improvement_lesson_is_heuristic_without_llm():
    s, db = await _db()
    await _seed_tasks(db, "known", failed=4, error="Timeout after 30s waiting for API")
    props = await collect_improvements(db, _registry(s, "known"), llm=None)
    assert len(props) == 1
    lesson = props[0].lesson
    assert "mitigation" in lesson.lower()
    assert "timeout" in lesson.lower()
    assert props[0].failures == 4


@pytest.mark.asyncio
async def test_single_failure_does_not_propose():
    s, db = await _db()
    await _seed_tasks(db, "known", failed=1, error="one-off")
    props = await collect_improvements(db, _registry(s, "known"), llm=None, min_failures=2)
    assert props == []
