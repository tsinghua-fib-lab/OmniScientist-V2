"""Async multi-agent control plane — Codex ``AgentControl`` V2 parity for omni.

The blocking fork-join :func:`run_subagents <omni.agent.subagents.run_subagents>`
serves a batch and returns only when *all* specialists finish. This module adds
the async half of the pattern: a coordinating ReAct turn can **fire** a
specialist (:meth:`spawn`), keep doing other work, then **collect** its result
later (:meth:`wait`) — mirroring Codex V2's ``spawn_agent`` / ``wait_agent``.

Design (the blocking counterpart lives in :mod:`omni.agent.subagents`):

* **Turn-scoped.** One :class:`SubagentControl` is created per coordinating turn
  (``ctx.subagent_control``) and joined/cancelled at turn end (:meth:`aclose`).
  It does not survive the turn — durable async work stays with the skill
  subtask runtime; this is the in-turn ``AgentControl`` analog.
* **Per-child isolation.** Each async subagent runs
  :func:`~omni.agent.subagents.run_subagent` in its own ``asyncio.Task`` with its
  **own** :class:`~omni.core.execution_control.ExecutionControl`, so
  :meth:`interrupt` targets exactly one child while a parent cancel still fans
  out through :meth:`aclose`.
* **Session-tree concurrency cap.** A single ``Semaphore(cfg.max_active)`` bounds
  *concurrently executing* subagents (the ``AgentExecutionLimiter`` analog),
  independent of how many have been spawned.

Isolation, budgets, and the reviewer gate are unchanged: they all live in
``run_subagent`` and are reused verbatim.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any

from omni.config.settings import SubagentsCfg
from omni.core.execution_control import ExecutionControl
from omni.skills_runtime.context import ExecContext

# Result-level statuses that mean the specialist completed usefully (vs failed).
_DONE_STATUSES = frozenset({"ok", "partial", "escalated"})


def _short(text: str, limit: int = 200) -> str:
    value = (text or "").strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


@dataclass
class LiveSubagent:
    """One in-flight (or finished) async subagent tracked by the control plane."""

    nickname: str
    spec: Any  # SubagentSpec — kept ``Any`` to avoid an import cycle.
    control: ExecutionControl
    task: asyncio.Task[Any] | None = None
    status: str = "running"  # running | done | error | cancelled
    result: Any = None  # SubagentResult | None
    final: dict[str, Any] | None = None
    collected: bool = False  # returned by a ``wait(None)`` already (avoid re-serving)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)


class SubagentControl:
    """Turn-scoped registry + limiter for async subagents (Codex V2 parity)."""

    def __init__(self, ctx: ExecContext, *, cfg: SubagentsCfg, depth: int = 0) -> None:
        self._ctx = ctx
        self._cfg = cfg
        self._settings = ctx.settings
        self._depth = int(depth)
        self._sem = asyncio.Semaphore(max(1, int(cfg.max_active)))
        self._agents: dict[str, LiveSubagent] = {}
        self._counter = 0
        self._closed = False

    # ── spawn ────────────────────────────────────────────────────────────
    async def spawn(self, spec: Any) -> str:
        """Fire one specialist in the background; return its handle (nickname)."""
        if self._closed:
            raise RuntimeError("subagent control is closed for this turn")
        nickname = self._next_nickname(getattr(spec, "role", "") or "specialist")
        child_control = ExecutionControl()
        # Seed ctx keeps the parent ``task_id`` (so the child links to it) but
        # swaps in a private ExecutionControl and clears any inherited control
        # plane so a specialist cannot re-enter this parent's registry.
        seed = dataclasses.replace(
            self._ctx,
            execution_control=child_control,
            subagent_control=None,
        )
        live = LiveSubagent(nickname=nickname, spec=spec, control=child_control)
        self._agents[nickname] = live
        live.task = asyncio.create_task(
            self._run_one(live, seed), name=f"subagent:{nickname}"
        )
        return nickname

    def _next_nickname(self, role: str) -> str:
        self._counter += 1
        base = re.sub(r"[^a-z0-9_-]+", "-", role.strip().lower()).strip("-") or "specialist"
        return f"{base}-{self._counter}"

    async def _run_one(self, live: LiveSubagent, seed: ExecContext) -> None:
        from omni.agent.subagents import run_subagent

        try:
            async with self._sem:
                result = await run_subagent(
                    live.spec, seed, settings=self._settings, depth=self._depth
                )
        except asyncio.CancelledError:
            live.status = "cancelled"
            live.final = self._envelope(live, None, status="cancelled", error="cancelled by coordinator")
            live.done_event.set()
            raise
        except Exception as exc:  # noqa: BLE001 - a crashed child must not kill the turn.
            live.status = "error"
            live.final = self._envelope(live, None, status="error", error=str(exc))
            live.done_event.set()
            return
        live.result = result
        live.status = "done" if result.status in _DONE_STATUSES else "error"
        live.final = self._envelope(live, result, status=result.status)
        live.done_event.set()

    def _envelope(
        self, live: LiveSubagent, result: Any, *, status: str, error: str = ""
    ) -> dict[str, Any]:
        return {
            "nickname": live.nickname,
            "role": getattr(live.spec, "role", "") or "specialist",
            "goal": _short(getattr(live.spec, "goal", "")),
            "status": status,
            "summary": (result.summary if result is not None else error),
            "review": (result.review if result is not None else None),
            "task_id": (result.task_id if result is not None else ""),
            "iterations": (result.iterations if result is not None else 0),
            "tool_calls": (result.tool_calls if result is not None else 0),
            "timed_out": False,
        }

    # ── wait ─────────────────────────────────────────────────────────────
    async def wait(self, nickname: str | None, timeout_s: float | None) -> dict[str, Any]:
        """Block until a subagent finishes (or ``timeout_s``); do not trigger a turn.

        With a ``nickname`` this waits for that one (idempotent — a completed
        subagent's result can be re-read). Without one it returns whichever
        subagent finishes first, preferring an already-finished-but-uncollected
        result before blocking, and never handing back the same one twice.
        """
        timeout = self._wait_timeout(timeout_s)
        if nickname:
            live = self._agents.get(nickname)
            if live is None:
                return {"error": f"unknown subagent {nickname!r}", "timed_out": False}
            env = await self._wait_one(live, timeout)
            if not env.get("timed_out"):
                live.collected = True
            return env
        ready = [live for live in self._agents.values() if live.final is not None and not live.collected]
        if ready:
            ready[0].collected = True
            return dict(ready[0].final)  # type: ignore[arg-type]
        pending = [live for live in self._agents.values() if live.final is None]
        if not pending:
            finished = [live for live in self._agents.values() if live.final is not None]
            if not finished:
                return {"error": "no subagents have been spawned", "timed_out": False}
            finished[-1].collected = True
            return dict(finished[-1].final)  # type: ignore[arg-type]
        env = await self._wait_any(pending, timeout)
        collected = self._agents.get(str(env.get("nickname") or ""))
        if collected is not None and not env.get("timed_out"):
            collected.collected = True
        return env

    def _wait_timeout(self, timeout_s: float | None) -> float:
        chosen = float(self._cfg.wait_default_s) if not timeout_s or timeout_s <= 0 else float(timeout_s)
        # Honor a short explicit timeout but never wait longer than a specialist
        # could possibly run (its own ``max_seconds`` bound).
        return max(0.1, min(chosen, float(self._cfg.max_seconds)))

    async def _wait_one(self, live: LiveSubagent, timeout: float) -> dict[str, Any]:
        if live.final is not None:
            return dict(live.final)
        try:
            await asyncio.wait_for(live.done_event.wait(), timeout)
        except TimeoutError:
            return {
                "nickname": live.nickname,
                "role": getattr(live.spec, "role", "") or "specialist",
                "goal": _short(getattr(live.spec, "goal", "")),
                "status": live.status,
                "summary": "",
                "timed_out": True,
            }
        return dict(live.final)  # type: ignore[arg-type]

    async def _wait_any(self, pending: list[LiveSubagent], timeout: float) -> dict[str, Any]:
        getters = [asyncio.ensure_future(live.done_event.wait()) for live in pending]
        try:
            done, still = await asyncio.wait(
                getters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for fut in getters:
                if not fut.done():
                    fut.cancel()
            await asyncio.gather(*getters, return_exceptions=True)
        if not done:
            return {"nickname": "", "status": "running", "summary": "", "timed_out": True}
        for live in pending:
            if live.final is not None:
                return dict(live.final)
        return {"nickname": "", "status": "running", "summary": "", "timed_out": True}

    # ── list / interrupt ──────────────────────────────────────────────────
    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "nickname": live.nickname,
                "role": getattr(live.spec, "role", "") or "specialist",
                "goal": _short(getattr(live.spec, "goal", "")),
                "status": live.status,
            }
            for live in self._agents.values()
        ]

    def interrupt(self, nickname: str) -> bool:
        """Cooperatively cancel one running subagent; it winds down and records itself."""
        live = self._agents.get(nickname)
        if live is None or live.final is not None:
            return False
        with contextlib.suppress(Exception):
            live.control.request_cancel()
        return True

    def message(self, nickname: str, text: str) -> bool:
        """Deliver an inter-agent message to a *running* subagent (Codex ``send_message``).

        Best-effort steering: the running specialist consumes it at its next
        safe boundary (it does not start a new turn). Returns ``False`` if the
        subagent is unknown or has already finished.
        """
        live = self._agents.get(nickname)
        if live is None or live.final is not None:
            return False
        with contextlib.suppress(Exception):
            live.control.push_steer(text)
        return True

    async def followup(self, nickname: str, text: str) -> str:
        """Continue a *finished* subagent (Codex ``followup_task``).

        Because a specialist is single-shot, a follow-up is a fresh subagent
        seeded with the previous result as context — a continuation. Returns the
        new handle. Raises ``ValueError`` if the subagent is unknown or still
        running (collect it with ``wait`` first).
        """
        from omni.agent.subagents import SubagentSpec

        live = self._agents.get(nickname)
        if live is None:
            raise ValueError(f"unknown subagent {nickname!r}")
        if live.final is None:
            raise ValueError(f"subagent {nickname!r} is still running; wait for it first")
        prev = live.spec
        prev_summary = str((live.final or {}).get("summary", ""))
        context_parts = [f"[Previous result]\n{prev_summary}"] if prev_summary else []
        if getattr(prev, "context", ""):
            context_parts.append(str(prev.context))
        return await self.spawn(
            SubagentSpec(
                goal=text,
                role=getattr(prev, "role", "specialist") or "specialist",
                context="\n\n".join(context_parts),
                tools=tuple(getattr(prev, "tools", ()) or ()),
                model=getattr(prev, "model", "") or "",
                compute_profile=getattr(prev, "compute_profile", "") or "",
                isolation=getattr(prev, "isolation", "") or "",
            )
        )

    # ── teardown ──────────────────────────────────────────────────────────
    async def aclose(self, grace_s: float = 2.0) -> None:
        """Join or cancel every spawned subagent so none outlives the turn."""
        self._closed = True
        tasks = [live.task for live in self._agents.values() if live.task is not None]
        running = [task for task in tasks if not task.done()]
        if running:
            _, pending = await asyncio.wait(running, timeout=max(0.0, grace_s))
            if pending:
                for live in self._agents.values():
                    if live.task is not None and not live.task.done():
                        with contextlib.suppress(Exception):
                            live.control.request_cancel()
                _, still = await asyncio.wait(pending, timeout=1.0)
                for task in still:
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["SubagentControl", "LiveSubagent"]
