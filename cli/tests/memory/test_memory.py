"""Memory: record, recall ranking, pinned + cross-session."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.memory.service import MemoryLayer, MemoryService
from omni.storage.db import get_database
from tests.conftest import ScriptedLLM


async def _service():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return MemoryService(db, s, llm=ScriptedLLM())


@pytest.mark.asyncio
async def test_record_and_recall_session_scope():
    mem = await _service()
    await mem.record(layer=MemoryLayer.SESSION, scope="session", scope_id="s1",
                     summary="gaussian splatting slam paper notes")
    await mem.record(layer=MemoryLayer.SESSION, scope="session", scope_id="other",
                     summary="unrelated cooking recipe")
    res = await mem.recall("gaussian splatting", session_id="s1")
    assert res
    assert "gaussian" in res[0].entry.summary


def test_embeddings_are_off_by_default():
    """New/unconfigured users stay on deterministic keyword recall."""
    assert load_settings().memory.embeddings_enabled is False


@pytest.mark.asyncio
async def test_disabled_embeddings_never_call_provider():
    """The off switch applies to both memory writes and query recall."""

    class _CountingLLM(ScriptedLLM):
        def __init__(self):
            super().__init__()
            self.embedding_calls = 0

        async def embed(self, texts):  # noqa: ANN001, ANN202
            self.embedding_calls += 1
            return await super().embed(texts)

    s = load_settings(overrides={"memory": {"embeddings_enabled": False}})
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    llm = _CountingLLM()
    mem = MemoryService(db, s, llm=llm)

    await mem.record(
        layer=MemoryLayer.SESSION, scope="session", scope_id="s1",
        summary="gaussian splatting notes",
    )
    result = await mem.recall("gaussian", session_id="s1")

    assert result
    assert llm.embedding_calls == 0


@pytest.mark.asyncio
async def test_recall_is_bounded_by_candidate_limit():
    """Recall never scans/returns more than ``recall_candidate_limit`` rows, even
    when a caller passes a huge ``limit`` (no full-store scan or exfiltration)."""
    s = load_settings()
    s.paths.ensure_dirs()
    s.memory.recall_candidate_limit = 5
    db = get_database(s.paths.project_db)
    await db.init()
    mem = MemoryService(db, s, llm=ScriptedLLM())

    for i in range(12):
        await mem.record(layer=MemoryLayer.SEMANTIC, scope="project",
                         summary=f"finding number {i}", memory_type="finding")
    # A caller asking for far more than the cap still gets at most the cap back.
    res = await mem.recall("finding", cross_session=True, limit=10_000)
    assert 0 < len(res) <= 5
    # recall_scoped honours the cap even when an explicit candidate_limit exceeds it.
    scoped = await mem.recall_scoped(
        "finding", layers=[MemoryLayer.SEMANTIC.value], limit=10_000, candidate_limit=10_000,
    )
    assert len(scoped) <= 5


@pytest.mark.asyncio
async def test_recall_degrades_to_keyword_when_embeddings_unavailable():
    """A chat-only endpoint (embed raises) must not break recall — it falls back
    to keyword overlap, fully functional, just not semantic."""

    class _NoEmbedLLM(ScriptedLLM):
        async def embed(self, texts):  # noqa: ANN001, ANN202
            raise NotImplementedError("this endpoint has no /embeddings route")

    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    mem = MemoryService(db, s, llm=_NoEmbedLLM())

    await mem.record(layer=MemoryLayer.SESSION, scope="session", scope_id="s1",
                     summary="gaussian splatting slam paper notes")
    await mem.record(layer=MemoryLayer.SESSION, scope="session", scope_id="s1",
                     summary="unrelated cooking recipe")
    res = await mem.recall("gaussian splatting", session_id="s1")
    assert res, "recall must still return results without embeddings"
    assert "gaussian" in res[0].entry.summary


@pytest.mark.asyncio
async def test_pinned_always_recalled():
    mem = await _service()
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="user",
                     summary="user prefers concise answers", pinned=True)
    res = await mem.recall("anything random", session_id="nope")
    assert any(m.entry.pinned for m in res)


@pytest.mark.asyncio
async def test_recall_block_formatting():
    mem = await _service()
    await mem.record(layer=MemoryLayer.EPISODIC, scope="session", scope_id="s1",
                     summary="finding X")
    res = await mem.recall("finding", session_id="s1")
    block = mem.build_recall_block(res)
    assert "Relevant memory" in block


@pytest.mark.asyncio
async def test_principal_isolation_cross_session_recall():
    """A peer's cross-session memory must not surface for another peer (P0 fix).

    The owner baseline ("local") stays visible to everyone; each IM peer only
    ever sees its own memory + the owner baseline, never a sibling peer's.
    """
    mem = await _service()
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="user",
                     summary="peer A likes gaussian splatting", principal="feishu:A")
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="user",
                     summary="peer B likes gaussian splatting", principal="feishu:B")
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="project",
                     summary="owner gaussian splatting baseline", principal="local")

    a_summaries = {m.entry.summary for m in await mem.recall(
        "gaussian", session_id="", cross_session=True, principal="feishu:A")}
    assert "peer A likes gaussian splatting" in a_summaries
    assert "owner gaussian splatting baseline" in a_summaries  # owner baseline shared
    assert "peer B likes gaussian splatting" not in a_summaries  # sibling isolated

    # recall_scoped (the compiler primitive) enforces the same boundary.
    b_scoped = {m.entry.summary for m in await mem.recall_scoped(
        "gaussian", layers=[MemoryLayer.SEMANTIC.value], principal="feishu:B")}
    assert "peer B likes gaussian splatting" in b_scoped
    assert "peer A likes gaussian splatting" not in b_scoped


@pytest.mark.asyncio
async def test_owner_only_memory_md_mirror(tmp_path):
    """Only the owner's user-scope writes mirror into the global MEMORY.md."""
    from omni.memory.files import load_curated_memory

    mem = await _service()
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="user",
                     summary="peer prefers English", principal="feishu:A")
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="user",
                     summary="owner prefers concise Chinese", principal="local")

    curated = load_curated_memory(mem._settings.paths)
    assert "owner prefers concise Chinese" in curated
    assert "peer prefers English" not in curated


