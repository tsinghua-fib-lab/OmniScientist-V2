"""Conversational/inspection turns are filed as ``kind="chat"``, not work units.

A terminal-succeeded turn that produced no durable work (no skill/subtask/
workflow/artifact/schedule) and was classified as a conversational intent is
reclassified to ``kind="chat"`` at settle time, so it drops out of the default
``/task`` ledger while staying queryable under ``--kind chat`` and mirrored into
the cross-workspace index.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from omni.cli.commands.tasks_cmd import _TASK_KINDS, _normalize_kind
from omni.config import load_settings
from omni.config.workspaces import register_workspace
from omni.runtime import task_index as task_index_mod
from omni.runtime.aggregate import list_tasks_all_workspaces
from omni.runtime.task_index import TaskIndex
from omni.runtime.task_recorder import TaskRecorder, _is_conversational_turn
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


# --- predicate --------------------------------------------------------------


def test_is_conversational_turn_matches_only_no_work_direct_answers():
    def task(**overrides) -> TaskORM:
        base = {"id": "t", "kind": "turn", "intent_type": "direct_answer"}
        base.update(overrides)
        return TaskORM(**base)

    # No-work conversational intents -> eligible for chat.
    assert _is_conversational_turn(task()) is True
    assert _is_conversational_turn(task(intent_type="react_fallback")) is True

    # Durable work of any shape keeps it a turn.
    assert _is_conversational_turn(task(submitted_subtask_ids=["s1"])) is False
    assert _is_conversational_turn(task(submitted_workflow_ids=["w1"])) is False
    assert _is_conversational_turn(task(artifact_ids=["a1"])) is False
    assert _is_conversational_turn(task(schedule_id="sch1")) is False
    assert _is_conversational_turn(task(parent_task_id="p1")) is False
    assert _is_conversational_turn(task(origin_workflow_run_id="wr1")) is False

    # Non-conversational intents are never reclassified, even with no artifact.
    assert _is_conversational_turn(task(intent_type="single_skill_task")) is False
    assert _is_conversational_turn(task(intent_type="workflow")) is False
    assert _is_conversational_turn(task(intent_type="memory_update")) is False
    assert _is_conversational_turn(task(intent_type="schedule")) is False
    assert _is_conversational_turn(task(intent_type="")) is False

    # Non-turn kinds (subagent / maintenance children) are out of scope.
    assert _is_conversational_turn(task(kind="maintenance")) is False
    assert _is_conversational_turn(task(kind="subagent")) is False


# --- settle-time reclassification -------------------------------------------


@pytest.mark.asyncio
async def test_direct_answer_turn_is_filed_as_chat_and_hidden_from_default_list():
    s, db = await _workspace("alpha")
    rec = TaskRecorder(
        db, project=s.paths.project_name, index=TaskIndex.for_workspace(s.paths)
    )
    task = await rec.create_task(
        session_id="sess", channel="cli", user_input="schedule show 3f3d600d"
    )
    await rec.record_plan(task.id, {"intent_type": "direct_answer"}, status="validated")

    await rec.finish_task(task.id, status="succeeded", summary="here is the schedule")

    settled = await rec.get_task(task.id)
    assert settled.kind == "chat"
    assert settled.status == "succeeded"

    # Hidden from the default (kind=turn) ledger, visible under --kind chat.
    assert [t.id for t in await rec.list_tasks(kind="turn")] == []
    assert [t.id for t in await rec.list_tasks(kind="chat")] == [task.id]

    # The global index mirrors kind, so cross-workspace listing agrees.
    rows = await _index_rows(s.paths.control_db)
    assert [(r.task_id, r.kind) for r in rows] == [(task.id, "chat")]
    turn_all = await list_tasks_all_workspaces(home=s.paths.home, kind="turn")
    assert all(r.id != task.id for r in turn_all)
    chat_all = await list_tasks_all_workspaces(home=s.paths.home, kind="chat")
    assert any(r.id == task.id for r in chat_all)


@pytest.mark.asyncio
async def test_turn_that_did_work_stays_a_turn():
    _s, db = await _workspace("alpha")
    rec = TaskRecorder(db, project=_s.paths.project_name)

    # Direct answer, but it produced an artifact -> real work, stays a turn.
    task = await rec.create_task(
        session_id="sess", channel="cli", user_input="make a figure"
    )
    await rec.record_plan(task.id, {"intent_type": "direct_answer"}, status="validated")
    async with db.session() as sess:
        row = await sess.get(TaskORM, task.id)
        row.artifact_ids = ["art-1"]
        await sess.commit()
    await rec.finish_task(task.id, status="succeeded", summary="figure ready")
    assert (await rec.get_task(task.id)).kind == "turn"

    # A skill-task intent is never reclassified even with nothing recorded yet.
    skill = await rec.create_task(
        session_id="sess", channel="cli", user_input="run a skill"
    )
    await rec.record_plan(skill.id, {"intent_type": "single_skill_task"}, status="validated")
    await rec.finish_task(skill.id, status="succeeded", summary="done")
    assert (await rec.get_task(skill.id)).kind == "turn"


@pytest.mark.asyncio
async def test_failed_direct_answer_stays_visible_as_turn():
    _s, db = await _workspace("alpha")
    rec = TaskRecorder(db, project=_s.paths.project_name)
    task = await rec.create_task(session_id="sess", channel="cli", user_input="broken")
    await rec.record_plan(task.id, {"intent_type": "direct_answer"}, status="validated")
    await rec.finish_task(task.id, status="failed", error="boom")
    assert (await rec.get_task(task.id)).kind == "turn"


@pytest.mark.asyncio
async def test_toggle_off_keeps_conversational_turns_as_turns():
    _s, db = await _workspace("alpha")
    rec = TaskRecorder(
        db, project=_s.paths.project_name, classify_conversational=False
    )
    task = await rec.create_task(session_id="sess", channel="cli", user_input="chit chat")
    await rec.record_plan(task.id, {"intent_type": "direct_answer"}, status="validated")
    await rec.finish_task(task.id, status="succeeded", summary="hi")
    assert (await rec.get_task(task.id)).kind == "turn"


# --- CLI kind contract ------------------------------------------------------


def test_chat_is_an_accepted_task_kind_filter():
    assert "chat" in _TASK_KINDS
    assert _normalize_kind("chat") == "chat"
    assert _normalize_kind("all") is None
    assert _normalize_kind("turn") == "turn"
