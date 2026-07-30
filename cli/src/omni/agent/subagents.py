"""Subagent delegation runtime — the *specialist* layer of the multi-agent stack.

A coordinating ReAct loop delegates focused subtasks to **specialist**
sub-agents. Each specialist:

* runs its **own** ReAct loop with an **isolated context** — a cloned
  :class:`ExecContext` with a fresh ``task_id`` and **no shared message history**
  (it never sees the coordinator's transcript or its siblings'),
* has its **own bounded tool budget** (``settings.subagents.*``),
* can run **in parallel** with its siblings (bounded by ``concurrency``),
* hands back a **compact summary** (its final answer) — the transcript and raw
  tool observations are intentionally dropped so the coordinator's context stays
  small (Claude-Science's coordinating→specialist→reviewer pattern).

An optional :mod:`reviewer <omni.agent.reviewer>` gate (LLM-as-judge) scores each
specialist output and can request one bounded revision.

Nesting is depth-bounded (``max_depth``): the delegation tool is only offered to
a context below the limit, so specialists can fan out one more level but never
recurse without end.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from omni.agent.cost import react_usage_limits, usage_budget_exhausted
from omni.agent.reviewer import gate, review_output
from omni.config.settings import SubagentsCfg
from omni.core.execution_budget import ToolExecutionBudget
from omni.core.llm import create_llm_client
from omni.core.termination import execution_outcome_status
from omni.core.tool_result import tool_event_suffix, tool_transport_status
from omni.runtime.execution_policy import skill_requires_approval
from omni.runtime.isolation import IsolationError, prepare_subagent_context
from omni.runtime.tool_gateway import ToolGateway
from omni.skills_runtime.context import ExecContext

# Privileged, side-effecting tools a specialist does NOT get by default: a
# read-only-ish coordinator must not silently gain write/exec via fan-out. A
# spec may re-grant them explicitly through ``SubagentSpec.tools``.
_MUTATION_TOOLS = frozenset({"write_file", "edit_file", "bash", "run_compute"})
_MAX_SUMMARY_CHARS = 6000


@dataclass(slots=True)
class SubagentSpec:
    """A focused subtask handed to one specialist sub-agent."""

    goal: str
    role: str = "specialist"
    context: str = ""
    # Optional allow-list of tool names. When set, it is the exact tool surface
    # (and may re-include mutation tools); when empty, the specialist gets the
    # builtin/research surface minus mutation tools.
    tools: tuple[str, ...] = ()
    model: str = ""
    compute_profile: str = ""
    isolation: str = ""  # none | worktree | container


@dataclass(slots=True)
class SubagentResult:
    """The compact hand-back from one specialist."""

    role: str
    goal: str
    status: str          # ok | partial | error | rejected | escalated
    summary: str
    tools_used: list[str] = field(default_factory=list)
    iterations: int = 0
    tool_calls: int = 0
    depth: int = 1
    review: dict[str, Any] | None = None
    model: str = ""
    compute_profile: str = ""
    isolation: str = "none"
    working_dir: str = ""
    task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "goal": self.goal,
            "status": self.status,
            "summary": self.summary,
            "tools_used": self.tools_used,
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "depth": self.depth,
            "model": self.model,
            "compute_profile": self.compute_profile,
            "isolation": self.isolation,
            "working_dir": self.working_dir,
            "task_id": self.task_id,
        }
        if self.review is not None:
            out["review"] = self.review
        return out


def _child_context(ctx: ExecContext, *, depth: int) -> ExecContext:
    """Clone ``ctx`` into an isolated child at ``depth + 1``.

    A fresh ``task_id`` keeps the specialist's events/artifacts distinct; the
    dynamic ``subagent_depth`` attribute drives the delegation-tool depth gate so
    nesting stays bounded. Memory ``principal`` is inherited so isolation between
    IM peers is preserved across delegation.
    """
    base = ctx.task_id or "run"
    child = dataclasses.replace(
        ctx,
        task_id=f"{base}::sub-{uuid.uuid4().hex[:8]}",
        file_uris=list(ctx.file_uris),
        # A specialist must never inherit the coordinator's async control plane,
        # or it could re-enter the parent's registry / leak tasks across depths.
        subagent_control=None,
    )
    child.subagent_depth = depth + 1  # type: ignore[attr-defined]
    return child


def _specialist_tools(
    child_ctx: ExecContext, allowed: Sequence[str], *, isolation: str = "none"
) -> list[Any]:
    """Build a specialist's tool surface (builtin + research + sync skills)."""
    from omni.skills_runtime.builtin_tools import build_builtin_tools

    tools = list(build_builtin_tools(child_ctx))
    registry = getattr(child_ctx, "registry", None)
    # Python/CLI skill providers execute in the host process. Container mode
    # therefore exposes only builtin/research adapters plus ``run_compute``;
    # otherwise an explicit skill allow-list could silently escape the container.
    if registry is not None and isolation != "container":
        from omni.agent.plan_revision import (
            provider_authority_error,
            specialist_skill_entries,
        )
        from omni.core.react_agent import ToolSpec
        from omni.skills_runtime.context import Tool
        from omni.skills_runtime.executor import _make_skill_handler
        from omni.skills_runtime.manifest import SkillKind

        existing = {t.spec.name for t in tools}
        authority = getattr(child_ctx, "provider_authority", None)
        delegated = (
            authority.get("delegated_provider_authorities")
            if isinstance(authority, dict)
            else None
        )
        expected_by_provider = {
            (
                str(item.get("provider_name") or ""),
                str(item.get("provider_source") or ""),
            ): item
            for item in (delegated or [])
            if isinstance(item, dict)
        }
        provider_step = {
            "input": {
                "tools": list(allowed),
                "isolation": isolation,
            }
        }
        for sk in specialist_skill_entries(registry, provider_step):
            if sk.kind in (SkillKind.PYTHON_ENGINE, SkillKind.CLI_EXEC) and sk.name not in existing:
                if delegated is not None:
                    expected = expected_by_provider.get((sk.name, sk.source))
                    error = (
                        provider_authority_error(sk, expected)
                        if expected is not None
                        else (
                            "provider execution authority is missing for "
                            f"delegated skill '{sk.name}'"
                        )
                    )
                    if error:
                        raise RuntimeError(error)
                tools.append(Tool(
                    ToolSpec(
                        sk.name,
                        sk.short_desc(200),
                        sk.input_schema,
                        replay_safe=sk.replay_safe,
                    ),
                    _make_skill_handler(sk, child_ctx),
                    sensitive=skill_requires_approval(sk),
                    input_schema=sk.input_schema,
                    output_schema=sk.output_schema,
                    replay_safe=sk.replay_safe,
                ))
    if allowed:
        allow = set(allowed)
        if isolation == "container":
            allow -= _MUTATION_TOOLS - {"run_compute"}
        return [t for t in tools if t.spec.name in allow]
    blocked = set(_MUTATION_TOOLS)
    if isolation == "container":
        blocked.discard("run_compute")
    return [t for t in tools if t.spec.name not in blocked]


