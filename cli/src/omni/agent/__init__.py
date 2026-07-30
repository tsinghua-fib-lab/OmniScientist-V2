"""Agent orchestration — the glue that wires core/skills/memory/runtime."""

from __future__ import annotations

__all__ = ["OmniAgent", "TurnResult"]


def __getattr__(name: str):  # noqa: ANN202
    """Load the public orchestrator lazily to keep runtime helpers acyclic."""
    if name in __all__:
        from omni.agent.orchestrator import OmniAgent, TurnResult

        return {"OmniAgent": OmniAgent, "TurnResult": TurnResult}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
