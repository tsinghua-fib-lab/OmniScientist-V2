"""P0 — machine-global memory: cross-workspace owner sharing + gray switch.

Acceptance for the memory topology work:
- an owner preference set in one project/workspace is recalled in another
  (identity memory follows the owner, not the directory);
- ``memory.global_store=off`` falls back to *exactly* today's behaviour
  (everything per-workspace, no global handle);
- enabling the store on a legacy install backfills existing user memory;
- the shared global store never leaks one IM peer's memory into another's.

All offline: the mock/``None`` provider path (keyword recall) is used throughout.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from omni.config import load_settings
from omni.memory.service import MemoryLayer, MemoryService, open_global_store
from omni.storage.db import get_database
from omni.storage.models import MemoryEntryORM


async def _memory_for(project: str, *, global_store: bool = True) -> tuple[MemoryService, object, object]:
    """Build a MemoryService for a named project sharing the isolated test home.

    Returns ``(service, workspace_db, global_db)``. Named projects (``-P``) share
    one ``home`` but keep separate ``project_db`` files, so two of them model two
    distinct workspaces on the same machine.
    """
    overrides = None if global_store else {"memory": {"global_store": False}}
    s = load_settings(project=project, overrides=overrides)
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    gdb = open_global_store(s)
    if gdb is not None:
        await gdb.init()
    return MemoryService(db, s, llm=None, global_db=gdb), db, gdb


@pytest.mark.asyncio
async def test_owner_preference_shared_across_workspaces() -> None:
    # Project A: the owner states a durable, user-scope preference.
    mem_a, _db_a, gdb_a = await _memory_for("proj_a")
    await mem_a.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="user prefers NeurIPS submission format", memory_type="preference",
        importance=0.8,
    )

    # Project B: a *different* workspace on the same machine recalls it.
    mem_b, db_b, gdb_b = await _memory_for("proj_b")
    res = await mem_b.recall("NeurIPS format", cross_session=True)
    assert any("NeurIPS" in sm.entry.summary for sm in res), "owner preference must cross workspaces"

    # It lives in the shared global store, never in project B's workspace db.
    assert gdb_a is gdb_b, "both workspaces must resolve the same global store"
    async with db_b.session() as s:
        local_rows = (await s.execute(select(MemoryEntryORM))).scalars().all()
    assert not any("NeurIPS" in r.summary for r in local_rows)
    async with gdb_b.session() as s:
        global_rows = (await s.execute(select(MemoryEntryORM))).scalars().all()
    assert any("NeurIPS" in r.summary for r in global_rows)


@pytest.mark.asyncio
async def test_global_store_off_is_legacy_workspace_only() -> None:
    s = load_settings(project="proj_a", overrides={"memory": {"global_store": False}})
    assert open_global_store(s) is None, "off ⇒ no global handle at all"

    mem_a, db_a, gdb_a = await _memory_for("proj_a", global_store=False)
    assert mem_a._global_db is None and gdb_a is None
    await mem_a.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="user prefers dark mode", memory_type="preference", importance=0.8,
    )
    # The preference stays in the workspace db (exactly today's behaviour) …
    async with db_a.session() as s2:
        rows = (await s2.execute(select(MemoryEntryORM))).scalars().all()
    assert any("dark mode" in r.summary for r in rows)

    # … and another workspace cannot see it (no shared store).
    mem_b, _db_b, _gdb_b = await _memory_for("proj_b", global_store=False)
    res = await mem_b.recall("dark mode preference", cross_session=True)
    assert not any("dark mode" in sm.entry.summary for sm in res)


@pytest.mark.asyncio
async def test_backfill_migrates_legacy_user_memory_to_global() -> None:
    # Legacy phase: global store OFF, user preference written to the workspace db.
    mem_legacy, _db, _gdb = await _memory_for("proj_a", global_store=False)
    await mem_legacy.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="user prefers concise answers with citations", memory_type="preference",
        importance=0.8,
    )

    # Enable the global store and run the one-time backfill for this workspace.
    mem, _db2, gdb = await _memory_for("proj_a")
    marker = mem._settings.paths.project_dir / ".global_memory_migrated"
    copied = await mem.migrate_identity_to_global(marker=marker)
    assert copied >= 1 and marker.exists()
    # Idempotent: a second run (marker present) copies nothing.
    assert await mem.migrate_identity_to_global(marker=marker) == 0

    async with gdb.session() as s:
        global_rows = (await s.execute(select(MemoryEntryORM))).scalars().all()
    assert any("concise answers" in r.summary for r in global_rows)

    # A different workspace now recalls the backfilled preference.
    mem_b, _db_b, _gdb_b = await _memory_for("proj_b")
    res = await mem_b.recall("concise answers citations", cross_session=True)
    assert any("concise answers" in sm.entry.summary for sm in res)


@pytest.mark.asyncio
async def test_global_store_preserves_peer_isolation() -> None:
    mem, _db, _gdb = await _memory_for("proj_a")
    # A peer's episodic memory routes to the global store but stays principal-tagged.
    await mem.record(
        layer=MemoryLayer.EPISODIC, scope="session", scope_id="sx",
        summary="bob asked about protein folding stability", memory_type="episode",
        principal="feishu:bob",
    )
    # Another peer must never recall it (no cross-peer leakage via the shared store).
    res_alice = await mem.recall("protein folding", principal="feishu:alice", cross_session=True)
    assert not any("bob" in sm.entry.summary for sm in res_alice)
    # The owning peer recalls their own memory.
    res_bob = await mem.recall("protein folding", principal="feishu:bob", cross_session=True)
    assert any("bob" in sm.entry.summary for sm in res_bob)
