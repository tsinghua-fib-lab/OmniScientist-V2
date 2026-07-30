"""Task history management — `task rm / clear / prune` runtime backbone.

Covers the storage-level operations that back the CLI:
- ``delete_subtask`` removes one row by id;
- ``clear_subtasks`` protects running/succeeded by default and honours filters;
- ``subtask_has_artifacts`` flags provenance so the CLI can require ``--force``;
- ``dry_run`` counts without deleting (the confirm-preview path).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from omni.config import load_settings
from omni.runtime.notifications import InboxNotifier
from omni.runtime.subtask_runtime import SubtaskRuntime
from omni.runtime.task_recorder import TaskRecorder
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database
from omni.storage.models import (
    ArtifactORM,
    SubtaskORM,
    TaskEventORM,
    TaskORM,
    WorkflowRunORM,
    WorkflowStepORM,
)


async def _runtime():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    reg = SkillRegistry(s)
    reg.build_index()
    inbox = InboxNotifier(s.paths.project_dir / "inbox.jsonl")

    def ctx_factory(session_id, channel):  # noqa: ANN001
        from omni.skills_runtime.context import ExecContext
        from omni.storage.artifacts import ArtifactStore

        return ExecContext(
            settings=s, paths=s.paths, session_id=session_id, channel=channel,
            db=db, artifacts=ArtifactStore(s.paths, db), llm=None,
        )

    return SubtaskRuntime(db, s, reg, ctx_factory, notifier=inbox), db


async def _seed(db, rows: list[dict]) -> None:
    async with db.session() as s:
        for r in rows:
            s.add(SubtaskORM(**r))
        await s.commit()


@pytest.mark.asyncio
async def test_delete_task_by_id():
    rt, db = await _runtime()
    await _seed(db, [{"id": "del00001", "skill_name": "x", "status": "failed"}])
    assert await rt.delete_subtask("del00001") is True
    assert await rt.get_subtask("del00001") is None
    assert await rt.delete_subtask("nope") is False


@pytest.mark.asyncio
async def test_clear_tasks_protects_running_and_succeeded_by_default():
    rt, db = await _runtime()
    await _seed(db, [
        {"id": "t_fail", "skill_name": "x", "status": "failed"},
        {"id": "t_pend", "skill_name": "x", "status": "pending"},
        {"id": "t_run", "skill_name": "x", "status": "running"},
        {"id": "t_ok", "skill_name": "x", "status": "succeeded"},
    ])
    # default protect = running + succeeded → only failed/pending are eligible
    removed = await rt.clear_subtasks()
    assert removed == 2
    remaining = {t.id for t in await rt.list_subtasks(limit=50)}
    assert remaining == {"t_run", "t_ok"}


@pytest.mark.asyncio
async def test_clear_tasks_status_filter_and_force_succeeded():
    rt, db = await _runtime()
    await _seed(db, [
        {"id": "t_fail", "skill_name": "x", "status": "failed"},
        {"id": "t_ok", "skill_name": "x", "status": "succeeded"},
    ])
    # status filter: only failed
    assert await rt.clear_subtasks(status="failed") == 1
    # succeeded still protected unless we relax `protect`
    assert await rt.clear_subtasks(status="succeeded") == 0
    assert await rt.clear_subtasks(status="succeeded", protect=("running",)) == 1
    assert await rt.list_subtasks(limit=50) == []


@pytest.mark.asyncio
async def test_clear_tasks_before_cutoff_and_dry_run():
    rt, db = await _runtime()
    old = datetime.now(UTC) - timedelta(days=40)
    new = datetime.now(UTC)
    await _seed(db, [
        {"id": "t_old", "skill_name": "x", "status": "failed", "created_at": old},
        {"id": "t_new", "skill_name": "x", "status": "failed", "created_at": new},
    ])
    cutoff = datetime.now(UTC) - timedelta(days=30)
    # dry run counts only the old one and deletes nothing
    assert await rt.clear_subtasks(before=cutoff, dry_run=True) == 1
    assert len(await rt.list_subtasks(limit=50)) == 2
    # real run removes the old one, keeps the recent one
    assert await rt.clear_subtasks(before=cutoff) == 1
    remaining = {t.id for t in await rt.list_subtasks(limit=50)}
    assert remaining == {"t_new"}


@pytest.mark.asyncio
async def test_archive_task_hides_from_default_lists_but_keeps_exact_lookup():
    rt, db = await _runtime()
    await _seed(db, [
        {"id": "t_archive", "skill_name": "x", "status": "succeeded"},
        {"id": "t_visible", "skill_name": "x", "status": "failed"},
    ])

    assert await rt.archive_subtask("t_archive", reason="old run") is True
    visible = {t.id for t in await rt.list_subtasks(limit=50)}
    assert visible == {"t_visible"}
    all_rows = {t.id for t in await rt.list_subtasks(limit=50, include_archived=True)}
    assert all_rows == {"t_archive", "t_visible"}
    archived = await rt.get_subtask("t_archive")
    assert archived is not None
    assert archived.archived_at is not None
    assert archived.archived_reason == "old run"

    # Archive is not deletion; explicit restore puts it back into normal lists.
    assert await rt.unarchive_subtask("t_archive") is True
    visible_again = {t.id for t in await rt.list_subtasks(limit=50)}
    assert visible_again == {"t_archive", "t_visible"}


@pytest.mark.asyncio
async def test_task_has_artifacts_detects_inline_and_table_refs():
    rt, db = await _runtime()
    await _seed(db, [
        {"id": "t_inline", "skill_name": "x", "status": "succeeded",
         "result_json": {"report_uri": "artifact://abc123"}},
        {"id": "t_table", "skill_name": "x", "status": "succeeded", "result_json": {}},
        {"id": "t_none", "skill_name": "x", "status": "succeeded", "result_json": {"summary": "no art"}},
    ])
    async with db.session() as s:
        s.add(ArtifactORM(uri="artifact://xyz", kind="figure", subtask_id="t_table"))
        await s.commit()

    inline = await rt.get_subtask("t_inline")
    table = await rt.get_subtask("t_table")
    none = await rt.get_subtask("t_none")
    assert await rt.subtask_has_artifacts(inline) is True
    assert await rt.subtask_has_artifacts(table) is True
    assert await rt.subtask_has_artifacts(none) is False


# ---------------------------------------------------------------------------
# Task-level lifecycle (TaskRecorder) — the layer `task rm/clear/archive`
# actually calls after the schema reset.
# ---------------------------------------------------------------------------


async def _recorder():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return TaskRecorder(db, project=s.paths.project_name), db


async def _seed_task_tree(db, task_id: str, *, status: str = "failed") -> None:
    """One task + one subtask + one event, to observe FK cascade on delete."""
    async with db.session() as s:
        s.add(TaskORM(id=task_id, status=status, title=f"task {task_id}", kind="turn"))
        await s.flush()
        s.add(SubtaskORM(id=f"sub-{task_id}", task_id=task_id, skill_name="x", status="succeeded"))
        s.add(TaskEventORM(id=f"evt-{task_id}", task_id=task_id, seq=1, event_type="task.ack"))
        await s.commit()


async def _seed_child_task(
    db,
    task_id: str,
    *,
    parent_task_id: str,
    status: str,
) -> None:
    """Add a delegated child whose lifecycle must protect its whole tree."""
    async with db.session() as s:
        s.add(
            TaskORM(
                id=task_id,
                parent_task_id=parent_task_id,
                status=status,
                title=f"child {task_id}",
                kind="subagent",
            )
        )
        await s.commit()


async def _seed_execution(
    db,
    task_id: str,
    *,
    object_kind: str,
    status: str = "running",
) -> None:
    """Attach one execution object whose active lease protects its owner."""
    async with db.session() as s:
        if object_kind == "workflow_run":
            s.add(
                WorkflowRunORM(
                    id=f"run-{task_id}",
                    task_id=task_id,
                    goal="active run",
                    status=status,
                )
            )
        elif object_kind == "workflow_step":
            s.add(
                WorkflowRunORM(
                    id=f"run-{task_id}",
                    task_id=task_id,
                    goal="settled run with active step",
                    status="succeeded",
                )
            )
            await s.flush()
            s.add(
                WorkflowStepORM(
                    id=f"step-{task_id}",
                    workflow_run_id=f"run-{task_id}",
                    task_id=task_id,
                    step_key="active-step",
                    status=status,
                )
            )
        elif object_kind == "subtask":
            s.add(
                SubtaskORM(
                    id=f"active-sub-{task_id}",
                    task_id=task_id,
                    skill_name="x",
                    status=status,
                )
            )
        else:  # pragma: no cover - test helper contract
            raise AssertionError(f"unsupported execution object: {object_kind}")
        await s.commit()


@pytest.mark.asyncio
async def test_delete_task_cascades_to_subtasks_and_events():
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_del")
    assert await rec.delete_task("tk_del") is True
    async with db.session() as s:
        assert await s.get(TaskORM, "tk_del") is None
        assert await s.get(SubtaskORM, "sub-tk_del") is None
        assert await s.get(TaskEventORM, "evt-tk_del") is None
    assert await rec.delete_task("absent") is False


@pytest.mark.asyncio
async def test_delete_task_preserves_artifact_and_clears_direct_owner():
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_artifact")
    async with db.session() as s:
        s.add(
            ArtifactORM(
                id="artifact-keep",
                task_id="tk_artifact",
                subtask_id="sub-tk_artifact",
                uri="artifact://artifact-keep",
                kind="report",
            )
        )
        await s.commit()

    assert await rec.delete_task("tk_artifact") is True
    async with db.session() as s:
        artifact = await s.get(ArtifactORM, "artifact-keep")
    assert artifact is not None
    assert artifact.task_id is None
    assert artifact.subtask_id is None


@pytest.mark.parametrize("descendant_status", ["running", "recovering", "succeeded"])
@pytest.mark.asyncio
async def test_legacy_delete_task_cannot_bypass_descendant_protection(
    descendant_status: str,
):
    """The compatibility bool API must apply the same full-tree policy."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_parent", status="failed")
    await _seed_child_task(
        db,
        "tk_child",
        parent_task_id="tk_parent",
        status=descendant_status,
    )

    assert await rec.delete_task("tk_parent") is False
    async with db.session() as s:
        assert await s.get(TaskORM, "tk_parent") is not None
        assert await s.get(TaskORM, "tk_child") is not None


