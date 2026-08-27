"""Assemble the ReAct system prompt from already-computed turn blocks."""

from __future__ import annotations

from typing import Any

from omni.agent.intent_plan import IntentPlan
from omni.agent.persona_stoma import load_turn_persona_overlay
from omni.agent.plan_recovery import react_context_block
from omni.agent.plan_runner_utils import assumption_block
from omni.config.settings import resolve_max_input_tokens
from omni.core.system_prompt import build_system_prompt
from omni.memory.files import load_curated_memory
from omni.memory.notebook import read_recent
from omni.runtime.git_info import repository_history_block
from omni.runtime.unpayable import unpayable_notice_text
from omni.skills_runtime.context import ExecContext


async def assemble_react_system_prompt(
    agent: Any,
    *,
    ctx: ExecContext,
    plan: IntentPlan,
    tool_specs: list[Any],
    memory_block: str,
    research_brief: str,
    recovery_react_notes: list[str],
    context_summary: str,
    recent_activity: str,
    user_message: str,
) -> str:
    """Join observation blocks and render the model-visible system prompt."""
    skill_catalog = (
        agent.registry.react_skill_catalog(
            context_window_tokens=resolve_max_input_tokens(agent.settings)
        )
        if plan.context_policy.include_skill_catalog and agent.registry.list_all()
        else ""
    )
    referenced = (
        await agent._referenced_task_context(user_message)  # noqa: SLF001
        if plan.context_policy.include_referenced_tasks
        else ""
    )
    clarification_block = await agent._open_clarifications_block(ctx)  # noqa: SLF001
    return build_system_prompt(
        role=agent._role,  # noqa: SLF001
        tools=tool_specs,
        persona_overlay=load_turn_persona_overlay(agent.paths, channel=ctx.channel),
        memory_block="\n\n".join(
            block
            for block in (
                clarification_block,
                react_context_block(recovery_react_notes),
                assumption_block(plan.missing_inputs),
                _unpayable_block(plan),
                context_summary if plan.context_policy.include_referenced_tasks else "",
                referenced,
                (agent.pending_thread_brief or "").strip(),
                research_brief,
                (
                    agent._domain_pack_brief()  # noqa: SLF001
                    if plan.context_policy.include_research_brief
                    else ""
                ),
                memory_block,
                skill_catalog,
            )
            if block
        ),
        project_memory=load_curated_memory(agent.paths),
        recent_activity=(
            recent_activity if plan.context_policy.include_recent_activity else ""
        ),
        project_name=agent.paths.project_name,
        notebook_summary=read_recent(agent.paths.notebook, max_chars=800),
        working_dir=ctx.working_dir,
        repo_history=repository_history_block(ctx.working_dir, user_message),
    )


def _unpayable_block(plan: IntentPlan) -> str:
    """Tell ReAct which bound files have no producer this turn."""
    items = list(getattr(plan, "unpayable_outputs", None) or [])
    notice = unpayable_notice_text(items)
    if not notice:
        return ""
    return (
        "[Unpayable this turn] "
        + notice
        + " Do the work that still has a producer. Do not hunt the host ledger."
    )
