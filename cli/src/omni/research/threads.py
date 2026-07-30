"""Research threads — resume/recall keyed by a hypothesis, across sessions (P2.4).

A *thread* is a research line anchored to a ``HypothesisORM``: its claims, the
experiment runs that tested it, and the conversations that touched it — which may
span many sessions and weeks. ``omni resume --thread <hyp>`` rebuilds this brief
so you can say "continue the Transformer-efficiency line" and pick up with full
context instead of a cold start.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omni.core.timefmt import ensure_aware
from omni.research.store import ResearchStore


def _aware(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.min.replace(tzinfo=UTC)
    return ensure_aware(dt)


async def build_thread_brief(store: ResearchStore, hyp_id: str) -> str | None:
    """Render a cross-session brief for a hypothesis, or ``None`` if not found."""
    hyp = await store.get_hypothesis(hyp_id)
    if hyp is None:
        return None
    claims = [c for c in await store.list_claims(limit=500) if c.hypothesis_id == hyp.id]
    runs = [r for r in await store.list_runs(limit=200) if r.hypothesis_id == hyp.id]
    sessions = sorted(
        {c.session_id for c in claims if c.session_id}
        | {r.session_id for r in runs if r.session_id}
    )
    lines = [
        f"Research thread: {hyp.statement[:200]}",
        f"Status: {hyp.status} · confidence {hyp.confidence:.0%} · {len(sessions)} sessions",
    ]
    if hyp.rationale:
        lines.append(f"Rationale: {hyp.rationale[:200]}")
    if claims:
        lines.append(f"Related claims ({len(claims)}):")
        for c in claims[:8]:
            lines.append(f"- {c.text[:140]} ({c.polarity}, confidence {c.confidence:.0%})")
    if runs:
        lines.append(f"Related experiments ({len(runs)}):")
        for r in runs[:6]:
            outs = ", ".join(r.output_uris[:3]) if r.output_uris else "—"
            lines.append(f"- {r.title or r.id[:8]} [{r.status}] artifacts: {outs}")
    if sessions:
        lines.append("Sessions: " + ", ".join(s[:8] for s in sessions[:8]))
    return "\n".join(lines)


async def latest_thread_session(store: ResearchStore, hyp_id: str) -> str | None:
    """Most recently active session id for a hypothesis (for resume)."""
    hyp = await store.get_hypothesis(hyp_id)
    if hyp is None:
        return None
    claims = [c for c in await store.list_claims(limit=500) if c.hypothesis_id == hyp.id]
    runs = [r for r in await store.list_runs(limit=200) if r.hypothesis_id == hyp.id]
    items: list[tuple[datetime, str]] = []
    for c in claims:
        if c.session_id:
            items.append((_aware(c.created_at), c.session_id))
    for r in runs:
        if r.session_id:
            items.append((_aware(r.created_at), r.session_id))
    if not items:
        return None
    items.sort(reverse=True)
    return items[0][1]


__all__ = ["build_thread_brief", "latest_thread_session"]
