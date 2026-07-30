"""Durable audit storage for immutable typed-plan revisions."""

from __future__ import annotations

import json

import pytest

from omni.config import load_settings
from omni.runtime import task_recorder as task_recorder_module
from omni.runtime.task_recorder import TaskRecorder
from omni.storage.db import get_database


@pytest.mark.asyncio
async def test_revision_events_keep_full_history_above_the_default_event_limit() -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    recorder = TaskRecorder(db, project=settings.paths.project_name)
    task = await recorder.create_task(
        session_id="session-revision",
        channel="cli",
        user_input="prepare a typed plan",
    )

    await recorder.record_plan(
        task.id,
        {"intent_type": "workflow", "workflow_steps": []},
        status="validated",
        emit_event=False,
    )
    assert not any(
        event.event_type == "plan.validated"
        for event in await recorder.list_events(task.id)
    )

    payload = {
        "revision": 2,
        "revision_id": "plan:r2:hash",
        "content_hash": "hash",
        "parent_hash": "parent",
        "source": "compiler",
        "stage": "accepted",
        "finding_ids": ["finding-1"],
        "diff": [],
        "catalog_hash": "catalog",
        "contract_hash": "contract",
        "plan": {"blob": "x" * 20_000},
    }
    await recorder.append_event(
        task.id,
        event_type="plan.revision.accepted",
        status="succeeded",
        name="compiler",
        output_json=payload,
    )

    event = (await recorder.list_events(task.id))[-1]
    assert event.output_json["source"] == "compiler"
    assert event.output_json["plan"]["blob"] == "x" * 20_000
    assert event.output_json.get("truncated") is not True


@pytest.mark.asyncio
async def test_oversized_revision_keeps_queryable_provenance_when_plan_is_clipped() -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    recorder = TaskRecorder(db, project=settings.paths.project_name)
    task = await recorder.create_task(
        session_id="session-oversized-revision",
        channel="cli",
        user_input="prepare a very large typed plan",
    )
    payload = {
        "revision": 3,
        "revision_id": "plan:r3:large",
        "content_hash": "large-hash",
        "parent_hash": "parent-hash",
        "source": "model_repair",
        "stage": "candidate",
        "finding_ids": ["finding-large"],
        "diff": [],
        "catalog_hash": "catalog-hash",
        "contract_hash": "contract-hash",
        "plan": {"blob": "y" * 300_000},
    }

    await recorder.append_event(
        task.id,
        event_type="plan.revision.candidate",
        status="rejected",
        name="model_repair",
        output_json=payload,
    )

    event = (await recorder.list_events(task.id))[-1]
    assert event.output_json["truncated"] is True
    assert event.output_json["source"] == "model_repair"
    assert event.output_json["content_hash"] == "large-hash"
    assert event.output_json["catalog_hash"] == "catalog-hash"
    assert event.output_json["contract_hash"] == "contract-hash"
    assert event.output_json["plan_preview"]


@pytest.mark.asyncio
async def test_revision_event_limit_counts_utf8_bytes() -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    recorder = TaskRecorder(db, project=settings.paths.project_name)
    task = await recorder.create_task(
        session_id="session-multibyte-revision",
        channel="cli",
        user_input="prepare a multilingual typed plan",
    )

    await recorder.append_event(
        task.id,
        event_type="plan.revision.candidate",
        status="rejected",
        name="model_repair",
        output_json={
            "revision": 4,
            "content_hash": "multibyte-hash",
            "source": "model_repair",
            "plan": {"blob": "中" * 100_000},
        },
    )

    event = (await recorder.list_events(task.id))[-1]
    assert event.output_json["truncated"] is True
    assert event.output_json["content_hash"] == "multibyte-hash"
    assert len(json.dumps(event.output_json).encode("utf-8")) <= (
        task_recorder_module._PLAN_REVISION_JSON_LIMIT
    )