@pytest.mark.asyncio
async def test_clear_tasks_outcome_buckets_and_dry_run():
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_fail", status="failed")
    await _seed_task_tree(db, "tk_cancel", status="cancelled")
    await _seed_task_tree(db, "tk_ok", status="succeeded")
    await _seed_task_tree(db, "tk_run", status="running")

    preview = await rec.clear_tasks(dry_run=True)
    assert preview.deleted_total == 2               # failed + cancelled
    assert preview.protected.get("succeeded") == 1  # needs --force
    assert preview.blocked.get("running") == 1      # never bulk-deleted
    async with db.session() as s:                   # dry run deleted nothing
        assert await s.get(TaskORM, "tk_fail") is not None

    done = await rec.clear_tasks()
    assert done.deleted_total == 2
    async with db.session() as s:
        assert await s.get(TaskORM, "tk_fail") is None
        assert await s.get(SubtaskORM, "sub-tk_fail") is None  # cascade
        assert await s.get(TaskORM, "tk_ok") is not None
        assert await s.get(TaskORM, "tk_run") is not None


@pytest.mark.asyncio
async def test_clear_tasks_force_takes_protected_but_never_running():
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_ok", status="succeeded")
    await _seed_task_tree(db, "tk_run", status="running")
    done = await rec.clear_tasks(force=True)
    assert done.deleted_total == 1
    async with db.session() as s:
        assert await s.get(TaskORM, "tk_ok") is None
        assert await s.get(TaskORM, "tk_run") is not None


