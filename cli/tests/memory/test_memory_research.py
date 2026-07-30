"""P2 — "gets to know you" + research-native memory + maintenance.

Covers:
- type-aware staleness & decay policy (P2.5);
- session-end decay/dedup + user-profile distillation (P2.1/2.2);
- ROM-anchored memory via the ``remember`` tool + ``omni verify`` audit (P2.3);
- thread/hypothesis brief & latest-session resolution (P2.4);
- run↔claim↔artifact chaining via ``get_task`` + bidirectional curated import (P2.6);
- ``MemoryService`` CRUD that backs ``omni memory pin/detail/clear`` (P2.7).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.config import load_settings
from omni.memory import policy
from omni.memory.files import import_curated_memory
from omni.memory.service import MemoryLayer, MemoryService
from omni.research.store import ResearchStore
from omni.research.threads import build_thread_brief, latest_thread_session
from omni.research.verify import audit_memory_findings, verify_session
from omni.skills_runtime.builtin_tools.recall import build_recall_tools
from omni.skills_runtime.context import ExecContext
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import MemoryEntryORM


async def _mem(*, facts: list[dict[str, str]] | None = None):
    s = load_settings()
    if facts is not None:
        s.model.provider = "test"
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    if facts is None:
        llm = None
    else:
        from tests.conftest import FactExtractionLLM

        llm = FactExtractionLLM(facts)
    return MemoryService(db, s, llm=llm), db, s


def _entry(memory_type: str, *, importance=0.5, pinned=0, age_days=0.0) -> MemoryEntryORM:
    created = datetime.now(UTC) - timedelta(days=age_days)
    return MemoryEntryORM(memory_type=memory_type, importance=importance,
                          pinned=pinned, created_at=created, summary="x", layer="M4")


# ── P2.5 policy ────────────────────────────────────────────────────────────


def test_policy_type_aware_staleness_and_decay():
    finding = _entry("finding", importance=0.5, age_days=60)
    pref = _entry("preference", importance=0.5, age_days=400)
    dead = _entry("dead_end", importance=0.5, age_days=100)
    pinned = _entry("finding", importance=0.5, pinned=1, age_days=60)

    assert policy.is_stale(finding, default_days=45)
    assert not policy.is_stale(pref, default_days=45)   # preferences never stale
    assert not policy.is_stale(dead, default_days=45)   # dead-ends long-lived
    assert not policy.is_stale(pinned, default_days=45)  # pinned exempt

    assert policy.decayed_importance(finding, factor=0.9) == pytest.approx(0.45)
    assert policy.decayed_importance(pref, factor=0.9) is None
    assert policy.decayed_importance(pinned, factor=0.9) is None


# ── P2.1/2.2 decay + dedup + profile ───────────────────────────────────────


@pytest.mark.asyncio
async def test_decay_dedup_and_user_profile():
    mem, _db, _s = await _mem()
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="project",
                     summary="方法 A 在 GLUE 基准上达到 92.0 分", memory_type="finding",
                     importance=0.7)
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="project",
                     summary="方法 A 在 GLUE 基准上达到 92.0 分", memory_type="finding",
                     importance=0.6)  # near-duplicate
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
                     summary="我更喜欢用中文输出、目标会议 NeurIPS", memory_type="preference",
                     importance=0.8, pinned=True)

    stats = await mem.decay_and_dedup()
    assert stats["merged"] >= 1
    assert stats["decayed"] >= 1

    rows = await mem.list_recent(limit=50)
    pref = next(r for r in rows if r.memory_type == "preference")
    assert pref.importance == pytest.approx(0.8)  # pinned + non-decaying: unchanged
    finding = next(r for r in rows if r.memory_type == "finding")
    assert finding.importance < 0.7  # decayed

    await mem.record(layer=MemoryLayer.SEMANTIC, scope="project",
                     summary="决定采用 RAG 检索增强方案", memory_type="decision", importance=0.7)
    profile = await mem.rebuild_user_profile()
    assert profile and "User profile" in profile
    prof_rows = await mem.list_recent(limit=5, memory_type="user_profile")
    assert prof_rows and prof_rows[0].pinned


@pytest.mark.asyncio
async def test_owner_profile_written_to_global_file_and_injected():
    """The owner profile is persisted to ~/.omni/profile.md and injected everywhere."""
    from omni.memory.files import load_curated_memory, load_user_profile

    mem, _db, s = await _mem()
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
                     summary="偏好中文、目标会议 NeurIPS", memory_type="preference",
                     importance=0.8, pinned=True)
    await mem.rebuild_user_profile()  # owner (default principal)

    body = load_user_profile(s.paths)
    assert "NeurIPS" in body
    # profile.md is folded into the curated block injected into every workspace.
    curated = load_curated_memory(s.paths)
    assert "User profile (automatic)" in curated
    assert "NeurIPS" in curated


@pytest.mark.asyncio
async def test_peer_profile_stays_out_of_global_file():
    """An IM peer's profile must never touch the owner's global profile.md (P0)."""
    from omni.memory.files import load_user_profile

    mem, _db, s = await _mem()
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
                     summary="peer 只写英文论文", memory_type="preference",
                     importance=0.8, principal="feishu:A")
    prof = await mem.rebuild_user_profile(principal="feishu:A")
    assert prof  # peer still gets a DB profile entry (recalled only for them)
    assert load_user_profile(s.paths) == ""  # …but no global file was written


# ── P2.3 ROM-anchored memory + verify audit ────────────────────────────────


@pytest.mark.asyncio
async def test_remember_grounding_and_verify_audit():
    mem, db, s = await _mem()
    store = ResearchStore(db)
    src = await store.add_source({"title": "Efficient Transformers", "arxiv_id": "2401.00001"})

    ctx = ExecContext(settings=s, paths=s.paths, session_id="S",
                      db=db, artifacts=ArtifactStore(s.paths, db), llm=None)
    tools = {t.spec.name: t for t in build_recall_tools(ctx)}

    ungrounded = await tools["remember"].handler({"text": "X 在 Y 上达 92%", "type": "finding"})
    assert ungrounded["grounded"] is False
    grounded = await tools["remember"].handler(
        {"text": "稀疏注意力将显存降低 40%", "type": "finding", "source_id": src.id}
    )
    assert grounded["grounded"] is True

    unsupported, n_grounded, total = await audit_memory_findings(db)
    assert total == 2 and n_grounded == 1 and len(unsupported) == 1

    report = await verify_session(store)
    assert report.memory_total == 2
    assert report.memory_grounded == 1
    assert len(report.memory_unsupported) == 1


# ── P2.4 thread/hypothesis resume ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_thread_brief_and_latest_session():
    _mem_svc, db, _s = await _mem()
    store = ResearchStore(db)
    hyp = await store.add_hypothesis("Transformer 效率可在不掉点下提升", session_id="s1")
    await store.add_claim("稀疏注意力等效精度", session_id="s1", hypothesis_id=hyp.id)
    await store.add_run(title="bench-v1", session_id="s2", hypothesis_id=hyp.id,
                        output_uris=["artifact://r1"], status="succeeded")

    brief = await build_thread_brief(store, hyp.id[:8])
    assert brief and "Research thread" in brief
    assert "稀疏注意力等效精度" in brief
    assert "bench-v1" in brief

    sid = await latest_thread_session(store, hyp.id)
    assert sid in {"s1", "s2"}

    assert await build_thread_brief(store, "nonexistent") is None


# ── P2.6 run chain + bidirectional curated import ───────────────────────────


@pytest.mark.asyncio
async def test_get_run_chain_and_curated_import(tmp_path):
    mem, db, s = await _mem()
    store = ResearchStore(db)
    hyp = await store.add_hypothesis("线 H", session_id="s1")
    await store.add_claim("论断 C", session_id="s1", hypothesis_id=hyp.id)
    run = await store.add_run(title="run-1", hypothesis_id=hyp.id, seed=7,
                              output_uris=["artifact://fig1"], cmd="python train.py",
                              status="succeeded")

    ctx = ExecContext(settings=s, paths=s.paths, session_id="S",
                      db=db, artifacts=ArtifactStore(s.paths, db), llm=None)
    tools = {t.spec.name: t for t in build_recall_tools(ctx)}
    got = await tools["get_run"].handler({"run_id": run.id[:8]})
    assert got["output_uris"] == ["artifact://fig1"]
    assert got["seed"] == 7
    assert any(c["text"] == "论断 C" for c in got["claims"])

    # bidirectional: human-flagged lines in MEMORY.md flow back into the store
    (s.paths.home / "MEMORY.md").write_text(
        "# 我的记忆\n- 始终用中文回答\n- 引用必须可溯源\n普通说明不导入\n! 关键：实验默认 seed=42\n",
        encoding="utf-8",
    )
    added = await import_curated_memory(s.paths, mem)
    assert added >= 3
    rows = await mem.list_recent(limit=50)
    assert any("始终用中文回答" in r.summary and r.pinned for r in rows)
    assert any("seed=42" in r.summary for r in rows)
    # idempotent
    assert await import_curated_memory(s.paths, mem) == 0


# ── P2.7 CRUD backing /memory pin/detail/clear ──────────────────────────────


@pytest.mark.asyncio
async def test_memory_crud():
    mem, _db, _s = await _mem()
    mid = await mem.record(layer=MemoryLayer.SEMANTIC, scope="project",
                           summary="可清理的发现", memory_type="finding", importance=0.5)
    pid = await mem.record(layer=MemoryLayer.SEMANTIC, scope="user",
                           summary="重要偏好", memory_type="preference",
                           importance=0.8, pinned=True)

    # get by prefix
    assert (await mem.get(mid[:8])).id == mid
    # pin toggle
    assert await mem.set_pinned(mid, True)
    assert (await mem.get(mid)).pinned == 1
    await mem.set_pinned(mid, False)

    # clear by type keeps pinned entries
    removed = await mem.clear(memory_type="finding")
    assert removed == 1
    assert await mem.get(mid) is None
    assert (await mem.get(pid)) is not None  # pinned preference survives

    # delete
    assert await mem.delete(pid) is True
    assert await mem.get(pid) is None


@pytest.mark.asyncio
async def test_memory_resolve_reports_ambiguous_prefix():
    # A prefix that matches >1 entry must resolve to "ambiguous" (not the first
    # match), so `memory rm <prefix>` never deletes the wrong record.
    mem, db, _s = await _mem()
    from omni.storage.models import MemoryEntryORM

    async with db.session() as s:
        s.add(MemoryEntryORM(id="dupe0001aaaa", layer="M4", scope="project",
                             summary="A", memory_type="finding", importance=0.5))
        s.add(MemoryEntryORM(id="dupe0002bbbb", layer="M4", scope="project",
                             summary="B", memory_type="finding", importance=0.5))
        await s.commit()

    row, status = await mem.resolve("dupe000")
    assert status == "ambiguous" and row is None
    assert await mem.get("dupe000") is None  # get() stays safe on ambiguity

    row, status = await mem.resolve("dupe0001")
    assert status == "ok" and row is not None and row.id == "dupe0001aaaa"

    row, status = await mem.resolve("nope")
    assert status == "not_found" and row is None


@pytest.mark.asyncio
async def test_extraction_drops_error_and_trivial_noise():
    # Runtime errors ("402 Payment Required") and trivial control chatter
    # ("exit") must never be distilled into durable memory or an episode.
    mem, _db, _s = await _mem()
    messages = [
        {"role": "user", "content": "exit"},
        {"role": "assistant", "content": "LLM 调用失败：Client error '402 Payment Required' for url ..."},
    ]
    recorded = await mem.extract_session("Snoise", messages)
    rows = await mem.list_recent(limit=50)
    assert recorded == []
    assert not any("402" in r.summary or "Payment" in r.summary for r in rows)
    assert not any(r.layer == MemoryLayer.EPISODIC.value for r in rows)


@pytest.mark.asyncio
async def test_maintenance_warns_once_when_store_exceeds_threshold(caplog):
    # Recall is now bounded, so the store-size hint fires from session-end
    # maintenance (decay_and_dedup), exactly once per process — not per turn.
    import logging

    mem, _db, s = await _mem(facts=[
        {"text": "我更喜欢用中文输出，目标会议是 NeurIPS。", "type": "preference", "scope": "user"},
    ])
    s.memory.max_entries_warn = 3
    for i in range(4):
        await mem.record(layer=MemoryLayer.SEMANTIC, scope="project",
                         summary=f"finding number {i}", memory_type="finding",
                         importance=0.5, pinned=True)
    with caplog.at_level(logging.WARNING, logger="omni.memory.service"):
        await mem.decay_and_dedup()
        await mem.decay_and_dedup()
    warnings = [r for r in caplog.records if "recall is bounded to the top" in r.message]
    assert len(warnings) == 1  # one-shot, not per-call


@pytest.mark.asyncio
async def test_user_preference_is_mirrored_into_global_memory_file():
    # A distilled user preference must land in ~/.omni/MEMORY.md so it follows
    # the researcher into every workspace (user-scope must be user-global).
    from omni.memory.files import user_memory_file

    mem, _db, s = await _mem(facts=[
        {"text": "我更喜欢用中文输出，目标会议是 NeurIPS。", "type": "preference", "scope": "user"},
    ])
    messages = [
        {"role": "user", "content": "我更喜欢用中文输出，目标会议是 NeurIPS。"},
        {"role": "assistant", "content": "好的，已记住。"},
    ]
    await mem.extract_session("Spref", messages)
    text = user_memory_file(s.paths).read_text(encoding="utf-8")
    assert "中文" in text and "- " in text
