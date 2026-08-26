"""Read-only, cross-workspace session aggregation for management surfaces.

The workspace catalog is discovery only; each workspace SQLite database remains
the source of truth.  Reads use SQLite's ``mode=ro`` URI so a stale catalog
entry can never recreate a deleted store or run schema initialization as a side
effect of opening the global session list.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from omni.agent.conversation_store import PERSONA_CONTROL_EXTERNAL_KEY
from omni.config.paths import user_home
from omni.config.workspaces import iter_catalog_workspaces

SessionSort = Literal["activity", "started", "completed", "created"]

_SORT_FIELDS: dict[SessionSort, str] = {
    "activity": "last_activity_at",
    "started": "last_started_at",
    "completed": "last_completed_at",
    "created": "created_at",
}
_STATUS_GROUPS = {
    "running",
    "needs_attention",
    "completed",
    "warning",
    "error",
    "cancelled",
    "empty",
}
_TERMINAL_GROUPS = {
    "succeeded": "completed",
    "degraded": "warning",
    "failed": "error",
    "interrupted": "error",
    "cancelled": "cancelled",
}


@dataclass(frozen=True, slots=True)
class SessionAggregateRow:
    """One session projected from its owning workspace database."""

    workspace_label: str
    project_dir: str
    workspace_root: str | None
    workspace_kind: str
    session_id: str
    title: str
    channel: str
    session_status: str
    status_group: str
    message_count: int
    created_at: datetime | None
    updated_at: datetime | None
    last_message_at: datetime | None
    last_activity_at: datetime | None
    last_started_at: datetime | None
    last_completed_at: datetime | None
    latest_task_id: str
    latest_task_status: str


@dataclass(frozen=True, slots=True)
class SkippedSessionStore:
    """A catalog store omitted from a best-effort global read."""

    workspace_label: str
    project_dir: str
    db_path: str
    code: Literal["missing", "unreadable"]
    reason: str


@dataclass(frozen=True, slots=True)
class SessionAggregatePage:
    """Stable global page plus non-fatal workspace read diagnostics."""

    rows: list[SessionAggregateRow]
    next_cursor: str | None
    skipped: list[SkippedSessionStore]


def _canonical_path(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve())


def _record_identity(record: Mapping[str, object]) -> tuple[str, str, Path]:
    db_path = Path(str(record.get("db") or "")).expanduser()
    project_dir = _canonical_path(
        str(record.get("project_dir") or "") or db_path.parent
    )
    label = str(record.get("name") or Path(project_dir).name)
    return label, project_dir, db_path


def _parse_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _latest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _display_title(title: object, first_user_message: object) -> str:
    value = str(title or "").strip() or str(first_user_message or "").strip()
    value = " ".join(value.split())
    return value or "New session"


def _status_group(
    *,
    has_running: bool,
    has_attention: bool,
    latest_terminal_status: str,
    has_tasks: bool,
) -> str:
    if has_running:
        return "running"
    if has_attention:
        return "needs_attention"
    if latest_terminal_status:
        return _TERMINAL_GROUPS.get(latest_terminal_status, "warning")
    return "warning" if has_tasks else "empty"


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.25)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=250")
    return connection


_SESSION_SQL = """
SELECT
    s.id,
    s.title,
    s.channel,
    s.status,
    s.created_at,
    s.updated_at,
    (
        SELECT COUNT(*)
        FROM conversation_messages AS counted
        WHERE counted.session_id = s.id
    ) AS message_count,
    (
        SELECT MAX(recent.created_at)
        FROM conversation_messages AS recent
        WHERE recent.session_id = s.id
    ) AS last_message_at,
    (
        SELECT first_user.content
        FROM conversation_messages AS first_user
        WHERE first_user.session_id = s.id
          AND first_user.role = 'user'
          AND (first_user.content_type = 'text' OR first_user.content_type IS NULL)
        ORDER BY first_user.created_at ASC, first_user.rowid ASC
        LIMIT 1
    ) AS first_user_message
FROM sessions AS s
WHERE COALESCE(s.external_key, '') != ?
"""

_TASK_SUMMARY_SQL = """
SELECT
    session_id,
    COUNT(*) AS task_count,
    MAX(CASE WHEN status IN ('running', 'recovering') THEN 1 ELSE 0 END) AS has_running,
    MAX(CASE WHEN status IN ('awaiting_approval', 'needs_input') THEN 1 ELSE 0 END) AS has_attention,
    MAX(created_at) AS latest_created_at,
    MAX(started_at) AS latest_started_at,
    MAX(finished_at) AS latest_finished_at,
    MAX(
        CASE WHEN status IN ('succeeded', 'degraded', 'failed', 'interrupted', 'cancelled')
             THEN finished_at END
    ) AS latest_completed_at
