"""Async SQLite engine/session management.

One physical SQLite file per project holds every structured table
(sessions, messages, tasks, subtasks, memory_entries, artifacts). WAL mode is
enabled so a long-running ``omni serve`` daemon and short-lived ``omni``
commands can read/write concurrently.

A store whose generation, watermark, and ORM shape already match this build
opens as a **reader**: inspect commands such as ``/task show`` must not take
the SQLite write lock. Additive DDL and a one-shot artifact-owner backfill run
only when the on-disk shape is actually behind. A busy lock is a queue, never
a signal to drop the schema.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TypeVar

from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from omni.storage.models import Base

logger = logging.getLogger(__name__)

T = TypeVar("T")

_DATABASES: dict[str, Database] = {}
# Current store shape (tasks / workflows / steps / subtasks / schedules / …).
# ``PRAGMA application_id`` marks this generation so older vocabularies
# (e.g. agent_runs/skill_tasks) are snapshotted and rebuilt rather than
# carried forward. ``PRAGMA user_version`` is a single baseline watermark —
# kept at 1 until a future compatibility cut needs a numbered boundary.
# Missing columns/tables/indexes are reconciled additively on every init;
# adding a column does **not** require bumping this watermark.
_SCHEMA_VERSION = 1
_SCHEMA_GENERATION = 0x4F4D4E33  # current-generation marker ("OMN3")

# Data-preserving column renames applied during additive migration, keyed by
# table → [(old_name, new_name), …]. SQLite ≥3.25 ``RENAME COLUMN`` keeps the
# data in place, so a rename never loses rows (unlike a drop+recreate).
# Empty for the current baseline (a legacy store is rebuilt, not renamed).
_COLUMN_RENAMES: dict[str, list[tuple[str, str]]] = {}

# How many pre-migration snapshots to keep per database file (older pruned).
_BACKUP_KEEP = 5


class Database:
    """A SQLAlchemy async engine bound to one SQLite file."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = path
        self._busy_timeout_ms = max(0, int(busy_timeout_ms))
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{path}"
        # aiosqlite's connect event often never applies PRAGMA busy_timeout, so
        # the sqlite3 timeout is the wait that actually runs. Cap it so a
        # cancel persist can retry inside a short turn instead of blocking 5s.
        timeout_s = max(0.05, min(1.0, self._busy_timeout_ms / 1000.0))
        timeout_ms = int(timeout_s * 1000)
        self.engine = create_async_engine(
            url, echo=False, future=True, connect_args={"timeout": timeout_s}
        )
        self._sessionmaker = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self._initialized = False

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn, _rec):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA synchronous=NORMAL")
            # Match the connect timeout. A 5s PRAGMA plus 15 retries turns a
            # cancel persist into a 75s tool failure on Linux, where the
            # pragma actually applies.
            cur.execute(f"PRAGMA busy_timeout={timeout_ms}")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    async def init(self) -> None:
        """Open the store, reconciling its schema to this codebase.

        Within the current schema generation (``PRAGMA application_id``),
        missing columns/tables/indexes are added in place. Column adds do not
        bump ``user_version``; the watermark stays at the single baseline until
        a future compatibility cut needs a boundary.

        When the on-disk generation, watermark, journal, and ORM shape already
        match this build, init is a read of sqlite_master / PRAGMA and returns
        without a write transaction — so ``omni serve`` can keep writing while
        a CLI inspects the same file.

        A store whose watermark is *ahead* of this build is left intact
        (forward-compatible), never rebuilt. A store from an **older
        generation** (different ``application_id``) is snapshotted and rebuilt
        from scratch — a deliberate clean break rather than a lossless
        migration. A busy lock during a needed write is retried, then raised;
        it never drops the schema.
        """
        if self._initialized:
            return
        # Fail closed on a missing parent rather than ``unable to open database
        # file`` from sqlite (common when a caller opens a brand-new OMNI_HOME).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _database_init_lock(self.path):
            dialect = self.engine.sync_engine.dialect
            async with self.engine.connect() as conn:
                stored, generation, has_tables, journal = await _read_store_fingerprint(conn)
                legacy = has_tables and generation != _SCHEMA_GENERATION
                normalize_watermark = (
                    has_tables and not legacy and stored > _SCHEMA_VERSION and stored < 100
                )
                future = has_tables and not legacy and stored >= 100
                needs_ddl = (not has_tables) or legacy or await _schema_needs_ddl(conn)
            needs_write = needs_ddl or (
                has_tables
                and not future
                and (stored != _SCHEMA_VERSION or normalize_watermark or journal != "wal")
            )
            if not needs_write:
                self._initialized = True
                return
            backup: Path | None = None
            if has_tables and (legacy or stored != _SCHEMA_VERSION):
                backup = await self._backup(stored)

            async def _write() -> None:
                async with self.engine.begin() as conn:
                    await _apply_schema_writes(
                        conn,
                        dialect=dialect,
                        legacy=legacy,
                        future=future,
                        has_tables=has_tables,
                        needs_ddl=needs_ddl,
                        stored=stored,
                        backup=backup,
                    )

            try:
                await retry_while_busy(_write)
            except OperationalError as exc:
                if sqlite_busy(exc):
                    logger.warning(
                        "Schema update deferred; another process holds the write lock on %s",
                        self.path,
                    )
                raise
        self._initialized = True

    async def _backup(self, stored_version: int) -> Path | None:
        """Snapshot the SQLite file before a structural change; return its path.

        Uses ``VACUUM INTO`` (a consistent single-file copy even under WAL) so a
        botched upgrade is always recoverable. Best-effort: a failed backup logs
        and returns ``None`` rather than blocking the upgrade.
        """
        backup_dir = self.path.parent / "backups"
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        dest = backup_dir / f"{self.path.stem}.v{stored_version}.{ts}{self.path.suffix}"
        try:
            await asyncio.to_thread(_snapshot_sqlite, self.path, dest)
        except Exception as exc:  # noqa: BLE001 - never let backup abort startup.
            logger.warning("Pre-upgrade schema backup failed; continuing upgrade: %s", exc)
            return None
        await asyncio.to_thread(_prune_backups, backup_dir, self.path.stem, self.path.suffix, _BACKUP_KEEP)
        logger.info("Backed up the store to %s", dest)
        return dest

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session

    async def healthcheck(self) -> bool:
        try:
            async with self.session() as s:
                await s.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()


