"""What a turn carries forward when its planned route fails.

A high-confidence plan can commit the whole turn to one skill. When that skill
fails, the turn does not end: it falls through to ReAct so the model can reach
the goal another way (the degradation shape Codex/OpenCode/OpenClaw share —
report the failure to the model, let the model choose the detour). Falling
through is only useful if the second attempt inherits two things from the first:
knowledge of what already failed, and a tool surface wide enough to route around
it. This module supplies both.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from omni.skills_runtime.admission import admission_fallthrough_lines, first_admission_result

# A provider result can contain a full report or stack trace. The fallback needs
# the outcome and reusable paths, not a second transcript.
_MAX_RESULT_DETAIL = 600
_MAX_ARTIFACT_REFS = 8


def _is_failed_attempt(attempt: Any) -> bool:
    """True when a runner ran something, lost, and handed the turn back."""
    return attempt is not None and not getattr(attempt, "handled", True)


def policy_after_failed_route(policy: Any, attempt: Any) -> Any:
    """Widen a spent single-route policy back to the runtime's own limits.

    A single-skill plan narrows the turn to exactly that skill: no tools, one
    call. That is the right shape while the skill is the plan, and the wrong one
    the moment it fails, because it leaves the model holding an empty catalog and
    a budget of one — unable to do the very thing the fall-through exists for.
    The deny list survives, since it states what this turn may never touch, and
    the approval gate remains the outer guard.
    """
    if not _is_failed_attempt(attempt):
        return policy
    return replace(policy, allowed_tools=None, max_tool_calls=None, max_iterations=None)


def history_with_failed_attempt(
    history: list[dict[str, Any]], attempt: Any
) -> list[dict[str, Any]]:
    """Append the route the plan already tried and lost, so ReAct starts informed.

    Falling through hands the model a second chance, but a second chance with no
    memory of the first is just the same attempt again: the plan's skill is still
    the obvious choice for the request, so the model re-runs it and re-earns the
    same failure. Stating what was tried and how it failed turns a silent retry
    into an informed detour.
    """
    if not _is_failed_attempt(attempt):
        return history
    lines: list[str] = []
    for item in getattr(attempt, "drained_results", []) or []:
        skill = str(item.get("skill") or "a skill")
        status = str(item.get("status") or "incomplete")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        detail = " ".join(str(item.get("error") or "").split())
        if not detail:
            detail = " ".join(
                str(
                    result.get("summary")
                    or result.get("message")
                    or result.get("text")
                    or "returned no diagnostic message"
                ).split()
            )
        lines.append(f"- `{skill}` ({status}): {detail[:_MAX_RESULT_DETAIL]}")
        artifact_lines: list[str] = []
        for artifact in result.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            uri = str(artifact.get("uri") or "")
            path = str(artifact.get("path") or "")
            if uri or path:
                artifact_lines.append(path or uri)
        for location in artifact_lines[:_MAX_ARTIFACT_REFS]:
            lines.append(f"  usable output: `{location}`")
        admission = first_admission_result(result) or first_admission_result(item)
        if admission is not None:
            lines.extend(admission_fallthrough_lines(skill, admission))
    if not lines:
        return history
    return [
        *history,
        {
            "role": "user",
            "content": (
                "[system] Before this turn reached you, the plan ran the skill it "
                "judged best for this request and it did not fully complete:\n"
                + "\n".join(lines)
                + "\nDo not simply re-run it. Treat any usable outputs above as "
                "evidence: inspect and continue them instead of recreating them. "
                "Reach the same goal another way with the tools you have, and say "
                "plainly in your answer which source you ended up using."
            ),
        },
    ]


def loop_result_with_failed_attempt(result: Any, attempt: Any) -> Any:
    """Keep the lost route on the ReAct trace so later judges still see it."""
    if not _is_failed_attempt(attempt):
        return result
    traces = list(getattr(attempt, "tool_trace", None) or [])
    if not traces:
        return result
    result.tool_trace = [*traces, *result.tool_trace]
    return result
