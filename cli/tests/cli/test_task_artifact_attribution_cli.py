"""`omni task show` exposes produced artifacts, never contextual references."""

from __future__ import annotations

import asyncio
import json

from typer.testing import CliRunner

from omni.cli.main import app
from omni.config import load_settings
from omni.storage.db import get_database
from omni.storage.models import ArtifactORM, TaskORM

runner = CliRunner()


def test_task_show_human_and_json_filter_a_foreign_cached_artifact():
    project = "task-artifact-attribution"
    owner_id = "abe000184a221234567890abcdefabcd"
    polluted_id = "4497f10e7aab1234567890abcdefabcd"
    artifact_id = "27a6c3fc634143b2a7a86bcf9197c10e"

    async def seed() -> None:
        settings = load_settings(project=project)
        settings.paths.ensure_dirs()
        db = get_database(settings.paths.project_db)
        await db.init()
        async with db.session() as session:
            session.add_all(
                [
                    TaskORM(
                        id=owner_id,
                        project=project,
                        status="succeeded",
                        kind="turn",
                        title="actual producer",
                        artifact_ids=[artifact_id],
                    ),
                    TaskORM(
                        id=polluted_id,
                        project=project,
                        status="succeeded",
                        kind="turn",
                        title="schedule creation",
                        artifact_ids=[artifact_id],
                    ),
                ]
            )
            await session.flush()
            session.add(
                ArtifactORM(
                    id=artifact_id,
                    task_id=owner_id,
                    uri=f"artifact://{artifact_id}",
                    title="foreign report",
                    kind="report",
                    rel_path="reports/foreign.md",
                )
            )
            await session.commit()

    asyncio.run(seed())

    human = runner.invoke(
        app,
        ["--project", project, "task", "show", polluted_id[:8]],
    )
    assert human.exit_code == 0, human.output
    assert "foreign report" not in human.stdout
    assert f"artifact://{artifact_id}" not in human.stdout

    raw = runner.invoke(
        app,
        ["--project", project, "task", "show", polluted_id[:8], "--json"],
    )
    assert raw.exit_code == 0, raw.output
    payload = json.loads(raw.stdout)
    assert payload["task_id"] == polluted_id
    assert payload["artifact_ids"] == []
