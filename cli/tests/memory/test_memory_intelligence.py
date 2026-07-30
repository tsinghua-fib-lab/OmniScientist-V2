"""P2 — memory intelligence: cross-channel identity, citation & cross-store graph.

Acceptance for the intelligence layer:
- ``channel_identity=owner`` (default): what the owner tells the bot on an IM
  channel (e.g. Feishu) is recalled from the CLI next time — identity follows the
  owner across channels;
- ``channel_identity=per_peer``: strict isolation, zero cross-talk — a peer's
  memory never surfaces for the owner or for another peer;
- citation-aware ranking: an equally-similar memory that is *grounded* in a real
  source/claim/run/artifact outranks an ungrounded recollection;
- the memory graph spreads across *both* backing stores, so a global preference's
  neighbour is surfaced by recall, not just workspace-local edges.

All offline: the mock/``None`` provider path (keyword recall) is used throughout.
"""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.memory.service import (
    PRINCIPAL_OWNER,
    MemoryLayer,
    MemoryService,
    open_global_store,
    principal_of,
)
from omni.storage.db import get_database


async def _memory_for(
    project: str, *, channel_identity: str = "owner"
) -> tuple[MemoryService, object]:
    """Build a MemoryService for ``project`` with a chosen ``channel_identity``.

    Named projects (``-P``) share one ``home`` (hence one global store) but keep
    separate workspace dbs, modelling two workspaces / a serve daemon on one
    machine. Returns ``(service, settings)``.
    """
    s = load_settings(project=project, overrides={"memory": {"channel_identity": channel_identity}})
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    gdb = open_global_store(s)
    if gdb is not None:
        await gdb.init()
    return MemoryService(db, s, llm=None, global_db=gdb), s


@pytest.mark.asyncio
async def test_owner_feishu_preference_recalled_in_cli() -> None:
    """owner mode: a preference stated on Feishu is recalled from the CLI."""
    mem, s = await _memory_for("proj_owner", channel_identity="owner")

    # An authorized Feishu message maps to the owner principal (pairing → owner).
    feishu_principal = principal_of("feishu", "u-1001", channel_identity=s.memory.channel_identity)
    assert feishu_principal == PRINCIPAL_OWNER
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="user prefers answers in metric units", memory_type="preference",
        importance=0.8, principal=feishu_principal,
    )

    # Next CLI turn (owner principal) recalls what was said on Feishu.
    cli_principal = principal_of("cli", "", channel_identity=s.memory.channel_identity)
    res = await mem.recall("what units do I prefer", principal=cli_principal, cross_session=True)
    assert any("metric units" in sm.entry.summary for sm in res)


@pytest.mark.asyncio
async def test_per_peer_mode_has_zero_crosstalk() -> None:
    """per_peer mode: peers are isolated from the owner and from each other."""
    mem, s = await _memory_for("proj_peer", channel_identity="per_peer")

    peer1 = principal_of("feishu", "u-1", channel_identity=s.memory.channel_identity)
    peer2 = principal_of("feishu", "u-2", channel_identity=s.memory.channel_identity)
    assert peer1 == "feishu:u-1" and peer2 == "feishu:u-2"
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="feishu:u-1",
        summary="peer one prefers verbose proofs", memory_type="preference",
        importance=0.8, principal=peer1,
    )

    # The owner (CLI) must not see the peer's preference …
    owner = principal_of("cli", "", channel_identity=s.memory.channel_identity)
    res_owner = await mem.recall("verbose proofs", principal=owner, cross_session=True)
    assert not any("verbose proofs" in sm.entry.summary for sm in res_owner)
    # … nor may another peer …
    res_peer2 = await mem.recall("verbose proofs", principal=peer2, cross_session=True)
    assert not any("verbose proofs" in sm.entry.summary for sm in res_peer2)
    # … while the owning peer still recalls their own.
    res_peer1 = await mem.recall("verbose proofs", principal=peer1, cross_session=True)
    assert any("verbose proofs" in sm.entry.summary for sm in res_peer1)


@pytest.mark.asyncio
async def test_citation_ranking_prefers_grounded_memory() -> None:
    """A grounded (source-anchored) memory outranks an equally-similar ungrounded one."""
    mem, _s = await _memory_for("proj_cite", channel_identity="owner")
    summary = "transformer attention scales quadratically with sequence length"
    # Ungrounded recollection …
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="project", scope_id="p",
        summary=summary, memory_type="finding", importance=0.6,
    )
    # … and an identical claim anchored to a real source (the provenance moat).
    grounded_id = await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="project", scope_id="p",
        summary=summary, memory_type="finding", importance=0.6,
        payload_ref="source://src-123",
    )

    res = await mem.recall("attention quadratic sequence length", cross_session=True, limit=2)
    assert res, "both findings must be recalled"
    assert res[0].entry.id == grounded_id, "the source-anchored memory must rank first"


@pytest.mark.asyncio
async def test_cross_store_graph_spread_surfaces_global_neighbor() -> None:
    """Recall spreads over the *global* store's graph, not only the workspace one."""
    mem, _s = await _memory_for("proj_graph", channel_identity="owner")
    # Two related owner preferences (shared tag) → auto-linked in the global graph.
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="user works primarily on protein folding kinetics", memory_type="preference",
        importance=0.8, tags=["protein-folding"],
    )
    neighbor_id = await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="prefers AlphaFold over Rosetta for structure prediction",
        memory_type="preference", importance=0.8, tags=["protein-folding"],
    )

    # A query that hits the first preference should surface its graph neighbour
    # even though the query text does not mention AlphaFold/Rosetta.
    res = await mem.recall("protein folding kinetics", cross_session=True, limit=5)
    assert any(sm.entry.id == neighbor_id for sm in res), (
        "global-store graph neighbour must be surfaced by cross-store spread"
    )
