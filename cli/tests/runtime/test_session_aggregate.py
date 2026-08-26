from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omni.storage.db import Database
from omni.storage.models import ConversationMessageORM, SessionORM, TaskORM


def _at(hour: int) -> datetime:
    return datetime(2026, 8, 26, hour, tzinfo=UTC)


async def _seed_workspace(
    home: Path,
    name: str,
    *,
    sessions: list[SessionORM],
    messages: list[ConversationMessageORM] | None = None,
    tasks: list[TaskORM] | None = None,
) -> Path:
    db_path = home / "projects" / name / "sessions.sqlite3"
    db = Database(db_path)
    await db.init()
    async with db.session() as session:
        session.add_all([*sessions, *(messages or []), *(tasks or [])])
        await session.commit()
    await db.dispose()
    return db_path


@pytest.mark.asyncio
async def test_global_session_aggregate_keeps_empty_sessions_and_status_precedence(
    omni_home: Path,
) -> None:
    from omni.runtime.session_aggregate import list_sessions_all_workspaces

    db_path = await _seed_workspace(
        omni_home,
        "alpha",
        sessions=[
            SessionORM(id="empty", title="Empty", created_at=_at(1), updated_at=_at(1)),
            SessionORM(id="live", title="Live", created_at=_at(1), updated_at=_at(1)),
            SessionORM(id="mixed", title="Mixed", created_at=_at(2), updated_at=_at(2)),
            SessionORM(id="waiting", title="Waiting", created_at=_at(3), updated_at=_at(3)),
        ],
        messages=[
            ConversationMessageORM(
                id="message-mixed",
                session_id="mixed",
                role="user",
                content="new message",
                created_at=_at(8),
            )
        ],
        tasks=[
            # An older running task must outrank the newer terminal task for the
            # aggregate group, while latest_task_status still describes the
            # newest logical root turn.
            TaskORM(
                id="mixed-running",
                session_id="mixed",
                kind="turn",
                status="running",
                created_at=_at(4),
                started_at=_at(5),
            ),
            TaskORM(
                id="mixed-newer",
                session_id="mixed",
                kind="turn",
                status="succeeded",
                created_at=_at(6),
                started_at=_at(6),
                finished_at=_at(7),
            ),
            TaskORM(
                id="waiting-input",
                session_id="waiting",
                kind="turn",
                status="needs_input",
                created_at=_at(9),
                started_at=_at(9),
            ),
        ],
    )

    page = await list_sessions_all_workspaces(
        home=omni_home,
        limit=20,
        live_sessions={(str(db_path.parent.resolve()), "live")},
    )
    rows = {row.session_id: row for row in page.rows}

    assert set(rows) == {"empty", "live", "mixed", "waiting"}
    assert rows["empty"].status_group == "empty"
    assert rows["empty"].latest_task_status == ""
    assert rows["live"].status_group == "running"
    assert rows["mixed"].status_group == "running"
    assert rows["mixed"].latest_task_id == "mixed-newer"
    assert rows["mixed"].latest_task_status == "succeeded"
    assert rows["mixed"].message_count == 1
    assert rows["mixed"].last_started_at == _at(6)
    assert rows["mixed"].last_completed_at == _at(7)
    assert rows["mixed"].last_activity_at == _at(8)
    assert rows["waiting"].status_group == "needs_attention"


@pytest.mark.asyncio
async def test_global_session_aggregate_preserves_terminal_groups_and_filters(
    omni_home: Path,
) -> None:
    from omni.runtime.session_aggregate import list_sessions_all_workspaces

    terminal = {
        "good": ("succeeded", "completed"),
        "warn": ("degraded", "warning"),
        "bad": ("failed", "error"),
        "stopped": ("interrupted", "error"),
        "cancel": ("cancelled", "cancelled"),
    }
    sessions = [
        SessionORM(
            id=session_id,
            channel="web" if session_id == "good" else "cli",
            created_at=_at(index),
            updated_at=_at(index),
        )
        for index, session_id in enumerate(terminal, start=1)
    ]
    tasks = [
        TaskORM(
            id=f"task-{session_id}",
            session_id=session_id,
            kind="turn",
            status=status,
            created_at=_at(index),
            started_at=_at(index),
            finished_at=_at(index) + timedelta(minutes=1),
        )
        for index, (session_id, (status, _group)) in enumerate(terminal.items(), start=10)
    ]
    project_dir = (await _seed_workspace(
        omni_home,
        "terminal",
        sessions=sessions,
        tasks=tasks,
    )).parent.resolve()

    page = await list_sessions_all_workspaces(home=omni_home, limit=20)
    rows = {row.session_id: row for row in page.rows}
    assert {session_id: rows[session_id].status_group for session_id in terminal} == {
        session_id: group for session_id, (_status, group) in terminal.items()
    }

    filtered = await list_sessions_all_workspaces(
        home=omni_home,
        workspace=str(project_dir),
        channel="web",
        status={"completed"},
        limit=20,
    )
    assert [row.session_id for row in filtered.rows] == ["good"]


