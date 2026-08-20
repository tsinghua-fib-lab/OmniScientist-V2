"""Session + transcript persistence for agent turns.

The orchestrator decides *what* happens in a turn; this store owns the durable
conversation state around it: sessions (create/list/get/fork/touch/delete), the
message transcript, the compaction-aware prompt history the ReAct loop consumes,
and the per-session memory-principal cache. It is a narrow collaborator — it depends only
on the workspace database plus the project name and channel-identity policy — so
the turn logic never reaches into SQLAlchemy directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError

from omni.agent.session_ops import copy_session_branch
from omni.memory.service import principal_of as _principal_of
from omni.storage.db import Database, retry_while_busy, sqlite_busy
from omni.storage.models import (
    MESSAGE_ORDER_ASC,
    MESSAGE_ORDER_DESC,
    ConversationMessageORM,
    SessionFocusORM,
    SessionORM,
    _utcnow,
)


@dataclass(frozen=True)
class SessionDeleteOutcome:
    """Result of deleting a conversation thread and its associated turns."""

    session_id: str
    deleted: bool
    deleted_task_ids: tuple[str, ...] = ()
    code: str = ""
    message: str = ""

logger = logging.getLogger(__name__)

# Owner / CLI identity for memory isolation (see MemoryService.PRINCIPAL_OWNER).
_PRINCIPAL_OWNER = "local"

# Web persona.start reuses this hidden session so protocol turns never join a
# research transcript. CLI/Agent listings and resume must not surface it.
PERSONA_CONTROL_EXTERNAL_KEY = "persona-control"


class ConversationStore:
    """Sessions, transcript, prompt history, and the principal cache."""

    def __init__(self, db: Database, *, project_name: str, channel_identity: str) -> None:
        self._db = db
        self._project_name = project_name
        self._channel_identity = channel_identity
        # session_id → memory principal ("local" or "<channel>:<external_key>"):
        # isolates per-identity memory without threading external_key everywhere.
        self._session_principal: dict[str, str] = {}

    async def ensure_session(
        self, *, channel: str = "cli", external_key: str = "", reuse_latest: bool = False,
        title: str = "",
    ) -> str:
        async with self._db.session() as s:
            if external_key or reuse_latest:
                q = select(SessionORM).where(
                    SessionORM.channel == channel, SessionORM.status == "active"
                ).order_by(SessionORM.updated_at.desc())
                if external_key:
                    q = q.where(SessionORM.external_key == external_key)
                existing = (await s.execute(q)).scalars().first()
                if existing:
                    self._session_principal[existing.id] = self.principal_of(channel, external_key)
                    return existing.id
            row = SessionORM(
                project=self._project_name, channel=channel,
                external_key=external_key, title=title or "",
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            self._session_principal[row.id] = self.principal_of(channel, external_key)
            return row.id

    def principal_of(self, channel: str, external_key: str) -> str:
        """``principal_of`` bound to this instance's ``memory.channel_identity``."""
        return _principal_of(channel, external_key, channel_identity=self._channel_identity)

    async def principal_for_session(self, session_id: str) -> str:
        """Resolve the memory principal for ``session_id`` (cached; reads the row
        on a cold cache, e.g. after a daemon restart)."""
        if not session_id:
            return _PRINCIPAL_OWNER
        cached = self._session_principal.get(session_id)
        if cached is not None:
            return cached
        try:
            async with self._db.session() as s:
                row = await s.get(SessionORM, session_id)
        except Exception:  # noqa: BLE001
            row = None
        principal = self.principal_of(row.channel, row.external_key) if row is not None else _PRINCIPAL_OWNER
        self._session_principal[session_id] = principal
        return principal

    async def recent_rows(self, session_id: str) -> list[ConversationMessageORM]:
        """Last ≤400 messages for ``session_id``, chronological."""
        async with self._db.session() as s:
            rows = (await s.execute(
                select(ConversationMessageORM)
                .where(ConversationMessageORM.session_id == session_id)
                .order_by(*MESSAGE_ORDER_DESC).limit(400)
            )).scalars().all()
        return list(reversed(rows))

    @staticmethod
    def normal_rows(rows: list[ConversationMessageORM]) -> list[ConversationMessageORM]:
        """Non-compacted user/assistant turns (excludes bridge + hidden rows)."""
        return [
            r for r in rows
            if r.role in {"user", "assistant"}
            and not (r.meta or {}).get("compacted")
            and (r.content_type or "") != "compaction"
        ]

    async def history(self, session_id: str, limit: int = 12) -> list[dict[str, Any]]:
        """Compaction-aware prompt history: latest bridge + last ``limit`` turns
        (``compacted`` rows are hidden — kept for replay, already in the bridge).

        The bridge is handed over as the user's, the way Codex does it. Spoken in
        the assistant's voice it reads as something the model itself concluded,
        and a model holds to its own prior claims: one session's bridge said the
        research was finished and both reports were stored, and that was still
        being restated turns later on a request that had produced neither. As
        material it is evidence to be used; as its own words it is a position to
        defend.
        """
        rows = await self.recent_rows(session_id)
        comps = [r for r in rows if not (r.meta or {}).get("compacted")
                 and (r.content_type or "") == "compaction"]
        out: list[dict[str, Any]] = []
        if comps:
            out.append({"role": "user", "content": comps[-1].content})
        out += [{"role": r.role, "content": r.content}
                for r in self.normal_rows(rows)[-limit:]]
        return out

    async def extraction_history(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        """User/assistant turns *with ``meta``* for extraction, so the extractor
        can skip degraded/partial/tool-limit and pure-retrieval turns (P4)."""
        rows = self.normal_rows(await self.recent_rows(session_id))
        return [
            {"role": r.role, "content": r.content, "meta": dict(r.meta or {})}
            for r in rows[-limit:]
        ]

    async def visible_normal_messages(self, session_id: str) -> list[ConversationMessageORM]:
        """All non-compacted user/assistant/tool-result rows, chronological."""
        async with self._db.session() as s:
            rows = (await s.execute(
                select(ConversationMessageORM)
                .where(ConversationMessageORM.session_id == session_id)
                .order_by(*MESSAGE_ORDER_ASC)
            )).scalars().all()
        return [
            r for r in rows
            if not (r.meta or {}).get("compacted") and (r.content_type or "") != "compaction"
        ]

    async def persist_message(self, session_id: str, role: str, content: str, **meta: Any) -> None:
        """Write one transcript row. A locked store must not fail the turn.

        Cancel already drops advisory events when the aiosqlite worker still
        holds the file lock. The assistant row is the same class of write:
        three short retries, then drop — the turn result is already in memory.
        """

        async def write() -> None:
            async with self._db.session() as s:
                # Read the session before adding the message so a SELECT cannot
                # autoflush a pending INSERT into a locked writer.
                row = await s.get(SessionORM, session_id)
                s.add(ConversationMessageORM(
                    session_id=session_id, role=role, content=content, meta=meta or {},
                ))
                if row is not None:
                    row.updated_at = _utcnow()
                await s.commit()

        try:
            await retry_while_busy(write, attempts=3)
        except OperationalError as exc:
            if not sqlite_busy(exc):
                raise
            logger.warning(
                "conversation.message.busy session=%s role=%s dropped",
                session_id[:8],
                role,
            )

    async def write_compaction_bridge(
        self, session_id: str, bridge: str, covered: list[str]
    ) -> None:
        """Persist the compaction bridge row and hide the covered turns.

        The covered rows are kept for replay (``compacted=True``) rather than
        deleted; the bridge summary stands in for them in the prompt history.
        Stored under the role it is replayed under — see :meth:`history`.
        """
        async with self._db.session() as s:
            s.add(ConversationMessageORM(
                session_id=session_id, role="user", content_type="compaction",
                content=bridge,
                meta={"kind": "compaction", "covered": covered, "count": len(covered)},
            ))
            hidden = (await s.execute(
                select(ConversationMessageORM).where(ConversationMessageORM.id.in_(covered))
            )).scalars().all()
            for r in hidden:
                meta = dict(r.meta or {})
                meta["compacted"] = True
                r.meta = meta
            await s.commit()

    async def list_sessions(self, *, limit: int = 30) -> list[tuple[SessionORM, int]]:
        """Sessions in this workspace, newest first, with message counts.

        No ``project`` filter: with path-keyed per-workspace DBs the database
        file *is* the workspace boundary, so every session in it belongs here.
        """
        async with self._db.session() as s:
            counts = (
                select(
                    ConversationMessageORM.session_id,
                    func.count().label("n"),
                )
                .group_by(ConversationMessageORM.session_id)
                .subquery()
            )
            q = (
                select(SessionORM, counts.c.n)
                .join(counts, counts.c.session_id == SessionORM.id, isouter=True)
                .where(SessionORM.external_key != PERSONA_CONTROL_EXTERNAL_KEY)
                .order_by(SessionORM.updated_at.desc())
                .limit(limit)
            )
            rows = (await s.execute(q)).all()
        return [(row[0], int(row[1] or 0)) for row in rows]

    async def get_session(self, session_id: str) -> SessionORM | None:
        """Resolve a session by exact id or unique prefix (within this workspace)."""
        async with self._db.session() as s:
            exact = (
                await s.execute(select(SessionORM).where(SessionORM.id == session_id))
            ).scalar_one_or_none()
            if exact is not None:
                return exact
            rows = (
                await s.execute(
                    select(SessionORM).order_by(SessionORM.updated_at.desc())
                )
            ).scalars().all()
        for row in rows:
            if row.id.startswith(session_id):
                return row
        return None

    async def session_messages(self, session_id: str) -> list[ConversationMessageORM]:
        """All messages for a session in chronological order."""
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(ConversationMessageORM)
                    .where(ConversationMessageORM.session_id == session_id)
                    .order_by(*MESSAGE_ORDER_ASC)
                )
            ).scalars().all()
        return list(rows)

    async def touch_session(self, session_id: str) -> bool:
        """Bump ``updated_at`` (and re-activate) so ``--continue`` picks it."""
        async with self._db.session() as s:
            row = (
                await s.execute(select(SessionORM).where(SessionORM.id == session_id))
            ).scalar_one_or_none()
            if row is None:
                return False
            row.updated_at = _utcnow()
            row.status = "active"
            await s.commit()
        return True

    async def fork_session(
        self, session_id: str, *, up_to_message: str = "", title: str = "",
    ) -> str | None:
        """Branch a session into a new one, copying its transcript (P2).

        Delegates the transcript copy to
        :func:`omni.agent.session_ops.copy_session_branch` and warms the branch's
        principal cache. Returns the new id, or ``None`` if the source is missing.
        """
        src = await self.get_session(session_id)
        if src is None:
            return None
        new_id = await copy_session_branch(
            self._db, self._project_name, src,
            up_to_message=up_to_message, title=title,
        )
        self._session_principal[new_id] = self.principal_of(src.channel, src.external_key)
        return new_id

    async def set_session_title(self, session_id: str, title: str) -> SessionORM | None:
        """Set the owner-authored title without bumping ``updated_at``.

        ``SessionORM.updated_at`` has ``onupdate=_utcnow``. A Core UPDATE that
        only writes ``title`` leaves that column alone so a rename is not
        mistaken for recent activity.
        """
        async with self._db.session() as s:
            row = await s.get(SessionORM, session_id)
            if row is None:
                return None
            kept_updated_at = row.updated_at
            await s.execute(
                update(SessionORM)
                .where(SessionORM.id == session_id)
                .values(title=title, updated_at=kept_updated_at)
            )
            await s.commit()
        return await self.get_session(session_id)

    async def delete_session(self, session_id: str) -> bool:
        """Remove the session row, its transcript, and session-focus pointers.

        Tasks are *not* deleted here: they are a separate durable record and
        must go through :meth:`omni.runtime.task_recorder.TaskRecorder.delete_tasks`
        so active work stays fail-closed and artifact files survive.
        """
        row = await self.get_session(session_id)
        if row is None:
            return False
        sid = row.id
        async with self._db.session() as s:
            await s.execute(
                delete(ConversationMessageORM).where(
                    ConversationMessageORM.session_id == sid
                )
            )
            await s.execute(
                delete(SessionFocusORM).where(SessionFocusORM.session_id == sid)
            )
            await s.execute(delete(SessionORM).where(SessionORM.id == sid))
            await s.commit()
        self._session_principal.pop(sid, None)
        return True
