"""Async SQLite engine/session management.

One physical SQLite file per project holds every structured table
(sessions, messages, tasks, subtasks, memory_entries, artifacts). WAL mode is
enabled so a long-running ``omni serve`` daemon and short-lived ``omni``
commands can read/write concurrently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from omni.storage.models import Base

logger = logging.getLogger(__name__)

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

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{path}"
        self.engine = create_async_engine(url, echo=False, future=True)
        self._sessionmaker = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self._initialized = False

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn, _rec):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    async def init(self) -> None:
        """Open the store, reconciling its schema to this codebase.

        Within the current schema generation (``PRAGMA application_id``),
        reconcile is **additive and data-preserving** on every start: snapshot
        when the on-disk watermark differs (:meth:`_backup`), then apply known
        column renames, create new tables, and add missing columns/indexes.
        Column adds do not bump ``user_version``; the watermark stays at the
        single baseline until a future compatibility cut needs a boundary.

        A store whose watermark is *ahead* of this build is left intact
        (forward-compatible), never rebuilt. A store from an **older
        generation** (different ``application_id``) is snapshotted and rebuilt
        from scratch — a deliberate clean break rather than a lossless
        migration.
        """
        if self._initialized:
            return
        with _database_init_lock(self.path):
            async with self.engine.connect() as conn:
                stored = int((await conn.execute(text("PRAGMA user_version"))).scalar_one() or 0)
                generation = int(
                    (await conn.execute(text("PRAGMA application_id"))).scalar_one() or 0
                )
                has_tables = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%' LIMIT 1"
                        )
                    )
                ).scalar_one_or_none() is not None
            legacy = has_tables and generation != _SCHEMA_GENERATION
            # Watermarks left by earlier interim counters on this generation —
            # normalize them back to the single baseline on the next open.
            normalize_watermark = (
                has_tables and not legacy and stored > _SCHEMA_VERSION and stored < 100
            )
            future = has_tables and not legacy and stored >= 100
            backup: Path | None = None
            if has_tables and (legacy or stored != _SCHEMA_VERSION):
                backup = await self._backup(stored)
            dialect = self.engine.sync_engine.dialect
            async with self.engine.begin() as conn:
                await conn.execute(text("PRAGMA journal_mode=WAL"))
                if legacy:
                    # Older-generation vocabulary. Deliberate clean break:
                    # snapshot (above), drop everything, rebuild current schema.
                    await _drop_sqlite_schema(conn)
                    await conn.run_sync(Base.metadata.create_all)
                    logger.warning(
                        "Rebuilt legacy store as the current schema "
                        "(previous watermark=%d); snapshot: %s",
                        stored,
                        backup or "unavailable",
                    )
                elif future:
                    # DB watermark is ahead of this build. Keep the store and
                    # its watermark; only ensure our known tables exist.
                    await conn.run_sync(Base.metadata.create_all)
                    logger.warning(
                        "Local store watermark %d is ahead of this build (%d); "
                        "preserving the additive store.",
                        stored,
                        _SCHEMA_VERSION,
                    )
                else:
                    # Current generation (fresh, current, or interim watermark):
                    # create_all + additive reconcile are both idempotent.
                    await conn.run_sync(Base.metadata.create_all)
                    if has_tables:
                        try:
                            await _migrate_schema_additively(conn, dialect)
                            if stored != _SCHEMA_VERSION or normalize_watermark:
                                backup_note = (
                                    f" (pre-reconcile backup: {backup})" if backup else ""
                                )
                                logger.info(
                                    "Reconciled the local schema in place "
                                    "(watermark %d → %d)%s",
                                    stored,
                                    _SCHEMA_VERSION,
                                    backup_note,
                                )
                        except Exception:
                            logger.exception(
                                "Additive reconcile failed; rebuilding after "
                                "preserving backup %s",
                                backup,
                            )
                            await _drop_sqlite_schema(conn)
                            await conn.run_sync(Base.metadata.create_all)
                # Stamp the generation marker. Advance/normalize the watermark
                # for current-generation stores; a true future watermark
                # (reserved range) is left alone so newer processes still
                # recognise it.
                await conn.execute(text(f"PRAGMA application_id = {_SCHEMA_GENERATION}"))
                if not future:
                    await conn.execute(text(f"PRAGMA user_version = {_SCHEMA_VERSION}"))
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


async def _add_missing_columns(conn, dialect) -> None:  # noqa: ANN001
    """Add ORM columns absent from an existing table (additive; keeps rows)."""
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
            logger.info("schema migrate: added column %s.%s", table.name, col.name)


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
    """Reconcile an older store to the current ORM without dropping data."""
    await _apply_column_renames(conn)
    await conn.run_sync(Base.metadata.create_all)  # brand-new tables (+ their indexes)
    await _add_missing_columns(conn, dialect)
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
