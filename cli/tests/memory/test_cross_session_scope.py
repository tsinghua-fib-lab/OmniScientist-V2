"""Cross-session recall scope: a layer filter narrows it, never widens it."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.memory.compiler import MemoryCompiler
from omni.memory.service import MemoryLayer, MemoryService
from omni.storage.db import get_database
from tests.conftest import ScriptedLLM

# What ``MemoryCompiler.compile_for_planning`` asks for.
_PLANNER_LAYERS = [
    MemoryLayer.SESSION.value,
    MemoryLayer.SEMANTIC.value,
    MemoryLayer.ARTIFACT.value,
]


async def _service():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return MemoryService(db, s, llm=ScriptedLLM())


@pytest.mark.asyncio
async def test_a_layer_filter_narrows_cross_session_reach_it_does_not_widen_it():
    """Asking for M1 asks for *this* session's dialogue, not every session's.

    A remark made once ("no key, try another approach") must not become a
    standing instruction in every later session.
    """
    mem = await _service()
    await mem.record(layer=MemoryLayer.SESSION, scope="session", scope_id="old",
                     summary="no semantic scholar key, try another approach")
    await mem.record(layer=MemoryLayer.SEMANTIC, scope="project",
                     summary="semantic scholar indexes citation graphs")

    recalled = await mem.recall_scoped(
        "semantic scholar", session_id="new", layers=_PLANNER_LAYERS, cross_session=True,
    )

    summaries = {sm.entry.summary for sm in recalled}
    assert "no semantic scholar key, try another approach" not in summaries
    assert "semantic scholar indexes citation graphs" in summaries


@pytest.mark.asyncio
async def test_the_dialogue_of_the_current_session_still_reaches_planning():
    """Narrowing cross-session reach must not cost within-session continuity."""
    mem = await _service()
    await mem.record(layer=MemoryLayer.SESSION, scope="session", scope_id="s1",
                     summary="the target venue for this paper is neurips")

    compiled = await MemoryCompiler(mem).compile_for_planning(
        query="target venue", session_id="s1",
    )

    assert "neurips" in compiled.text.lower()


@pytest.mark.asyncio
async def test_a_recalled_turn_carries_the_request_not_the_claim_about_it():
    """The entry that was replayed twenty-three times in one session.

    It announced that every task was complete and listed the deliverables, and
    recall surfaces by similarity with no sense of when a thing was written — so
    it came back on later requests that had produced none of them. Codex's
    compaction keeps the user's messages and drops assistant output; this is the
    same cut, and the request is the half that stays true.
    """
    from omni.agent.conversation_store import ConversationStore
    from omni.agent.turn_memory import TurnMemory
    from omni.core.react_agent import AgentLoopResult

    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    store = ConversationStore(
        db,
        project_name=settings.paths.project_name,
        channel_identity=settings.memory.channel_identity,
    )
    mem = MemoryService(db, settings, llm=ScriptedLLM())
    session_id = await store.ensure_session(channel="wechat")
    turns = TurnMemory(
        store=store, memory=mem, llm=ScriptedLLM(), settings=settings,
        tasks=None, paths=settings.paths,
    )

    await turns.record(
        session_id,
        "prepare materials for a RAG survey",
        AgentLoopResult(kind="text", content="All tasks complete. Full deliverables below: ..."),
        task_id="2e953101443b426e9ef1d947b6140891",
    )

    recorded = await mem.recall_scoped(
        "RAG survey materials", session_id=session_id, layers=[MemoryLayer.SESSION.value],
    )
    dialogue = [sm.entry.summary for sm in recorded if sm.entry.memory_type == "dialogue"]

    assert dialogue, "the request itself is still remembered"
    assert "prepare materials for a RAG survey" in dialogue[0]
    assert "2e953101" in dialogue[0], "and which task answered it"
    assert "All tasks complete" not in dialogue[0]


@pytest.mark.asyncio
async def test_a_recalled_deliverable_says_which_task_produced_it():
    """Recall stated that files existed and left their owner to be guessed.

    One session replayed an exchange 23 times whose summary read "全部任务完成，以下
    是完整交付物" — true of one task, recalled on every later one. Attribution is
    what lets the reader tell the two apart.
    """
    mem = await _service()
    await mem.record(layer=MemoryLayer.ARTIFACT, scope="task",
                     scope_id="90b7962d9e2f4a1b8c7d6e5f4a3b2c1d",
                     summary="RAG-System-Survey.md written")

    compiled = await MemoryCompiler(mem).compile_for_planning(
        query="RAG survey", session_id="s1",
    )

    assert "90b7962d" in compiled.text


@pytest.mark.asyncio
async def test_the_planner_and_the_owner_are_shown_the_same_cross_session_memory():
    """A memory that can steer a plan is one the owner can also find.

    ``memory search`` is the owner's window onto recall, so the two must apply
    one policy; anything outside it stays reachable through the unfiltered
    listing, which is what makes it deletable.
    """
    mem = await _service()
    await mem.record(layer=MemoryLayer.SESSION, scope="session", scope_id="old",
                     summary="latent space intervention survey went nowhere")

    planner = await mem.recall_scoped(
        "latent space intervention", session_id="new", layers=_PLANNER_LAYERS,
        cross_session=True,
    )
    owner = await mem.recall("latent space intervention", session_id="new", cross_session=True)

    assert [sm for sm in planner if sm.entry.scope_id == "old"] == []
    assert [sm for sm in owner if sm.entry.scope_id == "old"] == []
    listed = await mem.list_recent(layer=MemoryLayer.SESSION.value)
    assert any(row.scope_id == "old" for row in listed)
