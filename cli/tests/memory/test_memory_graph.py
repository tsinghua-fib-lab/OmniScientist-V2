"""P3 — multi-session memory graph: auto-linking + spreading-activation recall.

Covers:
- auto-link builds ``related`` edges between similar memories, bounded by the
  out-degree cap (``graph_max_edges``);
- ``same_topic`` edges from shared tags;
- spreading activation returns a decayed boost for linked neighbours;
- graph-aware recall surfaces a cross-session neighbour that flat recall ranks
  outside the window;
- principal isolation: an IM peer's identical memory is never linked to, nor
  reached from, the owner's (no cross-peer leak);
- neighbourhood BFS depth; manual ``add_edge`` + dedup;
- feature-disabled is a pure no-op.

All offline/deterministic: memories are recorded with ``llm=None`` so similarity
is keyword-overlap based (the mock/hash embeddings are non-semantic).
"""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.memory.service import MemoryLayer, MemoryService
from omni.storage.db import get_database
from omni.storage.models import MemoryEdgeORM


async def _graph_mem():
    """A MemoryService on keyword-similarity (no LLM) for deterministic linking."""
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return MemoryService(db, s, llm=None), db, s


async def _rec(mem: MemoryService, summary: str, **kw) -> str:
    return await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="project", memory_type="finding",
        summary=summary, **kw,
    )


async def _edges(db) -> list[MemoryEdgeORM]:
    from sqlalchemy import select
    async with db.session() as s:
        return list((await s.execute(select(MemoryEdgeORM))).scalars().all())


# ── auto-linking ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_builds_related_edge_between_similar_memories():
    mem, db, _s = await _graph_mem()
    a = await _rec(mem, "transformer attention scaling law study on language models")
    b = await _rec(mem, "transformer attention scaling law replication on language models")
    edges = await _edges(db)
    assert edges, "a related edge should be auto-built on the second write"
    e = edges[0]
    assert {e.src_id, e.dst_id} == {a, b}
    assert e.relation == "related"
    assert e.origin == "auto"
    assert e.weight >= _s.memory.graph_min_weight


@pytest.mark.asyncio
async def test_unrelated_memory_is_not_linked():
    mem, db, _s = await _graph_mem()
    await _rec(mem, "transformer attention scaling law study")
    await _rec(mem, "sourdough bread fermentation humidity control")
    assert await _edges(db) == [], "lexically unrelated memories must not auto-link"


@pytest.mark.asyncio
async def test_out_degree_capped_by_graph_max_edges():
    mem, db, s = await _graph_mem()
    s.memory.graph_max_edges = 2
    for i in range(5):
        await _rec(mem, f"shared cluster keyword alpha beta gamma delta variant {i}")
    last = await _rec(mem, "shared cluster keyword alpha beta gamma delta variant final")
    out = [e for e in await _edges(db) if e.src_id == last]
    assert len(out) == 2, f"new memory out-degree must be capped at 2, got {len(out)}"


@pytest.mark.asyncio
async def test_same_topic_edge_from_shared_tags():
    mem, db, _s = await _graph_mem()
    # lexically disjoint summaries → no ``related`` edge; only the shared tag links.
    a = await _rec(mem, "quantum error correction surface code threshold", tags=["qec"])
    b = await _rec(mem, "weekly lab logistics and freezer inventory notes", tags=["qec"])
    edges = await _edges(db)
    assert len(edges) == 1
    assert edges[0].relation == "same_topic"
    assert {edges[0].src_id, edges[0].dst_id} == {a, b}


# ── spreading activation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spread_boosts_linked_neighbor_with_decay():
    mem, db, _s = await _graph_mem()
    a = await _rec(mem, "ribosome translation elongation rate measurement")
    b = await _rec(mem, "ribosome translation elongation rate replication")
    (edge,) = await _edges(db)
    boosts = await mem.graph.spread([a], hops=1, decay=0.5)
    assert a not in boosts, "the seed itself is never boosted"
    assert boosts[b] == pytest.approx(edge.weight * 0.5), "boost == edge_weight * decay**hop"