def sqlite_busy(exc: BaseException) -> bool:
    """True when SQLite refused a write because another writer holds the lock."""
    detail = str(getattr(exc, "orig", exc)).lower()
    return "locked" in detail or "busy" in detail


_BUSY_RETRY_ATTEMPTS: ContextVar[int | None] = ContextVar(
    "omni_busy_retry_attempts", default=None
)


@contextmanager
def busy_retry_budget(attempts: int) -> Iterator[None]:
    """Cap nested :func:`retry_while_busy` calls for a fail-fast cancel persist."""
    token = _BUSY_RETRY_ATTEMPTS.set(max(1, int(attempts)))
    try:
        yield
    finally:
        _BUSY_RETRY_ATTEMPTS.reset(token)


async def retry_while_busy(
    write: Callable[[], Awaitable[T]],
    *,
    attempts: int = 15,
    backoff_seconds: float = 0.05,
) -> T:
    """Run ``write`` again while SQLite says another writer holds the lock.

    SQLite admits one writer at a time. A cancelled skill may still hold the
    file lock on its aiosqlite worker thread after the asyncio task is gone —
    Windows keeps that lock long enough that five short retries lose and the
    run stays ``running``. Cap each sleep so the queue fits inside a 2s turn.
    Only the caller knows whether replaying its write is the same write.
    A cancel persist installs :func:`busy_retry_budget` so this queue cannot
    replace ``CancelledError`` with a 15-second tool failure.
    """
    cap = _BUSY_RETRY_ATTEMPTS.get()
    if cap is not None:
        attempts = min(attempts, cap)
        backoff_seconds = min(backoff_seconds, 0.02)
    for attempt in range(attempts):
        try:
            return await write()
        except OperationalError as exc:
            if not sqlite_busy(exc) or attempt == attempts - 1:
                raise
            await asyncio.sleep(min(0.08, backoff_seconds * (attempt + 1)))
    raise AssertionError("unreachable: the loop above either returns or raises")


