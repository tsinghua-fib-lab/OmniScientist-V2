"""The recovery commands must see every object kind the resolver names.

``omni task retry`` and ``omni task resume`` resolved only skill executions and
workflow steps, so a top-level turn — the object a user actually holds an id for
— came back as ``Subtask <id> was not found``. The typed coordinator that knows
all four kinds was written and tested but reached no command; only the REPL's
retry hook called it. These tests pin each verb to the coordinator's contract.
"""

from __future__ import annotations

from typer.testing import CliRunner

from omni.cli.main import app
from omni.cli.state import AppState, make_agent, run_async
from omni.storage.models import SubtaskORM, TaskORM
from tests.conftest import cli_text

runner = CliRunner()


def _seed_turn(project: str, *, task_id: str, status: str) -> str:
    """Create one top-level turn in ``project`` and return its session id."""

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
                        status=status,
                        title=f"{project} turn",
                        user_input="summarise the attention paper",
                    )
                )
                await session.commit()
            return session_id
        finally:
            await agent.aclose()

    return run_async(_seed())


def _seed_execution(
    project: str, *, session_id: str, task_id: str, execution_id: str, status: str
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
                        project=project,
                        skill_name="literature-search",
                        status=status,
                        input_json={"query": "attention"},
                    )
                )
                await session.commit()
        finally:
            await agent.aclose()

    run_async(_seed())


def _run(project: str, *args: str):
    return runner.invoke(app, ["--project", project, "task", *args])


def test_retrying_a_turn_is_not_reported_as_a_missing_subtask():
    """A turn id names a task, and retry has to say something true about it.

    A succeeded turn is the cheapest proof of resolution: retry declines it on
    state, which it can only do after finding the task. The old command never
    got that far and denied the object instead.
    """
    project = "recovery-retry-turn"
    task_id = "9a1c4e77b0d24f6f8e3a2b5c6d7e8f90"
    _seed_turn(project, task_id=task_id, status="succeeded")

    result = _run(project, "retry", task_id[:8])

    said = " ".join((result.stdout + result.stderr).split())
    assert "was not found" not in said
    assert "Subtask" not in said
    assert task_id[:8] in said


def test_resuming_a_waiting_turn_explains_the_checkpoint_instead_of_denying_it():
    """A planner question has no checkpoint, and the user deserves to know why."""
    project = "recovery-resume-turn"
    task_id = "1b2c3d4e5f60718293a4b5c6d7e8f901"
    _seed_turn(project, task_id=task_id, status="needs_input")

    result = _run(project, "resume", task_id[:8])

    said = " ".join((result.stdout + result.stderr).split())
    assert "Subtask" not in said
    assert "checkpoint" in said.lower()
    assert f"omni task retry {task_id[:8]}" in said


def test_resume_accepts_the_choice_the_coordinator_asks_for():
    """The coordinator resolves a clarification by choice; the CLI must pass one."""
    result = runner.invoke(app, ["task", "resume", "--help"])

    assert "--input" in cli_text(result.stdout)


def test_requeue_returns_a_skill_execution_to_the_queue():
    """The old in-place resume keeps its behaviour under its own verb."""
    project = "recovery-requeue-execution"
    task_id = "2c3d4e5f60718293a4b5c6d7e8f90123"
    execution_id = "3d4e5f60718293a4b5c6d7e8f9012345"
    session_id = _seed_turn(project, task_id=task_id, status="failed")
    _seed_execution(
        project,
        session_id=session_id,
        task_id=task_id,
        execution_id=execution_id,
        status="failed",
    )

    result = _run(project, "requeue", execution_id[:8])

    assert result.exit_code == 0
    assert execution_id[:8] in result.stdout


def test_requeue_refuses_a_turn_and_names_the_verb_that_fits():
    """Requeue is for standalone executions; a turn belongs to retry."""
    project = "recovery-requeue-turn"
    task_id = "4e5f60718293a4b5c6d7e8f901234567"
    _seed_turn(project, task_id=task_id, status="failed")

    result = _run(project, "requeue", task_id[:8])

    said = " ".join((result.stdout + result.stderr).split())
    assert "Subtask" not in said
    assert f"omni task retry {task_id[:8]}" in said