@pytest.mark.asyncio
async def test_recall_surfaces_cross_session_neighbor_only_with_graph():
    mem, _db, s = await _graph_mem()
    x = await _rec(mem, "CRISPR base editing off-target profile", tags=["proj"], importance=0.6)
    y = await _rec(mem, "monday standup and freezer inventory list", tags=["proj"], importance=0.5)
    for phrase in (
        "photosynthesis chlorophyll absorption spectrum",
        "volcanic basalt geochemistry sampling",
        "neutrino oscillation baseline experiment",
        "polymer viscosity temperature dependence",
        "glacier mass balance annual survey",
    ):
        await _rec(mem, phrase, importance=0.6)
    query = "CRISPR base editing off-target"

    s.memory.graph_enabled = False
    off = {m.entry.id for m in await mem.recall(query, limit=3)}
    s.memory.graph_enabled = True
    on = {m.entry.id for m in await mem.recall(query, limit=3)}

    assert x in off and x in on, "the query-matching memory is always retrieved"
    assert y not in off, "flat recall ranks the tag-linked note outside the window"
    assert y in on, "graph recall surfaces the linked cross-session note"


# ── principal isolation ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_is_principal_isolated():
    mem, db, _s = await _graph_mem()
    owner = await _rec(mem, "dark matter halo concentration mass relation")
    owner2 = await _rec(mem, "dark matter halo concentration mass relation refit")
    peer = await _rec(
        mem, "dark matter halo concentration mass relation refit", principal="feishu:peer"
    )
    # No auto edge ever crosses principals.
    for e in await _edges(db):
        assert peer not in (e.src_id, e.dst_id), "peer memory must not be auto-linked to owner's"

    owner_neigh = {n.id for n in await mem.graph.neighbors(owner, principal="local")}
    assert owner2 in owner_neigh, "same-principal linking still works"
    assert peer not in owner_neigh, "owner traversal never reaches the peer's memory"

    boosts = await mem.graph.spread([owner], principal="local")
    assert peer not in boosts, "spreading activation never crosses to the peer"


# ── traversal + manual edges ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_neighbors_respects_depth():
    mem, _db, _s = await _graph_mem()
    a = await _rec(mem, "alpha topic one")
    b = await _rec(mem, "beta topic two")
    c = await _rec(mem, "gamma topic three")
    await mem.graph.add_edge(a, b, relation="derived_from")
    await mem.graph.add_edge(b, c, relation="derived_from")

    one = {n.id for n in await mem.graph.neighbors(a, depth=1)}
    assert one == {b}, "depth=1 reaches only the direct neighbour"
    two = {n.id for n in await mem.graph.neighbors(a, depth=2)}
    assert two == {b, c}, "depth=2 reaches the two-hop neighbour"


@pytest.mark.asyncio
async def test_add_edge_manual_and_dedup():
    mem, db, _s = await _graph_mem()
    a = await _rec(mem, "alpha isolated note")
    b = await _rec(mem, "beta isolated note distinct")
    eid = await mem.graph.add_edge(a, b, relation="contradicts", weight=0.9)
    assert eid
    again = await mem.graph.add_edge(a, b, relation="contradicts", weight=0.9)
    assert again == eid, "identical (src,dst,relation) is deduped, not duplicated"
    assert await mem.graph.add_edge(a, a) is None, "no self-edges"
    assert await mem.graph.add_edge(a, "does-not-exist") is None, "missing endpoint → no edge"
    manual = [e for e in await _edges(db) if e.origin == "manual"]
    assert len(manual) == 1


# ── feature flag ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_disabled_is_noop():
    mem, db, s = await _graph_mem()
    s.memory.graph_enabled = False
    a = await _rec(mem, "identical topic sentence for linking test")
    await _rec(mem, "identical topic sentence for linking test again")
    assert await _edges(db) == [], "no edges are built when the graph is disabled"
    assert await mem.graph.spread([a]) == {}, "spread is a no-op when disabled"
    # recall still works (flat)
    assert await mem.recall("identical topic", limit=5), "recall degrades to flat, still functional"