def _specialist_system(role: str, spec: SubagentSpec) -> str:
    base = role or "You are a rigorous research assistant."
    return (
        base
        + f"\n\n[Subagent role] You are the coordinator's {spec.role or 'specialist'} subagent. "
        + "Complete only the assigned subtask and return a self-contained final answer. "
        + "The coordinator sees only the final answer, so it must be complete, directly usable, and evidence-grounded."
    )


def _summary_of(content: str) -> str:
    text = (content or "").strip()
    if len(text) > _MAX_SUMMARY_CHARS:
        return text[:_MAX_SUMMARY_CHARS].rstrip() + "... (truncated)"
    return text


def _status_of(kind: str, reason: str = "") -> str:
    if kind == "escalated":
        return "escalated"
    outcome = execution_outcome_status(kind, reason)
    return {"succeeded": "ok", "degraded": "partial", "failed": "error"}[outcome]


async def _record_reviewer_signal(
    ctx: ExecContext, spec: SubagentSpec, verdict: Any, action: str
) -> None:
    """Persist a reviewer verdict as a ``reviewer.<verdict>`` event on the parent run.

    Reviewer verdicts are otherwise ephemeral (only embedded in the coordinator's
    tool result). Recording them on the *parent* run makes them a durable, first
    class signal the self-evolution loop can aggregate (see
    :mod:`omni.skills_runtime.signals`). Best-effort — never blocks the specialist.
    """
    db = getattr(ctx, "db", None)
    task_id = getattr(ctx, "task_id", "") or ""
    if db is None or not task_id:
        return
    verdict_name = str(getattr(verdict, "verdict", "") or "").strip().lower() or "pass"
    try:
        from omni.runtime.task_recorder import TaskRecorder

        recorder = TaskRecorder(db, project=getattr(ctx, "project", "default") or "default")
        await recorder.append_event(
            task_id,
            event_type=f"reviewer.{verdict_name}",
            status="succeeded",
            name="reviewer",
            output_json={
                "score": round(float(getattr(verdict, "score", 0.0) or 0.0), 3),
                "action": action,
                "role": spec.role,
                "goal": spec.goal[:200],
                "notes": str(getattr(verdict, "notes", "") or "")[:400],
            },
            summary=f"reviewer {verdict_name} action={action}",
        )
    except Exception:  # noqa: BLE001 — signal recording is best-effort, never fatal.
        pass


