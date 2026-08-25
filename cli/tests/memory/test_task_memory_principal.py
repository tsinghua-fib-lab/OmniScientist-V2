"""P2-G regression: background task memory writes must respect the principal.

A background task submitted by an IM peer completes asynchronously and writes
M2 (task_result) + M5 (artifact) memory. Before the fix those writes defaulted to
the machine owner (``local``), so a peer's async result leaked into the owner's
recallable memory. These tests pin the completion path to the owning session's
principal and assert cross-principal isolation on the cross-session M5 layer.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.memory.service import MemoryLayer
from omni.storage.models import ConversationMessageORM, MemoryEntryORM, SubtaskORM


async def _make_agent():
    # per_peer is the mode whose contract is strict isolation ("zero cross-talk");
    # under the default ``owner`` mode an authorized IM identity maps to the owner
    # by design, so the "leak" these tests guard against is only a leak here.
    settings = load_settings(
        overrides={"model": {"provider": "mock"}, "memory": {"channel_identity": "per_peer"}}
    )
    settings.paths.ensure_dirs()
    return await OmniAgent.create(settings)


async def _complete_task_for(agent, *, channel: str, external_key: str) -> str:
    """Run a background task to success under (channel, external_key); return subtask_id."""
    session_id = await agent.ensure_session(channel=channel, external_key=external_key)
    rt = agent.runtime
    async with rt._db.session() as s:
        task = SubtaskORM(
            session_id=session_id,
            project="test",
            skill_name="demo-skill",
            status="running",
            input_json={"input": "x"},
            notify_channel="",
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        subtask_id = task.id
    result = {
        "status": "ok",
        "summary": "produced a figure",
        "artifacts": [{"uri": "artifact://p2g-fig-1", "label": "figure"}],
    }
    await rt._complete_subtask(  # noqa: SLF001 - exercise the completion chokepoint.
        subtask_id,
        result,
        [],
        "",
        SimpleNamespace(name="demo-skill"),
        session_id,
        status="succeeded",
        persist_message=True,
    )
    return subtask_id


async def _entries_for_task(agent, subtask_id: str) -> list[MemoryEntryORM]:
    async with agent.runtime._db.session() as s:
        return list(
            (
                await s.execute(select(MemoryEntryORM).where(MemoryEntryORM.scope_id == subtask_id))
            ).scalars().all()
        )


@pytest.mark.asyncio
async def test_background_task_memory_is_written_under_peer_principal():
    agent = await _make_agent()
    try:
        subtask_id = await _complete_task_for(agent, channel="wechat", external_key="wx-p2g")
        entries = await _entries_for_task(agent, subtask_id)
        layers = {e.layer for e in entries}
        principals = {e.principal for e in entries}

        assert MemoryLayer.TASK.value in layers, "M2 task_result memory not written"
        assert MemoryLayer.ARTIFACT.value in layers, "M5 artifact memory not written"
        # The whole point of the fix: not the owner ("local").
        assert principals == {"wechat:wx-p2g"}, principals
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_owner_cannot_recall_peer_task_artifact_memory():
    agent = await _make_agent()
    try:
        await _complete_task_for(agent, channel="wechat", external_key="wx-peer")

        def labels(scored):
            return " ".join(s.entry.summary for s in scored)

        peer_hits = await agent.memory.recall_scoped(
            "产物", layers=[MemoryLayer.ARTIFACT.value], principal="wechat:wx-peer"
        )
        owner_hits = await agent.memory.recall_scoped(
            "产物", layers=[MemoryLayer.ARTIFACT.value], principal="local"
        )

        assert "demo-skill" in labels(peer_hits), "peer must recall its own artifact memory"
        assert "demo-skill" not in labels(owner_hits), "owner must NOT see the peer's artifact memory (leak)"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_write_back_skips_when_session_is_gone():
    agent = await _make_agent()
    try:
        await agent.runtime._write_back_result(  # noqa: SLF001
            "missing-session-id",
            "sub-gone",
            "demo-skill",
            "produced a figure",
            {"summary": "produced a figure", "artifacts": [{"uri": "artifact://gone", "label": "figure"}]},
        )
        async with agent.runtime._db.session() as session:
            messages = list(
                (
                    await session.execute(
                        select(ConversationMessageORM).where(
                            ConversationMessageORM.session_id == "missing-session-id"
                        )
                    )
                ).scalars().all()
            )
            memories = list(
                (
                    await session.execute(
                        select(MemoryEntryORM).where(MemoryEntryORM.scope_id == "sub-gone")
                    )
                ).scalars().all()
            )
        assert messages == []
        assert memories == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_memory_update_skips_unresolved_principal():
    from omni.agent.intent_plan import IntentPlan, IntentType
    from omni.agent.plan_executor import PlanExecutor

    recorded: list[dict] = []

    class Mem:
        async def record(self, **kwargs):  # noqa: ANN003
            recorded.append(kwargs)
            return "mid"

    class Tasks:
        async def append_event(self, *_args: object, **_kwargs: object) -> object:
            return object()

    executor = PlanExecutor(runtime=None, tasks=Tasks(), registry=None, memory=Mem())
    plan = IntentPlan(
        task_id="t",
        user_message="remember this",
        intent_type=IntentType.MEMORY_UPDATE,
        capability_inputs={"memory.update": {"content": "I prefer APA citations"}},
    )
    result = await executor._memory_update(  # noqa: SLF001
        plan, ctx=SimpleNamespace(principal="unresolved", task_id="t")
    )
    assert recorded == []
    assert result.kind == "error"
    assert result.terminated_reason == "unresolved_principal"