@pytest.mark.parametrize(
    ("descendant_status", "force", "outcome_bucket"),
    [
        ("running", False, "blocked"),
        ("recovering", True, "blocked"),
        ("succeeded", False, "protected"),
        ("degraded", False, "protected"),
        ("needs_input", False, "protected"),
        ("awaiting_approval", False, "protected"),
    ],
)
@pytest.mark.asyncio
async def test_clear_tasks_fail_closed_when_descendant_is_not_deletable(
    descendant_status: str,
    force: bool,
    outcome_bucket: str,
):
    """A selectable parent must not bypass protection on an unselected child."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_parent", status="failed")
    await _seed_child_task(
        db,
        "tk_child",
        parent_task_id="tk_parent",
        status=descendant_status,
    )

    outcome = await rec.clear_tasks(force=force)

    assert outcome.deleted_total == 0
    assert getattr(outcome, outcome_bucket).get(descendant_status) == 1
    async with db.session() as s:
        assert await s.get(TaskORM, "tk_parent") is not None
        assert await s.get(TaskORM, "tk_child") is not None


@pytest.mark.asyncio
async def test_clear_tasks_does_not_restart_below_a_protected_intermediate_descendant():
    """A lower matching row cannot bypass a barrier inside its selected ancestor tree."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_root", status="failed")
    await _seed_child_task(
        db,
        "tk_middle",
        parent_task_id="tk_root",
        status="succeeded",
    )
    await _seed_child_task(
        db,
        "tk_leaf",
        parent_task_id="tk_middle",
        status="failed",
    )

    outcome = await rec.clear_tasks(kind=None)

    assert outcome.deleted_total == 0
    assert outcome.protected.get("succeeded") == 1
    async with db.session() as s:
        for task_id in ("tk_root", "tk_middle", "tk_leaf"):
            assert await s.get(TaskORM, task_id) is not None