async def _run_once(
    react: Any,
    *,
    system: str,
    user: str,
    specs: list[Any],
    on_tool_event: Any = None,
    execution_control: Any = None,
) -> Any:
    # Propagate the parent's ExecutionControl so a user cancel/steer reaches the
    # child loop through the one shared, nestable instance (invariant: nested
    # loops must not spin up an empty second control owner).
    return await react.run(
        system_prompt=system,
        user_message=user,
        tools=specs,
        on_tool_event=on_tool_event,
        execution_control=execution_control,
    )


async def _start_child_run(ctx: ExecContext, spec: SubagentSpec, depth: int) -> tuple[Any, str]:
    db = getattr(ctx, "db", None)
    parent_task_id = str(getattr(ctx, "task_id", "") or "")
    if db is None or not parent_task_id:
        return None, ""
    from omni.runtime.task_recorder import TaskRecorder

    recorder = TaskRecorder(db, project=getattr(ctx, "project", "default") or "default")
    row = await recorder.create_task(
        session_id=ctx.session_id,
        channel=ctx.channel,
        user_input=spec.goal,
        title=f"{spec.role or 'specialist'}: {spec.goal[:80]}",
        parent_task_id=parent_task_id,
        kind="subagent",
        depth=depth + 1,
        origin_workflow_run_id=str(getattr(ctx, "workflow_run_id", "") or ""),
        origin_workflow_step_id=str(getattr(ctx, "workflow_step_id", "") or ""),
    )
    await recorder.append_event(
        row.id,
        event_type="subagent.started",
        status="running",
        name=spec.role or "specialist",
        input_json={"goal": spec.goal, "context": spec.context, "depth": depth + 1},
        summary=f"subagent {spec.role or 'specialist'} started",
    )
    return recorder, row.id


async def _finish_child_run(
    recorder: Any,
    task_id: str,
    *,
    status: str,
    summary: str,
    error: str = "",
) -> None:
    if recorder is None or not task_id:
        return
    await recorder.append_event(
        task_id,
        event_type="subagent.finished",
        status=status,
        name="subagent",
        output_json={"status": status, "summary": summary},
        error=error,
        summary=summary,
    )
    await recorder.settle_task(
        task_id,
        proposed_status=status,
        summary=summary,
        error=error,
    )


async def _record_subagent_loop_cost(
    recorder: Any,
    settings: Any,
    llm: Any,
    task_id: str,
    result: Any,
    *,
    system: str,
    user: str,
    component: str,
) -> None:
    if recorder is None or not task_id:
        return
    from omni.agent.cost import record_cost_event

    await record_cost_event(
        recorder,
        settings,
        llm,
        task_id,
        result,
        system=system,
        user_message=user,
        component=component,
    )


