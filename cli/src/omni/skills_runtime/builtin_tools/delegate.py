"""``spawn_subagents`` — the coordinating agent's delegation tool.

Offered to a ReAct loop when multi-agent delegation is enabled and the current
context is below the nesting-depth limit. It lets the coordinator fan a batch of
focused subtasks out to isolated specialist sub-agents (optionally in parallel)
and receive back only their compact summaries — keeping the coordinator's own
context small on long-horizon research (read N papers / branch M hypotheses).
"""

from __future__ import annotations

from typing import Any

from omni.core.react_agent import ToolSpec
from omni.skills_runtime.context import ExecContext, Tool

_SPEC = ToolSpec(
    name="spawn_subagents",
    description=(
        "Delegate focused subtasks to isolated expert subagents for parallel execution. Each subagent "
        "has an independent context and tool budget and returns only a self-contained result summary. "
        "Use this for long-running fan-out such as reading several papers or testing hypothesis branches."
    ),
    parameters={
        "type": "object",
        "properties": {
            "subtasks": {
                "type": "array",
                "description": "Subtasks to delegate; each item requires a goal.",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "Subtask the subagent must complete independently."},
                        "role": {"type": "string", "description": "Optional role label such as reader or analyst."},
                        "context": {"type": "string", "description": "Optional background context."},
                        "tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional tool allowlist; defaults to read-only research tools.",
                        },
                        "model": {
                            "type": "string",
                            "description": "Optional model override; otherwise inherit the configured subagent or coordinator model.",
                        },
                        "compute_profile": {
                            "type": "string",
                            "description": "Optional trusted user-level compute profile name.",
                        },
                        "isolation": {
                            "type": "string",
                            "enum": ["none", "worktree", "container"],
                            "description": "Execution isolation: shared workspace, separate git worktree, or container.",
                        },
                    },
                    "required": ["goal"],
                },
            },
        },
        "required": ["subtasks"],
    },
)

# ── Async delegation (Codex V2 parity) ───────────────────────────────────────
# Offered alongside ``spawn_subagents`` only when ``settings.subagents.async_enabled``
# is on and a turn-scoped ``SubagentControl`` is attached. Fire-and-collect: spawn
# returns a handle immediately; the coordinator keeps working and collects later.

_SPAWN_ONE_SPEC = ToolSpec(
    name="spawn_subagent",
    description=(
        "Start ONE isolated specialist subagent in the background and return its handle "
        "immediately (do not block). Keep working, then call wait_subagent to collect its "
        "result. Prefer this over spawn_subagents when you want to overlap a specialist with "
        "your own tool calls or run several without waiting for each in turn."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "Subtask the subagent must complete independently."},
            "role": {"type": "string", "description": "Optional role label such as reader or analyst."},
            "context": {"type": "string", "description": "Optional background context."},
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tool allowlist; defaults to read-only research tools.",
            },
            "model": {"type": "string", "description": "Optional model override."},
            "compute_profile": {"type": "string", "description": "Optional trusted compute profile name."},
            "isolation": {
                "type": "string",
                "enum": ["none", "worktree", "container"],
                "description": "Execution isolation: shared workspace, separate git worktree, or container.",
            },
        },
        "required": ["goal"],
    },
)

_WAIT_SPEC = ToolSpec(
    name="wait_subagent",
    description=(
        "Block until a spawned subagent finishes (or the timeout elapses) and return its result "
        "summary. Omit 'nickname' to wait for whichever running subagent finishes first. This does "
        "not start a new turn — it only collects a result you asked for."
    ),
    parameters={
        "type": "object",
        "properties": {
            "nickname": {"type": "string", "description": "Handle from spawn_subagent; omit to wait for any."},
            "timeout_s": {"type": "number", "description": "Max seconds to wait; defaults to the configured wait timeout."},
        },
        "required": [],
    },
)

_LIST_SPEC = ToolSpec(
    name="list_subagents",
    description="List the subagents spawned this turn with their role, goal, and status.",
    parameters={"type": "object", "properties": {}, "required": []},
)

_INTERRUPT_SPEC = ToolSpec(
    name="interrupt_subagent",
    description=(
        "Request cancellation of one running subagent by its handle. It winds down at its next safe "
        "point and records itself as cancelled."
    ),
    parameters={
        "type": "object",
        "properties": {
            "nickname": {"type": "string", "description": "Handle from spawn_subagent."},
        },
        "required": ["nickname"],
    },
)

_MESSAGE_SPEC = ToolSpec(
    name="message_subagent",
    description=(
        "Send a steering message to a RUNNING subagent (e.g. refine its focus or add a constraint). "
        "The subagent picks it up at its next step; this does not start a new turn and does not block. "
        "Use interrupt_subagent to stop it, or wait_subagent to collect its result."
    ),
    parameters={
        "type": "object",
        "properties": {
            "nickname": {"type": "string", "description": "Handle from spawn_subagent."},
            "message": {"type": "string", "description": "Steering instruction for the running subagent."},
        },
        "required": ["nickname", "message"],
    },
)

_FOLLOWUP_SPEC = ToolSpec(
    name="followup_subagent",
    description=(
        "Continue a FINISHED subagent with a new instruction. Starts a fresh specialist seeded with the "
        "previous result as context and returns a new handle to wait on. Collect the prior result with "
        "wait_subagent before following up."
    ),
    parameters={
        "type": "object",
        "properties": {
            "nickname": {"type": "string", "description": "Handle of the finished subagent to continue."},
            "message": {"type": "string", "description": "Follow-up instruction for the continuation."},
        },
        "required": ["nickname", "message"],
    },
)


