"""ResearchStore — CRUD over the Research Object Model (ROM).

A thin async data-access layer over the ROM tables (sources, source_chunks,
hypotheses, claims, evidence, experiment_runs). It is deliberately free of LLM
or embedding logic (that lives in :mod:`omni.research.corpus`) so it is trivial
to test offline and reuse from CLI commands, builtin tools, and skill engines.

The store binds to the same per-workspace ``Database`` the rest of the agent
uses, so research objects live beside sessions/memory/artifacts in one file and
share its WAL-mode concurrency. Rows reference sessions/tasks by id without SQL
foreign keys (mirroring ``ConversationMessageORM``), so nothing cascades.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from omni.storage.db import Database
from omni.storage.models import (
    ChunkORM,
    CitationORM,
    ClaimORM,
    EvidenceORM,
    HypothesisORM,
    RunORM,
    SourceORM,
    _utcnow,
)

_WS_RE = re.compile(r"\s+")

HYPOTHESIS_STATUSES = ("proposed", "testing", "supported", "refuted", "inconclusive")
EVIDENCE_STANCES = ("supports", "contradicts", "mentions")


def _norm_title(title: str) -> str:
    return _WS_RE.sub(" ", (title or "").strip().lower())


def source_dedup_key(meta: dict[str, Any]) -> str:
    """Stable identity for a source: arXiv id → DOI → URL → normalised title."""
    for field in ("arxiv_id", "doi"):
        val = str(meta.get(field) or "").strip().lower()
        if val:
            return f"{field}:{val}"
    url = str(meta.get("url") or meta.get("abs_url") or "").strip().lower()
    if url:
        return f"url:{url}"
    return "title:" + _norm_title(str(meta.get("title") or ""))


def _coerce_authors(value: Any) -> list[str]:
    if isinstance(value, str):
        return [a.strip() for a in value.split(",") if a.strip()]
    if isinstance(value, list):
        return [str(a) for a in value]
    return []


def _year_of(meta: dict[str, Any]) -> str:
    year = str(meta.get("year") or "").strip()
    if year:
        return year[:8]
    published = str(meta.get("published") or meta.get("updated") or "")
    m = re.search(r"(\d{4})", published)
    return m.group(1) if m else ""


class ResearchStore:
    """Async CRUD for the Research Object Model."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        """The underlying per-workspace database (shared with memory/sessions)."""
        return self._db

    # ── sources ───────────────────────────────────────────────────────────
    async def add_source(
        self,
        meta: dict[str, Any],
        *,
        origin: str = "manual",
        date_pin: str = "",
    ) -> SourceORM:
        """Insert (or return existing, deduped) a source row."""
        key = source_dedup_key(meta)
        async with self._db.session() as s:
            existing = (
                await s.execute(select(SourceORM).where(SourceORM.dedup_key == key))
            ).scalars().first()
            if existing is not None:
                return existing
            title = _WS_RE.sub(" ", str(meta.get("title") or "").strip())
            row = SourceORM(
                kind=str(meta.get("kind") or "paper"),
                arxiv_id=str(meta.get("arxiv_id") or "").strip(),
                doi=str(meta.get("doi") or "").strip(),
                url=str(meta.get("url") or meta.get("abs_url") or meta.get("pdf_url") or "").strip(),
                title=title,
                authors=_coerce_authors(meta.get("authors")),
                year=_year_of(meta),
                venue=str(meta.get("venue") or "").strip(),
                summary=_WS_RE.sub(" ", str(meta.get("summary") or "").strip()),
                origin=origin,
                dedup_key=key,
                content_hash=hashlib.sha256((title + key).encode("utf-8")).hexdigest()[:16],
                date_pin=date_pin or "",
                meta={k: v for k, v in meta.items() if k not in {"authors"}},
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row

    async def get_source(self, source_id: str) -> SourceORM | None:
        return await self._get_by_id_or_prefix(SourceORM, source_id)

    async def find_source(self, meta: dict[str, Any]) -> SourceORM | None:
        key = source_dedup_key(meta)
        async with self._db.session() as s:
            return (
                await s.execute(select(SourceORM).where(SourceORM.dedup_key == key))
            ).scalars().first()

    async def list_sources(self, *, limit: int = 50) -> list[SourceORM]:
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(SourceORM).order_by(SourceORM.created_at.desc()).limit(limit)
                )
            ).scalars().all()
        return list(rows)

    # ── chunks ────────────────────────────────────────────────────────────
    async def add_chunks(self, source_id: str, chunks: list[dict[str, Any]]) -> int:
        if not chunks:
            return 0
        async with self._db.session() as s:
            for i, ch in enumerate(chunks):
                s.add(
                    ChunkORM(
                        source_id=source_id,
                        ord=int(ch.get("ord", i)),
                        section=str(ch.get("section") or ""),
                        page=ch.get("page"),
                        text=str(ch.get("text") or ""),
                        embedding=list(ch.get("embedding") or []),
                        tokens=int(ch.get("tokens", 0)),
                    )
                )
            await s.commit()
        return len(chunks)

    async def chunks_for_source(self, source_id: str) -> list[ChunkORM]:
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(ChunkORM)
                    .where(ChunkORM.source_id == source_id)
                    .order_by(ChunkORM.ord.asc())
                )
            ).scalars().all()
        return list(rows)

    async def all_chunks(self) -> list[ChunkORM]:
        async with self._db.session() as s:
            rows = (await s.execute(select(ChunkORM))).scalars().all()
        return list(rows)

    async def chunk_count(self) -> int:
        async with self._db.session() as s:
            return int((await s.execute(select(func.count()).select_from(ChunkORM))).scalar() or 0)

    # ── citations (bibliographic reference graph) ────────────────────────────
    async def add_citation(
        self,
        citing_source_id: str,
        cited_meta: dict[str, Any],
        *,
        origin: str = "manual",
    ) -> CitationORM | None:
        """Record a ``citing → cited`` edge (deduped per citing/cited pair).

        ``cited_meta`` is a paper-dict; its stable dedup key becomes the edge's
        cited endpoint. If that work is already an ingested source, the edge is
        resolved to its ``cited_source_id`` too. Returns the (new or existing)
        edge, or ``None`` when the citing source id or cited identity is empty.
        """
        citing_source_id = (citing_source_id or "").strip()
        cited_key = source_dedup_key(cited_meta)
        if not citing_source_id or cited_key in ("title:", ""):
            return None
        async with self._db.session() as s:
            existing = (
                await s.execute(
                    select(CitationORM).where(
                        CitationORM.citing_source_id == citing_source_id,
                        CitationORM.cited_key == cited_key,
                    )
                )
            ).scalars().first()
            if existing is not None:
                return existing
            citing = (
                await s.execute(select(SourceORM).where(SourceORM.id == citing_source_id))
            ).scalar_one_or_none()
            cited = (
                await s.execute(select(SourceORM).where(SourceORM.dedup_key == cited_key))
            ).scalars().first()
            row = CitationORM(
                citing_source_id=citing_source_id,
                citing_key=getattr(citing, "dedup_key", "") or "",
                cited_source_id=getattr(cited, "id", "") or "",
                cited_key=cited_key,
                cited_title=_WS_RE.sub(" ", str(cited_meta.get("title") or "").strip()),
                cited_doi=str(cited_meta.get("doi") or "").strip().lower(),
                cited_year=_year_of(cited_meta),
                origin=origin,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row

    async def add_citations(
        self, citing_source_id: str, cited_list: list[dict[str, Any]], *, origin: str = "manual"
    ) -> int:
        """Record many outgoing edges for one citing source; return count added."""
        added = 0
        for cited in cited_list or []:
            row = await self.add_citation(citing_source_id, cited, origin=origin)
            if row is not None:
                added += 1
        return added

    async def references_of(self, source_id: str) -> list[CitationORM]:
        """Outgoing edges — the works ``source_id`` cites."""
        source_id = (source_id or "").strip()
        if not source_id:
            return []
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(CitationORM)
                    .where(CitationORM.citing_source_id == source_id)
                    .order_by(CitationORM.created_at.asc())
                )
            ).scalars().all()
        return list(rows)

    async def cited_by(self, source_id: str) -> list[CitationORM]:
        """Incoming edges — the works that cite ``source_id``.

        Matches on the resolved ``cited_source_id`` and, for edges recorded
        before the cited work was ingested, on its ``cited_key`` too.
        """
        src = await self.get_source(source_id)
        if src is None:
            return []
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(CitationORM)
                    .where(
                        (CitationORM.cited_source_id == src.id)
                        | (CitationORM.cited_key == src.dedup_key)
                    )
                    .order_by(CitationORM.created_at.asc())
                )
            ).scalars().all()
        return list(rows)

    async def resolve_pending_citations(self, source_id: str) -> int:
        """Fill ``cited_source_id`` on edges that pointed at a now-ingested work.

        Call after ingesting a source so earlier "cite-before-ingest" edges snap
        onto it, keeping cited-by traversal complete. Returns rows updated.
        """
        src = await self.get_source(source_id)
        if src is None or not src.dedup_key:
            return 0
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(CitationORM).where(
                        CitationORM.cited_key == src.dedup_key,
                        CitationORM.cited_source_id == "",
                    )
                )
            ).scalars().all()
            for row in rows:
                row.cited_source_id = src.id
            if rows:
                await s.commit()
        return len(rows)

    # ── hypotheses ──────────────────────────────────────────────────────────
    async def add_hypothesis(
        self,
        statement: str,
        *,
        session_id: str = "",
        rationale: str = "",
        status: str = "proposed",
        confidence: float = 0.5,
        tags: list[str] | None = None,
    ) -> HypothesisORM:
        async with self._db.session() as s:
            row = HypothesisORM(
                session_id=session_id,
                statement=statement.strip(),
                rationale=rationale.strip(),
                status=status if status in HYPOTHESIS_STATUSES else "proposed",
                confidence=_clamp(confidence),
                tags=list(tags or []),
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row

    async def get_hypothesis(self, hyp_id: str) -> HypothesisORM | None:
        return await self._get_by_id_or_prefix(HypothesisORM, hyp_id)

    async def list_hypotheses(self, *, limit: int = 50, status: str = "") -> list[HypothesisORM]:
        async with self._db.session() as s:
            q = select(HypothesisORM).order_by(HypothesisORM.updated_at.desc())
            if status:
                q = q.where(HypothesisORM.status == status)
            rows = (await s.execute(q.limit(limit))).scalars().all()
        return list(rows)

    async def set_hypothesis_status(
        self, hyp_id: str, status: str, *, confidence: float | None = None
    ) -> HypothesisORM | None:
        row = await self.get_hypothesis(hyp_id)
        if row is None:
            return None
        if status not in HYPOTHESIS_STATUSES:
            return row
        async with self._db.session() as s:
            obj = (
                await s.execute(select(HypothesisORM).where(HypothesisORM.id == row.id))
            ).scalar_one()
            obj.status = status
            if confidence is not None:
                obj.confidence = _clamp(confidence)
            obj.updated_at = _utcnow()
            await s.commit()
            await s.refresh(obj)
            return obj

    # ── claims ──────────────────────────────────────────────────────────────
    async def add_claim(
        self,
        text: str,
        *,
        session_id: str = "",
        hypothesis_id: str = "",
        polarity: str = "assert",
        confidence: float = 0.5,
        made_by: str = "agent",
    ) -> ClaimORM:
        async with self._db.session() as s:
            row = ClaimORM(
                session_id=session_id,
                hypothesis_id=hypothesis_id,
                text=text.strip(),
                polarity=polarity,
                confidence=_clamp(confidence),
                made_by=made_by,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row

    async def get_claim(self, claim_id: str) -> ClaimORM | None:
        return await self._get_by_id_or_prefix(ClaimORM, claim_id)

    async def list_claims(
        self, *, limit: int = 50, session_id: str = "", hypothesis_id: str = ""
    ) -> list[ClaimORM]:
        async with self._db.session() as s:
            q = select(ClaimORM).order_by(ClaimORM.created_at.desc())
            if session_id:
                q = q.where(ClaimORM.session_id == session_id)
            if hypothesis_id:
                q = q.where(ClaimORM.hypothesis_id == hypothesis_id)
            rows = (await s.execute(q.limit(limit))).scalars().all()
        return list(rows)

    # ── evidence ──────────────────────────────────────────────────────────
    async def add_evidence(
        self,
        claim_id: str,
        *,
        source_id: str = "",
        chunk_id: str = "",
        stance: str = "supports",
        quote: str = "",
        locator: str = "",
        strength: float = 0.5,
    ) -> EvidenceORM:
        async with self._db.session() as s:
            row = EvidenceORM(
                claim_id=claim_id,
                source_id=source_id,
                chunk_id=chunk_id,
                stance=stance if stance in EVIDENCE_STANCES else "supports",
                quote=quote.strip(),
                locator=locator.strip(),
                strength=_clamp(strength),
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row

    async def evidence_for_claim(self, claim_id: str) -> list[EvidenceORM]:
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(EvidenceORM)
                    .where(EvidenceORM.claim_id == claim_id)
                    .order_by(EvidenceORM.created_at.asc())
                )
            ).scalars().all()
        return list(rows)

    async def evidence_count_by_claim(self) -> dict[str, int]:
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(EvidenceORM.claim_id, func.count()).group_by(EvidenceORM.claim_id)
                )
            ).all()
        return {claim_id: int(n) for claim_id, n in rows}

    async def evidence_stance_counts(self) -> dict[str, dict[str, int]]:
        """Per-claim {supports, contradicts, mentions} counts (one grouped query)."""
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(EvidenceORM.claim_id, EvidenceORM.stance, func.count())
                    .group_by(EvidenceORM.claim_id, EvidenceORM.stance)
                )
            ).all()
        out: dict[str, dict[str, int]] = {}
        for claim_id, stance, n in rows:
            out.setdefault(claim_id, {})[stance] = int(n)
        return out

    # ── runs ──────────────────────────────────────────────────────────────
    async def add_run(
        self,
        *,
        title: str = "",
        session_id: str = "",
        hypothesis_id: str = "",
        subtask_id: str = "",
        cmd: str = "",
        code_uri: str = "",
        seed: int | None = None,
        env_lock: str = "",
        inputs: dict[str, Any] | None = None,
        output_uris: list[str] | None = None,
        metrics: dict[str, Any] | None = None,
        status: str = "recorded",
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> RunORM:
        async with self._db.session() as s:
            row = RunORM(
                title=title.strip(),
                session_id=session_id,
                hypothesis_id=hypothesis_id,
                subtask_id=subtask_id,
                cmd=cmd,
                code_uri=code_uri,
                seed=seed,
                env_lock=env_lock,
                inputs=dict(inputs or {}),
                output_uris=list(output_uris or []),
                metrics=dict(metrics or {}),
                status=status,
                started_at=started_at,
                finished_at=finished_at or (datetime.now(UTC) if status != "recorded" else None),
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row

    async def get_run(self, run_id: str) -> RunORM | None:
        return await self._get_by_id_or_prefix(RunORM, run_id)

    async def list_runs(self, *, limit: int = 50, session_id: str = "") -> list[RunORM]:
        async with self._db.session() as s:
            q = select(RunORM).order_by(RunORM.created_at.desc())
            if session_id:
                q = q.where(RunORM.session_id == session_id)
            rows = (await s.execute(q.limit(limit))).scalars().all()
        return list(rows)

    # ── aggregate ───────────────────────────────────────────────────────────
    async def counts(self) -> dict[str, int]:
        async with self._db.session() as s:
            async def _n(model: Any) -> int:
                return int((await s.execute(select(func.count()).select_from(model))).scalar() or 0)

            return {
                "sources": await _n(SourceORM),
                "chunks": await _n(ChunkORM),
                "citations": await _n(CitationORM),
                "hypotheses": await _n(HypothesisORM),
                "claims": await _n(ClaimORM),
                "evidence": await _n(EvidenceORM),
                "runs": await _n(RunORM),
            }

    async def _get_by_id_or_prefix(self, model: Any, ident: str) -> Any | None:
        ident = (ident or "").strip()
        if not ident:
            return None
        async with self._db.session() as s:
            exact = (
                await s.execute(select(model).where(model.id == ident))
            ).scalar_one_or_none()
            if exact is not None:
                return exact
            rows = (
                await s.execute(select(model).order_by(model.created_at.desc()).limit(500))
            ).scalars().all()
        for row in rows:
            if row.id.startswith(ident):
                return row
        return None


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return 0.5


__all__ = [
    "ResearchStore",
    "source_dedup_key",
    "HYPOTHESIS_STATUSES",
    "EVIDENCE_STANCES",
]
