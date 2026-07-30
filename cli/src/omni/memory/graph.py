"""Multi-session memory graph: auto-linking + spreading-activation recall (P3).

Where :class:`~omni.storage.models.MemoryEntryORM` rows are nodes, this module
manages the :class:`~omni.storage.models.MemoryEdgeORM` links between them and the
traversal that makes recall *cross-session*:

* :meth:`MemoryGraph.link_new_memory` — on write, connect a fresh memory to the
  semantically nearest existing memories (``related``) and to memories sharing a
  tag (``same_topic``). Bounded: candidate scanning reuses
  ``memory.recall_candidate_limit`` and out-degree is capped by
  ``memory.graph_max_edges`` so the graph stays sparse.
* :meth:`MemoryGraph.spread` — from the top recall hits, walk one (or a few) hops
  and return a per-memory *boost*, so retrieving one hit surfaces its neighbours
  even when they never co-occurred in a session and would otherwise fall outside
  the flat candidate window.
* :meth:`MemoryGraph.neighbors` — BFS neighbourhood for the ``omni memory graph``
  view; :meth:`MemoryGraph.add_edge` — manual ``omni memory link``.

Every traversal is filtered by visible ``principal`` (owner + caller) exactly
like recall, so one ``omni serve`` daemon never walks an edge from one IM peer's
memory into another's. Pure store I/O — no network, no LLM — so it is offline and
deterministic for tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select

from omni.memory.vectors import cosine
from omni.storage.models import MemoryEdgeORM, MemoryEntryORM, _uuid

# Layers whose memories are worth graphing (durable, cross-session knowledge).
# Mirrors ``service._CROSS_SESSION_LAYERS`` (M3/M4/M5) without importing it at
# module load (service imports this module).
_GRAPHABLE_LAYERS = {"M3", "M4", "M5"}


@dataclass
class MemoryNeighbor:
    """One memory reached during graph traversal, with hop distance + edge info."""

    id: str
    relation: str
    weight: float
    depth: int
    summary: str
    layer: str
    memory_type: str
    scope: str
    scope_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "relation": self.relation, "weight": round(self.weight, 4),
            "depth": self.depth, "summary": self.summary, "layer": self.layer,
            "memory_type": self.memory_type, "scope": self.scope, "scope_id": self.scope_id,
        }


class MemoryGraph:
    """Edge builder + traversal over the memory graph (see module docstring)."""

    def __init__(self, db: Any, settings: Any) -> None:
        self._db = db
        self._settings = settings

    # ── config helpers ────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings.memory, "graph_enabled", True))

    def _cap(self) -> int:
        return int(getattr(self._settings.memory, "recall_candidate_limit", 200) or 200)

    @staticmethod
    def _visible(entry_principal: str, principal: str) -> bool:
        """Own memory + the shared owner baseline; never another peer's."""
        ep = entry_principal or "local"
        return ep == principal or ep == "local"

    @staticmethod
    def _pair_similarity(node: MemoryEntryORM, other: MemoryEntryORM) -> float:
        """Relatedness of two memories: max(embedding cosine, keyword overlap).

        Keyword overlap is the floor so strongly lexically-overlapping memories
        link even when embeddings are absent or non-semantic (offline/mock use);
        real embeddings add the semantic dimension on top (paraphrases with low
        lexical overlap still link). Symmetric via the max over both directions
        of the (asymmetric) overlap.
        """
        from omni.memory.service import _keyword_overlap

        kw = max(
            _keyword_overlap(node.summary, other.summary),
            _keyword_overlap(other.summary, node.summary),
        )
        if node.embedding and other.embedding:
            return max(cosine(node.embedding, other.embedding), kw)
        return kw

    # ── write path: auto-link a fresh memory ──────────────────────────────

    async def link_new_memory(
        self, mem_id: str, *, max_edges: int | None = None, min_weight: float | None = None
    ) -> list[str]:
        """Link ``mem_id`` to its nearest existing neighbours; return new edge ids.

        Only links to same-principal candidates (no cross-principal edges ever),
        scanning at most ``recall_candidate_limit`` recent rows. Builds up to
        ``graph_max_edges`` ``related`` edges above ``graph_min_weight``, then a
        few ``same_topic`` edges from shared tags. Idempotent enough for repeated
        writes: a fresh node has no prior edges, and topic edges skip nodes
        already linked as related.
        """
        mem = self._settings.memory
        if not self.enabled:
            return []
        max_edges = int(max_edges if max_edges is not None else getattr(mem, "graph_max_edges", 5))
        if max_edges <= 0:
            return []
        min_weight = float(min_weight if min_weight is not None else getattr(mem, "graph_min_weight", 0.6))
        created: list[str] = []
        async with self._db.session() as s:
            node = await s.get(MemoryEntryORM, mem_id)
            if node is None or node.layer not in _GRAPHABLE_LAYERS:
                return []  # only durable, cross-session memories are graphed
            principal = node.principal or "local"
            candidates = list((await s.execute(
                select(MemoryEntryORM)
                .where(MemoryEntryORM.principal == principal)
                .where(MemoryEntryORM.id != mem_id)
                .order_by(MemoryEntryORM.created_at.desc())
                .limit(self._cap())
            )).scalars().all())
            scored = sorted(
                ((c, self._pair_similarity(node, c)) for c in candidates),
                key=lambda t: t[1], reverse=True,
            )
            linked: set[str] = set()
            for cand, sim in scored:
                if len(created) >= max_edges or sim < min_weight:
                    break  # list is sorted desc → nothing further qualifies
                created.append(self._new_edge(s, mem_id, cand.id, principal, "related", sim))
                linked.add(cand.id)
            remaining = max_edges - len(created)  # same_topic fills the leftover budget
            if remaining > 0:
                created += self._link_shared_topics(s, node, scored, principal, linked, remaining)
            await s.commit()
        return created

    def _link_shared_topics(
        self, s: Any, node: MemoryEntryORM, scored: list[tuple[MemoryEntryORM, float]],
        principal: str, linked: set[str], max_edges: int,
    ) -> list[str]:
        """Add ``same_topic`` edges to tag-sharing candidates not already linked."""
        node_tags = {str(t).lower() for t in (node.tags or []) if str(t).strip()}
        if not node_tags:
            return []
        out: list[str] = []
        for cand, _sim in scored:
            if len(out) >= max_edges:
                break
            if cand.id in linked:
                continue
            cand_tags = {str(t).lower() for t in (cand.tags or []) if str(t).strip()}
            shared = node_tags & cand_tags
            if not shared:
                continue
            jaccard = len(shared) / len(node_tags | cand_tags)
            out.append(self._new_edge(s, node.id, cand.id, principal, "same_topic", min(1.0, 0.5 + jaccard)))
            linked.add(cand.id)
        return out

    @staticmethod
    def _new_edge(
        s: Any, src_id: str, dst_id: str, principal: str, relation: str, weight: float
    ) -> str:
        eid = _uuid()
        s.add(MemoryEdgeORM(
            id=eid, src_id=src_id, dst_id=dst_id, principal=principal,
            relation=relation, weight=round(float(weight), 4), origin="auto",
        ))
        return eid

    async def add_edge(
        self, src_id: str, dst_id: str, *, relation: str = "related",
        weight: float = 1.0, origin: str = "manual",
    ) -> str | None:
        """Create a manual edge between two existing memories (dedup by triple)."""
        if not src_id or not dst_id or src_id == dst_id:
            return None
        async with self._db.session() as s:
            src = await s.get(MemoryEntryORM, src_id)
            dst = await s.get(MemoryEntryORM, dst_id)
            if src is None or dst is None:
                return None
            dup = (await s.execute(
                select(MemoryEdgeORM).where(
                    MemoryEdgeORM.src_id == src_id,
                    MemoryEdgeORM.dst_id == dst_id,
                    MemoryEdgeORM.relation == relation,
                ).limit(1)
            )).scalar_one_or_none()
            if dup is not None:
                return dup.id
            eid = _uuid()
            s.add(MemoryEdgeORM(
                id=eid, src_id=src_id, dst_id=dst_id, principal=src.principal or "local",
                relation=relation, weight=float(weight), origin=origin,
            ))
            await s.commit()
        return eid

    # ── read path: traversal + spreading activation ───────────────────────

    async def neighbors(
        self, mem_id: str, *, depth: int = 1, limit: int = 50, principal: str = "local"
    ) -> list[MemoryNeighbor]:
        """BFS the (undirected) neighbourhood of ``mem_id`` up to ``depth`` hops.

        Principal-filtered on both the edge and the reached node, so a traversal
        can never surface another peer's memory. ``limit`` bounds the node count.
        """
        principal = principal or "local"
        out: list[MemoryNeighbor] = []
        seen = {mem_id}
        frontier: list[tuple[str, int]] = [(mem_id, 0)]
        max_depth = max(1, int(depth))
        async with self._db.session() as s:
            while frontier and len(out) < limit:
                cur, d = frontier.pop(0)
                if d >= max_depth:
                    continue
                edges = (await s.execute(
                    select(MemoryEdgeORM).where(
                        or_(MemoryEdgeORM.src_id == cur, MemoryEdgeORM.dst_id == cur)
                    )
                )).scalars().all()
                for e in edges:
                    if not self._visible(e.principal, principal):
                        continue
                    other = e.dst_id if e.src_id == cur else e.src_id
                    if other in seen:
                        continue
                    node = await s.get(MemoryEntryORM, other)
                    if node is None or not self._visible(node.principal, principal):
                        continue
                    seen.add(other)
                    out.append(MemoryNeighbor(
                        id=other, relation=e.relation, weight=float(e.weight), depth=d + 1,
                        summary=node.summary, layer=node.layer, memory_type=node.memory_type,
                        scope=node.scope, scope_id=node.scope_id,
                    ))
                    if len(out) >= limit:
                        break
                    if d + 1 < max_depth:
                        frontier.append((other, d + 1))
        return out

    async def spread(
        self, seed_ids: list[str], *, hops: int | None = None,
        decay: float | None = None, principal: str = "local",
    ) -> dict[str, float]:
        """Spreading activation from ``seed_ids`` → ``{memory_id: boost}``.

        A node discovered ``hop`` steps out gets ``boost = edge_weight *
        decay**hop`` (max across paths). Seeds are excluded (they already scored
        in flat recall). Principal-filtered so boosts never cross peers.
        """
        mem = self._settings.memory
        if not self.enabled or not seed_ids:
            return {}
        hops = int(hops if hops is not None else getattr(mem, "graph_spread_hops", 1))
        decay = float(decay if decay is not None else getattr(mem, "graph_spread_decay", 0.5))
        if hops <= 0:
            return {}
        principal = principal or "local"
        boosts: dict[str, float] = {}
        visited = set(seed_ids)
        frontier = set(seed_ids)
        async with self._db.session() as s:
            for hop in range(1, hops + 1):
                if not frontier:
                    break
                edges = (await s.execute(
                    select(MemoryEdgeORM).where(
                        or_(
                            MemoryEdgeORM.src_id.in_(frontier),
                            MemoryEdgeORM.dst_id.in_(frontier),
                        )
                    )
                )).scalars().all()
                nxt: set[str] = set()
                for e in edges:
                    if not self._visible(e.principal, principal):
                        continue
                    for near, far in ((e.src_id, e.dst_id), (e.dst_id, e.src_id)):
                        if near in frontier and far not in visited:
                            boost = float(e.weight) * (decay ** hop)
                            boosts[far] = max(boosts.get(far, 0.0), boost)
                            nxt.add(far)
                visited |= nxt
                frontier = nxt
        return boosts

    async def load_visible(
        self, ids: list[str], *, principal: str = "local", limit: int | None = None
    ) -> list[MemoryEntryORM]:
        """Load memory rows by id, keeping only principal-visible ones (bounded)."""
        if not ids:
            return []
        cap = int(limit if limit is not None else self._cap())
        principal = principal or "local"
        async with self._db.session() as s:
            rows = list((await s.execute(
                select(MemoryEntryORM).where(MemoryEntryORM.id.in_(list(ids)[:cap]))
            )).scalars().all())
        return [r for r in rows if self._visible(r.principal, principal)]


__all__ = ["MemoryGraph", "MemoryNeighbor"]
