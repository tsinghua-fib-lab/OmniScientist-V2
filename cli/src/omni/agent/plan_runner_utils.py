"""Small shared helpers for deterministic plan runners."""

from __future__ import annotations

import inspect
from typing import Any

from omni.agent.capabilities import WRITING_DELIVERABLES
from omni.agent.intent_plan import IntentPlan
from omni.core.termination import execution_outcome_status

_RETRIEVE_ONLY_IGNORE = frozenset({"sources", "answer", "literature.search"})
_ARTIFACT_OUTPUTS = frozenset(
    {
        "artifact.figure",
        "artifact.pptx",
        "artifact.slides",
        "artifact.poster",
        "figure",
        "review",
        "response_letter",
    }
)


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
        f"skills: {skills}; contracts: {contract}; outputs: {declared_outputs(plan)}"
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


# Terminators in both scripts: a gap's question is written in the user's
# language, and a Latin full stop welded onto a sentence that already ended in
# "？" reads as a typo the host introduced.
_SENTENCE_END = (".", "?", "!", "。", "？", "！", ":", "：")

# Labels for the few field names the host itself emits, used only when the gap
# came with no words of its own.
_FIELD_LABELS = {
    "research_topic": "research topic",
    "deliverable_scope": "target deliverable or candidate paper/arXiv id",
    "topic": "specific topic",
}

# Gaps naming what the host cannot do rather than what a person can supply.
# No answer typed at a prompt installs a provider, so the question is
# unanswerable by construction — which is how a researcher who asked for
# protein structure prediction was invited to clarify "contracted providers".
_SYSTEM_OWNED_FIELDS = frozenset({"capability"})


def gap_question(item: Any) -> str:
    """What one missing input asks the user, or "" if it asks nothing.

    ``ask`` is written for a person and wins wherever it exists. ``reason`` is
    the fallback because that is where a model puts the question: the planner
    contract long offered it no ``ask`` field, so every gap the model declared
    itself carried its question there and the renderer dropped all of them
    (incident cff3eeda). Only grounding-gate gaps carry both, and there
    ``reason`` names the fabricated value for the event log — preferring ``ask``
    keeps that diagnostic off the screen.

    Falling back to ``reason`` is safe for the gaps a *model* wrote and unsafe
    for the gaps the host writes about itself, so the fallback stops at the
    system-owned fields. Every host gap meant for a person already sets
    ``ask``; one that sets only ``reason`` is addressed to the event log.

    A gap with none of the three says nothing the user can act on. Reporting
    that as a question is what produced "research goal and output scope" for a
    request whose goal and scope were never in doubt.
    """
    if not isinstance(item, dict):
        return ""
    field = str(item.get("field") or "")
    spoken = str(item.get("ask") or "").strip()
    if spoken:
        return spoken
    if field in _SYSTEM_OWNED_FIELDS:
        return ""
    return str(item.get("reason") or "").strip() or _FIELD_LABELS.get(field, "")


def gap_default(item: Any) -> str:
    """The value the turn will use for one gap when nobody answers it.

    A gap that carries one is not a reason to stop. Its presence is the model's
    own statement that it knows what to do here and is naming the choice rather
    than making it invisibly.
    """
    if not isinstance(item, dict):
        return ""
    return str(item.get("default") or "").strip()


def assumption_block(missing: list[dict[str, Any]]) -> str:
    """Tell the turn what it is assuming and require it to own the assumptions.

    Proceeding on an unstated guess is worse than the question it replaced: the
    user cannot correct what they cannot see. Codex pairs the same
    assumption-first default with an answer that lists "open questions or
    assumptions", and Plan mode spells out the pairing — an unanswered question
    means "proceed with the recommended option and record it as an assumption
    in the final plan".
    """
    assumed = [
        (str(item.get("field") or "").strip() or "unspecified", gap_default(item))
        for item in missing
        if isinstance(item, dict) and gap_default(item)
    ]
    if not assumed:
        return ""
    lines = "\n".join(f"- {field}: {value}" for field, value in assumed)
    return (
        "[Assumptions] The request did not specify the following, and rather than "
        "stop to ask, this turn proceeds on the values below. Use them, and state "
        "them plainly in your final answer so the user can correct any that are "
        f"wrong:\n{lines}"
    )


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
    asks: list[str] = []
    for item in missing:
        spoken = gap_question(item)
        if spoken and spoken not in asks:
            asks.append(spoken)
    if not asks:
        return "I need a little more information before continuing: research goal and output scope."
    body = asks[0] if len(asks) == 1 else "\n" + "\n".join(f"- {ask}" for ask in asks[:3])
    text = f"I need a little more information before continuing: {body}"
    return text if text.rstrip().endswith(_SENTENCE_END) else f"{text}."


