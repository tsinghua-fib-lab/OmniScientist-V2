"""Durable clarification checkpoints: the concurrency-safe resume backbone.

These pin the invariants the admission design depends on: a selection is a
compare-and-set, replays converge, an ambiguous id prefix fails closed, only the
original requester may answer, and an expired draft never resolves.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.runtime.action_checkpoints import (
    STATE_CANCELLED,
    STATE_RESOLVED,
    ActionCheckpointStore,
    AmbiguousCheckpointId,
)
from omni.storage.models import ActionCheckpointORM

_RESOLUTION = {
    "status": "ambiguous",
    "unresolved_fields": ["day_period"],
    "candidates": [
        {"id": "am", "value": {"kind": "once", "at": "2026-07-30T07:10:00+08:00"}, "validity": "past"},
        {"id": "pm", "value": {"kind": "once", "at": "2026-07-30T19:10:00+08:00"}, "validity": "future"},
    ],
}


def _open_kwargs(**over):
    base = dict(
        action_kind="schedule.create",
        contract_version="v1",
        policy_version="temporal-policy-v1",
        channel="cli",
        session_id="s1",
        actor_principal="local",
        payload={"goal": "prep RAG材料", "when": {"raw_expression": "今天7点10分"}},
        resolution=_RESOLUTION,
    )
    base.update(over)
    return base


async def _agent():
    return await OmniAgent.create(load_settings())


@pytest.mark.asyncio
async def test_open_dedups_identical_live_draft():
    agent = await _agent()
    try:
        store = ActionCheckpointStore(agent.db)
        a = await store.open_clarification(**_open_kwargs())
        b = await store.open_clarification(**_open_kwargs())
        assert a.id == b.id  # same requester + payload ⇒ one durable draft
        assert a.state == "open"
        assert a.required_decider == "local"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_get_prefix_is_fail_closed():
    agent = await _agent()
    try:
        store = ActionCheckpointStore(agent.db)
        async with agent.db.session() as s:
            s.add(ActionCheckpointORM(id="dupprefix-1", required_decider="local", state="open"))
            s.add(ActionCheckpointORM(id="dupprefix-2", required_decider="local", state="open"))
            await s.commit()
        # A unique full id resolves…
        assert (await store.get("dupprefix-1")).id == "dupprefix-1"
        # …but an ambiguous prefix raises rather than picking the first match.
        with pytest.raises(AmbiguousCheckpointId):
            await store.get("dupprefix")
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_resolve_cas_then_replay_then_conflict():
    agent = await _agent()
    try:
        store = ActionCheckpointStore(agent.db)
        rec = await store.open_clarification(**_open_kwargs())

        first = await store.resolve(rec.id, candidate_id="pm", decider="local")
        assert first.status == "resolved"
        assert first.candidate["value"]["at"].endswith("19:10:00+08:00")

        replay = await store.resolve(rec.id, candidate_id="pm", decider="local")
        assert replay.status == "replayed"

        conflict = await store.resolve(rec.id, candidate_id="am", decider="local")
        assert conflict.status == "conflict"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_only_original_requester_may_resolve():
    agent = await _agent()
    try:
        store = ActionCheckpointStore(agent.db)
        rec = await store.open_clarification(
            **_open_kwargs(actor_principal="wechat:alice", required_decider="wechat:alice")
        )
        forbidden = await store.resolve(rec.id, candidate_id="pm", decider="local")
        assert forbidden.status == "forbidden"
        # The rightful requester still can.
        ok = await store.resolve(rec.id, candidate_id="pm", decider="wechat:alice")
        assert ok.status == "resolved"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_invalid_candidate_is_rejected():
    agent = await _agent()
    try:
        store = ActionCheckpointStore(agent.db)
        rec = await store.open_clarification(**_open_kwargs())
        out = await store.resolve(rec.id, candidate_id="midnight", decider="local")
        assert out.status == "invalid_candidate"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_concurrent_selection_has_a_single_winner():
    agent = await _agent()
    try:
        store = ActionCheckpointStore(agent.db)
        rec = await store.open_clarification(**_open_kwargs())
        # Two racing selections of *different* candidates: CAS admits exactly one.
        a, b = await asyncio.gather(
            store.resolve(rec.id, candidate_id="pm", decider="local"),
            store.resolve(rec.id, candidate_id="am", decider="local"),
        )
        statuses = sorted([a.status, b.status])
        assert statuses.count("resolved") == 1
        assert "conflict" in statuses
        # And the checkpoint ended resolved exactly once.
        final = await store.get(rec.id)
        assert final.state == STATE_RESOLVED
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_expired_checkpoint_does_not_resolve():
    agent = await _agent()
    try:
        store = ActionCheckpointStore(agent.db)
        rec = await store.open_clarification(**_open_kwargs(ttl=timedelta(seconds=-5)))
        out = await store.resolve(rec.id, candidate_id="pm", decider="local")
        assert out.status == "expired"
        # expire_due sweeps and list_open no longer shows it.
        assert (await store.list_open(principal="local")) == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_list_open_is_scoped_to_the_decider():
    agent = await _agent()
    try:
        store = ActionCheckpointStore(agent.db)
        await store.open_clarification(**_open_kwargs())
        assert len(await store.list_open(principal="local")) == 1
        assert await store.list_open(principal="someone-else") == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_cancel_then_resolve_is_closed():
    agent = await _agent()
    try:
        store = ActionCheckpointStore(agent.db)
        rec = await store.open_clarification(**_open_kwargs())
        cancelled = await store.cancel(rec.id, decider="local")
        assert cancelled.status == "resolved"
        assert (await store.get(rec.id)).state == STATE_CANCELLED
        after = await store.resolve(rec.id, candidate_id="pm", decider="local")
        assert after.status == "closed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_attach_result_is_idempotent():
    agent = await _agent()
    try:
        store = ActionCheckpointStore(agent.db)
        rec = await store.open_clarification(**_open_kwargs())
        await store.resolve(rec.id, candidate_id="pm", decider="local")
        assert await store.attach_result(rec.id, result_kind="schedule", result_id="sid-1") is True
        assert await store.attach_result(rec.id, result_kind="schedule", result_id="sid-1") is True
        assert await store.attach_result(rec.id, result_kind="schedule", result_id="sid-2") is False
    finally:
        await agent.aclose()
