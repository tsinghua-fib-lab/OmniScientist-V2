"""Shape rules for progress events crossing a workflow boundary.

A nested skill reports progress in its own frame; the workflow relays it in the
run's frame. Two rules govern that hand-off, and both were once inlined in the
relay where they were easy to get wrong: which fields the relay owns, and which
fields are durable.
"""

from __future__ import annotations

import inspect
from typing import Any

# Fields the relay re-stamps with the workflow's frame of reference. Everything
# else a child reports belongs to the child and is forwarded untouched —
# enumerating the forwarded fields instead is what once stripped a nested tool's
# name and outcome on the way to the terminal.
RELAY_OWNED_KEYS = frozenset(
    {
        "stage",
        "pct",
        "ts",
        "subtask_id",
        "skill",
        "step_id",
        "execution_id",
        "execution_progress",
    }
)

# A tool result can be an entire file, and the trace is rewritten onto the run
# row on every tick, so keeping results would re-serialise them once per
# subsequent tick. Renderers read the result off the live event; the durable
# trace keeps the identity and the outcome.
_UNTRACED_KEYS = frozenset({"result"})


async def emit(callback: Any, phase: str, data: dict[str, Any]) -> None:
    """Deliver an event to an optional sync or async listener."""
    if callback is None:
        return
    result = callback(phase, data)
    if inspect.isawaitable(result):
        await result


def relayed_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Child-reported fields that survive relay into the workflow frame."""
    return {key: value for key, value in data.items() if key not in RELAY_OWNED_KEYS}


def traceable(event: dict[str, Any]) -> dict[str, Any]:
    """The durable form of a progress event."""
    return {key: value for key, value in event.items() if key not in _UNTRACED_KEYS}


__all__ = ["RELAY_OWNED_KEYS", "emit", "relayed_fields", "traceable"]