def declared_outputs(plan: IntentPlan) -> str:
    required = plan.verification_plan.required_outputs
    return ", ".join(required) if required else "not specified"


def settlement_status(drained: list[dict[str, Any]]) -> str:
    if not drained:
        return "not_applicable"
    statuses = [str(item.get("status") or "") for item in drained]
    if statuses and all(status == "succeeded" for status in statuses):
        return "succeeded"
    if any(status in {"succeeded", "degraded", "partial", "warning"} for status in statuses):
        return "degraded"
    return "failed"


def _source_ids_from_result(result: dict[str, Any]) -> list[str]:
    """Stable source identifiers from a literature skill/tool payload."""
    sources = result.get("sources")
    if isinstance(sources, list):
        ids = [
            str(item.get("source_id") or "").strip()
            for item in sources
            if isinstance(item, dict)
        ]
        ids = [item for item in ids if item]
        if ids:
            return ids
    raw = result.get("source_ids")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def is_retrieve_only_plan(plan: IntentPlan) -> bool:
    """True when the contracted deliverable is sources, not a manuscript or figure."""
    required = [str(item).strip() for item in plan.verification_plan.required_outputs if str(item).strip()]
    if "sources" not in required:
        return False
    named = {
        str(item).strip()
        for item in (*plan.outputs, *required)
        if str(item).strip()
    }
    leftover = named - _RETRIEVE_ONLY_IGNORE
    return not (leftover & (WRITING_DELIVERABLES | _ARTIFACT_OUTPUTS))


def project_retrieve_answer(source_ids: list[str]) -> str:
    """Host-owned source-id list. The model prose is not the deliverable."""
    ids = [str(item).strip() for item in source_ids if str(item).strip()]
    return "\n".join(list(dict.fromkeys(ids)))


def apply_retrieve_only_projection(
    plan: IntentPlan,
    *,
    source_ids: list[str],
    model_text: str,
) -> str:
    """Replace retrieve-only model copy with the ledger source_id list."""
    if not is_retrieve_only_plan(plan):
        return model_text
    projected = project_retrieve_answer(source_ids)
    if projected:
        return projected
    return (
        "No matching sources were found. "
        "I did not invent identifiers or substitute a narrative summary."
    )


def delivered_skill_answer(drained: list[dict[str, Any]]) -> str:
    """Return the completed skill body, never a submission receipt.

    Codex keeps a tool result in the turn until the model can speak from it.
    Omni's ``single_skill_task`` runner already waits when ``drain_tasks`` is
    on, but used to replace that body with ``Created execution``. Structured
    literature hits project ``source_id`` before a title ``summary`` so a
    retrieve-only turn cannot settle on venue lines (P1-02).
    """
    for item in drained:
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        source_ids = _source_ids_from_result(result)
        if source_ids:
            return "\n".join(source_ids)
        for key in ("text", "summary", "message", "title"):
            value = str(result.get(key) or "").strip()
            if value:
                return value
    return ""


def completed_skill_answer(
    drained: list[dict[str, Any]],
    *,
    skill: str,
) -> str:
    """Foreground copy after a drained skill: the result, or a completion line."""
    body = delivered_skill_answer(drained)
    if body:
        return body
    status = settlement_status(drained)
    if status == "degraded":
        item = drained[0]
        sid = str(item.get("subtask_id") or item.get("object_id") or "").strip()
        err = str(item.get("error") or "").strip()
        line = f"`{skill}` ended degraded"
        if sid:
            line += f" (execution `{sid[:8]}`)"
        if err:
            line += f": {err}"
        if sid:
            line += f" Retry with `/task retry {sid[:8]}` or inspect `/task show {sid[:8]}`."
        else:
            line += "."
        return line
    if drained and status != "failed":
        return f"`{skill}` completed."
    return ""
