"""Branch coverage for schedule checkpoint resume helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omni.runtime.action_checkpoints import AmbiguousCheckpointId, CheckpointRecord
from omni.runtime.schedule_checkpoint_resume import (
    find_open_checkpoint_for_task,
    map_choice_to_candidate,
    next_day_trigger,
)


def _record(*, candidates: list[dict] | None = None, task_id: str = "") -> CheckpointRecord:
    return CheckpointRecord(
        id="ckpt-aaaaaaaa",
        phase="semantic_clarification",
        action_kind="schedule.create",
        contract_version="v1",
        policy_version="temporal-policy-v1",
        task_id=task_id,
        channel="cli",
        session_id="sess",
        actor_principal="local",
        required_decider="local",
        payload={"goal": "remind"},
        resolution={
            "status": "ambiguous",
            "candidates": candidates
            or [
                {"id": "am", "label": "AM"},
                {"id": "pm", "label": "PM"},
            ],
        },
        state="open",
        version=1,
        idempotency_key="idem-1",
        decision={},
        result_kind="",
        result_id="",
        expires_at=None,
        created_at=None,
    )


def test_map_choice_and_next_day_trigger_edges() -> None:
    record = _record()
    assert map_choice_to_candidate("am", record) == "am"
    assert map_choice_to_candidate("PM", record) == "pm"
    assert map_choice_to_candidate("evening", record) == "pm"
    assert map_choice_to_candidate("pick:am", record) == "am"
    assert map_choice_to_candidate("repair_next_day:pm", record) == "pm"
    assert map_choice_to_candidate("nope", record) == ""
    assert map_choice_to_candidate("", record) == ""

    nxt = next_day_trigger({"at": "2099-01-01T07:10:00", "timezone": "UTC"})
    assert nxt["kind"] == "once" and nxt["at"].startswith("2099-01-02")
    assert next_day_trigger({"at": "not-a-date", "timezone": ""})["at"] == "not-a-date"
    assert next_day_trigger({})["at"] == ""


@pytest.mark.asyncio
async def test_find_open_checkpoint_for_task_fallback_paths() -> None:
    store = SimpleNamespace(
        get_open_for_task=AsyncMock(return_value=None),
        get=AsyncMock(return_value=None),
    )
    assert await find_open_checkpoint_for_task(store, task_id="") is None
    assert await find_open_checkpoint_for_task(store, task_id="task-1") is None
    assert await find_open_checkpoint_for_task(store, task_id="task-1", task_recorder=None) is None

    direct = _record(task_id="task-1")
    store.get_open_for_task = AsyncMock(return_value=direct)
    assert await find_open_checkpoint_for_task(store, task_id="task-1") is direct

    store.get_open_for_task = AsyncMock(return_value=None)
    recorder = SimpleNamespace(
        list_events=AsyncMock(
            return_value=[
                SimpleNamespace(event_type="other", output_json={}),
                SimpleNamespace(
                    event_type="action.checkpoint.created",
                    output_json={"checkpoint_id": "ckpt-aaaaaaaa"},
                ),
            ]
        )
    )
    store.get = AsyncMock(side_effect=AmbiguousCheckpointId("ckpt-aaaaaaaa"))
    assert await find_open_checkpoint_for_task(store, task_id="task-1", task_recorder=recorder) is None

    store.get = AsyncMock(return_value=None)
    assert await find_open_checkpoint_for_task(store, task_id="task-1", task_recorder=recorder) is None

    closed = _record(task_id="task-1")
    object.__setattr__(closed, "state", "resolved")
    store.get = AsyncMock(return_value=closed)
    assert await find_open_checkpoint_for_task(store, task_id="task-1", task_recorder=recorder) is None

    open_record = _record(task_id="task-1")
    store.get = AsyncMock(return_value=open_record)
    assert await find_open_checkpoint_for_task(store, task_id="task-1", task_recorder=recorder) is open_record

    recorder.list_events = AsyncMock(
        return_value=[SimpleNamespace(event_type="action.checkpoint.created", output_json={})]
    )
    assert await find_open_checkpoint_for_task(store, task_id="task-1", task_recorder=recorder) is None
