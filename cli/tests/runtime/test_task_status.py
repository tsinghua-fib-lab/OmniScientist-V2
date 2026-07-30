"""Single source of truth for a task's user-facing status.

Unit coverage for :mod:`omni.runtime.task_status`: the helpers ``/task show``,
``/inbox`` and ``/schedule`` all read so they can never disagree, plus the
settle-wait that fixes the "``pending`` turn result mislabels a passing run as
``degraded``" race.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from omni.runtime.task_status import (
    TERMINAL_TASK_STATUSES,
    await_settled_status,
    is_terminal,
    resolve_task_status,
)


@dataclass
class _Task:
    status: str


def test_resolve_status_reads_the_row_and_tolerates_none():
    assert resolve_task_status(_Task("succeeded")) == "succeeded"
    assert resolve_task_status(_Task("")) == ""
    assert resolve_task_status(None) == ""


def test_is_terminal_matches_the_settled_set():
    for status in ("succeeded", "degraded", "failed", "cancelled", "needs_input"):
        assert status in TERMINAL_TASK_STATUSES
        assert is_terminal(status) is True
    for status in ("", "pending", "running", "queued"):
        assert is_terminal(status) is False


class _FakeTasks:
    """``get_task`` yields a scripted sequence of task rows (one per re-read)."""

    def __init__(self, statuses: list[str]) -> None:
        self._statuses = list(statuses)
        self.reads = 0

    async def get_task(self, task_id: str):  # noqa: ANN201
        self.reads += 1
        idx = min(self.reads - 1, len(self._statuses) - 1)
        return _Task(self._statuses[idx])


@pytest.mark.asyncio
async def test_await_settled_returns_once_the_row_settles():
    # pending → pending → succeeded: the settle-wait must return the durable
    # terminal status, not the transient one delivered by the turn result.
    tasks = _FakeTasks(["pending", "pending", "succeeded"])
    status, task = await await_settled_status(tasks, "t1", attempts=5, delay=0.0)
    assert status == "succeeded"
    assert task is not None and task.status == "succeeded"
    assert tasks.reads == 3


@pytest.mark.asyncio
async def test_await_settled_gives_up_after_attempts_with_last_seen():
    tasks = _FakeTasks(["pending"])
    status, _task = await await_settled_status(tasks, "t1", attempts=3, delay=0.0)
    assert status == "pending"  # never settled; report what we last saw
    assert tasks.reads == 3


@pytest.mark.asyncio
async def test_await_settled_short_circuits_on_empty_id():
    tasks = _FakeTasks(["succeeded"])
    status, task = await await_settled_status(tasks, "", attempts=3, delay=0.0)
    assert status == "" and task is None
    assert tasks.reads == 0  # never queried