@pytest.mark.parametrize(
    ("descendant_status", "force", "outcome_bucket"),
    [
        ("running", False, "blocked"),
        ("recovering", True, "blocked"),
        ("succeeded", False, "protected"),
        ("degraded", False, "protected"),
        ("needs_input", False, "protected"),
        ("awaiting_approval", False, "protected"),
    ],
)
@pytest.mark.asyncio
async def test_delete_tasks_fail_closed_when_descendant_is_not_deletable(
    descendant_status: str,
    force: bool,
    outcome_bucket: str,
):
    """Explicit deletion applies active/protected policy to the full closure."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_parent", status="failed")
    await _seed_child_task(
        db,
        "tk_child",
        parent_task_id="tk_parent",
        status=descendant_status,
    )

    outcome = await rec.delete_tasks(["tk_parent"], force=force)

    assert outcome.deleted_total == 0
    assert getattr(outcome, outcome_bucket).get(descendant_status) == 1
    async with db.session() as s:
        assert await s.get(TaskORM, "tk_parent") is not None
        assert await s.get(TaskORM, "tk_child") is not None


@pytest.mark.asyncio
async def test_delete_tasks_is_atomic_across_multiple_roots():
    """One protected tree prevents deletion of every root in the request batch."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_blocked_root", status="failed")
    await _seed_child_task(
        db,
        "tk_protected_child",
        parent_task_id="tk_blocked_root",
        status="succeeded",
    )
    await _seed_task_tree(db, "tk_other_root", status="failed")

    outcome = await rec.delete_tasks(["tk_blocked_root", "tk_other_root"])

    assert outcome.deleted_total == 0
    assert outcome.protected.get("succeeded") == 1
    async with db.session() as s:
        for task_id in ("tk_blocked_root", "tk_protected_child", "tk_other_root"):
            assert await s.get(TaskORM, task_id) is not None


