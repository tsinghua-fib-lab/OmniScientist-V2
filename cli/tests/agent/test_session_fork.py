"""Session forking (P2): branch a conversation into an independent session.

Forking copies the source transcript into a fresh session so a line of inquiry
can *branch* — explore an alternative without polluting the original — after
which the two sessions evolve independently (no cross-branch contamination).
"""

from __future__ import annotations

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings


async def _seed(agent: OmniAgent, *turns: tuple[str, str]) -> str:
    """Create a session and persist ``(role, content)`` turns; return its id."""
    sid = await agent.ensure_session(channel="cli")
    for role, content in turns:
        await agent._persist_message(sid, role, content)  # noqa: SLF001
    return sid


@pytest.mark.asyncio
async def test_fork_copies_transcript_and_links_back():
    agent = await OmniAgent.create(load_settings())
    try:
        src = await _seed(
            agent, ("user", "研究 RAG 事实一致性"), ("assistant", "基线用 dense retriever")
        )
        src_msgs = await agent.session_messages(src)

        fork_id = await agent.fork_session(src)
        assert fork_id and fork_id != src

        fork_msgs = await agent.session_messages(fork_id)
        assert [(m.role, m.content) for m in fork_msgs] == [
            (m.role, m.content) for m in src_msgs
        ]

        fork_row = await agent.get_session(fork_id)
        assert fork_row is not None
        assert fork_row.forked_from == src
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_fork_is_isolated_from_source_both_ways():
    agent = await OmniAgent.create(load_settings())
    try:
        src = await _seed(agent, ("user", "原始版本"))
        fork_id = await agent.fork_session(src)
        assert fork_id

        base = len(await agent.session_messages(src))

        # Write into the branch → source must be untouched.
        await agent._persist_message(fork_id, "user", "branch-only edit")  # noqa: SLF001
        assert len(await agent.session_messages(src)) == base
        assert len(await agent.session_messages(fork_id)) == base + 1

        # Write into the source → branch must be untouched.
        await agent._persist_message(src, "user", "source-only edit")  # noqa: SLF001
        assert len(await agent.session_messages(src)) == base + 1
        assert len(await agent.session_messages(fork_id)) == base + 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_fork_up_to_message_truncates_the_copy():
    agent = await OmniAgent.create(load_settings())
    try:
        src = await _seed(
            agent,
            ("user", "第一步"),
            ("assistant", "第一步答复"),
            ("user", "第二步"),
            ("assistant", "第二步答复"),
        )
        msgs = await agent.session_messages(src)
        cut_at = msgs[1].id  # rewind: copy only up to the second message inclusive

        fork_id = await agent.fork_session(src, up_to_message=cut_at)
        fork_msgs = await agent.session_messages(fork_id)
        assert [m.content for m in fork_msgs] == ["第一步", "第一步答复"]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_fork_missing_source_returns_none():
    agent = await OmniAgent.create(load_settings())
    try:
        assert await agent.fork_session("does-not-exist") is None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_fork_accepts_prefix_and_custom_title():
    agent = await OmniAgent.create(load_settings())
    try:
        src = await _seed(agent, ("user", "hi"))
        fork_id = await agent.fork_session(src[:8], title="my-branch")
        assert fork_id
        row = await agent.get_session(fork_id)
        assert row is not None and row.title == "my-branch"
    finally:
        await agent.aclose()
