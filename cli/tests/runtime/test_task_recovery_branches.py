"""Branch-coverage tests for typed task recovery helpers and dispatch edges."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omni.runtime.task_object_resolver import TaskObjectResolution
from omni.runtime.task_recovery import (
    RecoveryOutcome,
    TaskRecoveryCoordinator,
    _resolve_local_workflow_step,
    _snapshot_from_task,
    enrich_resolution,
)
from omni.storage.models import TaskORM


def _step(
    *,
    step_id: str = "step-aaaaaaaa",
    step_key: str = "diagram",
    execution_id: str = "exec-aaaaaaaa",
    task_id: str = "task-aaaaaaaa",
    workflow_run_id: str = "flow-aaaaaaaa",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=step_id,
        step_key=step_key,
        current_execution_id=execution_id,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
    )


def _run(*, run_id: str = "flow-aaaaaaaa", task_id: str = "task-aaaaaaaa", status: str = "failed"):
    return SimpleNamespace(id=run_id, task_id=task_id, status=status)


def _execution(
    *,
    execution_id: str = "exec-aaaaaaaa",
    status: str = "failed",
    task_id: str = "task-aaaaaaaa",
    workflow_run_id: str = "",
    workflow_step_id: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=execution_id,
        status=status,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
        workflow_step_id=workflow_step_id,
    )


class _FakeRuntime:
    def __init__(
        self,
        *,
        runs: list[Any] | None = None,
        steps_by_run: dict[str, list[Any]] | None = None,
        executions: dict[str, Any] | None = None,
        workflows: dict[str, Any] | None = None,
    ) -> None:
        self._runs = runs or []
        self._steps_by_run = steps_by_run or {}
        self._executions = executions or {}
        self._workflows = workflows or {}
        self.retry_subtask = AsyncMock(return_value="exec-bbbbbbbb")
        self.requeue_subtask = AsyncMock(return_value=True)
        self.retry_workflow_step = AsyncMock(return_value="exec-cccccccc")
        self.resume_workflow_step = AsyncMock(return_value=True)
        self._workflow_step = AsyncMock(return_value=_step())

    async def list_workflow_runs(self, limit: int = 1000) -> list[Any]:
        return list(self._runs)[:limit]

    async def list_workflow_steps(self, run_id: str) -> list[Any]:
        return list(self._steps_by_run.get(run_id, []))

    async def get_subtask(self, execution_id: str) -> Any | None:
        return self._executions.get(execution_id)

    async def get_workflow_run(self, workflow_run_id: str) -> Any | None:
        return self._workflows.get(workflow_run_id)


class _FakeSession:
    def __init__(self, rows: dict[str, Any]) -> None:
        self._rows = rows

    async def get(self, _model: Any, key: str) -> Any | None:
        return self._rows.get(key)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeDB:
    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self._rows = rows or {}

    def session(self) -> _FakeSession:
        return _FakeSession(self._rows)


def _agent(runtime: _FakeRuntime, *, db: _FakeDB | None = None) -> SimpleNamespace:
    return SimpleNamespace(runtime=runtime, db=db or _FakeDB(), settings=SimpleNamespace())


@pytest.mark.asyncio
async def test_resolve_local_workflow_step_covers_lookup_edges() -> None:
    step = _step()
    runtime = _FakeRuntime(runs=[_run()], steps_by_run={"flow-aaaaaaaa": [step]})

    assert await _resolve_local_workflow_step(runtime, "") == (None, None, "not_found")
    run, found, status = await _resolve_local_workflow_step(runtime, "diagram")
    assert status == "ok" and found is step and run.id == "flow-aaaaaaaa"
    run, found, status = await _resolve_local_workflow_step(runtime, "step-aa")
    assert status == "ok" and found is step

    runtime._steps_by_run["flow-aaaaaaaa"] = [step, _step(step_id="step-bbbbbbbb", step_key="diagram")]
    assert (await _resolve_local_workflow_step(runtime, "diagram"))[2] == "ambiguous"
    assert (await _resolve_local_workflow_step(runtime, "missing"))[2] == "not_found"


@pytest.mark.asyncio
async def test_enrich_resolution_and_snapshot_helpers() -> None:
    assert RecoveryOutcome(status="ok").ok is True
    assert RecoveryOutcome(status="not_found").ok is False

    task = TaskORM(
        id="task-aaaaaaaa",
        status="failed",
        user_input="do the thing",
        channel="cli",
        external_key="ext-1",
        input_snapshot_json={"file_uris": ["file://a"]},
    )
    snap = _snapshot_from_task(task)
    assert snap["user_input"] == "do the thing"
    assert snap["file_uris"] == ["file://a"]
    task.input_snapshot_json = {"user_input": "from snap", "file_uris": []}
    assert _snapshot_from_task(task)["user_input"] == "from snap"

    step = _step()
    agent = _agent(_FakeRuntime(runs=[_run()], steps_by_run={"flow-aaaaaaaa": [step]}))
    ambiguous = await enrich_resolution(agent, TaskObjectResolution(status="ambiguous"), "x")
    assert ambiguous.status == "ambiguous"
    already_ok = await enrich_resolution(
        agent,
        TaskObjectResolution(status="ok", object_kind="task", object_id="task-aaaaaaaa"),
        "x",
    )
    assert already_ok.object_kind == "task"
    enriched = await enrich_resolution(agent, TaskObjectResolution(status="not_found"), "diagram")
    assert enriched.status == "ok" and enriched.object_kind == "workflow_step"
    agent.runtime._steps_by_run["flow-aaaaaaaa"] = [
        step,
        _step(step_id="step-bbbbbbbb", step_key="diagram"),
    ]
    assert (
        await enrich_resolution(agent, TaskObjectResolution(status="not_found"), "diagram")
    ).status == "ambiguous"
    kept = await enrich_resolution(agent, TaskObjectResolution(status="not_found"), "nope")
    assert kept.status == "not_found"


@pytest.mark.asyncio
async def test_retry_resume_requeue_dispatch_edges() -> None:
    runtime = _FakeRuntime(
        executions={
            "exec-aaaaaaaa": _execution(),
            "exec-active": _execution(execution_id="exec-active", status="running"),
            "exec-pending": _execution(execution_id="exec-pending", status="pending"),
            "exec-flow": _execution(
                execution_id="exec-flow",
                workflow_run_id="flow-aaaaaaaa",
                workflow_step_id="step-aaaaaaaa",
            ),
        },
        workflows={"flow-aaaaaaaa": _run()},
    )
    runtime.retry_subtask = AsyncMock(side_effect=[None, ValueError("boom"), "exec-new"])
    coord = TaskRecoveryCoordinator(_agent(runtime, db=_FakeDB({"step-aaaaaaaa": _step()})))

    assert (await coord.retry(TaskObjectResolution(status="ambiguous"), object_id="x")).status == "ambiguous"
    assert (await coord.retry(TaskObjectResolution(status="not_found"), object_id="x")).status == "not_found"
    assert (
        await coord.retry(
            TaskObjectResolution(status="ok", object_kind="workflow_run", object_id="flow-aaaaaaaa"),
            object_id="flow-aaaaaaaa",
        )
    ).status == "needs_step"
    assert (
        await coord.retry(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-aaaaaaaa"),
            object_id="exec-aaaaaaaa",
        )
    ).status == "not_found"
    assert (
        await coord.retry(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="missing"),
            object_id="missing",
        )
    ).status == "not_found"
    assert (
        await coord.retry(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-active"),
            object_id="exec-active",
        )
    ).status == "wrong_state"
    assert (
        await coord.retry(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-pending"),
            object_id="exec-pending",
        )
    ).status == "wrong_state"
    runtime.retry_subtask = AsyncMock(side_effect=ValueError("boom"))
    assert (
        await coord.retry(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-aaaaaaaa"),
            object_id="exec-aaaaaaaa",
        )
    ).status == "error"
    runtime.retry_subtask = AsyncMock(return_value="exec-new")
    ok = await coord.retry(
        TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-aaaaaaaa"),
        object_id="exec-aaaaaaaa",
        notify_channel="cli",
    )
    assert ok.status == "ok" and ok.new_id == "exec-new"

    assert (
        await coord.retry(
            TaskObjectResolution(status="ok", object_kind="workflow_step", object_id="step-aaaaaaaa"),
            object_id="step-aaaaaaaa",
        )
    ).status == "ok"
    assert (
        await coord.retry(
            TaskObjectResolution(status="ok", object_kind="workflow_step", object_id="missing-step"),
            object_id="missing-step",
        )
    ).status == "not_found"
    runtime.retry_workflow_step = AsyncMock(return_value=None)
    assert (
        await coord.retry(
            TaskObjectResolution(
                status="ok", object_kind="workflow_run", object_id="flow-aaaaaaaa", task_id="task-aaaaaaaa"
            ),
            object_id="flow-aaaaaaaa",
            step="diagram",
        )
    ).status == "not_found"
    runtime.retry_workflow_step = AsyncMock(return_value="exec-cccccccc")
    assert (
        await coord.retry(
            TaskObjectResolution(
                status="ok", object_kind="workflow_run", object_id="flow-aaaaaaaa", task_id="task-aaaaaaaa"
            ),
            object_id="flow-aaaaaaaa",
            step="diagram",
        )
    ).status == "ok"
    assert (
        await coord.retry(
            TaskObjectResolution(status="ok", object_kind="unknown", object_id="x"),  # type: ignore[arg-type]
            object_id="x",
        )
    ).status == "error"

    assert (await coord.resume(TaskObjectResolution(status="ambiguous"), object_id="x")).status == "ambiguous"
    assert (await coord.resume(TaskObjectResolution(status="not_found"), object_id="x")).status == "not_found"
    assert (
        await coord.resume(
            TaskObjectResolution(status="ok", object_kind="workflow_run", object_id="flow-aaaaaaaa"),
            object_id="flow-aaaaaaaa",
        )
    ).status == "needs_step"
    assert (
        await coord.resume(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="missing"),
            object_id="missing",
        )
    ).status == "not_found"
    assert (
        await coord.resume(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-aaaaaaaa"),
            object_id="exec-aaaaaaaa",
        )
    ).status == "checkpoint_required"
    runtime._workflow_step = AsyncMock(return_value=None)
    assert (
        await coord.resume(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-flow"),
            object_id="exec-flow",
        )
    ).status == "not_found"
    runtime._workflow_step = AsyncMock(return_value=_step())
    assert (
        await coord.resume(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-flow"),
            object_id="exec-flow",
        )
    ).status == "ok"
    assert (
        await coord.resume(
            TaskObjectResolution(status="ok", object_kind="workflow_step", object_id="step-aaaaaaaa"),
            object_id="step-aaaaaaaa",
        )
    ).status == "ok"
    assert (
        await coord.resume(
            TaskObjectResolution(status="ok", object_kind="workflow_step", object_id="missing-step"),
            object_id="missing-step",
        )
    ).status == "not_found"
    runtime.resume_workflow_step = AsyncMock(return_value=False)
    assert (
        await coord.resume(
            TaskObjectResolution(status="ok", object_kind="workflow_run", object_id="flow-aaaaaaaa"),
            object_id="flow-aaaaaaaa",
            step="diagram",
        )
    ).status == "wrong_state"
    runtime.resume_workflow_step = AsyncMock(return_value=True)
    assert (
        await coord.resume(
            TaskObjectResolution(status="ok", object_kind="workflow_run", object_id="missing-flow"),
            object_id="missing-flow",
            step="diagram",
        )
    ).status == "not_found"
    assert (
        await coord.resume(
            TaskObjectResolution(status="ok", object_kind="unknown", object_id="x"),  # type: ignore[arg-type]
            object_id="x",
        )
    ).status == "error"

    assert (await coord.requeue(TaskObjectResolution(status="ambiguous"), object_id="x")).status == "ambiguous"
    assert (await coord.requeue(TaskObjectResolution(status="not_found"), object_id="x")).status == "not_found"
    assert (
        await coord.requeue(
            TaskObjectResolution(status="ok", object_kind="task", object_id="task-aaaaaaaa"),
            object_id="task-aaaaaaaa",
        )
    ).status == "wrong_state"
    assert (
        await coord.requeue(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="missing"),
            object_id="missing",
        )
    ).status == "not_found"
    assert (
        await coord.requeue(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-flow"),
            object_id="exec-flow",
        )
    ).status == "wrong_state"
    assert (
        await coord.requeue(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-active"),
            object_id="exec-active",
        )
    ).status == "wrong_state"
    runtime.requeue_subtask = AsyncMock(return_value=False)
    assert (
        await coord.requeue(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-aaaaaaaa"),
            object_id="exec-aaaaaaaa",
        )
    ).status == "wrong_state"
    runtime.requeue_subtask = AsyncMock(return_value=True)
    assert (
        await coord.requeue(
            TaskObjectResolution(status="ok", object_kind="skill_execution", object_id="exec-aaaaaaaa"),
            object_id="exec-aaaaaaaa",
        )
    ).status == "ok"
