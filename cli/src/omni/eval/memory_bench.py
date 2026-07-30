"""Persistent-memory benchmark (P3): injection hit + citation hit + zero leakage.

Where the capability benchmark (``omni.eval.runner``) scores the *agent*, this
scores the durable-memory contract that makes OmniScientist a daily research
agent rather than an amnesiac one. It exercises the six dimensions that matter —
cross-session, cross-workspace, cross-channel, isolation, concurrency, offline —
and rolls every check up to one of three metrics:

* ``injection_hit`` — a durable preference is actually recalled when it should be
  (the memory reaches the next turn / workspace / channel);
* ``citation_hit`` — a source-anchored memory outranks an equally-similar but
  ungrounded recollection (the provenance moat);
* ``zero_leakage`` — a peer's memory never surfaces for the owner or another peer.

Pure store I/O — no network, no LLM (keyword recall) — so it is deterministic and
runs green in CI, against a throwaway data home so it never touches the owner's
real global store. Reuses the capability benchmark's :class:`BenchmarkReport`
model so the scoreboard / JSON trend format is identical.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from omni.config import load_settings
from omni.config.settings import OmniSettings
from omni.eval.report import BenchmarkReport, CheckOutcome, ScenarioResult
from omni.memory.service import (
    PRINCIPAL_OWNER,
    MemoryLayer,
    MemoryService,
    open_global_store,
    principal_of,
)
from omni.storage.db import _DATABASES, get_database

# The P3 scoreboard: every check attributes to one of these three metrics.
INJECTION_HIT = "injection_hit"
CITATION_HIT = "citation_hit"
ZERO_LEAKAGE = "zero_leakage"


@contextmanager
def _isolated_home() -> Iterator[str]:
    """Point ``OMNI_HOME`` at a throwaway dir for the duration of the benchmark.

    Keeps the run reproducible and, crucially, prevents the benchmark's writes
    from polluting the owner's real machine-global memory store.
    """
    prev = os.environ.get("OMNI_HOME")
    tmp = tempfile.mkdtemp(prefix="omni-membench-")
    os.environ["OMNI_HOME"] = tmp
    try:
        yield tmp
    finally:
        if prev is None:
            os.environ.pop("OMNI_HOME", None)
        else:
            os.environ["OMNI_HOME"] = prev
        shutil.rmtree(tmp, ignore_errors=True)


async def _dispose_under(tmp: str) -> None:
    """Dispose + drop cached Databases created under ``tmp`` (no dangling handles)."""
    root = str(Path(tmp).resolve())
    for key in [k for k in _DATABASES if k.startswith(root)]:
        await _DATABASES.pop(key).dispose()


async def _service(project: str, *, channel_identity: str = "owner") -> tuple[MemoryService, OmniSettings]:
    """Build an offline MemoryService for ``project`` (workspace db + shared global)."""
    s = load_settings(project=project, overrides={"memory": {"channel_identity": channel_identity}})
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    gdb = open_global_store(s)
    if gdb is not None:
        await gdb.init()
    return MemoryService(db, s, llm=None, global_db=gdb), s


def _hit(scored: list, needle: str) -> bool:
    return any(needle in sm.entry.summary for sm in scored)


async def _cross_session() -> ScenarioResult:
    """A durable preference set in one session is recalled in a brand-new one."""
    sid = "cross_session"
    mem, _s = await _service("bench_sess")
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="user prefers SI units in all answers", memory_type="preference", importance=0.8,
    )
    res = await mem.recall("which unit system do I prefer", cross_session=True)
    return ScenarioResult(sid, "durable pref recalled in a new session", (sid,), [
        CheckOutcome(sid, INJECTION_HIT, "recall_new_session", _hit(res, "SI units")),
    ])


async def _cross_workspace() -> ScenarioResult:
    """An owner preference set in project A is recalled from project B."""
    sid = "cross_workspace"
    mem_a, _ = await _service("bench_ws_a")
    await mem_a.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="user prefers NeurIPS submission format", memory_type="preference", importance=0.8,
    )
    mem_b, _ = await _service("bench_ws_b")
    res = await mem_b.recall("submission format preference", cross_session=True)
    return ScenarioResult(sid, "owner pref crosses workspaces", (sid,), [
        CheckOutcome(sid, INJECTION_HIT, "recall_other_workspace", _hit(res, "NeurIPS")),
    ])


async def _cross_channel() -> ScenarioResult:
    """What the owner says on Feishu (authorized ⇒ owner) is recalled in the CLI."""
    sid = "cross_channel"
    mem, s = await _service("bench_chan", channel_identity="owner")
    feishu = principal_of("feishu", "u-9", channel_identity=s.memory.channel_identity)
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
        summary="user prefers answers in metric units", memory_type="preference",
        importance=0.8, principal=feishu,
    )
    cli = principal_of("cli", "", channel_identity=s.memory.channel_identity)
    res = await mem.recall("units preference", principal=cli, cross_session=True)
    return ScenarioResult(sid, "authorized IM identity maps to owner", (sid,), [
        CheckOutcome(sid, INJECTION_HIT, "feishu_pref_recalled_in_cli",
                     feishu == PRINCIPAL_OWNER and _hit(res, "metric units")),
    ])


async def _isolation() -> ScenarioResult:
    """per_peer mode: a peer's memory never reaches the owner or another peer."""
    sid = "isolation"
    mem, s = await _service("bench_iso", channel_identity="per_peer")
    p1 = principal_of("feishu", "u-1", channel_identity=s.memory.channel_identity)
    p2 = principal_of("feishu", "u-2", channel_identity=s.memory.channel_identity)
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="user", scope_id=p1,
        summary="peer one prefers verbose proofs", memory_type="preference",
        importance=0.8, principal=p1,
    )
    owner = principal_of("cli", "", channel_identity=s.memory.channel_identity)
    res_owner = await mem.recall("verbose proofs", principal=owner, cross_session=True)
    res_p2 = await mem.recall("verbose proofs", principal=p2, cross_session=True)
    res_p1 = await mem.recall("verbose proofs", principal=p1, cross_session=True)
    return ScenarioResult(sid, "per_peer isolation has zero cross-talk", (sid,), [
        CheckOutcome(sid, ZERO_LEAKAGE, "owner_blind_to_peer", not _hit(res_owner, "verbose proofs")),
        CheckOutcome(sid, ZERO_LEAKAGE, "peer_blind_to_peer", not _hit(res_p2, "verbose proofs")),
        CheckOutcome(sid, INJECTION_HIT, "peer_recalls_own", _hit(res_p1, "verbose proofs")),
    ])