FROM tasks
WHERE kind = 'turn' AND parent_task_id IS NULL AND archived_at IS NULL
GROUP BY session_id
"""

_LATEST_TASK_SQL = """
WITH ranked AS (
    SELECT
        id,
        session_id,
        status,
        ROW_NUMBER() OVER (
            PARTITION BY session_id
            ORDER BY created_at DESC, id DESC
        ) AS rank
    FROM tasks
    WHERE kind = 'turn' AND parent_task_id IS NULL AND archived_at IS NULL
)
SELECT id, session_id, status FROM ranked WHERE rank = 1
"""

_LATEST_TERMINAL_SQL = """
WITH ranked AS (
    SELECT
        session_id,
        status,
        ROW_NUMBER() OVER (
            PARTITION BY session_id
            ORDER BY created_at DESC, id DESC
        ) AS rank
    FROM tasks
    WHERE kind = 'turn'
      AND parent_task_id IS NULL
      AND archived_at IS NULL
      AND status IN ('succeeded', 'degraded', 'failed', 'interrupted', 'cancelled')
)
SELECT session_id, status FROM ranked WHERE rank = 1
"""


def _read_workspace_sync(record: Mapping[str, object]) -> list[SessionAggregateRow]:
    label, project_dir, db_path = _record_identity(record)
    connection = _open_read_only(db_path)
    try:
        sessions = connection.execute(
            _SESSION_SQL,
            (PERSONA_CONTROL_EXTERNAL_KEY,),
        ).fetchall()
        summaries = {
            str(row["session_id"]): row
            for row in connection.execute(_TASK_SUMMARY_SQL).fetchall()
        }
        latest_tasks = {
            str(row["session_id"]): row
            for row in connection.execute(_LATEST_TASK_SQL).fetchall()
        }
        latest_terminal = {
            str(row["session_id"]): str(row["status"] or "")
            for row in connection.execute(_LATEST_TERMINAL_SQL).fetchall()
        }
    finally:
        connection.close()

    raw_root = record.get("root")
    root = str(raw_root) if raw_root else None
    kind = str(record.get("kind") or "")
    rows: list[SessionAggregateRow] = []
    for session in sessions:
        session_id = str(session["id"])
        summary = summaries.get(session_id)
        latest_task = latest_tasks.get(session_id)
        created_at = _parse_datetime(session["created_at"])
        updated_at = _parse_datetime(session["updated_at"])
        last_message_at = _parse_datetime(session["last_message_at"])
        latest_task_created = _parse_datetime(summary["latest_created_at"] if summary else None)
        latest_started_at = _parse_datetime(summary["latest_started_at"] if summary else None)
        latest_finished_at = _parse_datetime(summary["latest_finished_at"] if summary else None)
        latest_completed_at = _parse_datetime(summary["latest_completed_at"] if summary else None)
        has_tasks = bool(summary and int(summary["task_count"] or 0))
        rows.append(
            SessionAggregateRow(
                workspace_label=label,
                project_dir=project_dir,
                workspace_root=root,
                workspace_kind=kind,
                session_id=session_id,
                title=_display_title(session["title"], session["first_user_message"]),
                channel=str(session["channel"] or ""),
                session_status=str(session["status"] or ""),
                status_group=_status_group(
                    has_running=bool(summary and summary["has_running"]),
                    has_attention=bool(summary and summary["has_attention"]),
                    latest_terminal_status=latest_terminal.get(session_id, ""),
                    has_tasks=has_tasks,
                ),
                message_count=int(session["message_count"] or 0),
                created_at=created_at,
                updated_at=updated_at,
                last_message_at=last_message_at,
                last_activity_at=_latest(
                    updated_at,
                    last_message_at,
                    latest_task_created,
                    latest_started_at,
                    latest_finished_at,
                ),
                last_started_at=latest_started_at,
                last_completed_at=latest_completed_at,
                latest_task_id=str(latest_task["id"] if latest_task else ""),
                latest_task_status=str(latest_task["status"] if latest_task else ""),
            )
        )
    return rows


async def _read_workspace(record: Mapping[str, object]) -> list[SessionAggregateRow]:
    return await asyncio.to_thread(_read_workspace_sync, record)


def _sort_key(
    row: SessionAggregateRow,
    sort: SessionSort,
) -> tuple[int, int, str, str]:
    value = getattr(row, _SORT_FIELDS[sort])
    if value is None:
        return 1, 0, row.project_dir, row.session_id
    microseconds = int(value.timestamp() * 1_000_000)
    return 0, -microseconds, row.project_dir, row.session_id


def _encode_cursor(row: SessionAggregateRow, sort: SessionSort) -> str:
    value = getattr(row, _SORT_FIELDS[sort])
    payload = {
        "v": 1,
        "sort": sort,
        "timestamp": value.isoformat() if value is not None else None,
        "project_dir": row.project_dir,
        "session_id": row.session_id,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _decode_cursor(cursor: str, sort: SessionSort) -> tuple[int, int, str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if payload.get("v") != 1 or payload.get("sort") != sort:
            raise ValueError
        project_dir = str(payload["project_dir"])
        session_id = str(payload["session_id"])
        timestamp = _parse_datetime(payload.get("timestamp"))
        if payload.get("timestamp") is not None and timestamp is None:
            raise ValueError
    except (
        AttributeError,
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError("invalid or incompatible session cursor") from exc
    if timestamp is None:
        return 1, 0, project_dir, session_id
    return 0, -int(timestamp.timestamp() * 1_000_000), project_dir, session_id


def _with_live_status(
    row: SessionAggregateRow,
    live_sessions: set[tuple[str, str]],
) -> SessionAggregateRow:
    if (row.project_dir, row.session_id) not in live_sessions or row.status_group == "running":
        return row
    return replace(row, status_group="running")


async def list_sessions_all_workspaces(
    *,
    home: Path | None = None,
    workspace: str | None = None,
    channel: str | None = None,
    status: Collection[str] | None = None,
    sort: SessionSort = "activity",
    cursor: str | None = None,
    limit: int = 50,
    concurrency: int = 4,
    live_sessions: Collection[tuple[str, str]] | None = None,
    exclude_workspaces: Collection[str] | None = None,
) -> SessionAggregatePage:
    """Return a globally sorted page without creating or migrating stores.

    Filters and the global limit are applied only after every readable workspace
    has contributed its session projections.  A malformed workspace is reported
    in ``skipped`` and does not blank otherwise healthy results.
    """
    if sort not in _SORT_FIELDS:
        raise ValueError(f"unsupported session sort: {sort}")
    if limit < 1 or limit > 500:
        raise ValueError("session limit must be between 1 and 500")
    if concurrency < 1:
        raise ValueError("session read concurrency must be positive")
    selected_statuses = set(status or ())
    unknown_statuses = selected_statuses - _STATUS_GROUPS
    if unknown_statuses:
        raise ValueError(f"unsupported session status: {sorted(unknown_statuses)[0]}")

    root = (home or user_home()).expanduser()
    selected_workspace = _canonical_path(workspace) if workspace else ""
    excluded_workspaces = {
        _canonical_path(project_dir) for project_dir in (exclude_workspaces or ())
    }
    records: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for record in iter_catalog_workspaces(root):
        _label, project_dir, _db_path = _record_identity(record)
        if (
            project_dir in seen
            or project_dir in excluded_workspaces
            or (selected_workspace and project_dir != selected_workspace)
        ):
            continue
        seen.add(project_dir)
        records.append(record)

    semaphore = asyncio.Semaphore(min(concurrency, 16))

    async def read_one(
        record: Mapping[str, object],
    ) -> tuple[list[SessionAggregateRow], SkippedSessionStore | None]:
        label, project_dir, db_path = _record_identity(record)
        if not db_path.is_file():
            return [], SkippedSessionStore(
                workspace_label=label,
                project_dir=project_dir,
                db_path=str(db_path),
                code="missing",
                reason="workspace database does not exist",
            )
        try:
            async with semaphore:
                return await _read_workspace(record), None
        except Exception as exc:  # noqa: BLE001 - one bad store must not blank the page
            return [], SkippedSessionStore(
                workspace_label=label,
                project_dir=project_dir,
                db_path=str(db_path),
                code="unreadable",
                reason=f"{type(exc).__name__}: {exc}",
            )

    results = await asyncio.gather(*(read_one(record) for record in records))
    rows = [row for workspace_rows, _skipped in results for row in workspace_rows]
    skipped = sorted(
        [item for _rows, item in results if item is not None],
        key=lambda item: (item.project_dir, item.workspace_label),
    )

    normalized_live = {
        (_canonical_path(project_dir), session_id)
        for project_dir, session_id in (live_sessions or ())
    }
    if normalized_live:
        rows = [_with_live_status(row, normalized_live) for row in rows]
    if channel:
        rows = [row for row in rows if row.channel == channel]
    if selected_statuses:
        rows = [row for row in rows if row.status_group in selected_statuses]

    rows.sort(key=lambda row: _sort_key(row, sort))
    if cursor:
        cursor_key = _decode_cursor(cursor, sort)
        rows = [row for row in rows if _sort_key(row, sort) > cursor_key]
    page_rows = rows[:limit]
    next_cursor = (
        _encode_cursor(page_rows[-1], sort)
        if len(rows) > limit and page_rows
        else None
    )
    return SessionAggregatePage(
        rows=page_rows,
        next_cursor=next_cursor,
        skipped=skipped,
    )


__all__ = [
    "SessionAggregatePage",
    "SessionAggregateRow",
    "SessionSort",
    "SkippedSessionStore",
    "list_sessions_all_workspaces",
]
