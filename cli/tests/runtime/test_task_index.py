"""Global task index — cross-workspace list + routing (control.sqlite3).

Exercises the control/index tier of the two-tier task store: task writes in one
workspace's ``sessions.sqlite3`` are mirrored into the machine-global index so a
CLI resolved to a *different* workspace can still list and open them. This is the
fix for the "``--all`` lists it, ``show <id>`` can't find it" bug.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from omni.config import load_settings
from omni.config.workspaces import register_workspace
from omni.runtime import task_index as task_index_mod
from omni.runtime.aggregate import list_tasks_all_workspaces
from omni.runtime.task_index import (
    TaskIndex,
    reconcile_index,
    resolve_task_workspace,
)
from omni.runtime.task_recorder import TaskRecorder
from omni.storage.db import get_database
from omni.storage.models import TaskIndexORM, TaskORM


@pytest.fixture(autouse=True)
def _reset_reconcile_guard():
    """The one-shot reconcile guard is process-global; clear it per test."""
    task_index_mod._reconciled.clear()
    yield
    task_index_mod._reconciled.clear()


async def _workspace(name: str):
    """A named ``-P`` workspace (its own sessions DB, shared control DB)."""
    s = load_settings(project=name)
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    register_workspace(s.paths)
    return s, db


async def _index_rows(control_path) -> list[TaskIndexORM]:
    store = get_database(control_path)
    await store.init()
    async with store.session() as s:
        return list((await s.execute(select(TaskIndexORM))).scalars().all())


@pytest.mark.asyncio
async def test_create_task_dual_writes_index_row():
    s, db = await _workspace("alpha")
    rec = TaskRecorder(
        db, project=s.paths.project_name, index=TaskIndex.for_workspace(s.paths)
    )
    task = await rec.create_task(
        session_id="sess1", channel="cli", user_input="hello world", external_key="k1"
    )

    rows = await _index_rows(s.paths.control_db)
    assert len(rows) == 1
    row = rows[0]
    assert row.task_id == task.id
    assert row.project_dir == str(s.paths.project_dir)
    assert row.workspace_label == s.paths.project_name
    assert row.status == "running"
    assert row.title


@pytest.mark.asyncio
async def test_status_transition_and_delete_keep_index_in_sync():
    s, db = await _workspace("alpha")
    rec = TaskRecorder(
        db, project=s.paths.project_name, index=TaskIndex.for_workspace(s.paths)
    )
    task = await rec.create_task(
        session_id="s", channel="cli", user_input="do a thing", external_key="k"
    )

    await rec.finish_task(task.id, status="cancelled", summary="stop")
    rows = await _index_rows(s.paths.control_db)
    assert [r.status for r in rows] == ["cancelled"]

    await rec.delete_task(task.id)
    assert await _index_rows(s.paths.control_db) == []


@pytest.mark.asyncio
async def test_delete_parent_removes_the_complete_task_tree_from_index():
    """DB cascade and the machine-global index must delete the same closure."""
    s, db = await _workspace("alpha")
    rec = TaskRecorder(
        db,
        project=s.paths.project_name,
        index=TaskIndex.for_workspace(s.paths),
        classify_conversational=False,
    )
    parent = await rec.create_task(
        session_id="s",
        channel="cli",
        user_input="parent",
        external_key="parent-key",
    )
    child = await rec.create_task(
        session_id="s",
        channel="cli",
        user_input="child",
        external_key="child-key",
        parent_task_id=parent.id,
        kind="subagent",
        depth=1,
    )
    await rec.finish_task(child.id, status="failed", error="expected test failure")
    await rec.finish_task(parent.id, status="failed", error="expected test failure")
    assert {row.task_id for row in await _index_rows(s.paths.control_db)} == {
        parent.id,
        child.id,
    }

    assert await rec.delete_task(parent.id) is True

    async with db.session() as session:
        assert await session.get(TaskORM, parent.id) is None
        assert await session.get(TaskORM, child.id) is None
    assert await _index_rows(s.paths.control_db) == []


@pytest.mark.asyncio
async def test_delete_large_task_tree_removes_every_index_row():
    """>1000 descendants exercises closure collection and batched index cleanup."""
    settings, db = await _workspace("large-tree")
    root_id = "large-root"
    child_ids = [f"large-child-{i:04d}" for i in range(1001)]
    async with db.session() as session:
        session.add(
            TaskORM(id=root_id, status="failed", title="large root", kind="turn")
        )
        session.add_all(
            TaskORM(
                id=task_id,
                parent_task_id=root_id,
                status="failed",
                title=task_id,
                kind="subagent",
            )
            for task_id in child_ids
        )
        await session.commit()
    async with db.session() as session:
        tree = list((await session.execute(select(TaskORM))).scalars().all())

    index = TaskIndex.for_workspace(settings.paths)
    await index.record_many(tree)
    assert len(await _index_rows(settings.paths.control_db)) == 1002
    recorder = TaskRecorder(db, project=settings.paths.project_name, index=index)

    assert await recorder.delete_task(root_id) is True

    async with db.session() as session:
        assert (await session.execute(select(TaskORM))).scalars().all() == []
    assert await _index_rows(settings.paths.control_db) == []


@pytest.mark.asyncio
async def test_resolve_routes_a_task_id_to_its_owning_workspace():
    alpha, adb = await _workspace("alpha")
    beta, _bdb = await _workspace("beta")  # the CLI's current (wrong) workspace

    rec = TaskRecorder(
        adb, project=alpha.paths.project_name, index=TaskIndex.for_workspace(alpha.paths)
    )
    task = await rec.create_task(
        session_id="s", channel="cli", user_input="deep research", external_key="k"
    )

    # Full id and a unique prefix both route back to alpha, from beta's context.
    for ident in (task.id, task.id[:8]):
        target = await resolve_task_workspace(beta, ident)
        assert target is not None
        assert str(target.paths.project_dir) == str(alpha.paths.project_dir)


@pytest.mark.asyncio
async def test_resolve_self_heals_for_a_legacy_task_without_an_index_row():
    alpha, adb = await _workspace("alpha")
    beta, _bdb = await _workspace("beta")

    # Legacy task: written with no index (pre-index behaviour), so nothing was
    # dual-written to control.sqlite3.
    rec = TaskRecorder(adb, project=alpha.paths.project_name)
    task = await rec.create_task(
        session_id="s", channel="cli", user_input="legacy task", external_key="k"
    )
    assert await _index_rows(alpha.paths.control_db) == []

    target = await resolve_task_workspace(beta, task.id)
    assert target is not None
    assert str(target.paths.project_dir) == str(alpha.paths.project_dir)
    # Resolution backfilled the row, so the next lookup is a cheap index hit.
    assert any(r.task_id == task.id for r in await _index_rows(alpha.paths.control_db))


@pytest.mark.asyncio
async def test_resolve_returns_none_for_unknown_id():
    _alpha, _adb = await _workspace("alpha")
    beta, _bdb = await _workspace("beta")
    assert await resolve_task_workspace(beta, "does-not-exist") is None


@pytest.mark.asyncio
async def test_cross_workspace_task_prefix_resolution_fails_closed_when_ambiguous():
    alpha, adb = await _workspace("alpha")
    beta, bdb = await _workspace("beta")
    async with adb.session() as s:
        s.add(
            TaskORM(
                id="deadbeef000000000000000000000001",
                status="succeeded",
                title="alpha",
                kind="turn",
            )
        )
        await s.commit()
    async with bdb.session() as s:
        s.add(
            TaskORM(
                id="deadbeef000000000000000000000002",
                status="succeeded",
                title="beta",
                kind="turn",
            )
        )
        await s.commit()

    assert await resolve_task_workspace(beta, "deadbeef") is None


@pytest.mark.asyncio
async def test_backfill_workspace_is_marker_guarded():
    alpha, adb = await _workspace("alpha")
    async with adb.session() as s:
        s.add(TaskORM(id="t_legacy", status="succeeded", title="x", kind="turn"))
        await s.commit()

    idx = TaskIndex.for_workspace(alpha.paths)
    marker = alpha.paths.project_dir / ".task_index_backfilled"
    await idx.backfill_workspace(adb, marker=marker)
    assert marker.exists()
    assert any(r.task_id == "t_legacy" for r in await _index_rows(alpha.paths.control_db))

    # A second call is a no-op: a task added after the marker is not imported.
    async with adb.session() as s:
        s.add(TaskORM(id="t_after", status="succeeded", title="y", kind="turn"))
        await s.commit()
    await idx.backfill_workspace(adb, marker=marker)
    assert not any(r.task_id == "t_after" for r in await _index_rows(alpha.paths.control_db))


@pytest.mark.asyncio
async def test_reconcile_imports_every_registered_workspace():
    alpha, adb = await _workspace("alpha")
    beta, bdb = await _workspace("beta")
    async with adb.session() as s:
        s.add(TaskORM(id="a_task", status="succeeded", title="a", kind="turn"))
        await s.commit()
    async with bdb.session() as s:
        s.add(TaskORM(id="b_task", status="succeeded", title="b", kind="turn"))
        await s.commit()

    await reconcile_index(alpha.paths.home, control_db=alpha.paths.control_db, force=True)

    indexed = {r.task_id for r in await _index_rows(alpha.paths.control_db)}
    assert {"a_task", "b_task"} <= indexed


@pytest.mark.asyncio
async def test_list_all_workspaces_syncs_the_index():
    alpha, adb = await _workspace("alpha")
    rec = TaskRecorder(adb, project=alpha.paths.project_name)  # legacy: no dual-write
    task = await rec.create_task(
        session_id="s", channel="cli", user_input="aggregate me", external_key="k"
    )

    rows = await list_tasks_all_workspaces(home=alpha.paths.home)
    assert any(r.id == task.id for r in rows)
    # The scan mirrored the row into the index so a later show can route to it.
    assert any(r.task_id == task.id for r in await _index_rows(alpha.paths.control_db))
