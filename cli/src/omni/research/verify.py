"""Verification pass — honesty/uncertainty check over the claim/evidence graph.

A deliberately *manual* (``--verify``) check (the user decides when to spend the
extra pass): it does not re-run the model, it audits what was actually recorded.
It flags claims with no supporting evidence, claims that have contradicting
evidence, and over-confident-yet-thin claims — the failure modes that erode a
research user's trust. The result is rendered in the CLI so the user always
understands what verification means and how to act on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from omni.research.citation_support import CitationSupportReport, audit_citation_support
from omni.research.store import ResearchStore
from omni.storage.db import Database
from omni.storage.models import ClaimORM, MemoryEntryORM, RunORM, SourceORM

# memory_type values that assert an empirical fact and therefore *should* be
# anchored to a source/claim/run; an unanchored one is a hallucination risk.
_FINDING_TYPES = ("finding", "research_finding")
_GROUNDING = {"source": SourceORM, "claim": ClaimORM, "run": RunORM}


@dataclass
class VerifyReport:
    total_claims: int = 0
    unsupported: list[ClaimORM] = field(default_factory=list)
    contradicted: list[tuple[ClaimORM, int]] = field(default_factory=list)
    overconfident: list[ClaimORM] = field(default_factory=list)
    supported: int = 0
    # P2.3 — memory-derived empirical claims and whether they're source-anchored.
    memory_total: int = 0
    memory_grounded: int = 0
    memory_unsupported: list[MemoryEntryORM] = field(default_factory=list)
    # Semantic citation support (annotate-only): among structurally-supported
    # claims, how many are actually entailed by their cited evidence text.
    citation_support: CitationSupportReport | None = None
    # Host-recorded inventory so a compute turn is not "empty" just because
    # the model never called record_claim.
    run_count: int = 0
    source_count: int = 0

    @property
    def issues(self) -> int:
        semantic = self.citation_support
        semantic_issues = (
            len(semantic.weak) + len(semantic.unsupported) if semantic is not None else 0
        )
        return (
            len(self.unsupported) + len(self.contradicted) + len(self.overconfident)
            + len(self.memory_unsupported) + semantic_issues
        )

    @property
    def grounding_rate(self) -> float:
        if not self.total_claims:
            return 1.0
        return self.supported / self.total_claims


async def audit_memory_findings(
    db: Database,
) -> tuple[list[MemoryEntryORM], int, int]:
    """Audit empirical memory findings for source anchoring (provenance moat).

    A finding whose ``payload_ref`` resolves to an existing source/claim/run is
    *grounded*; one with no (or a dangling) ref is returned as unsupported so
    ``omni verify`` can surface "remembered facts" that aren't backed by anything.
    Returns ``(unsupported, grounded_count, total)``.
    """
    async with db.session() as s:
        findings = list((await s.execute(
            select(MemoryEntryORM).where(MemoryEntryORM.memory_type.in_(_FINDING_TYPES))
        )).scalars().all())
        unsupported: list[MemoryEntryORM] = []
        grounded = 0
        for f in findings:
            ref = (f.payload_ref or "").strip()
            scheme, _, ident = ref.partition("://")
            model = _GROUNDING.get(scheme)
            ok = False
            if model is not None and ident:
                ok = (await s.execute(
                    select(model.id).where(model.id == ident)
                )).first() is not None
            if ok:
                grounded += 1
            else:
                unsupported.append(f)
    return unsupported, grounded, len(findings)


async def verify_session(
    store: ResearchStore,
    *,
    session_id: str = "",
    overconfident_threshold: float = 0.7,
    audit_memory: bool = True,
    audit_citations: bool = True,
) -> VerifyReport:
    """Audit recorded claims for grounding, contradiction and over-confidence.

    ``session_id`` restricts to the current conversation (default: whole
    workspace). A claim is *unsupported* when no ``supports`` evidence exists,
    *contradicted* when it has ``contradicts`` evidence, and *overconfident* when
    its confidence ≥ threshold but it has no supporting evidence. When
    ``audit_citations`` is set, structurally-supported claims are additionally
    scored for *semantic* support (does the cited evidence text entail the
    claim) — surfaced as annotation, never a hard failure here.
    """
    claims = await store.list_claims(limit=2000, session_id=session_id)
    stance = await store.evidence_stance_counts()
    report = VerifyReport(total_claims=len(claims))
    for c in claims:
        counts = stance.get(c.id, {})
        supports = counts.get("supports", 0)
        contradicts = counts.get("contradicts", 0)
        if supports > 0:
            report.supported += 1
        else:
            report.unsupported.append(c)
            if c.confidence >= overconfident_threshold:
                report.overconfident.append(c)
        if contradicts > 0:
            report.contradicted.append((c, contradicts))
    if audit_memory:
        mem_unsupported, mem_grounded, mem_total = await audit_memory_findings(store.db)
        report.memory_unsupported = mem_unsupported
        report.memory_grounded = mem_grounded
        report.memory_total = mem_total
    if audit_citations:
        report.citation_support = await audit_citation_support(store, claims=claims)
    report.run_count = len(await store.list_runs(limit=2000, session_id=session_id))
    report.source_count = int((await store.counts()).get("sources") or 0)
    return report


__all__ = ["CitationSupportReport", "VerifyReport", "audit_memory_findings", "verify_session"]
