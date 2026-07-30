"""Produced artifacts stay separate from artifacts referenced as turn context."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.cli.commands.tasks_cmd import _resolve_task_artifacts, _task_json_payload
from omni.config import load_settings
from omni.runtime.task_recorder import TaskRecorder
from omni.runtime.verification import _verification_artifact_ids
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import TaskEventORM, TaskORM


@pytest.mark.asyncio
async def test_context_assembled_keeps_artifact_reference_without_promoting_output():
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    task_id = "4497f10e7aab1234567890abcdefabcd"
    old_artifact_id = "27a6c3fc634143b2a7a86bcf9197c10e"
    async with db.session() as session:
        session.add(TaskORM(id=task_id, status="running", kind="turn"))
        await session.commit()

    recorder = TaskRecorder(db, project=settings.paths.project_name)
    await recorder.append_event(
        task_id,
        event_type="context.assembled",
        status="succeeded",
        output_json={
            "active_target": {"artifact_uri": f"artifact://{old_artifact_id}"},
            "recent_artifacts": [{"uri": f"artifact://{old_artifact_id}"}],
        },
    )

    task = await recorder.get_task(task_id)
    events = await recorder.list_events(task_id)
    assert task is not None
    assert task.artifact_ids == []
    assert events[-1].output_json["active_target"]["artifact_uri"] == (
        f"artifact://{old_artifact_id}"
    )


@pytest.mark.asyncio
async def test_task_artifact_display_rejects_a_proven_foreign_cached_artifact():
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    owner_id = "abe000184a221234567890abcdefabcd"
    polluted_id = "4497f10e7aab1234567890abcdefabcd"
    async with db.session() as session:
        session.add_all(
            [
                TaskORM(id=owner_id, status="succeeded", kind="turn"),
                TaskORM(id=polluted_id, status="succeeded", kind="turn"),
            ]
        )
        await session.commit()

    artifact = await ArtifactStore(settings.paths, db).put_bytes(
        b"owned by the old task",
        kind="report",
        title="Old report",
        ext="md",
        task_id=owner_id,
    )
    async with db.session() as session:
        polluted = await session.get(TaskORM, polluted_id)
        assert polluted is not None
        polluted.artifact_ids = [artifact.id]
        await session.commit()

    rows = await _resolve_task_artifacts(
        task_id=polluted_id,
        subtasks=[],
        steps=[],
        db=db,
        paths=settings.paths,
    )
    assert rows == []

    payload = _task_json_payload(
        polluted,
        [],
        [],
        [],
        [],
        [],
        artifact_ids=[],
    )
    assert payload["artifact_ids"] == []


@pytest.mark.asyncio
async def test_task_prefix_resolution_is_not_limited_to_the_latest_500_rows():
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    old = datetime(2020, 1, 1, tzinfo=UTC)
    target_id = "target0000000000000000000000000001"
    async with db.session() as session:
        session.add(
            TaskORM(id=target_id, status="succeeded", kind="turn", created_at=old)
        )
        session.add_all(
            TaskORM(
                id=f"newer{i:027d}",
                status="succeeded",
                kind="turn",
                created_at=old + timedelta(days=1),
            )
            for i in range(500)
        )
        await session.commit()

    recorder = TaskRecorder(db, project=settings.paths.project_name)
    resolved = await recorder.get_task("target00")
    assert resolved is not None and resolved.id == target_id
    assert await recorder.get_task(target_id[-8:]) is None


def test_verification_does_not_count_context_references_as_emitted_artifacts():
    task = TaskORM(id="task", artifact_ids=[])
    referenced = TaskEventORM(
        task_id="task",
        event_type="context.assembled",
        output_json={"active_target": {"artifact_uri": "artifact://old-artifact"}},
    )
    produced = TaskEventORM(
        task_id="task",
        event_type="subtask.done",
        output_json={"artifact_ids": ["new-artifact"]},
    )

    assert _verification_artifact_ids(task, [], [referenced]) == []
    assert _verification_artifact_ids(task, [], [referenced, produced]) == [
        "new-artifact"
    ]


@pytest.mark.asyncio
async def test_schedule_creation_task_and_scheduled_run_keep_separate_artifacts():
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    creation_task_id = "4497f10e7aab1234567890abcdefabcd"
    run_task_id = "7f8a2d49e6ab1234567890abcdefabcd"
    async with db.session() as session:
        session.add_all(
            [
                TaskORM(id=creation_task_id, status="succeeded", kind="turn"),
                TaskORM(
                    id=run_task_id,
                    status="succeeded",
                    kind="turn",
                    schedule_id="schedule-1",
                ),
            ]
        )
        await session.commit()

    artifact = await ArtifactStore(settings.paths, db).put_bytes(
        b"# scheduled output",
        kind="report",
        title="Scheduled report",
        ext="md",
        task_id=run_task_id,
    )
    creation_rows = await _resolve_task_artifacts(
        task_id=creation_task_id,
        subtasks=[],
        steps=[],
        db=db,
        paths=settings.paths,
    )
    run_rows = await _resolve_task_artifacts(
        task_id=run_task_id,
        subtasks=[],
        steps=[],
        db=db,
        paths=settings.paths,
    )

    assert creation_rows == []
    assert [uri for _title, _path, uri in run_rows] == [artifact.uri]
    assert f"-{run_task_id[:8]}-{artifact.id[:8]}.md" in artifact.path.name
