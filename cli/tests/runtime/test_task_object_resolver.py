"""Typed, cross-workspace resolution for every inspectable task object."""

from __future__ import annotations

import pytest

from omni.cli import state as state_mod
from omni.cli.state import AppState, make_agent_for_object
from omni.config import load_settings
from omni.config.workspaces import register_workspace
from omni.runtime.task_object_resolver import resolve_task_object
from omni.storage.db import get_database
from omni.storage.models import (
    SubtaskORM,
    TaskORM,
    WorkflowRunORM,
    WorkflowStepORM,
)


async def _workspace(name: str):
    settings = load_settings(project=name)
    assert settings.paths is not None
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    register_workspace(settings.paths)
    return settings, db


async def _seed_hierarchy(
    name: str,
    *,
    task_id: str,
    workflow_id: str,
    step_id: str,
    execution_id: str,
):
    settings, db = await _workspace(name)
    async with db.session() as session:
        session.add(
            TaskORM(
                id=task_id,
                status="succeeded",
                title=f"{name} task",
                kind="turn",
            )
        )
        await session.flush()
        session.add(
            WorkflowRunORM(
                id=workflow_id,
                task_id=task_id,
                status="succeeded",
                goal=f"{name} workflow",
            )
        )
        await session.flush()
        session.add(
            WorkflowStepORM(
                id=step_id,
                workflow_run_id=workflow_id,
                task_id=task_id,
                step_key=f"{name}-step",
                status="succeeded",
            )
        )
        await session.flush()
        session.add(
            SubtaskORM(
                id=execution_id,
                task_id=task_id,
                workflow_run_id=workflow_id,
                workflow_step_id=step_id,
                skill_name="research-ideation",
                status="succeeded",
            )
        )
        await session.commit()
    return settings, db


@pytest.mark.asyncio
async def test_resolves_all_object_kinds_to_their_canonical_task_and_workspace():
    alpha, _db = await _seed_hierarchy(
        "alpha",
        task_id="10000000000000000000000000000001",
        workflow_id="20000000000000000000000000000002",
        step_id="30000000000000000000000000000003",
        execution_id="40000000000000000000000000000004",
    )
    beta, _beta_db = await _workspace("beta")

    expected = {
        "10000000": ("task", "10000000000000000000000000000001"),
        "20000000": ("workflow_run", "20000000000000000000000000000002"),
        "30000000": ("workflow_step", "30000000000000000000000000000003"),
        "40000000": ("skill_execution", "40000000000000000000000000000004"),
    }
    for ident, (kind, object_id) in expected.items():
        resolved = await resolve_task_object(beta, ident)
        assert resolved.status == "ok"
        assert resolved.object_kind == kind
        assert resolved.object_id == object_id
        assert resolved.task_id == "10000000000000000000000000000001"
        assert resolved.settings is not None
        assert resolved.settings.paths is not None
        assert resolved.settings.paths.project_dir == alpha.paths.project_dir


@pytest.mark.asyncio
async def test_exact_match_wins_over_prefix_matches_across_kinds_and_workspaces():
    alpha, alpha_db = await _workspace("alpha")
    async with alpha_db.session() as session:
        session.add(
            TaskORM(id="deadbeef", status="succeeded", title="exact", kind="turn")
        )
        await session.commit()
    await _seed_hierarchy(
        "beta",
        task_id="a0000000000000000000000000000001",
        workflow_id="deadbeef000000000000000000000002",
        step_id="b0000000000000000000000000000003",
        execution_id="c0000000000000000000000000000004",
    )

    resolved = await resolve_task_object(alpha, "deadbeef")

    assert resolved.status == "ok"
    assert resolved.object_kind == "task"
    assert resolved.object_id == "deadbeef"
    assert resolved.task_id == "deadbeef"


@pytest.mark.asyncio
async def test_exact_id_collision_across_object_kinds_is_ambiguous():
    shared_id = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    alpha, alpha_db = await _workspace("alpha")
    async with alpha_db.session() as session:
        session.add(
            TaskORM(id=shared_id, status="succeeded", title="task", kind="turn")
        )
        await session.commit()
    await _seed_hierarchy(
        "beta",
        task_id="ffffffffffffffffffffffffffffffff",
        workflow_id=shared_id,
        step_id="abababababababababababababababab",
        execution_id="cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd",
    )

    resolved = await resolve_task_object(alpha, shared_id)

    assert resolved.status == "ambiguous"
    assert resolved.object_kind is None
    assert resolved.object_id == ""
    assert resolved.task_id == ""
    assert resolved.settings is None


