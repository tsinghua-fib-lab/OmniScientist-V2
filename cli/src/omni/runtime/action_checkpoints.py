"""Durable, resumable Action checkpoints (semantic clarification, V1).

The repository behind the admission pipeline's clarification phase: when a
critical field cannot be uniquely resolved (classically an ambiguous schedule
time), the grounded candidates are persisted here so the *original requester*
can pick one later — across turns, processes, or a daemon restart — instead of
the choice living only in a blocking in-memory turn.

Correctness invariants (all covered by offline tests):

* **Compare-and-set** on every transition (``state`` + ``version``) so a
  concurrent or replayed selection converges on a single result.
* **Replay idempotency**: re-selecting the same candidate returns the same
  outcome; a *different* candidate after resolution is a conflict, never a
  second result.
* **Fail-closed id lookup**: an ambiguous short-id prefix raises rather than
  silently acting on the first match.
* **Decider identity**: only the ``required_decider`` (the original requester,
  for a clarification) may resolve it — separate from owner *authorization*.
* **Expiry**: an open checkpoint past its TTL lapses instead of resolving.

The SHA-256 ``payload_fingerprint`` is drift detection only; authority is the DB
boundary + ``required_decider`` + the CAS transitions, never the hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from omni.storage.models import ActionCheckpointORM, _utcnow

PHASE_CLARIFICATION = "semantic_clarification"

# Clarification lifecycle (kept distinct from the authorization vocabulary).
STATE_OPEN = "open"
STATE_RESOLVED = "resolved"
STATE_CANCELLED = "cancelled"
STATE_EXPIRED = "expired"
STATE_SUPERSEDED = "superseded"

DEFAULT_TTL = timedelta(hours=6)


class AmbiguousCheckpointId(Exception):
    """A short id prefix matched more than one checkpoint (fail closed)."""


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def fingerprint(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """A detached snapshot of an :class:`ActionCheckpointORM` row."""

    id: str
    phase: str
    action_kind: str
    contract_version: str
    policy_version: str
    channel: str
    session_id: str
    actor_principal: str
    required_decider: str
    payload: dict[str, Any]
    resolution: dict[str, Any]
    state: str
    version: int
    idempotency_key: str
    decision: dict[str, Any]
    result_kind: str
    result_id: str
    expires_at: datetime | None
    created_at: datetime | None

    @classmethod
    def of(cls, row: ActionCheckpointORM) -> CheckpointRecord:
        return cls(
            id=row.id,
            phase=row.phase,
            action_kind=row.action_kind,
            contract_version=row.contract_version,
            policy_version=row.policy_version,
            channel=row.channel,
            session_id=row.session_id,
            actor_principal=row.actor_principal,
            required_decider=row.required_decider,
            payload=dict(row.payload_json or {}),
            resolution=dict(row.resolution_json or {}),
            state=row.state,
            version=int(row.version or 0),
            idempotency_key=row.idempotency_key,
            decision=dict(row.decision_json or {}),
            result_kind=row.result_kind,
            result_id=row.result_id,
            expires_at=_as_utc(row.expires_at),
            created_at=_as_utc(row.created_at),
        )

    def candidate(self, candidate_id: str) -> dict[str, Any] | None:
        for cand in self.resolution.get("candidates", []) or []:
            if str(cand.get("id")) == str(candidate_id):
                return dict(cand)
        return None

    @property
    def candidate_ids(self) -> list[str]:
        return [str(c.get("id")) for c in (self.resolution.get("candidates") or [])]


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    """The result of attempting to resolve a clarification checkpoint."""

    status: str  # resolved | replayed | conflict | missing | forbidden | expired | closed | invalid_candidate
    record: CheckpointRecord | None = None
    candidate: dict[str, Any] | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"resolved", "replayed"}


class ActionCheckpointStore:
    """DB-backed repository for durable clarification checkpoints."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def open_clarification(
        self,
        *,
        action_kind: str,
        contract_version: str,
        policy_version: str,
        channel: str,
        session_id: str,
        actor_principal: str,
        required_decider: str = "",
        origin_project_dir: str = "",
        project: str = "default",
        payload: dict[str, Any],
        resolution: dict[str, Any],
        idempotency_key: str = "",
        ttl: timedelta = DEFAULT_TTL,
    ) -> CheckpointRecord:
        """Persist an ``open`` clarification, deduping an identical live draft.

        Dedup key: an explicit ``idempotency_key``, else the (required_decider +
        payload fingerprint) of an already-open, unexpired checkpoint — so the
        model re-proposing the same ambiguous request converges on one draft.
        """
        decider = required_decider or actor_principal
        fp = fingerprint(payload)
        now = _utcnow()
        async with self._db.session() as s:
            existing = await self._find_open(s, decider, fp, idempotency_key, now)
            if existing is not None:
                return CheckpointRecord.of(existing)
            row = ActionCheckpointORM(
                phase=PHASE_CLARIFICATION,
                action_kind=action_kind,
                contract_version=contract_version,
                policy_version=policy_version,
                project=project,
                origin_project_dir=origin_project_dir,
                channel=channel,
                session_id=session_id,
                actor_principal=actor_principal,
                required_decider=decider,
                payload_json=dict(payload),
                payload_fingerprint=fp,
                resolution_json=dict(resolution),
                state=STATE_OPEN,
                version=0,
                idempotency_key=idempotency_key,
                expires_at=now + ttl,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return CheckpointRecord.of(row)

    async def _find_open(
        self, s: Any, decider: str, fp: str, idem: str, now: datetime
    ) -> ActionCheckpointORM | None:
        q = select(ActionCheckpointORM).where(
            ActionCheckpointORM.phase == PHASE_CLARIFICATION,
            ActionCheckpointORM.state == STATE_OPEN,
        )
        for row in (await s.execute(q)).scalars().all():
            exp = _as_utc(row.expires_at)
            if exp is not None and exp <= now:
                continue
            if idem and row.idempotency_key == idem:
                return row
            if row.required_decider == decider and row.payload_fingerprint == fp:
                return row
        return None

    async def get(self, id_or_prefix: str) -> CheckpointRecord | None:
        """Resolve a full id or a **unique** short prefix (fail closed on ties)."""
        key = (id_or_prefix or "").strip()
        if not key:
            return None
        async with self._db.session() as s:
            exact = await s.get(ActionCheckpointORM, key)
            if exact is not None:
                return CheckpointRecord.of(exact)
            rows = [
                r
                for r in (await s.execute(select(ActionCheckpointORM))).scalars().all()
                if r.id.startswith(key)
            ]
            if len(rows) > 1:
                raise AmbiguousCheckpointId(
                    f"'{key}' matches {len(rows)} checkpoints; use a longer id."
                )
            return CheckpointRecord.of(rows[0]) if rows else None

    async def list_open(
        self,
        *,
        principal: str | None = None,
        session_id: str = "",
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[CheckpointRecord]:
        """Open, unexpired checkpoints (newest first).

        When ``principal`` is set, only rows that principal may resolve are
        returned (conversation admission). Pass ``principal=None`` for owner
        observability across channels (e.g. ``schedule clarifications``).
        """
        moment = now or _utcnow()
        async with self._db.session() as s:
            clauses = [
                ActionCheckpointORM.phase == PHASE_CLARIFICATION,
                ActionCheckpointORM.state == STATE_OPEN,
            ]
            if principal is not None:
                clauses.append(ActionCheckpointORM.required_decider == principal)
            q = (
                select(ActionCheckpointORM)
                .where(*clauses)
                .order_by(ActionCheckpointORM.created_at.desc())
            )
            out: list[CheckpointRecord] = []
            for row in (await s.execute(q)).scalars().all():
                exp = _as_utc(row.expires_at)
                if exp is not None and exp <= moment:
                    continue
                if session_id and row.session_id and row.session_id != session_id:
                    continue
                out.append(CheckpointRecord.of(row))
                if len(out) >= max(1, limit):
                    break
            return out

    async def resolve(
        self,
        id_or_prefix: str,
        *,
        candidate_id: str,
        decider: str,
        decision: dict[str, Any] | None = None,
    ) -> ResolveOutcome:
        """CAS ``open → resolved`` selecting a candidate; idempotent on replay."""
        record = await self.get(id_or_prefix)
        if record is None:
            return ResolveOutcome(status="missing", reason=f"No checkpoint matches '{id_or_prefix}'.")
        if record.required_decider and decider != record.required_decider:
            return ResolveOutcome(
                status="forbidden",
                record=record,
                reason="only the original requester may answer this clarification",
            )
        if record.state == STATE_RESOLVED:
            # Replay vs conflict: same choice converges, a different one does not.
            chosen = str(record.decision.get("candidate_id", ""))
            if chosen == str(candidate_id):
                return ResolveOutcome(
                    status="replayed", record=record, candidate=record.candidate(candidate_id)
                )
            return ResolveOutcome(
                status="conflict", record=record, reason=f"already resolved as '{chosen}'"
            )
        if record.state != STATE_OPEN:
            return ResolveOutcome(status="closed", record=record, reason=f"checkpoint is {record.state}")
        exp = record.expires_at
        if exp is not None and exp <= _utcnow():
            await self._transition(record.id, STATE_OPEN, record.version, STATE_EXPIRED)
            return ResolveOutcome(status="expired", record=record, reason="checkpoint expired")
        candidate = record.candidate(candidate_id)
        if candidate is None:
            return ResolveOutcome(
                status="invalid_candidate",
                record=record,
                reason=f"'{candidate_id}' is not one of {record.candidate_ids}",
            )

        decision_json = {"candidate_id": str(candidate_id), **(decision or {})}
        won = await self._transition(
            record.id, STATE_OPEN, record.version, STATE_RESOLVED, decision_json=decision_json
        )
        if won:
            fresh = await self.get(record.id)
            return ResolveOutcome(status="resolved", record=fresh, candidate=candidate)
        # Lost the race: re-read and report replay/conflict deterministically.
        fresh = await self.get(record.id)
        if fresh is not None and fresh.state == STATE_RESOLVED:
            if str(fresh.decision.get("candidate_id", "")) == str(candidate_id):
                return ResolveOutcome(status="replayed", record=fresh, candidate=candidate)
            return ResolveOutcome(status="conflict", record=fresh, reason="resolved concurrently")
        return ResolveOutcome(status="closed", record=fresh, reason="checkpoint changed concurrently")

    async def cancel(self, id_or_prefix: str, *, decider: str) -> ResolveOutcome:
        record = await self.get(id_or_prefix)
        if record is None:
            return ResolveOutcome(status="missing", reason=f"No checkpoint matches '{id_or_prefix}'.")
        if record.required_decider and decider != record.required_decider:
            return ResolveOutcome(status="forbidden", record=record)
        if record.state != STATE_OPEN:
            return ResolveOutcome(status="closed", record=record, reason=f"checkpoint is {record.state}")
        await self._transition(record.id, STATE_OPEN, record.version, STATE_CANCELLED)
        return ResolveOutcome(status="resolved", record=await self.get(record.id))

    async def supersede(self, id_or_prefix: str) -> bool:
        record = await self.get(id_or_prefix)
        if record is None or record.state != STATE_OPEN:
            return False
        return await self._transition(record.id, STATE_OPEN, record.version, STATE_SUPERSEDED)

    async def attach_result(
        self, id_or_prefix: str, *, result_kind: str, result_id: str
    ) -> bool:
        """Record where a resolved checkpoint materialised (idempotent convergence)."""
        record = await self.get(id_or_prefix)
        if record is None or record.state != STATE_RESOLVED:
            return False
        if record.result_id:
            return record.result_id == result_id
        return await self._transition(
            record.id,
            STATE_RESOLVED,
            record.version,
            STATE_RESOLVED,
            result_kind=result_kind,
            result_id=result_id,
        )

    async def expire_due(self, *, now: datetime | None = None) -> int:
        """Lapse every open checkpoint past its TTL; returns how many."""
        moment = now or _utcnow()
        async with self._db.session() as s:
            result = await s.execute(
                update(ActionCheckpointORM)
                .where(
                    ActionCheckpointORM.state == STATE_OPEN,
                    ActionCheckpointORM.expires_at.is_not(None),
                    ActionCheckpointORM.expires_at <= moment,
                )
                .values(state=STATE_EXPIRED, version=ActionCheckpointORM.version + 1)
            )
            await s.commit()
            return int(result.rowcount or 0)

    async def _transition(
        self,
        checkpoint_id: str,
        expected_state: str,
        expected_version: int,
        new_state: str,
        *,
        decision_json: dict[str, Any] | None = None,
        result_kind: str | None = None,
        result_id: str | None = None,
    ) -> bool:
        """Optimistic compare-and-set; returns True iff this call made the change."""
        values: dict[str, Any] = {"state": new_state, "version": expected_version + 1}
        if decision_json is not None:
            values["decision_json"] = decision_json
        if result_kind is not None:
            values["result_kind"] = result_kind
        if result_id is not None:
            values["result_id"] = result_id
        async with self._db.session() as s:
            result = await s.execute(
                update(ActionCheckpointORM)
                .where(
                    ActionCheckpointORM.id == checkpoint_id,
                    ActionCheckpointORM.state == expected_state,
                    ActionCheckpointORM.version == expected_version,
                )
                .values(**values)
            )
            await s.commit()
            return int(result.rowcount or 0) == 1


__all__ = [
    "PHASE_CLARIFICATION",
    "STATE_OPEN",
    "STATE_RESOLVED",
    "STATE_CANCELLED",
    "STATE_EXPIRED",
    "STATE_SUPERSEDED",
    "AmbiguousCheckpointId",
    "CheckpointRecord",
    "ResolveOutcome",
    "ActionCheckpointStore",
    "fingerprint",
]
