"""Citation-graph traversal over the local bibliographic edge store.

The corpus records directed ``citing → cited`` edges (:class:`CitationORM`).
This module walks them breadth-first in either direction so the agent can ask
"what does this paper build on?" (``references``) or "who built on this paper?"
(``cited_by``) and expand a seed set of papers into its citation neighbourhood —
the structural backbone of a literature review.

Traversal is pure store I/O (no network, no LLM), so it works fully offline over
whatever edges have been ingested, and is deterministic for tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from omni.research.store import ResearchStore

Direction = Literal["references", "cited_by"]


@dataclass
class CitationNode:
    """One node reached during traversal, with the hop distance from the seed."""

    key: str            # dedup key of the work (stable identity)
    source_id: str      # resolved corpus source id, or "" if not ingested
    title: str
    doi: str
    year: str
    depth: int          # hops from the seed source (1 = direct neighbour)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "source_id": self.source_id, "title": self.title,
            "doi": self.doi, "year": self.year, "depth": self.depth,
        }


@dataclass
class CitationNeighborhood:
    seed_source_id: str
    direction: Direction
    nodes: list[CitationNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed_source_id": self.seed_source_id,
            "direction": self.direction,
            "count": len(self.nodes),
            "nodes": [n.to_dict() for n in self.nodes],
        }


async def _neighbors(
    store: ResearchStore, source_id: str, direction: Direction
) -> list[tuple[str, str, str, str, str]]:
    """Return ``(key, source_id, title, doi, year)`` tuples one hop out."""
    if direction == "references":
        edges = await store.references_of(source_id)
        return [
            (e.cited_key, e.cited_source_id, e.cited_title, e.cited_doi, e.cited_year)
            for e in edges
        ]
    edges = await store.cited_by(source_id)
    out: list[tuple[str, str, str, str, str]] = []
    for e in edges:
        citing = await store.get_source(e.citing_source_id)
        if citing is None:
            continue
        out.append(
            (citing.dedup_key, citing.id, citing.title, citing.doi, citing.year)
        )
    return out


async def traverse(
    store: ResearchStore,
    source_id: str,
    *,
    direction: Direction = "references",
    depth: int = 1,
    limit: int = 50,
) -> CitationNeighborhood:
    """BFS the citation graph from ``source_id`` up to ``depth`` hops.

    Only edges that resolve to an ingested corpus source can be expanded beyond
    the first hop (an un-ingested cited work has no outgoing edges to follow);
    such leaves are still reported at their depth. ``limit`` bounds the number of
    distinct nodes returned so a hairball never blows up the context.
    """
    hood = CitationNeighborhood(seed_source_id=source_id, direction=direction)
    if not source_id:
        return hood
    max_depth = max(1, int(depth))
    seen: set[str] = set()
    # Frontier holds (source_id_to_expand, current_depth). We expand a node only
    # when it is an ingested source and we have depth budget left.
    frontier: list[tuple[str, int]] = [(source_id, 0)]
    while frontier and len(hood.nodes) < limit:
        current_id, cur_depth = frontier.pop(0)
        for key, sid, title, doi, year in await _neighbors(store, current_id, direction):
            marker = sid or key
            if not marker or marker in seen:
                continue
            seen.add(marker)
            node = CitationNode(
                key=key, source_id=sid, title=title, doi=doi, year=year,
                depth=cur_depth + 1,
            )
            hood.nodes.append(node)
            if len(hood.nodes) >= limit:
                break
            if sid and cur_depth + 1 < max_depth:
                frontier.append((sid, cur_depth + 1))
    return hood


__all__ = ["Direction", "CitationNode", "CitationNeighborhood", "traverse"]
