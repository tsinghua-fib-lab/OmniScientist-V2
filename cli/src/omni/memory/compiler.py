"""Compile scoped memory into a small turn context.

The memory service stores many layers; the agent should not inject all matching
history into every prompt. ``MemoryCompiler`` selects layers based on the
current IntentPlan and renders a compact, budgeted block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.memory.service import MemoryLayer, MemoryService, ScoredMemory

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(slots=True)
class CompiledMemoryContext:
    text: str = ""
    selected_memory_ids: list[str] = field(default_factory=list)
    omitted_count: int = 0
    layers: list[str] = field(default_factory=list)
    budget: int = 0


class MemoryCompiler:
    """Plan-aware memory recall and rendering."""

    def __init__(self, memory: MemoryService) -> None:
        self._memory = memory

    async def compile_for_planning(
        self,
        *,
        query: str,
        session_id: str = "",
        token_budget: int = 320,
        principal: str = "local",
    ) -> CompiledMemoryContext:
        """Small context slice available before intent planning.

        This is deliberately narrower than plan-aware recall: it lets the
        planner resolve "this paper/result" from current/session/project memory
        without making low-confidence turns heavy or globally noisy.
        """
        if token_budget <= 0:
            return CompiledMemoryContext()
        layers = [MemoryLayer.SESSION.value, MemoryLayer.SEMANTIC.value, MemoryLayer.ARTIFACT.value]
        limit = _limit_for_budget(token_budget)
        memories = await self._memory.recall_scoped(
            query,
            session_id=session_id,
            layers=layers,
            limit=limit,
            candidate_limit=max(24, limit * 10),
            principal=principal,
        )
        text = _render(memories, role="planner", budget_chars=max(160, token_budget * 4))
        return CompiledMemoryContext(
            text=text,
            selected_memory_ids=[sm.entry.id for sm in memories if sm.entry.id],
            omitted_count=max(0, len(memories) - len(_rendered_entries(text))),
            layers=layers,
            budget=token_budget,
        )

    async def compile_for_turn(
        self,
        plan: IntentPlan,
        *,
        query: str,
        session_id: str = "",
        subtask_id: str = "",
        role: str = "assistant",
        token_budget: int = 600,
        principal: str = "local",
    ) -> CompiledMemoryContext:
        if not plan.context_policy.include_memory or token_budget <= 0:
            return CompiledMemoryContext()
        layers = _layers_for_intent(plan.intent_type)
        limit = _limit_for_budget(token_budget)
        memories = await self._memory.recall_scoped(
            query,
            session_id=session_id,
            subtask_id=subtask_id,
            layers=layers,
            limit=limit,
            candidate_limit=max(40, limit * 16),
            principal=principal,
        )
        text = _render(memories, role=role, budget_chars=max(160, token_budget * 4))
        return CompiledMemoryContext(
            text=text,
            selected_memory_ids=[sm.entry.id for sm in memories if sm.entry.id],
            omitted_count=max(0, len(memories) - len(_rendered_entries(text))),
            layers=layers,
            budget=token_budget,
        )


def _layers_for_intent(intent_type: IntentType) -> list[str]:
    if intent_type == IntentType.WORKFLOW:
        return [
            MemoryLayer.TASK.value,
            MemoryLayer.EPISODIC.value,
            MemoryLayer.SEMANTIC.value,
            MemoryLayer.ARTIFACT.value,
        ]
    if intent_type in {
        IntentType.QA_PLUS_ARTIFACT,
        IntentType.SINGLE_SKILL_TASK,
    }:
        return [
            MemoryLayer.SESSION.value,
            MemoryLayer.TASK.value,
            MemoryLayer.EPISODIC.value,
            MemoryLayer.SEMANTIC.value,
            MemoryLayer.ARTIFACT.value,
        ]
    if intent_type == IntentType.DIRECT_ANSWER:
        return [MemoryLayer.SESSION.value, MemoryLayer.SEMANTIC.value]
    return [MemoryLayer.SESSION.value, MemoryLayer.SEMANTIC.value, MemoryLayer.ARTIFACT.value]


def _limit_for_budget(token_budget: int) -> int:
    if token_budget <= 160:
        return 4
    if token_budget <= 420:
        return 6
    return 10


def _render(memories: Sequence[ScoredMemory], *, role: str, budget_chars: int) -> str:
    if not memories:
        return ""
    header = f"Compiled memory (role={role}; scoped and budgeted; advisory only):"
    lines = [header]
    used = len(header)
    for sm in memories:
        entry = sm.entry
        summary = " ".join(str(entry.summary or "").split())
        if not summary:
            continue
        scope = entry.scope
        if scope == "task" and entry.scope_id:
            scope = f"task {str(entry.scope_id)[:8]}"
        tag = f"[{entry.layer}/{entry.memory_type}/{scope}]"
        line = f"- {tag} {summary[:220]}"
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


def _rendered_entries(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]