def get_database(path: Path) -> Database:
    """Return a cached :class:`Database` for ``path`` (one per file)."""
    key = str(path.resolve())
    db = _DATABASES.get(key)
    if db is None:
        db = Database(path)
        _DATABASES[key] = db
    return db


async def reset_databases() -> None:
    """Dispose and clear the cache (used by tests)."""
    for db in list(_DATABASES.values()):
        await db.dispose()
    _DATABASES.clear()


async def dispose_databases_under(root: Path) -> None:
    """Dispose cached engines whose SQLite files live below ``root``.

    Evaluation attempts use independent temporary roots and may run in
    parallel, so a process-wide reset would close another active attempt.
    Scoped disposal releases Windows file handles before each temporary
    directory is removed without disturbing databases owned by other roots.
    """
    resolved_root = root.resolve()
    for key, db in list(_DATABASES.items()):
        if not Path(key).is_relative_to(resolved_root):
            continue
        cached = _DATABASES.pop(key, None)
        if cached is db:
            await db.dispose()


def code_schema_version() -> int:
    """On-disk schema watermark this build stamps (see ``_SCHEMA_VERSION``)."""
    return _SCHEMA_VERSION


async def read_stored_schema_version(db: Database) -> int | None:
    """Read the DB's on-disk ``PRAGMA user_version`` without touching the ORM.

    Deliberately raw SQL: this is how a long-lived process detects that another
    (newer) Omni build advanced the store underneath it, so it must work even
    when the ORM and the physical schema have diverged (a renamed/removed column
    would make any ORM query raise). Returns ``None`` when it can't be read.
    """
    try:
        async with db.engine.connect() as conn:
            result = await conn.execute(text("PRAGMA user_version"))
            return int(result.scalar_one() or 0)
    except Exception:  # noqa: BLE001 - never let a liveness probe crash the caller.
        return None


async def schema_drifted(db: Database) -> bool:
    """True when the on-disk watermark is ahead of this process's build.

    Signals that a newer Omni build advanced the store while this (now-stale)
    process kept running — so its ORM no longer matches the physical tables and
    it should restart onto the new code instead of erroring on every query. A
    fresh/unversioned store (``0``), the current baseline, or an unreadable
    watermark is treated as "no drift" so the probe fails safe.
    """
    stored = await read_stored_schema_version(db)
    return stored is not None and stored > _SCHEMA_VERSION


