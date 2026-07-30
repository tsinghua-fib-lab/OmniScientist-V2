"""Structural diff between two plan snapshots, for the revision audit trail."""

from __future__ import annotations

import copy
from typing import Any

from omni.agent.intent_plan import IntentPlan


def plan_diff(before: IntentPlan, after: IntentPlan) -> list[dict[str, Any]]:
    """Return a deterministic, audit-safe structural diff."""
    output: list[dict[str, Any]] = []
    _diff_value(before.to_dict(), after.to_dict(), "", output)
    return output


def _diff_value(
    before: Any,
    after: Any,
    path: str,
    output: list[dict[str, Any]],
) -> None:
    if type(before) is not type(after):
        output.append({"op": "replace", "path": path or "/", "before": before, "after": after})
        return
    if isinstance(before, dict):
        for key in sorted(set(before) | set(after)):
            child = f"{path}/{_escape_pointer(str(key))}"
            if key not in before:
                output.append({"op": "add", "path": child, "after": copy.deepcopy(after[key])})
            elif key not in after:
                output.append({"op": "remove", "path": child, "before": copy.deepcopy(before[key])})
            else:
                _diff_value(before[key], after[key], child, output)
        return
    if before != after:
        output.append(
            {
                "op": "replace",
                "path": path or "/",
                "before": copy.deepcopy(before),
                "after": copy.deepcopy(after),
            }
        )


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


__all__ = ["plan_diff"]
