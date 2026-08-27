"""Read APIs, background turns, and resume-without-rewriting-channel."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omni.config import load_settings
from omni.config import trust as trustmod

pytest.importorskip("starlette")

from omni.agent import OmniAgent  # noqa: E402
from omni.storage.artifacts import ArtifactStore  # noqa: E402
from omni.storage.models import SubtaskORM, WorkflowRunORM, WorkflowStepORM  # noqa: E402
from omni.web.app import create_app  # noqa: E402


async def _rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    res = await client.post(
        "/api",
        headers={"X-Omni-Web": "1"},
        json={"method": method, "params": params or {}},
    )
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.asyncio
async def test_turn_sse_and_resume_keeps_cli_channel(tmp_path: Path) -> None:
    work = tmp_path / "sse-repo"
    work.mkdir()
    trustmod.set_trusted(work)

    settings = load_settings(cwd=work, trusted=True)
    seed = await OmniAgent.create(settings)
    try:
        sid = await seed.ensure_session(channel="cli", title="cli-thread")
        await seed.conversations.persist_message(sid, "user", "prior cli turn")
    finally:
        await seed.aclose()

    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        opened = await _rpc(client, "workspace.open", {"path": str(work)})
        assert opened["ok"] is True

        health = await client.get("/health")
        assert health.json()["ok"] is True

        listed = await _rpc(client, "host.listDirectory", {"path": str(tmp_path), "show_hidden": True})
        assert listed["ok"] is True
        assert listed["path"] == str(tmp_path.resolve())

        drawers = await _rpc(client, "task.list", {"workspace": str(work)})
        assert drawers["ok"] is True
        rom = await _rpc(client, "rom.get", {"workspace": str(work)})
        assert rom["ok"] is True
        notebook = await _rpc(client, "notebook.get", {"workspace": str(work)})
        assert notebook["ok"] is True
        cost = await _rpc(client, "cost.get", {"workspace": str(work)})
        assert cost["ok"] is True

        started = await _rpc(
            client,
            "turn.start",
            {
                "workspace": str(work),
                "session_id": sid,
                "text": "say hello",
                "interaction_mode": "auto",
            },
        )
        assert started["ok"] is True
        assert started["session_id"] == sid
        assert started["task_id"]
        # Do not open task.watch here. ASGI SSE ignores httpx timeouts and
        # can ignore CancelledError, which parked Linux 3.12 coverage and
        # Linux 3.13 at 97% until the job was cancelled.
        for _ in range(80):
            messages = await _rpc(
                client, "session.messages", {"workspace": str(work), "session_id": sid}
            )
            if any(row.get("role") == "assistant" for row in messages.get("messages") or []):
                break
            await asyncio.sleep(0.05)
        events = await _rpc(
            client,
            "task.events",
            {"workspace": str(work), "task_id": started["task_id"], "after_seq": 0},
        )
        assert events["ok"] is True
        assert "input_json" not in str(events.get("events"))

        after = await _rpc(client, "session.get", {"workspace": str(work), "session_id": sid})
        assert after["ok"] is True
        assert after["session"]["channel"] == "cli"

        created = await _rpc(client, "session.create", {"workspace": str(work)})
        web_sid = created["session"]["id"]
        assert created["session"]["channel"] == "web"
        filtered = await _rpc(
            client, "session.list", {"workspace": str(work), "channel": "cli"}
        )
        assert all(row["channel"] == "cli" for row in filtered["sessions"])
        assert web_sid not in {row["id"] for row in filtered["sessions"]}
        assert sid in {row["id"] for row in filtered["sessions"]}


@pytest.mark.asyncio
async def test_turn_finishes_without_a_watcher(tmp_path: Path) -> None:
    work = tmp_path / "orphan-watch"
    work.mkdir()
    trustmod.set_trusted(work)
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        created = await _rpc(client, "session.create", {"workspace": str(work)})
        sid = created["session"]["id"]
        started = await _rpc(
            client,
            "turn.start",
            {"workspace": str(work), "session_id": sid, "text": "say hello"},
        )
        assert started["ok"] is True
        for _ in range(50):
            messages = await _rpc(
                client, "session.messages", {"workspace": str(work), "session_id": sid}
            )
            if any(row.get("role") == "assistant" for row in messages.get("messages") or []):
                return
            await asyncio.sleep(0.05)
        raise AssertionError("background turn did not persist an assistant message")


@pytest.mark.asyncio
async def test_session_tasks_and_task_artifacts_are_exact_read_models(tmp_path: Path) -> None:
    work = tmp_path / "task-artifact-navigation"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        session_a = await agent.ensure_session(channel="cli", title="session-a")
        session_b = await agent.ensure_session(channel="cli", title="session-b")
        task_a = await agent.tasks.create_task(
            session_id=session_a,
            channel="cli",
            user_input="task a",
        )
        child = await agent.tasks.create_task(
            session_id=session_a,
            channel="cli",
            user_input="child",
            parent_task_id=task_a.id,
            kind="turn",
        )
        task_b = await agent.tasks.create_task(
            session_id=session_b,
            channel="cli",
            user_input="task b",
        )
        archived = await agent.tasks.create_task(
            session_id=session_a,
            channel="cli",
            user_input="archived task",
        )
        await agent.tasks.finish_task(task_a.id, status="succeeded", summary="done a")
        await agent.tasks.finish_task(child.id, status="succeeded", summary="done child")
        await agent.tasks.finish_task(task_b.id, status="succeeded", summary="done b")
        await agent.tasks.finish_task(archived.id, status="succeeded", summary="archived")
        await agent.tasks.archive_task(archived.id, reason="test fixture")
        store = ArtifactStore(settings.paths, agent.db)
        artifact_a = await store.put_bytes(
            b"a",
            kind="document",
            title="artifact a",
            ext="md",
            mime="text/markdown",
            session_id=session_a,
            task_id=task_a.id,
        )
        artifact_b = await store.put_bytes(
            b"b",
            kind="document",
            title="artifact b",
            ext="md",
            mime="text/markdown",
            session_id=session_b,
            task_id=task_b.id,
        )
    finally:
        await agent.aclose()

    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        tasks = await _rpc(
            client,
            "task.list",
            {"workspace": str(work), "session_id": session_a},
        )
        assert [row["id"] for row in tasks["tasks"]] == [task_a.id]
        assert tasks["tasks"][0]["finished_at"]

        artifacts = await _rpc(
            client,
            "artifact.list",
            {
                "workspace": str(work),
                "session_id": session_a,
                "task_id": task_a.id,
            },
        )
        assert [row["id"] for row in artifacts["artifacts"]] == [artifact_a.id]
        assert artifacts["artifacts"][0]["task_id"] == task_a.id
        assert artifact_b.id not in {row["id"] for row in artifacts["artifacts"]}

        foreign = await _rpc(
            client,
            "artifact.list",
            {
                "workspace": str(work),
                "session_id": session_b,
                "task_id": task_a.id,
            },
        )
        assert foreign["ok"] is False
        assert foreign["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_task_detail_and_artifacts_preserve_execution_identity(tmp_path: Path) -> None:
    work = tmp_path / "execution-identity"
    work.mkdir()
    trustmod.set_trusted(work)
    settings = load_settings(cwd=work, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        sid = await agent.ensure_session(channel="wechat", title="workflow")
        task = await agent.tasks.create_task(
            session_id=sid,
            channel="wechat",
            user_input="build a report",
        )
        workflow_id = "workflow-web-identity"
        step_id = "step-web-identity"
        execution_ids = ["execution-web-attempt-1", "execution-web-attempt-2"]
        async with agent.db.session() as session:
            session.add(
                WorkflowRunORM(
                    id=workflow_id,
                    task_id=task.id,
                    session_id=sid,
                    status="succeeded",
                    goal="Build the report",
                    current_step_id="write",
                    attempt=2,
                )
            )
            await session.commit()
        async with agent.db.session() as session:
            session.add(
                WorkflowStepORM(
                    id=step_id,
                    workflow_run_id=workflow_id,
                    task_id=task.id,
                    step_key="write",
                    position=1,
                    skill_name="scientific-writing",
                    capability="research-report",
                    deliverable="report",
                    status="succeeded",
                    current_execution_id=execution_ids[-1],
                    execution_ids=execution_ids,
                )
            )
            await session.commit()
        async with agent.db.session() as session:
            session.add_all(
                [
                    SubtaskORM(
                        id=execution_ids[0],
                        session_id=sid,
                        task_id=task.id,
                        workflow_run_id=workflow_id,
                        workflow_step_id=step_id,
                        skill_name="scientific-writing",
                        status="failed",
                        attempt=0,
                        step_attempt=1,
                        error="first attempt failed",
                    ),
                    SubtaskORM(
                        id=execution_ids[1],
                        session_id=sid,
                        task_id=task.id,
                        workflow_run_id=workflow_id,
                        workflow_step_id=step_id,
                        skill_name="scientific-writing",
                        status="succeeded",
                        attempt=1,
                        step_attempt=2,
                        retry_of=execution_ids[0],
                        result_json={"summary": "report ready"},
                    ),
                ]
            )
            await session.commit()
        await agent.tasks.append_event(
            task.id,
            event_type="step.completed",
            status="succeeded",
            name="write",
            skill_name="scientific-writing",
            workflow_run_id=workflow_id,
            workflow_step_id=step_id,
            subtask_id=execution_ids[-1],
            summary="second execution completed",
        )
        await agent.tasks.finish_task(task.id, status="succeeded", summary="report done")
        store = ArtifactStore(settings.paths, agent.db)
        artifact = await store.put_bytes(
            b"# Report",
            kind="document",
            title="final report",
            ext="md",
            mime="text/markdown",
            session_id=sid,
            task_id=task.id,
            subtask_id=execution_ids[-1],
            workflow_run_id=workflow_id,
            meta={"presentation_role": "primary"},
        )
    finally:
        await agent.aclose()

    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        detail = await _rpc(
            client,
            "task.get",
            {"workspace": str(work), "task_id": task.id},
        )
        assert detail["ok"] is True
        assert detail["workflows"][0]["workflow_run_id"] == workflow_id
        assert detail["workflows"][0]["attempt"] == 2
        assert detail["steps"][0]["workflow_step_id"] == step_id
        assert detail["steps"][0]["execution_ids"] == execution_ids
        assert [row["subtask_id"] for row in detail["executions"]] == execution_ids
        assert detail["executions"][1]["step_attempt"] == 2
        assert detail["executions"][1]["retry_of"] == execution_ids[0]
        assert detail["subtasks"] == detail["executions"]
        completed = next(
            row for row in detail["events"] if row["summary"] == "second execution completed"
        )
        assert completed["workflow_run_id"] == workflow_id
        assert completed["workflow_step_id"] == step_id
        assert completed["subtask_id"] == execution_ids[-1]

        artifacts = await _rpc(
            client,
            "artifact.list",
            {
                "workspace": str(work),
                "session_id": sid,
                "task_id": task.id,
            },
        )
        row = next(item for item in artifacts["artifacts"] if item["id"] == artifact.id)
        assert row["subtask_id"] == execution_ids[-1]
        assert row["workflow_run_id"] == workflow_id
        assert row["presentation_role"] == "primary"
