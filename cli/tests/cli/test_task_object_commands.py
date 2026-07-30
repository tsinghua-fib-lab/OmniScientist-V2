"""Public CLI contracts for typed Task/Workflow object resolution."""

from __future__ import annotations

import inspect

from sqlalchemy import select
from typer.testing import CliRunner

from omni.cli.commands.tasks_cmd import (
    _awaits_a_checkpoint,
    _task_recommended_action,
    resume_cmd,
)
from omni.cli.main import app
from omni.cli.state import AppState, make_agent, run_async
from omni.storage.models import (
    ConversationMessageORM,
    SubtaskORM,
    TaskEventORM,
    TaskORM,
    WorkflowRunORM,
)

runner = CliRunner()


def _seed_task_and_workflow(
    project: str,
    *,
    task_id: str,
    workflow_id: str,
) -> str:
    async def _seed() -> str:
        agent = await make_agent(AppState(project=project))
        try:
            session_id = await agent.ensure_session(channel="cli", title=project)
            async with agent.db.session() as session:
                session.add(
                    TaskORM(
                        id=task_id,
                        session_id=session_id,
                        project=project,
                        channel="cli",
                        kind="turn",
                        status="succeeded",
                        title=f"{project} task",
                        user_input=f"{project} request",
                        submitted_workflow_ids=[workflow_id],
                        current_workflow_id=workflow_id,
                    )
                )
                await session.flush()
                session.add(
                    WorkflowRunORM(
                        id=workflow_id,
                        task_id=task_id,
                        session_id=session_id,
                        project=project,
                        status="succeeded",
                        goal=f"{project} workflow",
                        result_json={"summary": "workflow complete"},
                    )
                )
                await session.commit()
            return session_id
        finally:
            await agent.aclose()

    return run_async(_seed())


def _seed_skill_execution(
    project: str,
    *,
    session_id: str,
    task_id: str,
    workflow_id: str,
    execution_id: str,
) -> None:
    async def _seed() -> None:
        agent = await make_agent(AppState(project=project))
        try:
            async with agent.db.session() as session:
                session.add(
                    SubtaskORM(
                        id=execution_id,
                        session_id=session_id,
                        task_id=task_id,
                        workflow_run_id=workflow_id,
                        project=project,
                        skill_name="literature-search",
                        status="succeeded",
                        result_json={"summary": "execution complete"},
                    )
                )
                await session.commit()
        finally:
            await agent.aclose()

    run_async(_seed())


def _create_session(project: str) -> str:
    async def _create() -> str:
        agent = await make_agent(AppState(project=project))
        try:
            return await agent.ensure_session(channel="cli", title=f"{project} target")
        finally:
            await agent.aclose()

    return run_async(_create())


