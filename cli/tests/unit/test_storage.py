"""Storage: db init, sessions/messages, artifact store."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import ArtifactORM, ConversationMessageORM, SessionORM, TaskORM


@pytest.mark.asyncio
async def test_db_init_and_session_roundtrip():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    assert await db.healthcheck()
    async with db.session() as session:
        row = SessionORM(project="default", channel="cli")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        sid = row.id
        session.add(ConversationMessageORM(session_id=sid, role="user", content="hi"))
        await session.commit()
    async with db.session() as session:
        from sqlalchemy import select
        msgs = (await session.execute(
            select(ConversationMessageORM).where(ConversationMessageORM.session_id == sid)
        )).scalars().all()
    assert len(msgs) == 1 and msgs[0].content == "hi"


@pytest.mark.asyncio
async def test_schema_version_helpers_detect_drift():
    from sqlalchemy import text

    from omni.storage.db import (
        code_schema_version,
        get_database,
        read_stored_schema_version,
        schema_drifted,
    )

    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    # After init the on-disk version matches this codebase → no drift.
    assert await read_stored_schema_version(db) == code_schema_version()
    assert await schema_drifted(db) is False
    # Simulate a newer build rebuilding the store underneath a live daemon.
    async with db.engine.begin() as conn:
        await conn.execute(text(f"PRAGMA user_version = {code_schema_version() + 1}"))
    assert await schema_drifted(db) is True


@pytest.mark.asyncio
async def test_schema_drift_fails_safe_on_unversioned_store():
    from sqlalchemy import text

    from omni.storage.db import get_database, schema_drifted

    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    async with db.engine.begin() as conn:
        await conn.execute(text("PRAGMA user_version = 0"))
    # An unversioned/fresh store (0) must never be read as drift (fail-safe),
    # so the daemon liveness probe can't trigger a spurious shutdown.
    assert await schema_drifted(db) is False


@pytest.mark.asyncio
async def test_legacy_generation_store_is_rebuilt_and_backed_up():
    """An older-generation store (agent_runs/skill_tasks era, ``application_id`` 0)
    is a deliberate clean break: snapshotted, then rebuilt as the current
    task/workflow/execution schema (historical rows are not migrated).
    """
    import sqlite3

    from sqlalchemy import select, text

    from omni.storage.db import (
        code_schema_version,
        get_database,
        read_stored_schema_version,
        reset_databases,
    )
    from omni.storage.models import MemoryEntryORM

    s = load_settings()
    s.paths.ensure_dirs()
    db_path = s.paths.project_db

    # Hand-craft a legacy store: old vocabulary tables, a memory row,
    # a non-baseline watermark, and ``application_id`` 0.
    con = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        con.execute(
            "CREATE TABLE memory_entries ("
            " id VARCHAR(40) PRIMARY KEY,"
            " user_id VARCHAR(64) NOT NULL DEFAULT 'local',"
            " layer VARCHAR(8),"
            " scope VARCHAR(16),"
            " summary TEXT,"
            " created_at DATETIME)"
        )
        con.execute("CREATE TABLE agent_runs (id VARCHAR(40) PRIMARY KEY, status VARCHAR(32))")
        con.execute("CREATE TABLE skill_tasks (id VARCHAR(40) PRIMARY KEY, run_id VARCHAR(40))")
        con.execute(
            "INSERT INTO memory_entries (id, user_id, layer, scope, summary, created_at) "
            "VALUES ('m-legacy', 'feishu:alice', 'M4', 'user', "
            "'prefers concise bullet answers', '2026-01-01 00:00:00.000000')"
        )
        con.execute("PRAGMA user_version = 12")  # older generation, application_id stays 0
    finally:
        con.close()

    await reset_databases()
    db = get_database(db_path)
    await db.init()

    # Watermark resets to the current baseline; the new vocabulary tables
    # exist and the old ones are gone.
    assert await read_stored_schema_version(db) == code_schema_version() == 1
    async with db.engine.connect() as conn:
        tables = {
            r[0] for r in (await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )).fetchall()
        }
        generation = int((await conn.execute(text("PRAGMA application_id"))).scalar_one() or 0)
    assert {"tasks", "subtasks", "task_events", "task_controls"} <= tables
    assert "agent_runs" not in tables and "skill_tasks" not in tables
    assert generation == 0x4F4D4E33  # current-generation marker stamped

    # A pre-rebuild snapshot was written (the historical store is recoverable).
    backups = list((db_path.parent / "backups").glob(f"{db_path.stem}.v12.*{db_path.suffix}"))
    assert backups, "expected a pre-rebuild backup snapshot"

    # The legacy memory row was NOT migrated (clean break), and the rebuilt
    # schema is writable through the ORM.
    async with db.session() as session:
        assert (await session.execute(
            select(MemoryEntryORM).where(MemoryEntryORM.id == "m-legacy")
        )).scalar_one_or_none() is None
        session.add(MemoryEntryORM(layer="M1", scope="session", summary="fresh after rebuild"))
        await session.commit()


@pytest.mark.asyncio
async def test_additive_reconcile_adds_missing_scheduling_columns():
    """An existing store gains ``schedules.approved_tools`` +
    ``subtasks.schedule_id`` via additive reconcile — no rebuild, rows kept.

    This is the upgrade a deployed user hits: their store predates the
    scheduling-autonomy columns, so the daemon must ADD them (back-filling the
    NOT-NULL defaults) rather than wipe the schedule they already created.
    """
    from sqlalchemy import select, text

    from omni.storage.db import (
        code_schema_version,
        get_database,
        read_stored_schema_version,
        reset_databases,
    )
    from omni.storage.models import ScheduleORM, SubtaskORM

    s = load_settings()
    s.paths.ensure_dirs()
    db_path = s.paths.project_db

    db = get_database(db_path)
    await db.init()
    async with db.session() as session:
        session.add(ScheduleORM(id="sch-keep", skill_name="agent-goal", title="daily digest"))
        session.add(SubtaskORM(id="sub-keep", skill_name="agent-goal", status="succeeded"))
        await session.commit()

    # Simulate a store written before the new columns existed: drop the
    # indexed column's index first (SQLite refuses to drop an indexed column),
    # then the two columns. Watermark stays at the baseline — column adds are
    # reconciled on every init without bumping it.
    async with db.engine.begin() as conn:
        for row in (await conn.execute(text('PRAGMA index_list("subtasks")'))).fetchall():
            if "schedule_id" in str(row[1]):
                await conn.execute(text(f'DROP INDEX IF EXISTS "{row[1]}"'))
        await conn.execute(text('ALTER TABLE "subtasks" DROP COLUMN "schedule_id"'))
        await conn.execute(text('ALTER TABLE "schedules" DROP COLUMN "approved_tools"'))

    await reset_databases()
    db2 = get_database(db_path)
    await db2.init()  # additive reconcile path (generation marker intact)

    assert await read_stored_schema_version(db2) == code_schema_version()
    async with db2.engine.connect() as conn:
        sched_cols = {r[1] for r in (await conn.execute(text('PRAGMA table_info("schedules")'))).fetchall()}
        sub_cols = {r[1] for r in (await conn.execute(text('PRAGMA table_info("subtasks")'))).fetchall()}
    assert "approved_tools" in sched_cols
    assert "schedule_id" in sub_cols

    # Rows survived, and the freshly-added NOT-NULL columns were back-filled to
    # their declared defaults ([] / "") so the ORM can read them.
    async with db2.session() as session:
        sched = (await session.execute(
            select(ScheduleORM).where(ScheduleORM.id == "sch-keep")
        )).scalar_one()
        assert sched.title == "daily digest"
        assert sched.approved_tools == []
        sub = (await session.execute(
            select(SubtaskORM).where(SubtaskORM.id == "sub-keep")
        )).scalar_one()
        assert sub.schedule_id == ""


@pytest.mark.asyncio
async def test_additive_reconcile_adds_task_authority_fingerprints() -> None:
    """Existing task stores gain fail-closed approval authority columns."""
    from sqlalchemy import text

    from omni.storage.db import get_database, reset_databases

    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    async with db.session() as session:
        session.add(TaskORM(id="task-authority-upgrade", status="succeeded"))
        await session.commit()

    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                'ALTER TABLE "tasks" DROP COLUMN '
                '"current_authority_fingerprint"'
            )
        )
        await conn.execute(
            text(
                'ALTER TABLE "tasks" DROP COLUMN '
                '"approval_authority_fingerprint"'
            )
        )

    await reset_databases()
    upgraded = get_database(settings.paths.project_db)
    await upgraded.init()

    async with upgraded.engine.connect() as conn:
        columns = {
            row[1]
            for row in (
                await conn.execute(text('PRAGMA table_info("tasks")'))
            ).fetchall()
        }
    assert {
        "current_authority_fingerprint",
        "approval_authority_fingerprint",
    } <= columns
    async with upgraded.session() as session:
        task = await session.get(TaskORM, "task-authority-upgrade")
    assert task is not None
    assert task.current_authority_fingerprint == ""
    assert task.approval_authority_fingerprint == ""


@pytest.mark.asyncio
async def test_additive_reconcile_adds_tool_outcome_columns_without_losing_events() -> None:
    """Existing stores gain typed lifecycle fields without rebuilding history."""
    from sqlalchemy import select, text

    from omni.storage.db import get_database, reset_databases
    from omni.storage.models import TaskEventORM

    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    async with db.session() as session:
        session.add(TaskORM(id="task-outcome-upgrade", status="succeeded"))
        await session.commit()
        session.add(
            TaskEventORM(
                task_id="task-outcome-upgrade",
                seq=1,
                event_type="react.tool.done",
                status="succeeded",
                tool_name="get_task",
            )
        )
        await session.commit()

    async with db.engine.begin() as conn:
        await conn.execute(
            text('ALTER TABLE "task_events" DROP COLUMN "lifecycle_status"')
        )
        await conn.execute(
            text('ALTER TABLE "task_events" DROP COLUMN "result_success"')
        )

    await reset_databases()
    upgraded = get_database(settings.paths.project_db)
    await upgraded.init()

    async with upgraded.engine.connect() as conn:
        columns = {
            row[1]
            for row in (
                await conn.execute(text('PRAGMA table_info("task_events")'))
            ).fetchall()
        }
    assert {"lifecycle_status", "result_success"} <= columns
    async with upgraded.session() as session:
        event = (
            await session.execute(
                select(TaskEventORM).where(
                    TaskEventORM.task_id == "task-outcome-upgrade"
                )
            )
        ).scalar_one_or_none()
    assert event is not None
    assert event.tool_name == "get_task"
    assert event.lifecycle_status == ""
    assert event.result_success is None


@pytest.mark.asyncio
async def test_additive_reconcile_adds_async_provider_authority_columns() -> None:
    """Legacy workflow stores gain all fail-closed provider authority fields."""
    from sqlalchemy import text

    from omni.storage.db import get_database, reset_databases
    from omni.storage.models import SubtaskORM, WorkflowRunORM, WorkflowStepORM

    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    async with db.session() as session:
        session.add(TaskORM(id="task-provider-authority-upgrade", status="failed"))
        run = WorkflowRunORM(
            id="workflow-provider-authority-upgrade",
            task_id="task-provider-authority-upgrade",
            status="failed",
        )
        session.add(run)
        await session.flush()
        step = WorkflowStepORM(
            id="step-provider-authority-upgrade",
            workflow_run_id=run.id,
            task_id=run.task_id,
            step_key="step",
            status="failed",
        )
        session.add(step)
        await session.flush()
        session.add(
            SubtaskORM(
                id="subtask-provider-authority-upgrade",
                task_id=run.task_id,
                workflow_run_id=run.id,
                workflow_step_id=step.id,
                skill_name="legacy",
                status="failed",
            )
        )
        await session.commit()

    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                'ALTER TABLE "workflow_runs" DROP COLUMN '
                '"execution_authority_json"'
            )
        )
        await conn.execute(
            text(
                'ALTER TABLE "workflow_steps" DROP COLUMN '
                '"provider_authority_json"'
            )
        )
        await conn.execute(
            text(
                'ALTER TABLE "subtasks" DROP COLUMN '
                '"provider_authority_json"'
            )
        )

    await reset_databases()
    upgraded = get_database(settings.paths.project_db)
    await upgraded.init()

    async with upgraded.engine.connect() as conn:
        columns = {}
        for table in ("workflow_runs", "workflow_steps", "subtasks"):
            columns[table] = {
                row[1]
                for row in (
                    await conn.execute(
                        text(f'PRAGMA table_info("{table}")')
                    )
                ).fetchall()
            }
    assert "execution_authority_json" in columns["workflow_runs"]
    assert "provider_authority_json" in columns["workflow_steps"]
    assert "provider_authority_json" in columns["subtasks"]
    async with upgraded.session() as session:
        run = await session.get(
            WorkflowRunORM,
            "workflow-provider-authority-upgrade",
        )
        step = await session.get(
            WorkflowStepORM,
            "step-provider-authority-upgrade",
        )
        subtask = await session.get(
            SubtaskORM,
            "subtask-provider-authority-upgrade",
        )
    assert run is not None and run.execution_authority_json == {}
    assert step is not None and step.provider_authority_json == {}
    assert subtask is not None and subtask.provider_authority_json == {}


@pytest.mark.asyncio
async def test_additive_reconcile_backfills_artifact_owner_and_cleans_foreign_cache():
    """A pre-owner store gains canonical artifact ownership without losing rows."""
    import json
    import sqlite3

    from omni.storage.db import get_database, reset_databases

    s = load_settings()
    s.paths.ensure_dirs()
    db_path = s.paths.project_db
    owner_id = "abe000184a221234567890abcdefabcd"
    polluted_id = "4497f10e7aab1234567890abcdefabcd"
    artifact_id = "27a6c3fc634143b2a7a86bcf9197c10e"
    direct_artifact_id = "a2efebaee12b4b3f8624f722c990a26a"

    await reset_databases()
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        con.execute(
            "CREATE TABLE tasks ("
            "id VARCHAR(40) PRIMARY KEY, session_id VARCHAR(40), artifact_ids JSON)"
        )
        con.execute(
            "CREATE TABLE subtasks ("
            "id VARCHAR(40) PRIMARY KEY, task_id VARCHAR(40), "
            "skill_name VARCHAR(128), status VARCHAR(24))"
        )
        con.execute(
            "CREATE TABLE artifacts ("
            "id VARCHAR(40) PRIMARY KEY, subtask_id VARCHAR(40), "
            "workflow_run_id VARCHAR(40), session_id VARCHAR(40), "
            "uri VARCHAR(1024), rel_path VARCHAR(1024), "
            "kind VARCHAR(32), metadata JSON)"
        )
        for task_id in (owner_id, polluted_id):
            con.execute(
                "INSERT INTO tasks (id, session_id, artifact_ids) VALUES (?, ?, ?)",
                (task_id, "session-1", json.dumps([artifact_id])),
            )
        con.execute(
            "INSERT INTO subtasks (id, task_id, skill_name, status) VALUES (?, ?, ?, ?)",
            ("sub-owner", owner_id, "scientific-figure", "succeeded"),
        )
        con.execute(
            "INSERT INTO artifacts (id, subtask_id, uri, kind) VALUES (?, ?, ?, ?)",
            (artifact_id, "sub-owner", f"artifact://{artifact_id}", "figure"),
        )
        con.execute(
            "INSERT INTO artifacts "
            "(id, session_id, uri, rel_path, kind, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                direct_artifact_id,
                "session-1",
                f"artifact://{direct_artifact_id}",
                (
                    "reports/RAG-review-"
                    f"{owner_id[:8]}-{direct_artifact_id[:8]}.md"
                ),
                "report",
                "{}",
            ),
        )
        con.execute("PRAGMA application_id = 0x4F4D4E33")
        con.execute("PRAGMA user_version = 1")
    finally:
        con.close()

    db2 = get_database(db_path)
    await db2.init()

    async with db2.session() as session:
        artifact = await session.get(ArtifactORM, artifact_id)
        direct_artifact = await session.get(ArtifactORM, direct_artifact_id)
        owner = await session.get(TaskORM, owner_id)
        polluted = await session.get(TaskORM, polluted_id)
    assert artifact is not None and artifact.task_id == owner_id
    assert direct_artifact is not None and direct_artifact.task_id == owner_id
    assert owner is not None and owner.artifact_ids == [
        artifact_id,
        direct_artifact_id,
    ]
    assert polluted is not None and polluted.artifact_ids == []


@pytest.mark.asyncio
async def test_current_store_init_is_a_reader_while_another_writer_holds_the_lock():
    """``/task show`` opens an already-current store; it must not UPDATE artifacts
    (or otherwise take the write lock) while ``omni serve`` is writing.
    """
    import sqlite3
    import time

    from omni.storage.db import get_database, reset_databases

    s = load_settings()
    s.paths.ensure_dirs()
    db_path = s.paths.project_db
    db = get_database(db_path)
    await db.init()
    async with db.session() as session:
        session.add(TaskORM(id="task-current-reader", status="succeeded", kind="turn"))
        await session.commit()

    await reset_databases()
    holder = sqlite3.connect(str(db_path), timeout=0.1)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE tasks SET status = status")
        db2 = get_database(db_path)
        started = time.monotonic()
        await db2.init()
        assert time.monotonic() - started < 1.0
        async with db2.session() as session:
            task = await session.get(TaskORM, "task-current-reader")
        assert task is not None and task.status == "succeeded"
    finally:
        holder.execute("ROLLBACK")
        holder.close()


@pytest.mark.asyncio
async def test_current_store_reopen_does_not_rerun_artifact_owner_backfill(monkeypatch):
    """Owner backfill is a one-shot for stores that just gained ``task_id``."""
    from omni.storage import db as storage_db
    from omni.storage.db import get_database, reset_databases

    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()

    calls = {"n": 0}

    async def boom(_conn):  # noqa: ANN001
        calls["n"] += 1
        raise AssertionError("artifact owner backfill must not run on a current store")

    monkeypatch.setattr(storage_db, "_reconcile_artifact_task_ownership", boom)
    await reset_databases()
    db2 = get_database(s.paths.project_db)
    await db2.init()
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_locked_additive_migrate_does_not_drop_the_store():
    """A busy lock during needed DDL is a queue, not a signal to wipe the file."""
    import sqlite3

    import pytest
    from sqlalchemy.exc import OperationalError

    from omni.storage.db import Database, get_database, reset_databases

    s = load_settings()
    s.paths.ensure_dirs()
    db_path = s.paths.project_db
    db = get_database(db_path)
    await db.init()
    async with db.session() as session:
        session.add(TaskORM(id="task-keep-on-lock", status="succeeded", kind="turn"))
        await session.commit()
    async with db.engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(
            text('ALTER TABLE "tasks" DROP COLUMN "current_authority_fingerprint"')
        )

    await reset_databases()
    holder = sqlite3.connect(str(db_path), timeout=0.1)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE tasks SET status = status")
        locked = Database(db_path, busy_timeout_ms=50)
        with pytest.raises(OperationalError, match="locked|busy"):
            await locked.init()
        await locked.dispose()
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    restored = get_database(db_path)
    await restored.init()
    async with restored.session() as session:
        task = await session.get(TaskORM, "task-keep-on-lock")
    assert task is not None
    assert task.status == "succeeded"
    assert task.current_authority_fingerprint == ""


@pytest.mark.asyncio
async def test_artifact_owner_reconcile_leaves_conflicting_legacy_links_unresolved():
    from omni.storage.db import get_database, reset_databases
    from omni.storage.models import SubtaskORM, WorkflowRunORM

    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    async with db.session() as session:
        session.add_all(
            [
                TaskORM(id="task-a", status="succeeded", kind="turn"),
                TaskORM(id="task-b", status="succeeded", kind="turn"),
            ]
        )
        await session.flush()
        session.add(
            WorkflowRunORM(
                id="workflow-b",
                task_id="task-b",
                status="succeeded",
            )
        )
        await session.flush()
        session.add(
            SubtaskORM(
                id="subtask-a",
                task_id="task-a",
                workflow_run_id="workflow-b",
                skill_name="scientific-figure",
                status="succeeded",
            )
        )
        await session.flush()
        session.add(
            ArtifactORM(
                id="artifact-conflict",
                subtask_id="subtask-a",
                workflow_run_id="workflow-b",
                uri="artifact://artifact-conflict",
                kind="figure",
            )
        )
        await session.commit()

    await reset_databases()
    db2 = get_database(s.paths.project_db)
    await db2.init()
    async with db2.session() as session:
        artifact = await session.get(ArtifactORM, "artifact-conflict")
    assert artifact is not None
    assert artifact.task_id is None


@pytest.mark.asyncio
async def test_interim_watermark_normalized_to_baseline():
    """An interim on-disk watermark (< 100) is normalized back to the baseline."""
    from sqlalchemy import text

    from omni.storage.db import (
        code_schema_version,
        get_database,
        read_stored_schema_version,
        reset_databases,
    )

    s = load_settings()
    s.paths.ensure_dirs()
    db_path = s.paths.project_db

    db = get_database(db_path)
    await db.init()
    async with db.engine.begin() as conn:
        await conn.execute(text("PRAGMA user_version = 5"))  # interim, not reserved-future

    await reset_databases()
    db2 = get_database(db_path)
    await db2.init()
    assert await read_stored_schema_version(db2) == code_schema_version() == 1


@pytest.mark.asyncio
async def test_newer_store_is_not_rebuilt_on_downgrade():
    """A store whose watermark is ahead of this build is preserved (never wiped)."""
    from sqlalchemy import select, text

    from omni.storage.db import get_database, read_stored_schema_version, reset_databases
    from omni.storage.models import MemoryEntryORM

    s = load_settings()
    s.paths.ensure_dirs()
    db_path = s.paths.project_db

    db = get_database(db_path)
    await db.init()
    async with db.session() as session:
        session.add(MemoryEntryORM(id="keep-me", layer="M4", scope="user", summary="do not lose"))
        await session.commit()
    # Simulate the file having been written by a *newer* build (reserved
    # forward-compat watermark range ≥ 100).
    async with db.engine.begin() as conn:
        await conn.execute(text("PRAGMA user_version = 999"))

    await reset_databases()
    db2 = get_database(db_path)
    await db2.init()

    # Ahead watermark left intact and the row survived (no destructive rebuild).
    assert await read_stored_schema_version(db2) == 999
    async with db2.session() as session:
        got = (await session.execute(
            select(MemoryEntryORM).where(MemoryEntryORM.id == "keep-me")
        )).scalar_one_or_none()
    assert got is not None and got.summary == "do not lose"


@pytest.mark.asyncio
async def test_artifact_store_put_and_resolve():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    store = ArtifactStore(s.paths, db)
    art = await store.put_bytes(b"# report", kind="report", title="t", ext="md", mime="text/markdown")
    assert art.uri.startswith("artifact://")
    path = await store.resolve_path(art.uri)
    assert path is not None and path.read_bytes() == b"# report"
    recent = await store.list_recent()
    assert any(a.id == art.id for a in recent)


@pytest.mark.asyncio
async def test_artifact_store_refuses_registered_path_outside_workspace(tmp_path):
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    store = ArtifactStore(s.paths, db)
    art = await store.put_bytes(b"inside", kind="report", title="t", ext="md")
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside")

    async with db.session() as session:
        row = await session.get(ArtifactORM, art.id)
        assert row is not None
        row.rel_path = str(outside)
        await session.commit()

    assert await store.resolve_path(art.uri) is None


@pytest.mark.asyncio
async def test_artifact_store_names_files_semantically():
    """On-disk names are ``<slug>-<task8>-<art8>.<ext>`` — readable and
    attributable to the owning task instead of a bare hash."""
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    store = ArtifactStore(s.paths, db)

    art = await store.put_bytes(
        "# 综述".encode(),
        kind="report",
        title="RAG 全自动领航员优化综述",
        ext="md",
        mime="text/markdown",
        task_id="27023bf7xxxx",
    )
    assert art.path.name == f"RAG-全自动领航员优化综述-27023bf7-{art.id[:8]}.md"
    assert (await store.resolve_path(art.uri)) == art.path

    # Titles that end with the format keep a single extension mention.
    fig = await store.put_bytes(
        b"<svg/>",
        kind="figure",
        title="RAG 系统架构图 SVG",
        ext="svg",
        mime="image/svg+xml",
        task_id="27023bf7xxxx",
    )
    assert fig.path.name == f"RAG-系统架构图-27023bf7-{fig.id[:8]}.svg"

    # Unsafe path characters are scrubbed; without a task id the art id remains.
    odd = await store.put_bytes(b"x", kind="report", title='a/b\\c:d*e?"f<g>h|i', ext="txt", mime="text/plain")
    assert "/" not in odd.path.name and "\\" not in odd.path.name and ":" not in odd.path.name
    assert odd.path.name.endswith(f"-{odd.id[:8]}.txt")

    # No title → falls back to the artifact kind, never a bare hash.
    anon = await store.put_bytes(b"y", kind="data", title="", ext="json", mime="application/json")
    assert anon.path.name == f"data-{anon.id[:8]}.json"


@pytest.mark.asyncio
async def test_artifact_store_persists_the_full_producing_task_id():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    task_id = "4497f10e7aab1234567890abcdefabcd"
    async with db.session() as session:
        session.add(TaskORM(id=task_id, status="running", kind="turn"))
        await session.commit()

    art = await ArtifactStore(s.paths, db).put_bytes(
        b"# owned",
        kind="report",
        title="Owned report",
        ext="md",
        mime="text/markdown",
        task_id=task_id,
    )

    async with db.session() as session:
        row = await session.get(ArtifactORM, art.id)
    assert row is not None
    assert row.task_id == task_id
    assert art.path.name.endswith(f"-{task_id[:8]}-{art.id[:8]}.md")


@pytest.mark.asyncio
async def test_artifact_store_resolves_only_exact_or_unique_leading_prefixes():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    artifact_id = "27a6c3fc634143b2a7a86bcf9197c10e"
    async with db.session() as session:
        session.add(
            ArtifactORM(
                id=artifact_id,
                uri=f"artifact://{artifact_id}",
                kind="report",
            )
        )
        await session.commit()

    store = ArtifactStore(s.paths, db)
    assert (await store.get("27a6c3fc")).id == artifact_id
    assert await store.get(artifact_id[-8:]) is None

    colliding_id = "27a6c3fc000000000000000000000002"
    async with db.session() as session:
        session.add(
            ArtifactORM(
                id=colliding_id,
                uri=f"artifact://{colliding_id}",
                kind="report",
            )
        )
        await session.commit()
    assert await store.get("27a6c3fc") is None
    assert (await store.get("27a6c3fc6")).id == artifact_id


@pytest.mark.asyncio
async def test_artifact_store_put_file_uses_source_stem_when_untitled(tmp_path):
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    store = ArtifactStore(s.paths, db)

    src = tmp_path / "experiment-notes.md"
    src.write_text("# notes", encoding="utf-8")
    art = await store.put_file(src, kind="report", mime="text/markdown", task_id="taskabcd")
    assert art.path.name == f"experiment-notes-taskabcd-{art.id[:8]}.md"
    assert (await store.resolve_path(art.uri)) == art.path


@pytest.mark.asyncio
async def test_concurrent_init_on_one_file_does_not_deadlock(tmp_path):
    """Two Database objects must share an asyncio lock, not a blocking flock."""
    import asyncio

    from omni.storage.db import Database

    path = tmp_path / "concurrent.sqlite3"
    first = Database(path)
    second = Database(path)
    try:
        await asyncio.wait_for(asyncio.gather(first.init(), second.init()), timeout=5)
        assert first._initialized
        assert second._initialized
    finally:
        await first.dispose()
        await second.dispose()


@pytest.mark.asyncio
async def test_init_lock_wait_does_not_block_the_event_loop(tmp_path):
    """A held init.lock must wait in a worker thread so other tasks keep running."""
    import asyncio
    import os
    import sys

    if sys.platform == "win32":
        pytest.skip("fcntl flock is not used on Windows")

    import fcntl

    from omni.storage.db import Database

    path = tmp_path / "blocked.sqlite3"
    lock_path = path.with_name(f"{path.name}.init.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    progress = 0

    async def ticker() -> None:
        nonlocal progress
        for _ in range(8):
            await asyncio.sleep(0.04)
            progress += 1

    db = Database(path)
    try:
        init_task = asyncio.create_task(db.init())
        tick_task = asyncio.create_task(ticker())
        await asyncio.sleep(0.2)
        assert progress >= 3
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        fd = -1
        await asyncio.wait_for(init_task, timeout=5)
        await tick_task
        assert db._initialized
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        await db.dispose()


def test_slugify_filename_preserves_cjk_and_limits_length():
    from omni.storage.artifacts import artifact_filename, slugify_filename

    assert slugify_filename("RAG 系统 架构图") == "RAG-系统-架构图"
    assert slugify_filename("Draft   report\tv2") == "Draft-report-v2"
    assert slugify_filename(':*?"<>|') == ""
    assert slugify_filename("") == ""
    assert (
        artifact_filename(
            title="RAG系统架构图", kind="figure", art_id="aabbccdd1122", ext="dot", task_id="27023bf7"
        )
        == "RAG系统架构图-27023bf7-aabbccdd.dot"
    )
    assert len(slugify_filename("很长的标题" * 40)) <= 60