@pytest.mark.asyncio
async def test_delete_tasks_deduplicates_overlapping_root_closures():
    """Selecting a root and its child counts/deletes each task exactly once."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_root", status="failed")
    await _seed_child_task(
        db,
        "tk_child",
        parent_task_id="tk_root",
        status="cancelled",
    )
    await _seed_task_tree(db, "tk_other_root", status="interrupted")
    selected = ["tk_root", "tk_child", "tk_other_root", "tk_root"]

    preview = await rec.delete_tasks(selected, dry_run=True)
    assert preview.deleted_total == 3
    async with db.session() as s:
        for task_id in ("tk_root", "tk_child", "tk_other_root"):
            assert await s.get(TaskORM, task_id) is not None

    outcome = await rec.delete_tasks(selected)
    assert outcome.deleted_total == 3
    async with db.session() as s:
        for task_id in ("tk_root", "tk_child", "tk_other_root"):
            assert await s.get(TaskORM, task_id) is None


@pytest.mark.parametrize("object_kind", ["workflow_run", "workflow_step", "subtask"])
@pytest.mark.asyncio
async def test_delete_tasks_blocks_terminal_task_with_active_execution(object_kind: str):
    """A stale terminal projection cannot authorize deleting live execution work."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_terminal", status="failed")
    await _seed_execution(db, "tk_terminal", object_kind=object_kind)

    outcome = await rec.delete_tasks(["tk_terminal"])

    assert outcome.deleted_total == 0
    assert outcome.blocked.get("running") == 1
    assert len(outcome.blocked_executions) == 1
    blocker = outcome.blocked_executions[0]
    assert blocker.task_id == "tk_terminal"
    assert blocker.object_kind == object_kind.replace("subtask", "skill_execution")
    assert blocker.status == "running"
    assert await rec.get_task("tk_terminal") is not None


@pytest.mark.asyncio
async def test_delete_rechecks_state_after_waiting_for_a_concurrent_writer():
    """The deletion-side write reservation closes the status-check TOCTOU gap."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_race", status="failed")

    async with db.session() as writer:
        await writer.execute(text("BEGIN IMMEDIATE"))
        row = await writer.get(TaskORM, "tk_race")
        row.status = "running"
        await writer.flush()
        deletion = asyncio.create_task(rec.delete_tasks(["tk_race"]))
        await asyncio.sleep(0.05)
        assert not deletion.done()
        await writer.commit()

    outcome = await deletion
    assert outcome.deleted_total == 0
    assert outcome.blocked.get("running") == 1
    assert await rec.get_task("tk_race") is not None


@pytest.mark.asyncio
async def test_delete_corrupt_parent_cycle_fails_closed():
    """Malformed ancestry cannot report success while leaving Task rows behind."""
    rec, db = await _recorder()
    async with db.session() as session:
        session.add(
            TaskORM(
                id="tk_cycle",
                parent_task_id="tk_cycle",
                status="failed",
                title="cycle",
                kind="turn",
            )
        )
        await session.commit()

    outcome = await rec.delete_tasks(["tk_cycle"])

    assert outcome.deleted_total == 0
    assert outcome.retained.get("invalid_task_tree") == 1
    assert await rec.get_task("tk_cycle") is not None


@pytest.mark.parametrize("object_kind", ["workflow_run", "workflow_step", "subtask"])
@pytest.mark.asyncio
async def test_clear_tasks_blocks_terminal_task_with_active_execution(object_kind: str):
    """Filter-based cleanup observes leases below the selected Task row."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_terminal", status="failed")
    await _seed_execution(db, "tk_terminal", object_kind=object_kind)

    outcome = await rec.clear_tasks()

    assert outcome.deleted_total == 0
    assert outcome.blocked.get("running") == 1
    assert await rec.get_task("tk_terminal") is not None