async def _record_subagent_review_cost(
    recorder: Any,
    settings: Any,
    llm: Any,
    task_id: str,
    verdict: Any,
    *,
    component: str,
) -> None:
    output = str(getattr(verdict, "metering_output", "") or "")
    if recorder is None or not task_id or not output:
        return
    from omni.agent.cost import record_text_cost_event

    await record_text_cost_event(
        recorder,
        settings,
        llm,
        task_id,
        system="subagent reviewer",
        user_message=str(getattr(verdict, "metering_input", "") or ""),
        output=output,
        component=component,
    )


async def run_subagent(
    spec: SubagentSpec,
    ctx: ExecContext,
    *,
    settings: Any = None,
    depth: int = 0,
) -> SubagentResult:
    """Run one specialist in an isolated ReAct loop and hand back a summary."""
    effective_settings = settings or ctx.settings
    cfg: SubagentsCfg = effective_settings.subagents
    child = _child_context(ctx, depth=depth)
    child_recorder, child_task_id = await _start_child_run(ctx, spec, depth)
    if child_task_id:
        child.task_id = child_task_id
    child_settings = effective_settings.model_copy(deep=True)
    model = (spec.model or cfg.default_model or "").strip()
    compute_profile = (spec.compute_profile or cfg.default_compute_profile or "").strip()
    isolation = (spec.isolation or cfg.default_isolation or "none").strip().lower()
    if model:
        child_settings.model.model = model
        child.llm = create_llm_client(child_settings)
    child.settings = child_settings
    try:
        child = await prepare_subagent_context(
            child,
            mode=isolation,
            compute_profile=compute_profile,
        )
    except IsolationError as exc:
        await _finish_child_run(
            child_recorder,
            child_task_id,
            status="failed",
            summary=str(exc),
            error=str(exc),
        )
        return SubagentResult(
            spec.role,
            spec.goal,
            "error",
            str(exc),
            depth=depth + 1,
            model=model or str(getattr(child.llm, "model", "") or ""),
            compute_profile=compute_profile,
            isolation=isolation,
            task_id=child_task_id,
        )
    if child.llm is None:
        await _finish_child_run(
            child_recorder,
            child_task_id,
            status="failed",
            summary="no LLM configured for subagent",
            error="no LLM configured for subagent",
        )
        return SubagentResult(spec.role, spec.goal, "error",
                              "no LLM configured for subagent", depth=depth + 1,
                              task_id=child_task_id)

    from omni.core.react_agent import ReActLoopAgent

    tools = _specialist_tools(child, spec.tools, isolation=isolation)
    tool_specs = [t.spec for t in tools]

    async def on_tool_event(phase: str, data: dict[str, Any]) -> None:
        if child_recorder is None or not child_task_id:
            return
        name = str(data.get("name") or "")
        if not name:
            return
        error = str(data.get("error") or "")
        status = tool_transport_status(data.get("status"), error)
        event_suffix = tool_event_suffix(status)
        await child_recorder.append_event(
            child_task_id,
            event_type=(
                "subagent.tool.start"
                if phase == "start" else f"subagent.tool.{event_suffix}"
            ),
            status="running" if phase == "start" else status,
            name=name,
            tool_name=name,
            input_json=data.get("arguments") or {},
            output_json=data.get("result") or {},
            error=error,
            duration_ms=float(data.get("duration_ms") or 0.0),
        )

    gateway = ToolGateway.from_context(
        child,
        event_family="subagent",
        tools=tools,
    )
    invoker = gateway.invoker()

    subagent_tool_budget = ToolExecutionBudget(cfg.max_tool_calls)
    react = ReActLoopAgent(
        child.llm, invoker,
        max_iterations=cfg.max_iterations,
        max_tool_calls=cfg.max_tool_calls,
        max_seconds=cfg.max_seconds,
        shared_tool_budget=subagent_tool_budget,
        **react_usage_limits(child_settings, child.llm),
    )
    system = _specialist_system(ctx.settings.role, spec)
    user = spec.goal if not spec.context else f"{spec.goal}\n\n[Context]\n{spec.context}"

    try:
        result = await _run_once(
            react,
            system=system,
            user=user,
            specs=tool_specs,
            on_tool_event=on_tool_event,
            execution_control=getattr(child, "execution_control", None),
        )
    except asyncio.CancelledError:
        await _finish_child_run(
            child_recorder,
            child_task_id,
            status="cancelled",
            summary="subagent cancelled by user",
        )
        raise
    await _record_subagent_loop_cost(
        child_recorder,
        child_settings,
        child.llm,
        child_task_id,
        result,
        system=system,
        user=user,
        component="subagent.initial",
    )
    summary = _summary_of(result.content)
    status = _status_of(result.kind, result.terminated_reason)

    review_dict: dict[str, Any] | None = None
    if (
        cfg.reviewer_enabled
        and status in ("ok", "partial")
        and child.llm is not None
        and not usage_budget_exhausted(result)
    ):
        verdict = await review_output(child.llm, goal=spec.goal, output=summary)
        await _record_subagent_review_cost(
            child_recorder,
            child_settings,
            child.llm,
            child_task_id,
            verdict,
            component="reviewer.subagent.1",
        )
        action = gate(verdict, min_score=cfg.reviewer_min_score)
        revises = 0
        while action == "revise" and revises < cfg.reviewer_max_revises:
            revises += 1
            revise_user = (
                f"{user}\n\n[Review feedback] {verdict.notes}\n"
                "Revise the answer to be more complete and evidence-grounded."
            )
            result = await _run_once(
                react,
                system=system,
                user=revise_user,
                specs=tool_specs,
                on_tool_event=on_tool_event,
                execution_control=getattr(child, "execution_control", None),
            )
            await _record_subagent_loop_cost(
                child_recorder,
                child_settings,
                child.llm,
                child_task_id,
                result,
                system=system,
                user=revise_user,
                component=f"subagent.revision.{revises}",
            )
            summary = _summary_of(result.content)
            status = _status_of(result.kind, result.terminated_reason)
            if usage_budget_exhausted(result):
                break
            verdict = await review_output(child.llm, goal=spec.goal, output=summary)
            await _record_subagent_review_cost(
                child_recorder,
                child_settings,
                child.llm,
                child_task_id,
                verdict,
                component=f"reviewer.subagent.{revises + 1}",
            )
            action = gate(verdict, min_score=cfg.reviewer_min_score)
        review_dict = verdict.to_dict()
        # Reviewer verdicts are parent-level quality signals.  The child run
        # keeps its own execution trace, while the coordinator aggregates the
        # verdict for learning and operational review.
        await _record_reviewer_signal(ctx, spec, verdict, action)
        if action == "reject":
            status = "rejected"

    terminal_status = (
        "succeeded" if status == "ok" else "degraded" if status == "partial" else "failed"
    )
    await _finish_child_run(
        child_recorder,
        child_task_id,
        status=terminal_status,
        summary=summary[:500],
        error=summary[:500] if terminal_status == "failed" else "",
    )
    return SubagentResult(
        role=spec.role,
        goal=spec.goal,
        status=status,
        summary=summary,
        tools_used=result.tool_names(),
        iterations=result.total_iterations,
        tool_calls=result.total_tool_calls,
        depth=depth + 1,
        review=review_dict,
        model=model or str(getattr(child.llm, "model", "") or ""),
        compute_profile=compute_profile,
        isolation=isolation,
        working_dir=str(child.working_dir or ""),
        task_id=child_task_id,
    )


async def run_subagents(
    specs: Sequence[SubagentSpec],
    ctx: ExecContext,
    *,
    settings: Any = None,
    depth: int = 0,
) -> list[SubagentResult]:
    """Run several specialists (bounded parallelism), preserving request order."""
    cfg: SubagentsCfg = (settings or ctx.settings).subagents
    chosen = list(specs)[: max(1, cfg.max_subagents)]
    if not chosen:
        return []
    sem = asyncio.Semaphore(max(1, cfg.concurrency))

    async def _one(s: SubagentSpec) -> SubagentResult:
        async with sem:
            return await run_subagent(s, ctx, settings=settings, depth=depth)

    return list(await asyncio.gather(*(_one(s) for s in chosen)))


__all__ = ["SubagentSpec", "SubagentResult", "run_subagent", "run_subagents"]