@pytest.mark.asyncio
async def test_revision_event_limit_includes_json_escape_overhead() -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    recorder = TaskRecorder(db, project=settings.paths.project_name)
    task = await recorder.create_task(
        session_id="session-escaped-revision",
        channel="cli",
        user_input="prepare a quoted multilingual typed plan",
    )

    await recorder.append_event(
        task.id,
        event_type="plan.revision.candidate",
        status="rejected",
        name="model_repair",
        output_json={
            "revision": 5,
            "content_hash": "escaped-hash",
            "source": "model_repair",
            "diff": [{"value": '"\\中' * 20_000}],
            "plan": {"blob": '"\\中' * 100_000},
        },
    )

    event = (await recorder.list_events(task.id))[-1]
    assert event.output_json["truncated"] is True
    assert event.output_json["content_hash"] == "escaped-hash"
    assert len(json.dumps(event.output_json).encode("utf-8")) <= (
        task_recorder_module._PLAN_REVISION_JSON_LIMIT
    )


@pytest.mark.asyncio
async def test_plan_transition_atomically_updates_projection_and_ordered_audit() -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    recorder = TaskRecorder(db, project=settings.paths.project_name)
    task = await recorder.create_task(
        session_id="session-plan-transition",
        channel="cli",
        user_input="seal one plan transition",
    )
    prior_events = await recorder.list_events(task.id)

    await recorder.record_plan_transition(
        task.id,
        {
            "intent_type": "workflow",
            "revision_hash": "accepted-hash",
            "workflow_steps": [],
        },
        status="validated",
        current_authority_fingerprint="authority-hash",
        events=[
            {
                "event_type": "plan.revision.accepted",
                "status": "succeeded",
                "name": "compiler",
                "output_json": {"content_hash": "accepted-hash"},
                "summary": "accepted revision",
            },
            {
                "event_type": "plan.validated",
                "status": "succeeded",
                "name": "workflow",
                "output_json": {"revision_hash": "accepted-hash"},
                "summary": "validated revision",
            },
        ],
    )

    persisted = await recorder.get_task(task.id)
    events = await recorder.list_events(task.id)
    assert persisted is not None
    assert persisted.plan_json["revision_hash"] == "accepted-hash"
    assert persisted.current_authority_fingerprint == "authority-hash"
    assert [event.event_type for event in events[-2:]] == [
        "plan.revision.accepted",
        "plan.validated",
    ]
    assert [event.seq for event in events[-2:]] == [
        prior_events[-1].seq + 1,
        prior_events[-1].seq + 2,
    ]


@pytest.mark.asyncio
async def test_plan_transition_rolls_back_projection_when_audit_batch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    recorder = TaskRecorder(db, project=settings.paths.project_name)
    task = await recorder.create_task(
        session_id="session-plan-transition-rollback",
        channel="cli",
        user_input="reject a partial transition",
    )
    before = await recorder.get_task(task.id)
    before_events = await recorder.list_events(task.id)
    original_event_row = task_recorder_module._event_row
    calls = 0

    def _fail_second_event(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic audit insert failure")
        return original_event_row(*args, **kwargs)

    monkeypatch.setattr(task_recorder_module, "_event_row", _fail_second_event)

    with pytest.raises(RuntimeError, match="synthetic audit insert failure"):
        await recorder.record_plan_transition(
            task.id,
            {"intent_type": "workflow", "revision_hash": "must-not-persist"},
            status="validated",
            current_authority_fingerprint="must-not-persist",
            events=[
                {"event_type": "plan.revision.accepted"},
                {"event_type": "plan.validated"},
            ],
        )

    after = await recorder.get_task(task.id)
    after_events = await recorder.list_events(task.id)
    assert before is not None and after is not None
    assert after.plan_json == before.plan_json
    assert after.current_authority_fingerprint == (
        before.current_authority_fingerprint
    )
    assert [event.id for event in after_events] == [
        event.id for event in before_events
    ]
