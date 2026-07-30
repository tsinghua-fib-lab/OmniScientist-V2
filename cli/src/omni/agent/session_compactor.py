"""Session transcript compaction.

Folds older turns into a single bridge summary once a session exceeds the
message-count guard or the model-window token budget, after flushing durable
facts so nothing is lost (Claude/Codex-style auto-compaction). A narrow
collaborator over the conversation store (transcript I/O), the memory service
(fact extraction), the LLM (summary), settings (window + autocompact pct), and
the task recorder (cost metering).
"""

from __future__ import annotations

import logging

from omni.agent.conversation_store import ConversationStore
from omni.agent.cost import record_text_cost_event
from omni.config.settings import OmniSettings
from omni.core.llm import LLMClient
from omni.memory.service import MemoryService
from omni.runtime.task_recorder import TaskRecorder

logger = logging.getLogger(__name__)

# Auto-compaction guardrails: fold older turns into a summary once a session's
# visible transcript exceeds either the message-count guard below or the
# model-window token budget (``token_budget``), keeping the most recent turns.
_COMPACT_THRESHOLD = 30  # visible user/assistant/tool-result messages
_COMPACT_KEEP_LAST = 8


class SessionCompactor:
    """Threshold-driven transcript folding with a fact flush."""

    def __init__(
        self,
        *,
        store: ConversationStore,
        memory: MemoryService,
        llm: LLMClient,
        settings: OmniSettings,
        tasks: TaskRecorder,
    ) -> None:
        self._store = store
        self._memory = memory
        self._llm = llm
        self._settings = settings
        self._tasks = tasks

    def token_budget(self) -> int:
        """Transcript token budget = model window × ``memory.autocompact_pct``
        (model-aware, Claude/Codex-style, rather than a fixed char budget)."""
        from omni.config.settings import resolve_context_window_tokens

        window = resolve_context_window_tokens(self._settings)
        pct = min(max(float(getattr(self._settings.memory, "autocompact_pct", 0.70) or 0.70), 0.1), 0.95)
        return max(1, int(window * pct))

    async def maybe_compact(self, session_id: str, *, task_id: str = "") -> None:
        from omni.memory.compaction import estimate_tokens

        rows = await self._store.visible_normal_messages(session_id)
        tokens = sum(estimate_tokens(r.content or "") for r in rows)
        # Two triggers: a message-count guard (many small turns) and the
        # model-window token budget (few but huge turns). Either folds.
        if len(rows) <= _COMPACT_THRESHOLD and tokens <= self.token_budget():
            return
        try:
            await self.compact(session_id, keep_last=_COMPACT_KEEP_LAST, task_id=task_id)
        except Exception:  # noqa: BLE001
            logger.debug("auto-compaction failed", exc_info=True)

    async def compact(
        self,
        session_id: str,
        *,
        keep_last: int = 8,
        task_id: str = "",
    ) -> dict[str, int]:
        """Flush durable facts, then fold older turns into one compaction summary."""
        from omni.memory.compaction import (
            estimate_messages_tokens,
            estimate_tokens,
            summarize_messages,
        )

        rows = await self._store.visible_normal_messages(session_id)
        before_tokens = estimate_messages_tokens(await self._store.history(session_id))
        if len(rows) <= keep_last + 1:
            return {
                "compacted": 0,
                "kept": len(rows),
                "before_tokens": before_tokens,
                "after_tokens": before_tokens,
                "saved_tokens": 0,
            }
        older = rows[:-keep_last]
        older_msgs = [
            {"role": r.role, "content": r.content, "meta": dict(r.meta or {})}
            for r in older
        ]

        async def meter_facts(system: str, user: str, output: str) -> None:
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

        # 1) flush durable facts BEFORE hiding the turns (never lose information).
        try:
            principal = await self._store.principal_for_session(session_id)
            await self._memory.extract_session(
                session_id,
                older_msgs,
                principal=principal,
                on_llm_call=meter_facts if task_id else None,
            )
        except Exception:  # noqa: BLE001
            logger.debug("pre-compaction flush failed", exc_info=True)
        # 2) build the bridge summary.
        async def meter(system: str, user: str, output: str) -> None:
            await record_text_cost_event(
                self._tasks,
                self._settings,
                self._llm,
                task_id,
                system=system,
                user_message=user,
                output=output,
                component="memory:compaction",
            )

        summary = await summarize_messages(
            self._llm,
            self._settings,
            older_msgs,
            on_llm_call=meter if task_id else None,
        )
        bridge_prefix = "[Earlier conversation summary]\n"
        kept_prompt = [
            {"role": row.role, "content": row.content}
            for row in rows[-keep_last:]
            if row.role in {"user", "assistant"}
        ]
        bridge_budget = max(0, before_tokens - estimate_messages_tokens(kept_prompt))
        while summary and estimate_tokens(bridge_prefix + summary) > bridge_budget:
            summary = summary[: max(0, int(len(summary) * 0.8))].rstrip()
        bridge = bridge_prefix + summary if summary else ""
        covered = [r.id for r in older]
        await self._store.write_compaction_bridge(session_id, bridge, covered)
        after_tokens = estimate_messages_tokens(await self._store.history(session_id))
        return {
            "compacted": len(covered),
            "kept": keep_last,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "saved_tokens": max(0, before_tokens - after_tokens),
        }
