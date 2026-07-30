"""Memory service: record, recall (hybrid), consolidate, notebook.

Recall blends cosine similarity (when embeddings are available) with
recency, importance and a pin boost — the same signals HelixForge uses, but
computed locally. When the provider has no embeddings (e.g. partial offline
setups) it degrades to keyword overlap so recall always works.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select

from omni.config.settings import OmniSettings
from omni.core.termination import is_bounded_termination
from omni.core.timefmt import ensure_aware
from omni.memory import policy
from omni.memory.graph import MemoryGraph
from omni.memory.sanitize import redact_secrets
from omni.memory.vectors import cosine
from omni.storage.db import Database, get_database
from omni.storage.models import MemoryEntryORM, SubtaskORM, _utcnow

logger = logging.getLogger(__name__)


def open_global_store(settings: OmniSettings) -> Database | None:
    """Resolve the machine-global memory store from ``settings``, or ``None``.

    Returns ``None`` when ``memory.global_store`` is off, when settings carry no
    resolved paths, or when the global path would collide with the per-workspace
    db — in every such case :class:`MemoryService` behaves exactly as before.
    The returned :class:`Database` is process-cached (one per file), so every
    ``MemoryService`` in a process shares the same global handle. It is *not*
    initialised here; the owning ``OmniAgent`` calls ``init()`` once at setup.
    """
    if not getattr(settings.memory, "global_store", False):
        return None
    paths = getattr(settings, "paths", None)
    if paths is None:
        return None
    try:
        gdb = paths.global_memory_db
        if gdb.resolve() == paths.project_db.resolve():
            return None
        return get_database(gdb)
    except Exception:  # noqa: BLE001 — never let global memory abort a caller.
        logger.debug("global memory store unavailable; using workspace store", exc_info=True)
        return None

# Content that must never be distilled into durable memory: runtime error
# messages, payment/HTTP failures, and trivial control chatter. Without this
# gate the extractor turns provider errors or an "exit"
# into a remembered "research finding" that later pollutes recall.
_NOISE_RE = re.compile(
    r"(payment required|\b40[0-9]\b|\b5\d{2}\b|http/1|traceback|"
    r"error\s*:|exception|invalid request|rate.?limit|timed? ?out|"
    r"connection (?:error|refused)|401 unauthorized|token expired)",
    re.IGNORECASE,
)
# Whole-message chatter that should not seed an episodic summary on its own.
_TRIVIAL_MESSAGES = frozenset({"exit", "quit", "q", "/exit", "/quit"})

# A turn that terminated without a clean, converged answer: its assistant text is
# a salvage/partial note, not a durable finding, so it must not seed memory.
_DEGRADED_TERMINATED = frozenset(
    {
        "max_tool_calls",
        "max_iterations",
        "timeout",
        "llm_error",
        "llm_transcript_invalid",
        "llm_auth_error",
        "llm_configuration_error",
        "llm_rate_limited",
        "llm_unavailable",
        "llm_invalid_request",
        "llm_timeout",
    }
)
# Read-only retrieval tools. A turn whose tools are *only* these produced no new
# grounded conclusion of its own (it just fetched/searched), so it is skipped as
# a finding source — the grounded write path is the ``remember`` tool.
_RETRIEVAL_ONLY_TOOLS = frozenset(
    {
        "web_fetch", "web_search", "memory_search", "memory_get", "open_artifact",
        "list_session_artifacts", "get_subtask", "get_task", "list_recent_tasks",
        "search_tasks", "corpus_search", "arxiv_search", "openalex_search", "crossref_search",
        "unpaywall_lookup", "search_papers", "literature_search",
    }
)


def _is_low_value_assistant_turn(m: dict[str, Any]) -> bool:
    """True when an assistant turn should not seed durable findings/episodes.

    Covers degraded/partial/tool-limit turns and pure external-retrieval turns
    (no grounded conclusion of their own). User messages are never dropped by
    this — a preference stated during a failed turn is still worth keeping.
    """
    if m.get("role") != "assistant":
        return False
    meta = m.get("meta") or {}
    if str(meta.get("kind") or "") in ("error", "partial"):
        return True
    reason = str(meta.get("terminated_reason") or "")
    if reason in _DEGRADED_TERMINATED or is_bounded_termination(reason):
        return True
    tools = [str(t) for t in (meta.get("tools") or []) if t]
    return bool(tools) and set(tools).issubset(_RETRIEVAL_ONLY_TOOLS)


def _is_noise_text(text: str) -> bool:
    """True when ``text`` is a runtime error / trivial control message.

    Used to keep long-term memory clean: such content has no research value and,
    once remembered, gets re-injected into future prompts as if it were a fact.
    """
    stripped = (text or "").strip()
    if len(stripped) < 5:
        return True
    if stripped.lower() in _TRIVIAL_MESSAGES:
        return True
    return bool(_NOISE_RE.search(stripped))


class MemoryLayer(StrEnum):
    SESSION = "M1"
    TASK = "M2"
    EPISODIC = "M3"
    SEMANTIC = "M4"
    ARTIFACT = "M5"


# Layers eligible for cross-session recall by default.
_CROSS_SESSION_LAYERS = {MemoryLayer.EPISODIC.value, MemoryLayer.SEMANTIC.value,
                         MemoryLayer.ARTIFACT.value}

# The machine owner / CLI identity. Also the *shared baseline*: owner-curated and
# project knowledge is visible to every principal this machine serves (mirrors
# the shared MEMORY.md/AGENTS.md injection), while an IM peer's own memory stays
# private to that peer.
PRINCIPAL_OWNER = "local"


def principal_of(channel: str, external_key: str, *, channel_identity: str = "per_peer") -> str:
    """Canonical (channel, external_key) → memory principal mapping.

    The CLI/machine owner (and MCP) is always :data:`PRINCIPAL_OWNER`. How an IM
    identity maps depends on ``channel_identity`` (``memory.channel_identity``):

    * ``owner`` — every *authorized* IM identity shares the owner's memory. Only
      allow-listed / paired messages ever reach this function, so what you tell
      the bot on Feishu is recalled in the CLI and vice-versa (the personal-
      assistant default). "Pairing" thus binds the identity to the owner.
    * ``per_peer`` — each IM identity is its own principal
      (``"<channel>:<external_key>"``), seeing only its own memory + the owner
      baseline (multi-user shared-bot safe).

    This is the single source of truth shared by the orchestrator's foreground
    path and the background task runtime — both must agree, or async task results
    leak across principals.
    """
    ch = (channel or "cli").strip().lower()
    key = (external_key or "").strip()
    if ch in ("cli", "", "mcp") or not key:
        return PRINCIPAL_OWNER
    if (channel_identity or "per_peer").strip().lower() == "owner":
        return PRINCIPAL_OWNER
    return f"{ch}:{key}"


def _principal_visible(entry_principal: str, principal: str) -> bool:
    """Whether a memory owned by ``entry_principal`` may be recalled for ``principal``.

    A principal sees its own memory plus the shared owner baseline; it never sees
    another IM peer's memory. This is the guard that lets one ``omni serve``
    daemon serve many chats without cross-contaminating recall.
    """
    ep = entry_principal or PRINCIPAL_OWNER
    return ep == principal or ep == PRINCIPAL_OWNER


@dataclass
class ScoredMemory:
    entry: MemoryEntryORM
    score: float


@dataclass(frozen=True, slots=True)
class ExtractedMemoryFact:
    text: str
    memory_type: str = "finding"
    scope: str = "project"


class MemoryService:
    def __init__(
        self,
        db: Database,
        settings: OmniSettings,
        llm: Any = None,
        *,
        global_db: Database | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._llm = llm
        self._warned_large_store = False  # one-shot recall full-scan size warning
        # Machine-global store for cross-workspace *identity* memory (user-scope
        # preferences, the persona profile, episodic summaries). ``None`` when the
        # ``memory.global_store`` switch is off (or it resolves to the same file as
        # the workspace store) → every write/read stays in ``_db`` exactly like
        # before. When set, :meth:`_store_for` routes identity rows here so the
        # owner's memory follows them across projects, terminals and channels.
        self._global_db = global_db if (global_db is not None and global_db is not db) else None
        # The global store is a shared, process-cached handle whose schema is
        # normally created by the owning ``OmniAgent`` at setup. A MemoryService
        # built outside that path (recall tools, research tools, tests) may be the
        # first to touch it, so ensure its schema lazily & idempotently on first
        # store-spanning call (``Database.init`` returns immediately if ready).
        self._global_ready = False
        # Multi-session memory graph (P3): auto-links new memories + spreads
        # recall one hop over those edges. Public so the CLI can traverse it.
        self.graph = MemoryGraph(db, settings)
        # A parallel graph over the global store so global identity rows get
        # linked within their own store; recall spreads across both graphs and
        # merges the boosts (see :meth:`_graph_spread`).
        self._global_graph = MemoryGraph(self._global_db, settings) if self._global_db else None

    # ── store routing (machine-global vs per-workspace) ────────────────────

    async def _ensure_global(self) -> None:
        """Idempotently create the global store's schema before first use.

        No-op when there is no global store or it is already initialised. On
        failure the global store is dropped (service degrades to workspace-only)
        rather than crashing recall/record.
        """
        if self._global_db is None or self._global_ready:
            return
        try:
            await self._global_db.init()
        except Exception:  # noqa: BLE001 — degrade to workspace-only, never crash.
            logger.debug("global memory store init failed; using workspace only", exc_info=True)
            self._global_db = None
            self._global_graph = None
        self._global_ready = True

    def _stores(self) -> list[Database]:
        """All backing stores to read from: workspace first, then global."""
        return [self._db, self._global_db] if self._global_db is not None else [self._db]

    def _is_global_row(self, *, layer: str, scope: str, memory_type: str) -> bool:
        """Whether a row is cross-workspace *identity* memory (→ global store).

        Global holds the owner's durable "who you are": user-scope preferences,
        the synthesized ``user_profile``, and episodic summaries (what has been
        worked on). Everything else — session dialogue (M1), task results (M2),
        artifact refs (M5), and project-scope findings (M4) — is workspace-bound
        and stays in ``_db``.
        """
        if self._global_db is None:
            return False
        return (
            scope == "user"
            or memory_type == "user_profile"
            or layer == MemoryLayer.EPISODIC.value
        )

    def _store_for(self, *, layer: str, scope: str, memory_type: str) -> Database:
        return (
            self._global_db
            if self._is_global_row(layer=layer, scope=scope, memory_type=memory_type)
            else self._db
        )

    def _graph_for(self, db: Database) -> MemoryGraph:
        if self._global_db is not None and db is self._global_db:
            return self._global_graph  # type: ignore[return-value]
        return self.graph

    def _graphs(self) -> list[MemoryGraph]:
        """All backing graphs to traverse: workspace first, then global.

        Edges never cross a store boundary (a global preference and a workspace
        finding live in different SQLite files), so cross-store spread means
        walking each store's graph from the shared seeds and merging the boosts.
        """
        return [self.graph] + ([self._global_graph] if self._global_graph is not None else [])

    async def _embed(self, text: str) -> list[float] | None:
        if (
            not self._llm
            or not self._settings.memory.embeddings_enabled
            or self._settings.memory.vector_backend == "none"
        ):
            return None
        try:
            vecs = await self._llm.embed([text])
            return vecs[0] if vecs else None
        except NotImplementedError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("embed failed, falling back to keyword recall: %s", exc)
            return None

    async def record(
        self,
        *,
        layer: MemoryLayer | str,
        summary: str,
        scope: str = "session",
        scope_id: str = "",
        memory_type: str = "note",
        tags: list[str] | None = None,
        importance: float = 0.5,
        pinned: bool = False,
        payload_ref: str = "",
        embed: bool = True,
        principal: str = PRINCIPAL_OWNER,
    ) -> str:
        await self._ensure_global()
        layer_val = layer.value if isinstance(layer, MemoryLayer) else str(layer)
        principal = (principal or PRINCIPAL_OWNER).strip() or PRINCIPAL_OWNER
        vec = await self._embed(summary) if embed else None
        entry = MemoryEntryORM(
            principal=principal,
            layer=layer_val,
            scope=scope,
            scope_id=scope_id,
            memory_type=memory_type,
            summary=summary,
            payload_ref=payload_ref,
            embedding=vec or [],
            tags=tags or [],
            importance=float(importance),
            pinned=1 if pinned else 0,
        )
        # Route cross-workspace identity rows to the machine-global store, all
        # other rows to the per-workspace store (see :meth:`_is_global_row`).
        store = self._store_for(layer=layer_val, scope=scope, memory_type=memory_type)
        async with store.session() as s:
            s.add(entry)
            await s.commit()
            await s.refresh(entry)
        self._mirror_user_scope(scope, memory_type, summary, principal)
        await self._graph_link(entry.id, layer_val, memory_type, store)
        return entry.id

    async def _graph_link(
        self, mem_id: str, layer: str, memory_type: str, store: Database | None = None
    ) -> None:
        """Auto-link a fresh durable memory into the graph (P3), best-effort.

        Only cross-session layers (M3/M4/M5) are graphed, and the synthetic
        ``user_profile`` rollup is skipped (its component preferences are already
        graphed individually). Never fatal to a write.
        """
        if (
            not getattr(self._settings.memory, "graph_enabled", True)
            or layer not in _CROSS_SESSION_LAYERS
            or memory_type == "user_profile"
        ):
            return
        graph = self._graph_for(store) if store is not None else self.graph
        try:
            await graph.link_new_memory(mem_id)
        except Exception as exc:  # noqa: BLE001 — graph linking is advisory
            logger.debug("memory graph link failed: %s", exc)

    def _mirror_user_scope(self, scope: str, memory_type: str, summary: str, principal: str) -> None:
        """Mirror an *owner* ``scope="user"`` write into the global ``~/.omni/MEMORY.md``.

        User-scope rows live in the per-workspace ``sessions.sqlite3`` and would
        otherwise never follow the researcher into another project. Mirroring the
        summary into MEMORY.md (the file every workspace injects and re-imports as
        pinned memory) is what makes the *owner's* preferences truly user-global.

        Critically, this only fires for the owner (``principal == "local"``). An IM
        peer's learned preference must stay in that peer's principal scope inside
        the DB — never written to the shared global file, or it would leak into
        every other peer's and the owner's context. ``user_profile`` is skipped:
        its component preferences are already mirrored individually.
        """
        if principal != PRINCIPAL_OWNER or scope != "user" or memory_type == "user_profile":
            return
        try:
            from omni.memory.files import append_user_preference

            append_user_preference(self._settings.paths, summary)
        except Exception as exc:  # noqa: BLE001
            logger.debug("MEMORY.md user-scope mirror failed: %s", exc)

    async def recall(
        self,
        query: str,
        *,
        session_id: str = "",
        subtask_id: str = "",
        limit: int | None = None,
        cross_session: bool | None = None,
        principal: str = PRINCIPAL_OWNER,
    ) -> list[ScoredMemory]:
        """Recall the most relevant memories for ``query`` (bounded).

        Thin wrapper over :meth:`recall_scoped` with the default scope gate
        (current session/task + that session's tasks + cross-session layers +
        pinned). Delegating keeps recall on the *bounded* candidate path — it no
        longer reads the whole ``memory_entries`` table into Python — while
        preserving the legacy signature used by the CLI / REPL / debug paths.
        """
        return await self.recall_scoped(
            query,
            session_id=session_id,
            subtask_id=subtask_id,
            limit=limit,
            cross_session=cross_session,
            principal=principal,
        )

    async def recall_scoped(
        self,
        query: str,
        *,
        session_id: str = "",
        subtask_id: str = "",
        layers: list[str] | None = None,
        scopes: list[str] | None = None,
        limit: int | None = None,
        candidate_limit: int | None = None,
        cross_session: bool | None = None,
        principal: str = PRINCIPAL_OWNER,
    ) -> list[ScoredMemory]:
        """Recall from a bounded candidate set selected for the current turn.

        This is the retrieval primitive used by ``MemoryCompiler`` (and now by
        :meth:`recall` itself). It keeps the cross-session value of M3/M4/M5
        memories but never reads the whole memory table into Python: it pulls at
        most ``memory.recall_candidate_limit`` of the top pinned/important/recent
        rows to score. A caller's ``limit`` and any explicit ``candidate_limit``
        are both clamped to that hard cap, so a large/adversarial ``limit`` can
        never turn recall into a full-store scan or exfiltrate the whole store.
        The candidate set is gated on principal so an IM peer only ever sees its
        own memory + the owner baseline.
        """
        if not self._settings.memory.enabled:
            return []
        await self._ensure_global()
        cap = int(getattr(self._settings.memory, "recall_candidate_limit", 200) or 200)
        limit = int(limit or self._settings.memory.recall_limit)
        limit = max(1, min(limit, cap))  # never return/scan more than the hard cap
        if candidate_limit is None:
            candidate_limit = cap
        candidate_limit = min(int(candidate_limit), cap)  # explicit callers can't exceed the cap
        scan = max(limit, candidate_limit)  # ≤ cap, since both are ≤ cap
        cross = self._settings.memory.cross_session if cross_session is None else cross_session
        principal = (principal or PRINCIPAL_OWNER).strip() or PRINCIPAL_OWNER
        layer_values = [str(layer.value if isinstance(layer, MemoryLayer) else layer) for layer in (layers or [])]
        scope_values = [str(scope) for scope in (scopes or []) if str(scope)]

        # Session→task mapping lives in the workspace store (SubtaskORM); resolve
        # it there once, then apply the same scope filter to every backing store.
        session_task_ids: set[str] = set()
        if session_id:
            async with self._db.session() as s:
                session_task_ids = set(
                    (await s.execute(
                        select(SubtaskORM.id).where(SubtaskORM.session_id == session_id)
                    )).scalars().all()
                )
        scope_clauses = [MemoryEntryORM.pinned == 1]
        if session_id:
            scope_clauses.append(
                (MemoryEntryORM.scope == "session") & (MemoryEntryORM.scope_id == session_id)
            )
        subtask_ids = {item for item in [subtask_id, *session_task_ids] if item}
        if subtask_ids:
            scope_clauses.append(
                (MemoryEntryORM.scope == "task") & (MemoryEntryORM.scope_id.in_(subtask_ids))
            )
        if cross:
            cross_layers = layer_values or list(_CROSS_SESSION_LAYERS)
            scope_clauses.append(MemoryEntryORM.layer.in_(cross_layers))
        if scope_values:
            scope_clauses.append(MemoryEntryORM.scope.in_(scope_values))

        principals = [principal, PRINCIPAL_OWNER] if principal != PRINCIPAL_OWNER else [PRINCIPAL_OWNER]
        query_stmt = (
            select(MemoryEntryORM)
            .where(or_(*scope_clauses))
            .where(MemoryEntryORM.principal.in_(principals))
        )
        if layer_values:
            query_stmt = query_stmt.where(MemoryEntryORM.layer.in_(layer_values))
        query_stmt = query_stmt.order_by(
            MemoryEntryORM.pinned.desc(),
            MemoryEntryORM.importance.desc(),
            MemoryEntryORM.created_at.desc(),
        ).limit(scan)

        # Union the bounded candidate set across the workspace and global stores
        # (dedup by id) so a preference the owner set in another project is still
        # a candidate here. Each store contributes ≤ scan rows; scoring then
        # ranks the merged set and clamps to ``limit``.
        rows: list[MemoryEntryORM] = []
        seen_ids: set[str] = set()
        for store in self._stores():
            async with store.session() as s:
                store_rows = (await s.execute(query_stmt)).scalars().all()
            for r in store_rows:
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    rows.append(r)

        now = datetime.now(UTC)
        rows = [r for r in rows if not _expired(r, now)]
        if not rows:
            return []
        query_vec = await self._embed(query) if query else None
        scored = [ScoredMemory(r, self._score(r, query, query_vec, now)) for r in rows]
        scored.sort(key=lambda sm: sm.score, reverse=True)
        scored = await self._graph_spread(scored, query, query_vec, now, limit, cap, principal)
        top = scored[:limit]
        await self._touch([sm.entry.id for sm in top])
        return top

    async def _graph_spread(
        self, scored: list[ScoredMemory], query: str, query_vec: list[float] | None,
        now: datetime, limit: int, cap: int, principal: str,
    ) -> list[ScoredMemory]:
        """Boost + surface cross-session neighbours of the top hits (P3 graph recall).

        Spreads one (or a few) hops from the top ``limit`` base hits: existing
        candidates that are neighbours get their score lifted, and neighbours that
        fell *outside* the flat candidate window are loaded (bounded, principal-
        filtered) and added — this is what lets recall reach a related memory from
        another session that plain similarity alone would miss. A no-op when the
        graph is disabled or nothing spreads.
        """
        if not (getattr(self._settings.memory, "graph_enabled", True) and query and scored):
            return scored
        seeds = [sm.entry.id for sm in scored[:limit]]
        # Spread within each backing store's graph (edges never cross stores) and
        # merge boosts (max) so a workspace finding and a global preference both
        # lift their neighbours from the same top hits.
        boosts: dict[str, float] = {}
        for graph in self._graphs():
            for mid, bump in (await graph.spread(seeds, principal=principal)).items():
                boosts[mid] = max(boosts.get(mid, 0.0), bump)
        if not boosts:
            return scored
        have = {sm.entry.id for sm in scored}
        for sm in scored:  # lift already-present neighbours
            bump = boosts.get(sm.entry.id)
            if bump:
                sm.score += bump
        extra_ids = [mid for mid in boosts if mid not in have]
        if extra_ids:
            loaded: dict[str, MemoryEntryORM] = {}
            for graph in self._graphs():  # a boosted id lives in exactly one store
                for row in await graph.load_visible(extra_ids, principal=principal, limit=cap):
                    loaded.setdefault(row.id, row)
            for row in loaded.values():
                if _expired(row, now):
                    continue
                base = self._score(row, query, query_vec, now)
                scored.append(ScoredMemory(row, base + boosts[row.id]))
        scored.sort(key=lambda sm: sm.score, reverse=True)
        return scored

    def _warn_if_store_large(self, count: int) -> None:
        """Warn once per process when the memory store grows past the threshold.

        Recall itself is now bounded (``recall_candidate_limit``), so this is a
        hygiene hint rather than a latency guard: a very large store means older,
        low-priority memories fall outside recall's candidate window. Emitted from
        session-end maintenance (``decay_and_dedup``, which already reads the row
        count) so it never adds a query to the hot path.
        """
        threshold = int(getattr(self._settings.memory, "max_entries_warn", 0) or 0)
        if threshold <= 0 or self._warned_large_store or count < threshold:
            return
        self._warned_large_store = True
        logger.warning(
            "memory store has %d entries (> %d): recall is bounded to the top %d "
            "candidates, so older low-priority memories may fall outside recall. "
            "Prune with `omni memory clear --type episode --yes` or let decay run; "
            "raise memory.max_entries_warn to silence.",
            count, threshold,
            int(getattr(self._settings.memory, "recall_candidate_limit", 200) or 200),
        )

    def _score(self, r: MemoryEntryORM, query: str, query_vec: list[float] | None, now: datetime) -> float:
        sim = 0.0
        if query_vec and r.embedding:
            sim = cosine(query_vec, r.embedding)
        elif query:
            sim = _keyword_overlap(query, r.summary)
        age_days = max(0.0, (now - _aware(r.created_at)).total_seconds() / 86400.0)
        recency = 1.0 / (1.0 + age_days)
        pin = 0.3 if r.pinned else 0.0
        # Usage-aware (P2): memories the researcher actually leans on rise over
        # equally-similar but never-used ones. Saturating (0 when unused → ~1
        # after ~10 recalls) so a single hot entry can't dominate similarity.
        usage = 1.0 - 1.0 / (1.0 + max(0, r.recall_count))
        # Citation-aware (P2): a memory anchored to a real source/claim/run/
        # artifact (``payload_ref``) is more trustworthy than an equally-similar
        # but ungrounded recollection — a small, fixed lift (the provenance moat)
        # that never overrides similarity.
        cited = 0.06 if (r.payload_ref or "").strip() else 0.0
        return 0.52 * sim + 0.2 * float(r.importance) + 0.12 * recency + 0.1 * usage + cited + pin

    async def _touch(self, ids: list[str]) -> None:
        if not ids:
            return
        # A recalled id lives in exactly one store; update recall stats wherever
        # it is found (each store only touches the rows it actually holds).
        for store in self._stores():
            async with store.session() as s:
                rows = (
                    await s.execute(select(MemoryEntryORM).where(MemoryEntryORM.id.in_(ids)))
                ).scalars().all()
                if not rows:
                    continue
                for r in rows:
                    r.recall_count += 1
                    r.accessed_at = _utcnow()
                await s.commit()

    def build_recall_block(self, memories: list[ScoredMemory], *, budget: int = 2400) -> str:
        """Render recalled memories with provenance + staleness, within a char budget.

        Each line is tagged ``[layer/type·scope]`` so the model can weigh where a
        fact came from, and entries that are old, low-importance and unverified
        are marked ``stale`` so old knowledge is treated with caution.
        """
        if not memories:
            return ""
        now = datetime.now(UTC)
        default_days = int(getattr(self._settings.memory, "staleness_days", 45))
        lines = ["[Relevant memory] Historical context; entries marked stale require verification."]
        used = len(lines[0])
        for sm in memories:
            e = sm.entry
            scope = {"session": "session", "task": "task", "global": "global"}.get(e.scope, e.scope)
            stale = " stale" if policy.is_stale(e, default_days=default_days, now=now) else ""
            pin = "📌" if e.pinned else ""
            tag = f"[{e.layer}/{e.memory_type}·{scope}]{pin}{stale}"
            line = f"- {tag} {e.summary.strip()[:240]}"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    async def extract_session(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        max_facts: int = 6,
        transcript_chars: int = 6000,
        principal: str = PRINCIPAL_OWNER,
        on_llm_call: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> list[str]:
        """Distil durable facts (M4) + one episodic summary (M3) from a transcript.

        Offline-safe: uses the LLM when a real provider is configured and skips
        semantic fact extraction otherwise. Facts are sanitised and de-duplicated
        so the long-term store stays clean and bounded. Everything is tagged with
        ``principal`` so an IM peer's distilled facts stay private to that peer.
        """
        if not self._settings.memory.enabled:
            return []
        convo = [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
        user_msgs = [m for m in convo if m.get("role") == "user"]
        if not user_msgs:
            return []
        # Findings/episode are seeded only from substantive turns: drop
        # degraded/partial/tool-limit and pure external-retrieval assistant turns
        # so failures and raw search dumps never become "remembered findings".
        substantive = [m for m in convo if not _is_low_value_assistant_turn(m)]
        transcript = "\n".join(
            f"{m['role']}: {str(m['content'])[:500]}" for m in substantive
        )[:transcript_chars]

        facts = await self._extract_facts_llm(
            transcript,
            max_facts,
            on_llm_call=on_llm_call,
        )
        if facts is None:
            facts = []

        recorded: list[str] = []
        for fact in facts:
            clean = redact_secrets(fact.text).strip()
            if len(clean) < 5 or _is_noise_text(clean) or await self._is_duplicate_semantic(clean):
                continue
            mtype, scope = fact.memory_type, fact.scope
            # Owner scope="user" writes are mirrored to the global MEMORY.md
            # centrally inside record(); an IM peer's stay in its principal scope.
            recorded.append(await self.record(
                layer=MemoryLayer.SEMANTIC, scope=scope,
                scope_id="local" if scope == "user" else "",
                summary=clean, memory_type=mtype, importance=0.65,
                principal=principal,
            ))

        episode = redact_secrets(self._episode_summary(substantive))
        if episode and not await self._is_duplicate_semantic(episode, layer=MemoryLayer.EPISODIC.value):
            recorded.append(await self.record(
                layer=MemoryLayer.EPISODIC, scope="session", scope_id=session_id,
                summary=episode, memory_type="episode", importance=0.6,
                principal=principal,
            ))
        return recorded

    async def _extract_facts_llm(
        self,
        transcript: str,
        max_facts: int,
        *,
        on_llm_call: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> list[ExtractedMemoryFact] | None:
        provider = (self._settings.model.provider or "mock").lower()
        if not self._llm or provider in ("mock", "", "offline"):
            return None
        try:
            system = (
                "Extract only durable memory from the conversation. Preserve each fact in its original "
                "language; never translate it. Return a JSON array with at most "
                f"{max_facts} objects. Each object must have text, type (preference|decision|finding), "
                "and scope (user|project). Do not store runtime errors, transient chatter, raw search "
                "results, or unsupported conclusions. Return [] when nothing is durable."
            )
            out = await self._llm.chat(system, transcript)
        except Exception as exc:  # noqa: BLE001
            logger.debug("llm fact extraction failed: %s", exc)
            return None
        if on_llm_call is not None:
            try:
                await on_llm_call(system, transcript, out)
            except Exception:  # noqa: BLE001
                logger.debug("fact extraction observer failed", exc_info=True)
        return _parse_extracted_facts(out, max_facts=max_facts)

    @staticmethod
    def _episode_summary(convo: list[dict[str, Any]]) -> str:
        # Anchor the episode on the first *substantive* user ask and the last
        # *substantive* assistant reply, skipping trivial chatter ("exit") and
        # error turns so we never remember noise as a
        # research episode.
        first_user = next(
            (str(m["content"]) for m in convo
             if m.get("role") == "user" and not _is_noise_text(str(m.get("content") or ""))),
            "",
        )
        last_asst = next(
            (str(m["content"]) for m in reversed(convo)
             if m.get("role") == "assistant" and not _is_noise_text(str(m.get("content") or ""))),
            "",
        )
        if not first_user:
            return ""
        out = f"Session episode started with: {first_user[:140]}"
        if last_asst:
            out += f"; latest progress: {last_asst[:160]}"
        return out

    async def _is_duplicate_semantic(self, text: str, *, layer: str | None = None) -> bool:
        layer = layer or MemoryLayer.SEMANTIC.value
        norm = text.strip()
        await self._ensure_global()
        # Runs on every record()/extract; bound the scan to the most recent
        # candidates (near-duplicates are recent) so writes stay O(cap), not
        # O(store), as the memory grows.
        cap = int(getattr(self._settings.memory, "recall_candidate_limit", 200) or 200)
        # Check every backing store: a preference may already live in the global
        # store while a finding lives in the workspace store, and we must not
        # write a near-duplicate into either.
        for store in self._stores():
            if await self._dup_in_store(store, norm, layer, cap):
                return True
        return False

    async def _dup_in_store(self, store: Database, norm: str, layer: str, cap: int) -> bool:
        async with store.session() as s:
            rows = (await s.execute(
                select(MemoryEntryORM)
                .where(MemoryEntryORM.layer == layer)
                .order_by(MemoryEntryORM.created_at.desc())
                .limit(cap)
            )).scalars().all()
        for r in rows:
            if r.summary.strip() == norm or _keyword_overlap(norm, r.summary) >= 0.85:
                return True
        return False

    async def migrate_identity_to_global(self, *, marker: Path | None = None) -> int:
        """One-time copy of this workspace's *identity* memory into the global store.

        After enabling ``memory.global_store`` on an existing install, the owner's
        user-scope preferences, persona profile, and episodic summaries already
        sit in the per-workspace ``sessions.sqlite3``. This copies them into the
        machine-global store (dedup-guarded against the global store only, so it
        is idempotent) so recall follows the owner into other projects. New writes
        already route to the global store, so this only backfills legacy rows.

        ``marker`` (a per-workspace sentinel file) makes the scan run at most once:
        if it exists the copy is skipped; otherwise it is written afterwards. The
        copy itself is dedup-safe, so a lost marker only costs a harmless rescan.
        Returns the number of rows copied; a no-op when the global store is off.
        """
        if self._global_db is None:
            return 0
        if marker is not None and marker.exists():
            return 0
        await self._ensure_global()
        if self._global_db is None:  # init may have failed and disabled the store
            return 0
        cap = int(getattr(self._settings.memory, "recall_candidate_limit", 200) or 200)
        async with self._db.session() as s:
            rows = list((await s.execute(
                select(MemoryEntryORM).where(
                    or_(
                        MemoryEntryORM.scope == "user",
                        MemoryEntryORM.memory_type == "user_profile",
                        MemoryEntryORM.layer == MemoryLayer.EPISODIC.value,
                    )
                )
            )).scalars().all())
        copied = 0
        for r in rows:
            if await self._dup_in_store(self._global_db, r.summary.strip(), r.layer, cap):
                continue
            async with self._global_db.session() as gs:
                gs.add(MemoryEntryORM(
                    principal=r.principal, layer=r.layer, scope=r.scope, scope_id=r.scope_id,
                    memory_type=r.memory_type, summary=r.summary, payload_ref=r.payload_ref,
                    embedding=list(r.embedding or []), tags=list(r.tags or []),
                    importance=r.importance, pinned=r.pinned,
                ))
                await gs.commit()
            copied += 1
        if marker is not None:
            try:
                marker.write_text(str(copied), encoding="utf-8")
            except OSError:
                logger.debug("global-memory migration marker write failed", exc_info=True)
        return copied

    async def list_recent(
        self,
        limit: int = 20,
        *,
        memory_type: str = "",
        layer: str = "",
        offset: int = 0,
    ) -> list[MemoryEntryORM]:
        # Merge recent rows across all backing stores so `omni memory` surfaces
        # both workspace and global entries in one chronological view.
        await self._ensure_global()
        merged: list[MemoryEntryORM] = []
        for store in self._stores():
            async with store.session() as s:
                q = select(MemoryEntryORM).order_by(MemoryEntryORM.created_at.desc())
                if memory_type:
                    q = q.where(MemoryEntryORM.memory_type == memory_type)
                if layer:
                    q = q.where(MemoryEntryORM.layer == layer)
                # Pull enough from each store to satisfy offset+limit after merge.
                rows = (await s.execute(q.limit(max(0, offset) + limit))).scalars().all()
            merged.extend(rows)
        merged.sort(key=lambda r: _aware(r.created_at), reverse=True)
        return merged[max(0, offset):max(0, offset) + limit]

    async def _locate(self, mem_id: str) -> tuple[Database | None, MemoryEntryORM | None, str]:
        """Resolve a memory id/prefix across all stores → ``(store, row, status)``.

        ``status`` is ``"ok"``, ``"not_found"`` or ``"ambiguous"``. Exact-id hits
        win; otherwise a prefix must match exactly one row across all stores.
        """
        mem_id = (mem_id or "").strip()
        if not mem_id:
            return None, None, "not_found"
        await self._ensure_global()
        for store in self._stores():
            async with store.session() as s:
                row = (await s.execute(
                    select(MemoryEntryORM).where(MemoryEntryORM.id == mem_id)
                )).scalar_one_or_none()
            if row is not None:
                return store, row, "ok"
        if len(mem_id) < 4:
            return None, None, "not_found"
        hits: list[tuple[Database, MemoryEntryORM]] = []
        for store in self._stores():
            async with store.session() as s:
                matches = (await s.execute(
                    select(MemoryEntryORM).where(MemoryEntryORM.id.like(f"{mem_id}%")).limit(2)
                )).scalars().all()
            hits.extend((store, m) for m in matches)
        if len(hits) == 1:
            return hits[0][0], hits[0][1], "ok"
        if len(hits) > 1:
            return None, None, "ambiguous"
        return None, None, "not_found"

    # ── CRUD helpers (P2.7 `/memory` UX) ──────────────────────────────────

    async def get(self, mem_id: str) -> MemoryEntryORM | None:
        """Resolve one memory by exact id or a *unique* prefix.

        Returns ``None`` when a prefix is ambiguous so callers never act on the
        wrong entry (mirrors task prefix resolution). Use :meth:`resolve` when
        you need to distinguish "not found" from "ambiguous".
        """
        row, _ = await self.resolve(mem_id)
        return row

    async def resolve(self, mem_id: str) -> tuple[MemoryEntryORM | None, str]:
        """Resolve a memory id/prefix → ``(row, status)`` across all stores.

        ``status`` is ``"ok"``, ``"not_found"``, or ``"ambiguous"`` (a prefix
        matching more than one entry). Deletes/pins surface a precise message
        instead of silently touching the first match.
        """
        _store, row, status = await self._locate(mem_id)
        return row, status

    async def set_pinned(self, mem_id: str, pinned: bool) -> bool:
        store, row, status = await self._locate(mem_id)
        if store is None or row is None or status != "ok":
            return False
        async with store.session() as s:
            obj = await s.get(MemoryEntryORM, row.id)
            obj.pinned = 1 if pinned else 0
            await s.commit()
        return True

    async def delete(self, mem_id: str) -> bool:
        store, row, status = await self._locate(mem_id)
        if store is None or row is None or status != "ok":
            return False
        async with store.session() as s:
            obj = await s.get(MemoryEntryORM, row.id)
            await s.delete(obj)
            await s.commit()
        return True

    async def clear(self, *, memory_type: str = "", layer: str = "", scope: str = "") -> int:
        """Delete memories matching the given filters (pinned are kept). Returns count."""
        await self._ensure_global()
        n = 0
        for store in self._stores():
            async with store.session() as s:
                rows = list((await s.execute(select(MemoryEntryORM))).scalars().all())
                for r in rows:
                    if r.pinned:
                        continue
                    if memory_type and r.memory_type != memory_type:
                        continue
                    if layer and r.layer != layer:
                        continue
                    if scope and r.scope != scope:
                        continue
                    await s.delete(r)
                    n += 1
                await s.commit()
        return n

    # ── maintenance: decay + dedup + user profile (P2.2) ──────────────────

    async def decay_and_dedup(self) -> dict[str, int]:
        """Session-end hygiene: decay non-pinned decaying entries, merge near-dups.

        Keeps the inline (full-scan) recall fast and stops stale empirical
        findings from out-ranking fresh, verified knowledge. Pinned and
        non-decaying types (preferences/decisions/profile) are never touched.
        """
        if not self._settings.memory.enabled:
            return {"decayed": 0, "merged": 0}
        await self._ensure_global()
        factor = float(getattr(self._settings.memory, "decay_factor", 0.9))
        decayed = merged = 0
        # Run hygiene on every backing store so global identity memory is decayed
        # and de-duplicated alongside workspace memory.
        for store in self._stores():
            d, m = await self._decay_and_dedup_store(store, factor)
            decayed += d
            merged += m
        return {"decayed": decayed, "merged": merged}

    async def _decay_and_dedup_store(self, store: Database, factor: float) -> tuple[int, int]:
        decayed = merged = 0
        async with store.session() as s:
            rows = list((await s.execute(select(MemoryEntryORM))).scalars().all())
            self._warn_if_store_large(len(rows))
            for r in rows:
                new_imp = policy.decayed_importance(r, factor=factor)
                if new_imp is not None:
                    r.importance = new_imp
                    decayed += 1
            # near-duplicate merge within the same (layer, scope): keep the
            # strongest, drop the rest, preserving pin + recall history.
            seen: list[MemoryEntryORM] = []
            for r in sorted(rows, key=lambda x: (x.pinned, x.importance), reverse=True):
                dup = next(
                    (k for k in seen
                     if k.layer == r.layer and k.scope == r.scope
                     and _keyword_overlap(r.summary, k.summary) >= 0.85),
                    None,
                )
                if dup is not None and dup.id != r.id:
                    dup.recall_count = max(dup.recall_count, r.recall_count)
                    dup.pinned = max(dup.pinned, r.pinned)
                    await s.delete(r)
                    merged += 1
                else:
                    seen.append(r)
            await s.commit()
        return decayed, merged

    async def global_summary_bullets(
        self, *, principal: str = PRINCIPAL_OWNER, limit: int = 12
    ) -> list[str]:
        """Deterministic digest of the owner's durable global memory (offline).

        The compact "who you are + how you work" injected into every turn: the
        owner's stable preferences from the machine-global store, ordered by
        pin / importance / usage / recency and de-duplicated. Deterministic (no
        LLM) so it is cheap to recompute and the file rewrite can be gated purely
        on content change. Empty when the global store is off.
        """
        if self._global_db is None:
            return []
        await self._ensure_global()
        if self._global_db is None:
            return []
        principal = (principal or PRINCIPAL_OWNER).strip() or PRINCIPAL_OWNER
        async with self._global_db.session() as s:
            rows = list((await s.execute(
                select(MemoryEntryORM).where(
                    MemoryEntryORM.principal == principal,
                    MemoryEntryORM.layer == MemoryLayer.SEMANTIC.value,
                    MemoryEntryORM.memory_type.in_(
                        ["preference", "user_preference", "decision"]
                    ),
                )
            )).scalars().all())
        rows.sort(
            key=lambda r: (r.pinned, r.importance, r.recall_count, _aware(r.created_at)),
            reverse=True,
        )
        out: list[str] = []
        seen: set[str] = set()
        for r in rows:
            txt = " ".join(r.summary.strip().split())
            key = txt[:48]
            if txt and key not in seen:
                seen.add(key)
                out.append(txt[:200])
            if len(out) >= max(1, limit):
                break
        return out

    async def refresh_global_summary(self, paths: Any) -> bool:
        """Rewrite the owner's bounded global memory digest, only when it changed.

        Deterministic + change-gated: computes the digest bullets from the global
        store and writes ``memory_summary.md`` only if the content hash differs, so
        an unproductive session end causes no file churn (and no re-injection).
        """
        from omni.memory.files import write_memory_summary

        budget = int(getattr(self._settings.memory, "summary_token_budget", 700) or 700) * 4
        return write_memory_summary(paths, await self.global_summary_bullets(), budget_chars=budget)

    _PROFILE_HEADER = "User profile (automatically summarized; editable in profile.md or MEMORY.md):"

    async def rebuild_user_profile(
        self,
        *,
        principal: str = PRINCIPAL_OWNER,
        on_llm_call: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> str | None:
        """Distil a self-maintaining persona note from durable preferences.

        The adaptive-profile core folds the owner's stable preferences and decisions into
        one compact profile that is injected into *every* workspace. Selection is
        usage-aware (recall frequency + importance + recency) and merging is
        diff-style — an LLM (when configured) drops stale/contradictory items,
        keeping the profile small and current; offline it degrades to a
        deterministic dedup. For the owner the result is also written to the
        global ``~/.omni/profile.md`` so it truly follows the researcher across
        projects. Idempotent: the prior ``user_profile`` entry is replaced.
        """
        if not (self._settings.memory.enabled and getattr(self._settings.memory, "profile_enabled", True)):
            return None
        await self._ensure_global()
        principal = (principal or PRINCIPAL_OWNER).strip() or PRINCIPAL_OWNER
        # Fold preferences (global store, scope=user) *and* decisions (workspace,
        # scope=project) — union across stores so the persona reflects both.
        rows = []
        for store in self._stores():
            async with store.session() as s:
                rows.extend((await s.execute(
                    select(MemoryEntryORM).where(
                        MemoryEntryORM.layer == MemoryLayer.SEMANTIC.value,
                        MemoryEntryORM.principal == principal,
                    )
                )).scalars().all())
        prefs = [
            r for r in rows
            if r.memory_type in ("preference", "user_preference", "decision")
        ]
        if not prefs:
            return None
        # Usage-aware ordering: what the researcher actually leans on (pinned,
        # frequently recalled) and what is strong/recent rises to the top.
        prefs.sort(
            key=lambda r: (r.pinned, r.recall_count, r.importance, _aware(r.created_at)),
            reverse=True,
        )
        candidates: list[str] = []
        seen_txt: set[str] = set()
        for r in prefs[:20]:
            txt = " ".join(r.summary.strip().split())
            key = txt[:40]
            if txt and key not in seen_txt:
                seen_txt.add(key)
                candidates.append(txt[:160])

        prior = self._prior_profile_body(principal)
        body = await self._merge_profile_llm(
            prior,
            candidates,
            on_llm_call=on_llm_call,
        )
        if body is None:  # offline / no provider → deterministic dedup
            body = "\n".join(f"- {c}" for c in candidates[:12])
        profile = redact_secrets(f"{self._PROFILE_HEADER}\n{body}")

        # replace this principal's prior profile entry (idempotent). The profile
        # is identity memory, so it lives in the global store when enabled; clear
        # any stale copies from every store to stay idempotent after a migration.
        for store in self._stores():
            async with store.session() as s:
                old = (await s.execute(
                    select(MemoryEntryORM).where(
                        MemoryEntryORM.memory_type == "user_profile",
                        MemoryEntryORM.principal == principal,
                    )
                )).scalars().all()
                for o in old:
                    await s.delete(o)
                await s.commit()
        await self.record(
            layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
            summary=profile, memory_type="user_profile", importance=0.9, pinned=True,
            principal=principal,
        )
        # Owner-only: persist the global, cross-workspace profile file.
        if principal == PRINCIPAL_OWNER:
            try:
                from omni.memory.files import write_user_profile

                write_user_profile(self._settings.paths, body)
            except Exception as exc:  # noqa: BLE001
                logger.debug("profile.md write failed: %s", exc)
        return profile

    async def compact_memory_file(
        self,
        *,
        max_bullets: int = 20,
        keep: int = 14,
        on_llm_call: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> int:
        """LLM-merge an overgrown ``~/.omni/MEMORY.md`` into a concise list (P4).

        Owner-only hygiene mirroring Claude's "move detail out of the main file":
        once the personal memory file accumulates many bullets, fold duplicates
        and drop stale/contradictory ones so it stays readable. Safe by design —
        it only rewrites a *bullet-only* file (never one with hand-written prose)
        and only with a real provider (offline it can't merge, so it no-ops).
        Returns the new bullet count, or 0 when skipped.
        """
        if not self._settings.memory.enabled:
            return 0
        provider = (self._settings.model.provider or "mock").lower()
        if not self._llm or provider in ("mock", "", "offline"):
            return 0
        from omni.memory.files import read_user_memory_bullets, rewrite_user_memory

        bullets, safe = read_user_memory_bullets(self._settings.paths)
        if not safe or len(bullets) <= max_bullets:
            return 0
        system = (
            "Merge this list of durable preferences. Preserve the original language of every retained "
            "item. Remove duplicates, stale items, and contradictions; keep only stable collaboration "
            f"preferences. Return at most {keep} bullets in `- item` form with no commentary."
        )
        user = "\n".join(f"- {b}" for b in bullets)
        try:
            out = await self._llm.chat(system, user)
        except Exception as exc:  # noqa: BLE001
            logger.debug("MEMORY.md merge failed: %s", exc)
            return 0
        if on_llm_call is not None:
            try:
                await on_llm_call(system, user, out)
            except Exception:  # noqa: BLE001
                logger.debug("MEMORY.md merge observer failed", exc_info=True)
        merged: list[str] = []
        for raw in (out or "").splitlines():
            line = raw.lstrip("-•*· \t").strip()
            if len(line) >= 4:
                merged.append(redact_secrets(line)[:200])
        merged = merged[:keep]
        if not merged:
            return 0
        rewrite_user_memory(self._settings.paths, merged)
        return len(merged)

    def _prior_profile_body(self, principal: str) -> str:
        """The current profile text used as the 'prior' for diff-style merging.

        For the owner this is the human-editable ``profile.md`` (so manual edits
        are respected/merged); otherwise it's the principal's last stored
        ``user_profile`` summary (best-effort, never raises).
        """
        if principal == PRINCIPAL_OWNER:
            try:
                from omni.memory.files import load_user_profile

                return load_user_profile(self._settings.paths)
            except Exception:  # noqa: BLE001
                return ""
        return ""

    async def _merge_profile_llm(
        self,
        prior: str,
        candidates: list[str],
        *,
        on_llm_call: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> str | None:
        """LLM-merge prior profile + new observations into a concise bullet list.

        Diff-style forgetting: the model is told to drop stale/contradictory
        items and merge duplicates. Returns ``None`` offline (no real provider or
        no candidates) so the caller falls back to deterministic dedup.
        """
        provider = (self._settings.model.provider or "mock").lower()
        if not self._llm or provider in ("mock", "", "offline") or not candidates:
            return None
        prior_block = prior.strip() or "(none)"
        cand_block = "\n".join(f"- {c}" for c in candidates)
        system = (
            "Maintain a researcher's durable user profile. Merge duplicates and remove stale or "
            "contradictory items. Keep stable preferences, research directions, writing conventions, "
            "and tool habits. Preserve each item's original language; do not translate it. Return at "
            "most ten `- item` bullets with no commentary, or an empty response when nothing is durable."
        )
        user = f"Existing profile:\n{prior_block}\n\nNew observations:\n{cand_block}"
        try:
            out = await self._llm.chat(system, user)
        except Exception as exc:  # noqa: BLE001
            logger.debug("profile merge failed: %s", exc)
            return None
        if on_llm_call is not None:
            try:
                await on_llm_call(system, user, out)
            except Exception:  # noqa: BLE001
                logger.debug("profile merge observer failed", exc_info=True)
        bullets: list[str] = []
        for raw in (out or "").splitlines():
            line = raw.lstrip("-•*· \t").strip()
            if len(line) >= 4:
                bullets.append(f"- {line[:160]}")
        return "\n".join(bullets[:10]) or None


# ── helpers ──


def _parse_extracted_facts(raw: str, *, max_facts: int) -> list[ExtractedMemoryFact]:
    """Parse the model's typed JSON memory contract without language heuristics."""
    try:
        payload = json.loads((raw or "").strip() or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    facts: list[ExtractedMemoryFact] = []
    allowed_types = {"preference", "decision", "finding"}
    allowed_scopes = {"user", "project"}
    for item in payload:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        memory_type = str(item.get("type") or "finding").strip().lower()
        scope = str(item.get("scope") or "project").strip().lower()
        if len(text) < 5 or memory_type not in allowed_types or scope not in allowed_scopes:
            continue
        if memory_type == "preference":
            scope = "user"
        facts.append(ExtractedMemoryFact(text=text[:500], memory_type=memory_type, scope=scope))
        if len(facts) >= max_facts:
            break
    return facts


def _aware(dt: datetime) -> datetime:
    return ensure_aware(dt)


def _expired(r: MemoryEntryORM, now: datetime) -> bool:
    return bool(r.expires_at and _aware(r.expires_at) < now)


# CJK runs (Chinese/Japanese/Korean) carry no whitespace word boundaries, so a
# plain ``.split()`` yields one giant token and lexical overlap silently degrades
# to ~0 as the store grows. We tokenise CJK into overlapping character bigrams
# (single chars kept when a run is length 1) so Chinese queries recall grounded
# memories without requiring the embedding backend.
_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+")


def _tokenize(text: str) -> set[str]:
    lowered = (text or "").lower()
    tokens = {w for w in re.findall(r"[a-z0-9]+", lowered) if len(w) > 1}
    for run in _CJK_RE.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _keyword_overlap(query: str, text: str) -> float:
    q = _tokenize(query)
    t = _tokenize(text)
    if not q or not t:
        return 0.0
    return len(q & t) / len(q)


_ = time  # keep import for potential profiling hooks
