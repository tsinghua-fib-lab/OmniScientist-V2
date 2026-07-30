"""P1 — consolidation pipeline: bounded, change-gated global digest + concurrency.

Acceptance:
- ``memory_summary.md`` is token-bounded and rewritten *only* when the underlying
  global memory changed (a no-op consolidation touches nothing);
- a session end consolidates new durable facts into the global store, and they
  surface in the injected digest;
- two independent processes writing the global store concurrently never corrupt
  it (WAL) and the consolidation lock serializes read-modify-write maintenance.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text

from omni.config import load_settings
from omni.memory.files import load_memory_summary, write_memory_summary
from omni.memory.locks import global_memory_lock
from omni.memory.service import MemoryLayer, MemoryService, open_global_store
from omni.storage.db import Database
from omni.storage.models import MemoryEntryORM


async def _memory_for(project: str) -> tuple[MemoryService, object, object]:
    s = load_settings(project=project)
    s.paths.ensure_dirs()
    from omni.storage.db import get_database

    db = get_database(s.paths.project_db)
    await db.init()
    gdb = open_global_store(s)
    await gdb.init()
    return MemoryService(db, s, llm=None, global_db=gdb), s, gdb


@pytest.mark.asyncio
async def test_summary_is_bounded_and_only_refreshes_on_change() -> None:
    mem, s, _gdb = await _memory_for("proj_a")
    # Seed many owner preferences so the digest would overflow without a bound.
    for i in range(40):
        await mem.record(
            layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
            summary=f"user preference number {i} " + "x" * 80,
            memory_type="preference", importance=0.6,
        )

    budget = int(s.memory.summary_token_budget) * 4
    changed = await mem.refresh_global_summary(s.paths)
    assert changed is True
    path = s.paths.memory_summary_file
    assert path.is_file()
    body = load_memory_summary(s.paths, budget=10_000)
    assert body and len(body) <= budget + 200  # bounded (header/marker stripped)

    # A second refresh with no memory change must NOT rewrite the file.
    mtime = path.stat().st_mtime_ns
    assert await mem.refresh_global_summary(s.paths) is False
    assert path.stat().st_mtime_ns == mtime, "unchanged memory must not churn the digest"

    # A new durable preference *does* change it.
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="user strongly prefers dark mode terminals", memory_type="preference",
        importance=0.99, pinned=True,
    )
    assert await mem.refresh_global_summary(s.paths) is True
    assert "dark mode" in load_memory_summary(s.paths, budget=10_000)


@pytest.mark.asyncio
async def test_digest_is_injected_via_curated_memory() -> None:
    from omni.memory.files import load_curated_memory

    _mem, s, _gdb = await _memory_for("proj_a")
    write_memory_summary(s.paths, ["user prefers concise, cited answers"])
    block = load_curated_memory(s.paths)
    assert "Global memory digest" in block
    assert "concise, cited answers" in block
    # The internal hash marker must never leak into the prompt.
    assert "memhash" not in block


@pytest.mark.asyncio
async def test_write_memory_summary_empty_removes_stale_file() -> None:
    _mem, s, _gdb = await _memory_for("proj_a")
    assert write_memory_summary(s.paths, ["- keep me"]) is True
    assert s.paths.memory_summary_file.is_file()
    # Empty digest → stale file removed so we never inject outdated data.
    assert write_memory_summary(s.paths, []) is True
    assert not s.paths.memory_summary_file.is_file()


@pytest.mark.asyncio
async def test_session_end_consolidates_new_facts_into_global() -> None:
    from tests.conftest import FactExtractionLLM

    mem, s, gdb = await _memory_for("proj_a")
    # A real (non-mock) provider is required for LLM fact extraction to run.
    s.model.provider = "openai_compatible"
    mem._llm = FactExtractionLLM(
        [{"text": "user prefers answers grounded in citations", "type": "preference", "scope": "user"}]
    )
    messages = [
        {"role": "user", "content": "Please always cite sources when you answer."},
        {"role": "assistant", "content": "Understood — I will ground answers in citations."},
    ]
    recorded = await mem.extract_session("sess1", messages)
    assert recorded, "session end should distil at least one durable fact"

    # The new durable preference landed in the machine-global store …
    async with gdb.session() as gs:
        rows = (await gs.execute(
            select(MemoryEntryORM).where(MemoryEntryORM.memory_type == "preference")
        )).scalars().all()
    assert any("citations" in r.summary for r in rows)

    # … and surfaces in the injected, change-gated digest.
    assert await mem.refresh_global_summary(s.paths) is True
    assert "citation" in load_memory_summary(s.paths, budget=10_000).lower()


@pytest.mark.asyncio
async def test_concurrent_processes_do_not_corrupt_global_store() -> None:
    # Two *separate* Database handles on the same file model two processes.
    s = load_settings(project="proj_a")
    s.paths.ensure_dirs()
    gpath = s.paths.global_memory_db
    db1, db2 = Database(gpath), Database(gpath)
    await db1.init()
    await db2.init()
    mem1 = MemoryService(get_ws(s), s, llm=None, global_db=db1)
    mem2 = MemoryService(get_ws(s), s, llm=None, global_db=db2)

    async def writer(mem: MemoryService, tag: str, n: int) -> None:
        for i in range(n):
            await mem.record(
                layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
                summary=f"{tag} preference {i}", memory_type="preference",
                importance=0.5, embed=False,
            )

    # Interleave concurrent writers from both "processes".
    await asyncio.gather(writer(mem1, "A", 25), writer(mem2, "B", 25))

    # Store is intact and readable, with all rows present (no corruption/loss).
    async with db1.session() as sconn:
        assert (await sconn.execute(text("PRAGMA integrity_check"))).scalar_one() == "ok"
        count = len((await sconn.execute(select(MemoryEntryORM))).scalars().all())
    assert count == 50

    # The consolidation lock is mutually exclusive across handles: while one is
    # held, a second acquisition times out fast (yields False) rather than racing.
    async with global_memory_lock(s.paths, timeout_s=1.0) as held1:
        assert held1 is True
        async with global_memory_lock(s.paths, timeout_s=0.2) as held2:
            assert held2 is False
    await db1.dispose()
    await db2.dispose()


def get_ws(s):  # noqa: ANN001, ANN201 - tiny helper for the concurrency test
    from omni.storage.db import get_database

    return get_database(s.paths.project_db)
