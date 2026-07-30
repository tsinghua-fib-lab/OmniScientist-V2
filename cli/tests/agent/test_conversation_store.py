"""ConversationStore: session + transcript persistence in isolation.

Extracted from the orchestrator god-object, the store now owns sessions, the
message transcript, the compaction-aware prompt history, and the per-session
principal cache. These tests pin that contract directly (not via a full agent
turn), which is exactly why the extraction is worthwhile.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import OperationalError

from omni.agent.conversation_store import ConversationStore
from omni.config import load_settings
from omni.storage.db import get_database
from omni.storage.models import ConversationMessageORM


async def _store() -> ConversationStore:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    return ConversationStore(
        db,
        project_name=settings.paths.project_name,
        channel_identity=settings.memory.channel_identity,
    )


@pytest.mark.asyncio
async def test_ensure_session_caches_principal_and_history_roundtrips():
    store = await _store()

    session_id = await store.ensure_session(channel="cli")
    assert session_id
    # The owner principal is cached synchronously (no DB read on the hot path).
    assert store._session_principal[session_id] == "local"  # noqa: SLF001

    await store.persist_message(session_id, "user", "hi")
    await store.persist_message(session_id, "assistant", "hello")

    history = await store.history(session_id)
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert [m["content"] for m in history] == ["hi", "hello"]


@pytest.mark.asyncio
async def test_principal_for_session_reads_row_on_cold_cache():
    store = await _store()
    session_id = await store.ensure_session(channel="feishu", external_key="u42")

    # Cold cache (simulate a daemon restart): the principal is re-derived from
    # the persisted row (matching the identity policy), not lost.
    warm = await store.principal_for_session(session_id)
    store._session_principal.clear()  # noqa: SLF001
    cold = await store.principal_for_session(session_id)

    assert cold == warm == store.principal_of("feishu", "u42")


def test_normal_rows_excludes_compacted_and_bridge_rows():
    rows = [
        ConversationMessageORM(session_id="s", role="user", content="keep"),
        ConversationMessageORM(session_id="s", role="assistant", content="hidden", meta={"compacted": True}),
        ConversationMessageORM(session_id="s", role="user", content="bridge", content_type="compaction"),
        ConversationMessageORM(session_id="s", role="assistant", content="keep2"),
    ]
    visible = ConversationStore.normal_rows(rows)
    assert [r.content for r in visible] == ["keep", "keep2"]


@pytest.mark.asyncio
async def test_the_compaction_bridge_is_replayed_as_material_not_as_the_models_own_words():
    """A summary in the assistant's voice is a claim the model will stand behind.

    The bridge for one session said the research was done and both reports were
    stored; turns later the model was still restating it, on a request that had
    produced neither. Codex hands its summary over as the user's for this reason.
    """
    store = await _store()
    session_id = await store.ensure_session(channel="wechat")
    await store.persist_message(session_id, "user", "现在就执行吧")
    await store.write_compaction_bridge(session_id, "[Earlier conversation summary]\n调研已完成", [])

    history = await store.history(session_id)
    bridge = [m for m in history if "调研已完成" in m["content"]]

    assert [m["role"] for m in bridge] == ["user"]


@pytest.mark.asyncio
async def test_fork_session_copies_transcript_and_warms_principal_cache():
    store = await _store()
    src = await store.ensure_session(channel="cli", title="src")
    await store.persist_message(src, "user", "seed")

    forked = await store.fork_session(src, title="branch")

    assert forked and forked != src
    assert forked in store._session_principal  # noqa: SLF001
    messages = await store.session_messages(forked)
    assert any(m.content == "seed" for m in messages)


@pytest.mark.asyncio
async def test_persist_message_swallows_sqlite_busy():
    store = await _store()
    session_id = await store.ensure_session(channel="cli")
    attempts = {"n": 0}

    @asynccontextmanager
    async def always_busy():
        attempts["n"] += 1
        raise OperationalError(
            "INSERT conversation_messages", {}, Exception("database is locked")
        )
        yield  # pragma: no cover

    store._db.session = always_busy
    await store.persist_message(
        session_id,
        "assistant",
        "Partial result: The user cancelled execution.",
        kind="partial",
        terminated_reason="cancelled",
    )
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_get_session_resolves_by_unique_prefix():
    store = await _store()
    session_id = await store.ensure_session(channel="cli")

    resolved = await store.get_session(session_id[:8])
    assert resolved is not None
    assert resolved.id == session_id