def _snapshot_sqlite(src: Path, dest: Path) -> None:
    """Write a consistent copy of ``src`` to ``dest`` via ``VACUUM INTO``.

    Runs on a stdlib ``sqlite3`` connection in autocommit mode (VACUUM cannot run
    inside a transaction). ``VACUUM INTO`` produces a single clean file even with
    WAL enabled and concurrent readers.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    escaped = str(dest).replace("'", "''")
    con = sqlite3.connect(str(src), isolation_level=None)
    try:
        con.execute(f"VACUUM INTO '{escaped}'")
    finally:
        con.close()


def _prune_backups(backup_dir: Path, stem: str, suffix: str, keep: int) -> None:
    """Keep only the ``keep`` most-recent snapshots for one database file."""
    try:
        snaps = sorted(
            backup_dir.glob(f"{stem}.v*{suffix}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for old in snaps[max(0, keep):]:
        try:
            old.unlink()
        except OSError:
            pass


async def _read_store_fingerprint(conn) -> tuple[int, int, bool, str]:  # noqa: ANN001
    """Return ``(user_version, application_id, has_tables, journal_mode)``."""
    stored = int((await conn.execute(text("PRAGMA user_version"))).scalar_one() or 0)
    generation = int((await conn.execute(text("PRAGMA application_id"))).scalar_one() or 0)
    has_tables = (
        await conn.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' LIMIT 1"
            )
        )
    ).scalar_one_or_none() is not None
    journal = str((await conn.execute(text("PRAGMA journal_mode"))).scalar_one() or "").lower()
    return stored, generation, has_tables, journal


async def _schema_needs_ddl(conn) -> bool:  # noqa: ANN001
    """True when the ORM has a table, column, rename, or index the file lacks.

    Pure reads of sqlite_master / PRAGMA table_info. Callers that get False
    can skip the write transaction entirely.
    """
    tables = await _existing_table_names(conn)
    for table, renames in _COLUMN_RENAMES.items():
        if table not in tables:
            continue
        cols = await _table_column_names(conn, table)
        for old, new in renames:
            if old in cols and new not in cols:
                return True
    indexes = await _existing_index_names(conn)
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            return True
        existing = await _table_column_names(conn, table.name)
        for col in table.columns:
            if col.name not in existing and not col.primary_key:
                return True
        for idx in table.indexes:
            if idx.name and idx.name not in indexes:
                return True
    return False


async def _apply_schema_writes(
    conn,  # noqa: ANN001
    *,
    dialect: object,
    legacy: bool,
    future: bool,
    has_tables: bool,
    needs_ddl: bool,
    stored: int,
    backup: Path | None,
) -> None:
    """Apply WAL, DDL, and watermarks. Never drops the store on a busy lock."""
    await conn.execute(text("PRAGMA journal_mode=WAL"))
    if legacy:
        await _drop_sqlite_schema(conn)
        await conn.run_sync(Base.metadata.create_all)
        logger.warning(
            "Rebuilt legacy store as the current schema "
            "(previous watermark=%d); snapshot: %s",
            stored,
            backup or "unavailable",
        )
    elif needs_ddl:
        await conn.run_sync(Base.metadata.create_all)
        if future:
            logger.warning(
                "Local store watermark is ahead of this build (%d); "
                "preserving the additive store while ensuring known tables exist.",
                stored,
            )
        elif has_tables:
            await _migrate_schema_additively(conn, dialect)
            if stored != _SCHEMA_VERSION:
                backup_note = f" (pre-reconcile backup: {backup})" if backup else ""
                logger.info(
                    "Reconciled the local schema in place (watermark %d → %d)%s",
                    stored,
                    _SCHEMA_VERSION,
                    backup_note,
                )
    await conn.execute(text(f"PRAGMA application_id = {_SCHEMA_GENERATION}"))
    if not future:
        await conn.execute(text(f"PRAGMA user_version = {_SCHEMA_VERSION}"))


async def _existing_table_names(conn) -> set[str]:  # noqa: ANN001
    rows = (
        await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        )
    ).fetchall()
    return {str(r[0]) for r in rows}


async def _table_column_names(conn, table: str) -> set[str]:  # noqa: ANN001
    rows = (await conn.execute(text(f'PRAGMA table_info("{table}")'))).fetchall()
    return {str(r[1]) for r in rows}  # r = (cid, name, type, notnull, dflt, pk)


async def _existing_index_names(conn) -> set[str]:  # noqa: ANN001
    rows = (
        await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name IS NOT NULL")
        )
    ).fetchall()
    return {str(r[0]) for r in rows if r[0]}


def _sql_literal(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _column_default_literal(col) -> str | None:  # noqa: ANN001
    """A SQL literal for a column's declared default, or ``None`` if not scalar."""
    default = getattr(col, "default", None)
    if default is not None and getattr(default, "is_scalar", False):
        return _sql_literal(default.arg)
    server_default = getattr(col, "server_default", None)
    arg = getattr(server_default, "arg", None)
    if arg is not None:
        return getattr(arg, "text", None) or str(arg)
    return None


