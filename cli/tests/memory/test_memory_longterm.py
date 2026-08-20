"""P1 — long-term memory backbone + compaction guardrails.

Covers:
- secret redaction before durable memory (P1.5);
- session extraction → M4 semantic + M3 episodic, with dedup (P1.2);
- recall scope: a task's M2 memory is reachable from its owning session (P1.1);
- transcript compaction folds older turns into a bridge summary and flushes
  durable facts first, keeping ``_history`` bounded (P1.3);
- ``context_report`` diagnostics (P1.4).
"""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.memory.compaction import summarize_messages
from omni.memory.sanitize import contains_secret, redact_secrets
from omni.memory.service import MemoryLayer, MemoryService
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM


async def _mem(*, facts: list[dict[str, str]] | None = None):
    s = load_settings()
    if facts is not None:
        s.model.provider = "test"
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    from tests.conftest import FactExtractionLLM, ScriptedLLM

    llm = FactExtractionLLM(facts) if facts is not None else ScriptedLLM()
    return MemoryService(db, s, llm=llm), db, s


# ── P1.5 secret sanitization ──────────────────────────────────────────────


def test_redact_secrets():
    assert "[REDACTED]" in redact_secrets("here is sk-ABCD1234EFGH5678IJKL token")
    assert "[REDACTED]" in redact_secrets("api_key: ABCD1234EFGH5678")
    assert "[REDACTED]" in redact_secrets("Authorization: Bearer abcdef123456ghijkl")
    out = redact_secrets("password=hunter2_long_secret_value")
    assert "hunter2_long_secret_value" not in out
    assert not contains_secret("just a normal research note about RAG")
    assert redact_secrets("") == ""


# ── P1.2 session extraction (heuristic, offline) ──────────────────────────


@pytest.mark.asyncio
async def test_extract_session_records_pref_decision_episode_and_dedups():
    mem, _db, _s = await _mem(facts=[
        {"text": "我更喜欢用中文输出，目标会议是 NeurIPS。", "type": "preference", "scope": "user"},
        {"text": "我们决定采用 RAG 方案来做检索。", "type": "decision", "scope": "project"},
        {"text": "请以后默认用我的 api_key=SECRETTOKEN12345 调用接口。", "type": "preference", "scope": "user"},
    ])
    messages = [
        {"role": "user", "content": "我更喜欢用中文输出，目标会议是 NeurIPS。"},
        {"role": "assistant", "content": "好的，已记住。"},
        {"role": "user", "content": "我们决定采用 RAG 方案来做检索。"},
        {"role": "assistant", "content": "明白，采用 RAG。"},
        {"role": "user", "content": "请以后默认用我的 api_key=SECRETTOKEN12345 调用接口。"},
    ]
    recorded = await mem.extract_session("S1", messages)
    assert recorded, "should distil at least one durable fact + an episode"

    rows = await mem.list_recent(limit=50)
    sem = [r for r in rows if r.layer == MemoryLayer.SEMANTIC.value]
    epi = [r for r in rows if r.layer == MemoryLayer.EPISODIC.value]
    assert any(r.memory_type == "preference" and r.scope == "user" for r in sem)
    assert any(r.memory_type == "decision" and r.scope == "project" for r in sem)
    assert epi, "an episodic summary should be recorded"

    # secrets never reach durable memory
    assert all("SECRETTOKEN12345" not in r.summary for r in rows)
    assert any("[REDACTED]" in r.summary for r in sem)

    # second extraction of the same transcript adds nothing (dedup)
    again = await mem.extract_session("S1", messages)
    assert again == []


# ── user-scope writes cross workspaces via the global MEMORY.md mirror ─────


@pytest.mark.asyncio
async def test_user_scope_write_mirrors_to_global_memory_file():
    from omni.memory.files import user_memory_file

    mem, _db, s = await _mem()
    # Any user-scope write (not just extraction) must land in ~/.omni/MEMORY.md
    # so it follows the researcher into other workspaces.
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="图示统一使用中文标签、正式论文风格", memory_type="preference",
    )
    mirrored = user_memory_file(s.paths).read_text(encoding="utf-8")
    assert "图示统一使用中文标签、正式论文风格" in mirrored