def build_delegation_tools(ctx: ExecContext) -> list[Tool]:
    """Return ``[spawn_subagents]`` when delegation is allowed for ``ctx``."""
    cfg = getattr(ctx.settings, "subagents", None)
    if cfg is None or not cfg.enabled or ctx.llm is None:
        return []
    depth = int(getattr(ctx, "subagent_depth", 0))
    if depth >= cfg.max_depth:
        return []

    async def handler(args: dict) -> Any:
        from omni.agent.subagents import SubagentSpec, run_subagents

        raw = args.get("subtasks") or []
        specs: list[SubagentSpec] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            goal = str(item.get("goal", "")).strip()
            if not goal:
                continue
            specs.append(SubagentSpec(
                goal=goal,
                role=str(item.get("role", "specialist")).strip() or "specialist",
                context=str(item.get("context", "")),
                tools=tuple(str(t) for t in (item.get("tools") or []) if str(t).strip()),
                model=str(item.get("model", "")).strip(),
                compute_profile=str(item.get("compute_profile", "")).strip(),
                isolation=str(item.get("isolation", "")).strip(),
            ))
        if not specs:
            return {"status": "error", "error": "no subtasks with a goal"}
        results = await run_subagents(specs, ctx, depth=depth)
        accepted = sum(1 for r in results if r.status in ("ok", "partial"))
        return {
            "status": "ok",
            "count": len(results),
            "accepted": accepted,
            "results": [r.to_dict() for r in results],
        }

    tools = [Tool(_SPEC, handler)]

    # Async delegation is a coordinator capability: it requires the feature flag
    # and a turn-scoped control plane. Specialists (which clear ``subagent_control``)
    # therefore keep only the blocking batch tool above.
    if getattr(cfg, "async_enabled", False) and getattr(ctx, "subagent_control", None) is not None:
        tools.extend(_build_async_tools(ctx))
    return tools


def _spec_from_args(args: dict) -> Any:
    from omni.agent.subagents import SubagentSpec

    return SubagentSpec(
        goal=str(args.get("goal", "")).strip(),
        role=str(args.get("role", "specialist")).strip() or "specialist",
        context=str(args.get("context", "")),
        tools=tuple(str(t) for t in (args.get("tools") or []) if str(t).strip()),
        model=str(args.get("model", "")).strip(),
        compute_profile=str(args.get("compute_profile", "")).strip(),
        isolation=str(args.get("isolation", "")).strip(),
    )


def _build_async_tools(ctx: ExecContext) -> list[Tool]:
    async def spawn_one(args: dict) -> Any:
        control = getattr(ctx, "subagent_control", None)
        if control is None:
            return {"status": "error", "error": "async subagents are not enabled for this turn"}
        if not str(args.get("goal", "")).strip():
            return {"status": "error", "error": "goal is required"}
        try:
            nickname = await control.spawn(_spec_from_args(args))
        except Exception as exc:  # noqa: BLE001 - surface spawn refusal to the model.
            return {"status": "error", "error": str(exc)}
        return {
            "status": "ok",
            "nickname": nickname,
            "note": "running in the background; call wait_subagent to collect its result.",
        }

    async def wait(args: dict) -> Any:
        control = getattr(ctx, "subagent_control", None)
        if control is None:
            return {"status": "error", "error": "async subagents are not enabled for this turn"}
        nickname = str(args.get("nickname", "")).strip() or None
        timeout = args.get("timeout_s")
        result = await control.wait(nickname, timeout if isinstance(timeout, (int, float)) else None)
        if "error" in result:
            return {"status": "error", "error": result["error"]}
        return {"status": "ok", "timed_out": bool(result.get("timed_out")), "subagent": result}

    async def list_agents(_args: dict) -> Any:
        control = getattr(ctx, "subagent_control", None)
        if control is None:
            return {"status": "error", "error": "async subagents are not enabled for this turn"}
        return {"status": "ok", "subagents": control.list()}

    async def interrupt(args: dict) -> Any:
        control = getattr(ctx, "subagent_control", None)
        if control is None:
            return {"status": "error", "error": "async subagents are not enabled for this turn"}
        nickname = str(args.get("nickname", "")).strip()
        if not nickname:
            return {"status": "error", "error": "nickname is required"}
        return {"status": "ok", "interrupted": control.interrupt(nickname)}

    async def message(args: dict) -> Any:
        control = getattr(ctx, "subagent_control", None)
        if control is None:
            return {"status": "error", "error": "async subagents are not enabled for this turn"}
        nickname = str(args.get("nickname", "")).strip()
        text = str(args.get("message", "")).strip()
        if not nickname or not text:
            return {"status": "error", "error": "nickname and message are required"}
        delivered = control.message(nickname, text)
        if not delivered:
            return {"status": "error", "error": f"subagent {nickname!r} is not running"}
        return {"status": "ok", "delivered": True}

    async def followup(args: dict) -> Any:
        control = getattr(ctx, "subagent_control", None)
        if control is None:
            return {"status": "error", "error": "async subagents are not enabled for this turn"}
        nickname = str(args.get("nickname", "")).strip()
        text = str(args.get("message", "")).strip()
        if not nickname or not text:
            return {"status": "error", "error": "nickname and message are required"}
        try:
            new_nickname = await control.followup(nickname, text)
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        return {
            "status": "ok",
            "nickname": new_nickname,
            "note": "continuation running in the background; call wait_subagent to collect it.",
        }

    return [
        Tool(_SPAWN_ONE_SPEC, spawn_one),
        Tool(_WAIT_SPEC, wait),
        Tool(_LIST_SPEC, list_agents),
        Tool(_INTERRUPT_SPEC, interrupt),
        Tool(_MESSAGE_SPEC, message),
        Tool(_FOLLOWUP_SPEC, followup),
    ]


__all__ = ["build_delegation_tools"]