@pytest.mark.parametrize(
    ("ancestor_status", "archived", "bucket", "reason"),
    [
        ("running", False, "blocked", "running"),
        ("recovering", False, "blocked", "recovering"),
        ("succeeded", False, "protected", "succeeded"),
        ("degraded", False, "protected", "degraded"),
        ("failed", True, "retained", "archived"),
    ],
)
@pytest.mark.asyncio
async def test_prunable_clear_cannot_delete_failed_child_below_retained_ancestor(
    ancestor_status: str,
    archived: bool,
    bucket: str,
    reason: str,
):
    """Cleanup never severs a child from an ancestor outside its deletion set."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_ancestor", status=ancestor_status)
    await _seed_child_task(
        db,
        "tk_failed_child",
        parent_task_id="tk_ancestor",
        status="failed",
    )
    if archived:
        async with db.session() as s:
            ancestor = await s.get(TaskORM, "tk_ancestor")
            ancestor.archived_at = datetime.now(UTC)
            await s.commit()

    outcome = await rec.clear_tasks(kind=None, prunable_only=True)

    assert outcome.deleted_total == 0
    assert getattr(outcome, bucket).get(reason) == 1
    async with db.session() as s:
        assert await s.get(TaskORM, "tk_ancestor") is not None
        assert await s.get(TaskORM, "tk_failed_child") is not None


@pytest.mark.asyncio
async def test_archived_descendant_is_a_barrier_unless_explicitly_included():
    """A root cannot implicitly delete hidden history below it."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_root", status="failed")
    await _seed_child_task(
        db,
        "tk_archived_child",
        parent_task_id="tk_root",
        status="failed",
    )
    async with db.session() as s:
        child = await s.get(TaskORM, "tk_archived_child")
        child.archived_at = datetime.now(UTC)
        await s.commit()

    retained = await rec.clear_tasks(include_archived=False)
    assert retained.deleted_total == 0
    assert retained.retained.get("archived") == 1
    async with db.session() as s:
        assert await s.get(TaskORM, "tk_root") is not None
        assert await s.get(TaskORM, "tk_archived_child") is not None

    deleted = await rec.clear_tasks(include_archived=True)
    assert deleted.deleted_total == 2
    async with db.session() as s:
        assert await s.get(TaskORM, "tk_root") is None
        assert await s.get(TaskORM, "tk_archived_child") is None


@pytest.mark.asyncio
async def test_recent_descendant_blocks_deleting_old_root_by_cutoff():
    """A cutoff applies to every node that the root's FK cascade would remove."""
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_old_root", status="failed")
    await _seed_child_task(
        db,
        "tk_recent_child",
        parent_task_id="tk_old_root",
        status="failed",
    )
    old = datetime.now(UTC) - timedelta(days=40)
    cutoff = datetime.now(UTC) - timedelta(days=30)
    async with db.session() as s:
        root = await s.get(TaskORM, "tk_old_root")
        root.created_at = old
        root.finished_at = old
        await s.commit()

    outcome = await rec.clear_tasks(before=cutoff)

    assert outcome.deleted_total == 0
    assert outcome.retained.get("newer_than_cutoff") == 1
    async with db.session() as s:
        assert await s.get(TaskORM, "tk_old_root") is not None
        assert await s.get(TaskORM, "tk_recent_child") is not None


@pytest.mark.asyncio
async def test_task_archive_roundtrip_and_artifact_detection():
    rec, db = await _recorder()
    await _seed_task_tree(db, "tk_arch", status="succeeded")
    async with db.session() as s:
        s.add(ArtifactORM(uri="artifact://task-level", kind="figure", subtask_id="sub-tk_arch"))
        await s.commit()

    assert await rec.archive_task("tk_arch", reason="done reviewing") is True
    archived = await rec.get_task("tk_arch")
    assert archived is not None and archived.archived_at is not None
    assert archived.archived_reason == "done reviewing"
    default_rows = {t.id for t in await rec.list_tasks(limit=50)}
    assert "tk_arch" not in default_rows
    assert await rec.unarchive_task("tk_arch") is True
    assert await rec.task_has_artifacts("tk_arch") is True