def _fallback_default_literal(col) -> str:  # noqa: ANN001
    """A safe NOT-NULL default for back-filling existing rows on ADD COLUMN.

    SQLite forbids ``CURRENT_TIMESTAMP`` in ``ALTER TABLE ADD COLUMN`` defaults,
    so datetime columns get a constant migration-time literal the ORM can parse.
    """
    # Prefer the ORM's declared default *factory* (e.g. ``default=list`` → '[]',
    # ``default=dict`` → '{}') so back-filled rows match exactly what the ORM
    # writes for new rows. Guessing purely from ``col.type.python_type`` is
    # wrong for JSON columns: SQLAlchemy's generic JSON maps to ``dict``, which
    # would back-fill a list-valued column (``Mapped[list]``) with '{}'.
    default = getattr(col, "default", None)
    if default is not None and getattr(default, "is_callable", False):
        factory = getattr(default, "arg", None)
        # SQLAlchemy wraps the raw factory so it takes an execution ``ctx``
        # argument; the original zero-arg callable is on ``__wrapped__``. Try
        # the unwrapped form first, then the ctx-taking wrapper (``fn(None)``).
        unwrapped = getattr(factory, "__wrapped__", None)
        for produce in (
            unwrapped if callable(unwrapped) else None,
            (lambda f=factory: f(None)) if callable(factory) else None,
        ):
            if produce is None:
                continue
            try:
                produced = produce()
            except Exception:  # noqa: BLE001 - not a simple factory → keep trying.
                continue
            if isinstance(produced, (list, dict)):
                return "'" + json.dumps(produced).replace("'", "''") + "'"
    try:
        py = col.type.python_type
    except Exception:  # noqa: BLE001 - exotic/unknown type → treat as text.
        py = str
    if py is bool:
        return "0"
    if py in (int, float):
        return "0"
    if py is dict:
        return "'{}'"
    if py is list:
        return "'[]'"
    if py is datetime:
        return "'" + datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f") + "'"
    return "''"