@pytest.mark.asyncio
async def test_project_scope_and_user_profile_are_not_mirrored():
    from omni.memory.files import user_memory_file

    mem, _db, s = await _mem()
    # project-scope stays workspace-local (only repo AGENTS.md crosses)…
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="project",
        summary="本项目决定采用 reranker 二次排序", memory_type="decision",
    )
    # …and the distilled user_profile blob is skipped (its prefs are mirrored individually).
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="用户画像：偏好中文、关注 RAG", memory_type="user_profile",
    )
    path = user_memory_file(s.paths)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    assert "本项目决定采用 reranker 二次排序" not in text
    assert "用户画像" not in text


# ── P1.1 recall scope: task memory reachable from owning session ──────────


@pytest.mark.asyncio
async def test_recall_reaches_owning_session_task_memory():
    mem, db, _s = await _mem()
    await mem.record(
        layer=MemoryLayer.TASK, scope="task", scope_id="tkAAA111",
        summary="任务产出：写好了引言部分，artifact://intro", memory_type="task",
        importance=0.5,
    )
    async with db.session() as ss:
        ss.add(SubtaskORM(id="tkAAA111", skill_name="synthesis.final",
                            status="succeeded", session_id="OWNER"))
        await ss.commit()

    # reachable from the owning session even without passing subtask_id
    hits = await mem.recall("引言", session_id="OWNER")
    assert any(h.entry.scope_id == "tkAAA111" for h in hits)

    # a different session must NOT see this task-scoped memory (M2 isn't cross-session)
    other = await mem.recall("引言", session_id="STRANGER")
    assert not any(h.entry.scope_id == "tkAAA111" for h in other)


# ── P1.3 compaction summariser (heuristic, offline) ───────────────────────


@pytest.mark.asyncio
async def test_summarize_messages_heuristic():
    s = load_settings()
    msgs = [
        {"role": "user", "content": "帮我调研 RAG 的最新进展"},
        {"role": "assistant", "content": "已产出综述 artifact://survey1"},
        {"role": "user", "content": "再写一段方法对比"},
    ]
    out = await summarize_messages(None, s, msgs)
    assert "RAG" in out
    assert "artifact://survey1" in out


# ── P1.3/P1.4 compaction + context report (agent-level) ───────────────────


@pytest.mark.asyncio
async def test_compact_session_folds_history_and_flushes():
    from omni.agent import OmniAgent

    agent = await OmniAgent.create(load_settings())
    try:
        sid = "compact-sess"
        for i in range(20):
            await agent._persist_message(sid, "user", f"问题 {i}：请继续推进研究第 {i} 步")
            await agent._persist_message(sid, "assistant", f"回答 {i}：已完成第 {i} 步")

        before = await agent._history(sid, limit=12)
        assert len(before) == 12  # bounded by limit even before compaction

        stats = await agent.compact_session(sid, keep_last=4)
        assert stats["compacted"] == 40 - 4  # 40 msgs total, keep last 4
        assert stats["after_tokens"] <= stats["before_tokens"]
        assert stats["saved_tokens"] == stats["before_tokens"] - stats["after_tokens"]

        # a single compaction bridge row now exists and originals are hidden
        visible = await agent._visible_normal_messages(sid)
        assert len(visible) == 4
        hist = await agent._history(sid, limit=12)
        assert "Earlier conversation summary" in hist[0]["content"]
        assert len(hist) == 1 + 4  # bridge + last 4 turns

        snapshot = await agent.context_snapshot(sid, include_injected=False)
        assert snapshot.prompt_messages == 5
        assert snapshot.active_messages == 4
        assert snapshot.compacted_messages == 36
        assert snapshot.transcript_tokens == stats["after_tokens"]
        assert snapshot.clearable_tokens == snapshot.transcript_tokens

        report = await agent.context_report(sid)
        assert "Context snapshot" in report
        assert "transcript" in report
        assert "compacted" in report
        assert "tokens" in report
        assert "/clear" in report
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_maybe_compact_does_not_trigger_on_message_count_alone():
    """Codex-aligned: many small turns stay intact while the window has room."""
    from omni.agent import OmniAgent
    from omni.agent.session_compactor import _COMPACT_THRESHOLD

    agent = await OmniAgent.create(load_settings())
    try:
        sid = "auto-sess"
        n = _COMPACT_THRESHOLD + 4
        for i in range(n):
            await agent._persist_message(sid, "user", f"msg {i} " + "x" * 20)
        compact_calls = {"n": 0}
        original = agent.compactor.compact

        async def _spy(*args, **kwargs):
            compact_calls["n"] += 1
            return await original(*args, **kwargs)

        agent.compactor.compact = _spy  # type: ignore[method-assign]
        await agent._maybe_compact(sid)
        visible = await agent._visible_normal_messages(sid)
        assert compact_calls["n"] == 0
        assert len(visible) == n
    finally:
        await agent.aclose()