def _attachment_messages(project: str, session_id: str) -> list[ConversationMessageORM]:
    async def _load() -> list[ConversationMessageORM]:
        agent = await make_agent(AppState(project=project))
        try:
            async with agent.db.session() as db_session:
                return list(
                    (
                        await db_session.execute(
                            select(ConversationMessageORM).where(
                                ConversationMessageORM.session_id == session_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await agent.aclose()

    return run_async(_load())


def test_show_and_attach_fail_closed_for_cross_kind_prefix_collision():
    project = "object-prefix-collision"
    task_id = "abcd123400000000000000000000000001"
    workflow_id = "abcd123400000000000000000000000002"
    session_id = _seed_task_and_workflow(
        project,
        task_id=task_id,
        workflow_id=workflow_id,
    )

    shown = runner.invoke(app, ["--project", project, "task", "show", "abcd1234"])
    attached = runner.invoke(
        app,
        [
            "--project",
            project,
            "task",
            "attach",
            "abcd1234",
            "--session",
            session_id[:8],
        ],
    )

    assert shown.exit_code == 1
    assert "ambiguous" in (shown.stdout + shown.stderr).lower()
    assert attached.exit_code == 1
    assert "ambiguous" in (attached.stdout + attached.stderr).lower()

    exact_task = runner.invoke(app, ["--project", project, "task", "show", task_id])
    exact_workflow = runner.invoke(
        app, ["--project", project, "task", "show", workflow_id]
    )
    assert exact_task.exit_code == 0
    assert "object_kind" in exact_task.stdout
    assert exact_workflow.exit_code == 0
    assert "workflow_run" in exact_workflow.stdout


def test_show_and_attach_route_workflow_prefix_across_workspaces():
    owner_project = "object-owner"
    caller_project = "object-caller"
    task_id = "05571218b61b4f1aab86fd83a660c75e"
    workflow_id = "f4902f1686924dd9a74efa920bbc6626"
    _seed_task_and_workflow(
        owner_project,
        task_id=task_id,
        workflow_id=workflow_id,
    )
    # Register and initialise a distinct caller workspace.
    caller_session_id = _create_session(caller_project)

    shown = runner.invoke(
        app,
        ["--project", caller_project, "task", "show", workflow_id[:8]],
    )
    attached = runner.invoke(
        app,
        [
            "--project",
            caller_project,
            "task",
            "attach",
            workflow_id[:8],
            "--session",
            caller_session_id[:8],
        ],
    )

    assert shown.exit_code == 0
    assert f"Workflow {workflow_id[:8]}" in shown.stdout
    assert f"Full task: /task show {task_id[:8]}" in " ".join(shown.stdout.split())
    assert attached.exit_code == 0
    assert f"Attached workflow_run {workflow_id[:8]}" in " ".join(
        attached.stdout.split()
    )
    messages = _attachment_messages(caller_project, caller_session_id)
    assert messages[-1].meta["kind"] == "task_attachment"
    assert messages[-1].meta["attached_object_kind"] == "workflow_run"
    assert messages[-1].meta["attached_task_id"] == task_id


def test_show_routes_skill_execution_prefix_across_workspaces():
    owner_project = "execution-object-owner"
    caller_project = "execution-object-caller"
    task_id = "05571218b61b4f1aab86fd83a660c75e"
    workflow_id = "f4902f1686924dd9a74efa920bbc6626"
    execution_id = "e67299a1bc2e4595afffa680c0ddc26b"
    session_id = _seed_task_and_workflow(
        owner_project,
        task_id=task_id,
        workflow_id=workflow_id,
    )
    _seed_skill_execution(
        owner_project,
        session_id=session_id,
        task_id=task_id,
        workflow_id=workflow_id,
        execution_id=execution_id,
    )
    caller = run_async(make_agent(AppState(project=caller_project)))
    run_async(caller.aclose())

    shown = runner.invoke(
        app,
        ["--project", caller_project, "task", "show", execution_id[:8]],
    )

    normalized = " ".join(shown.stdout.split())
    assert shown.exit_code == 0
    assert f"Skill execution {execution_id[:8]}" in normalized
    assert "object_kind skill_execution" in normalized
    assert f"task_id {task_id}" in normalized
    assert f"Full task: /task show {task_id[:8]}" in normalized


def _waiting_task() -> TaskORM:
    return TaskORM(
        id="7d6b2b0a1f5c4e2b9a3d8c7f6e5d4c3b",
        session_id="s",
        project="p",
        channel="cli",
        kind="turn",
        status="needs_input",
        title="waiting",
        user_input="waiting",
    )


def test_a_planner_question_is_not_pointed_at_a_resume_it_has_no_checkpoint_for():
    """A question asked before any tool ran leaves nothing to resume."""
    advice = _task_recommended_action(_waiting_task(), resumable=_awaits_a_checkpoint([]))

    assert "resume" not in advice
    assert advice == "answer in session"


def test_a_suspended_action_offers_the_shortcut_that_resolves_it():
    """A tool that suspended mid-run left a choice ``--input`` can carry."""
    waiting = _waiting_task()
    opened = [TaskEventORM(task_id=waiting.id, event_type="action.checkpoint.created")]

    advice = _task_recommended_action(waiting, resumable=_awaits_a_checkpoint(opened))

    assert advice == f"omni task resume {waiting.id[:8]} --input <choice>"


def test_an_answered_question_stops_advertising_a_resume():
    """A resolved checkpoint would refuse the resume it used to justify."""
    waiting = _waiting_task()
    settled = [
        TaskEventORM(task_id=waiting.id, event_type="action.checkpoint.created"),
        TaskEventORM(task_id=waiting.id, event_type="action.checkpoint.resolved"),
    ]

    assert _awaits_a_checkpoint(settled) is False


def test_every_flag_the_advice_names_exists_on_the_command_it_names():
    """Pin the recommendation against the real signature of ``resume``.

    The advice once named ``--input`` for two years' worth of waiting tasks while
    the command had no such option. Deriving the truth from the signature means
    the next rename breaks this test rather than the user's next command.
    """
    accepted = {
        option
        for parameter in inspect.signature(resume_cmd).parameters.values()
        for option in getattr(parameter.default, "param_decls", None) or ()
    }
    waiting = _waiting_task()
    opened = [TaskEventORM(task_id=waiting.id, event_type="action.checkpoint.created")]

    advice = _task_recommended_action(waiting, resumable=_awaits_a_checkpoint(opened))

    named = {word for word in advice.split() if word.startswith("--")}
    assert named
    assert named <= accepted
