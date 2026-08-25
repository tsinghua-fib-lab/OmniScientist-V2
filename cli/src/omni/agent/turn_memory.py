"""Per-turn memory recording, periodic consolidation, and session-end maintenance.

The orchestrator runs the turn; this collaborator owns what the turn *remembers*:
it records a bounded dialogue memory each turn, distils durable facts every few
turns (and again at session end), and runs the P2 hygiene pass (decay, dedup,
profile + memory-file refresh) under a cross-process lock. A narrow collaborator
over the conversation store, the memory service, the LLM, settings, the task
recorder, and workspace paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from omni.agent.conversation_store import (
    PERSONA_CONTROL_EXTERNAL_KEY,
    ConversationStore,
)
from omni.agent.cost import record_text_cost_event
from omni.config.settings import OmniSettings
from omni.core.llm import LLMClient
from omni.core.react_agent import AgentLoopResult
from omni.memory.service import MemoryLayer, MemoryService
from omni.runtime.task_recorder import TaskRecorder

logger = logging.getLogger(__name__)

# Distil durable memory every N recorded turns (cheap offline; LLM only when a
# real provider is configured).
_CONSOLIDATE_EVERY = 8

# Owner / CLI identity for memory isolation (see MemoryService.PRINCIPAL_OWNER).
_PRINCIPAL_OWNER = "local"


class TurnMemory:
    """Turn recording, periodic consolidation, and end-of-session maintenance."""

    def __init__(
        self,
        *,
        store: ConversationStore,
        memory: MemoryService,
        llm: LLMClient,
        settings: OmniSettings,
        tasks: TaskRecorder,
        paths,  # noqa: ANN001 - OmniPaths (avoid a heavy import cycle)
    ) -> None:
        self._store = store
        self._memory = memory
        self._llm = llm
        self._settings = settings
        self._tasks = tasks
        self._paths = paths
        # Per-session turn counters + single-flight consolidation locks
        # (in-process; keeps session-end extraction from overlapping with a
        # threshold trigger).
        self._turn_counts: dict[str, int] = {}
        self._consolidation_locks: dict[str, asyncio.Lock] = {}

    async def record(
        self,
        session_id: str,
        user_message: str,
        result: AgentLoopResult,
        *,
        task_id: str = "",
    ) -> None:
        if str(user_message or "").lstrip().startswith("$soulagent "):
            row = await self._store.get_session(session_id)
            if row is not None and (row.external_key or "") == PERSONA_CONTROL_EXTERNAL_KEY:
                return
        principal = await self._store.principal_for_session(session_id)
        if principal is None:
            return
        tool_names = sorted({n for n in result.tool_names()}) if result.tool_trace else []
        # What was asked, attributed to the task that answered it — not what the
        # answer claimed. Recall is by similarity, so an entry surfaces with no
        # sense of when it was written: one entry in a single session, replayed
        # twenty-three times, announced that every task was complete and listed
        # the deliverables, and it was recalled on later requests that had
        # produced none of them. Codex's compaction keeps the user's messages and
        # drops assistant output for this reason. What a turn produced is carried
        # by the task and artifact memories instead, which name their own task,
        # and the recent transcript still holds the replies verbatim.
        summary = f"task {task_id[:8]} — " if task_id else ""
        summary += f"User: {user_message[:160]}"
        importance = 0.45 + (0.1 if tool_names else 0.0)
        try:
            await self._memory.record(
                layer=MemoryLayer.SESSION, scope="session", scope_id=session_id,
                summary=summary, memory_type="dialogue",
                tags=tool_names[:6], importance=min(importance, 0.6),
                principal=principal,
            )
        except Exception:  # noqa: BLE001
            pass
        # Periodically distil durable facts so a long session keeps "learning"
        # without waiting for an explicit end. Cheap offline (heuristic).
        self._turn_counts[session_id] = self._turn_counts.get(session_id, 0) + 1
        if self._turn_counts[session_id] % _CONSOLIDATE_EVERY == 0:
            try:
                await self.consolidate(session_id, task_id=task_id)
            except Exception:  # noqa: BLE001
                pass

    async def consolidate(self, session_id: str, *, task_id: str = "") -> list[str]:
        """Run session extraction under a per-session single-flight lock."""
        if not session_id:
            return []
        lock = self._consolidation_locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            return []
        async with lock:
            principal = await self._store.principal_for_session(session_id)
            if principal is None:
                return []
            messages = await self._store.extraction_history(session_id, limit=40)

            async def meter(system: str, user: str, output: str) -> None:
                await record_text_cost_event(
                    self._tasks,
                    self._settings,
                    self._llm,
                    task_id,
                    system=system,
                    user_message=user,
                    output=output,
                    component="memory:fact_extraction",
                )

            return await self._memory.extract_session(
                session_id,
                messages,
                principal=principal,
                on_llm_call=meter if task_id else None,
            )

    async def enqueue_session_maintenance(self, session_id: str) -> str:
        """Record that a session owes durable-memory maintenance, and return.

        The pass itself costs several model round trips under a cross-process
        lock. Charging that to whoever is trying to leave means it outlives any
        shutdown budget, gets cancelled halfway, and lands nothing — so session
        end only parks the work. :meth:`drain_pending_maintenance` runs it later,
        when nobody is waiting.
        """
        task_id = await self._create_maintenance_run(session_id)
        if task_id:
            await self._tasks.park_maintenance(task_id)
        return task_id

    async def _create_maintenance_run(self, session_id: str) -> str:
        """Open the run that owns one session's maintenance, queued or not."""
        try:
            maintenance = await self._tasks.create_task(
                session_id=session_id,
                channel="maintenance",
                user_input="session memory maintenance",
                title="Session memory maintenance",
                kind="maintenance",
                require_session=True,
            )
            await self._tasks.record_plan(
                maintenance.id,
                {
                    "intent_type": "maintenance",
                    "verification_plan": {
                        "required_events": ["maintenance.completed"],
                    },
                },
                status="validated",
            )
        except Exception:  # noqa: BLE001
            logger.debug("memory maintenance task creation failed", exc_info=True)
            return ""
        return maintenance.id

    async def drain_pending_maintenance(
        self, *, limit: int = 5, stale_after_s: float = 1800.0
    ) -> int:
        """Run the maintenance passes earlier sessions parked. Best-effort.

        Claiming is exclusive, so several windows draining at once is safe, and a
        pass that fails settles its own run rather than blocking the queue.
        """
        try:
            await self._tasks.settle_orphaned_maintenance(stale_after_s=stale_after_s)
        except Exception:  # noqa: BLE001
            logger.debug("settling orphaned memory maintenance failed", exc_info=True)
        claimed = await self._tasks.claim_pending_maintenance(limit=limit)
        for row in claimed:
            try:
                await self.run_session_maintenance(
                    row.session_id or "", maintenance_task_id=row.id
                )
            except Exception:  # noqa: BLE001
                logger.debug("queued memory maintenance failed", exc_info=True)
        return len(claimed)

    async def end_session(self, session_id: str) -> list[str]:
        """Run the maintenance a session owes, right now.

        For callers with nobody waiting on them, which want durable memory
        updated before they return. The run never enters the queue, so a drain
        elsewhere cannot pick up the same session in parallel. Interactive
        surfaces park instead — see :meth:`enqueue_session_maintenance`.
        """
        maintenance_task_id = await self._create_maintenance_run(session_id)
        return await self.run_session_maintenance(
            session_id, maintenance_task_id=maintenance_task_id
        )

    async def run_session_maintenance(
        self, session_id: str, *, maintenance_task_id: str = ""
    ) -> list[str]:
        """Consolidate + maintain durable memory for one parked session.

        Beyond extraction this runs the P2 hygiene pass: importance decay,
        near-duplicate merge, and a refreshed single-shot user profile — so the
        agent "gets to know you" while the long-term store stays bounded.

        The run is settled whatever happens, including cancellation: settlement
        used to sit after the expensive part, so a cut-off pass left its own run
        open in ``running`` forever.
        """
        errors: list[str] = []
        recorded: list[str] = []
        try:
            recorded = await self._consolidate_and_maintain(
                session_id, maintenance_task_id, errors
            )
        except BaseException as exc:
            # Settle as degraded rather than letting the run claim a success it
            # never reached; the queue can offer the session again later.
            errors.append(f"interrupted: {type(exc).__name__}")
            raise
        finally:
            if maintenance_task_id:
                # Shielded: when the pass is cancelled, the awaits that settle it
                # would be cancelled too, which is exactly how runs were orphaned.
                with contextlib.suppress(BaseException):
                    await asyncio.shield(
                        self._settle_maintenance(
                            maintenance_task_id, recorded=recorded, errors=errors
                        )
                    )
        return recorded

    async def _consolidate_and_maintain(
        self, session_id: str, maintenance_task_id: str, errors: list[str]
    ) -> list[str]:
        try:
            recorded = await self.consolidate(session_id, task_id=maintenance_task_id)
        except Exception as exc:  # noqa: BLE001
            recorded = []
            errors.append(f"fact extraction: {exc}")

        async def meter(component: str, system: str, user: str, output: str) -> None:
            await record_text_cost_event(
                self._tasks,
                self._settings,
                self._llm,
                maintenance_task_id,
                system=system,
                user_message=user,
                output=output,
                component=component,
            )

        async def meter_profile(system: str, user: str, output: str) -> None:
            await meter("memory:profile_merge", system, user, output)

        async def meter_file(system: str, user: str, output: str) -> None:
            await meter("memory:file_compaction", system, user, output)

        try:
            principal = await self._store.principal_for_session(session_id)
            # Serialize the heavy read-modify-write consolidation across processes
            # (WAL + lock). Non-blocking: if another process holds it we skip —
            # maintenance is best-effort and will run again next session end.
            from omni.memory.locks import global_memory_lock

            async with global_memory_lock(self._paths) as held:
                if held:
                    await self._memory.decay_and_dedup()
                    if principal:
                        await self._memory.rebuild_user_profile(
                            principal=principal,
                            on_llm_call=meter_profile if maintenance_task_id else None,
                        )
                    if principal == _PRINCIPAL_OWNER:
                        await self._memory.compact_memory_file(
                            on_llm_call=meter_file if maintenance_task_id else None,
                        )
                        await self._memory.refresh_global_summary(self._paths)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"memory hygiene: {exc}")
            logger.debug("memory maintenance failed", exc_info=True)
        return recorded

    async def _settle_maintenance(
        self, maintenance_task_id: str, *, recorded: list[str], errors: list[str]
    ) -> None:
        """Give the maintenance run a terminal status and say what it achieved."""
        status = "degraded" if errors else "succeeded"
        summary = (
            f"session memory maintenance completed; recorded={len(recorded)}"
            if not errors
            else f"session memory maintenance completed with {len(errors)} warning(s)"
        )
        try:
            await self._tasks.append_event(
                maintenance_task_id,
                event_type="maintenance.completed",
                status=status,
                name="memory",
                output_json={"recorded": len(recorded), "warnings": errors},
                summary=summary,
            )
            await self._tasks.finish_task(
                maintenance_task_id,
                status=status,
                summary=summary,
                error="; ".join(errors),
            )
        except Exception:  # noqa: BLE001
            logger.debug("memory maintenance task settlement failed", exc_info=True)
