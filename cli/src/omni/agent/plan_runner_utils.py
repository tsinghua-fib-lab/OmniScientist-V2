"""Small shared helpers for deterministic plan runners."""

from __future__ import annotations

import inspect
from typing import Any

from omni.agent.intent_plan import IntentPlan
from omni.core.termination import execution_outcome_status


async def emit_tool_event(callback: Any, phase: str, data: dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(phase, data)
    if inspect.isawaitable(result):
        await result


def plan_capabilities(plan: IntentPlan) -> list[str]:
    out: list[str] = []
    for selection in plan.selected_skills:
        for capability in selection.matched_capabilities:
            if capability not in out:
                out.append(capability)
    for output in plan.outputs:
        if output not in out:
            out.append(output)
    for step in plan.workflow_steps:
        capability = str(step.get("capability") or "")
        if capability and capability not in out:
            out.append(capability)
    return out


def plan_summary(plan: IntentPlan) -> str:
    skills = ", ".join(sel.skill for sel in plan.selected_skills) or "none"
    contract = ", ".join(f"{sel.skill}:{sel.contract_level}" for sel in plan.selected_skills) or "n/a"
    return (
        f"Plan: {plan.intent_type.value}; reason: {plan.rationale}; "
        f"skills: {skills}; contracts: {contract}; verification: {verification_expectation(plan)}"
    )


def approval_tools_for_plan(plan: IntentPlan, registry: Any) -> list[str]:
    """Resolve the narrow consent scope declared by a persisted plan."""
    from omni.core.approval import SENSITIVE_TOOLS
    from omni.runtime.execution_policy import skill_requires_approval

    blocked = set(plan.tool_policy.blocked_tools or [])
    allowed = set(plan.tool_policy.allowed_tools or [])
    grants = (allowed & set(SENSITIVE_TOOLS)) - blocked
    skill_refs = {
        (
            item.skill,
            str(getattr(item, "skill_source", "") or ""),
        )
        for item in plan.selected_skills
        if item.skill
    }
    skill_refs.update(
        (
            str(step.get("skill_name") or ""),
            str(
                step.get("skill_source")
                or (
                    step.get("input", {}).get("_skill_source")
                    if isinstance(step.get("input"), dict)
                    else ""
                )
                or ""
            ),
        )
        for step in plan.workflow_steps
        if step.get("skill_name")
    )
    for name, source in skill_refs:
        entry = (
            registry.resolve_ref(name, source)
            if hasattr(registry, "resolve_ref")
            else registry.get(name)
        )
        if entry is None:
            continue
        if skill_requires_approval(entry):
            grants.add(entry.name)
        grants.update(set(entry.allowed_tools or []) & set(SENSITIVE_TOOLS))
    return sorted(grants)


def result_brief(result: Any, error: str = "", n: int = 70) -> str:
    """One-line human summary of a tool/task result for events and displays."""
    if error:
        return f"failed: {str(error)[:n]}"
    if isinstance(result, dict):
        for key in ("summary", "title", "text", "abstract", "message"):
            if result.get(key):
                return str(result[key])[:n]
        if result.get("status"):
            return f"status={result['status']}"
    if result not in (None, ""):
        return str(result)[:n]
    return "done"


def workflow_terminal_message(result: dict[str, Any], task_id: str) -> str:
    """Render a recoverable workflow terminal summary."""
    summary = str(result.get("summary") or "The workflow did not fully complete; recoverable results were retained.")
    error = str(result.get("error") or "")
    hint = ""
    if task_id:
        hint = (
            f"\nUse /task show {task_id[:8]} to inspect each step, "
            f"or /task show {task_id[:8]} --json for the complete record."
        )
    failure = f"\nFailure: {error}" if error else ""
    return f"{summary}{failure}{hint}"


def last_tool_step(result: Any) -> str:
    """Return a compact description of the last ReAct tool observation."""
    if not result.tool_trace:
        return ""
    last = result.tool_trace[-1]
    return f"{last.name}: {last.error[:180]}" if last.error else last.name


def loop_result_event(result: Any) -> tuple[str, dict[str, Any]]:
    """Project one loop result into the canonical run-event status and payload."""
    status = (
        "needs_input"
        if result.kind == "needs_input"
        else execution_outcome_status(result.kind, result.terminated_reason)
    )
    return status, {
        "kind": result.kind,
        "terminated_reason": result.terminated_reason,
        "iterations": result.total_iterations,
        "tool_call_count": result.total_tool_calls,
        "tool_names": result.tool_names(),
        "tool_budget": result.tool_budget,
        "usage_budget": result.usage_budget,
        "transcript_repairs": result.transcript_repairs,
    }


def needs_input_text(missing: list[dict[str, Any]]) -> str:
    """Render a clarifying-question message from a plan's missing_inputs."""
    if any(item.get("field") == "paper_target" for item in missing if isinstance(item, dict)):
        candidates: list[dict[str, Any]] = []
        for item in missing:
            if isinstance(item, dict) and item.get("field") == "paper_target":
                candidates = [c for c in item.get("candidates") or [] if isinstance(c, dict)]
                break
        if candidates:
            lines = [
                "I need to confirm which paper should be analyzed. The current context has multiple candidates:",
                *[
                    f"- {idx}. {str(candidate.get('label') or candidate.get('value') or '')}"
                    for idx, candidate in enumerate(candidates[:5], start=1)
                ],
                "Reply with a number, or provide an arXiv id, DOI, URL, or PDF path.",
            ]
            return "\n".join(lines)
        return (
            "I need a paper target before continuing. Provide an arXiv id, DOI, paper URL, PDF path, "
            "or paste the paper content."
        )
    asks = []
    if any(item.get("field") == "research_topic" for item in missing if isinstance(item, dict)):
        asks.append("research topic")
    if any(item.get("field") == "deliverable_scope" for item in missing if isinstance(item, dict)):
        asks.append("target deliverable or candidate paper/arXiv id")
    if any(item.get("field") == "topic" for item in missing if isinstance(item, dict)):
        asks.append("specific topic")
    # A single missing field surfaced by the recovery ladder (e.g. an arXiv id):
    # ask concretely so the user can unblock in one line.
    for item in missing:
        if isinstance(item, dict) and item.get("field") and item.get("reason") and not asks:
            asks.append(str(item.get("reason")))
    return (
        "I need a little more information before continuing: "
        + (", ".join(asks) if asks else "research goal and output scope")
        + "."
    )


def verification_expectation(plan: IntentPlan) -> str:
    required = plan.verification_plan.required_outputs
    return ", ".join(required) if required else "not specified"


def verification_status(drained: list[dict[str, Any]]) -> str:
    if not drained:
        return "not_applicable"
    if all(item.get("status") == "succeeded" for item in drained):
        return "passed"
    if any(item.get("status") == "succeeded" for item in drained):
        return "degraded"
    return "failed"
