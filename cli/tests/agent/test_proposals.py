"""Human-review queue for self-evolution proposals (enqueue → review → apply)."""

from __future__ import annotations

from omni.config import load_settings
from omni.skills_runtime.evolution import CandidateSkill, ImprovementProposal
from omni.skills_runtime.manifest import SkillEntry, SkillKind
from omni.skills_runtime.proposals import (
    APPLIED,
    PENDING,
    REJECTED,
    apply_proposal,
    approve,
    enqueue,
    get,
    load_proposals,
    proposal_from_candidate,
    proposal_from_improvement,
    reject,
)
from omni.skills_runtime.registry import SkillRegistry


def _candidate(name="evolved-lit") -> CandidateSkill:
    return CandidateSkill(
        name=name,
        description="（自进化）复用一类任务",
        when_to_use="当出现同类任务时",
        trigger_phrases=["写文献综述小节"],
        capabilities=["evolved.lit"],
        allowed_tools=["search_corpus"],
        body="## 步骤\n\n1. 检索\n2. 综合\n",
        support=3,
        source_task_ids=["t1", "t2", "t3"],
    )


def _improvement(name="analyze") -> ImprovementProposal:
    return ImprovementProposal(
        skill_name=name, failures=3, total=3, failure_rate=1.0,
        lesson="常见失败与规避：\n- 反复出现：KeyError\n规避建议：\n- 先校验输入。",
        error_signatures=["KeyError"], sample_goals=["分析数据"], reasons=["failed 3/3"],
    )


def _registry(s, entry: SkillEntry | None = None) -> SkillRegistry:
    reg = SkillRegistry(s)
    reg.build_index()
    if entry is not None:
        reg.register(entry)
    return reg


# ── enqueue + dedup ──────────────────────────────────────────────────────────
def test_enqueue_dedups_pending_by_kind_and_skill(tmp_path):
    path = tmp_path / "proposals.jsonl"
    p1 = proposal_from_improvement(_improvement("analyze"))
    added1 = enqueue(path, [p1])
    assert len(added1) == 1
    # a second pending improvement for the same skill is skipped
    added2 = enqueue(path, [proposal_from_improvement(_improvement("analyze"))])
    assert added2 == []
    assert len(load_proposals(path)) == 1


def test_enqueue_allows_distinct_targets(tmp_path):
    path = tmp_path / "proposals.jsonl"
    enqueue(path, [proposal_from_improvement(_improvement("a"))])
    added = enqueue(path, [proposal_from_candidate(_candidate("evolved-x"))])
    assert len(added) == 1
    assert len(load_proposals(path)) == 2


def test_get_by_prefix(tmp_path):
    path = tmp_path / "proposals.jsonl"
    (p,) = enqueue(path, [proposal_from_candidate(_candidate())])
    assert get(path, p.id).id == p.id
    assert get(path, p.id[:6]).id == p.id
    assert get(path, "zzzz") is None


# ── apply: new skill ─────────────────────────────────────────────────────────
def test_apply_new_skill_writes_manifest():
    s = load_settings()
    s.paths.ensure_dirs()
    reg = _registry(s)
    prop = proposal_from_candidate(_candidate("evolved-newone"))
    path = apply_proposal(prop, s.paths, reg)
    assert path.endswith("SKILL.md")
    written = (s.paths.user_skills_dir / "evolved-newone" / "SKILL.md")
    assert written.is_file()
    reg2 = _registry(s)
    assert reg2.get("evolved-newone") is not None


# ── apply: improve existing (copy-on-write) ──────────────────────────────────
def test_apply_improvement_copies_and_appends_lesson():
    s = load_settings()
    s.paths.ensure_dirs()
    # a real on-disk user skill to improve
    skill_dir = s.paths.user_skills_dir / "analyzer"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: analyzer\ndescription: analyze\n---\n\n# analyzer\n\n原始正文。\n",
        encoding="utf-8",
    )
    reg = _registry(s)
    assert reg.get("analyzer") is not None

    prop = proposal_from_improvement(_improvement("analyzer"))
    out = apply_proposal(prop, s.paths, reg)
    text = (s.paths.user_skills_dir / "analyzer" / "SKILL.md").read_text(encoding="utf-8")
    assert out.endswith("SKILL.md")
    assert "原始正文" in text            # original body preserved
    assert "Known pitfalls and learned safeguards" in text  # lesson section appended
    assert "先校验输入" in text
    # still parses as a valid skill
    reg2 = _registry(s)
    entry = reg2.get("analyzer")
    assert entry is not None
    assert entry.kind in (SkillKind.PROMPT_ONLY, SkillKind.PYTHON_ENGINE, SkillKind.CLI_EXEC)


# ── approve / reject transitions ─────────────────────────────────────────────
def test_approve_applies_and_marks_applied(tmp_path):
    s = load_settings()
    s.paths.ensure_dirs()
    reg = _registry(s)
    path = tmp_path / "proposals.jsonl"
    (p,) = enqueue(path, [proposal_from_candidate(_candidate("evolved-approve"))])
    updated, applied_path = approve(path, p.id, s.paths, reg)
    assert updated.status == APPLIED
    assert applied_path.endswith("SKILL.md")
    assert (s.paths.user_skills_dir / "evolved-approve" / "SKILL.md").is_file()
    # persisted
    reloaded = get(path, p.id)
    assert reloaded.status == APPLIED


def test_reject_marks_rejected_without_writing(tmp_path):
    s = load_settings()
    s.paths.ensure_dirs()
    path = tmp_path / "proposals.jsonl"
    (p,) = enqueue(path, [proposal_from_candidate(_candidate("evolved-reject"))])
    updated = reject(path, p.id)
    assert updated.status == REJECTED
    assert not (s.paths.user_skills_dir / "evolved-reject").exists()
    # no longer in the pending view
    assert [x.id for x in load_proposals(path, status=PENDING)] == []
