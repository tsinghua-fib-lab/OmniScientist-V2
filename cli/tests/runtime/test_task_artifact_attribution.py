"""Produced artifacts stay separate from artifacts referenced as turn context."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omni.cli.commands.tasks_cmd import _resolve_task_artifacts, _task_json_payload
from omni.config import load_settings
from omni.runtime.task_recorder import TaskRecorder
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM, TaskORM


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
async def test_a_result_that_names_only_a_uri_still_shows_the_file_it_wrote():
    """`/task show` prints a path its reader can open, not a store identifier.

    research-ideation reports its report as ``report_uri`` and nothing else, so
    the entry taken from the result had no path — and, holding the key the
    stored row would have been pushed under, it kept the one side that knew the
    file from ever being listed. Task cbffcbb6 showed
    ``Report artifact://738774…`` for a registered file sitting on disk.
    """
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    task_id = "cbffcbb6a1b21234567890abcdefabcd"
    subtask_id = "e80a6b0d99cc1234567890abcdefabcd"
    async with db.session() as session:
        session.add(TaskORM(id=task_id, status="succeeded", kind="turn"))
        await session.commit()

    artifact = await ArtifactStore(settings.paths, db).put_bytes(
        b"# Research Ideation Report\n",
        kind="report",
        title="Research ideation",
        ext="md",
        task_id=task_id,
    )
    subtask = SubtaskORM(
        id=subtask_id,
        task_id=task_id,
        skill_name="research-ideation",
        status="succeeded",
        result_json={"report_uri": artifact.uri},
    )

    rows = await _resolve_task_artifacts(
        task_id=task_id,
        subtasks=[subtask],
        steps=[],
        db=db,
        paths=settings.paths,
    )

    assert [uri for _, _, uri in rows] == [artifact.uri]
    (_, path, _), *rest = rows
    assert not rest
    assert Path(path).read_bytes() == b"# Research Ideation Report\n"


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