# ── P2 model-aware compaction + microcompact ──────────────────────────────


def test_estimate_tokens_and_window_inference():
    from omni.config.settings import (
        infer_max_input_tokens,
        load_settings,
        resolve_max_input_tokens,
    )
    from omni.memory.compaction import estimate_tokens

    # CJK counts ~1 token/char; ASCII ~0.25.
    assert estimate_tokens("中文" * 100) > estimate_tokens("a" * 100)

    s = load_settings()
    s.model.model = "claude-3-5-sonnet"
    assert infer_max_input_tokens(s.model) == 200_000
    s.model.model = "deepseek-chat"
    assert infer_max_input_tokens(s.model) == 1_000_000
    s.model.model = "totally-unknown-model"
    assert infer_max_input_tokens(s.model) == 32_768
    # explicit pin wins over inference
    s.memory.context_window_tokens = 12_345
    assert resolve_max_input_tokens(s) == 12_345


def test_microcompact_tool_results_keeps_recent():
    from omni.memory.compaction import _MICROCOMPACT_PLACEHOLDER, microcompact_tool_results

    big = "X" * 5000
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "name": "search", "content": big},
        {"role": "tool", "tool_call_id": "2", "name": "search", "content": big},
        {"role": "tool", "tool_call_id": "3", "name": "search", "content": big},
    ]
    trimmed = microcompact_tool_results(messages, keep_last=2, max_chars=100)
    assert trimmed == 1  # only the oldest tool result shrinks
    assert len(messages[2]["content"]) < 200  # oldest trimmed
    assert "chars truncated" in messages[2]["content"]
    assert "Warning: truncated output" in messages[2]["content"]
    assert "X" in messages[2]["content"]
    assert messages[2]["content"].rstrip().endswith(_MICROCOMPACT_PLACEHOLDER)
    assert len(messages[3]["content"]) == 5000  # recent kept
    assert len(messages[4]["content"]) == 5000
    # idempotent: a second pass trims nothing new
    assert microcompact_tool_results(messages, keep_last=2, max_chars=100) == 0


@pytest.mark.asyncio
async def test_maybe_compact_token_budget_triggers_on_huge_turn():
    """A few but huge turns compact via the model-window token budget."""
    from omni.agent import OmniAgent
    from omni.agent.session_compactor import _COMPACT_KEEP_LAST

    settings = load_settings()
    settings.memory.context_window_tokens = 2_000  # tiny window → easy to exceed
    settings.memory.autocompact_pct = 0.5  # budget ≈ 1000 tokens
    agent = await OmniAgent.create(settings)
    try:
        sid = "huge-sess"
        # 12 turns, each ~400 CJK tokens → ~4800 tokens ≫ 1000 budget, but only
        # 12 messages (< count threshold), so this exercises the token trigger.
        for _ in range(6):
            await agent._persist_message(sid, "user", "问" * 400)
            await agent._persist_message(sid, "assistant", "答" * 400)
        await agent._maybe_compact(sid)
        visible = await agent._visible_normal_messages(sid)
        assert len(visible) == _COMPACT_KEEP_LAST
    finally:
        await agent.aclose()