async def _apply_column_renames(conn) -> None:  # noqa: ANN001
    """Rename known legacy columns in place (data-preserving) before adds."""
    tables = await _existing_table_names(conn)
    for table, renames in _COLUMN_RENAMES.items():
        if table not in tables:
            continue
        cols = await _table_column_names(conn, table)
        for old, new in renames:
            if old in cols and new not in cols:
                await conn.execute(
                    text(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')
                )
                cols.discard(old)
                cols.add(new)
                logger.info("schema migrate: renamed %s.%s → %s", table, old, new)


async def _add_missing_columns(conn, dialect) -> list[tuple[str, str]]:  # noqa: ANN001
    """Add ORM columns absent from an existing table (additive; keeps rows)."""
    added: list[tuple[str, str]] = []
    tables = await _existing_table_names(conn)
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue  # freshly created by create_all → already complete
        existing = await _table_column_names(conn, table.name)
        for col in table.columns:
            if col.name in existing or col.primary_key:
                continue  # can't ADD a PRIMARY KEY column in SQLite
            coltype = col.type.compile(dialect=dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            literal = _column_default_literal(col)
            if not col.nullable:
                ddl += f" NOT NULL DEFAULT {literal or _fallback_default_literal(col)}"
            elif literal is not None:
                ddl += f" DEFAULT {literal}"
            await conn.execute(text(ddl))
            added.append((table.name, col.name))
            logger.info("schema migrate: added column %s.%s", table.name, col.name)
    return added


async def _create_missing_indexes(conn) -> None:  # noqa: ANN001
    """Create ORM indexes missing from existing tables (idempotent)."""
    tables = await _existing_table_names(conn)
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        for idx in table.indexes:
            if not idx.name:
                continue
            cols = ", ".join(f'"{c.name}"' for c in idx.columns)
            if not cols:
                continue
            unique = "UNIQUE " if idx.unique else ""
            await conn.execute(
                text(
                    f'CREATE {unique}INDEX IF NOT EXISTS "{idx.name}" '
                    f'ON "{table.name}" ({cols})'
                )
            )


async def _reconcile_artifact_task_ownership(conn) -> None:  # noqa: ANN001
    """Backfill canonical artifact owners and repair proven foreign task caches.

    Producing subtask/workflow links are the strongest evidence. For direct
    legacy artifacts, an explicit metadata owner or an unambiguous filename
    prefix in the same session is accepted. Conflicts remain unresolved.
    """
    tables = await _existing_table_names(conn)
    if not {"artifacts", "tasks", "subtasks", "workflow_runs"} <= tables:
        return
    artifact_cols = await _table_column_names(conn, "artifacts")
    task_cols = await _table_column_names(conn, "tasks")
    if "task_id" not in artifact_cols or "artifact_ids" not in task_cols:
        return

    await conn.execute(
        text(
            """
            UPDATE artifacts AS a
            SET task_id = (
                SELECT s.task_id FROM subtasks AS s WHERE s.id = a.subtask_id
            )
            WHERE COALESCE(a.task_id, '') = ''
              AND EXISTS (
                  SELECT 1 FROM subtasks AS s
                  WHERE s.id = a.subtask_id AND COALESCE(s.task_id, '') <> ''
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM subtasks AS s
                  JOIN workflow_runs AS w ON w.id = a.workflow_run_id
                  WHERE s.id = a.subtask_id
                    AND COALESCE(w.task_id, '') <> ''
                    AND w.task_id <> s.task_id
              )
            """
        )
    )
    await conn.execute(
        text(
            """
            UPDATE artifacts AS a
            SET task_id = (
                SELECT w.task_id FROM workflow_runs AS w WHERE w.id = a.workflow_run_id
            )
            WHERE COALESCE(a.task_id, '') = ''
              AND EXISTS (
                  SELECT 1 FROM workflow_runs AS w
                  WHERE w.id = a.workflow_run_id AND COALESCE(w.task_id, '') <> ''
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM workflow_runs AS w
                  JOIN subtasks AS s ON s.id = a.subtask_id
                  WHERE w.id = a.workflow_run_id
                    AND COALESCE(s.task_id, '') <> ''
                    AND s.task_id <> w.task_id
              )
            """
        )
    )

    task_rows = (
        await conn.execute(text("SELECT id, session_id, artifact_ids FROM tasks"))
    ).fetchall()
    task_info = {
        str(row[0]): {
            "session_id": str(row[1] or ""),
            "artifact_ids": row[2],
        }
        for row in task_rows
    }
    unresolved_rows = (
        await conn.execute(
            text(
                "SELECT id, session_id, rel_path, metadata FROM artifacts "
                "WHERE COALESCE(task_id, '') = ''"
            )
        )
    ).fetchall()
    for artifact_id_raw, session_id_raw, rel_path_raw, metadata_raw in unresolved_rows:
        artifact_id = str(artifact_id_raw)
        artifact_session = str(session_id_raw or "")
        candidates: set[str] = set()
        try:
            metadata = (
                json.loads(metadata_raw)
                if isinstance(metadata_raw, str)
                else dict(metadata_raw or {})
            )
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        for key in ("task_id", "producer_task_id"):
            candidate = str(metadata.get(key) or "")
            if candidate in task_info:
                candidates.add(candidate)

        filename = Path(str(rel_path_raw or "")).name
        artifact_marker = f"-{artifact_id[:8]}"
        marker_at = filename.rfind(artifact_marker)
        if marker_at >= 0:
            marker_end = marker_at + len(artifact_marker)
            if marker_end == len(filename) or filename[marker_end] == ".":
                filename_head = filename[:marker_at]
                task_prefix = filename_head.rsplit("-", 1)[-1]
                if (
                    "-" in filename_head
                    and len(task_prefix) == 8
                    and all(char in "0123456789abcdefABCDEF" for char in task_prefix)
                ):
                    candidates.update(
                        task_id
                        for task_id in task_info
                        if task_id.startswith(task_prefix)
                    )

        compatible = [
            task_id
            for task_id in candidates
            if not artifact_session
            or not task_info[task_id]["session_id"]
            or task_info[task_id]["session_id"] == artifact_session
        ]
        if len(compatible) == 1:
            await conn.execute(
                text("UPDATE artifacts SET task_id = :task_id WHERE id = :artifact_id"),
                {"task_id": compatible[0], "artifact_id": artifact_id},
            )

    artifact_rows = (
        await conn.execute(
            text(
                "SELECT id, task_id FROM artifacts "
                "WHERE COALESCE(task_id, '') <> ''"
            )
        )
    ).fetchall()
    owner_by_artifact = {str(row[0]): str(row[1]) for row in artifact_rows}
    if not owner_by_artifact:
        return
    for task_id, info in task_info.items():
        artifact_ids_raw = info["artifact_ids"]
        try:
            current = (
                json.loads(artifact_ids_raw)
                if isinstance(artifact_ids_raw, str)
                else list(artifact_ids_raw or [])
            )
        except (TypeError, ValueError):
            current = []
        repaired = [
            str(artifact_id)
            for artifact_id in current
            if not owner_by_artifact.get(str(artifact_id))
            or owner_by_artifact[str(artifact_id)] == task_id
        ]
        for artifact_id, owner_id in owner_by_artifact.items():
            if owner_id == task_id and artifact_id not in repaired:
                repaired.append(artifact_id)
        if repaired != current:
            await conn.execute(
                text("UPDATE tasks SET artifact_ids = :artifact_ids WHERE id = :task_id"),
                {
                    "artifact_ids": json.dumps(repaired, ensure_ascii=False),
                    "task_id": task_id,
                },
            )


async def _migrate_schema_additively(conn, dialect) -> None:  # noqa: ANN001
    """Reconcile an older store to the current ORM without dropping data.

    Artifact-owner backfill runs only when this pass actually added
    ``artifacts.task_id``. A current store must not UPDATE artifacts on every
    CLI open — that write contends with ``omni serve``.
    """
    await _apply_column_renames(conn)
    await conn.run_sync(Base.metadata.create_all)  # brand-new tables (+ their indexes)
    added = await _add_missing_columns(conn, dialect)
    if any(table == "artifacts" and column == "task_id" for table, column in added):
        await _reconcile_artifact_task_ownership(conn)
    await _create_missing_indexes(conn)


async def _drop_sqlite_schema(conn) -> None:  # noqa: ANN001
    """Drop all user-created SQLite objects before recreating the current schema."""
    result = await conn.execute(
        text(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' "
            "AND type IN ('view', 'trigger', 'index', 'table') "
            "ORDER BY CASE type "
            "WHEN 'view' THEN 0 WHEN 'trigger' THEN 1 WHEN 'index' THEN 2 ELSE 3 END"
        )
    )
    for object_type, name in result.fetchall():
        ddl = f"DROP {str(object_type).upper()} IF EXISTS {_quote_identifier(str(name))}"
        await conn.execute(text(ddl))


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@contextmanager
def _database_init_lock(path: Path) -> Iterator[None]:
    """Serialize WAL/schema setup across Omni processes for one SQLite file."""
    lock_path = path.with_name(f"{path.name}.init.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a", encoding="utf-8")
    lock_impl: ModuleType | None = None
    try:
        try:
            import fcntl as lock_impl

            lock_impl.flock(lock_file.fileno(), lock_impl.LOCK_EX)
        except ImportError:
            lock_impl = None
        yield
    finally:
        try:
            if lock_impl is not None:
                lock_impl.flock(lock_file.fileno(), lock_impl.LOCK_UN)
        finally:
            lock_file.close()