@pytest.mark.asyncio
async def test_prefix_collision_across_workspaces_and_kinds_is_ambiguous():
    alpha, alpha_db = await _workspace("alpha")
    async with alpha_db.session() as session:
        session.add(
            TaskORM(
                id="abcd1234000000000000000000000001",
                status="succeeded",
                title="task",
                kind="turn",
            )
        )
        await session.commit()
    await _seed_hierarchy(
        "beta",
        task_id="f000000000000000000000000000001",
        workflow_id="f100000000000000000000000000002",
        step_id="f200000000000000000000000000003",
        execution_id="abcd1234000000000000000000000004",
    )

    resolved = await resolve_task_object(alpha, "abcd1234")

    assert resolved.status == "ambiguous"
    assert resolved.settings is None


@pytest.mark.asyncio
async def test_unknown_and_empty_ids_are_not_found():
    alpha, _db = await _workspace("alpha")

    for ident in ("", "   ", "does-not-exist"):
        resolved = await resolve_task_object(alpha, ident)
        assert resolved.status == "not_found"
        assert resolved.object_kind is None
        assert resolved.settings is None


@pytest.mark.asyncio
async def test_prefix_resolution_does_not_depend_on_recent_row_limits():
    alpha, db = await _workspace("alpha")
    task_id = "11111111111111111111111111111111"
    target = "target0000000000000000000000000001"
    async with db.session() as session:
        session.add(
            TaskORM(id=task_id, status="succeeded", title="many runs", kind="turn")
        )
        session.add(
            WorkflowRunORM(
                id=target,
                task_id=task_id,
                status="succeeded",
                goal="old target",
            )
        )
        session.add_all(
            [
                WorkflowRunORM(
                    id=f"filler{i:026d}",
                    task_id=task_id,
                    status="succeeded",
                    goal="filler",
                )
                for i in range(1001)
            ]
        )
        await session.commit()

    resolved = await resolve_task_object(alpha, "target")

    assert resolved.status == "ok"
    assert resolved.object_kind == "workflow_run"
    assert resolved.object_id == target
    assert resolved.task_id == task_id


@pytest.mark.asyncio
async def test_make_agent_for_object_preserves_ambiguity_and_routes_remote(
    monkeypatch,
):
    alpha, alpha_db = await _workspace("alpha")
    beta, _beta_db = await _workspace("beta")
    remote_id = "99999999999999999999999999999999"
    async with alpha_db.session() as session:
        session.add(
            TaskORM(id=remote_id, status="succeeded", title="remote", kind="turn")
        )
        session.add(
            WorkflowRunORM(
                id="aaaa0000000000000000000000000001",
                task_id=remote_id,
                status="succeeded",
            )
        )
        session.add(
            WorkflowStepORM(
                id="aaaa0000000000000000000000000002",
                workflow_run_id="aaaa0000000000000000000000000001",
                task_id=remote_id,
                step_key="step",
            )
        )
        await session.commit()
    async with _beta_db.session() as session:
        session.add(
            TaskORM(
                id="aaaa0000000000000000000000000003",
                status="succeeded",
                title="collision",
                kind="turn",
            )
        )
        await session.commit()

    class FakeAgent:
        def __init__(self, settings):
            self.settings = settings

    async def fake_make_agent(settings, **_kwargs):
        return FakeAgent(settings)

    monkeypatch.setattr(state_mod, "resolve_workspace_trust", lambda _state: True)
    monkeypatch.setattr(state_mod, "make_agent_from_settings", fake_make_agent)
    state = AppState(project="beta", trusted=True)

    agent, resolution, remote = await make_agent_for_object(state, remote_id[:8])
    assert resolution.status == "ok"
    assert remote is True
    assert agent.settings.paths.project_dir == alpha.paths.project_dir

    local_agent, ambiguous, remote = await make_agent_for_object(state, "aaaa")
    assert ambiguous.status == "ambiguous"
    assert ambiguous.settings is None
    assert remote is False
    assert local_agent.settings.paths.project_dir == beta.paths.project_dir
