"""``update_plan`` — the model's own task checklist.

Codex parity (`codex-rs/protocol/src/plan_tool.rs` + `tools/handlers/plan.rs`):
the plan is **not** a pre-execution contract the host computes and then enforces.
It is a tool the *model* calls mid-turn to publish a short checklist, which the
harness renders. The model owns the steps and their order; the host owns only
the rendering.

That inversion is the point. A plan produced before the first tool call has to
be validated, repaired and recovered when reality disagrees with it; a plan the
model rewrites whenever it learns something new needs none of that machinery —
the next ``update_plan`` call *is* the repair.

The handler is deliberately inert: it normalizes, then hands the checklist back
for display. It touches no store, so it works on every surface (CLI, IM,
headless) and in offline tests.
"""

from __future__ import annotations

from typing import Any

from omni.core.react_agent import ToolSpec
from omni.skills_runtime.context import ExecContext, Tool

# Codex's three states, verbatim: anything else normalizes to ``pending`` rather
# than being rejected — a mis-spelled status must not cost the model a turn.
_STATES = ("pending", "in_progress", "completed")

_DESCRIPTION = (
    "Publish or update a short checklist of the steps you intend to take for a "
    "multi-step task, and keep it current as you work. Call it once early with "
    "the full list, then again to mark progress. Keep exactly one step "
    "in_progress. Skip it for trivial single-step requests."
)

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "explanation": {
            "type": "string",
            "description": "Optional one-line reason for this plan revision.",
        },
        "plan": {
            "type": "array",
            "description": "The complete checklist, in order. Replaces any previous plan.",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string", "description": "Short imperative description"},
                    "status": {"type": "string", "enum": list(_STATES)},
                },
                "required": ["step", "status"],
            },
        },
    },
    "required": ["plan"],
}


def normalize_plan(raw: Any) -> list[dict[str, str]]:
    """Project arbitrary model output to ``[{step, status}]`` (never raises)."""
    steps: list[dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, str):
            text, status = item, "pending"
        elif isinstance(item, dict):
            text = str(item.get("step") or item.get("description") or "").strip()
            status = str(item.get("status") or "pending").strip().lower()
        else:
            continue
        text = " ".join(str(text).split())
        if not text:
            continue
        steps.append({"step": text, "status": status if status in _STATES else "pending"})
    return steps


def build_plan_tools(ctx: ExecContext) -> list[Tool]:
    """Build the model-facing plan checklist tool bound to ``ctx``."""

    async def update_plan(args: dict) -> Any:
        steps = normalize_plan(args.get("plan"))
        if not steps:
            return {"status": "error", "error": "plan must contain at least one step"}
        explanation = " ".join(str(args.get("explanation") or "").split())
        return {
            "status": "ok",
            "plan": steps,
            "explanation": explanation,
            # Codex answers the model with a bare acknowledgement: the checklist
            # is already in its own message, so echoing it back wastes context.
            "note": "Plan updated.",
        }

    return [Tool(ToolSpec("update_plan", _DESCRIPTION, _PARAMETERS), update_plan)]


__all__ = ["build_plan_tools", "normalize_plan"]
