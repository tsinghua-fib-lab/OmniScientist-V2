"""Codex-aligned "look before asking" continuity fixes.

Covers the four fixes that stop the agent forgetting its own recent output:
1. the model planner receives the full turn context (recalled memory + recent
   activity), not just the bounded turn summary;
2. a principal-scoped, cross-session recent-activity digest is available so
   "最近/上次/again" resolves deterministically (offline mock too);
3. a clarifying question that references prior work is downgraded to a capable,
   tool-enabled look-it-up turn — while genuinely vague requests still ask;
4. lexical recall is CJK-aware (character-bigram fallback for Chinese runs).
"""

from __future__ import annotations

import pytest

from omni.agent.recent_activity import recent_activity_digest
from omni.agent.reference_markers import references_prior_work
from omni.config import load_settings
from omni.memory.service import _keyword_overlap, principal_of
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import TaskORM

# ── Fix 3 (detection): referential markers ──────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "你最近给我生成的架构图是讲的什么啊，给我重新生成一份吧",
        "把上次那个报告再发我一下",
        "刚才那张图重新生成一下",
        "regenerate the figure you generated earlier",
        "show me that diagram again",
    ],
)
def test_references_prior_work_positive(text: str) -> None:
    assert references_prior_work(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "帮我生成一张 RAG 系统架构图",  # a brand-new request, no referent
        "what is retrieval augmented generation",
        "写一段关于 transformer 的介绍",
        "",
    ],
)
def test_references_prior_work_negative(text: str) -> None:
    assert references_prior_work(text) is False


# ── Fix 4: CJK-aware lexical overlap ─────────────────────────────────────────


def test_keyword_overlap_is_cjk_aware() -> None:
    # Whitespace .split() would make these one token each → overlap 0. Bigram
    # tokenisation recovers the shared "架构图 / RAG 系统" signal.
    score = _keyword_overlap("最近的RAG系统架构图", "为你生成的RAG系统架构图")
    assert score > 0.3
    # Unrelated Chinese text stays low.
    assert _keyword_overlap("今天天气怎么样", "为你生成的RAG系统架构图") < 0.2
    # ASCII behaviour is preserved.
    assert _keyword_overlap("neurips format", "prefers neurips format") == 1.0


# ── Fix 2: principal-scoped, cross-session recent-activity digest ────────────


async def _seed(db, artifacts):  # noqa: ANN001
    art = await artifacts.put_bytes(
        b"<svg/>", kind="figure", title="RAG 系统架构图", ext="svg", session_id="s-owner"
    )
    async with db.session() as s:
        s.add(TaskORM(
            id="ownertask01", kind="turn", channel="cli", external_key="", status="succeeded",
            session_id="s-owner", title="生成 RAG 系统架构图",
            artifact_ids=[art.uri.removeprefix("artifact://")],
        ))
        s.add(TaskORM(
            id="peeratask01", kind="turn", channel="feishu", external_key="oc_a",
            status="succeeded", session_id="s-a", title="peer A 的分析报告",
        ))
        s.add(TaskORM(
            id="peerbtask01", kind="turn", channel="feishu", external_key="oc_b",
            status="succeeded", session_id="s-b", title="peer B 的机密图",
        ))
        s.add(TaskORM(
            id="failedreview", kind="turn", channel="cli", external_key="",
            status="failed", session_id="s-owner", title="Paper review failed during extraction",
        ))
        # An in-flight turn has no settled status yet → excluded.
        s.add(TaskORM(id="runningtask", kind="turn", channel="cli", status="running",
                      session_id="s-owner", title="正在进行"))
        await s.commit()


@pytest.mark.asyncio
async def test_recent_activity_digest_owner_unifies_channels() -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    artifacts = ArtifactStore(settings.paths, db)
    await _seed(db, artifacts)

    def owner_of(channel: str, external_key: str) -> str:
        return principal_of(channel, external_key, channel_identity="owner")

    digest = await recent_activity_digest(
        db, artifacts, principal="local", principal_of=owner_of
    )
    # Owner sees CLI + authorised-IM activity unified under `local`.
    assert "生成 RAG 系统架构图" in digest
    assert "RAG 系统架构图" in digest  # artifact output attached
    assert "peer A 的分析报告" in digest
    assert "Paper review failed during extraction" in digest
    assert "正在进行" not in digest  # running turn excluded


@pytest.mark.asyncio
async def test_recent_activity_digest_per_peer_isolation() -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    artifacts = ArtifactStore(settings.paths, db)
    await _seed(db, artifacts)

    def peer_of(channel: str, external_key: str) -> str:
        return principal_of(channel, external_key, channel_identity="per_peer")

    digest = await recent_activity_digest(
        db, artifacts, principal="feishu:oc_a", principal_of=peer_of
    )
    # Peer A sees only its own deliverables — never the owner's or peer B's.
    assert "peer A 的分析报告" in digest
    assert "生成 RAG 系统架构图" not in digest
    assert "peer B 的机密图" not in digest
