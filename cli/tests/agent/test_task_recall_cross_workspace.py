"""Cross-workspace recall (Layer 1).

An agent running in workspace A can still read, list, and search tasks that live
in workspace B, by routing through the global task index — the same machinery
``omni task show <id>`` / ``omni task --all`` already use. Before this, recall
tools were hard-scoped to ``ctx.db`` + ``ctx.project`` and dead-ended on
"not found in this workspace" for anything created elsewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.config.workspaces import register_workspace
from omni.runtime import task_index as task_index_mod
from omni.runtime.task_index import TaskIndex
from omni.skills_runtime.builtin_tools.recall import build_recall_tools
from omni.storage.models import TaskORM


@pytest.fixture(autouse=True)
def _reset_reconcile_guard():
    """The one-shot reconcile guard is process-global; clear it per test."""
    task_index_mod._reconciled.clear()
    yield
    task_index_mod._reconciled.clear()


async def _agent(project: str) -> OmniAgent:
    agent = await OmniAgent.create(load_settings(project=project))
    register_workspace(agent.paths)
    return agent


async def _seed_task(
    agent: OmniAgent,
    *,
    user_input: str,
    title: str,
    summary: str = "",
    status: str = "succeeded",
    created_at: datetime | None = None,
) -> TaskORM:
    run = await agent.tasks.create_task(
        session_id="seed", channel="cli", user_input=user_input
    )
    async with agent.db.session() as session:
        stored = await session.get(TaskORM, run.id)
        assert stored is not None
        stored.title = title
        stored.summary = summary
        stored.status = status
        if created_at is not None:
            stored.created_at = created_at
        await session.commit()
        refreshed = await session.get(TaskORM, run.id)
    # Dual-write the final state into the global index so cross-workspace routing
    # resolves it directly (matching steady-state TaskRecorder behaviour).
    await TaskIndex.for_workspace(agent.paths).record(refreshed)
    return refreshed


def _recall_tools(agent: OmniAgent):
    session_id = agent.paths.project_name + "-sess"
    ctx = agent._make_ctx(session_id, "cli", None, task_id="current", principal="local")
    return {tool.spec.name: tool for tool in build_recall_tools(ctx)}


@pytest.mark.asyncio
async def test_get_task_routes_to_owning_workspace_when_not_local() -> None:
    agent_a = await _agent("alpha")
    agent_b = await _agent("beta")
    try:
        run = await _seed_task(
            agent_b,
            user_input="生成 Beta 项目综述",
            title="Beta review",
            summary="beta done",
        )
        tools = _recall_tools(agent_a)
        detail = await tools["get_task"].handler({"task_id": f"task:{run.id}"})
    finally:
        await agent_a.aclose()
        await agent_b.aclose()

    assert detail.get("task_id") == run.id
    assert detail["status"] == "succeeded"
    # Layer 2 attribution: the model can name the owning workspace.
    assert detail["workspace"] == "beta"
    assert "not found" not in str(detail.get("error", ""))


@pytest.mark.asyncio
async def test_get_task_local_first_is_unchanged() -> None:
    agent_a = await _agent("alpha")
    try:
        run = await _seed_task(
            agent_a, user_input="alpha task", title="Alpha", summary="alpha done"
        )
        tools = _recall_tools(agent_a)
        detail = await tools["get_task"].handler({"task_id": f"task:{run.id}"})
    finally:
        await agent_a.aclose()

    assert detail["task_id"] == run.id
    assert detail["status"] == "succeeded"


@pytest.mark.asyncio
async def test_local_recall_attributes_its_own_workspace() -> None:
    # Layer 2: attribution is present on the local path too, so a mixed
    # (workspace + all) answer names every task's owning project consistently.
    agent_a = await _agent("alpha")
    try:
        run = await _seed_task(
            agent_a, user_input="alpha task", title="Alpha", summary="done"
        )
        tools = _recall_tools(agent_a)
        detail = await tools["get_task"].handler({"task_id": f"task:{run.id}"})
        listed = await tools["list_recent_tasks"].handler({})
        found = await tools["search_tasks"].handler({"query": "Alpha"})
    finally:
        await agent_a.aclose()

    assert detail["workspace"] == "alpha"
    assert all(task["workspace"] == "alpha" for task in listed["tasks"])
    assert all(match["workspace"] == "alpha" for match in found["matches"])


@pytest.mark.asyncio
async def test_get_task_unknown_id_reports_not_found_not_crash() -> None:
    agent_a = await _agent("alpha")
    try:
        tools = _recall_tools(agent_a)
        detail = await tools["get_task"].handler({"task_id": "task:deadbeefdeadbeef"})
    finally:
        await agent_a.aclose()

    assert "not found" in detail["error"]


@pytest.mark.asyncio
async def test_list_recent_tasks_scope_all_spans_workspaces() -> None:
    agent_a = await _agent("alpha")
    agent_b = await _agent("beta")
    try:
        await _seed_task(agent_a, user_input="alpha work", title="Alpha work")
        run_b = await _seed_task(agent_b, user_input="beta work", title="Beta work")
        tools = _recall_tools(agent_a)

        local = await tools["list_recent_tasks"].handler({})
        crossed = await tools["list_recent_tasks"].handler({"scope": "all"})
    finally:
        await agent_a.aclose()
        await agent_b.aclose()

    local_ids = {task["task_id"] for task in local["tasks"]}
    assert run_b.id not in local_ids  # default scope stays workspace-local

    crossed_ids = {task["task_id"] for task in crossed["tasks"]}
    assert run_b.id in crossed_ids
    beta_row = next(task for task in crossed["tasks"] if task["task_id"] == run_b.id)
    assert beta_row["workspace"] == "beta"


@pytest.mark.asyncio
async def test_list_recent_tasks_scope_all_days_window() -> None:
    agent_a = await _agent("alpha")
    try:
        fresh = await _seed_task(agent_a, user_input="today", title="Today")
        stale = await _seed_task(
            agent_a,
            user_input="last week",
            title="Old",
            created_at=datetime.now(UTC) - timedelta(days=5),
        )
        tools = _recall_tools(agent_a)
        windowed = await tools["list_recent_tasks"].handler(
            {"scope": "all", "days": 2}
        )
    finally:
        await agent_a.aclose()

    ids = {task["task_id"] for task in windowed["tasks"]}
    assert fresh.id in ids
    assert stale.id not in ids


@pytest.mark.asyncio
async def test_search_tasks_scope_all_matches_across_workspaces() -> None:
    agent_a = await _agent("alpha")
    agent_b = await _agent("beta")
    try:
        run_b = await _seed_task(
            agent_b,
            user_input="RAG 系统综述",
            title="RAG systematic review",
            summary="done",
        )
        tools = _recall_tools(agent_a)

        local = await tools["search_tasks"].handler(
            {"query": "RAG systematic review"}
        )
        crossed = await tools["search_tasks"].handler(
            {"query": "RAG systematic review", "scope": "all"}
        )
    finally:
        await agent_a.aclose()
        await agent_b.aclose()

    assert all(match["task_id"] != run_b.id for match in local.get("matches", []))
    crossed_ids = {match["task_id"] for match in crossed["matches"]}
    assert run_b.id in crossed_ids