async def _concurrency() -> ScenarioResult:
    """Two workspaces write to the shared global store at once; both survive."""
    sid = "concurrency"
    mem_a, _ = await _service("bench_cc_a")
    mem_b, _ = await _service("bench_cc_b")
    await asyncio.gather(
        mem_a.record(
            layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
            summary="user prefers concise abstracts", memory_type="preference", importance=0.8,
        ),
        mem_b.record(
            layer=MemoryLayer.SEMANTIC, scope="user", scope_id="local",
            summary="user prefers figures over tables", memory_type="preference", importance=0.8,
        ),
    )
    # A third workspace recalls both — proves both landed and the store is intact.
    mem_c, _ = await _service("bench_cc_c")
    res1 = await mem_c.recall("concise abstracts", cross_session=True)
    res2 = await mem_c.recall("figures over tables", cross_session=True)
    return ScenarioResult(sid, "concurrent writes survive without corruption", (sid,), [
        CheckOutcome(sid, INJECTION_HIT, "first_concurrent_write_recalled", _hit(res1, "concise abstracts")),
        CheckOutcome(sid, INJECTION_HIT, "second_concurrent_write_recalled", _hit(res2, "figures over tables")),
    ])


async def _offline_citation() -> ScenarioResult:
    """Offline keyword recall + citation ranking: grounded outranks ungrounded."""
    sid = "offline"
    mem, _ = await _service("bench_cite")
    summary = "transformer attention scales quadratically with sequence length"
    await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="project", scope_id="p",
        summary=summary, memory_type="finding", importance=0.6,
    )
    grounded = await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="project", scope_id="p",
        summary=summary, memory_type="finding", importance=0.6, payload_ref="source://src-1",
    )
    res = await mem.recall("attention quadratic sequence length", cross_session=True, limit=2)
    top_is_grounded = bool(res) and res[0].entry.id == grounded
    return ScenarioResult(sid, "grounded memory outranks ungrounded (offline)", (sid,), [
        CheckOutcome(sid, CITATION_HIT, "grounded_ranks_first", top_is_grounded),
    ])


async def run_memory_benchmark() -> BenchmarkReport:
    """Run the persistent-memory benchmark offline; return a scored report.

    Every scenario is one memory dimension; every check rolls up to one of the
    three P3 metrics (:data:`INJECTION_HIT` / :data:`CITATION_HIT` /
    :data:`ZERO_LEAKAGE`). Deterministic and side-effect-free (throwaway home).
    """
    with _isolated_home() as tmp:
        try:
            results = [
                await _cross_session(),
                await _cross_workspace(),
                await _cross_channel(),
                await _isolation(),
                await _concurrency(),
                await _offline_citation(),
            ]
        finally:
            await _dispose_under(tmp)
    return BenchmarkReport(results=results)


__all__ = [
    "INJECTION_HIT",
    "CITATION_HIT",
    "ZERO_LEAKAGE",
    "run_memory_benchmark",
]