@pytest.mark.asyncio
async def test_global_session_aggregate_sorts_before_limit_and_pages_stably(
    omni_home: Path,
) -> None:
    from omni.runtime.session_aggregate import list_sessions_all_workspaces

    for workspace, session_ids in (("alpha", ("a", "c")), ("beta", ("b", "d"))):
        await _seed_workspace(
            omni_home,
            workspace,
            sessions=[
                SessionORM(
                    id=session_id,
                    created_at=_at(1),
                    updated_at=_at(1),
                )
                for session_id in session_ids
            ],
            tasks=[
                TaskORM(
                    id=f"task-{session_id}",
                    session_id=session_id,
                    kind="turn",
                    status="succeeded",
                    created_at=_at(2),
                    started_at=_at(3),
                    finished_at=_at(4) if session_id != "d" else None,
                )
                for session_id in session_ids
            ],
        )

    first = await list_sessions_all_workspaces(
        home=omni_home,
        sort="completed",
        limit=2,
    )
    assert len(first.rows) == 2
    assert first.next_cursor
    # Equal timestamps use canonical project_dir then session_id, not catalog
    # discovery or task insertion order.
    assert [(row.workspace_label, row.session_id) for row in first.rows] == [
        ("alpha", "a"),
        ("alpha", "c"),
    ]

    second = await list_sessions_all_workspaces(
        home=omni_home,
        sort="completed",
        cursor=first.next_cursor,
        limit=2,
    )
    assert [(row.workspace_label, row.session_id) for row in second.rows] == [
        ("beta", "b"),
        ("beta", "d"),
    ]
    assert second.rows[-1].last_completed_at is None
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_global_session_aggregate_excludes_workspaces_before_limit(
    omni_home: Path,
) -> None:
    from omni.runtime.session_aggregate import list_sessions_all_workspaces

    hidden_db = await _seed_workspace(
        omni_home,
        "hidden",
        sessions=[SessionORM(id="hidden", created_at=_at(9), updated_at=_at(9))],
    )
    await _seed_workspace(
        omni_home,
        "visible",
        sessions=[SessionORM(id="visible", created_at=_at(1), updated_at=_at(1))],
    )

    page = await list_sessions_all_workspaces(
        home=omni_home,
        exclude_workspaces={str(hidden_db.parent)},
        limit=1,
    )

    assert [row.session_id for row in page.rows] == ["visible"]
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_global_session_aggregate_skips_missing_and_unreadable_stores_without_creating(
    omni_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omni.runtime.session_aggregate as aggregate

    good_db = await _seed_workspace(
        omni_home,
        "good",
        sessions=[SessionORM(id="good-session")],
    )
    bad_db = omni_home / "projects" / "bad" / "sessions.sqlite3"
    bad_db.parent.mkdir(parents=True)
    bad_db.write_text("not a sqlite database", encoding="utf-8")
    schema_gap_db = omni_home / "projects" / "schema-gap" / "sessions.sqlite3"
    schema_gap_db.parent.mkdir(parents=True)
    sqlite3.connect(schema_gap_db).close()
    missing_db = omni_home / "projects" / "missing" / "sessions.sqlite3"
    records = [
        {"name": "good", "project_dir": str(good_db.parent), "db": str(good_db)},
        {"name": "bad", "project_dir": str(bad_db.parent), "db": str(bad_db)},
        {
            "name": "schema-gap",
            "project_dir": str(schema_gap_db.parent),
            "db": str(schema_gap_db),
        },
        {"name": "missing", "project_dir": str(missing_db.parent), "db": str(missing_db)},
    ]
    monkeypatch.setattr(aggregate, "iter_catalog_workspaces", lambda _home=None: records)

    page = await aggregate.list_sessions_all_workspaces(home=omni_home, limit=20)

    assert [row.session_id for row in page.rows] == ["good-session"]
    assert {(item.workspace_label, item.code) for item in page.skipped} == {
        ("bad", "unreadable"),
        ("schema-gap", "unreadable"),
        ("missing", "missing"),
    }
    assert not missing_db.exists()


@pytest.mark.asyncio
async def test_global_session_aggregate_bounds_concurrent_workspace_reads(
    omni_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omni.runtime.session_aggregate as aggregate

    records = []
    for index in range(7):
        db_path = omni_home / "projects" / f"w{index}" / "sessions.sqlite3"
        db_path.parent.mkdir(parents=True)
        db_path.touch()
        records.append(
            {"name": f"w{index}", "project_dir": str(db_path.parent), "db": str(db_path)}
        )
    monkeypatch.setattr(aggregate, "iter_catalog_workspaces", lambda _home=None: records)

    active = 0
    peak = 0

    async def fake_read(_record):  # noqa: ANN001
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    monkeypatch.setattr(aggregate, "_read_workspace", fake_read)

    page = await aggregate.list_sessions_all_workspaces(
        home=omni_home,
        concurrency=2,
        limit=20,
    )

    assert page.skipped == []
    assert peak == 2
