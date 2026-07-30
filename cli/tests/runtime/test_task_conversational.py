"""Conversational turns are filed as ``kind="chat"``, not work units.

A terminal-succeeded turn the planner routed to a direct answer, which then
answered without reaching for anything — no tool call, no delegated child task,
no skill/subtask/workflow/artifact/schedule — is reclassified to ``kind="chat"``
at settle time, so it drops out of the default ``/task`` ledger while staying
queryable under ``--kind chat`` and mirrored into the cross-workspace index.

Everything else stays a turn. The tests below hold that line from the side it
actually failed on: the old predicate read work off the task row alone, where a
turn that shells out, writes memory or spawns sub-agents leaves no mark, and so
it hid the agent loop's longest runs.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event, select

from omni.cli.commands.tasks_cmd import _TASK_KINDS, _normalize_kind
from omni.config import load_settings
from omni.config.workspaces import register_workspace
from omni.runtime import task_index as task_index_mod
from omni.runtime import task_recorder as task_recorder_mod
from omni.runtime.aggregate import list_tasks_all_workspaces
from omni.runtime.task_index import TaskIndex
from omni.runtime.task_recorder import (
    TaskRecorder,
    _is_conversational_turn,
    repair_misfiled_chat,
)
from omni.storage.db import get_database
from omni.storage.models import TaskIndexORM, TaskORM


@pytest.fixture(autouse=True)
def _reset_reconcile_guard():
    """The one-shot reconcile guards are process-global; clear them per test."""
    task_index_mod._reconciled.clear()
    task_recorder_mod._repaired_chat.clear()
    yield
    task_index_mod._reconciled.clear()
    task_recorder_mod._repaired_chat.clear()


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

    # A no-work direct answer -> eligible for chat.
    assert _is_conversational_turn(task()) is True

    # react_fallback is the general agent loop, not a conversational intent.
    assert _is_conversational_turn(task(intent_type="react_fallback")) is False

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
async def test_a_turn_that_ran_the_agent_loop_is_never_filed_as_chat():
    """``react_fallback`` is where the heaviest work happens, not small talk.

    Run 2db31f83 asked for shell permission, then spent 473 seconds on 48 bash
    calls, 4 read_file, 2 remember, and 2 spawn_subagents that produced four
    child tasks — and settled as ``chat``, so it left the ``/task`` ledger the
    moment it finished. It recorded no subtask, workflow or artifact, which was
    the whole of the old test for "durable work".
    """
    _s, db = await _workspace("alpha")
    rec = TaskRecorder(db, project=_s.paths.project_name)
    task = await rec.create_task(
        session_id="sess", channel="cli", user_input="review today's commits"
    )
    await rec.record_plan(task.id, {"intent_type": "react_fallback"}, status="validated")

    await rec.finish_task(task.id, status="succeeded", summary="reviewed")

    assert (await rec.get_task(task.id)).kind == "turn"


@pytest.mark.asyncio
async def test_a_turn_that_spawned_a_child_is_not_a_conversation():
    """Child tasks are durable work that no ``submitted_*`` column records."""
    _s, db = await _workspace("alpha")
    rec = TaskRecorder(db, project=_s.paths.project_name)
    parent = await rec.create_task(
        session_id="sess", channel="cli", user_input="look into this"
    )
    await rec.record_plan(parent.id, {"intent_type": "direct_answer"}, status="validated")
    child = await rec.create_task(
        session_id="sess", channel="cli", user_input="reviewer", kind="subagent"
    )
    async with db.session() as sess:
        row = await sess.get(TaskORM, child.id)
        row.parent_task_id = parent.id
        await sess.commit()

    await rec.finish_task(parent.id, status="succeeded", summary="looked")

    assert (await rec.get_task(parent.id)).kind == "turn"


@pytest.mark.asyncio
async def test_a_turn_that_called_a_tool_is_not_a_conversation():
    """Anything that reached for a tool did something worth finding later."""
    _s, db = await _workspace("alpha")
    rec = TaskRecorder(db, project=_s.paths.project_name)
    task = await rec.create_task(
        session_id="sess", channel="cli", user_input="what changed?"
    )
    await rec.record_plan(task.id, {"intent_type": "direct_answer"}, status="validated")
    await rec.append_event(
        task.id, event_type="react.tool.done", status="succeeded", name="bash"
    )

    await rec.finish_task(task.id, status="succeeded", summary="answered")

    assert (await rec.get_task(task.id)).kind == "turn"


@pytest.mark.asyncio
async def test_an_answer_that_touched_nothing_is_still_a_conversation():
    """The case the feature was built for survives: a pure look-up answer."""
    _s, db = await _workspace("alpha")
    rec = TaskRecorder(db, project=_s.paths.project_name)
    task = await rec.create_task(
        session_id="sess", channel="cli", user_input="schedule show 3f3d600d"
    )
    await rec.record_plan(task.id, {"intent_type": "direct_answer"}, status="validated")

    await rec.finish_task(task.id, status="succeeded", summary="here it is")

    assert (await rec.get_task(task.id)).kind == "chat"


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


# --- repairing what the old predicate hid -----------------------------------


@pytest.mark.asyncio
async def test_listing_returns_the_turns_the_old_predicate_hid():
    """Rows already filed as ``chat`` come back the next time anyone looks.

    Fixing the predicate only protects future turns; the ones already misfiled
    stay invisible until something moves them, and the user cannot be expected
    to know they exist to ask for them.
    """
    _s, db = await _workspace("alpha")
    rec = TaskRecorder(db, project=_s.paths.project_name)
    worked = await rec.create_task(session_id="sess", channel="cli", user_input="review")
    talked = await rec.create_task(session_id="sess", channel="cli", user_input="hello")
    async with db.session() as sess:
        row = await sess.get(TaskORM, worked.id)
        row.kind, row.intent_type, row.status = "chat", "react_fallback", "succeeded"
        row = await sess.get(TaskORM, talked.id)
        row.kind, row.intent_type, row.status = "chat", "direct_answer", "succeeded"
        await sess.commit()

    listed = [t.id for t in await rec.list_tasks(kind="turn")]

    assert worked.id in listed
    # A turn that really was a conversation is left where it is.
    assert talked.id not in listed
    assert [t.id for t in await rec.list_tasks(kind="chat")] == [talked.id]


@pytest.mark.asyncio
async def test_the_repair_reports_what_it_moved_and_then_stays_quiet():
    """Idempotent: a second sweep finds nothing left to move."""
    _s, db = await _workspace("alpha")
    rec = TaskRecorder(db, project=_s.paths.project_name)
    task = await rec.create_task(session_id="sess", channel="cli", user_input="review")
    async with db.session() as sess:
        row = await sess.get(TaskORM, task.id)
        row.kind, row.intent_type = "chat", "react_fallback"
        await sess.commit()

    assert await repair_misfiled_chat(db, force=True) == 1
    assert await repair_misfiled_chat(db, force=True) == 0


@pytest.mark.asyncio
async def test_a_workspace_with_nothing_to_repair_is_never_written_to():
    """The repair runs on a list, which may happen while a task is writing.

    SQLite grants one writer at a time, so a workspace that was already swept
    must answer with a read. Taking the write lock to discover there is nothing
    to do is taking it from a run that has something to do.
    """
    _s, db = await _workspace("alpha")
    rec = TaskRecorder(db, project=_s.paths.project_name)
    task = await rec.create_task(session_id="sess", channel="cli", user_input="hello")
    async with db.session() as sess:
        row = await sess.get(TaskORM, task.id)
        row.kind, row.intent_type = "chat", "direct_answer"
        await sess.commit()
    writes: list[str] = []

    @event.listens_for(db.engine.sync_engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, *_args):  # noqa: ANN001, ANN202
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            writes.append(statement)

    try:
        assert await repair_misfiled_chat(db, force=True) == 0
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", _record)

    assert writes == []


# --- CLI kind contract ------------------------------------------------------


def test_chat_is_an_accepted_task_kind_filter():
    assert "chat" in _TASK_KINDS
    assert _normalize_kind("chat") == "chat"
    assert _normalize_kind("all") is None
    assert _normalize_kind("turn") == "turn"