@pytest.mark.asyncio
async def test_recall_is_usage_aware():
    """Between equally-similar memories, the more-recalled one ranks higher (P4)."""
    mem = await _service()
    hot = await mem.record(layer=MemoryLayer.SEMANTIC, scope="project",
                           summary="retrieval augmented generation baseline", memory_type="finding")
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="project",
                     summary="retrieval augmented generation baseline note", memory_type="finding")
    # Simulate the hot entry having been recalled many times.
    async with mem._db.session() as s:
        from omni.storage.models import MemoryEntryORM
        obj = await s.get(MemoryEntryORM, hot)
        obj.recall_count = 20
        await s.commit()
    res = await mem.recall("retrieval augmented generation", cross_session=True)
    assert res and res[0].entry.id == hot


@pytest.mark.asyncio
async def test_extraction_skips_degraded_and_external_only_turns():
    """Findings are not seeded from degraded/partial or pure-retrieval turns (P4)."""
    mem = await _service()
    messages = [
        {"role": "user", "content": "请判断方法A是否优于方法B"},
        # degraded assistant turn: must not become a finding
        {"role": "assistant", "content": "方法A在所有基准上都优于方法B，结论确定",
         "meta": {"kind": "partial", "terminated_reason": "max_iterations"}},
        # pure external-retrieval turn: must not become a finding either
        {"role": "assistant", "content": "方法C是当前最佳方案，已确认",
         "meta": {"kind": "text", "tools": ["web_fetch", "arxiv_search"]}},
    ]
    await mem.extract_session("degraded-sess", messages)
    summaries = " ".join(
        r.summary for r in await mem.list_recent(limit=50)
    )
    # the degraded/external claims must not have been distilled into memory
    assert "方法A在所有基准上都优于方法B" not in summaries
    assert "方法C是当前最佳方案" not in summaries


def test_user_memory_bullets_rewrite_roundtrip():
    """The MEMORY.md bullet reader/writer is safe and lossless for bullet files."""
    from omni.config import load_settings
    from omni.memory.files import (
        append_user_preference,
        read_user_memory_bullets,
        rewrite_user_memory,
    )

    s = load_settings()
    s.paths.ensure_dirs()
    append_user_preference(s.paths, "偏好中文正式写作")
    append_user_preference(s.paths, "目标会议 NeurIPS")
    bullets, safe = read_user_memory_bullets(s.paths)
    assert safe and "偏好中文正式写作" in bullets and "目标会议 NeurIPS" in bullets

    rewrite_user_memory(s.paths, ["合并后的唯一偏好"])
    bullets2, safe2 = read_user_memory_bullets(s.paths)
    assert safe2 and bullets2 == ["合并后的唯一偏好"]
