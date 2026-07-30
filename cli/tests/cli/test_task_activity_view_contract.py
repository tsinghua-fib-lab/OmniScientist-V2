"""What ``omni task show`` puts in front of someone reading a finished run.

Incident dc787efa: the activity table carried a ``step`` column filled top to
bottom with ``·``. The task was a single delegated skill execution — it had no
workflow plan, so there was no position to report — but the column was emitted
unconditionally and spent width saying "not applicable" twenty times.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omni.cli.commands.tasks_cmd import render_task_detail
from omni.cli.render import console
from omni.storage.models import (
    SubtaskORM,
    TaskEventORM,
    TaskORM,
    WorkflowRunORM,
    WorkflowStepORM,
)

_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _task(**overrides: object) -> TaskORM:
    fields: dict[str, object] = {
        "id": "dc787efa" + "0" * 24,
        "kind": "turn",
        "depth": 0,
        "status": "degraded",
        "title": "review",
        "user_input": "Review of 'Attention Is All You Need'.",
        "summary": "",
        "error": "",
        "channel": "cli",
        "session_id": "s1",
        "created_at": _NOW,
        "started_at": _NOW,
        "finished_at": _NOW,
    }
    fields.update(overrides)
    return TaskORM(**fields)


def _event(seq: int, **overrides: object) -> TaskEventORM:
    fields: dict[str, object] = {
        "id": f"e{seq:04d}",
        "task_id": "dc787efa" + "0" * 24,
        "seq": seq,
        "event_type": "tool.done",
        "status": "ok",
        "name": f"tool-{seq}",
        "created_at": _NOW,
    }
    fields.update(overrides)
    return TaskEventORM(**fields)


def _render(task: TaskORM, events: list[TaskEventORM], steps: list[WorkflowStepORM]) -> str:
    workflows: list[WorkflowRunORM] = []
    subtasks: list[SubtaskORM] = []
    with console.capture() as capture:
        render_task_detail(task, events, workflows, steps, subtasks, [])
    return capture.get()


def _activity_block(view: str) -> str:
    """The activity table only, so header words cannot be matched elsewhere."""
    assert "activity" in view
    return view.split("activity", 1)[1]


def _activity_header(view: str) -> str:
    block = _activity_block(view)
    return next(line for line in block.split("\n") if "type" in line and "status" in line)


def _unwrapped(view: str) -> str:
    """The view with table borders and line wrapping removed.

    A narrow terminal splits a long note across rows; the assertions below are
    about which records survive the window, not about where Rich breaks a line.
    """
    stripped = view.translate({ord(char): None for char in "│┃╇╈┼┤├"})
    return "".join(stripped.split())


def test_a_task_without_a_plan_does_not_show_a_step_column():
    view = _render(_task(), [_event(i) for i in range(1, 6)], [])
    block = _activity_block(view)

    assert "step" not in _activity_header(view)
    # The dots were the whole symptom: a column whose every cell said
    # "this record has no position in a plan".
    assert "·" not in block


def test_a_task_with_a_plan_still_locates_each_record_in_it():
    run_id = "w" * 32
    steps = [
        WorkflowStepORM(
            id="step-one" + "0" * 24,
            workflow_run_id=run_id,
            task_id="dc787efa" + "0" * 24,
            step_key="fetch",
            position=1,
            status="ok",
        ),
        WorkflowStepORM(
            id="step-two" + "0" * 24,
            workflow_run_id=run_id,
            task_id="dc787efa" + "0" * 24,
            step_key="review",
            position=2,
            status="ok",
        ),
    ]
    events = [
        _event(1, workflow_step_id="step-one" + "0" * 24),
        _event(2, workflow_step_id="step-two" + "0" * 24),
    ]

    view = _render(_task(), events, steps)

    assert "step" in _activity_header(view)
    assert "1/2" in view
    assert "2/2" in view


@pytest.mark.parametrize("status", ["running", "degraded"])
def test_the_column_choice_follows_the_plan_not_the_task_status(status: str):
    """A plan-less run has no positions to report whether it is live or over."""
    block = _activity_block(_render(_task(status=status), [_event(1)], []))

    assert "·" not in block


def test_a_single_step_plan_does_not_show_a_step_column():
    """Run 138c7b6e: "does it have a plan" was the wrong question.

    That plan had exactly one step, so every located record read ``1`` and every
    other record read ``·`` — a column with one value and a blank. Having a plan
    is not the point; being able to tell two positions apart is.
    """
    steps = [
        WorkflowStepORM(
            id="step-two" + "0" * 24,
            workflow_run_id="w" * 32,
            task_id="dc787efa" + "0" * 24,
            step_key="step2",
            position=1,
            status="degraded",
        ),
    ]
    events = [
        _event(1, workflow_step_id="step-two" + "0" * 24),
        _event(2),
    ]

    view = _render(_task(), events, steps)

    assert "step" not in _activity_header(view)
    assert "·" not in _activity_block(view)


# ── which records survive the window ──
#
# Run 0792bf0a was opened after it settled to find out why. The refusal that
# shaped it — ``write_file rejected by execution policy: tool_limit_exceeded:10``
# — had happened early and scrolled out of the twenty-row tail, so the view
# showed twenty unremarkable ticks and nothing that explained the outcome.

_REFUSAL = "tool 'write_file' rejected by execution policy: tool_limit_exceeded:10"


def _long_run_with_an_early_refusal() -> list[TaskEventORM]:
    return [
        _event(1, status="rejected", error=_REFUSAL),
        *[_event(i) for i in range(2, 40)],
    ]


def test_a_finished_run_keeps_the_record_that_explains_how_it_ended():
    view = _render(_task(status="degraded"), _long_run_with_an_early_refusal(), [])

    assert "tool_limit_exceeded:10" in _unwrapped(view)


def test_a_finished_run_still_shows_what_happened_most_recently():
    """Keeping the cause must not cost the tail; both fit in the window."""
    view = _render(_task(status="degraded"), _long_run_with_an_early_refusal(), [])

    assert "tool-39" in _unwrapped(view)


def test_a_live_run_is_followed_by_its_tail():
    """While running, the newest records are the point of looking."""
    view = _render(_task(status="running"), _long_run_with_an_early_refusal(), [])

    assert "tool-39" in _unwrapped(view)
    assert "tool_limit_exceeded" not in _unwrapped(view)


def test_the_view_says_it_is_not_showing_everything():
    view = _render(_task(status="degraded"), _long_run_with_an_early_refusal(), [])

    assert "39" in view.split("activity", 1)[1]