# ---------------------------------------------------------------------------
# P4 forward hygiene: dead running tasks settle as `interrupted` at startup /
# housekeeping, and retention deletes old prunable history automatically.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_stale_tasks_interrupts_only_silent_active_tasks():
    rec, db = await _recorder()
    old = datetime.now(UTC) - timedelta(hours=3)
    async with db.session() as s:
        # silent for hours → interrupted
        s.add(TaskORM(id="tk_dead", status="running", title="dead", created_at=old, started_at=old))
        # active but with a *recent* event → alive, untouched
        s.add(TaskORM(id="tk_live", status="running", title="live", created_at=old, started_at=old))
        # paused on a human, not a worker → untouched even though old
        s.add(TaskORM(id="tk_wait", status="needs_input", title="waiting", created_at=old, started_at=old))
        await s.flush()
        s.add(TaskEventORM(id="evt_live", task_id="tk_live", seq=1, event_type="tool.call.start"))
        await s.commit()

    interrupted = await rec.reconcile_stale_tasks(stale_after_s=1800)
    assert interrupted == ["tk_dead"]

    dead = await rec.get_task("tk_dead")
    assert dead.status == "interrupted"
    assert "no activity" in dead.error
    assert dead.finished_at is not None
    events = await rec.list_events("tk_dead")
    assert any(e.event_type == "task.interrupted" for e in events)

    assert (await rec.get_task("tk_live")).status == "running"
    assert (await rec.get_task("tk_wait")).status == "needs_input"

    # 0 disables reconciliation entirely
    assert await rec.reconcile_stale_tasks(stale_after_s=0) == []


@pytest.mark.asyncio
async def test_housekeep_applies_interrupt_and_retention_policies():
    rt, db = await _runtime()
    s = load_settings()
    rec = TaskRecorder(db, project=s.paths.project_name)
    rt.set_task_recorder(rec)

    old = datetime.now(UTC) - timedelta(days=40)
    async with db.session() as s2:
        s2.add(TaskORM(id="tk_stuck", status="running", title="stuck", created_at=old, started_at=old))
        s2.add(TaskORM(id="tk_oldfail", status="failed", title="old failure", created_at=old))
        s2.add(TaskORM(id="tk_oldok", status="succeeded", title="old success", created_at=old))
        s2.add(TaskORM(id="tk_newfail", status="failed", title="recent failure"))
        await s2.flush()
        s2.add(SubtaskORM(id="sub-tk_oldfail", task_id="tk_oldfail", skill_name="x", status="failed"))
        await s2.commit()

    rt._settings.tasks.interrupt_stale_after_s = 1800
    rt._settings.tasks.retention_days = 30
    out = await rt.housekeep()
    assert out["interrupted"] == 1
    # old failed deleted by retention; the freshly-interrupted task is newer
    # than the cutoff and stays; succeeded provenance is never auto-deleted.
    assert out["retention_deleted"] == 1
    async with db.session() as s3:
        assert await s3.get(TaskORM, "tk_oldfail") is None
        assert await s3.get(SubtaskORM, "sub-tk_oldfail") is None  # cascade
        assert (await s3.get(TaskORM, "tk_stuck")).status == "interrupted"
        assert await s3.get(TaskORM, "tk_oldok") is not None
        assert await s3.get(TaskORM, "tk_newfail") is not None


@pytest.mark.asyncio
async def test_housekeep_disabled_by_default_retention():
    rt, db = await _runtime()
    s = load_settings()
    rec = TaskRecorder(db, project=s.paths.project_name)
    rt.set_task_recorder(rec)
    old = datetime.now(UTC) - timedelta(days=400)
    async with db.session() as s2:
        s2.add(TaskORM(id="tk_ancient", status="failed", title="ancient", created_at=old))
        await s2.commit()

    rt._settings.tasks.interrupt_stale_after_s = 0  # reconcile off
    rt._settings.tasks.retention_days = 0           # retention off
    out = await rt.housekeep()
    assert out == {"interrupted": 0, "retention_deleted": 0, "orphans": 0}
    async with db.session() as s3:
        assert await s3.get(TaskORM, "tk_ancient") is not None
