"""Catalog + TaskIndex-first routing closes the IM-anchor visibility gap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omni.config import load_settings
from omni.config.paths import get_paths, user_home
from omni.config.workspaces import list_workspaces, registry_path
from omni.runtime.aggregate import (
    list_schedules_all_workspaces,
    list_tasks_all_workspaces,
    resolve_schedule_workspace,
)
from omni.runtime.home_service import HomeService
from omni.runtime.task_index import TaskIndex
from omni.runtime.task_object_resolver import resolve_task_object
from omni.storage.db import get_database
from omni.storage.models import ScheduleORM, TaskORM


async def _seed_named_task(name: str, task_id: str, *, title: str, channel: str = "wechat"):
    settings = load_settings(project=name)
    assert settings.paths is not None
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    async with db.session() as session:
        session.add(
            TaskORM(
                id=task_id,
                status="succeeded",
                title=title,
                kind="turn",
                channel=channel,
                project=name,
            )
        )
        await session.commit()
        task = await session.get(TaskORM, task_id)
        assert task is not None
        # Dual-write while attributes are still loaded on the session.
        await TaskIndex.for_workspace(settings.paths).record(task)
    return settings


async def _seed_named_schedule(name: str, schedule_id: str, *, title: str):
    settings = load_settings(project=name)
    assert settings.paths is not None
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    async with db.session() as session:
        session.add(
            ScheduleORM(
                id=schedule_id,
                project=name,
                channel="wechat",
                title=title,
                skill_name="agent-goal",
                input_json={"input": title},
                kind="once",
                enabled=False,
            )
        )
        await session.commit()
    return settings


@pytest.mark.asyncio
async def test_task_all_sees_unregistered_im_anchor(omni_home, tmp_path):

    task_id = "784058a9000000000000000000000001"
    await _seed_named_task("default", task_id, title="wechat deliverable")

    # Registry empty — previously /task all only showed path workspaces.
    assert list_workspaces(omni_home) == []
    assert not registry_path(omni_home).exists()

    rows = await list_tasks_all_workspaces(home=omni_home, limit_per=50)
    assert any(r.id == task_id and r.workspace == "default" for r in rows)


@pytest.mark.asyncio
async def test_schedule_all_and_show_route_unregistered_anchor(omni_home, tmp_path):

    schedule_id = "d106b237000000000000000000000001"
    await _seed_named_schedule("default", schedule_id, title="RAG 系统综述材料准备")
    assert list_workspaces(omni_home) == []

    rows = await list_schedules_all_workspaces(home=omni_home)
    assert any(r.id == schedule_id and r.workspace == "default" for r in rows)

    # From a path-keyed CLI cwd, show must still resolve to the anchor.
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    local = load_settings(cwd=repo)
    local.paths.ensure_dirs()
    remote = await resolve_schedule_workspace(local, schedule_id[:8])
    assert remote is not None
    assert remote.paths.project_name == "default"


@pytest.mark.asyncio
async def test_resolve_task_object_index_first_without_registry(omni_home, tmp_path):

    task_id = "784058a9000000000000000000000002"
    await _seed_named_task("default", task_id, title="wechat task")
    assert list_workspaces(omni_home) == []

    # Caller is a different path workspace (repo CLI).
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    local = load_settings(cwd=repo)
    local.paths.ensure_dirs()

    resolution = await resolve_task_object(local, task_id[:8])
    assert resolution.status == "ok"
    assert resolution.object_kind == "task"
    assert resolution.object_id == task_id
    assert resolution.settings is not None
    assert resolution.settings.paths.project_name == "default"


@pytest.mark.asyncio
async def test_home_service_registers_hosted_workspaces(omni_home, tmp_path):

    # Ensure the anchor project dir exists before the service hosts it.
    get_paths(project="default").ensure_dirs()

    service = HomeService(
        load_settings(project="default"),
        workers=1,
        enable_channels=False,
        reconcile_interval_s=999.0,
    )
    await service.start()
    try:
        names = {r["name"] for r in list_workspaces(user_home())}
        assert "default" in names
        # Registry file should mention the anchor project_dir.
        data = json.loads(registry_path(user_home()).read_text(encoding="utf-8"))
        assert any(Path(key).parts[-2:] == ("projects", "default") for key in data)
    finally:
        await service.stop()
